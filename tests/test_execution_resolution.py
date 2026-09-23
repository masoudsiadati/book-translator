from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator import execution_resolution
from book_translator.cli import app
from book_translator.execution_resolution import (
    EXECUTION_RESOLUTION_ENGINE_VERSION,
    ExecutionResolutionError,
    ReservationResolutionPlan,
    execute_reservation_resolution,
    inspect_execution_reservations,
    plan_reservation_resolution,
    planned_result,
    resolution_outcome,
)
from book_translator.models import (
    ExecutionReservationResolution,
    ExecutionReservationResolutionKind,
    ExecutionResolutionProvenance,
    ProviderName,
    TranslationAcceptanceStatus,
    TranslationAttemptStatus,
    TranslationJob,
    TranslationJobStatus,
)
from book_translator.provider_execution import ExecutionPlan, execute_translation_job
from book_translator.provider_execution.protocol import ProviderRequest, ProviderResult
from book_translator.provider_execution.providers.fake import FakeTranslationProvider
from book_translator.translation_queue import (
    QueueError,
    claim_job,
    load_run_summary,
    recover_run,
    retry_job,
)
from book_translator.translation_queue.policy import claim_ineligibility_reason, is_claimable
from book_translator.translation_repair import repair_translation_job
from book_translator.translation_worker import run_translation_worker
from tests.test_provider_execution import EXECUTION_NOW, _claimed_execution, _plan
from tests.test_translation_queue import NOW
from tests.test_translation_worker import (
    SyntheticProvider,
    _options,
    _prepared_run,
    _run_path,
    _terminology_run,
)

RESOLUTION_NOW = EXECUTION_NOW + timedelta(hours=2)
NOT_CONTACTED = ExecutionReservationResolutionKind.PROVIDER_NOT_CONTACTED
CONTACT_UNKNOWN = ExecutionReservationResolutionKind.PROVIDER_CONTACT_UNKNOWN
CONTACTED_NO_RESULT = ExecutionReservationResolutionKind.PROVIDER_CONTACTED_NO_RESULT
REASON = "synthetic worker died before any provider request could be issued"
WIDE = {"COLUMNS": "200"}
runner = CliRunner()


class _ProcessCrash(FakeTranslationProvider):
    def execute(self, request: ProviderRequest) -> ProviderResult:
        raise KeyboardInterrupt


def _requesting(root: Path, *, token: str = "secret-claim-token") -> tuple[str, Path, str, Path]:
    """Leave one job running behind an unresolved `requesting` reservation."""
    run_id, run_path, job_id, claim_token = _claimed_execution(root)
    plan = _plan(root, run_id, job_id, claim_token)
    with pytest.raises(KeyboardInterrupt):
        execute_translation_job(
            plan,
            worker_id="worker",
            claim_token=claim_token,
            provider=_ProcessCrash(),
            now=lambda: EXECUTION_NOW,
        )
    return run_id, run_path, job_id, plan.reservation_path


def _resolve(
    root: Path,
    run_id: str,
    job_id: str,
    kind: ExecutionReservationResolutionKind,
    *,
    attempt_number: int = 1,
    reason: str = REASON,
    now: Callable[[], datetime] = lambda: RESOLUTION_NOW,
) -> ReservationResolutionPlan:
    return plan_reservation_resolution(
        run_id,
        job_id,
        attempt_number=attempt_number,
        resolution=kind,
        reason=reason,
        project_root=root,
        now=now,
    )


def _apply(root: Path, plan: ReservationResolutionPlan) -> None:
    execute_reservation_resolution(plan, project_root=root, now=lambda: RESOLUTION_NOW)


def _job(root: Path, run_id: str) -> TranslationJob:
    return load_run_summary(run_id, project_root=root).jobs[0]


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _record(root: Path, plan: ReservationResolutionPlan) -> ExecutionReservationResolution:
    return ExecutionReservationResolution.model_validate(
        yaml.safe_load((root / plan.resolution_file).read_text(encoding="utf-8"))
    )


