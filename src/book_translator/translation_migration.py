"""Provider-free migration of a historical run's usable work into its successor run.

A successor run answers a newer frozen snapshot, so every job starts pending. The work a
historical run already paid for is not all equally reusable, and this boundary decides — per job,
from immutable evidence — which existing governed mechanism may carry it, without adding a new way
to accept anything:

* an accepted output whose rendered request is byte-identical is **reused** (accepted-translation
  reuse, exact rendered-request tier);
* an accepted output whose request moved, and any QA-rejected output, is **re-assessed** under the
  successor's frozen QA through historical re-evaluation, which demands a governed terminology
  move and publishes the result — accepted, or rejected and therefore ready for an explicit repair;
* a job whose historical attempts produced no usable output, or that was never attempted, is left
  pending for fresh translation, and its consumed historical attempts stay in the historical run;
* anything the governed mechanisms refuse is left pending with the refusal named.

Nothing here contacts a provider or changes the historical run. Every publication is re-planned
under the successor's queue lock against a fresh read of the successor and must reproduce the
dry-run decision exactly, or nothing is written for that job.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from book_translator.models import TranslationAcceptanceStatus, TranslationJob, TranslationJobStatus
from book_translator.translation_historical_reevaluation import (
    ELIGIBLE,
    HistoricalReevaluationPlan,
    plan_historical_output_reevaluation,
)
from book_translator.translation_historical_reevaluation import _publish as _publish_carry_forward
from book_translator.translation_queue.locking import queue_lock
from book_translator.translation_queue.workflow import (
    LOCK_TIMEOUT_SECONDS,
    QueueState,
    _load_queue,
    _resolve_run_path,
)
from book_translator.translation_reuse import ReuseJobPlan, plan_accepted_translation_reuse
from book_translator.translation_reuse import _publish_one as _publish_reuse

TRANSLATION_MIGRATION_VERSION = "1.0.0"


class MigrationError(ValueError):
    """Raised when a migration decision changed between planning and publication."""


class MigrationClass(StrEnum):
    #: Class 1, identical request: accepted-translation reuse.
    REUSE_EXACT = "reuse_exact"
    #: Class 1, moved request: historical re-evaluation of an accepted output.
    REASSESS_ACCEPTED = "reassess_accepted_output"
    #: Class 2: a rejected output the successor's frozen QA accepts.
    REASSESS_REJECTED = "reassess_rejected_output"
    #: Class 3: a rejected output carried forward still rejected, ready for an operator repair.
    CARRY_FORWARD_FOR_REPAIR = "carry_forward_for_repair"
    #: No usable historical output: never attempted, or no provider result.
    FRESH_TRANSLATION = "fresh_translation"
    #: A usable output the governed mechanisms refused to carry; left pending.
    BLOCKED = "blocked_pending"
    ALREADY_MIGRATED = "already_migrated"
    DESTINATION_NOT_PENDING = "destination_not_pending"

    @property
    def publishes(self) -> bool:
        return self in {
            MigrationClass.REUSE_EXACT,
            MigrationClass.REASSESS_ACCEPTED,
            MigrationClass.REASSESS_REJECTED,
            MigrationClass.CARRY_FORWARD_FOR_REPAIR,
        }


@dataclass(frozen=True, slots=True)
class JobMigration:
    job_id: str
    chunk_id: str
    migration_class: MigrationClass
    historical_job_status: TranslationJobStatus | None
    historical_status: TranslationAcceptanceStatus | None
    projected_status: TranslationAcceptanceStatus | None = None
    source_attempt_number: int | None = None
    reasons: tuple[str, ...] = ()
    historical_attempts_consumed: int = 0
    reuse: ReuseJobPlan | None = None
    reassessment: HistoricalReevaluationPlan | None = None


@dataclass(frozen=True, slots=True)
class RunMigrationPlan:
    run_id: str
    source_run_id: str
    jobs: tuple[JobMigration, ...]

    @property
    def counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.migration_class.value for item in self.jobs).items()))

    def of(self, migration_class: MigrationClass) -> tuple[JobMigration, ...]:
        return tuple(item for item in self.jobs if item.migration_class is migration_class)

    @property
    def provider_attempts_preserved(self) -> int:
        """Historical provider outputs this plan carries instead of paying for them again."""
        return sum(item.migration_class.publishes for item in self.jobs)


@dataclass(frozen=True, slots=True)
class RunMigrationResult:
    plan: RunMigrationPlan
    published: dict[str, int]
    dry_run: bool

    @property
    def provider_calls(self) -> int:
        """There is no provider code path in this boundary; the count is structural."""
        return 0


def _fresh_reason(job: TranslationJob) -> str:
    if job.attempt_count == 0:
        return "historical_job_never_attempted"
    return f"historical_job_{job.status.value}_without_provider_output"


def _classify_reassessment(
    job: TranslationJob,
    old_job: TranslationJob,
    plan: HistoricalReevaluationPlan,
    *,
    accepted_history: bool,
    reuse: ReuseJobPlan | None,
) -> JobMigration:
    common = {
        "job_id": job.job_id,
        "chunk_id": job.chunk_id,
        "historical_job_status": old_job.status,
        "historical_status": old_job.acceptance_status,
        "source_attempt_number": old_job.accepted_attempt,
        "historical_attempts_consumed": old_job.provider_attempt_count,
        "reuse": reuse,
        "reassessment": plan,
    }
    if plan.outcome != ELIGIBLE or plan.projected_status is None:
        reasons = plan.blocking_reasons
        if reuse is not None:
            reasons = (*reuse.reasons, *reasons)
        return JobMigration(
            migration_class=MigrationClass.BLOCKED,
            reasons=tuple(dict.fromkeys(reasons)),
            **common,  # type: ignore[arg-type]
        )
    if plan.projected_status is TranslationAcceptanceStatus.QA_REJECTED:
        migration_class = MigrationClass.CARRY_FORWARD_FOR_REPAIR
    elif accepted_history:
        migration_class = MigrationClass.REASSESS_ACCEPTED
    else:
        migration_class = MigrationClass.REASSESS_REJECTED
    return JobMigration(
        migration_class=migration_class,
        projected_status=plan.projected_status,
        reasons=plan.persisting_historical_findings,
        **common,  # type: ignore[arg-type]
    )


def _plan_job(
    job: TranslationJob,
    old_job: TranslationJob | None,
    reuse: ReuseJobPlan | None,
    *,
    new_state: QueueState,
    old_state: QueueState,
    project_root: Path,
    timestamp: datetime,
) -> JobMigration:
    if job.reuse_count or job.carry_forward_count:
        return JobMigration(
            job.job_id,
            job.chunk_id,
            MigrationClass.ALREADY_MIGRATED,
            old_job.status if old_job else None,
            old_job.acceptance_status if old_job else None,
            projected_status=job.acceptance_status,
        )
    if job.status is not TranslationJobStatus.PENDING or job.attempt_count:
        return JobMigration(
            job.job_id,
            job.chunk_id,
            MigrationClass.DESTINATION_NOT_PENDING,
            old_job.status if old_job else None,
            old_job.acceptance_status if old_job else None,
            reasons=(f"destination_{job.status.value}",),
        )
    package_missing = not (
        new_state.path / "jobs" / job.chunk_id / "prompt-package/package.yaml"
    ).is_file()
    if old_job is None:
        return JobMigration(
            job.job_id,
            job.chunk_id,
            MigrationClass.FRESH_TRANSLATION,
            None,
            None,
            reasons=("historical_job_missing",),
        )
    if old_job.acceptance_status is None:
        if old_job.successful_attempt is not None:
            return JobMigration(
                job.job_id,
                job.chunk_id,
                MigrationClass.BLOCKED,
                old_job.status,
                None,
                reasons=("historical_output_unassessed",),
                historical_attempts_consumed=old_job.provider_attempt_count,
            )
        return JobMigration(
            job.job_id,
            job.chunk_id,
            MigrationClass.FRESH_TRANSLATION,
            old_job.status,
            None,
            reasons=(_fresh_reason(old_job),),
            historical_attempts_consumed=old_job.provider_attempt_count,
        )
    if package_missing:
        # Both governed mechanisms compare the historical request against the successor's
        # rendered request, so a carry cannot be proven before the successor's package exists.
        return JobMigration(
            job.job_id,
            job.chunk_id,
            MigrationClass.BLOCKED,
            old_job.status,
            old_job.acceptance_status,
            reasons=("destination_prompt_package_missing",),
            historical_attempts_consumed=old_job.provider_attempt_count,
        )
    accepted = old_job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    if accepted and reuse is not None and reuse.outcome == "reusable":
        return JobMigration(
            job.job_id,
            job.chunk_id,
            MigrationClass.REUSE_EXACT,
            old_job.status,
            old_job.acceptance_status,
            projected_status=None,
            source_attempt_number=reuse.source_attempt_number,
            historical_attempts_consumed=old_job.provider_attempt_count,
            reuse=reuse,
        )
    assert old_job.accepted_attempt is not None
    reassessment = plan_historical_output_reevaluation(
        new_state.path,
        from_run=old_state.path,
        job_id=job.job_id,
        attempt_number=old_job.accepted_attempt,
        project_root=project_root,
        now=lambda: timestamp,
        new_state=new_state,
        old_state=old_state,
    )
    return _classify_reassessment(
        job, old_job, reassessment, accepted_history=accepted, reuse=reuse if accepted else None
    )


def plan_run_migration(
    run_id_or_path: str | Path,
    *,
    from_run: str | Path,
    project_root: Path,
    now: Callable[[], datetime] | None = None,
    new_state: QueueState | None = None,
    old_state: QueueState | None = None,
) -> RunMigrationPlan:
    """Decide, without writing anything, how every successor job relates to the historical run."""
    project_root = project_root.resolve()
    timestamp = (now or (lambda: datetime.now(UTC)))()
    new = new_state or _load_queue(_resolve_run_path(run_id_or_path, project_root), project_root)
    old = old_state or _load_queue(_resolve_run_path(from_run, project_root), project_root)
    if new.run.run_id == old.run.run_id:
        raise MigrationError("a run cannot be migrated into itself")
    reuse_plan = plan_accepted_translation_reuse(
        new.path,
        from_run=old.path,
        project_root=project_root,
        now=lambda: timestamp,
        new_state=new,
        old_state=old,
    )
    reuse_by_chunk = {item.chunk_id: item for item in reuse_plan.jobs}
    old_by_chunk = {job.chunk_id: job for job in old.jobs}
    return RunMigrationPlan(
        new.run.run_id,
        old.run.run_id,
        tuple(
            _plan_job(
                job,
                old_by_chunk.get(job.chunk_id),
                reuse_by_chunk.get(job.chunk_id),
                new_state=new,
                old_state=old,
                project_root=project_root,
                timestamp=timestamp,
            )
            for job in new.jobs
        ),
    )


def _comparable(plan: HistoricalReevaluationPlan) -> object:
    evidence = plan.evidence
    return (
        plan.outcome,
        plan.projected_status,
        evidence.model_dump(exclude={"created_at"}) if evidence is not None else None,
    )


def _publish_item(
    item: JobMigration,
    *,
    run_path: Path,
    old_state: QueueState,
    new_state: QueueState,
    project_root: Path,
    timestamp: datetime,
) -> None:
    """Re-plan one job under the successor's lock and publish only the decision already planned."""
    with queue_lock(run_path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        state = _load_queue(
            run_path, project_root, snapshot_validation=new_state.snapshot_validation
        )
        job = next(entry for entry in state.jobs if entry.job_id == item.job_id)
        old_job = next(entry for entry in old_state.jobs if entry.chunk_id == item.chunk_id)
        if item.migration_class is MigrationClass.REUSE_EXACT:
            (replanned,) = plan_accepted_translation_reuse(
                run_path,
                from_run=old_state.path,
                project_root=project_root,
                job_id=job.job_id,
                now=lambda: timestamp,
                new_state=state,
                old_state=old_state,
            ).jobs
            planned = item.reuse
            if (
                planned is None
                or replanned.outcome != "reusable"
                or replanned.source_translation_sha256 != planned.source_translation_sha256
                or replanned.source_attempt_number != planned.source_attempt_number
            ):
                raise MigrationError(f"reuse decision changed after planning: {job.job_id}")
            _publish_reuse(state, old_state, replanned, project_root, timestamp)
            return
        assert old_job.accepted_attempt is not None and item.reassessment is not None
        confirmed = plan_historical_output_reevaluation(
            run_path,
            from_run=old_state.path,
            job_id=job.job_id,
            attempt_number=old_job.accepted_attempt,
            project_root=project_root,
            now=lambda: timestamp,
            new_state=state,
            old_state=old_state,
        )
        if _comparable(confirmed) != _comparable(item.reassessment):
            raise MigrationError(f"re-assessment decision changed after planning: {job.job_id}")
        _publish_carry_forward(confirmed, state, project_root, timestamp)


def migrate_run(
    run_id_or_path: str | Path,
    *,
    from_run: str | Path,
    project_root: Path,
    dry_run: bool = True,
    now: Callable[[], datetime] | None = None,
) -> RunMigrationResult:
    """Plan the migration and, unless ``dry_run``, publish every carrying decision in job order.

    An interruption leaves each already-published job complete (each publication is atomic and
    idempotent); rerunning re-plans, recognizes those jobs as migrated, and continues.
    """
    project_root = project_root.resolve()
    timestamp = (now or (lambda: datetime.now(UTC)))()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    new_state = _load_queue(run_path, project_root)
    old_state = _load_queue(_resolve_run_path(from_run, project_root), project_root)
    plan = plan_run_migration(
        run_path,
        from_run=old_state.path,
        project_root=project_root,
        now=lambda: timestamp,
        new_state=new_state,
        old_state=old_state,
    )
    published: Counter[str] = Counter()
    if not dry_run:
        for item in plan.jobs:
            if not item.migration_class.publishes:
                continue
            _publish_item(
                item,
                run_path=run_path,
                old_state=old_state,
                new_state=new_state,
                project_root=project_root,
                timestamp=timestamp,
            )
            published[item.migration_class.value] += 1
    return RunMigrationResult(plan, dict(sorted(published.items())), dry_run)
