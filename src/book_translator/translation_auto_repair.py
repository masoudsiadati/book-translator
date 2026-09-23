"""Bounded automatic repair of mechanically QA-rejected translation jobs.

This module is the orchestration layer between two things that must not absorb each other: the
existing repair *mechanism* (:mod:`book_translator.translation_repair`), which sends one rejected
translation and its findings back through the provider boundary, and the pure repair *policy*
(:mod:`book_translator.translation_repair_policy`), which decides whether an unattended process
is allowed to do that at all. Nothing here reimplements a repair; one cycle is ``plan ->
authorize -> delegate -> re-assess``, and every provider interaction still happens inside the
same governed repair path an operator would drive by hand.

Three properties matter more than convenience:

* **It terminates.** Each cycle claims a new attempt slot before contacting a provider, so the
  budget the policy is re-evaluated against strictly shrinks. The loop is additionally guarded so
  that an anomalous budget cannot become an unbounded spend.
* **It never launders a rejection into an acceptance.** The only route to `accepted` is the
  ordinary acceptance sidecar that QA publishes over a real repair attempt. A repair that was
  attempted but did not satisfy QA leaves the job exactly as rejected as it was.
* **It says why.** Before each authorized repair it publishes an immutable decision record naming
  the policy version, the classified findings, and the budget the attempt was drawn against, so a
  spent provider attempt stays explainable long after the queue event ring buffer has rotated.
"""

import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from book_translator.models import (
    SCHEMA_VERSION,
    AutoRepairFindingRecord,
    AutoRepairVerdict,
    ProviderName,
    TranslationAcceptanceStatus,
    TranslationAttemptStatus,
    TranslationAutoRepairDecision,
    TranslationJob,
    TranslationRepairInitiator,
)
from book_translator.paths import ProjectPaths
from book_translator.provider_execution import TranslationProvider
from book_translator.provider_execution.artifacts import (
    load_yaml,
    private_write,
    serialize_yaml,
    sha256_bytes,
)
from book_translator.terminology.validation import (
    STRUCTURAL_QA_VERSION,
    TERMINOLOGY_QA_VERSION,
)
from book_translator.translation_provider_recovery import _repair_decisions_used
from book_translator.translation_queue.workflow import (
    _load_queue,
    _resolve_run_path,
)
from book_translator.translation_repair import (
    EffectiveRejection,
    RepairResult,
    load_effective_rejection,
    repair_translation_job,
)
from book_translator.translation_repair_policy import (
    DEFAULT_MAX_REPAIR_ATTEMPTS,
    AutoRepairAssessment,
    RepairBudget,
    RepairFindingClass,
    assess_auto_repair,
    is_auto_repair_candidate,
)

AUTO_REPAIR_ENGINE_VERSION = "1.0.0"

#: Outcome vocabulary shared with the worker so a repaired job is reported in the same terms as
#: an ordinary one. A job is counted once, whatever it took to get there.
ACCEPTED = "accepted"
QA_REJECTED = "qa_rejected"
PROVIDER_FAILED = "provider_failed"
PERMANENT_FAILURE = "permanent_failure"


class AutoRepairError(ValueError):
    """Raised when automatic repair cannot safely continue."""


@dataclass(frozen=True, slots=True)
class AutoRepairPlan:
    """A complete read-only answer for one job: what would happen, and why."""

    run_id: str
    job_id: str
    job: TranslationJob
    assessment: AutoRepairAssessment
    rejection: EffectiveRejection
    repaired_from_attempt: int
    repair_attempt_number: int

    @property
    def repairable(self) -> bool:
        return self.assessment.repairable


@dataclass(frozen=True, slots=True)
class AutoRepairOutcome:
    """What a bounded automatic repair cycle actually did to one job."""

    run_id: str
    job_id: str
    outcome: str
    verdict: AutoRepairVerdict
    acceptance_status: TranslationAcceptanceStatus | None
    repair_attempts: int
    attempt_number: int | None
    triggering_codes: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    provider_contacted: bool


def _repair_attempts_used(run_path: Path, job: TranslationJob) -> int:
    """Count the repair *decisions* this job has already spent, from the artifacts themselves.

    The count is derived rather than stored so it cannot drift from the immutable attempts, and
    so a repair performed by the explicit operator command is charged against exactly the same
    budget as an automatic one.

    What it counts is a decision, not an execution. Bounded provider recovery may re-send a
    repair the provider failed to deliver, and that replay is the *same* repair action: QA chose
    it once and its findings have not changed. Charging the repair budget for the replay would
    let a provider outage look like a second QA judgement and exhaust the allowance without QA
    ever having reconsidered anything, so attempt slots a repair recovery authorized are
    excluded. This is the single accounting rule shared with
    :func:`book_translator.translation_provider_recovery._repair_decisions_used`, which the
    recovery boundary uses to record the same number in its own provenance.
    """
    return _repair_decisions_used(run_path, job)


