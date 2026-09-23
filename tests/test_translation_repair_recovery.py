"""Bounded recovery of a repair that a provider failure ended.

The scenario every test here circles is one job: an initial translation QA rejected, a repair
authorized to answer that rejection, and a provider that failed to carry the repair out. The
question is whether an unattended worker may send that same repair again, and the answers have to
hold three things apart that look alike from a distance — a repair *decision*, a repair
*execution*, and a provider *recovery* — because conflating any two of them either strands the job
or spends a budget nothing authorized.

Everything below is synthetic: a fake provider, invented Persian prose, and a throwaway workspace.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import (
    ProviderErrorCategory,
    ProviderFailureClass,
    ProviderName,
    ProviderRecoveryVerdict,
    TranslationAcceptanceEvidence,
    TranslationAcceptanceStatus,
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationAutoRepairDecision,
    TranslationExecutionKind,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
    TranslationProviderRecoveryDecision,
    TranslationRepairInitiator,
)
from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
from book_translator.provider_execution import (
    ProviderExecutionError,
    ProviderPreflightReport,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
    list_attempts,
)
from book_translator.provider_execution.artifacts import load_yaml, sha256_bytes
from book_translator.provider_execution.errors import ErrorClassification
from book_translator.provider_recovery_policy import (
    PROVIDER_RECOVERY_POLICY_VERSION,
    ProviderRecoveryBudget,
    RepairReplayEvidence,
    assess_provider_recovery,
)
from book_translator.translation_provider_recovery import (
    ProviderRecoveryError,
    _repair_decisions_used,
    is_provider_recovery_candidate,
    is_repair_recovery_candidate,
    plan_provider_recovery,
    recovery_execution_kind,
)
from book_translator.translation_queue import (
    QueueError,
    claim_job,
    create_run,
    load_run_summary,
    plan_repair_target_restoration,
    plan_run,
    recover_run,
    release_job,
    restore_repair_target,
)
from book_translator.translation_queue.policy import can_transition_translation_job
from book_translator.translation_repair import (
    _claim_repair,
    build_repair_request,
    repair_translation_job,
)
from book_translator.translation_repair_policy import is_auto_repair_candidate, is_failed_repair
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import (
    TranslationWorkerError,
    WorkerResult,
    run_translation_worker,
)
from tests.test_translation_queue import NOW, SNAPSHOT_NOW
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import _options, _queue_bytes, _run_path

#: The initial translation. One paragraph against a two-paragraph source, so structural QA
#: rejects it with `paragraph_count_mismatch` — a finding the repair policy calls auto-repairable.
REJECTED = "فقط یک بند.\n"
#: A repair output that satisfies QA.
ACCEPTABLE = "بند نخست.\n\nبند دوم.\n"
#: A repair output that does not: still one paragraph, so QA rejects it again.
STILL_REJECTED = "همچنان فقط یک بند.\n"

CHUNK = "001-001"


def _prepared_run(
    root: Path,
    *,
    max_attempts: int = 5,
    retry_backoff_seconds: int = 0,
    chapters: int = 1,
) -> str:
    synthetic_project(root, chapters=chapters)
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    created = create_run(
        plan_run(
            snapshot.snapshot_id,
            project_root=root,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            now=lambda: NOW,
        )
    )
    execute_prompt_packages(
        plan_prompt_packages(created.run_id, project_root=root, now=lambda: NOW)
    )
    return created.run_id


class ScriptedRepairProvider:
    """One provider whose successive answers are scripted, so a whole repair round can be staged.

    Each entry is either a translation to return or a classification to fail with, so a run like
    "draft rejected, repair fails, recovered repair succeeds" is written as three entries and
    needs no branching in the test itself.
    """

    provider_name = "fake"
    adapter_version = "test-1.0.0"

    def __init__(
        self,
        script: tuple[str | ErrorClassification, ...],
        calls: list[str] | None = None,
    ) -> None:
        self.script = script
        self.calls: list[str] = calls if calls is not None else []
        self.requests: list[ProviderRequest] = []

    def execute(self, request: ProviderRequest) -> ProviderResult:
        step = self.script[min(len(self.calls), len(self.script) - 1)]
        self.calls.append(request.job_id)
        self.requests.append(request)
        if isinstance(step, ErrorClassification):
            raise ProviderExecutionError(step)
        return ProviderResult(
            response_id="synthetic-response",
            model=request.model or "fake-v1",
            status="completed",
            text_outputs=(step,),
            usage=ProviderUsage(10, 10, 20),
            sanitized_response={"status": "completed"},
        )


def _failure(
    category: ProviderErrorCategory = ProviderErrorCategory.AUTHENTICATION,
    *,
    retryable: bool = False,
    ambiguous: bool = False,
) -> ErrorClassification:
    return ErrorClassification(category, retryable, ambiguous, False, "synthetic provider failure")


HEALTHY_DETAILS = (
    ("executable", "claude"),
    ("cli_version", "2.1.235"),
    ("authenticated", "yes"),
    ("auth_method", "claude.ai"),
    ("api_key_used", "no"),
)


def _healthy_preflight(
    calls: list[ProviderName] | None = None,
) -> Callable[[ProviderName], ProviderPreflightReport]:
    def preflight(provider: ProviderName) -> ProviderPreflightReport:
        if calls is not None:
            calls.append(provider)
        return ProviderPreflightReport(
            provider=provider.value, supported=True, passed=True, details=HEALTHY_DETAILS
        )

    return preflight


def _unhealthy_preflight(provider: ProviderName) -> ProviderPreflightReport:
    raise ProviderExecutionError(
        ErrorClassification(
            ProviderErrorCategory.AUTHENTICATION,
            False,
            False,
            False,
            "Claude Code is not logged in; run 'claude auth login' first",
        )
    )


def _worker(
    root: Path,
    run_id: str,
    script: tuple[str | ErrorClassification, ...],
    *,
    calls: list[str] | None = None,
    auto_repair: bool = True,
    include_qa_rejected: bool = False,
    auto_recover: bool = True,
    include_failed: bool = True,
    max_repair_attempts: int = 2,
    max_provider_recovery_attempts: int = 2,
    max_jobs: int = 1,
    dry_run: bool = False,
    preflight: Callable[[ProviderName], ProviderPreflightReport] | None = None,
    offset: int = 10,
) -> WorkerResult:
    provider = ScriptedRepairProvider(script, calls)
    return run_translation_worker(
        run_id,
        project_root=root,
        options=_options(
            auto_repair=auto_repair,
            include_qa_rejected=include_qa_rejected,
            max_repair_attempts=max_repair_attempts,
            auto_recover_provider_failures=auto_recover,
            include_failed=include_failed,
            max_provider_recovery_attempts=max_provider_recovery_attempts,
            max_jobs=max_jobs,
            dry_run=dry_run,
        ),
        provider_factory=lambda _job: provider,
        recovery_preflight=preflight or _healthy_preflight(),
        now=lambda: NOW + timedelta(seconds=offset),
    )


def _job(root: Path, run_id: str) -> TranslationJob:
    return load_run_summary(run_id, project_root=root).jobs[0]


def _attempts(root: Path, run_id: str) -> tuple[TranslationAttempt, ...]:
    job = _job(root, run_id)
    return tuple(item.attempt for item in list_attempts(run_id, job.job_id, project_root=root))


def _recovery_decision(
    root: Path, run_id: str, attempt: int
) -> TranslationProviderRecoveryDecision:
    path = (
        _run_path(root, run_id)
        / "jobs"
        / CHUNK
        / "provider-recovery"
        / f"{attempt:03d}"
        / "decision.yaml"
    )
    return TranslationProviderRecoveryDecision.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def _auto_repair_decisions(root: Path, run_id: str) -> tuple[TranslationAutoRepairDecision, ...]:
    directory = _run_path(root, run_id) / "jobs" / CHUNK / "auto-repair"
    if not directory.is_dir():
        return ()
    return tuple(
        TranslationAutoRepairDecision.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
        for path in sorted(directory.glob("*/decision.yaml"))
    )


def _tree_of(root: Path, run_id: str, chunk_id: str) -> dict[str, bytes]:
    """Every file under one job directory, so immutability can be asserted byte for byte."""
    directory = _run_path(root, run_id) / "jobs" / chunk_id
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _stranded_repair(
    root: Path,
    run_id: str,
    *,
    category: ProviderErrorCategory = ProviderErrorCategory.AUTHENTICATION,
) -> TranslationJob:
    """Produce one genuine failed repair: QA rejects the draft, the repair call then fails.

    Recovery is deliberately off, so the worker stops exactly where the gap used to be and every
    later test starts from a state a real run can reach.
    """
    _worker(
        root,
        run_id,
        (REJECTED, _failure(category)),
        auto_recover=False,
        include_failed=False,
    )
    job = _job(root, run_id)
    assert is_failed_repair(job)
    return job


# ------------------------------------------------------------------------------------------
# The state a failed repair leaves, and how each boundary reads it
# ------------------------------------------------------------------------------------------


def test_a_failed_repair_is_a_repair_recovery_candidate_and_nothing_else(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    job = _stranded_repair(tmp_path, run_id)

    assert job.status is TranslationJobStatus.FAILED
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert job.accepted_attempt == 1 and job.last_attempt == 2
    assert is_repair_recovery_candidate(job)
    # The other two classes must not claim it: an ordinary translation recovery would re-send the
    # wrong request, and auto-repair requires a live provider success this job no longer has.
    assert not is_provider_recovery_candidate(job)
    assert not is_auto_repair_candidate(job)
    assert recovery_execution_kind(job) is TranslationExecutionKind.REPAIR


def test_planning_names_the_repair_lineage_it_would_preserve(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    job = _stranded_repair(tmp_path, run_id)
    before = _queue_bytes(tmp_path, run_id)

    plan = plan_provider_recovery(run_id, job.job_id, project_root=tmp_path)

    assert plan.execution_kind is TranslationExecutionKind.REPAIR
    assert plan.repair is not None
    assert plan.repair.repair_root_attempt == 1
    assert plan.repair.failed_repair_attempt == 2
    assert plan.repair.request_reproduced_exactly
    assert plan.repair.triggering_codes == ["paragraph_count_mismatch"]
    assert plan.repair.repair_decisions_used == 1
    assert plan.authorized_attempt_number == 3
    assert plan.restored_successful_attempt == 1
    # Planning reads and hashes; it never writes.
    assert _queue_bytes(tmp_path, run_id) == before


# ------------------------------------------------------------------------------------------
# The core cycle
# ------------------------------------------------------------------------------------------


def test_a_recovered_repair_succeeds_and_qa_accepts_it(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    calls: list[str] = []

    result = _worker(tmp_path, run_id, (REJECTED, _failure(), ACCEPTABLE), calls=calls)
    job = _job(tmp_path, run_id)

    assert len(calls) == 3
    assert result.accepted == 1 and result.qa_rejected == 0 and result.provider_failed == 0
    assert job.status is TranslationJobStatus.SUCCEEDED
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.accepted_attempt == 3


def test_the_recovered_attempt_is_still_a_repair_of_the_original_rejection(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path)
    _worker(tmp_path, run_id, (REJECTED, _failure(), ACCEPTABLE))
    attempts = _attempts(tmp_path, run_id)

    assert [attempt.execution_kind for attempt in attempts] == [
        TranslationExecutionKind.TRANSLATION,
        TranslationExecutionKind.REPAIR,
        TranslationExecutionKind.REPAIR,
    ]
    # The recovered attempt repairs the rejected *translation*, not the failed repair that
    # happens to be numerically previous.
    assert [attempt.repaired_from_attempt for attempt in attempts] == [None, 1, 1]


def test_the_recovered_repair_sends_byte_identical_messages(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    provider = ScriptedRepairProvider((REJECTED, _failure(), ACCEPTABLE))
    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(
            auto_repair=True,
            auto_recover_provider_failures=True,
            include_failed=True,
        ),
        provider_factory=lambda _job: provider,
        recovery_preflight=_healthy_preflight(),
        now=lambda: NOW + timedelta(seconds=10),
    )
    failed, recovered = provider.requests[1], provider.requests[2]

    assert failed.system_message == recovered.system_message
    assert failed.user_message == recovered.user_message


def test_recovery_does_not_spend_a_second_qa_repair_decision(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    result = _worker(tmp_path, run_id, (REJECTED, _failure(), ACCEPTABLE))
    decisions = _auto_repair_decisions(tmp_path, run_id)
    job = _job(tmp_path, run_id)

    # One QA judgement, two repair executions, one recovery, three provider attempts.
    assert len(decisions) == 1 and decisions[0].repair_attempt_number == 2
    assert result.repair_decisions == 1
    assert result.repair_attempts == 2
    assert result.repair_recovery_attempts == 1
    assert result.provider_recovery_attempts == 1
    assert job.provider_attempt_count == 3
    assert _repair_decisions_used(_run_path(tmp_path, run_id), job) == 1


def test_run_counters_describe_one_accepted_job(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    result = _worker(tmp_path, run_id, (REJECTED, _failure(), ACCEPTABLE))
    state = load_run_summary(run_id, project_root=tmp_path)

    assert result.processed == 1 and result.accepted == 1
    assert result.provider_recovered == 1 and result.repair_recovered == 1
    assert state.run.accepted_count == 1
    assert state.run.qa_rejected_count == 0
    assert state.run.failed_count == 0


# ------------------------------------------------------------------------------------------
# Provenance
# ------------------------------------------------------------------------------------------


def test_the_recovery_decision_records_the_whole_repair_lineage(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _worker(tmp_path, run_id, (REJECTED, _failure(), ACCEPTABLE))
    decision = _recovery_decision(tmp_path, run_id, 3)
    job = _job(tmp_path, run_id)
    repair = decision.repair

    assert decision.recovered_execution_kind is TranslationExecutionKind.REPAIR
    assert decision.failed_attempt_number == 2
    assert decision.authorized_attempt_number == 3
    assert decision.provider is ProviderName.FAKE
    assert decision.preflight is not None and decision.preflight.passed
    assert repair is not None
    assert repair.repair_root_attempt == 1
    assert repair.failed_repair_attempt == 2
    assert repair.rejection_evidence_file.as_posix().endswith("acceptance/001/evidence.yaml")
    assert repair.rejected_attempt_file.as_posix().endswith("attempts/001/attempt.yaml")
    assert repair.repair_decision_file is not None
    assert repair.repair_decision_file.as_posix().endswith("auto-repair/002/decision.yaml")
    assert repair.triggering_codes == ["paragraph_count_mismatch"]
    assert repair.source_sha256 == job.chunk_source_sha256
    assert repair.context_sha256 == job.context_sha256
    assert repair.repair_decisions_used == 1
    assert repair.request_reproduced_exactly


def test_recovery_provenance_carries_no_translated_content(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _worker(tmp_path, run_id, (REJECTED, _failure(), ACCEPTABLE))
    path = (
        _run_path(tmp_path, run_id) / "jobs" / CHUNK / "provider-recovery" / "003" / "decision.yaml"
    )
    rendered = path.read_text(encoding="utf-8")

    for prose in (REJECTED.strip(), ACCEPTABLE.strip(), "بند"):
        assert prose not in rendered


def test_historical_attempts_and_qa_evidence_stay_byte_identical(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _stranded_repair(tmp_path, run_id)
    run_path = _run_path(tmp_path, run_id)
    historical = {
        str(path.relative_to(run_path)): path.read_bytes()
        for path in sorted(run_path.rglob("*"))
        if path.is_file()
        and (
            "/attempts/001/" in path.as_posix()
            or "/attempts/002/" in path.as_posix()
            or "/acceptance/001/" in path.as_posix()
            or "/auto-repair/002/" in path.as_posix()
        )
    }

    _worker(tmp_path, run_id, (ACCEPTABLE,), offset=40)

    assert historical
    assert {
        str(path.relative_to(run_path)): path.read_bytes()
        for path in sorted(run_path.rglob("*"))
        if str(path.relative_to(run_path)) in historical
    } == historical


# ------------------------------------------------------------------------------------------
# Budgets
# ------------------------------------------------------------------------------------------


def test_a_repair_that_keeps_failing_stops_at_the_recovery_budget(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, max_attempts=9)
    calls: list[str] = []

    result = _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure(), _failure(), _failure(), _failure()),
        calls=calls,
        max_provider_recovery_attempts=2,
    )
    job = _job(tmp_path, run_id)

    # One draft plus one repair plus exactly two recovered repairs.
    assert len(calls) == 4
    assert result.repair_recovery_attempts == 2
    assert result.jobs[0].provider_recovery_verdict == ProviderRecoveryVerdict.EXHAUSTED.value
    assert result.qa_rejected == 1 and result.accepted == 0
    assert is_failed_repair(job)
    # Still one QA decision: four provider calls, one judgement.
    assert len(_auto_repair_decisions(tmp_path, run_id)) == 1


def test_a_still_rejected_recovered_repair_may_spend_a_new_repair_decision(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path)
    calls: list[str] = []

    result = _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure(), STILL_REJECTED, ACCEPTABLE),
        calls=calls,
        max_repair_attempts=2,
    )
    job = _job(tmp_path, run_id)
    decisions = _auto_repair_decisions(tmp_path, run_id)

    assert len(calls) == 4
    assert result.accepted == 1
    # QA looked at new output and decided again, so the second decision is genuinely a second
    # judgement rather than a replay.
    assert [item.repair_attempt_number for item in decisions] == [2, 4]
    assert [item.repaired_from_attempt for item in decisions] == [1, 3]
    assert result.repair_decisions == 2 and result.repair_attempts == 3
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED


def test_a_new_repair_decision_whose_provider_call_fails_re_enters_the_same_loop(
    tmp_path: Path,
) -> None:
    """The loop closes: recovery -> still rejected -> new decision -> failure -> recovery again.

    Without this the worker would strand the job one step further along than before, which is the
    same operator gap wearing a different attempt number.
    """
    run_id = _prepared_run(tmp_path, max_attempts=9)
    calls: list[str] = []

    result = _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure(), STILL_REJECTED, _failure(), ACCEPTABLE),
        calls=calls,
        max_repair_attempts=3,
        max_provider_recovery_attempts=3,
    )
    job = _job(tmp_path, run_id)
    decisions = _auto_repair_decisions(tmp_path, run_id)

    assert len(calls) == 5
    assert result.accepted == 1
    # Two QA judgements — the original rejection and the still-rejected recovered output — and
    # two recoveries, one replaying each of them.
    assert [item.repair_attempt_number for item in decisions] == [2, 4]
    assert result.repair_decisions == 2
    assert result.repair_recovery_attempts == 2
    assert result.repair_attempts == 4
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.accepted_attempt == 5


def test_an_exhausted_repair_budget_leaves_a_recovered_rejection_standing(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path)
    calls: list[str] = []

    result = _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure(), STILL_REJECTED, ACCEPTABLE),
        calls=calls,
        max_repair_attempts=1,
    )
    job = _job(tmp_path, run_id)

    # The recovered repair ran — it replays a decision already made — but the still-rejected
    # output cannot buy a new one.
    assert len(calls) == 3
    assert result.qa_rejected == 1 and result.accepted == 0
    assert result.repair_decisions == 1 and result.repair_attempts == 2
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert job.accepted_attempt == 3
    assert len(_auto_repair_decisions(tmp_path, run_id)) == 1


def test_the_job_attempt_budget_bounds_repair_recovery_too(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, max_attempts=3)
    calls: list[str] = []

    result = _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure(), _failure(), _failure()),
        calls=calls,
        max_provider_recovery_attempts=5,
    )

    # Three attempt slots exist in total, so exactly one recovery fits.
    assert len(calls) == 3
    assert result.repair_recovery_attempts == 1
    assert result.jobs[0].provider_recovery_verdict == ProviderRecoveryVerdict.EXHAUSTED.value


# ------------------------------------------------------------------------------------------
# Failure families and provider health
# ------------------------------------------------------------------------------------------


def test_an_authentication_failure_recovers_against_a_healthy_preflight(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    preflights: list[ProviderName] = []

    result = _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure(ProviderErrorCategory.AUTHENTICATION), ACCEPTABLE),
        preflight=_healthy_preflight(preflights),
    )

    assert preflights == [ProviderName.FAKE]
    assert result.accepted == 1
    decision = _recovery_decision(tmp_path, run_id, 3)
    assert decision.failure_class is ProviderFailureClass.AUTHENTICATION
    assert decision.preflight is not None and decision.preflight.authenticated


def test_an_authentication_failure_is_blocked_by_a_failing_preflight(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    calls: list[str] = []

    result = _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure(ProviderErrorCategory.AUTHENTICATION), ACCEPTABLE),
        calls=calls,
        preflight=_unhealthy_preflight,
    )
    job = _job(tmp_path, run_id)

    assert len(calls) == 2
    assert (
        result.jobs[0].provider_recovery_verdict == ProviderRecoveryVerdict.OPERATOR_REQUIRED.value
    )
    assert is_failed_repair(job)
    assert not list(_run_path(tmp_path, run_id).glob("jobs/*/provider-recovery/*"))


def test_a_transient_repair_failure_recovers_without_a_preflight(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    preflights: list[ProviderName] = []

    result = _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure(ProviderErrorCategory.SERVER_ERROR), ACCEPTABLE),
        preflight=_healthy_preflight(preflights),
    )
    decision = _recovery_decision(tmp_path, run_id, 3)

    assert preflights == []
    assert result.accepted == 1
    assert decision.failure_class is ProviderFailureClass.TRANSIENT
    assert decision.preflight is None and not decision.preflight_required


def test_a_rate_limited_repair_waits_for_the_governed_backoff(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, retry_backoff_seconds=600)
    calls: list[str] = []

    result = _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure(ProviderErrorCategory.RATE_LIMIT), ACCEPTABLE),
        calls=calls,
    )

    assert len(calls) == 2
    assert (
        result.jobs[0].provider_recovery_verdict == ProviderRecoveryVerdict.WAIT_FOR_BACKOFF.value
    )
    assert is_failed_repair(_job(tmp_path, run_id))


def test_a_malformed_repair_response_fails_closed(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    calls: list[str] = []

    result = _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure(ProviderErrorCategory.MALFORMED_RESPONSE), ACCEPTABLE),
        calls=calls,
    )

    assert len(calls) == 2
    assert (
        result.jobs[0].provider_recovery_verdict == ProviderRecoveryVerdict.OPERATOR_REQUIRED.value
    )
    assert result.jobs[0].provider_failure_class == ProviderFailureClass.MALFORMED_RESPONSE.value
    assert is_failed_repair(_job(tmp_path, run_id))


def test_an_ambiguous_repair_execution_is_never_replayed(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    calls: list[str] = []

    result = _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure(ProviderErrorCategory.TIMEOUT_AMBIGUOUS, ambiguous=True), ACCEPTABLE),
        calls=calls,
    )
    job = _job(tmp_path, run_id)

    assert len(calls) == 2
    # An ambiguous attempt blocks the job outright, which is the reservation boundary's business.
    assert job.status is TranslationJobStatus.BLOCKED
    assert result.permanent_failure == 1
    assert not list(_run_path(tmp_path, run_id).glob("jobs/*/provider-recovery/*"))


def test_an_outstanding_reservation_blocks_repair_recovery(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    # A transient failure, so nothing but the reservation could stand in recovery's way.
    job = _stranded_repair(tmp_path, run_id, category=ProviderErrorCategory.SERVER_ERROR)
    reservation = (
        _run_path(tmp_path, run_id) / "jobs" / CHUNK / "attempts" / ".002.reservation.yaml"
    )
    reservation.write_text("placeholder\n", encoding="utf-8")

    plan = plan_provider_recovery(run_id, job.job_id, project_root=tmp_path)

    assert plan.verdict is ProviderRecoveryVerdict.UNSAFE_TO_REPLAY
    assert plan.assessment.blocking_reasons == ("failed_attempt_replay_safety_unproven",)


# ------------------------------------------------------------------------------------------
# Integrity: the request must be provably the same one
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relative", "reason"),
    [
        (
            "jobs/001-001/attempts/002/repair-user.md",
            "failed_repair_request_messages_do_not_match_their_recorded_hashes",
        ),
        (
            "jobs/001-001/attempts/002/request.json",
            "failed_repair_request_evidence_is_unreadable_or_tampered",
        ),
    ],
)
def test_a_tampered_failed_repair_artifact_blocks_recovery(
    tmp_path: Path, relative: str, reason: str
) -> None:
    run_id = _prepared_run(tmp_path)
    job = _stranded_repair(tmp_path, run_id)
    target = _run_path(tmp_path, run_id) / relative
    target.write_text(target.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")

    plan = plan_provider_recovery(run_id, job.job_id, project_root=tmp_path)

    assert plan.verdict is ProviderRecoveryVerdict.OPERATOR_REQUIRED
    assert plan.assessment.blocking_reasons == (reason,)
    assert plan.repair is None


def test_tampered_qa_evidence_blocks_recovery(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    job = _stranded_repair(tmp_path, run_id)
    evidence = _run_path(tmp_path, run_id) / "jobs" / CHUNK / "acceptance/001/evidence.yaml"
    evidence.write_text(
        evidence.read_text(encoding="utf-8").replace("acceptance_engine_version: 1.0.0", ""),
        encoding="utf-8",
    )

    plan = plan_provider_recovery(run_id, job.job_id, project_root=tmp_path)

    assert plan.verdict is ProviderRecoveryVerdict.OPERATOR_REQUIRED
    assert plan.assessment.blocking_reasons == ("repair_request_could_not_be_rederived",)


def test_a_missing_frozen_resource_blocks_recovery(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    job = _stranded_repair(tmp_path, run_id)
    snapshot = load_run_summary(run_id, project_root=tmp_path).run.snapshot_path
    manifest = TranslationManifest.model_validate(
        yaml.safe_load((tmp_path / snapshot / "manifest.yaml").read_text(encoding="utf-8"))
    )
    # Exactly the resource the repair derivation reads and hash-checks.
    (tmp_path / manifest.style_guide.file).unlink()

    plan = plan_provider_recovery(run_id, job.job_id, project_root=tmp_path)

    assert plan.verdict is ProviderRecoveryVerdict.OPERATOR_REQUIRED
    assert plan.assessment.blocking_reasons == ("repair_request_could_not_be_rederived",)


def test_the_repair_request_identity_binds_lineage_as_well_as_bytes(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    job = _stranded_repair(tmp_path, run_id)

    request = build_repair_request(
        _run_path(tmp_path, run_id), job, project_root=tmp_path, allow_failed_repair=True
    )
    again = build_repair_request(
        _run_path(tmp_path, run_id), job, project_root=tmp_path, allow_failed_repair=True
    )

    assert request.identity_sha256 == again.identity_sha256
    assert request.system_sha256 == sha256_bytes(request.system.encode("utf-8"))
    assert request.repaired_from_attempt == 1


# ------------------------------------------------------------------------------------------
# Selection, gating, and resuming work an earlier pass stranded
# ------------------------------------------------------------------------------------------


def test_an_existing_failed_repair_is_resumed_without_operator_setup(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _stranded_repair(tmp_path, run_id)

    result = _worker(tmp_path, run_id, (ACCEPTABLE,), offset=40)
    job = _job(tmp_path, run_id)

    assert result.accepted == 1
    assert result.repair_recovery_attempts == 1
    assert result.repair_decisions == 0
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.accepted_attempt == 3


def test_selecting_a_failed_repair_requires_both_repair_and_recovery_to_be_enabled(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path)
    job = _stranded_repair(tmp_path, run_id)

    with pytest.raises(TranslationWorkerError) as error:
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_options(
                job_id=job.job_id,
                auto_recover_provider_failures=True,
                include_failed=True,
            ),
            now=lambda: NOW + timedelta(seconds=40),
        )

    assert "--auto-repair" in str(error.value)


def test_pending_translations_are_never_displaced_by_a_failed_repair(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    chapters = load_run_summary(run_id, project_root=tmp_path).jobs
    assert len(chapters) >= 2
    first = chapters[0].job_id
    # Strand the first job's repair, then let one worker pass choose freely between the stranded
    # repair and the untouched pending jobs behind it.
    _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure()),
        auto_recover=False,
        include_failed=False,
    )

    result = _worker(tmp_path, run_id, (ACCEPTABLE,), max_jobs=1, offset=40)

    assert [item.job_id for item in result.jobs] != [first]
    assert result.jobs[0].job_id == chapters[1].job_id


def test_an_accepted_job_is_never_reprocessed(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _worker(tmp_path, run_id, (REJECTED, _failure(), ACCEPTABLE))
    before = _queue_bytes(tmp_path, run_id)
    job = _job(tmp_path, run_id)
    calls: list[str] = []

    result = _worker(tmp_path, run_id, (ACCEPTABLE,), calls=calls, max_jobs=1, offset=60)

    assert calls == [] or job.job_id not in calls
    assert all(item.job_id != job.job_id for item in result.jobs)
    assert {
        name: value for name, value in _queue_bytes(tmp_path, run_id).items() if CHUNK in name
    } == {name: value for name, value in before.items() if CHUNK in name}


# ------------------------------------------------------------------------------------------
# Dry run
# ------------------------------------------------------------------------------------------


def test_dry_run_reports_the_repair_it_would_recover_and_changes_nothing(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _stranded_repair(tmp_path, run_id)
    before = _queue_bytes(tmp_path, run_id)
    calls: list[str] = []

    result = _worker(tmp_path, run_id, (ACCEPTABLE,), calls=calls, dry_run=True, offset=40)

    assert calls == []
    assert result.dry_run and result.processed == 1
    planned = result.jobs[0]
    assert planned.outcome == "would_recover_failed_repair"
    assert planned.recovered_execution_kind == TranslationExecutionKind.REPAIR.value
    assert planned.repair_root_attempt == 1
    assert planned.attempt_number == 3
    assert _queue_bytes(tmp_path, run_id) == before


def test_dry_run_reports_a_refusal_without_pretending_it_is_recoverable(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure(ProviderErrorCategory.MALFORMED_RESPONSE)),
        auto_recover=False,
        include_failed=False,
    )
    before = _queue_bytes(tmp_path, run_id)

    result = _worker(tmp_path, run_id, (ACCEPTABLE,), dry_run=True, offset=40)

    assert result.jobs[0].outcome == "would_refuse_repair_recovery"
    assert (
        result.jobs[0].provider_recovery_verdict == ProviderRecoveryVerdict.OPERATOR_REQUIRED.value
    )
    assert _queue_bytes(tmp_path, run_id) == before


# ------------------------------------------------------------------------------------------
# The pure policy and the governed restoration, on their own
# ------------------------------------------------------------------------------------------


def test_the_policy_refuses_a_repair_whose_request_was_not_reproduced() -> None:
    assessment = assess_provider_recovery(
        failure_class=ProviderFailureClass.TRANSIENT,
        budget=ProviderRecoveryBudget(2, 0, 5, 2),
        failed_at=NOW,
        retry_backoff_seconds=0,
        now=NOW + timedelta(seconds=60),
        execution_kind=TranslationExecutionKind.REPAIR,
        repair=RepairReplayEvidence(True, False, ("repair_request_not_reproducible",)),
    )

    assert assessment.verdict is ProviderRecoveryVerdict.OPERATOR_REQUIRED
    assert assessment.blocking_reasons == ("repair_request_not_reproducible",)
    assert assessment.execution_kind is TranslationExecutionKind.REPAIR
    assert assessment.policy_version == PROVIDER_RECOVERY_POLICY_VERSION


def test_the_policy_requires_replay_evidence_for_a_repair_and_refuses_it_otherwise() -> None:
    with pytest.raises(ValueError, match="repair replay evidence"):
        assess_provider_recovery(
            failure_class=ProviderFailureClass.TRANSIENT,
            budget=ProviderRecoveryBudget(2, 0, 5, 2),
            failed_at=NOW,
            retry_backoff_seconds=0,
            now=NOW,
            execution_kind=TranslationExecutionKind.REPAIR,
        )
    with pytest.raises(ValueError, match="repair replay evidence"):
        assess_provider_recovery(
            failure_class=ProviderFailureClass.TRANSIENT,
            budget=ProviderRecoveryBudget(2, 0, 5, 2),
            failed_at=NOW,
            retry_backoff_seconds=0,
            now=NOW,
            repair=RepairReplayEvidence(True, True, ()),
        )


def test_the_restoration_transition_is_opt_in_only() -> None:
    assert not can_transition_translation_job(
        TranslationJobStatus.FAILED, TranslationJobStatus.SUCCEEDED
    )
    assert can_transition_translation_job(
        TranslationJobStatus.FAILED, TranslationJobStatus.SUCCEEDED, repair_recovery=True
    )
    # The opt-in opens exactly one edge and no others.
    assert not can_transition_translation_job(
        TranslationJobStatus.PENDING, TranslationJobStatus.SUCCEEDED, repair_recovery=True
    )
    assert not can_transition_translation_job(
        TranslationJobStatus.BLOCKED, TranslationJobStatus.SUCCEEDED, repair_recovery=True
    )


def test_restoration_refuses_a_job_that_is_not_a_failed_repair(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _worker(tmp_path, run_id, (ACCEPTABLE,))
    job = _job(tmp_path, run_id)

    with pytest.raises(QueueError, match="failure ended"):
        restore_repair_target(
            run_id,
            job.job_id,
            project_root=tmp_path,
            successful_attempt=1,
            reason="unauthorized",
        )


def test_planning_refuses_a_job_that_no_recovery_class_claims(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _worker(tmp_path, run_id, (ACCEPTABLE,))
    job = _job(tmp_path, run_id)

    with pytest.raises(ProviderRecoveryError, match="not a provider-failed job"):
        plan_provider_recovery(run_id, job.job_id, project_root=tmp_path)


# ------------------------------------------------------------------------------------------
# The neighbouring boundaries stay exactly as they were
# ------------------------------------------------------------------------------------------


def test_ordinary_translation_recovery_is_unchanged(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)

    result = _worker(tmp_path, run_id, (_failure(), ACCEPTABLE))
    decision = _recovery_decision(tmp_path, run_id, 2)
    job = _job(tmp_path, run_id)

    assert result.accepted == 1 and result.provider_recovery_attempts == 1
    assert result.repair_recovery_attempts == 0 and result.repair_attempts == 0
    assert decision.recovered_execution_kind is TranslationExecutionKind.TRANSLATION
    assert decision.repair is None
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED


def test_ordinary_auto_repair_is_unchanged(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)

    result = _worker(
        tmp_path, run_id, (REJECTED, ACCEPTABLE), auto_recover=False, include_failed=False
    )
    decisions = _auto_repair_decisions(tmp_path, run_id)

    assert result.accepted == 1 and result.auto_repaired == 1
    assert result.repair_attempts == 1 and result.repair_decisions == 1
    assert result.repair_recovery_attempts == 0
    assert [item.repair_attempt_number for item in decisions] == [2]


def test_an_explicit_operator_repair_is_unchanged(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _worker(tmp_path, run_id, (REJECTED,), auto_repair=False)
    job = _job(tmp_path, run_id)

    result = repair_translation_job(
        run_id,
        job.job_id,
        project_root=tmp_path,
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="operator",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        provider_adapter=ScriptedRepairProvider((ACCEPTABLE,)),
        now=lambda: NOW + timedelta(seconds=30),
    )

    assert result.repaired_from_attempt == 1
    assert result.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    # An operator repair publishes no policy record, and is still charged one repair decision.
    assert _auto_repair_decisions(tmp_path, run_id) == ()
    assert _repair_decisions_used(_run_path(tmp_path, run_id), _job(tmp_path, run_id)) == 1


def test_an_operator_driven_failed_repair_is_still_recoverable(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _worker(tmp_path, run_id, (REJECTED,), auto_repair=False)
    job = _job(tmp_path, run_id)
    repair_translation_job(
        run_id,
        job.job_id,
        project_root=tmp_path,
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="operator",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        provider_adapter=ScriptedRepairProvider((_failure(),)),
        now=lambda: NOW + timedelta(seconds=30),
    )

    plan = plan_provider_recovery(run_id, job.job_id, project_root=tmp_path)

    assert plan.repair is not None
    # No auto-repair record exists to cite, because a person decided; the replay is still proven.
    assert plan.repair.repair_decision_file is None
    assert plan.repair.repair_decisions_used == 1
    assert plan.repair.request_reproduced_exactly


def test_a_failed_translation_and_a_failed_repair_are_recovered_in_that_order(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    jobs = load_run_summary(run_id, project_root=tmp_path).jobs
    # Strand the first job's repair, and fail the second job's translation outright.
    _worker(
        tmp_path,
        run_id,
        (REJECTED, _failure()),
        auto_recover=False,
        include_failed=False,
    )
    _worker(
        tmp_path,
        run_id,
        (_failure(),),
        auto_recover=False,
        include_failed=False,
        offset=20,
    )

    result = _worker(tmp_path, run_id, (ACCEPTABLE,), max_jobs=9, offset=40)
    remediated = [item.job_id for item in result.jobs if item.provider_recovery_attempts > 0]

    assert remediated == [jobs[1].job_id, jobs[0].job_id]


def test_a_recovered_repair_attempt_validates_like_any_other(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _worker(tmp_path, run_id, (REJECTED, _failure(), ACCEPTABLE))
    job = _job(tmp_path, run_id)
    listed = list_attempts(run_id, job.job_id, project_root=tmp_path)

    assert [item.validation for item in listed] == ["valid", "valid", "valid"]
    recovered = load_yaml(
        _run_path(tmp_path, run_id) / "jobs" / CHUNK / "attempts/003/attempt.yaml",
        TranslationAttempt,
    )
    assert recovered.execution_kind is TranslationExecutionKind.REPAIR
    assert recovered.repaired_from_attempt == 1


def test_a_recovered_repair_never_becomes_claimable_as_a_translation(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _stranded_repair(tmp_path, run_id)
    job = _job(tmp_path, run_id)

    plan = plan_provider_recovery(run_id, job.job_id, project_root=tmp_path)
    assert plan.restored_successful_attempt is not None
    restored = restore_repair_target(
        run_id,
        job.job_id,
        project_root=tmp_path,
        successful_attempt=plan.restored_successful_attempt,
        reason="test restoration",
        now=lambda: NOW + timedelta(seconds=40),
    )

    # The restored job never passes through `pending`, so no ordinary claim can reach it.
    assert restored.applied
    assert restored.resulting_status is TranslationJobStatus.SUCCEEDED
    assert restored.successful_attempt == 1
    settled = _job(tmp_path, run_id)
    assert settled.status is TranslationJobStatus.SUCCEEDED
    assert settled.successful_attempt == 1
    assert settled.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert is_auto_repair_candidate(settled)
    assert not is_failed_repair(settled)


# ------------------------------------------------------------------------------------------
# A repair claim that no provider ever answered
# ------------------------------------------------------------------------------------------


def _held_repair_claim(
    root: Path, run_id: str, *, worker_id: str, lease_seconds: int
) -> tuple[TranslationJob, TranslationJob]:
    """Stage the gap: a QA rejection, an operator repair claim, and no provider call at all.

    Returns the settled job as QA left it and the same job as the claim left it. The claim is
    taken through the production path rather than written by hand, because the defect this guards
    is precisely that the claim clears ``completed_at`` and nothing put it back.
    """
    _worker(root, run_id, (REJECTED,), auto_repair=False)
    settled = _job(root, run_id)
    assert settled.status is TranslationJobStatus.SUCCEEDED
    assert settled.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert settled.completed_at is not None

    claimed = _claim_repair(
        _run_path(root, run_id),
        settled.job_id,
        project_root=root,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        initiator=TranslationRepairInitiator.OPERATOR,
        now=NOW + timedelta(seconds=20),
    ).job
    assert claimed.status is TranslationJobStatus.CLAIMED
    assert claimed.completed_at is None
    return settled, claimed


def _acceptance_created_at(root: Path, job: TranslationJob) -> datetime:
    assert job.acceptance_file is not None
    return load_yaml(root / job.acceptance_file, TranslationAcceptanceEvidence).created_at


def _reloaded(root: Path, run_id: str, job: TranslationJob) -> TranslationJob:
    """Re-validate the job straight off disk, so the contract itself judges the written record."""
    path = _run_path(root, run_id) / "jobs" / job.chunk_id / "job.yaml"
    return TranslationJob.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def test_releasing_a_repair_claim_settles_it_back_to_its_own_completion(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    settled, claimed = _held_repair_claim(
        tmp_path, run_id, worker_id="repair-worker", lease_seconds=900
    )

    released = release_job(
        run_id,
        claimed.job_id,
        project_root=tmp_path,
        worker_id="repair-worker",
        reason="orchestration ended after the claim; no provider execution",
        now=lambda: NOW + timedelta(seconds=30),
    )

    assert released.status is TranslationJobStatus.SUCCEEDED
    assert released.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert released.completed_at == settled.completed_at
    assert released.completed_at == _acceptance_created_at(tmp_path, settled)
    assert released.attempt_count == claimed.attempt_count
    assert (released.claimed_by, released.claim_token) == (None, None)
    assert (released.claimed_at, released.lease_expires_at) == (None, None)
    # The written record is a valid TranslationJob, which before the fix it was not.
    assert _reloaded(tmp_path, run_id, released) == released
    assert is_auto_repair_candidate(released)


def test_an_expired_repair_claim_recovers_to_its_own_completion(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    settled, claimed = _held_repair_claim(
        tmp_path, run_id, worker_id="repair-worker", lease_seconds=60
    )

    result = recover_run(
        run_id,
        project_root=tmp_path,
        now=lambda: NOW + timedelta(seconds=200),
    )

    assert result.expired_claims == (claimed.job_id,)
    assert result.blockers == ()
    recovered = _job(tmp_path, run_id)
    assert recovered.status is TranslationJobStatus.SUCCEEDED
    assert recovered.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert recovered.completed_at == settled.completed_at
    assert recovered.attempt_count == claimed.attempt_count
    assert (recovered.claimed_by, recovered.claim_token) == (None, None)
    assert (recovered.claimed_at, recovered.lease_expires_at) == (None, None)
    assert _reloaded(tmp_path, run_id, recovered) == recovered


def test_an_ordinary_expired_translation_claim_still_returns_to_pending(tmp_path: Path) -> None:
    """The fix must not touch the claim a job that was never settled holds."""
    run_id = _prepared_run(tmp_path)
    claimed = claim_job(
        run_id,
        project_root=tmp_path,
        worker_id="translation-worker",
        lease_seconds=60,
        now=lambda: NOW,
    ).job
    assert claimed.acceptance_status is None

    released = release_job(
        run_id,
        claimed.job_id,
        project_root=tmp_path,
        worker_id="translation-worker",
        now=lambda: NOW + timedelta(seconds=10),
    )
    assert released.status is TranslationJobStatus.PENDING
    assert released.completed_at is None
    assert released.attempt_count == 1

    reclaimed = claim_job(
        run_id,
        project_root=tmp_path,
        worker_id="translation-worker",
        lease_seconds=60,
        now=lambda: NOW + timedelta(seconds=20),
    ).job
    recovered_run = recover_run(
        run_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=200)
    )
    assert recovered_run.expired_claims == (reclaimed.job_id,)
    recovered = _job(tmp_path, run_id)
    assert recovered.status is TranslationJobStatus.PENDING
    assert recovered.completed_at is None
    assert recovered.attempt_count == 2


def test_a_repair_claim_whose_acceptance_evidence_is_unreadable_fails_closed(
    tmp_path: Path,
) -> None:
    """No settlement is invented: an unprovable completion blocks rather than guesses a clock."""
    run_id = _prepared_run(tmp_path)
    _, claimed = _held_repair_claim(tmp_path, run_id, worker_id="repair-worker", lease_seconds=60)
    assert claimed.acceptance_file is not None
    (tmp_path / claimed.acceptance_file).write_text("status: tampered\n", encoding="utf-8")

    with pytest.raises(QueueError, match="acceptance evidence hash mismatch"):
        release_job(
            run_id,
            claimed.job_id,
            project_root=tmp_path,
            worker_id="repair-worker",
            now=lambda: NOW + timedelta(seconds=30),
        )

    result = recover_run(run_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=200))
    assert result.expired_claims == ()
    assert any("acceptance lineage" in blocker for blocker in result.blockers)
    # The claim is left exactly as it was rather than written back in an invalid shape.
    assert _job(tmp_path, run_id).status is TranslationJobStatus.CLAIMED


# ------------------------------------------------------------------------------------------
# Restoring a repair whose standing draft was carried forward, not produced here
# ------------------------------------------------------------------------------------------


def _carried_forward_failed_repair(root: Path) -> tuple[str, TranslationJob]:
    """Stage the real shape: an inherited QA-rejected draft whose repair the provider failed.

    The job's standing draft is a carry-forward, so it has no successful provider attempt at all
    and its `accepted_attempt` is a carry-forward number — which collides with the attempt slot
    the repair then spends.
    """
    from tests.test_translation_historical_reevaluation import (
        STRUCTURALLY_DEFECTIVE_OUTPUT,
        _carry_forward,
        _corrected_successor_run,
        _rejected_old_run,
    )

    old = _rejected_old_run(root, translation=STRUCTURALLY_DEFECTIVE_OUTPUT)
    new = _corrected_successor_run(root)
    _carry_forward(root, new, old)
    carried = _job(root, new)
    assert carried.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert (carried.carry_forward_count, carried.accepted_attempt) == (1, 1)
    assert (carried.attempt_count, carried.successful_attempt) == (0, None)

    repair_translation_job(
        new,
        carried.job_id,
        project_root=root,
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="repair-worker",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        provider_adapter=ScriptedRepairProvider((_failure(ProviderErrorCategory.AUTHENTICATION),)),
        now=lambda: NOW + timedelta(seconds=40),
    )
    failed = _job(root, new)
    assert failed.status is TranslationJobStatus.FAILED
    assert is_failed_repair(failed)
    assert failed.successful_attempt is None
    return new, failed


def test_a_carried_forward_repair_restores_without_claiming_a_provider_success(
    tmp_path: Path,
) -> None:
    run_id, failed = _carried_forward_failed_repair(tmp_path)

    plan = plan_repair_target_restoration(run_id, failed.job_id, project_root=tmp_path)
    assert plan.settlement == "carry-forward"
    assert plan.carry_forward_number == 1
    assert plan.successful_attempt is None
    assert plan.previous_status is TranslationJobStatus.FAILED
    assert plan.resulting_status is TranslationJobStatus.SUCCEEDED
    assert plan.applied is False
    # Planning writes nothing.
    assert _job(tmp_path, run_id).status is TranslationJobStatus.FAILED

    result = restore_repair_target(
        run_id,
        failed.job_id,
        project_root=tmp_path,
        reason="settling a repair the provider never delivered",
        now=lambda: NOW + timedelta(seconds=60),
    )

    assert result.applied
    restored = _job(tmp_path, run_id)
    assert restored.status is TranslationJobStatus.SUCCEEDED
    assert restored.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    # The whole point: no provider success is asserted for a draft this run never produced.
    assert restored.successful_attempt is None
    assert (restored.claimed_by, restored.claim_token) == (None, None)
    assert (restored.claimed_at, restored.lease_expires_at, restored.next_retry_at) == (
        None,
        None,
        None,
    )
    assert not is_failed_repair(restored)


def test_the_restored_completion_comes_from_the_standing_acceptance_evidence(
    tmp_path: Path,
) -> None:
    run_id, failed = _carried_forward_failed_repair(tmp_path)
    assert failed.completed_at is None
    assert failed.acceptance_file is not None
    settled_at = load_yaml(
        tmp_path / failed.acceptance_file, TranslationAcceptanceEvidence
    ).created_at

    plan = plan_repair_target_restoration(run_id, failed.job_id, project_root=tmp_path)
    assert plan.completed_at == settled_at

    restore_repair_target(
        run_id,
        failed.job_id,
        project_root=tmp_path,
        reason="settled completion is read, never supplied",
        now=lambda: NOW + timedelta(seconds=60),
    )
    assert _job(tmp_path, run_id).completed_at == settled_at


def test_attempt_accounting_and_history_survive_a_carry_forward_restoration(
    tmp_path: Path,
) -> None:
    run_id, failed = _carried_forward_failed_repair(tmp_path)
    before = _tree_of(tmp_path, run_id, failed.chunk_id)

    restore_repair_target(
        run_id,
        failed.job_id,
        project_root=tmp_path,
        reason="accounting must not move",
        now=lambda: NOW + timedelta(seconds=60),
    )

    restored = _job(tmp_path, run_id)
    assert restored.attempt_count == failed.attempt_count
    assert restored.abandoned_attempt_count == failed.abandoned_attempt_count
    assert restored.provider_attempt_count == failed.provider_attempt_count
    assert restored.last_attempt == failed.last_attempt
    assert restored.accepted_attempt == failed.accepted_attempt
    assert restored.carry_forward_count == failed.carry_forward_count
    # Every artifact beside the job record is immutable history and must be byte-identical.
    after = _tree_of(tmp_path, run_id, failed.chunk_id)
    assert {k: v for k, v in after.items() if k != "job.yaml"} == {
        k: v for k, v in before.items() if k != "job.yaml"
    }


def test_a_provider_backed_repair_still_restores_its_successful_attempt(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    failed = _stranded_repair(tmp_path, run_id)
    assert failed.carry_forward_count == 0

    plan = plan_repair_target_restoration(
        run_id, failed.job_id, project_root=tmp_path, successful_attempt=1
    )
    assert plan.settlement == "provider attempt"
    assert plan.carry_forward_number is None
    assert plan.successful_attempt == 1

    restore_repair_target(
        run_id,
        failed.job_id,
        project_root=tmp_path,
        successful_attempt=1,
        reason="provider-backed restoration is unchanged",
        now=lambda: NOW + timedelta(seconds=60),
    )
    restored = _job(tmp_path, run_id)
    assert restored.status is TranslationJobStatus.SUCCEEDED
    assert restored.successful_attempt == 1
    assert restored.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED


def test_a_provider_backed_repair_cannot_be_restored_without_its_attempt(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    failed = _stranded_repair(tmp_path, run_id)

    with pytest.raises(QueueError, match="carry-forward"):
        plan_repair_target_restoration(run_id, failed.job_id, project_root=tmp_path)


def test_a_carry_forward_repair_refuses_a_fabricated_successful_attempt(tmp_path: Path) -> None:
    run_id, failed = _carried_forward_failed_repair(tmp_path)

    with pytest.raises(QueueError, match="no successful provider attempt to restore"):
        plan_repair_target_restoration(
            run_id, failed.job_id, project_root=tmp_path, successful_attempt=1
        )


def test_an_ordinary_failed_translation_cannot_use_the_restoration_path(tmp_path: Path) -> None:
    """A failure with no standing rejection is a translation, and this path never claims it."""
    run_id = _prepared_run(tmp_path)
    _worker(
        tmp_path,
        run_id,
        (_failure(ProviderErrorCategory.AUTHENTICATION),),
        auto_recover=False,
        include_failed=False,
    )
    job = _job(tmp_path, run_id)
    assert job.status is TranslationJobStatus.FAILED
    assert job.acceptance_status is None
    assert not is_failed_repair(job)

    with pytest.raises(QueueError, match="failure ended"):
        plan_repair_target_restoration(run_id, job.job_id, project_root=tmp_path)


def test_tampered_carry_forward_evidence_fails_closed(tmp_path: Path) -> None:
    run_id, failed = _carried_forward_failed_repair(tmp_path)
    assert failed.latest_carry_forward_file is not None
    (tmp_path / failed.latest_carry_forward_file).write_text("schema_version: '1.0'\n")

    with pytest.raises(QueueError, match="does not verify"):
        plan_repair_target_restoration(run_id, failed.job_id, project_root=tmp_path)
    assert _job(tmp_path, run_id).status is TranslationJobStatus.FAILED


def test_a_tampered_carried_forward_translation_fails_closed(tmp_path: Path) -> None:
    run_id, failed = _carried_forward_failed_repair(tmp_path)
    evidence = load_yaml(
        tmp_path / failed.acceptance_file,  # type: ignore[operator]
        TranslationAcceptanceEvidence,
    )
    (tmp_path / evidence.translation_file).write_text("دست‌کاری شده.\n", encoding="utf-8")

    with pytest.raises(QueueError, match="translation hash mismatch"):
        plan_repair_target_restoration(run_id, failed.job_id, project_root=tmp_path)


def test_an_accepted_attempt_that_is_not_the_carry_forward_fails_closed(tmp_path: Path) -> None:
    run_id, failed = _carried_forward_failed_repair(tmp_path)
    path = _run_path(tmp_path, run_id) / "jobs" / failed.chunk_id / "job.yaml"
    path.write_text(
        yaml.safe_dump(
            failed.model_copy(update={"accepted_attempt": 2}).model_dump(
                mode="json", exclude_none=False
            ),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(QueueError, match="does not verify|carry-forward draft"):
        plan_repair_target_restoration(run_id, failed.job_id, project_root=tmp_path)


def test_a_successful_provider_attempt_blocks_a_null_restoration(tmp_path: Path) -> None:
    """If a provider success exists, the settlement is provider-backed and null would be a lie."""
    run_id, failed = _carried_forward_failed_repair(tmp_path)
    attempt_path = (
        _run_path(tmp_path, run_id)
        / "jobs"
        / failed.chunk_id
        / "attempts"
        / f"{failed.attempt_count:03d}"
        / "attempt.yaml"
    )
    attempt = load_yaml(attempt_path, TranslationAttempt)
    attempt_path.write_text(
        yaml.safe_dump(
            attempt.model_copy(
                update={
                    "status": TranslationAttemptStatus.SUCCEEDED,
                    "error_category": None,
                    "error_message": None,
                    "ambiguous": False,
                }
            ).model_dump(mode="json", exclude_none=False),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(QueueError, match="successful provider attempt exists"):
        plan_repair_target_restoration(run_id, failed.job_id, project_root=tmp_path)


def test_the_cli_plans_by_default_and_applies_only_on_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, failed = _carried_forward_failed_repair(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    argv = [
        "restore-translation-repair-target",
        run_id,
        failed.job_id,
        "--reason",
        "settling a repair the provider never delivered",
    ]

    planned = runner.invoke(app, argv, env={"COLUMNS": "200"})
    assert planned.exit_code == 0, planned.output
    assert "carry-forward" in planned.output
    assert "failed -> succeeded" in planned.output
    assert "no (plan only)" in planned.output
    assert _job(tmp_path, run_id).status is TranslationJobStatus.FAILED

    applied = runner.invoke(app, [*argv, "--apply"], env={"COLUMNS": "200"})
    assert applied.exit_code == 0, applied.output
    assert "yes" in applied.output
    restored = _job(tmp_path, run_id)
    assert restored.status is TranslationJobStatus.SUCCEEDED
    assert restored.successful_attempt is None
    # A second apply is refused rather than repeated: the job is no longer a failed repair.
    again = runner.invoke(app, [*argv, "--apply"], env={"COLUMNS": "200"})
    assert again.exit_code == 1


def test_a_published_execution_resolution_is_untouched_by_a_restoration(tmp_path: Path) -> None:
    """The operator's assertion about the provider call is history and must survive intact."""
    run_id, failed = _carried_forward_failed_repair(tmp_path)
    resolutions = _run_path(tmp_path, run_id) / "jobs" / failed.chunk_id / "resolutions"
    resolutions.mkdir(parents=True, exist_ok=True)
    marker = resolutions / "001" / "resolution.yaml"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("resolution: provider_contacted_no_result\n", encoding="utf-8")
    before = marker.read_bytes()

    restore_repair_target(
        run_id,
        failed.job_id,
        project_root=tmp_path,
        reason="resolution lineage is immutable",
        now=lambda: NOW + timedelta(seconds=60),
    )

    assert marker.read_bytes() == before
