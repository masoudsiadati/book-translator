from datetime import timedelta
from pathlib import Path

import pytest

from book_translator.models import (
    ProviderName,
    TranslationAcceptanceStatus,
    TranslationExecutionKind,
)
from book_translator.provider_execution import list_attempts
from book_translator.translation_queue import load_run_summary
from book_translator.translation_repair import TranslationRepairError, repair_translation_job
from book_translator.translation_worker import run_translation_worker
from tests.test_translation_queue import NOW
from tests.test_translation_worker import (
    SyntheticProvider,
    _options,
    _prepared_run,
    _terminology_run,
)


def _rejected(root: Path, *, max_attempts: int = 3) -> tuple[str, str]:
    run_id = _terminology_run(root)
    if max_attempts != 3:
        state = load_run_summary(run_id, project_root=root)
        # The helper creates the canonical default; tests requiring exhaustion use attempt 3 below.
        assert state.run.max_attempts == 3
    run_translation_worker(
        run_id,
        project_root=root,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("فقط یک بند.‌\n"),
        now=lambda: NOW + timedelta(seconds=10),
    )
    job = load_run_summary(run_id, project_root=root).jobs[0]
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    return run_id, job.job_id


def test_repair_fixes_terminology_and_structure_and_preserves_history(tmp_path: Path) -> None:
    run_id, job_id = _rejected(tmp_path)
    state = load_run_summary(run_id, project_root=tmp_path)
    original_attempt = state.path / "jobs" / state.jobs[0].chunk_id / "attempts/001"
    original_evidence = state.path / "jobs" / state.jobs[0].chunk_id / "acceptance/001"
    before_attempt = {path.name: path.read_bytes() for path in original_attempt.iterdir()}
    before_evidence = {path.name: path.read_bytes() for path in original_evidence.iterdir()}
    calls: list[str] = []

    result = repair_translation_job(
        run_id,
        job_id,
        project_root=tmp_path,
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="repair-worker",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        provider_adapter=SyntheticProvider("آلفا  در بند نخست.\n\nبند دوم.\n", calls),
        now=lambda: NOW + timedelta(seconds=20),
    )

    assert calls == [job_id]
    assert result.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    attempts = list_attempts(run_id, job_id, project_root=tmp_path)
    assert attempts[1].attempt.execution_kind is TranslationExecutionKind.REPAIR
    assert attempts[1].attempt.repaired_from_attempt == 1
    assert {path.name: path.read_bytes() for path in original_attempt.iterdir()} == before_attempt
    assert {path.name: path.read_bytes() for path in original_evidence.iterdir()} == before_evidence
    repaired = load_run_summary(run_id, project_root=tmp_path).jobs[0]
    assert repaired.accepted_attempt == 2
    assert repaired.acceptance_file is not None
    assert repaired.acceptance_file.as_posix().endswith("acceptance/002/evidence.yaml")


@pytest.mark.parametrize("defect", ["terminology", "structural"])
def test_repair_fixes_each_finding_class_independently(tmp_path: Path, defect: str) -> None:
    run_id = _terminology_run(tmp_path) if defect == "terminology" else _prepared_run(tmp_path)
    rejected_text = "ترجمه نخست.\n\nترجمه دوم.\n" if defect == "terminology" else "فقط یک بند.\n"
    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider(rejected_text),
        now=lambda: NOW + timedelta(seconds=10),
    )
    job_id = load_run_summary(run_id, project_root=tmp_path).jobs[0].job_id
    repaired_text = (
        "آلفا در بند نخست.\n\nبند دوم.\n" if defect == "terminology" else "بند نخست.\n\nبند دوم.\n"
    )
    result = repair_translation_job(
        run_id,
        job_id,
        project_root=tmp_path,
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="repair-worker",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        provider_adapter=SyntheticProvider(repaired_text),
        now=lambda: NOW + timedelta(seconds=20),
    )
    assert result.acceptance_status is TranslationAcceptanceStatus.ACCEPTED


def test_failed_repair_remains_qa_rejected_and_is_bounded(tmp_path: Path) -> None:
    run_id, job_id = _rejected(tmp_path)
    result = repair_translation_job(
        run_id,
        job_id,
        project_root=tmp_path,
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="repair-worker",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        provider_adapter=SyntheticProvider("هنوز فقط یک بند.\n"),
        now=lambda: NOW + timedelta(seconds=20),
    )
    assert result.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert load_run_summary(run_id, project_root=tmp_path).jobs[0].accepted_attempt == 2


def test_repair_dry_run_has_no_mutation_or_provider_call(tmp_path: Path) -> None:
    run_id, job_id = _rejected(tmp_path)
    state = load_run_summary(run_id, project_root=tmp_path)
    job_path = state.path / "jobs" / state.jobs[0].chunk_id / "job.yaml"
    before = job_path.read_bytes()
    calls: list[str] = []
    result = repair_translation_job(
        run_id,
        job_id,
        project_root=tmp_path,
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="repair-worker",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        dry_run=True,
        provider_adapter=SyntheticProvider("unused", calls),
        now=lambda: NOW + timedelta(seconds=20),
    )
    assert result.dry_run and not result.provider_contacted
    assert calls == [] and job_path.read_bytes() == before


def test_repair_respects_maximum_attempts(tmp_path: Path) -> None:
    run_id, job_id = _rejected(tmp_path)
    repair_translation_job(
        run_id,
        job_id,
        project_root=tmp_path,
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="repair-worker",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        provider_adapter=SyntheticProvider("هنوز فقط یک بند.\n"),
        now=lambda: NOW + timedelta(seconds=20),
    )
    repair_translation_job(
        run_id,
        job_id,
        project_root=tmp_path,
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="repair-worker",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        provider_adapter=SyntheticProvider("باز هم فقط یک بند.\n"),
        now=lambda: NOW + timedelta(seconds=30),
    )
    with pytest.raises(TranslationRepairError, match="exhausted"):
        repair_translation_job(
            run_id,
            job_id,
            project_root=tmp_path,
            provider=ProviderName.FAKE,
            model="fake-v1",
            worker_id="repair-worker",
            lease_seconds=900,
            timeout_seconds=120,
            max_output_tokens=1000,
            dry_run=True,
        )