def plan_auto_repair(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
    max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
    installed_terminology_qa_version: str = TERMINOLOGY_QA_VERSION,
    installed_structural_qa_version: str = STRUCTURAL_QA_VERSION,
    terminology_overrides: Mapping[str, RepairFindingClass] | None = None,
) -> AutoRepairPlan:
    """Assess one job against the auto-repair policy without mutating or contacting anything.

    ``terminology_overrides`` is passed straight through to the policy; see its module docstring
    for what a caller has to know before supplying one.
    """
    if max_repair_attempts < 0:
        raise AutoRepairError("maximum repair attempts must not be negative")
    project_root = project_root.resolve()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    state = _load_queue(run_path, project_root)
    job = next((item for item in state.jobs if item.job_id == job_id), None)
    if job is None:
        raise AutoRepairError(f"translation job not found: {job_id}")
    if not is_auto_repair_candidate(job):
        raise AutoRepairError(
            f"job {job_id} is not a provider-succeeded QA-rejected job and cannot be repaired"
        )
    rejection = load_effective_rejection(job, project_root=project_root)
    budget = RepairBudget(
        max_repair_attempts=max_repair_attempts,
        repair_attempts_used=_repair_attempts_used(run_path, job),
        max_provider_attempts=job.max_attempts,
        provider_attempts_used=job.provider_attempt_count,
    )
    assessment = assess_auto_repair(
        terminology_report=rejection.terminology_report,
        structural_report=rejection.structural_report,
        alignment_report=rejection.alignment_report,
        budget=budget,
        recorded_terminology_qa_version=rejection.terminology_qa_version,
        recorded_structural_qa_version=rejection.structural_qa_version,
        installed_terminology_qa_version=installed_terminology_qa_version,
        installed_structural_qa_version=installed_structural_qa_version,
        terminology_overrides=terminology_overrides,
    )
    return AutoRepairPlan(
        run_id=job.run_id,
        job_id=job.job_id,
        job=job,
        assessment=assessment,
        rejection=rejection,
        repaired_from_attempt=rejection.evidence.attempt_number,
        repair_attempt_number=job.attempt_count + 1,
    )


def _decision(plan: AutoRepairPlan, now: datetime) -> TranslationAutoRepairDecision:
    budget = plan.assessment.budget
    return TranslationAutoRepairDecision(
        schema_version=SCHEMA_VERSION,
        auto_repair_engine_version=AUTO_REPAIR_ENGINE_VERSION,
        auto_repair_policy_version=plan.assessment.policy_version,
        run_id=plan.run_id,
        job_id=plan.job_id,
        initiator=TranslationRepairInitiator.AUTOMATIC,
        verdict=plan.assessment.verdict,
        repaired_from_attempt=plan.repaired_from_attempt,
        repair_attempt_number=plan.repair_attempt_number,
        repair_attempts_used=budget.repair_attempts_used,
        max_repair_attempts=budget.max_repair_attempts,
        provider_attempts_used=budget.provider_attempts_used,
        max_provider_attempts=budget.max_provider_attempts,
        remaining_repair_attempts=budget.remaining,
        rejection_origin=plan.rejection.origin,
        rejection_evidence_file=plan.rejection.evidence_file,
        rejection_evidence_sha256=plan.rejection.evidence_sha256,
        attempt_file=plan.rejection.attempt_file,
        attempt_sha256=plan.rejection.attempt_sha256,
        source_sha256=plan.rejection.evidence.source_sha256,
        rejected_translation_sha256=plan.rejection.evidence.translation_sha256,
        terminology_qa_version=plan.rejection.terminology_qa_version,
        structural_qa_version=plan.rejection.structural_qa_version,
        findings=[
            AutoRepairFindingRecord(
                origin="terminology" if finding.origin == "terminology" else "structural",
                code=finding.code,
                severity="error" if finding.blocking else "warning",
                classification=finding.classification,
                blocking=finding.blocking,
            )
            for finding in plan.assessment.findings
        ],
        triggering_codes=list(plan.assessment.triggering_codes),
        created_at=now,
    )


