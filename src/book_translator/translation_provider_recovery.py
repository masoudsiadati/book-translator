"""Bounded, governed recovery of translation jobs that a provider failure ended.

This module is the orchestration layer between three things that must not absorb each other: the
immutable historical attempt, the pure recovery policy
(:mod:`book_translator.provider_recovery_policy`), and the existing queue lifecycle. It contains
no provider execution of its own. One cycle is ``classify -> assess -> preflight if required ->
authorize -> re-enter the queue``, and the recovered attempt is then executed by exactly the same
governed translation path every other attempt uses.

One recovery boundary serves both kinds of request a job can fail on, because the question is the
same one: a provider failed to carry out a request, and re-sending it is a scheduling decision that
has to be bounded, justified, and provable. What differs is what "the same request" means, and the
distinction is never blurred:

* Recovering a **translation** re-enters the job through the ordinary
  failed -> retry-pending -> pending lifecycle. The frozen chunk and the rendered prompt package
  already define what will be sent, so the queue can simply serve the job again.
* Recovering a **repair** re-derives the repair request from the standing QA rejection, proves it
  is byte-identical to the one that failed, restores the QA-rejected draft the failed attempt
  displaced, and hands the job back to the repair path. It never becomes a translation, it never
  points at the failed attempt as something to repair *from*, and it never counts as a second QA
  repair decision — QA chose that repair once, and the provider simply failed to deliver it.

Four properties matter more than convenience:

* **History is never rewritten.** The failed attempt is read, hashed, and referenced. Its
  ``retryable`` flag, its error category, and its bytes are untouched. A recovery decision is a
  new record that *cites* the old one.
* **It terminates.** Every authorization publishes a decision record before the queue is touched,
  and the used-budget count is derived from those records, so the budget the policy re-evaluates
  against strictly shrinks. The job's own provider-attempt budget bounds it a second time.
* **It never invents ordering.** Recovery re-enters a job through the lifecycle its kind belongs
  to, and does not fabricate a claim, skip the queue, or publish an attempt. A recovered
  translation becomes pending and is then claimed by ID, so no pending job is displaced; a
  recovered repair is restored to the repair-eligible state and is never claimable as a
  translation at all.
* **It says why.** The published decision names the policy version, the failure family, the
  preflight evidence, and the budget the attempt was drawn against, so a spent provider attempt
  stays explainable long after the queue event ring buffer has rotated.
"""

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from book_translator.models import (
    SCHEMA_VERSION,
    ProviderFailureClass,
    ProviderName,
    ProviderPreflightDetail,
    ProviderPreflightRecord,
    ProviderRecoveryInitiator,
    ProviderRecoveryVerdict,
    ProviderReplaySafety,
    TranslationAcceptanceStatus,
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationAutoRepairDecision,
    TranslationExecutionKind,
    TranslationJob,
    TranslationJobStatus,
    TranslationProviderRecoveryDecision,
    TranslationRepairRecoveryLineage,
)
from book_translator.paths import ProjectPaths
from book_translator.provider_execution import (
    PROVIDER_PREFLIGHT_VERSION,
    ProviderExecutionError,
    ProviderPreflightReport,
    preflight_provider,
)
from book_translator.provider_execution.artifacts import (
    load_yaml,
    private_write,
    project_relative,
    serialize_yaml,
    sha256_bytes,
)
from book_translator.provider_execution.errors import ExecutorError
from book_translator.provider_recovery_policy import (
    DEFAULT_MAX_PROVIDER_RECOVERY_ATTEMPTS,
    PreflightEvidence,
    ProviderRecoveryAssessment,
    ProviderRecoveryBudget,
    RepairReplayEvidence,
    assess_provider_recovery,
    classify_provider_failure,
)
from book_translator.translation_queue import (
    QueueError,
    recover_run,
    restore_repair_target,
    retry_job,
)
from book_translator.translation_queue.workflow import _load_queue, _resolve_run_path
from book_translator.translation_repair import (
    REPAIR_PROMPT_VERSION,
    RepairRequest,
    TranslationRepairError,
    build_repair_request,
)
from book_translator.translation_repair_policy import (
    DEFAULT_MAX_REPAIR_ATTEMPTS,
    is_failed_repair,
)

PROVIDER_RECOVERY_ENGINE_VERSION = "1.0.0"