def test_provider_not_contacted_preserves_the_reservation_and_publishes_lineage(
    tmp_path: Path,
) -> None:
    run_id, _, job_id, reservation_path = _requesting(tmp_path)
    before = reservation_path.read_bytes()

    plan = _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)
    _apply(tmp_path, plan)

    record = _record(tmp_path, plan)
    assert reservation_path.read_bytes() == before
    assert record.resolution_id == plan.resolution_id
    assert record.provenance is ExecutionResolutionProvenance.OPERATOR
    assert record.resolver_engine_version == EXECUTION_RESOLUTION_ENGINE_VERSION
    assert record.reservation_status is TranslationAttemptStatus.REQUESTING
    assert record.provider is ProviderName.FAKE
    assert record.reason == REASON
    assert record.previous_job_status is TranslationJobStatus.RUNNING
    assert record.resulting_job_status is TranslationJobStatus.PENDING
    assert (record.previous_attempt_count, record.resulting_attempt_count) == (1, 1)
    assert (record.previous_abandoned_attempt_count, record.resulting_abandoned_attempt_count) == (
        0,
        1,
    )
    assert record.provider_attempt_consumed is False
    assert record.replay_permitted is True
    assert record.created_at == RESOLUTION_NOW


def test_resolution_restores_the_queue_without_spending_a_provider_attempt(tmp_path: Path) -> None:
    run_id, _, job_id, _ = _requesting(tmp_path)

    _apply(tmp_path, _resolve(tmp_path, run_id, job_id, NOT_CONTACTED))

    job = _job(tmp_path, run_id)
    assert job.status is TranslationJobStatus.PENDING
    assert (job.attempt_count, job.abandoned_attempt_count) == (1, 1)
    assert job.provider_attempt_count == 0
    assert job.claim_token is None and job.claimed_by is None
    assert is_claimable(job)
    assert job.recent_events[-1].event_type == "execution_reservation_resolved"


def test_replay_uses_a_new_attempt_slot_and_leaves_history_intact(tmp_path: Path) -> None:
    run_id, run_path, job_id, reservation_path = _requesting(tmp_path)
    preserved = reservation_path.read_bytes()
    _apply(tmp_path, _resolve(tmp_path, run_id, job_id, NOT_CONTACTED))

    claim = claim_job(
        run_id,
        project_root=tmp_path,
        worker_id="worker",
        now=lambda: RESOLUTION_NOW,
        token_factory=lambda: "second-claim-token",
    )
    assert (claim.job.attempt_count, claim.job.provider_attempt_count) == (2, 1)
    execute_translation_job(
        _plan(tmp_path, run_id, job_id, claim.claim_token, now=lambda: RESOLUTION_NOW),
        worker_id="worker",
        claim_token=claim.claim_token,
        now=lambda: RESOLUTION_NOW,
        monotonic=lambda: 1.0,
    )

    attempts = run_path / "jobs" / claim.job.chunk_id / "attempts"
    assert _job(tmp_path, run_id).status is TranslationJobStatus.SUCCEEDED
    assert (attempts / "002" / "attempt.yaml").is_file()
    assert not (attempts / "001").exists()
    assert reservation_path.read_bytes() == preserved


def test_abandoned_slots_preserve_the_full_provider_attempt_budget(tmp_path: Path) -> None:
    run_id, _, job_id, _ = _requesting(tmp_path)
    _apply(tmp_path, _resolve(tmp_path, run_id, job_id, NOT_CONTACTED))

    job = _job(tmp_path, run_id)
    exhausted = job.model_copy(
        update={"attempt_count": 4, "abandoned_attempt_count": 1, "status": job.status}
    )
    assert exhausted.provider_attempt_count == 3
    assert not is_claimable(exhausted)
    assert claim_ineligibility_reason(exhausted) == "attempt budget is exhausted (3/3)"
    with pytest.raises(ValueError, match="provider attempts must not exceed"):
        job.model_copy(update={"attempt_count": 5, "abandoned_attempt_count": 1}).model_validate(
            job.model_copy(update={"attempt_count": 5, "abandoned_attempt_count": 1}).model_dump()
        )
    with pytest.raises(ValueError, match="abandoned_attempt_count"):
        TranslationJob.model_validate(
            job.model_copy(update={"attempt_count": 1, "abandoned_attempt_count": 2}).model_dump()
        )


