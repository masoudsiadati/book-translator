"""The release-time semantic-gate waiver: explicit, scoped, and never a substitute for a PASS."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from book_translator.models import (
    ReleaseManifest,
    ReleaseSemanticWaiver,
    ReleaseTranslationReport,
    SemanticGateStatus,
    TranslationJob,
)
from book_translator.release_assembly import ReleaseAssemblyError
from book_translator.release_assembly.waiver import (
    RELEASE_WAIVER_VERSION,
    ReleaseWaiverError,
    semantic_coverage,
    waiver_for,
    waiver_refusals,
)
from book_translator.release_assembly.waiver_workflow import (
    execute_release_waiver,
    load_release_waiver,
    plan_release_waiver,
)
from book_translator.release_assembly.workflow import (
    execute_release_assembly,
    plan_release_assembly,
)
from book_translator.translation_queue.workflow import _load_queue, _resolve_run_path
from tests.test_release_assembly import RELEASE_CLOCK
from tests.test_semantic_gate import ALL_PASS, ScriptedTransport, _gate, _job, _run
from tests.test_translation_worker import _run_path

REASON = "Operator-authorized publication without semantic-judge coverage"
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def _state(root: Path, run_id: str):  # type: ignore[no-untyped-def]
    return _load_queue(_resolve_run_path(run_id, root), root)


def _authorize(root: Path, run_id: str, *, reason: str = REASON, apply: bool = True):  # type: ignore[no-untyped-def]
    state = _state(root, run_id)
    plan = plan_release_waiver(
        state.run, tuple(state.jobs), project_root=root, reason=reason, now=lambda: NOW
    )
    if apply:
        execute_release_waiver(plan)
    return plan


def _export(root: Path, run_id: str, *, name: str = "book-fa-v1", **updates: object) -> Path:
    options: dict[str, object] = {
        "output": f"workspace/releases/{name}",
        "project_root": root,
        "now": lambda: RELEASE_CLOCK,
    }
    options.update(updates)
    plan = plan_release_assembly(run_id, **options)  # type: ignore[arg-type]
    execute_release_assembly(plan, project_root=root)
    return plan.output_path


# --- the gate still closes ----------------------------------------------------------------------


def test_without_a_waiver_an_unjudged_gated_run_is_still_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    with pytest.raises(ReleaseAssemblyError, match="no semantic acceptance"):
        _export(root, run_id)


def test_an_ungated_run_needs_no_waiver_and_cannot_have_one(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root, gated=False)

    _export(root, run_id)  # releases exactly as it always did
    with pytest.raises(ReleaseWaiverError, match="no semantic gate policy"):
        _authorize(root, run_id, apply=False)


# --- the waiver admits exactly what it says ------------------------------------------------------


def test_the_matching_waiver_admits_the_release_and_is_recorded_in_it(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    plan = _authorize(root, run_id)
    waiver = plan.waiver

    release = _export(root, run_id, semantic_waiver=waiver.waiver_id)

    manifest = ReleaseManifest.model_validate(
        yaml.safe_load((release / "manifest.yaml").read_text(encoding="utf-8"))
    )
    report = ReleaseTranslationReport.model_validate(
        yaml.safe_load((release / "translation-report.yaml").read_text(encoding="utf-8"))
    )
    assert manifest.semantic_waiver is not None
    assert manifest.semantic_waiver.waiver_id == waiver.waiver_id
    assert manifest.semantic_waiver.semantic_coverage == "waived_not_judged"
    assert manifest.semantic_waiver.not_judged == waiver.not_judged
    assert manifest.semantic_waiver.reason == REASON
    assert manifest.semantic_waiver.decided_by == "user"
    assert report.semantic_not_judged == waiver.not_judged
    assert report.semantic_judged_pass == 0
    assert report.semantic_waiver is not None


def test_the_waiver_never_becomes_a_semantic_pass(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    plan = _authorize(root, run_id)

    _export(root, run_id, semantic_waiver=plan.waiver.waiver_id)

    job = _job(root, run_id)
    assert job.semantic_status is None
    assert job.semantic_evidence_count == 0
    assert job.latest_semantic_file is None


def test_a_judged_run_needs_no_waiver(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    _gate(root, run_id, ScriptedTransport((ALL_PASS,)))
    assert _job(root, run_id).semantic_status is SemanticGateStatus.PASS

    with pytest.raises(ReleaseWaiverError, match="needs no waiver"):
        _authorize(root, run_id, apply=False)


# --- fail closed ---------------------------------------------------------------------------------


def test_a_waiver_for_another_run_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    state = _state(root, run_id)
    foreign_run = state.run.model_copy(update={"run_id": "tr-0123456789ab-001"})
    foreign = waiver_for(foreign_run, tuple(state.jobs), reason=REASON, created_at=NOW)

    refusals = waiver_refusals(foreign, state.run, tuple(state.jobs))
    assert "waiver_names_another_run:tr-0123456789ab-001" in refusals

    # And the release path refuses it end to end rather than only in the pure rule.
    path = root / "workspace/release-waivers" / f"{foreign.waiver_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(foreign.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(ReleaseWaiverError, match="waiver_names_another_run"):
        _export(root, run_id, semantic_waiver=foreign.waiver_id)


def test_a_waiver_for_another_snapshot_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    state = _state(root, run_id)
    elsewhere = state.run.model_copy(update={"snapshot_id": "translation-fa-" + "a" * 24})
    foreign = waiver_for(elsewhere, tuple(state.jobs), reason=REASON, created_at=NOW)

    refusals = waiver_refusals(foreign, state.run, tuple(state.jobs))

    assert any(item.startswith("waiver_names_another_snapshot") for item in refusals)


def test_a_tampered_waiver_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    plan = _authorize(root, run_id)
    path = plan.path
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["reason"] = "something else entirely"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

    with pytest.raises(ReleaseWaiverError, match="waiver_identity_mismatch"):
        _export(root, run_id, semantic_waiver=plan.waiver.waiver_id)


def test_a_missing_waiver_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    with pytest.raises(ReleaseWaiverError, match="unreadable release waiver"):
        _export(root, run_id, semantic_waiver="rw-" + "0" * 24)


def test_a_stale_waiver_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    plan = _authorize(root, run_id)
    # The run moved on after the waiver was authorized: the judge has now passed the job it
    # covered, so the counts it states are no longer this run's.
    _gate(root, run_id, ScriptedTransport((ALL_PASS,)))

    with pytest.raises(ReleaseWaiverError, match="waiver_is_stale"):
        _export(root, run_id, semantic_waiver=plan.waiver.waiver_id)


def test_planning_a_waiver_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    before = {p for p in (root / "workspace").rglob("*")}

    plan = _authorize(root, run_id, apply=False)

    assert not plan.path.exists()
    assert {p for p in (root / "workspace").rglob("*")} == before


def test_an_empty_reason_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    with pytest.raises(ReleaseWaiverError, match="records why"):
        _authorize(root, run_id, reason="   ", apply=False)


def test_republishing_an_identical_waiver_reuses_it(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    first = _authorize(root, run_id)
    before = first.path.read_bytes()

    again = _authorize(root, run_id)

    assert again.action == "skip"
    assert again.waiver.waiver_id == first.waiver.waiver_id
    assert first.path.read_bytes() == before
    assert load_release_waiver(first.path).content_sha256 == first.waiver.content_sha256


# --- a failed judgement is never waivable --------------------------------------------------------


def _judged(job: TranslationJob, status: SemanticGateStatus | None) -> TranslationJob:
    if status is None:
        return job
    return job.model_copy(
        update={
            "semantic_status": status,
            "semantic_evidence_count": 1,
            "latest_semantic_file": Path("workspace/semantic/evidence.yaml"),
            "latest_semantic_sha256": "0" * 64,
        }
    )


def test_a_judgement_that_failed_may_not_be_waived(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    state = _state(root, run_id)
    jobs = (_judged(state.jobs[0], SemanticGateStatus.UNRESOLVED),)

    coverage = semantic_coverage(jobs)
    assert coverage.judged_not_pass == 1
    with pytest.raises(ReleaseWaiverError, match="repaired, never waived"):
        waiver_for(state.run, jobs, reason=REASON, created_at=NOW)


def test_a_waiver_stops_admitting_once_a_job_is_judged_and_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    state = _state(root, run_id)
    waiver = waiver_for(state.run, tuple(state.jobs), reason=REASON, created_at=NOW)
    failed = (_judged(state.jobs[0], SemanticGateStatus.REVISE),)

    refusals = waiver_refusals(waiver, state.run, failed)

    assert any(item.startswith("judged_without_passing") for item in refusals)
    assert "waiver_is_stale" in refusals


def test_the_waiver_records_its_own_engine_version_and_scope(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    waiver = _authorize(root, run_id).waiver

    assert waiver.waiver_engine_version == RELEASE_WAIVER_VERSION
    assert waiver.waiver_kind == "semantic_gate"
    assert waiver.translation_run_id == run_id
    assert waiver.snapshot_id == _state(root, run_id).run.snapshot_id
    assert waiver.decided_by == "user"
    assert waiver.judged_not_pass == 0
    assert waiver.qa_rejected == 0
    assert isinstance(waiver, ReleaseSemanticWaiver)
    assert _run_path(root, run_id).is_dir()