#: Details a provider preflight reports that are already modelled as typed fields. They stay in
#: the record's typed half rather than being duplicated into the free-form detail list.
_TYPED_PREFLIGHT_DETAILS = frozenset(
    {"executable", "cli_version", "authenticated", "auth_method", "api_key_used"}
)


class ProviderRecoveryError(ValueError):
    """Raised when provider recovery cannot safely continue."""


def is_provider_recovery_candidate(job: TranslationJob) -> bool:
    """Return whether a job is in the state an ordinary *translation* recovery may act on.

    A published provider failure on a job that has no assessed draft is the whole precondition,
    and both halves matter. A blocked job is deliberately outside it: ambiguity is resolved
    through ``resolve-translation-reservation``, never replayed here. So is a job carrying an
    acceptance status, because there the failed attempt was a *repair* of an existing rejected
    draft; re-entering that as an ordinary translation would spend a provider attempt whose
    result the QA rejection on record would not describe. That job belongs to
    :func:`is_repair_recovery_candidate`, which recovers it *as a repair*.
    """
    return job.status is TranslationJobStatus.FAILED and job.acceptance_status is None


def is_repair_recovery_candidate(job: TranslationJob) -> bool:
    """Return whether a job is in the state a *repair* recovery may act on.

    This is the shape a failed repair leaves: the job failed, the QA rejection it was answering
    still stands, and the accepted attempt is still the rejected draft rather than the attempt
    that just failed. Recovering it re-sends the same repair; it is never re-entered as a
    translation.
    """
    return is_failed_repair(job)


def recovery_execution_kind(job: TranslationJob) -> TranslationExecutionKind | None:
    """Return what kind of request recovering this job would re-send, or ``None`` if neither.

    The two classes are mutually exclusive by construction — one requires no acceptance status
    and the other requires a standing rejection — so this is a total answer rather than a
    priority order.
    """
    if is_provider_recovery_candidate(job):
        return TranslationExecutionKind.TRANSLATION
    if is_repair_recovery_candidate(job):
        return TranslationExecutionKind.REPAIR
    return None


def _preflight_details(report: ProviderPreflightReport) -> list[ProviderPreflightDetail]:
    return [
        ProviderPreflightDetail(name=name, value=value)
        for name, value in report.details
        if name not in _TYPED_PREFLIGHT_DETAILS
    ]


def _detail(report: ProviderPreflightReport, name: str) -> str | None:
    return next((value for key, value in report.details if key == name), None)


def record_preflight(
    provider: ProviderName,
    *,
    performed_at: datetime,
    preflight: Callable[[ProviderName], ProviderPreflightReport] | None = None,
) -> ProviderPreflightRecord:
    """Take one provider preflight and turn it into durable, credential-free evidence.

    A failing preflight is evidence too: it is recorded as a failed record rather than raised, so
    the caller can stop recovery cleanly instead of treating an unhealthy provider as an error.
    """
    runner = preflight or preflight_provider
    try:
        report = runner(provider)
    except ProviderExecutionError as exc:
        return ProviderPreflightRecord(
            provider=provider,
            provider_preflight_version=PROVIDER_PREFLIGHT_VERSION,
            provider_implementation=provider.value,
            performed_at=performed_at,
            supported=True,
            passed=False,
            authenticated=False,
            failure_category=exc.classification.category,
            failure_message=exc.classification.message,
        )
    return ProviderPreflightRecord(
        provider=provider,
        provider_preflight_version=PROVIDER_PREFLIGHT_VERSION,
        provider_implementation=report.provider,
        performed_at=performed_at,
        supported=report.supported,
        passed=report.passed,
        authenticated=_detail(report, "authenticated") == "yes",
        auth_method=_detail(report, "auth_method"),
        executable=_detail(report, "executable"),
        executable_version=_detail(report, "cli_version"),
        api_key_used=_detail(report, "api_key_used") == "yes",
        details=_preflight_details(report),
    )


def _evidence(record: ProviderPreflightRecord | None) -> PreflightEvidence | None:
    if record is None:
        return None
    return PreflightEvidence(
        performed=True, passed=record.passed, authenticated=record.authenticated
    )