def test_legacy_jobs_without_the_counter_load_as_fully_charged(tmp_path: Path) -> None:
    run_id, run_path, job_id, _ = _requesting(tmp_path)
    path = run_path / "jobs" / _job(tmp_path, run_id).chunk_id / "job.yaml"
    legacy = yaml.safe_load(path.read_text(encoding="utf-8"))
    del legacy["abandoned_attempt_count"]
    path.write_text(yaml.safe_dump(legacy, sort_keys=False), encoding="utf-8")

    job = _job(tmp_path, run_id)

    assert job.abandoned_attempt_count == 0
    assert job.provider_attempt_count == job.attempt_count == 1


def test_automatic_recovery_blocks_until_an_explicit_safe_resolution_exists(tmp_path: Path) -> None:
    run_id, _, job_id, _ = _requesting(tmp_path)

    blocked = recover_run(run_id, project_root=tmp_path, now=lambda: RESOLUTION_NOW)
    assert blocked.blockers and "requires resolution" in blocked.blockers[0]
    assert _job(tmp_path, run_id).status is TranslationJobStatus.RUNNING

    _apply(tmp_path, _resolve(tmp_path, run_id, job_id, NOT_CONTACTED))
    recovered = recover_run(run_id, project_root=tmp_path, now=lambda: RESOLUTION_NOW)

    assert recovered.blockers == ()
    assert recovered.prepared_executions == ()


def test_prepared_reservations_keep_their_automatic_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import book_translator.provider_execution.executor as executor

    run_id, _, job_id, token = _claimed_execution(tmp_path)
    plan = _plan(tmp_path, run_id, job_id, token)

    def fail_before_send(plan: ExecutionPlan, now: datetime) -> None:
        raise OSError("injected pre-send crash")

    monkeypatch.setattr(executor, "_mark_requesting", fail_before_send)
    with pytest.raises(OSError):
        execute_translation_job(
            plan, worker_id="worker", claim_token=token, now=lambda: EXECUTION_NOW
        )

    with pytest.raises(ExecutionResolutionError, match="recover-translation-run"):
        _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)
    assert recover_run(run_id, project_root=tmp_path, now=lambda: RESOLUTION_NOW).blockers == ()
    assert _job(tmp_path, run_id).status is TranslationJobStatus.CLAIMED


def test_provider_contact_unknown_stays_blocking_and_non_replayable(tmp_path: Path) -> None:
    run_id, _, job_id, reservation_path = _requesting(tmp_path)
    before = reservation_path.read_bytes()

    plan = _resolve(tmp_path, run_id, job_id, CONTACT_UNKNOWN)
    _apply(tmp_path, plan)

    job = _job(tmp_path, run_id)
    assert job.status is TranslationJobStatus.BLOCKED
    assert (job.abandoned_attempt_count, job.provider_attempt_count) == (0, 1)
    assert not is_claimable(job)
    assert _record(tmp_path, plan).replay_permitted is False
    assert reservation_path.read_bytes() == before
    with pytest.raises(QueueError):
        claim_job(run_id, project_root=tmp_path, worker_id="worker", now=lambda: RESOLUTION_NOW)
    with pytest.raises(QueueError, match="failed job"):
        retry_job(
            run_id, job_id, project_root=tmp_path, reason="replay", now=lambda: RESOLUTION_NOW
        )


def test_provider_contacted_no_result_charges_the_attempt_and_needs_an_explicit_retry(
    tmp_path: Path,
) -> None:
    run_id, _, job_id, _ = _requesting(tmp_path)

    plan = _resolve(tmp_path, run_id, job_id, CONTACTED_NO_RESULT)
    _apply(tmp_path, plan)

    job = _job(tmp_path, run_id)
    assert job.status is TranslationJobStatus.FAILED
    assert (job.abandoned_attempt_count, job.provider_attempt_count) == (0, 1)
    assert job.last_error_category == "unknown"
    assert _record(tmp_path, plan).replay_permitted is False
    assert recover_run(run_id, project_root=tmp_path, now=lambda: RESOLUTION_NOW).blockers == ()
    assert _job(tmp_path, run_id).status is TranslationJobStatus.FAILED

    retry_job(
        run_id, job_id, project_root=tmp_path, reason="operator retry", now=lambda: RESOLUTION_NOW
    )
    later = RESOLUTION_NOW + timedelta(hours=1)
    recover_run(run_id, project_root=tmp_path, now=lambda: later)
    claim = claim_job(run_id, project_root=tmp_path, worker_id="worker", now=lambda: later)

    assert (claim.job.attempt_count, claim.job.provider_attempt_count) == (2, 2)


