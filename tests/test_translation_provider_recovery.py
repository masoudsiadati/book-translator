from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import (
    ProviderErrorCategory,
    ProviderFailureClass,
    ProviderName,
    ProviderPreflightDetail,
    ProviderPreflightRecord,
    ProviderRecoveryInitiator,
    ProviderRecoveryVerdict,
    ProviderReplaySafety,
    TranslationAcceptanceStatus,
    TranslationAttempt,
    TranslationJob,
    TranslationJobStatus,
    TranslationProviderRecoveryDecision,
    TranslationRunStatus,
)
from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
from book_translator.provider_execution import (
    ProviderExecutionError,
    ProviderPreflightReport,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from book_translator.provider_execution.artifacts import load_yaml
from book_translator.provider_execution.errors import ErrorClassification
from book_translator.provider_recovery_policy import (
    PROVIDER_RECOVERY_POLICY_VERSION,
    PreflightEvidence,
    ProviderRecoveryAssessment,
    ProviderRecoveryBudget,
    assess_provider_recovery,
    classify_provider_failure,
    cooldown_seconds,
)
from book_translator.translation_provider_recovery import (
    PROVIDER_RECOVERY_ENGINE_VERSION,
    ProviderRecoveryError,
    plan_provider_recovery,
    record_preflight,
)
from book_translator.translation_queue import (
    create_run,
    load_run_summary,
    plan_run,
    retry_job,
)
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import (
    TranslationWorkerError,
    WorkerResult,
    run_translation_worker,
)
from tests.test_translation_queue import NOW, SNAPSHOT_NOW
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import _options, _queue_bytes, _run_path

runner = CliRunner()

TWO_PARAGRAPHS = "بند نخست.\n\nبند دوم.\n"
ONE_PARAGRAPH = "فقط یک بند.\n"


def _prepared_run(root: Path, *, retry_backoff_seconds: int = 0, max_attempts: int = 3) -> str:
    """Create one synthetic run whose backoff is explicit, because recovery reasons about it."""
    synthetic_project(root)
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


class ScriptedFailingProvider:
    """A provider that fails with a chosen classification, then optionally starts succeeding."""

    provider_name = "fake"
    adapter_version = "test-1.0.0"

    def __init__(
        self,
        category: ProviderErrorCategory,
        *,
        failures: int = 1,
        retryable: bool = False,
        ambiguous: bool = False,
        texts: tuple[str, ...] = (TWO_PARAGRAPHS,),
        calls: list[str] | None = None,
    ) -> None:
        self.category = category
        self.failures = failures
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.texts = texts
        self.calls: list[str] = calls if calls is not None else []

    def execute(self, request: ProviderRequest) -> ProviderResult:
        self.calls.append(request.job_id)
        if len(self.calls) <= self.failures:
            raise ProviderExecutionError(
                ErrorClassification(
                    self.category,
                    self.retryable,
                    self.ambiguous,
                    False,
                    "synthetic provider failure",
                )
            )
        index = min(len(self.calls) - self.failures - 1, len(self.texts) - 1)
        return ProviderResult(
            response_id="synthetic-response",
            model=request.model or "fake-v1",
            status="completed",
            text_outputs=(self.texts[index],),
            usage=ProviderUsage(10, 10, 20),
            sanitized_response={"status": "completed"},
        )


HEALTHY_DETAILS = (
    ("executable", "claude"),
    ("cli_version", "2.1.235"),
    ("non_interactive", "supported"),
    ("authenticated", "yes"),
    ("auth_method", "claude.ai"),
    ("api_provider", "anthropic"),
    ("subscription_type", "max"),
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


def _fail_once(
    root: Path,
    run_id: str,
    *,
    category: ProviderErrorCategory = ProviderErrorCategory.AUTHENTICATION,
    retryable: bool = False,
    ambiguous: bool = False,
    now: datetime = NOW,
) -> WorkerResult:
    """Produce one genuine published provider failure through the ordinary worker path."""
    return run_translation_worker(
        run_id,
        project_root=root,
        options=_options(),
        provider_factory=lambda _job: ScriptedFailingProvider(
            category, failures=1, retryable=retryable, ambiguous=ambiguous
        ),
        now=lambda: now,
    )


def _job(root: Path, run_id: str, chunk_id: str = "001-001") -> TranslationJob:
    state = load_run_summary(run_id, project_root=root)
    return next(job for job in state.jobs if job.chunk_id == chunk_id)


def _decision_path(root: Path, run_id: str, attempt_number: int, chunk_id: str = "001-001") -> Path:
    return (
        _run_path(root, run_id)
        / "jobs"
        / chunk_id
        / "provider-recovery"
        / f"{attempt_number:03d}"
        / "decision.yaml"
    )


def _attempt_path(root: Path, run_id: str, attempt_number: int, chunk_id: str = "001-001") -> Path:
    return (
        _run_path(root, run_id)
        / "jobs"
        / chunk_id
        / "attempts"
        / f"{attempt_number:03d}"
        / "attempt.yaml"
    )


# --------------------------------------------------------------------------------------------
# The pure policy
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (ProviderErrorCategory.AUTHENTICATION, ProviderFailureClass.AUTHENTICATION),
        (ProviderErrorCategory.AUTHORIZATION, ProviderFailureClass.AUTHENTICATION),
        (ProviderErrorCategory.RATE_LIMIT, ProviderFailureClass.RATE_LIMIT),
        (ProviderErrorCategory.SERVER_ERROR, ProviderFailureClass.TRANSIENT),
        (ProviderErrorCategory.CONNECTION_PRE_SEND, ProviderFailureClass.TRANSIENT),
        (ProviderErrorCategory.TIMEOUT_AMBIGUOUS, ProviderFailureClass.AMBIGUOUS_EXECUTION),
        (ProviderErrorCategory.MALFORMED_RESPONSE, ProviderFailureClass.MALFORMED_RESPONSE),
        (ProviderErrorCategory.OUTPUT_TRUNCATED, ProviderFailureClass.MALFORMED_RESPONSE),
        (ProviderErrorCategory.INVALID_REQUEST, ProviderFailureClass.CONTRACT_VIOLATION),
        (ProviderErrorCategory.CONTENT_POLICY, ProviderFailureClass.CONTENT_POLICY),
        (ProviderErrorCategory.EXECUTABLE_NOT_FOUND, ProviderFailureClass.PROVIDER_UNAVAILABLE),
        (ProviderErrorCategory.LOCAL_IO, ProviderFailureClass.LOCAL_ENVIRONMENT),
        (ProviderErrorCategory.NONZERO_EXIT, ProviderFailureClass.UNKNOWN),
        (ProviderErrorCategory.UNKNOWN, ProviderFailureClass.UNKNOWN),
    ],
)
def test_every_executor_category_maps_to_one_recovery_family(
    category: ProviderErrorCategory, expected: ProviderFailureClass
) -> None:
    assert classify_provider_failure(category, ambiguous=False) is expected