@dataclass(frozen=True, slots=True)
class ProviderRecoveryPlan:
    """A complete read-only answer for one provider-failed job: what would happen, and why."""

    run_id: str
    job_id: str
    job: TranslationJob
    failed_attempt: TranslationAttempt
    failed_attempt_file: Path
    failed_attempt_sha256: str
    failure_class: ProviderFailureClass
    replay_safety: ProviderReplaySafety
    assessment: ProviderRecoveryAssessment
    preflight: ProviderPreflightRecord | None
    authorized_attempt_number: int
    #: Present exactly when a failed repair is being recovered: the proof, entirely in hashes,
    #: that the same repair would be re-sent. ``None`` for an ordinary translation recovery.
    repair: TranslationRepairRecoveryLineage | None = None
    #: The restored draft a repair recovery would put back, read from the immutable attempt.
    restored_successful_attempt: int | None = None
    restored_completed_at: datetime | None = None

    @property
    def verdict(self) -> ProviderRecoveryVerdict:
        return self.assessment.verdict

    @property
    def recoverable(self) -> bool:
        return self.assessment.recoverable

    @property
    def execution_kind(self) -> TranslationExecutionKind:
        return self.assessment.execution_kind

    @property
    def repair_root_attempt(self) -> int | None:
        """The attempt a recovered repair would still repair *from*, if this recovers a repair."""
        return self.repair.repair_root_attempt if self.repair is not None else None


@dataclass(frozen=True, slots=True)
class ProviderRecoveryAuthorization:
    """What one published authorization did to the queue."""

    run_id: str
    job_id: str
    decision_file: Path
    authorized_attempt_number: int
    job: TranslationJob
    execution_kind: TranslationExecutionKind = TranslationExecutionKind.TRANSLATION


def _attempt_directory(run_path: Path, job: TranslationJob, attempt_number: int) -> Path:
    expected = ProjectPaths(run_path.parents[2]).translation_attempt(
        job.run_id, job.chunk_id, attempt_number
    )
    if expected.parent.parent.parent.parent != run_path:
        raise ProviderRecoveryError("translation attempt path does not match run lineage")
    return expected


def _recovery_decisions(
    run_path: Path, job: TranslationJob
) -> tuple[TranslationProviderRecoveryDecision, ...]:
    """Load every recovery authorization published for one job, in attempt-slot order."""
    directory = run_path / "jobs" / job.chunk_id / "provider-recovery"
    if not directory.is_dir():
        return ()
    return tuple(
        load_yaml(path, TranslationProviderRecoveryDecision)
        for path in sorted(directory.glob("*/decision.yaml"))
    )


def _recovery_attempts_used(run_path: Path, job: TranslationJob) -> int:
    """Count the recovery attempts this job has already spent, from the decisions themselves.

    The count is derived rather than stored so it cannot drift, and so a recovery an operator
    drove is charged against exactly the same budget as an automatic one.

    Recovering a translation and recovering a repair draw on the *same* allowance deliberately.
    The budget answers "how many times may an unattended process re-enter this one job after a
    provider failure", and that question does not become two questions because the request that
    failed happened to be a repair. Sharing it is also the conservative choice: it can only ever
    permit fewer automatic re-entries than two separate counters would.
    """
    directory = run_path / "jobs" / job.chunk_id / "provider-recovery"
    if not directory.is_dir():
        return 0
    return sum(1 for _ in directory.glob("*/decision.yaml"))


def _replay_safety(
    run_path: Path, job: TranslationJob, attempt: TranslationAttempt
) -> ProviderReplaySafety:
    """Establish why re-entering this job cannot duplicate or overwrite paid work.

    Safety is asserted from artifacts, never assumed: the attempt must have failed conclusively,
    have produced no stored output, and have left no outstanding execution reservation.
    """
    if attempt.ambiguous or attempt.status is not TranslationAttemptStatus.FAILED:
        return ProviderReplaySafety.UNSAFE
    if (
        attempt.translation_file is not None
        or attempt.response_file is not None
        or attempt.rejected_output_file is not None
    ):
        # A rejected answer is stored provider output like any other: the call was paid for and
        # something arrived, so replaying it "as if nothing happened" would be a fiction.
        return ProviderReplaySafety.UNSAFE
    attempts = run_path / "jobs" / job.chunk_id / "attempts"
    if (attempts / f".{attempt.attempt_number:03d}.reservation.yaml").exists():
        return ProviderReplaySafety.UNSAFE
    resolutions = run_path / "jobs" / job.chunk_id / "resolutions"
    if (resolutions / f"{attempt.attempt_number:03d}" / "resolution.yaml").is_file():
        return ProviderReplaySafety.RESERVATION_RESOLVED
    return ProviderReplaySafety.FAILURE_IS_CONCLUSIVE