def test_repeated_identical_resolution_is_idempotent(tmp_path: Path) -> None:
    run_id, run_path, job_id, _ = _requesting(tmp_path)
    first = _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)
    _apply(tmp_path, first)
    after_first = _tree(run_path)

    repeated = _resolve(tmp_path, run_id, job_id, NOT_CONTACTED, reason="a differently worded note")
    result = execute_reservation_resolution(
        repeated, project_root=tmp_path, now=lambda: RESOLUTION_NOW + timedelta(hours=1)
    )

    assert repeated.action == "skip"
    assert repeated.resolution_id == first.resolution_id
    assert (result.lineage_published, result.job_reconciled) == (False, False)
    assert _record(tmp_path, first).reason == REASON
    assert _tree(run_path) == after_first
    resolutions = run_path / "jobs" / _job(tmp_path, run_id).chunk_id / "resolutions"
    assert [path.name for path in sorted(resolutions.iterdir())] == ["001"]


def test_conflicting_second_resolution_is_refused(tmp_path: Path) -> None:
    run_id, run_path, job_id, _ = _requesting(tmp_path)
    _apply(tmp_path, _resolve(tmp_path, run_id, job_id, NOT_CONTACTED))
    published = _tree(run_path)

    with pytest.raises(ExecutionResolutionError, match="conflicting resolution is refused"):
        _resolve(tmp_path, run_id, job_id, CONTACT_UNKNOWN)

    assert _tree(run_path) == published


def test_unreconciled_lineage_blocks_recovery_and_is_repaired_by_rerunning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _, job_id, _ = _requesting(tmp_path)

    def fail_after_publication(*args: object, **kwargs: object) -> None:
        raise OSError("injected crash after lineage publication")

    monkeypatch.setattr(execution_resolution, "_persist_job_and_run", fail_after_publication)
    with pytest.raises(OSError):
        _apply(tmp_path, _resolve(tmp_path, run_id, job_id, NOT_CONTACTED))
    monkeypatch.undo()

    blocked = recover_run(run_id, project_root=tmp_path, now=lambda: RESOLUTION_NOW)
    assert blocked.blockers and "unreconciled" in blocked.blockers[0]

    repeated = _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)
    assert repeated.action == "reconcile"
    result = execute_reservation_resolution(
        repeated, project_root=tmp_path, now=lambda: RESOLUTION_NOW
    )

    assert (result.lineage_published, result.job_reconciled) == (False, True)
    assert _job(tmp_path, run_id).status is TranslationJobStatus.PENDING
    assert recover_run(run_id, project_root=tmp_path, now=lambda: RESOLUTION_NOW).blockers == ()


def test_published_attempt_evidence_cannot_be_resolved_this_way(tmp_path: Path) -> None:
    run_id, run_path, job_id, _ = _requesting(tmp_path)
    attempts = run_path / "jobs" / _job(tmp_path, run_id).chunk_id / "attempts"
    (attempts / "001").mkdir()

    with pytest.raises(ExecutionResolutionError, match="already published provider evidence"):
        _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)


def test_a_later_attempt_prevents_retroactive_resolution(tmp_path: Path) -> None:
    run_id, run_path, job_id, reservation_path = _requesting(tmp_path)
    attempts = run_path / "jobs" / _job(tmp_path, run_id).chunk_id / "attempts"
    (attempts / "002").mkdir()

    with pytest.raises(ExecutionResolutionError, match="later attempt 002"):
        _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)

    (attempts / "002").rmdir()
    (attempts / ".002.reservation.yaml").write_bytes(reservation_path.read_bytes())

    with pytest.raises(ExecutionResolutionError, match="later execution reservation"):
        _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)


def test_succeeded_and_unclaimed_jobs_are_refused(tmp_path: Path) -> None:
    run_id, _, job_id, token = _claimed_execution(tmp_path)
    execute_translation_job(
        _plan(tmp_path, run_id, job_id, token),
        worker_id="worker",
        claim_token=token,
        now=lambda: EXECUTION_NOW,
        monotonic=lambda: 1.0,
    )

    assert _job(tmp_path, run_id).status is TranslationJobStatus.SUCCEEDED
    with pytest.raises(ExecutionResolutionError, match="supersedes this attempt"):
        _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)
    with pytest.raises(ExecutionResolutionError, match="not found in this run"):
        _resolve(tmp_path, run_id, "tr-000000000000-001__missing", NOT_CONTACTED)