def test_a_recorded_ambiguity_outranks_the_category_and_an_absent_category_fails_closed() -> None:
    assert (
        classify_provider_failure(ProviderErrorCategory.SERVER_ERROR, ambiguous=True)
        is ProviderFailureClass.AMBIGUOUS_EXECUTION
    )
    assert classify_provider_failure(None, ambiguous=False) is ProviderFailureClass.UNKNOWN


def _assess(
    failure_class: ProviderFailureClass,
    *,
    recovery_used: int = 0,
    max_recovery: int = 2,
    attempts_used: int = 1,
    max_attempts: int = 3,
    elapsed_seconds: int = 600,
    backoff: int = 60,
    preflight: PreflightEvidence | None = None,
) -> ProviderRecoveryAssessment:
    failed_at = NOW
    return assess_provider_recovery(
        failure_class=failure_class,
        budget=ProviderRecoveryBudget(max_recovery, recovery_used, max_attempts, attempts_used),
        failed_at=failed_at,
        retry_backoff_seconds=backoff,
        now=failed_at + timedelta(seconds=elapsed_seconds),
        preflight=preflight,
    )


def test_transient_failure_is_recoverable_without_any_provider_re_evidence() -> None:
    assessment = _assess(ProviderFailureClass.TRANSIENT)

    assert assessment.verdict is ProviderRecoveryVerdict.RECOVERABLE_NOW
    assert assessment.preflight_required is False
    assert assessment.policy_version == PROVIDER_RECOVERY_POLICY_VERSION