def _repair_decisions_used(run_path: Path, job: TranslationJob) -> int:
    """Count the QA repair *decisions* charged to this job, from the artifacts themselves.

    A repair execution is not the same thing as a repair decision. QA decides once that a
    rejection should be answered by a repair; if the provider fails to carry that out, bounded
    recovery re-sends the *same* request, and charging the repair budget again would let one
    provider outage look like two QA judgements and exhaust the allowance without QA ever having
    changed its mind.

    So the count is repair-kind attempts minus the attempt slots a repair recovery authorized.
    Both halves are read from artifacts rather than stored, so the number cannot drift, and an
    operator-driven repair is charged exactly like an automatic one.
    """
    attempts = run_path / "jobs" / job.chunk_id / "attempts"
    if not attempts.is_dir():
        return 0
    replayed = {
        decision.authorized_attempt_number
        for decision in _recovery_decisions(run_path, job)
        if decision.recovered_execution_kind is TranslationExecutionKind.REPAIR
    }
    used = 0
    for metadata in sorted(attempts.glob("*/attempt.yaml")):
        attempt = load_yaml(metadata, TranslationAttempt)
        if (
            attempt.execution_kind is TranslationExecutionKind.REPAIR
            and attempt.attempt_number not in replayed
        ):
            used += 1
    return used


def _auto_repair_decision(
    run_path: Path, job: TranslationJob, attempt_number: int, project_root: Path
) -> tuple[Path, str] | None:
    """Return the auto-repair authorization for one repair attempt slot, if one was published.

    An operator repair publishes none, because a person deciding needs no policy record. Its
    absence is therefore information rather than a defect, and is recorded as such.
    """
    path = (
        run_path / "jobs" / job.chunk_id / "auto-repair" / f"{attempt_number:03d}" / "decision.yaml"
    )
    if not path.is_file():
        return None
    decision = load_yaml(path, TranslationAutoRepairDecision)
    if decision.job_id != job.job_id or decision.repair_attempt_number != attempt_number:
        raise ProviderRecoveryError("auto-repair decision lineage does not match the job")
    return project_relative(path, project_root), sha256_bytes(path.read_bytes())