def test_malformed_or_mismatched_reservations_are_refused(tmp_path: Path) -> None:
    run_id, _, job_id, reservation_path = _requesting(tmp_path)
    canonical = reservation_path.read_bytes()

    with pytest.raises(ExecutionResolutionError, match="current attempt slot"):
        _resolve(tmp_path, run_id, job_id, NOT_CONTACTED, attempt_number=2)
    with pytest.raises(ExecutionResolutionError, match="reason"):
        _resolve(tmp_path, run_id, job_id, NOT_CONTACTED, reason="   ")

    reservation_path.write_bytes(canonical + b"# operator note\n")
    with pytest.raises(ExecutionResolutionError, match="not canonical"):
        _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)

    drifted = yaml.safe_load(canonical.decode("utf-8"))
    drifted["job_id"] = "tr-000000000000-001__001"
    reservation_path.write_text(yaml.safe_dump(drifted, sort_keys=False), encoding="utf-8")
    with pytest.raises(ExecutionResolutionError, match="does not belong to the selected run"):
        _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)

    reservation_path.write_text("not: [valid", encoding="utf-8")
    with pytest.raises(ExecutionResolutionError, match="invalid execution artifact"):
        _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)

    reservation_path.unlink()
    with pytest.raises(ExecutionResolutionError, match="no execution reservation"):
        _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)


def test_a_drifted_prompt_package_refuses_the_request_identity(tmp_path: Path) -> None:
    run_id, run_path, job_id, _ = _requesting(tmp_path)
    package = (
        run_path / "jobs" / _job(tmp_path, run_id).chunk_id / "prompt-package" / "package.yaml"
    )
    stored = yaml.safe_load(package.read_text(encoding="utf-8"))
    stored["package_id"] = "pp-000000000000000000000000"
    package.write_text(yaml.safe_dump(stored, sort_keys=False), encoding="utf-8")

    with pytest.raises(ExecutionResolutionError, match="request identity"):
        _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)


def test_planning_writes_nothing(tmp_path: Path) -> None:
    run_id, run_path, job_id, _ = _requesting(tmp_path)
    before = _tree(run_path)

    plan = _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)
    result = planned_result(plan)

    assert result.dry_run is True
    assert result.resulting_job_status is TranslationJobStatus.PENDING
    assert _tree(run_path) == before
    assert not (run_path / "jobs" / plan.chunk_id / "resolutions").exists()


def test_resolution_semantics_are_pure_and_total() -> None:
    outcomes = {kind: resolution_outcome(kind) for kind in ExecutionReservationResolutionKind}

    assert outcomes[NOT_CONTACTED].job_status is TranslationJobStatus.PENDING
    assert outcomes[CONTACT_UNKNOWN].job_status is TranslationJobStatus.BLOCKED
    assert outcomes[CONTACTED_NO_RESULT].job_status is TranslationJobStatus.FAILED
    assert all(
        outcome.replay_permitted is not outcome.provider_attempt_consumed
        for outcome in outcomes.values()
    )
    assert len({outcome.job_status for outcome in outcomes.values()}) == 3


def test_inspection_reports_unresolved_resolved_and_drifted_reservations(tmp_path: Path) -> None:
    run_id, _, job_id, reservation_path = _requesting(tmp_path)

    (unresolved,) = inspect_execution_reservations(run_id, project_root=tmp_path)
    assert (unresolved.resolved, unresolved.replay_permitted) == (False, False)
    assert unresolved.resolution is None
    assert unresolved.automatically_recoverable is False
    assert unresolved.reservation_status is TranslationAttemptStatus.REQUESTING

    _apply(tmp_path, _resolve(tmp_path, run_id, job_id, NOT_CONTACTED))
    (resolved,) = inspect_execution_reservations(run_id, project_root=tmp_path, job_id=job_id)

    assert (resolved.resolved, resolved.replay_permitted, resolved.integrity) == (
        True,
        True,
        "valid",
    )
    assert resolved.resolution is NOT_CONTACTED
    assert resolved.job_status is TranslationJobStatus.PENDING
    assert inspect_execution_reservations(run_id, project_root=tmp_path, job_id="other") == ()

    reservation_path.write_bytes(reservation_path.read_bytes() + b"\n")
    (drifted,) = inspect_execution_reservations(run_id, project_root=tmp_path)
    assert drifted.integrity == "reservation drift"