def test_authentication_failure_is_undecidable_until_a_preflight_is_supplied() -> None:
    assert _assess(ProviderFailureClass.AUTHENTICATION).verdict is (
        ProviderRecoveryVerdict.PREFLIGHT_REQUIRED
    )


def test_authentication_failure_with_a_healthy_fresh_preflight_is_recoverable() -> None:
    assessment = _assess(
        ProviderFailureClass.AUTHENTICATION,
        preflight=PreflightEvidence(performed=True, passed=True, authenticated=True),
    )

    assert assessment.verdict is ProviderRecoveryVerdict.RECOVERABLE_NOW
    assert assessment.preflight_required is True


@pytest.mark.parametrize(
    "preflight",
    [
        PreflightEvidence(performed=True, passed=False, authenticated=False),
        PreflightEvidence(performed=True, passed=True, authenticated=False),
    ],
)
def test_authentication_failure_stays_blocked_without_a_proven_login(
    preflight: PreflightEvidence,
) -> None:
    assessment = _assess(ProviderFailureClass.AUTHENTICATION, preflight=preflight)

    assert assessment.verdict is ProviderRecoveryVerdict.OPERATOR_REQUIRED
    assert assessment.blocking_reasons


@pytest.mark.parametrize(
    "failure_class",
    [
        ProviderFailureClass.MALFORMED_RESPONSE,
        ProviderFailureClass.CONTRACT_VIOLATION,
        ProviderFailureClass.CONTENT_POLICY,
        ProviderFailureClass.LOCAL_ENVIRONMENT,
        ProviderFailureClass.UNKNOWN,
    ],
)
def test_unsafe_families_fail_closed_however_healthy_the_provider_now_is(
    failure_class: ProviderFailureClass,
) -> None:
    assessment = _assess(
        failure_class,
        preflight=PreflightEvidence(performed=True, passed=True, authenticated=True),
    )

    assert assessment.verdict is ProviderRecoveryVerdict.OPERATOR_REQUIRED


def test_an_ambiguous_execution_is_never_replayed() -> None:
    assessment = _assess(ProviderFailureClass.AMBIGUOUS_EXECUTION)

    assert assessment.verdict is ProviderRecoveryVerdict.UNSAFE_TO_REPLAY
    assert assessment.blocking_reasons == ("execution_may_have_reached_the_provider",)


@pytest.mark.parametrize(
    ("recovery_used", "attempts_used", "expected_bound"),
    [(2, 1, "provider_recovery_attempts"), (0, 3, "attempts")],
)
def test_either_budget_alone_exhausts_recovery(
    recovery_used: int, attempts_used: int, expected_bound: str
) -> None:
    assessment = _assess(
        ProviderFailureClass.TRANSIENT, recovery_used=recovery_used, attempts_used=attempts_used
    )

    assert assessment.verdict is ProviderRecoveryVerdict.EXHAUSTED
    assert assessment.blocking_reasons == (f"{expected_bound}_exhausted",)


def test_a_failure_is_not_replayed_before_its_cooldown_has_elapsed() -> None:
    assessment = _assess(ProviderFailureClass.TRANSIENT, elapsed_seconds=30, backoff=60)

    assert assessment.verdict is ProviderRecoveryVerdict.WAIT_FOR_BACKOFF
    assert assessment.eligible_at == NOW + timedelta(seconds=60)


def test_a_rate_limited_provider_is_rested_longer_than_a_dropped_connection() -> None:
    assert cooldown_seconds(ProviderFailureClass.RATE_LIMIT, 60) == 300
    assert cooldown_seconds(ProviderFailureClass.TRANSIENT, 60) == 60
    assert (
        _assess(ProviderFailureClass.RATE_LIMIT, elapsed_seconds=120, backoff=60).verdict
        is ProviderRecoveryVerdict.WAIT_FOR_BACKOFF
    )


