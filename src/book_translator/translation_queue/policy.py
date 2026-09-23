"""Pure Translation Queue lifecycle and aggregate-counter policy."""

from collections import Counter
from dataclasses import dataclass

from book_translator.models import (
    TranslationAcceptanceStatus,
    TranslationJob,
    TranslationJobStatus,
    TranslationRunStatus,
)


@dataclass(frozen=True, slots=True)
class QueueCounters:
    pending: int = 0
    claimed: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    retry_pending: int = 0
    cancelled: int = 0
    blocked: int = 0
    accepted: int = 0
    qa_rejected: int = 0
    provider_succeeded_unassessed: int = 0

    @property
    def total(self) -> int:
        return sum(
            (
                self.pending,
                self.claimed,
                self.running,
                self.succeeded,
                self.failed,
                self.retry_pending,
                self.cancelled,
                self.blocked,
            )
        )


_JOB_TRANSITIONS: dict[TranslationJobStatus, frozenset[TranslationJobStatus]] = {
    TranslationJobStatus.PENDING: frozenset(
        {TranslationJobStatus.CLAIMED, TranslationJobStatus.BLOCKED}
    ),
    TranslationJobStatus.CLAIMED: frozenset(
        {
            TranslationJobStatus.PENDING,
            TranslationJobStatus.RUNNING,
            TranslationJobStatus.BLOCKED,
        }
    ),
    TranslationJobStatus.RUNNING: frozenset(
        {
            TranslationJobStatus.SUCCEEDED,
            TranslationJobStatus.FAILED,
            TranslationJobStatus.BLOCKED,
            TranslationJobStatus.CLAIMED,
            # Only an explicit, audited reservation resolution returns a running job to the
            # queue: automatic recovery restores `claimed`, never `pending`.
            TranslationJobStatus.PENDING,
        }
    ),
    TranslationJobStatus.SUCCEEDED: frozenset({TranslationJobStatus.CLAIMED}),
    TranslationJobStatus.FAILED: frozenset({TranslationJobStatus.RETRY_PENDING}),
    TranslationJobStatus.RETRY_PENDING: frozenset({TranslationJobStatus.PENDING}),
    TranslationJobStatus.CANCELLED: frozenset(),
    TranslationJobStatus.BLOCKED: frozenset(),
}

_RUN_TRANSITIONS: dict[TranslationRunStatus, frozenset[TranslationRunStatus]] = {
    TranslationRunStatus.CREATED: frozenset(
        {TranslationRunStatus.READY, TranslationRunStatus.INVALID}
    ),
    TranslationRunStatus.READY: frozenset(
        {TranslationRunStatus.ACTIVE, TranslationRunStatus.CANCELLED, TranslationRunStatus.INVALID}
    ),
    TranslationRunStatus.ACTIVE: frozenset(
        {
            TranslationRunStatus.COMPLETED,
            TranslationRunStatus.COMPLETED_WITH_FAILURES,
            TranslationRunStatus.CANCELLED,
            TranslationRunStatus.INVALID,
        }
    ),
    TranslationRunStatus.COMPLETED: frozenset({TranslationRunStatus.ACTIVE}),
    TranslationRunStatus.COMPLETED_WITH_FAILURES: frozenset({TranslationRunStatus.ACTIVE}),
    TranslationRunStatus.CANCELLED: frozenset(),
    TranslationRunStatus.INVALID: frozenset(),
}


def claim_priority_key(job: TranslationJob) -> tuple[int, int, int, str]:
    """Return the canonical deterministic queue ordering key for a job."""
    return (job.priority, job.chapter_sequence, job.chunk_sequence, job.job_id)


def attempt_budget_exhausted(job: TranslationJob) -> bool:
    """Return whether a job has spent every provider attempt its run permits.

    Abandoned attempt slots — reservations an operator explicitly resolved as never having
    reached a provider — are not provider attempts and never consume this budget.
    """
    return job.provider_attempt_count >= job.max_attempts


def is_claimable(job: TranslationJob) -> bool:
    """Return whether a job satisfies the canonical claim eligibility rule."""
    return job.status is TranslationJobStatus.PENDING and not attempt_budget_exhausted(job)