def test_cli_plans_applies_and_never_prints_claim_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, run_path, job_id, reservation_path = _requesting(tmp_path)
    token = _job(tmp_path, run_id).claim_token
    assert token is not None
    monkeypatch.chdir(tmp_path)
    before = _tree(run_path)

    planned = runner.invoke(
        app,
        [
            "resolve-translation-reservation",
            run_id,
            job_id,
            "--attempt-number",
            "1",
            "--resolution",
            NOT_CONTACTED.value,
            "--reason",
            REASON,
        ],
        env=WIDE,
    )
    assert planned.exit_code == 0
    assert "no (plan only)" in planned.stdout
    assert _tree(run_path) == before

    applied = runner.invoke(
        app,
        [
            "resolve-translation-reservation",
            run_id,
            job_id,
            "--attempt-number",
            "1",
            "--resolution",
            NOT_CONTACTED.value,
            "--reason",
            REASON,
            "--apply",
        ],
        env=WIDE,
    )
    listed = runner.invoke(app, ["inspect-translation-reservations", run_id], env=WIDE)
    recovered = runner.invoke(app, ["recover-translation-run", run_id], env=WIDE)

    assert applied.exit_code == 0
    assert listed.exit_code == 0
    assert recovered.exit_code == 0
    assert "provider_not_contacted" in listed.stdout
    assert _job(tmp_path, run_id).status is TranslationJobStatus.PENDING
    resolution_bytes = (
        run_path / "jobs" / _job(tmp_path, run_id).chunk_id / "resolutions/001/resolution.yaml"
    ).read_bytes()
    for output in (planned.stdout, applied.stdout, listed.stdout, recovered.stdout):
        assert token not in output
        assert "Alpha beta" not in output
    assert token.encode() not in resolution_bytes
    assert (
        reservation_path.read_bytes() == before[reservation_path.relative_to(run_path).as_posix()]
    )


def test_cli_refuses_an_unresolvable_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _, job_id, _ = _requesting(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "resolve-translation-reservation",
            run_id,
            job_id,
            "--attempt-number",
            "3",
            "--resolution",
            NOT_CONTACTED.value,
            "--reason",
            REASON,
            "--apply",
        ],
    )

    assert result.exit_code == 1
    assert _job(tmp_path, run_id).status is TranslationJobStatus.RUNNING


def test_ordinary_worker_execution_is_unchanged(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)

    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("ترجمه نخست.\n\nترجمه دوم.\n"),
        now=lambda: NOW + timedelta(seconds=10),
    )

    job = _job(tmp_path, run_id)
    run_path = tmp_path / "workspace/translation-runs" / run_id
    assert (result.processed, result.accepted, result.errors) == (1, 1, 0)
    assert job.status is TranslationJobStatus.SUCCEEDED
    assert (job.attempt_count, job.abandoned_attempt_count, job.provider_attempt_count) == (1, 0, 1)
    assert inspect_execution_reservations(run_id, project_root=tmp_path) == ()
    assert not (run_path / "jobs" / job.chunk_id / "resolutions").exists()


# ------------------------------------------------------------------------------------------
# A repair reservation: the standing rejection is the target, not a supersession
# ------------------------------------------------------------------------------------------


def _write_job(root: Path, run_id: str, job: TranslationJob) -> None:
    path = _run_path(root, run_id) / "jobs" / job.chunk_id / "job.yaml"
    path.write_text(
        yaml.safe_dump(
            job.model_dump(mode="json", exclude_none=False), allow_unicode=True, sort_keys=False
        ),
        encoding="utf-8",
    )