def test_a_preflight_proven_family_is_gated_by_proof_rather_than_by_a_timer() -> None:
    assert cooldown_seconds(ProviderFailureClass.AUTHENTICATION, 600) == 0
    assert cooldown_seconds(ProviderFailureClass.PROVIDER_UNAVAILABLE, 600) == 0
    assessment = _assess(
        ProviderFailureClass.AUTHENTICATION,
        elapsed_seconds=0,
        backoff=600,
        preflight=PreflightEvidence(performed=True, passed=True, authenticated=True),
    )

    assert assessment.verdict is ProviderRecoveryVerdict.RECOVERABLE_NOW


def test_the_budget_is_never_more_permissive_than_the_queue() -> None:
    budget = ProviderRecoveryBudget(
        max_provider_recovery_attempts=5,
        provider_recovery_attempts_used=0,
        max_provider_attempts=3,
        provider_attempts_used=2,
    )

    assert budget.remaining == 1 and budget.limiting_bound == "attempts"


# --------------------------------------------------------------------------------------------
# Preflight evidence
# --------------------------------------------------------------------------------------------


def test_preflight_evidence_records_provider_identity_without_secret_material() -> None:
    record = record_preflight(
        ProviderName.CLAUDE_CODE, performed_at=NOW, preflight=_healthy_preflight()
    )

    assert record.passed and record.authenticated and record.api_key_used is False
    assert record.executable_version == "2.1.235" and record.auth_method == "claude.ai"
    serialized = record.model_dump_json()
    assert "sk-" not in serialized and "token" not in serialized.lower()


def test_a_failing_preflight_is_recorded_as_evidence_rather_than_raised() -> None:
    record = record_preflight(
        ProviderName.CLAUDE_CODE, performed_at=NOW, preflight=_unhealthy_preflight
    )

    assert record.passed is False and record.authenticated is False
    assert record.failure_category is ProviderErrorCategory.AUTHENTICATION


@pytest.mark.parametrize(
    "value",
    [
        "sk-ant-api03-abcdefghijklmnop",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "Bearer abc123",
    ],
)
def test_preflight_details_refuse_credential_shaped_values(value: str) -> None:
    with pytest.raises(ValidationError):
        ProviderPreflightDetail(name="auth_method", value=value)


def test_preflight_evidence_refuses_an_api_key_path() -> None:
    with pytest.raises(ValidationError):
        ProviderPreflightRecord(
            provider=ProviderName.CLAUDE_CODE,
            provider_preflight_version="1.0.0",
            provider_implementation="claude-code",
            performed_at=NOW,
            supported=True,
            passed=True,
            authenticated=True,
            api_key_used=True,
        )


# --------------------------------------------------------------------------------------------
# Recovery against a real queue
# --------------------------------------------------------------------------------------------


def test_transient_failure_is_recovered_and_the_job_completes(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id, category=ProviderErrorCategory.SERVER_ERROR)
    assert _job(tmp_path, run_id).status is TranslationJobStatus.FAILED

    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_recover_provider_failures=True, include_failed=True),
        provider_factory=lambda _job: ScriptedFailingProvider(
            ProviderErrorCategory.SERVER_ERROR, failures=0
        ),
        now=lambda: NOW,
    )

    job = _job(tmp_path, run_id)
    assert result.provider_recovered == 1 and result.provider_recovery_attempts == 1
    assert result.jobs[0].outcome == "accepted"
    assert result.jobs[0].provider_failure_class == ProviderFailureClass.TRANSIENT.value
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.accepted_attempt == 2


def test_authentication_failure_with_a_failed_preflight_is_left_alone(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id)
    before = _queue_bytes(tmp_path, run_id)
    calls: list[str] = []

    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_recover_provider_failures=True, include_failed=True),
        provider_factory=lambda _job: ScriptedFailingProvider(
            ProviderErrorCategory.AUTHENTICATION, failures=0, calls=calls
        ),
        recovery_preflight=_unhealthy_preflight,
        now=lambda: NOW,
    )

    assert result.jobs[0].outcome == "provider_failed"
    assert result.jobs[0].provider_recovery_verdict == (
        ProviderRecoveryVerdict.OPERATOR_REQUIRED.value
    )
    assert not calls
    assert _queue_bytes(tmp_path, run_id) == before
    assert not _decision_path(tmp_path, run_id, 2).exists()