def _recorded_request(attempt_directory: Path, attempt: TranslationAttempt) -> dict[str, object]:
    """Load and hash-verify what the failed attempt recorded about the request it sent."""
    request_path = attempt_directory / "request.json"
    request_bytes = request_path.read_bytes()
    if sha256_bytes(request_bytes) != attempt.request_sha256:
        raise ProviderRecoveryError("failed attempt request payload hash mismatch")
    payload = json.loads(request_bytes.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ProviderRecoveryError("failed attempt request payload is not a mapping")
    return payload


def _recorded_message_hash(payload: dict[str, object], key: str) -> str:
    section = payload.get(key)
    if not isinstance(section, dict):
        raise ProviderRecoveryError(f"failed attempt request payload lacks {key}")
    recorded = section.get("sha256")
    if not isinstance(recorded, str):
        raise ProviderRecoveryError(f"failed attempt request payload lacks a {key} hash")
    return recorded


def _repair_replay(
    run_path: Path,
    job: TranslationJob,
    attempt: TranslationAttempt,
    attempt_directory: Path,
    *,
    project_root: Path,
    max_repair_attempts: int,
) -> tuple[TranslationRepairRecoveryLineage | None, RepairReplayEvidence]:
    """Prove — or refuse to prove — that the exact failed repair request can be re-sent.

    The proof is deliberately a chain of hashes rather than a comparison of text. Re-deriving the
    repair reads the standing rejection, the rejected draft, and the frozen resources, each
    verified against the hash the evidence recorded for it; the derived messages are then hashed
    and matched against what the failed attempt itself recorded sending. If any link differs —
    tampered evidence, an edited draft, a frozen resource that moved, a request payload someone
    rewrote — no lineage is produced and the reason is named. Nothing here contacts a provider,
    writes anything, or holds derived prose beyond the call.
    """

    def refuse(*reasons: str) -> tuple[None, RepairReplayEvidence]:
        return None, RepairReplayEvidence(False, False, reasons)

    if attempt.execution_kind is not TranslationExecutionKind.REPAIR:
        return refuse("failed_attempt_is_not_a_repair")
    if attempt.repaired_from_attempt is None:
        return refuse("failed_repair_attempt_lacks_repair_lineage")
    try:
        request: RepairRequest = build_repair_request(
            run_path, job, project_root=project_root, allow_failed_repair=True
        )
    except (TranslationRepairError, ExecutorError, OSError, UnicodeError, ValueError):
        # The reason is deliberately a stable token: the underlying message can name workspace
        # paths, and this string is persisted in the run's own audit trail.
        return refuse("repair_request_could_not_be_rederived")
    if attempt.repaired_from_attempt != request.repaired_from_attempt:
        return refuse("failed_repair_targets_a_different_rejected_attempt")
    if request.repaired_from_attempt != job.accepted_attempt:
        return refuse("repair_root_is_not_the_standing_accepted_attempt")
    try:
        payload = _recorded_request(attempt_directory, attempt)
        recorded_system = _recorded_message_hash(payload, "system_message")
        recorded_user = _recorded_message_hash(payload, "user_message")
        stored_system = sha256_bytes((attempt_directory / "repair-system.md").read_bytes())
        stored_user = sha256_bytes((attempt_directory / "repair-user.md").read_bytes())
    except (ProviderRecoveryError, OSError, UnicodeError, json.JSONDecodeError):
        return refuse("failed_repair_request_evidence_is_unreadable_or_tampered")
    if stored_system != recorded_system or stored_user != recorded_user:
        return refuse("failed_repair_request_messages_do_not_match_their_recorded_hashes")
    if payload.get("output_contract") != attempt.output_contract:
        return refuse("failed_repair_output_contract_disagrees_with_its_attempt")
    if request.system_sha256 != recorded_system or request.user_sha256 != recorded_user:
        return refuse("repair_request_not_reproducible")
    decision = _auto_repair_decision(run_path, job, attempt.attempt_number, project_root)
    rejection = request.rejection
    triggering = sorted(
        {
            finding.code
            for finding in rejection.terminology_report.findings
            if finding.severity == "error"
        }
        | {
            finding.code
            for finding in rejection.structural_report.findings
            if finding.severity == "error"
        }
    )
    if not triggering:
        return refuse("standing_rejection_no_longer_carries_a_blocking_finding")
    lineage = TranslationRepairRecoveryLineage(
        recovered_execution_kind=TranslationExecutionKind.REPAIR,
        repair_root_attempt=request.repaired_from_attempt,
        failed_repair_attempt=attempt.attempt_number,
        repair_decision_file=decision[0] if decision is not None else None,
        repair_decision_sha256=decision[1] if decision is not None else None,
        rejection_origin=rejection.origin,
        rejection_evidence_file=rejection.evidence_file,
        rejection_evidence_sha256=rejection.evidence_sha256,
        triggering_codes=triggering,
        rejected_attempt_file=rejection.attempt_file,
        rejected_attempt_sha256=rejection.attempt_sha256,
        rejected_translation_sha256=request.rejected_translation_sha256,
        source_sha256=request.source_sha256,
        context_sha256=request.context_sha256,
        prompt_sha256=request.prompt_sha256,
        style_guide_sha256=request.style_guide_sha256,
        terminology_sha256=request.terminology_sha256,
        repair_prompt_version=REPAIR_PROMPT_VERSION,
        output_contract=attempt.output_contract,
        repair_request_system_sha256=request.system_sha256,
        repair_request_user_sha256=request.user_sha256,
        repair_request_identity_sha256=request.identity_sha256,
        request_reproduced_exactly=True,
        repair_decisions_used=max(1, _repair_decisions_used(run_path, job)),
        max_repair_attempts=max(1, max_repair_attempts),
    )
    return lineage, RepairReplayEvidence(True, True, ())


def _restored_draft(
    run_path: Path, job: TranslationJob, project_root: Path
) -> tuple[int, datetime]:
    """Read the draft a repair recovery would restore, from the immutable accepted attempt.

    Both values come from the attempt the job already points at, never from the clock or from a
    caller: restoring a draft must put back what was there, not something plausible.
    """
    if job.accepted_attempt is None:
        raise ProviderRecoveryError("a failed repair must reference the draft it was repairing")
    attempt_path = _attempt_directory(run_path, job, job.accepted_attempt) / "attempt.yaml"
    attempt = load_yaml(attempt_path, TranslationAttempt)
    if (
        attempt.job_id != job.job_id
        or attempt.status is not TranslationAttemptStatus.SUCCEEDED
        or attempt.completed_at is None
    ):
        raise ProviderRecoveryError("the draft a failed repair displaced is not a provider success")
    return job.accepted_attempt, attempt.completed_at


def plan_provider_recovery(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
    max_provider_recovery_attempts: int = DEFAULT_MAX_PROVIDER_RECOVERY_ATTEMPTS,
    max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
    preflight: ProviderPreflightRecord | None = None,
    now: Callable[[], datetime] | None = None,
) -> ProviderRecoveryPlan:
    """Assess one provider-failed job without mutating anything or contacting any provider.

    The job's own shape decides which kind of request would be re-sent: a failure with no
    assessed draft is a translation, a failure that left a standing QA rejection intact is a
    repair. Nothing selects between them by argument, so a caller cannot ask for a failed repair
    to be recovered as a translation.

    Pass ``preflight`` only with evidence taken after the failure. Without it a family that
    depends on provider health can only ever be answered ``preflight_required``.
    ``max_repair_attempts`` is recorded on a repair recovery for audit; it does not bound one,
    because replaying a repair spends no new QA repair decision.
    """
    if max_provider_recovery_attempts < 0:
        raise ProviderRecoveryError("maximum provider recovery attempts must not be negative")
    if max_repair_attempts < 0:
        raise ProviderRecoveryError("maximum repair attempts must not be negative")
    project_root = project_root.resolve()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    state = _load_queue(run_path, project_root)
    job = next((item for item in state.jobs if item.job_id == job_id), None)
    if job is None:
        raise ProviderRecoveryError(f"translation job not found: {job_id}")
    execution_kind = recovery_execution_kind(job)
    if execution_kind is None:
        raise ProviderRecoveryError(
            f"job {job_id} is not a provider-failed job and cannot be recovered"
        )
    if job.last_attempt is None:
        raise ProviderRecoveryError("a provider-failed job must reference its failed attempt")
    attempt_directory = _attempt_directory(run_path, job, job.last_attempt)
    attempt_path = attempt_directory / "attempt.yaml"
    attempt = load_yaml(attempt_path, TranslationAttempt)
    if attempt.job_id != job.job_id or attempt.attempt_number != job.last_attempt:
        raise ProviderRecoveryError("failed attempt lineage does not match the job")
    failure_class = classify_provider_failure(attempt.error_category, ambiguous=attempt.ambiguous)
    safety = _replay_safety(run_path, job, attempt)
    budget = ProviderRecoveryBudget(
        max_provider_recovery_attempts=max_provider_recovery_attempts,
        provider_recovery_attempts_used=_recovery_attempts_used(run_path, job),
        max_provider_attempts=job.max_attempts,
        provider_attempts_used=job.provider_attempt_count,
    )
    repair: TranslationRepairRecoveryLineage | None = None
    replay: RepairReplayEvidence | None = None
    restored: tuple[int, datetime] | None = None
    if execution_kind is TranslationExecutionKind.REPAIR:
        repair, replay = _repair_replay(
            run_path,
            job,
            attempt,
            attempt_directory,
            project_root=project_root,
            max_repair_attempts=max_repair_attempts,
        )
        if repair is not None:
            restored = _restored_draft(run_path, job, project_root)
    assessment = assess_provider_recovery(
        failure_class=failure_class,
        budget=budget,
        failed_at=attempt.completed_at or attempt.started_at,
        retry_backoff_seconds=state.run.retry_backoff_seconds,
        now=(now or (lambda: datetime.now(UTC)))(),
        preflight=_evidence(preflight),
        execution_kind=execution_kind,
        repair=replay,
    )
    if safety is ProviderReplaySafety.UNSAFE and assessment.recoverable:
        assessment = ProviderRecoveryAssessment(
            assessment.policy_version,
            ProviderRecoveryVerdict.UNSAFE_TO_REPLAY,
            failure_class,
            budget,
            assessment.preflight_required,
            assessment.eligible_at,
            ("failed_attempt_replay_safety_unproven",),
            execution_kind,
        )
    return ProviderRecoveryPlan(
        run_id=job.run_id,
        job_id=job.job_id,
        job=job,
        failed_attempt=attempt,
        failed_attempt_file=project_relative(attempt_path, project_root),
        failed_attempt_sha256=sha256_bytes(attempt_path.read_bytes()),
        failure_class=failure_class,
        replay_safety=safety,
        assessment=assessment,
        preflight=preflight,
        authorized_attempt_number=job.attempt_count + 1,
        repair=repair,
        restored_successful_attempt=restored[0] if restored is not None else None,
        restored_completed_at=restored[1] if restored is not None else None,
    )


def _decision(
    plan: ProviderRecoveryPlan, initiator: ProviderRecoveryInitiator, now: datetime
) -> TranslationProviderRecoveryDecision:
    budget = plan.assessment.budget
    attempt = plan.failed_attempt
    return TranslationProviderRecoveryDecision(
        schema_version=SCHEMA_VERSION,
        provider_recovery_engine_version=PROVIDER_RECOVERY_ENGINE_VERSION,
        provider_recovery_policy_version=plan.assessment.policy_version,
        run_id=plan.run_id,
        job_id=plan.job_id,
        initiator=initiator,
        verdict=plan.assessment.verdict,
        provider=attempt.provider,
        failure_class=plan.failure_class,
        failed_attempt_number=attempt.attempt_number,
        failed_attempt_id=attempt.attempt_id,
        failed_attempt_file=plan.failed_attempt_file,
        failed_attempt_sha256=plan.failed_attempt_sha256,
        failed_at=attempt.completed_at or attempt.started_at,
        historical_error_category=attempt.error_category,
        historical_retryable=attempt.retryable,
        historical_ambiguous=attempt.ambiguous,
        replay_safety=plan.replay_safety,
        recovered_execution_kind=plan.execution_kind,
        repair=plan.repair,
        provider_recovery_attempts_used=budget.provider_recovery_attempts_used,
        max_provider_recovery_attempts=budget.max_provider_recovery_attempts,
        remaining_provider_recovery_attempts=budget.remaining,
        provider_attempts_used=budget.provider_attempts_used,
        max_provider_attempts=budget.max_provider_attempts,
        preflight_required=plan.assessment.preflight_required,
        preflight=plan.preflight,
        authorized_attempt_number=plan.authorized_attempt_number,
        retry_eligible_at=plan.assessment.eligible_at,
        created_at=now,
    )


def _publish_decision(
    run_path: Path,
    plan: ProviderRecoveryPlan,
    *,
    initiator: ProviderRecoveryInitiator,
    now: datetime,
) -> Path:
    """Publish the authorization for one recovery attempt, or reuse an identical existing one.

    Writing before the queue is touched is deliberate: an interrupted recovery must still be able
    to explain the attempt slot it consumed. Re-entering the same slot after an interruption
    reuses the record instead of rewriting it, so recovery is idempotent and no evidence is
    replaced.
    """
    destination = ProjectPaths(run_path.parents[2]).translation_provider_recovery_decision(
        plan.run_id, plan.job.chunk_id, plan.authorized_attempt_number
    )
    if destination.parent.parent != run_path / "jobs" / plan.job.chunk_id:
        raise ProviderRecoveryError("provider recovery decision path does not match run lineage")
    record = _decision(plan, initiator, now)
    decision_path = destination / "decision.yaml"
    if destination.exists():
        existing = load_yaml(decision_path, TranslationProviderRecoveryDecision)
        if (
            existing.run_id != record.run_id
            or existing.job_id != record.job_id
            or existing.authorized_attempt_number != record.authorized_attempt_number
            or existing.failed_attempt_number != record.failed_attempt_number
            or existing.failed_attempt_sha256 != record.failed_attempt_sha256
            or existing.recovered_execution_kind is not record.recovered_execution_kind
            or (existing.repair is None) != (record.repair is None)
            or (
                existing.repair is not None
                and record.repair is not None
                and existing.repair.repair_request_identity_sha256
                != record.repair.repair_request_identity_sha256
            )
        ):
            raise ProviderRecoveryError(
                "existing provider recovery decision contradicts the current one"
            )
        return decision_path
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{plan.authorized_attempt_number:03d}.staging.", dir=destination.parent
        )
    )
    os.chmod(staging, 0o700)
    try:
        private_write(staging / "decision.yaml", serialize_yaml(record).encode())
        os.rename(staging, destination)
    finally:
        if staging.exists():
            staging.rmdir()
    persisted = load_yaml(decision_path, TranslationProviderRecoveryDecision)
    if sha256_bytes(decision_path.read_bytes()) != sha256_bytes(serialize_yaml(persisted).encode()):
        raise ProviderRecoveryError("provider recovery decision did not round-trip")
    return decision_path