def _repair_requesting(root: Path) -> tuple[str, str]:
    """Leave one job running behind an unresolved `requesting` reservation for a *repair*.

    The rejection comes from a real QA verdict and the repair is claimed and sent through the
    ordinary boundary, so the state under test is one a killed worker actually produces.
    """
    run_id = _terminology_run(root)
    run_translation_worker(
        run_id,
        project_root=root,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("فقط یک بند.\n"),
        now=lambda: NOW + timedelta(seconds=10),
    )
    job = _job(root, run_id)
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert job.status is TranslationJobStatus.SUCCEEDED

    with pytest.raises(KeyboardInterrupt):
        repair_translation_job(
            run_id,
            job.job_id,
            project_root=root,
            provider=ProviderName.FAKE,
            model="fake-v1",
            worker_id="repair-worker",
            lease_seconds=900,
            timeout_seconds=120,
            max_output_tokens=1000,
            provider_adapter=_ProcessCrash(),
            now=lambda: NOW + timedelta(seconds=20),
        )
    stranded = _job(root, run_id)
    assert stranded.status is TranslationJobStatus.RUNNING
    assert stranded.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert stranded.attempt_count == 2
    return run_id, stranded.job_id


def test_a_repair_reservation_is_resolvable_despite_its_standing_rejection(
    tmp_path: Path,
) -> None:
    run_id, job_id = _repair_requesting(tmp_path)

    plan = _resolve(tmp_path, run_id, job_id, CONTACTED_NO_RESULT, attempt_number=2)

    assert plan.previous_job_status is TranslationJobStatus.RUNNING
    assert plan.resulting_job_status is TranslationJobStatus.FAILED
    assert plan.provider_attempt_consumed is True
    assert plan.replay_permitted is False
    assert (plan.previous_attempt_count, plan.resulting_attempt_count) == (2, 2)
    assert plan.resulting_abandoned_attempt_count == plan.previous_abandoned_attempt_count

    _apply(tmp_path, plan)
    settled = _job(tmp_path, run_id)
    assert settled.status is TranslationJobStatus.FAILED
    # The rejection the repair was answering is untouched: it was never the thing superseded.
    assert settled.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert settled.accepted_attempt == 1
    assert (settled.claimed_by, settled.claim_token) == (None, None)
    assert _record(tmp_path, plan).resolver_engine_version == EXECUTION_RESOLUTION_ENGINE_VERSION


def test_an_accepted_translation_still_supersedes_a_repair_reservation(tmp_path: Path) -> None:
    run_id, job_id = _repair_requesting(tmp_path)
    job = _job(tmp_path, run_id)
    _write_job(
        tmp_path,
        run_id,
        job.model_copy(update={"acceptance_status": TranslationAcceptanceStatus.ACCEPTED}),
    )
    # Changing the verdict moves the run's aggregates; repair them so the resolution boundary
    # refuses on the evidence rather than on a queue the test itself made inconsistent.
    recover_run(run_id, project_root=tmp_path, now=lambda: RESOLUTION_NOW)

    with pytest.raises(ExecutionResolutionError, match="supersedes this repair attempt"):
        _resolve(tmp_path, run_id, job_id, CONTACTED_NO_RESULT, attempt_number=2)


def test_a_repair_whose_own_slot_holds_the_standing_draft_supersedes_itself(
    tmp_path: Path,
) -> None:
    run_id, job_id = _repair_requesting(tmp_path)
    job = _job(tmp_path, run_id)
    _write_job(tmp_path, run_id, job.model_copy(update={"accepted_attempt": 2}))

    with pytest.raises(ExecutionResolutionError, match="supersedes itself"):
        _resolve(tmp_path, run_id, job_id, CONTACTED_NO_RESULT, attempt_number=2)


def test_a_repair_reservation_with_unverifiable_lineage_is_refused(tmp_path: Path) -> None:
    """Fail closed: without a provable repair target the blunt rule is not relaxed."""
    run_id, job_id = _repair_requesting(tmp_path)
    job = _job(tmp_path, run_id)
    assert job.acceptance_file is not None
    (tmp_path / job.acceptance_file).write_text("status: tampered\n", encoding="utf-8")

    with pytest.raises(ExecutionResolutionError, match="does not verify"):
        _resolve(tmp_path, run_id, job_id, CONTACTED_NO_RESULT, attempt_number=2)