def test_authentication_failure_with_a_healthy_preflight_is_recovered(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id)
    preflights: list[ProviderName] = []

    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_recover_provider_failures=True, include_failed=True),
        provider_factory=lambda _job: ScriptedFailingProvider(
            ProviderErrorCategory.AUTHENTICATION, failures=0
        ),
        recovery_preflight=_healthy_preflight(preflights),
        now=lambda: NOW,
    )

    job = _job(tmp_path, run_id)
    assert preflights == [ProviderName.FAKE]
    assert result.jobs[0].outcome == "accepted"
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED

    decision = load_yaml(_decision_path(tmp_path, run_id, 2), TranslationProviderRecoveryDecision)
    assert decision.failure_class is ProviderFailureClass.AUTHENTICATION
    assert decision.historical_retryable is False
    assert decision.preflight_required is True
    assert decision.preflight is not None and decision.preflight.authenticated


@pytest.mark.parametrize(
    "category",
    [
        ProviderErrorCategory.MALFORMED_RESPONSE,
        ProviderErrorCategory.INVALID_REQUEST,
        ProviderErrorCategory.NONZERO_EXIT,
    ],
)
def test_unsafe_and_unclassified_failures_are_never_recovered(
    tmp_path: Path, category: ProviderErrorCategory
) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id, category=category)
    before = _queue_bytes(tmp_path, run_id)
    calls: list[str] = []

    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_recover_provider_failures=True, include_failed=True),
        provider_factory=lambda _job: ScriptedFailingProvider(category, failures=0, calls=calls),
        recovery_preflight=_healthy_preflight(),
        now=lambda: NOW,
    )

    assert result.jobs[0].outcome == "provider_failed"
    assert result.jobs[0].provider_recovery_verdict == (
        ProviderRecoveryVerdict.OPERATOR_REQUIRED.value
    )
    assert not calls and _queue_bytes(tmp_path, run_id) == before


def test_an_ambiguous_attempt_stays_with_the_reservation_boundary(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id, category=ProviderErrorCategory.TIMEOUT_AMBIGUOUS, ambiguous=True)
    job = _job(tmp_path, run_id)
    assert job.status is TranslationJobStatus.BLOCKED
    before = _queue_bytes(tmp_path, run_id)

    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_recover_provider_failures=True, include_failed=True),
        provider_factory=lambda _job: ScriptedFailingProvider(
            ProviderErrorCategory.UNKNOWN, failures=0
        ),
        recovery_preflight=_healthy_preflight(),
        now=lambda: NOW,
    )

    assert result.processed == 0 and _queue_bytes(tmp_path, run_id) == before
    with pytest.raises(ProviderRecoveryError, match="not a provider-failed job"):
        plan_provider_recovery(run_id, job.job_id, project_root=tmp_path, now=lambda: NOW)


def test_recovery_budget_exhaustion_stops_further_attempts(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id)
    calls: list[str] = []
    shared = ScriptedFailingProvider(ProviderErrorCategory.AUTHENTICATION, failures=5, calls=calls)

    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(
            auto_recover_provider_failures=True,
            include_failed=True,
            max_provider_recovery_attempts=1,
        ),
        provider_factory=lambda _job: shared,
        recovery_preflight=_healthy_preflight(),
        now=lambda: NOW,
    )

    job = _job(tmp_path, run_id)
    assert len(calls) == 1
    assert result.jobs[0].provider_recovery_attempts == 1
    assert result.jobs[0].provider_recovery_verdict == ProviderRecoveryVerdict.EXHAUSTED.value
    assert job.status is TranslationJobStatus.FAILED and job.attempt_count == 2


def test_a_second_provider_failure_is_recovered_within_the_bounded_budget(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id)
    preflights: list[ProviderName] = []

    provider = ScriptedFailingProvider(ProviderErrorCategory.AUTHENTICATION, failures=1)
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_recover_provider_failures=True, include_failed=True),
        provider_factory=lambda _job: provider,
        recovery_preflight=_healthy_preflight(preflights),
        now=lambda: NOW,
    )

    job = _job(tmp_path, run_id)
    # The first recovery's preflight cannot vouch for the provider that failed again after it.
    assert len(preflights) == 2
    assert result.jobs[0].provider_recovery_attempts == 2
    assert result.provider_recovered == 1
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.accepted_attempt == 3
    assert _decision_path(tmp_path, run_id, 2).is_file()
    assert _decision_path(tmp_path, run_id, 3).is_file()


