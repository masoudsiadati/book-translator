import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import (
    DecisionSource,
    ProviderName,
    SnapshotResourceKind,
    TerminologyFinding,
    TerminologyRegistry,
    TerminologyType,
    TerminologyValidationReport,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationQAReport,
)
from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
from book_translator.terminology import save_registry, set_decision
from book_translator.terminology import validation as qa_validation
from book_translator.translation_queue import create_run, load_run_summary, plan_run
from book_translator.translation_reevaluation import (
    TranslationReevaluationError,
    inspect_translation_reevaluations,
    reevaluate_translation_job,
)
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import WorkerOptions, run_translation_worker
from tests.test_translation_queue import NOW, SNAPSHOT_NOW
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import SyntheticProvider

runner = CliRunner()


def _options() -> WorkerOptions:
    return WorkerOptions(
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="worker-test",
        max_jobs=1,
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
    )


def _run_with_alias_registry(root: Path) -> str:
    synthetic_project(root)
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )
    registry = set_decision(
        registry,
        source_term="Alpha Person",
        target_term="آلفا شخص",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=True,
        aliases=("Alpha",),
    )
    save_registry(root / "config/terminology.yaml", registry)
    (root / "config/terminology.metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "resource_kind": SnapshotResourceKind.TERMINOLOGY.value,
                "resource_file": "config/terminology.yaml",
                "version": "1.0.0",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    created = create_run(plan_run(snapshot.snapshot_id, project_root=root, now=lambda: NOW))
    execute_prompt_packages(
        plan_prompt_packages(created.run_id, project_root=root, now=lambda: NOW)
    )
    return created.run_id


def _historically_rejected_alias_run(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, warning: bool = False
) -> tuple[str, str]:
    run_id = _run_with_alias_registry(root)

    def old_alias_qa(source: str, translation: str, registry: TerminologyRegistry) -> object:
        return TerminologyValidationReport(
            schema_version="1.0",
            valid=False,
            source_sha256=hashlib.sha256(source.encode()).hexdigest(),
            translation_sha256=hashlib.sha256(translation.encode()).hexdigest(),
            checked_decisions=1,
            findings=[
                TerminologyFinding(
                    code="canonical_term_missing",
                    severity="error",
                    source_term="Alpha Person",
                    message="synthetic historical alias regression",
                )
            ],
        )

    monkeypatch.setattr("book_translator.translation_worker.validate_terminology", old_alias_qa)
    translation = "آلفا آمد.  \n\nبند دوم.\n" if warning else "آلفا آمد.\n\nبند دوم.\n"
    run_translation_worker(
        run_id,
        project_root=root,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider(translation),
        now=lambda: NOW + timedelta(seconds=10),
    )
    monkeypatch.undo()
    state = load_run_summary(run_id, project_root=root)
    return run_id, state.jobs[0].job_id


def _artifact_bytes(root: Path, relative: Path) -> bytes:
    return (root / relative).read_bytes()


def test_rejected_becomes_accepted_without_changing_history_or_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, job_id = _historically_rejected_alias_run(tmp_path, monkeypatch)
    before = load_run_summary(run_id, project_root=tmp_path)
    job = before.jobs[0]
    assert job.acceptance_file is not None
    acceptance_before = _artifact_bytes(tmp_path, job.acceptance_file)
    attempt_dir = before.path / "jobs" / job.chunk_id / "attempts/001"
    attempt_before = {path.name: path.read_bytes() for path in attempt_dir.iterdir()}

    result = reevaluate_translation_job(
        run_id,
        job_id,
        1,
        project_root=tmp_path,
        now=lambda: NOW + timedelta(seconds=20),
    )

    after = load_run_summary(run_id, project_root=tmp_path)
    changed = after.jobs[0]
    assert result.resulting_status is TranslationAcceptanceStatus.ACCEPTED
    assert changed.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert changed.acceptance_origin is TranslationAcceptanceOrigin.QA_REEVALUATION
    assert after.run.accepted_count == 1 and after.run.qa_rejected_count == 0
    assert _artifact_bytes(tmp_path, job.acceptance_file) == acceptance_before
    assert {path.name: path.read_bytes() for path in attempt_dir.iterdir()} == attempt_before


def test_dry_run_and_warning_only_result_are_accepted_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, job_id = _historically_rejected_alias_run(tmp_path, monkeypatch, warning=True)
    run_path = tmp_path / "workspace/translation-runs" / run_id
    before = {path: path.read_bytes() for path in run_path.rglob("*") if path.is_file()}

    result = reevaluate_translation_job(run_id, job_id, 1, project_root=tmp_path, dry_run=True)

    after = {path: path.read_bytes() for path in run_path.rglob("*") if path.is_file()}
    assert result.resulting_status is TranslationAcceptanceStatus.ACCEPTED
    assert result.structural_finding_codes == ("trailing_double_space",)
    assert before == after
    assert not (run_path / "jobs/001-001/reevaluations").exists()


def test_mutable_terminology_is_ignored_and_frozen_registry_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, job_id = _historically_rejected_alias_run(tmp_path, monkeypatch)
    mutable = TerminologyRegistry(
        schema_version="1.0",
        version="9.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )
    mutable = set_decision(
        mutable,
        source_term="Alpha",
        target_term="مقدار متفاوت",
        classification=TerminologyType.STORY_TERM,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    save_registry(tmp_path / "config/terminology.yaml", mutable)

    result = reevaluate_translation_job(run_id, job_id, 1, project_root=tmp_path, dry_run=True)

    assert result.resulting_status is TranslationAcceptanceStatus.ACCEPTED
    assert not result.terminology_finding_codes


def test_idempotent_reuse_and_sequential_engine_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, job_id = _historically_rejected_alias_run(tmp_path, monkeypatch)
    first = reevaluate_translation_job(run_id, job_id, 1, project_root=tmp_path)
    state = load_run_summary(run_id, project_root=tmp_path)
    job_path = state.path / "jobs" / state.jobs[0].chunk_id / "job.yaml"
    run_path = state.path / "run.yaml"
    before = (job_path.read_bytes(), run_path.read_bytes())
    duplicate = reevaluate_translation_job(run_id, job_id, 1, project_root=tmp_path)
    assert duplicate.reused and duplicate.reevaluation_id == first.reevaluation_id
    assert (job_path.read_bytes(), run_path.read_bytes()) == before

    # Derive the simulated bump from the real version so this never silently no-ops when the QA
    # engine is itself versioned forward.
    major, minor, patch = (int(part) for part in qa_validation.TERMINOLOGY_QA_VERSION.split("."))
    monkeypatch.setattr(qa_validation, "TERMINOLOGY_QA_VERSION", f"{major}.{minor + 1}.{patch}")
    second = reevaluate_translation_job(run_id, job_id, 1, project_root=tmp_path)
    history = inspect_translation_reevaluations(run_id, job_id, project_root=tmp_path)
    assert second.reevaluation_number == 2
    assert second.reevaluation_id != first.reevaluation_id
    assert len(history.records) == 2


@pytest.mark.parametrize(
    "corruption", ["attempt", "source", "terminology", "prompt", "style", "acceptance"]
)
def test_corrupted_immutable_inputs_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    run_id, job_id = _historically_rejected_alias_run(tmp_path, monkeypatch)
    state = load_run_summary(run_id, project_root=tmp_path)
    job = state.jobs[0]
    assert job.acceptance_file is not None
    snapshot = tmp_path / state.run.snapshot_path
    manifest = yaml.safe_load((snapshot / "manifest.yaml").read_text(encoding="utf-8"))
    paths = {
        "attempt": state.path / "jobs" / job.chunk_id / "attempts/001/attempt.yaml",
        "source": tmp_path / job.chunk_source_file,
        "terminology": tmp_path / manifest["terminology"]["frozen_file"],
        "prompt": tmp_path / manifest["prompt"]["file"],
        "style": tmp_path / manifest["style_guide"]["file"],
        "acceptance": tmp_path / job.acceptance_file,
    }
    paths[corruption].write_bytes(paths[corruption].read_bytes() + b"corrupt")

    with pytest.raises((TranslationReevaluationError, ValueError)):
        reevaluate_translation_job(run_id, job_id, 1, project_root=tmp_path, dry_run=True)


def test_missing_historical_acceptance_is_rejected_without_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, job_id = _historically_rejected_alias_run(tmp_path, monkeypatch)
    state = load_run_summary(run_id, project_root=tmp_path)
    job = state.jobs[0]
    assert job.acceptance_file is not None
    (tmp_path / job.acceptance_file).rename(tmp_path / "missing-evidence.yaml")

    with pytest.raises(TranslationReevaluationError):
        reevaluate_translation_job(run_id, job_id, 1, project_root=tmp_path, dry_run=True)


def test_historical_accepted_can_be_reevaluated_rejected_by_current_qa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _run_with_alias_registry(tmp_path)

    def historically_lenient(source: str, translation: str, *, target_language: str) -> object:
        source_hash = hashlib.sha256(source.encode()).hexdigest()
        translation_hash = hashlib.sha256(translation.encode()).hexdigest()
        return TranslationQAReport(
            schema_version="1.0",
            valid=True,
            source_sha256=source_hash,
            translation_sha256=translation_hash,
            source_paragraph_count=2,
            translation_paragraph_count=2,
            findings=[],
        )

    monkeypatch.setattr(
        "book_translator.translation_worker.validate_translation_qa", historically_lenient
    )
    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("فقط یک بند.\n"),
        now=lambda: NOW + timedelta(seconds=10),
    )
    monkeypatch.undo()
    state = load_run_summary(run_id, project_root=tmp_path)
    job = state.jobs[0]
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED

    result = reevaluate_translation_job(run_id, job.job_id, 1, project_root=tmp_path)

    assert result.resulting_status is TranslationAcceptanceStatus.QA_REJECTED
    assert result.structural_finding_codes == ("paragraph_count_mismatch",)
    current = load_run_summary(run_id, project_root=tmp_path)
    assert current.run.accepted_count == 0 and current.run.qa_rejected_count == 1


def test_published_evidence_is_adopted_after_pointer_update_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, job_id = _historically_rejected_alias_run(tmp_path, monkeypatch)
    reevaluate_translation_job(run_id, job_id, 1, project_root=tmp_path)
    state = load_run_summary(run_id, project_root=tmp_path)
    job_path = state.path / "jobs" / state.jobs[0].chunk_id / "job.yaml"
    job_data = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    job_data.update(
        acceptance_status="qa_rejected",
        acceptance_origin="initial_qa",
        reevaluation_count=0,
        latest_reevaluation_file=None,
        latest_reevaluation_sha256=None,
    )
    job_path.write_text(
        yaml.safe_dump(job_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    run_path = state.path / "run.yaml"
    run_data = yaml.safe_load(run_path.read_text(encoding="utf-8"))
    run_data.update(accepted_count=0, qa_rejected_count=1, status="completed_with_failures")
    run_path.write_text(
        yaml.safe_dump(run_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    adopted = reevaluate_translation_job(run_id, job_id, 1, project_root=tmp_path)

    assert adopted.reused
    current = load_run_summary(run_id, project_root=tmp_path)
    assert current.jobs[0].reevaluation_count == 1
    assert current.run.accepted_count == 1 and current.run.qa_rejected_count == 0


def test_inspection_cli_shows_resolution_without_translation_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, job_id = _historically_rejected_alias_run(tmp_path, monkeypatch)
    reevaluate_translation_job(run_id, job_id, 1, project_root=tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["show-translation-reevaluations", run_id, job_id])

    assert result.exit_code == 0
    assert "Historical acceptance" in result.stdout
    assert "Effective acceptance" in result.stdout
    assert "qa_reevaluation" in result.stdout
    assert "آلفا آمد" not in result.stdout