def test_a_translation_reservation_keeps_its_existing_resolution_behaviour(
    tmp_path: Path,
) -> None:
    """The ordinary path is untouched: same transition, same accounting, same refusals."""
    run_id, _, job_id, _ = _requesting(tmp_path)

    plan = _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)
    assert (plan.previous_job_status, plan.resulting_job_status) == (
        TranslationJobStatus.RUNNING,
        TranslationJobStatus.PENDING,
    )
    assert plan.provider_attempt_consumed is False
    assert plan.resulting_abandoned_attempt_count == plan.previous_abandoned_attempt_count + 1
    _apply(tmp_path, plan)
    restored = _job(tmp_path, run_id)
    assert restored.status is TranslationJobStatus.PENDING
    assert restored.acceptance_status is None
    assert is_claimable(restored)


def test_a_published_resolution_still_verifies_after_the_engine_is_bumped(
    tmp_path: Path,
) -> None:
    """A record is addressed by the engine that wrote it, so history survives a version bump."""
    run_id, _, job_id, _ = _requesting(tmp_path)
    plan = _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)
    _apply(tmp_path, plan)
    record = _record(tmp_path, plan)

    legacy = record.model_copy(update={"resolver_engine_version": "0.9.0"})
    legacy_id = execution_resolution._resolution_identity(
        run_id=legacy.run_id,
        job_id=legacy.job_id,
        attempt_id=legacy.attempt_id,
        attempt_number=legacy.attempt_number,
        reservation_sha256=legacy.reservation_sha256,
        request_sha256=legacy.request_sha256,
        resolution=legacy.resolution,
        resolver_engine_version=legacy.resolver_engine_version,
    )
    assert legacy_id != record.resolution_id
    path = tmp_path / plan.resolution_file
    path.write_text(
        yaml.safe_dump(
            legacy.model_copy(update={"resolution_id": legacy_id}).model_dump(mode="json"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # Re-planning verifies the old record against its own engine version rather than this one.
    rerun = _resolve(tmp_path, run_id, job_id, NOT_CONTACTED)
    assert rerun.resolution_id == legacy_id


def test_a_carried_forward_repair_sharing_attempt_number_one_is_still_resolvable(
    tmp_path: Path,
) -> None:
    """The two numbering spaces collide at 1, and the guard must not read one as the other.

    A carried-forward draft is numbered by carry-forward, so its first repair attempt is attempt
    slot 1 while `accepted_attempt` is *carry-forward* 1. Treating that coincidence as "this
    attempt already defines the standing translation" refuses exactly the stranded repair the
    repair branch exists to let through.
    """
    from tests.test_translation_historical_reevaluation import (
        STRUCTURALLY_DEFECTIVE_OUTPUT,
        _carry_forward,
        _corrected_successor_run,
        _rejected_old_run,
    )

    old = _rejected_old_run(tmp_path, translation=STRUCTURALLY_DEFECTIVE_OUTPUT)
    new = _corrected_successor_run(tmp_path)
    _carry_forward(tmp_path, new, old)
    carried = _job(tmp_path, new)
    assert carried.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert (carried.carry_forward_count, carried.accepted_attempt) == (1, 1)
    assert (carried.attempt_count, carried.successful_attempt) == (0, None)

    with pytest.raises(KeyboardInterrupt):
        repair_translation_job(
            new,
            carried.job_id,
            project_root=tmp_path,
            provider=ProviderName.FAKE,
            model="fake-v1",
            worker_id="repair-worker",
            lease_seconds=900,
            timeout_seconds=120,
            max_output_tokens=1000,
            provider_adapter=_ProcessCrash(),
            now=lambda: NOW + timedelta(seconds=40),
        )
    stranded = _job(tmp_path, new)
    # The repair took attempt slot 1, the same number the standing carry-forward carries.
    assert stranded.status is TranslationJobStatus.RUNNING
    assert (stranded.attempt_count, stranded.accepted_attempt) == (1, 1)

    plan = _resolve(tmp_path, new, carried.job_id, CONTACTED_NO_RESULT, attempt_number=1)
    assert plan.resulting_job_status is TranslationJobStatus.FAILED
    assert plan.provider_attempt_consumed is True
    _apply(tmp_path, plan)
    settled = _job(tmp_path, new)
    assert settled.status is TranslationJobStatus.FAILED
    assert settled.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert settled.accepted_attempt == 1
    assert settled.successful_attempt is None