def test_a_recovered_attempt_that_qa_rejects_is_handed_to_automatic_repair(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id)

    provider = ScriptedFailingProvider(
        ProviderErrorCategory.AUTHENTICATION, failures=0, texts=(ONE_PARAGRAPH, TWO_PARAGRAPHS)
    )
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(
            auto_recover_provider_failures=True,
            include_failed=True,
            auto_repair=True,
            include_qa_rejected=True,
        ),
        provider_factory=lambda _job: provider,
        recovery_preflight=_healthy_preflight(),
        now=lambda: NOW,
    )

    job = _job(tmp_path, run_id)
    assert result.jobs[0].outcome == "accepted"
    assert result.jobs[0].provider_recovery_attempts == 1
    assert result.jobs[0].repair_attempts == 1
    assert result.auto_repaired == 1 and result.provider_recovered == 1
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    # The two remedies stay distinct records over two distinct attempt slots.
    assert _decision_path(tmp_path, run_id, 2).is_file()
    assert (_run_path(tmp_path, run_id) / "jobs/001-001/auto-repair/003/decision.yaml").is_file()


def test_an_accepted_job_is_never_reprocessed(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: ScriptedFailingProvider(
            ProviderErrorCategory.UNKNOWN, failures=0
        ),
        now=lambda: NOW,
    )
    before = _queue_bytes(tmp_path, run_id)
    calls: list[str] = []

    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_recover_provider_failures=True, include_failed=True),
        provider_factory=lambda _job: ScriptedFailingProvider(
            ProviderErrorCategory.UNKNOWN, failures=0, calls=calls
        ),
        recovery_preflight=_healthy_preflight(),
        now=lambda: NOW,
    )

    assert result.processed == 0 and not calls
    assert _queue_bytes(tmp_path, run_id) == before


def test_an_existing_failed_job_is_resumed_only_when_explicitly_included(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id)
    before = _queue_bytes(tmp_path, run_id)

    untouched = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_recover_provider_failures=True),
        provider_factory=lambda _job: ScriptedFailingProvider(
            ProviderErrorCategory.AUTHENTICATION, failures=0
        ),
        recovery_preflight=_healthy_preflight(),
        now=lambda: NOW,
    )

    assert untouched.processed == 0 and _queue_bytes(tmp_path, run_id) == before

    resumed = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_recover_provider_failures=True, include_failed=True),
        provider_factory=lambda _job: ScriptedFailingProvider(
            ProviderErrorCategory.AUTHENTICATION, failures=0
        ),
        recovery_preflight=_healthy_preflight(),
        now=lambda: NOW,
    )

    assert resumed.jobs[0].outcome == "accepted"


def test_including_failed_jobs_requires_the_recovery_flag(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)

    with pytest.raises(TranslationWorkerError, match="auto-recover-provider-failures"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_options(include_failed=True),
            now=lambda: NOW,
        )


def test_a_dry_run_contacts_no_provider_and_mutates_nothing(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id)
    before = _queue_bytes(tmp_path, run_id)
    calls: list[str] = []
    job = _job(tmp_path, run_id)

    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(
            auto_recover_provider_failures=True,
            include_failed=True,
            job_id=job.job_id,
            dry_run=True,
        ),
        provider_factory=lambda _job: ScriptedFailingProvider(
            ProviderErrorCategory.AUTHENTICATION, failures=0, calls=calls
        ),
        recovery_preflight=_healthy_preflight(),
        now=lambda: NOW,
    )

    assert not calls
    assert _queue_bytes(tmp_path, run_id) == before
    assert result.dry_run and result.jobs[0].outcome == "would_recover_provider_failure"
    assert result.jobs[0].attempt_number == 2
    assert result.jobs[0].provider_recovery_verdict == (
        ProviderRecoveryVerdict.RECOVERABLE_NOW.value
    )