def _publish_decision(run_path: Path, plan: AutoRepairPlan, *, now: datetime) -> Path:
    """Publish the authorization for one repair attempt, or reuse an identical existing one.

    Writing before the provider call is deliberate: an interrupted repair must still be able to
    explain the attempt slot it consumed. Re-entering the same slot after an interruption reuses
    the record instead of rewriting it, so recovery is idempotent and no evidence is replaced.
    """
    destination = ProjectPaths(run_path.parents[2]).translation_auto_repair_decision(
        plan.run_id, plan.job.chunk_id, plan.repair_attempt_number
    )
    if destination.parent.parent != run_path / "jobs" / plan.job.chunk_id:
        raise AutoRepairError("auto-repair decision path does not match run lineage")
    record = _decision(plan, now)
    decision_path = destination / "decision.yaml"
    if destination.exists():
        existing = load_yaml(decision_path, TranslationAutoRepairDecision)
        if (
            existing.run_id != record.run_id
            or existing.job_id != record.job_id
            or existing.repair_attempt_number != record.repair_attempt_number
            or existing.repaired_from_attempt != record.repaired_from_attempt
            or existing.rejection_evidence_sha256 != record.rejection_evidence_sha256
        ):
            raise AutoRepairError("existing auto-repair decision contradicts the current one")
        return decision_path
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{plan.repair_attempt_number:03d}.staging.", dir=destination.parent
        )
    )
    os.chmod(staging, 0o700)
    try:
        private_write(staging / "decision.yaml", serialize_yaml(record).encode())
        os.rename(staging, destination)
    finally:
        if staging.exists():
            staging.rmdir()
    persisted = load_yaml(decision_path, TranslationAutoRepairDecision)
    if sha256_bytes(decision_path.read_bytes()) != sha256_bytes(serialize_yaml(persisted).encode()):
        raise AutoRepairError("auto-repair decision did not round-trip")
    return decision_path


def _outcome(
    plan: AutoRepairPlan,
    outcome: str,
    *,
    acceptance_status: TranslationAcceptanceStatus | None,
    repair_attempts: int,
    attempt_number: int | None,
    provider_contacted: bool,
) -> AutoRepairOutcome:
    return AutoRepairOutcome(
        run_id=plan.run_id,
        job_id=plan.job_id,
        outcome=outcome,
        verdict=plan.assessment.verdict,
        acceptance_status=acceptance_status,
        repair_attempts=repair_attempts,
        attempt_number=attempt_number,
        triggering_codes=plan.assessment.triggering_codes,
        blocking_reasons=plan.assessment.blocking_reasons,
        provider_contacted=provider_contacted,
    )


def auto_repair_job(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
    provider: ProviderName,
    model: str | None,
    worker_id: str,
    lease_seconds: int,
    timeout_seconds: int,
    max_output_tokens: int,
    reasoning_effort: str | None = None,
    store_raw_response: bool = True,
    max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
    terminology_overrides: Mapping[str, RepairFindingClass] | None = None,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None = None,
    now: Callable[[], datetime] | None = None,
) -> AutoRepairOutcome:
    """Repair one QA-rejected job automatically for as long as the policy and budget allow."""
    project_root = project_root.resolve()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    clock = now or (lambda: datetime.now(UTC))
    repairs = 0
    attempt_number: int | None = None
    while True:
        plan = plan_auto_repair(
            run_path,
            job_id,
            project_root=project_root,
            max_repair_attempts=max_repair_attempts,
            terminology_overrides=terminology_overrides,
        )
        if not plan.repairable:
            return _outcome(
                plan,
                QA_REJECTED,
                acceptance_status=TranslationAcceptanceStatus.QA_REJECTED,
                repair_attempts=repairs,
                attempt_number=attempt_number,
                provider_contacted=repairs > 0,
            )
        if repairs >= plan.assessment.budget.max_repair_attempts:
            # Unreachable while the budget accounting is sound; kept so that an anomaly can only
            # ever stop this loop, never let it spend without bound.
            raise AutoRepairError("automatic repair exceeded its own attempt budget")
        _publish_decision(run_path, plan, now=clock())
        result: RepairResult = repair_translation_job(
            run_path,
            job_id,
            project_root=project_root,
            provider=provider,
            model=model,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            store_raw_response=store_raw_response,
            initiator=TranslationRepairInitiator.AUTOMATIC,
            provider_adapter=(provider_factory(plan.job) if provider_factory is not None else None),
            now=now,
        )
        repairs += 1
        attempt_number = result.attempt_number
        if result.attempt_status is TranslationAttemptStatus.SUCCEEDED:
            if result.acceptance_status is TranslationAcceptanceStatus.ACCEPTED:
                return _outcome(
                    plan,
                    ACCEPTED,
                    acceptance_status=result.acceptance_status,
                    repair_attempts=repairs,
                    attempt_number=attempt_number,
                    provider_contacted=True,
                )
            continue
        return _outcome(
            plan,
            (
                PROVIDER_FAILED
                if result.attempt_status is TranslationAttemptStatus.FAILED
                else PERMANENT_FAILURE
            ),
            acceptance_status=TranslationAcceptanceStatus.QA_REJECTED,
            repair_attempts=repairs,
            attempt_number=attempt_number,
            provider_contacted=result.provider_contacted,
        )