def _reenter_translation(
    run_path: Path,
    plan: ProviderRecoveryPlan,
    *,
    project_root: Path,
    timestamp: datetime,
) -> TranslationJob:
    """Return a recovered translation to the canonical queue as an ordinary pending job."""
    try:
        retry_job(
            run_path,
            plan.job_id,
            project_root=project_root,
            reason=(
                f"provider recovery ({plan.failure_class.value};"
                f" policy {plan.assessment.policy_version})"
            ),
            eligible_at=max(timestamp, plan.assessment.eligible_at),
            now=lambda: timestamp,
        )
    except QueueError as exc:
        raise ProviderRecoveryError(
            f"authorized provider recovery could not reschedule {plan.job_id}: {exc}"
        ) from exc
    recovery = recover_run(run_path, project_root=project_root, now=lambda: timestamp)
    if recovery.blockers:
        raise ProviderRecoveryError(
            "translation run recovery has blockers; provider recovery stopped before claiming"
        )
    state = _load_queue(run_path, project_root)
    job = next(item for item in state.jobs if item.job_id == plan.job_id)
    if job.status is not TranslationJobStatus.PENDING:
        raise ProviderRecoveryError(
            f"recovered job did not re-enter the queue as pending: {job.status.value}"
        )
    return job


def _reenter_repair(
    run_path: Path,
    plan: ProviderRecoveryPlan,
    *,
    project_root: Path,
    timestamp: datetime,
) -> TranslationJob:
    """Restore the QA-rejected draft a failed repair displaced, so the repair path can run again.

    A recovered repair deliberately does **not** pass through ``pending``. A pending job is a job
    the queue may hand to any worker as an ordinary translation, and this one carries a
    successful attempt that ordinary execution refuses outright — so the pending state would be
    both wrong and briefly claimable. Restoring the draft instead leaves the job in exactly the
    state an untouched QA rejection occupies, which is the state the repair mechanism requires
    and which no translation claim can reach.
    """
    if plan.restored_successful_attempt is None or plan.restored_completed_at is None:
        raise ProviderRecoveryError("an authorized repair recovery must know the draft to restore")
    # The plan still reads the draft's own completion, because failing to read it is evidence the
    # lineage is wrong; the restoration itself derives the settled timestamp from the acceptance,
    # so every way back to a settled rejection writes the same one.
    try:
        restore_repair_target(
            run_path,
            plan.job_id,
            project_root=project_root,
            successful_attempt=plan.restored_successful_attempt,
            reason=(
                f"repair recovery ({plan.failure_class.value};"
                f" policy {plan.assessment.policy_version})"
            ),
            now=lambda: timestamp,
        )
    except QueueError as exc:
        raise ProviderRecoveryError(
            f"authorized repair recovery could not restore {plan.job_id}: {exc}"
        ) from exc
    state = _load_queue(run_path, project_root)
    job = next(item for item in state.jobs if item.job_id == plan.job_id)
    if (
        job.status is not TranslationJobStatus.SUCCEEDED
        or job.acceptance_status is not TranslationAcceptanceStatus.QA_REJECTED
        or job.accepted_attempt != plan.repair_root_attempt
    ):
        raise ProviderRecoveryError(
            "recovered repair did not return the job to its standing QA rejection"
        )
    return job