def test_the_historical_failed_attempt_stays_byte_identical(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id)
    historical = _attempt_path(tmp_path, run_id, 1).read_bytes()

    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_recover_provider_failures=True, include_failed=True),
        provider_factory=lambda _job: ScriptedFailingProvider(
            ProviderErrorCategory.AUTHENTICATION, failures=0
        ),
        recovery_preflight=_healthy_preflight(),
        now=lambda: NOW,
    )

    assert _attempt_path(tmp_path, run_id, 1).read_bytes() == historical
    attempt = load_yaml(_attempt_path(tmp_path, run_id, 1), TranslationAttempt)
    assert attempt.retryable is False and attempt.ambiguous is False
    assert attempt.error_category is ProviderErrorCategory.AUTHENTICATION


def test_recovery_provenance_is_immutable_and_distinct_from_repair(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id)
    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_recover_provider_failures=True, include_failed=True),
        provider_factory=lambda _job: ScriptedFailingProvider(
            ProviderErrorCategory.AUTHENTICATION, failures=0
        ),
        recovery_preflight=_healthy_preflight(),
        now=lambda: NOW,
    )
    decision_path = _decision_path(tmp_path, run_id, 2)
    published = decision_path.read_bytes()
    decision = load_yaml(decision_path, TranslationProviderRecoveryDecision)

    assert decision.provider_recovery_engine_version == PROVIDER_RECOVERY_ENGINE_VERSION
    assert decision.provider_recovery_policy_version == PROVIDER_RECOVERY_POLICY_VERSION
    assert decision.initiator is ProviderRecoveryInitiator.AUTOMATIC
    assert decision.verdict is ProviderRecoveryVerdict.RECOVERABLE_NOW
    assert decision.replay_safety is ProviderReplaySafety.FAILURE_IS_CONCLUSIVE
    assert decision.failed_attempt_number == 1 and decision.authorized_attempt_number == 2
    assert decision.provider_recovery_attempts_used == 0
    assert decision.remaining_provider_recovery_attempts == 2
    assert decision.provider_attempts_used == 1 and decision.max_provider_attempts == 3
    assert decision.historical_error_category is ProviderErrorCategory.AUTHENTICATION
    assert (
        decision.failed_attempt_sha256
        == __import__("hashlib").sha256(_attempt_path(tmp_path, run_id, 1).read_bytes()).hexdigest()
    )
    # A recovery is never encoded as a repair, and never as an ordinary queue retry.
    assert not (_run_path(tmp_path, run_id) / "jobs/001-001/auto-repair").exists()
    assert decision_path.read_bytes() == published


def test_a_persisted_decision_only_ever_describes_an_authorization() -> None:
    with pytest.raises(ValidationError):
        TranslationProviderRecoveryDecision(
            schema_version="1.0",
            provider_recovery_engine_version=PROVIDER_RECOVERY_ENGINE_VERSION,
            provider_recovery_policy_version=PROVIDER_RECOVERY_POLICY_VERSION,
            run_id="tr-0123456789ab-001",
            job_id="tr-0123456789ab-001__001-001",
            initiator=ProviderRecoveryInitiator.AUTOMATIC,
            verdict=ProviderRecoveryVerdict.OPERATOR_REQUIRED,
            provider=ProviderName.FAKE,
            failure_class=ProviderFailureClass.TRANSIENT,
            failed_attempt_number=1,
            failed_attempt_id="tr-0123456789ab-001__001-001__attempt-001",
            failed_attempt_file=Path("workspace/x/attempt.yaml"),
            failed_attempt_sha256="0" * 64,
            failed_at=NOW,
            historical_retryable=False,
            historical_ambiguous=False,
            replay_safety=ProviderReplaySafety.FAILURE_IS_CONCLUSIVE,
            provider_recovery_attempts_used=0,
            max_provider_recovery_attempts=2,
            remaining_provider_recovery_attempts=2,
            provider_attempts_used=1,
            max_provider_attempts=3,
            preflight_required=False,
            authorized_attempt_number=2,
            retry_eligible_at=NOW,
            created_at=NOW,
        )