def claim_ineligibility_reason(job: TranslationJob) -> str | None:
    """Return a structural explanation for a job that cannot be claimed, otherwise ``None``."""
    if is_claimable(job):
        return None
    if job.status is not TranslationJobStatus.PENDING:
        acceptance = (
            f" with acceptance {job.acceptance_status.value}"
            if job.acceptance_status is not None
            else ""
        )
        return f"job status is {job.status.value}{acceptance}"
    return f"attempt budget is exhausted ({job.provider_attempt_count}/{job.max_attempts})"


def can_transition_translation_job(
    current: TranslationJobStatus,
    target: TranslationJobStatus,
    *,
    repair_recovery: bool = False,
) -> bool:
    """Return whether a translation-job transition is permitted by the frozen policy.

    ``repair_recovery`` opens exactly one further edge, and only for an authorized recovery of a
    repair whose provider call failed. Such a failure overwrote the job's *status* but not its
    substance: the QA-rejected draft it was repairing is still the job's accepted attempt, still
    hash-verified, still the only translation the job has. Restoring that draft is not laundering
    a failure into a success — the failed attempt stays on disk, immutable, and still counted
    against the provider budget — it is undoing a status the failed attempt had no business
    changing, so the ordinary repair path can answer the same rejection again.

    It is a keyword-only opt-in for the same reason ``explicit_retry`` is on the chapter and chunk
    policies: nothing reaches this edge by default, and the caller has to say that it holds a
    published authorization.
    """
    if current == target:
        return True
    if repair_recovery:
        return current is TranslationJobStatus.FAILED and target is TranslationJobStatus.SUCCEEDED
    return target in _JOB_TRANSITIONS[current]


def can_transition_translation_run(
    current: TranslationRunStatus, target: TranslationRunStatus
) -> bool:
    if current == target:
        return True
    return target in _RUN_TRANSITIONS[current]


def recompute_counters(jobs: tuple[TranslationJob, ...]) -> QueueCounters:
    counts = Counter(job.status for job in jobs)
    return QueueCounters(
        pending=counts[TranslationJobStatus.PENDING],
        claimed=counts[TranslationJobStatus.CLAIMED],
        running=counts[TranslationJobStatus.RUNNING],
        succeeded=counts[TranslationJobStatus.SUCCEEDED],
        failed=counts[TranslationJobStatus.FAILED],
        retry_pending=counts[TranslationJobStatus.RETRY_PENDING],
        cancelled=counts[TranslationJobStatus.CANCELLED],
        blocked=counts[TranslationJobStatus.BLOCKED],
        accepted=sum(job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED for job in jobs),
        qa_rejected=sum(
            job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED for job in jobs
        ),
        provider_succeeded_unassessed=sum(
            job.status is TranslationJobStatus.SUCCEEDED
            and job.acceptance_status is None
            and job.reuse_count == 0
            for job in jobs
        ),
    )


def derive_run_status(
    jobs: tuple[TranslationJob, ...], current: TranslationRunStatus
) -> TranslationRunStatus:
    if current in {TranslationRunStatus.CANCELLED, TranslationRunStatus.INVALID}:
        return current
    counters = recompute_counters(jobs)
    if counters.succeeded == counters.total:
        if counters.accepted == counters.total:
            return TranslationRunStatus.COMPLETED
        if counters.qa_rejected and not counters.provider_succeeded_unassessed:
            return TranslationRunStatus.COMPLETED_WITH_FAILURES
        return TranslationRunStatus.ACTIVE
    permanent_failures = (
        sum(
            job.status is TranslationJobStatus.FAILED and attempt_budget_exhausted(job)
            for job in jobs
        )
        + counters.blocked
    )
    executable = sum(
        job.status
        in {
            TranslationJobStatus.PENDING,
            TranslationJobStatus.CLAIMED,
            TranslationJobStatus.RUNNING,
            TranslationJobStatus.RETRY_PENDING,
        }
        and not (job.status is TranslationJobStatus.PENDING and attempt_budget_exhausted(job))
        for job in jobs
    )
    if permanent_failures and executable == 0:
        return TranslationRunStatus.COMPLETED_WITH_FAILURES
    if all(job.status is TranslationJobStatus.PENDING and job.attempt_count == 0 for job in jobs):
        return TranslationRunStatus.READY
    return TranslationRunStatus.ACTIVE