def authorize_provider_recovery(
    run_id_or_path: str | Path,
    plan: ProviderRecoveryPlan,
    *,
    project_root: Path,
    initiator: ProviderRecoveryInitiator = ProviderRecoveryInitiator.AUTOMATIC,
    now: Callable[[], datetime] | None = None,
) -> ProviderRecoveryAuthorization:
    """Publish one authorization and return the job to the lifecycle its kind belongs to.

    Nothing is executed here, for either kind. A recovered translation leaves this call claimable
    exactly as any pending job is; a recovered repair leaves it in the QA-rejected state the
    repair mechanism acts on. Both keep recovery a scheduling decision rather than a second
    execution path.
    """
    if not plan.recoverable:
        raise ProviderRecoveryError(
            f"provider recovery is not authorized for {plan.job_id}: {plan.verdict.value}"
        )
    project_root = project_root.resolve()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    decision_file = _publish_decision(run_path, plan, initiator=initiator, now=timestamp)
    reenter = (
        _reenter_repair
        if plan.execution_kind is TranslationExecutionKind.REPAIR
        else _reenter_translation
    )
    job = reenter(run_path, plan, project_root=project_root, timestamp=timestamp)
    return ProviderRecoveryAuthorization(
        run_id=plan.run_id,
        job_id=plan.job_id,
        decision_file=project_relative(decision_file, project_root),
        authorized_attempt_number=plan.authorized_attempt_number,
        job=job,
        execution_kind=plan.execution_kind,
    )