def test_run_counters_stay_correct_across_a_recovery(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id)
    failed_state = load_run_summary(run_id, project_root=tmp_path)
    assert failed_state.run.failed_count == 1 and failed_state.run.accepted_count == 0

    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_recover_provider_failures=True, include_failed=True),
        provider_factory=lambda _job: ScriptedFailingProvider(
            ProviderErrorCategory.AUTHENTICATION, failures=0
        ),
        recovery_preflight=_healthy_preflight(),
        now=lambda: NOW,
    )

    state = load_run_summary(run_id, project_root=tmp_path)
    assert state.run.failed_count == 0
    assert state.run.retry_pending_count == 0 and state.run.pending_count == 0
    assert state.run.succeeded_count == 1 and state.run.accepted_count == 1
    assert state.run.status is TranslationRunStatus.COMPLETED
    assert not state.validation.counter_mismatch and not state.validation.status_mismatch


def test_a_cooling_failure_is_not_recovered_yet(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, retry_backoff_seconds=600)
    _fail_once(tmp_path, run_id, category=ProviderErrorCategory.SERVER_ERROR)
    before = _queue_bytes(tmp_path, run_id)

    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_recover_provider_failures=True, include_failed=True),
        provider_factory=lambda _job: ScriptedFailingProvider(
            ProviderErrorCategory.SERVER_ERROR, failures=0
        ),
        recovery_preflight=_healthy_preflight(),
        now=lambda: NOW + timedelta(seconds=60),
    )

    assert result.jobs[0].provider_recovery_verdict == (
        ProviderRecoveryVerdict.WAIT_FOR_BACKOFF.value
    )
    assert _queue_bytes(tmp_path, run_id) == before


# --------------------------------------------------------------------------------------------
# Compatibility with what already existed
# --------------------------------------------------------------------------------------------


def test_existing_queue_retry_behaviour_is_unchanged(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, retry_backoff_seconds=60)
    _fail_once(tmp_path, run_id)
    job = _job(tmp_path, run_id)

    scheduled = retry_job(
        run_id,
        job.job_id,
        project_root=tmp_path,
        reason="operator retry",
        now=lambda: NOW + timedelta(hours=1),
    )

    assert scheduled.status is TranslationJobStatus.RETRY_PENDING
    assert scheduled.next_retry_at == NOW + timedelta(hours=1, seconds=60)


def test_an_explicit_retry_eligibility_can_never_precede_the_decision(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, retry_backoff_seconds=60)
    _fail_once(tmp_path, run_id)
    job = _job(tmp_path, run_id)

    with pytest.raises(Exception, match="must not precede"):
        retry_job(
            run_id,
            job.job_id,
            project_root=tmp_path,
            reason="too early",
            eligible_at=NOW - timedelta(hours=1),
            now=lambda: NOW,
        )


def test_retry_translation_job_command_still_schedules_the_ordinary_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path, retry_backoff_seconds=60)
    _fail_once(tmp_path, run_id)
    job = _job(tmp_path, run_id)
    monkeypatch.chdir(tmp_path)

    invocation = runner.invoke(
        app,
        [
            "retry-translation-job",
            run_id,
            job.job_id,
            "--reason",
            "operator retry",
            "--now",
            (NOW + timedelta(hours=1)).isoformat(),
        ],
    )

    assert invocation.exit_code == 0
    retried = _job(tmp_path, run_id)
    assert retried.status is TranslationJobStatus.RETRY_PENDING
    assert retried.next_retry_at == NOW + timedelta(hours=1, seconds=60)


def test_the_worker_names_the_flags_a_failed_target_needs(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id)
    job = _job(tmp_path, run_id)

    with pytest.raises(TranslationWorkerError, match="--auto-recover-provider-failures"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_options(job_id=job.job_id),
            now=lambda: NOW,
        )


def test_recovery_refuses_a_negative_budget(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _fail_once(tmp_path, run_id)
    job = _job(tmp_path, run_id)

    with pytest.raises(ProviderRecoveryError, match="must not be negative"):
        plan_provider_recovery(
            run_id,
            job.job_id,
            project_root=tmp_path,
            max_provider_recovery_attempts=-1,
            now=lambda: NOW,
        )
