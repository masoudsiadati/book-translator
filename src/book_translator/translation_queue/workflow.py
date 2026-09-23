"""Persistent planning, publication, claims, retries, and recovery for translation queues."""

import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.hashing import sha256_file
from book_translator.models import (
    SCHEMA_VERSION,
    AttemptReservation,
    QueueEvent,
    TranslationAcceptanceEvidence,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
    TranslationQueueReport,
    TranslationQueueValidation,
    TranslationRepairEscalation,
    TranslationRun,
    TranslationRunStatus,
)
from book_translator.paths import ProjectPaths, book_workspace_of
from book_translator.translation_queue.locking import QueueLockTimeout, queue_lock
from book_translator.translation_queue.policy import (
    QueueCounters,
    attempt_budget_exhausted,
    can_transition_translation_job,
    can_transition_translation_run,
    claim_priority_key,
    derive_run_status,
    is_claimable,
    recompute_counters,
)
from book_translator.translation_repair_policy import is_failed_repair
from book_translator.translation_snapshot import SnapshotError, validate_snapshot
from book_translator.translation_snapshot.workflow import SnapshotValidationResult

TRANSLATION_QUEUE_VERSION = "1.0.0"
#: How long a queue mutation waits for the run lock. The lock is only ever held across a parse of
#: the run's own records, the checks on them, and a handful of atomic writes. Everything a mutation
#: merely reads or computes — live snapshot lineage validation above all, which on a real book
#: re-derives its series terminology and takes tens of seconds, and acceptance QA — runs *before*
#: the lock is taken (see :func:`_locked_queue`). With several execution slots contending, a waiter
#: can legitimately queue behind every other slot's critical section, so the bound is sized for
#: eight slots with margin. It fails closed: a lock that is never released ends the mutation with
#: ``QueueLockTimeout``, and it is not a knob for making long critical sections fit.
LOCK_TIMEOUT_SECONDS = 120.0
MAX_RECENT_EVENTS = 20
RESOLUTION_FILENAME = "resolution.yaml"
#: ``last_error_category`` and event type of a worker-side failure recorded on a held claim; they
#: are deliberately outside the provider error vocabulary because no provider was involved.
CLAIM_ERROR_CATEGORY = "worker"
CLAIM_ERROR_EVENT = "worker_error"


class QueueError(ValueError):
    """Raised when queue state cannot be created or changed safely."""


@dataclass(frozen=True, slots=True)
class PlannedTranslationRun:
    project_root: Path
    output_root: Path
    run: TranslationRun
    report: TranslationQueueReport
    jobs: tuple[TranslationJob, ...]

    @property
    def output_directory(self) -> Path:
        return self.output_root / self.run.run_id


@dataclass(frozen=True, slots=True)
class RunCreationResult:
    run_id: str
    path: Path
    job_count: int


@dataclass(frozen=True, slots=True)
class QueueState:
    path: Path
    run: TranslationRun
    report: TranslationQueueReport
    jobs: tuple[TranslationJob, ...]
    validation: TranslationQueueValidation
    snapshot_valid: bool
    #: The book workspace the run's frozen lineage names (``None`` for the legacy root). Every
    #: run-side reader of book configuration resolves through it rather than a fixed file.
    book: str | None = None
    #: The snapshot validation this state was loaded against, so a mutation that gates on it does
    #: not pay for a second one.
    snapshot_validation: SnapshotValidationResult | None = None


@dataclass(frozen=True, slots=True)
class ClaimResult:
    job: TranslationJob
    claim_token: str


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    run_id: str
    expired_claims: tuple[str, ...]
    due_retries: tuple[str, ...]
    counter_repair: bool
    status_repair: bool
    blockers: tuple[str, ...]
    dry_run: bool
    prepared_executions: tuple[str, ...] = ()


def _serialize(model: Any) -> str:
    rendered = yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise QueueError("could not serialize translation queue contract")
    return rendered


def _load(path: Path, model: type[Any]) -> Any:
    try:
        return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise QueueError(f"invalid queue artifact {path}: {exc}") from exc


def _is_repair_claim(job: TranslationJob) -> bool:
    """Return whether a held claim is a repair of a standing QA rejection rather than a draft."""
    return job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED


def _settled_completion(job: TranslationJob, project_root: Path) -> datetime:
    """Return the completion timestamp a released repair claim must be settled back to.

    Claiming a repair clears ``completed_at`` because the claim displaces the settled draft the
    repair is about to replace. When no repair happens — an operator release, or a lease that
    expired with nothing sent — the job returns to ``succeeded``, and the contract makes that
    state invalid without the timestamp that settled it. Nothing in the job still carries the
    old value, so it is read back from the job's own immutable acceptance evidence, whose
    ``created_at`` is the moment the standing verdict was published: for a carry-forward or a
    re-evaluation that settles a job it is the same instant that was written to ``completed_at``.

    Every link is re-checked before the value is trusted, and a mismatch fails closed rather than
    substituting the clock: a timestamp that cannot be read from lineage is never invented.
    """
    if job.acceptance_file is None or job.acceptance_sha256 is None:
        raise QueueError(f"settled repair claim has no acceptance evidence: {job.job_id}")
    path = project_root / job.acceptance_file
    if not path.is_file():
        raise QueueError(f"acceptance evidence is missing: {job.acceptance_file}")
    if sha256_file(path) != job.acceptance_sha256:
        raise QueueError(f"acceptance evidence hash mismatch: {job.acceptance_file}")
    evidence: TranslationAcceptanceEvidence = _load(path, TranslationAcceptanceEvidence)
    if (
        evidence.job_id != job.job_id
        or evidence.status is not job.acceptance_status
        or evidence.attempt_number != job.accepted_attempt
    ):
        raise QueueError(f"acceptance evidence lineage does not match the job: {job.job_id}")
    return evidence.created_at


def _verified_carry_forward_restoration(
    job: TranslationJob, run_path: Path, project_root: Path
) -> int:
    """Prove a failed repair's standing draft is a carry-forward, and return its number.

    Restoring such a job must leave ``successful_attempt`` null, because there is no successful
    provider attempt behind the draft — the job inherited it. That is a claim about immutable
    evidence, so it is proven here rather than assumed, and every way of being wrong fails closed.

    The heavy lifting is the repair boundary's own prover, which is the single definition of what
    a carried-forward rejection *is*: it re-checks the acceptance bytes, the carry-forward record
    that produced them, the two numbers agreeing, and the historical attempt the draft came from.
    Two things it does not answer are checked here: that the translated bytes on disk still hash
    to what the acceptance says, and that no successful provider attempt exists for this job —
    because if one did, this would be a provider-backed settlement and a null would be a lie.
    """
    if job.carry_forward_count == 0:
        raise QueueError(
            "only a repair whose standing draft is a carry-forward may be restored "
            f"without a successful attempt: {job.job_id}"
        )
    if job.acceptance_origin is not TranslationAcceptanceOrigin.HISTORICAL_REEVALUATION:
        raise QueueError(
            "carry-forward restoration requires a historical re-evaluation acceptance origin: "
            f"{job.job_id}"
        )
    # Imported here on purpose: the repair boundary already imports this module, so a module-level
    # edge back to it would be circular. Only the pure prover is used, and it writes nothing.
    from book_translator.translation_repair import (
        TranslationRepairError,
        load_effective_rejection,
    )

    try:
        rejection = load_effective_rejection(
            job, project_root=project_root, allow_failed_repair=True
        )
    except (TranslationRepairError, OSError) as exc:
        raise QueueError(f"carry-forward rejection lineage does not verify: {exc}") from exc
    carry_forward_number = rejection.carry_forward_number
    if carry_forward_number is None or carry_forward_number != job.accepted_attempt:
        raise QueueError(
            f"standing acceptance does not identify a carry-forward draft: {job.job_id}"
        )
    evidence = rejection.evidence
    translation_path = project_root / evidence.translation_file
    if not translation_path.is_file():
        raise QueueError(f"carried-forward translation is missing: {evidence.translation_file}")
    if sha256_file(translation_path) != evidence.translation_sha256:
        raise QueueError(f"carried-forward translation hash mismatch: {evidence.translation_file}")
    attempts = _job_directory(run_path, job.chunk_id) / "attempts"
    for attempt_path in sorted(attempts.glob("*/attempt.yaml")):
        attempt: TranslationAttempt = _load(attempt_path, TranslationAttempt)
        if attempt.status is TranslationAttemptStatus.SUCCEEDED:
            raise QueueError(
                "a successful provider attempt exists, so this settlement is not a "
                f"carry-forward: {job.job_id}"
            )
    return carry_forward_number


def _released_completion(
    job: TranslationJob, target: TranslationJobStatus, project_root: Path
) -> datetime | None:
    """Return the ``completed_at`` a claim release or recovery should write for ``job``."""
    if target is not TranslationJobStatus.SUCCEEDED or job.completed_at is not None:
        return job.completed_at
    return _settled_completion(job, project_root)


def _relative(path: Path, project_root: Path) -> Path:
    try:
        return Path(path.resolve().relative_to(project_root.resolve()).as_posix())
    except ValueError as exc:
        raise QueueError(f"queue path must be inside the project: {path}") from exc


def _run_book_workspace(run: TranslationRun, manifest: TranslationManifest) -> str | None:
    """Derive the book workspace a run belongs to from its own frozen lineage, failing closed.

    The run's persisted ``snapshot_path`` and the frozen manifest's workspace paths were written by
    the same publication and must agree; a run whose two records name different workspaces has
    no single book configuration to execute under, so it is refused rather than guessed at.
    """
    from_run = book_workspace_of(run.snapshot_path)
    from_manifest = book_workspace_of(manifest.book.toc_file)
    if from_run != from_manifest:
        raise QueueError(
            "translation run lineage is ambiguous: run names book workspace "
            f"{from_run or 'legacy'} but its frozen manifest names {from_manifest or 'legacy'}"
        )
    return from_run


def _snapshot_digest_prefix(manifest_hash: str) -> str:
    return manifest_hash[:12]


def _next_run_id(output_root: Path, manifest_hash: str) -> str:
    prefix = f"tr-{_snapshot_digest_prefix(manifest_hash)}-"
    sequences: list[int] = []
    if output_root.is_dir():
        for candidate in output_root.iterdir():
            if candidate.is_dir() and re.fullmatch(rf"{re.escape(prefix)}\d{{3,}}", candidate.name):
                sequences.append(int(candidate.name.removeprefix(prefix)))
    return f"{prefix}{max(sequences, default=0) + 1:03d}"


def _job_id(run_id: str, chunk_id: str) -> str:
    return f"{run_id}__{chunk_id}"


def _event(
    event_type: str,
    now: datetime,
    *,
    worker_id: str | None = None,
    reason: str | None = None,
) -> QueueEvent:
    return QueueEvent(
        event_type=event_type,
        occurred_at=now,
        worker_id=worker_id,
        reason=reason,
    )


def _append_event(job: TranslationJob, event: QueueEvent) -> list[QueueEvent]:
    return [*job.recent_events, event][-MAX_RECENT_EVENTS:]


def _run_with_aggregates(
    run: TranslationRun,
    jobs: tuple[TranslationJob, ...],
    now: datetime,
    *,
    force_status: TranslationRunStatus | None = None,
) -> TranslationRun:
    counters = recompute_counters(jobs)
    status = force_status or derive_run_status(jobs, run.status)
    completed_at = (
        now
        if status
        in {
            TranslationRunStatus.COMPLETED,
            TranslationRunStatus.COMPLETED_WITH_FAILURES,
        }
        else None
    )
    return run.model_copy(
        update={
            "pending_count": counters.pending,
            "claimed_count": counters.claimed,
            "running_count": counters.running,
            "succeeded_count": counters.succeeded,
            "failed_count": counters.failed,
            "retry_pending_count": counters.retry_pending,
            "cancelled_count": counters.cancelled,
            "blocked_count": counters.blocked,
            "accepted_count": counters.accepted,
            "qa_rejected_count": counters.qa_rejected,
            "provider_succeeded_unassessed_count": counters.provider_succeeded_unassessed,
            "status": status,
            "updated_at": now,
            "completed_at": completed_at,
        }
    )


def _run_counters(run: TranslationRun) -> QueueCounters:
    return QueueCounters(
        pending=run.pending_count,
        claimed=run.claimed_count,
        running=run.running_count,
        succeeded=run.succeeded_count,
        failed=run.failed_count,
        retry_pending=run.retry_pending_count,
        cancelled=run.cancelled_count,
        blocked=run.blocked_count,
        accepted=run.accepted_count,
        qa_rejected=run.qa_rejected_count,
        provider_succeeded_unassessed=run.provider_succeeded_unassessed_count,
    )


def _build_jobs(
    *,
    run_id: str,
    snapshot_id: str,
    manifest_hash: str,
    manifest: TranslationManifest,
    max_attempts: int,
    timestamp: datetime,
) -> tuple[TranslationJob, ...]:
    jobs: list[TranslationJob] = []
    priority = 0
    for chapter_sequence, chapter in enumerate(manifest.chapters, start=1):
        for chunk in chapter.chunks:
            priority += 1
            jobs.append(
                TranslationJob(
                    schema_version=SCHEMA_VERSION,
                    job_id=_job_id(run_id, chunk.chunk_id),
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    manifest_sha256=manifest_hash,
                    chapter_id=chapter.chapter_id,
                    chapter_sequence=chapter_sequence,
                    chunk_id=chunk.chunk_id,
                    chunk_sequence=chunk.sequence,
                    chunk_source_file=chunk.source_file,
                    chunk_source_sha256=chunk.source_sha256,
                    context_file=chunk.context_file,
                    context_sha256=chunk.context_sha256,
                    source_word_count=chunk.source_word_count,
                    context_word_count=chunk.context_word_count,
                    prompt_version=manifest.prompt.version,
                    glossary_version=manifest.glossary.version,
                    style_guide_version=manifest.style_guide.version,
                    source_language=manifest.book.source_language,
                    target_language=manifest.book.target_language,
                    queue_version=TRANSLATION_QUEUE_VERSION,
                    status=TranslationJobStatus.PENDING,
                    priority=priority,
                    attempt_count=0,
                    max_attempts=max_attempts,
                    created_at=timestamp,
                    updated_at=timestamp,
                    recent_events=[_event("job_created", timestamp)],
                )
            )
    if not jobs:
        raise QueueError("snapshot contains no chunks to queue")
    job_ids = [job.job_id for job in jobs]
    chunk_ids = [job.chunk_id for job in jobs]
    if len(job_ids) != len(set(job_ids)) or len(chunk_ids) != len(set(chunk_ids)):
        raise QueueError("snapshot chunk coverage does not produce unique queue jobs")
    return tuple(jobs)


def plan_run(
    snapshot_id_or_path: str | Path,
    *,
    project_root: Path,
    label: str | None = None,
    output_root: Path | None = None,
    max_attempts: int = 3,
    retry_backoff_seconds: int = 60,
    output_contract_version: str | None = None,
    semantic_gate_policy_version: str | None = None,
    now: Callable[[], datetime] | None = None,
    book: str | None = None,
) -> PlannedTranslationRun:
    """Plan a queue over one frozen snapshot.

    A bare snapshot ID is looked up in exactly one workspace: the legacy root, or the book
    workspace named by `book`. No other workspace is searched, so an ID can never resolve
    ambiguously; an explicit path bypasses the lookup and is validated against the workspace its
    own frozen manifest records, which `book` may confirm but never override.
    """
    if max_attempts < 1:
        raise QueueError("max attempts must be at least one")
    if retry_backoff_seconds < 0:
        raise QueueError("retry backoff must not be negative")
    project_root = project_root.resolve()
    try:
        validation = validate_snapshot(snapshot_id_or_path, project_root=project_root, book=book)
    except SnapshotError as exc:
        raise QueueError(str(exc)) from exc
    if (
        not validation.report.valid
        or not validation.lineage_current
        or validation.snapshot is None
        or validation.manifest is None
    ):
        raise QueueError("translation snapshot is not current, valid, and frozen")
    snapshot = validation.snapshot
    manifest = validation.manifest
    # Run storage is project-level on purpose for now: every run-side command (listing, claiming,
    # execution, QA, reuse, ...) resolves bare run IDs under this one root, run IDs are keyed by
    # the content-addressed manifest hash, and `snapshot_path` below names the workspace the run
    # belongs to. Moving runs under `workspace/books/<book>/` is a separate migration of that
    # whole command family, not a side effect of resolving snapshots by book.
    output = (output_root or ProjectPaths(project_root).translation_runs).resolve()
    _relative(output, project_root)
    run_id = _next_run_id(output, snapshot.manifest_sha256)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    jobs = _build_jobs(
        run_id=run_id,
        snapshot_id=snapshot.snapshot_id,
        manifest_hash=snapshot.manifest_sha256,
        manifest=manifest,
        max_attempts=max_attempts,
        timestamp=timestamp,
    )
    snapshot_path = _relative(validation.path, project_root)
    run = TranslationRun(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        label=label,
        snapshot_id=snapshot.snapshot_id,
        snapshot_path=snapshot_path,
        snapshot_manifest_sha256=snapshot.manifest_sha256,
        queue_engine_version=TRANSLATION_QUEUE_VERSION,
        output_contract_version=output_contract_version,
        semantic_gate_policy_version=semantic_gate_policy_version,
        source_language=snapshot.source_language,
        target_language=snapshot.target_language,
        chapter_count=len(manifest.chapters),
        job_count=len(jobs),
        pending_count=len(jobs),
        claimed_count=0,
        running_count=0,
        succeeded_count=0,
        failed_count=0,
        retry_pending_count=0,
        cancelled_count=0,
        blocked_count=0,
        max_attempts=max_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        status=TranslationRunStatus.READY,
        created_at=timestamp,
        updated_at=timestamp,
        warnings=[],
    )
    if not can_transition_translation_run(TranslationRunStatus.CREATED, TranslationRunStatus.READY):
        raise QueueError("canonical created-to-ready transition is unavailable")
    report = TranslationQueueReport(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        snapshot_id=snapshot.snapshot_id,
        manifest_sha256=snapshot.manifest_sha256,
        queue_engine_version=TRANSLATION_QUEUE_VERSION,
        chapter_count=len(manifest.chapters),
        job_count=len(jobs),
        first_job_id=jobs[0].job_id,
        last_job_id=jobs[-1].job_id,
        pending_count=len(jobs),
        max_attempts=max_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        one_to_one_coverage=True,
        ordering_valid=True,
        snapshot_valid=True,
        warnings=[],
        created_at=timestamp,
    )
    return PlannedTranslationRun(project_root, output, run, report, jobs)


def _job_directory(run_directory: Path, chunk_id: str) -> Path:
    return run_directory / "jobs" / chunk_id


def _reservation_path(run_directory: Path, chunk_id: str, attempt_number: int) -> Path:
    """Derive the stage-owned pre-send marker for one attempt slot."""
    attempts = _job_directory(run_directory, chunk_id) / "attempts"
    return attempts / f".{attempt_number:03d}.reservation.yaml"


def _resolution_path(run_directory: Path, chunk_id: str, attempt_number: int) -> Path:
    """Derive the durable operator resolution record for one attempt slot."""
    resolutions = _job_directory(run_directory, chunk_id) / "resolutions"
    return resolutions / f"{attempt_number:03d}" / RESOLUTION_FILENAME


def _write_staging(plan: PlannedTranslationRun, staging: Path) -> None:
    for job in plan.jobs:
        atomic_write_text(
            _job_directory(staging, job.chunk_id) / "job.yaml",
            _serialize(job),
        )
    for job in plan.jobs:
        persisted = _load(_job_directory(staging, job.chunk_id) / "job.yaml", TranslationJob)
        if persisted != job:
            raise QueueError(f"staged queue job validation failed: {job.job_id}")
    atomic_write_text(staging / "queue-report.yaml", _serialize(plan.report))
    atomic_write_text(staging / "run.yaml", _serialize(plan.run))


def create_run(plan: PlannedTranslationRun) -> RunCreationResult:
    plan.output_root.mkdir(parents=True, exist_ok=True)
    allocation_lock = plan.output_root / ".allocation.lock"
    try:
        with queue_lock(allocation_lock, timeout_seconds=LOCK_TIMEOUT_SECONDS):
            expected_id = _next_run_id(plan.output_root, plan.run.snapshot_manifest_sha256)
            if expected_id != plan.run.run_id:
                raise QueueError(
                    f"proposed run ID {plan.run.run_id} lost an allocation race; replan"
                )
            staging = Path(
                tempfile.mkdtemp(prefix=f".{plan.run.run_id}.staging.", dir=plan.output_root)
            )
            try:
                _write_staging(plan, staging)
                staged = _load_queue(
                    staging,
                    plan.project_root,
                    validate_snapshot_live=False,
                    expected_run_id=plan.run.run_id,
                )
                if not staged.validation.valid:
                    raise QueueError("staged translation run validation failed")
                os.rename(staging, plan.output_directory)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
    except (QueueLockTimeout, FileExistsError, OSError) as exc:
        if isinstance(exc, QueueError):
            raise
        raise QueueError(f"translation run publication failed: {exc}") from exc
    return RunCreationResult(plan.run.run_id, plan.output_directory, len(plan.jobs))


def _resolve_run_path(value: str | Path, project_root: Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or len(candidate.parts) > 1:
        return candidate.resolve()
    return ProjectPaths(project_root.resolve()).translation_runs / candidate


def _expected_jobs(
    run: TranslationRun, manifest: TranslationManifest
) -> tuple[dict[str, Any], ...]:
    expected: list[dict[str, Any]] = []
    priority = 0
    for chapter_sequence, chapter in enumerate(manifest.chapters, start=1):
        for chunk in chapter.chunks:
            priority += 1
            expected.append(
                {
                    "job_id": _job_id(run.run_id, chunk.chunk_id),
                    "run_id": run.run_id,
                    "snapshot_id": run.snapshot_id,
                    "manifest_sha256": run.snapshot_manifest_sha256,
                    "chapter_id": chapter.chapter_id,
                    "chapter_sequence": chapter_sequence,
                    "chunk_id": chunk.chunk_id,
                    "chunk_sequence": chunk.sequence,
                    "chunk_source_file": chunk.source_file,
                    "chunk_source_sha256": chunk.source_sha256,
                    "context_file": chunk.context_file,
                    "context_sha256": chunk.context_sha256,
                    "source_word_count": chunk.source_word_count,
                    "context_word_count": chunk.context_word_count,
                    "prompt_version": manifest.prompt.version,
                    "glossary_version": manifest.glossary.version,
                    "style_guide_version": manifest.style_guide.version,
                    "source_language": manifest.book.source_language,
                    "target_language": manifest.book.target_language,
                    "queue_version": TRANSLATION_QUEUE_VERSION,
                    "priority": priority,
                }
            )
    return tuple(expected)


def _granted_repair_escalation(run_path: Path, chunk_id: str) -> int:
    """Sum the attempt slots governed escalations have granted to one job.

    Reading the records rather than trusting the job keeps the amendment auditable: a ceiling
    that exceeds the run's has to be able to point at the artifact that raised it.
    """
    root = run_path / "jobs" / chunk_id / "repair-escalation"
    if not root.is_dir():
        return 0
    granted = 0
    for index, directory in enumerate(sorted(path for path in root.iterdir() if path.is_dir()), 1):
        if directory.name != f"{index:03d}":
            raise QueueError("repair escalation numbering is not contiguous")
        record = _load(directory / "escalation.yaml", TranslationRepairEscalation)
        granted += record.granted_attempts
    return granted


def _check_attempt_ceiling(run_path: Path, run: TranslationRun, job: TranslationJob) -> None:
    """A job's attempt ceiling is the run's, unless a persisted escalation raised exactly it.

    The ceiling used to be compared against the run's for equality, which is still what almost
    every job satisfies. The exception is deliberate and narrow: an exceptional repair escalation
    is a per-job amendment, so the queue verifies the amendment instead of forbidding it. Any
    other excess — a hand-edited job, a lost record — is still a stale queue.
    """
    if job.max_attempts < run.max_attempts:
        raise QueueError(f"job attempt ceiling is below the run's: {job.job_id}")
    excess = job.max_attempts - run.max_attempts
    if excess and excess != _granted_repair_escalation(run_path, job.chunk_id):
        raise QueueError(f"job attempt ceiling has no governed escalation: {job.job_id}")


def _load_queue(
    run_path: Path,
    project_root: Path,
    *,
    validate_snapshot_live: bool = True,
    expected_run_id: str | None = None,
    snapshot_validation: SnapshotValidationResult | None = None,
) -> QueueState:
    """Parse and check one run's records.

    ``snapshot_validation`` is a validation of the run's frozen snapshot the caller already
    performed — normally just before taking the queue lock, because a live validation is the most
    expensive part of a load. It is used only if it validated the very snapshot this run names;
    otherwise the snapshot is validated here, as it is when none is given.
    """
    raw_run = yaml.safe_load((run_path / "run.yaml").read_text(encoding="utf-8"))
    legacy_acceptance_counters = not isinstance(raw_run, dict) or not {
        "accepted_count",
        "qa_rejected_count",
        "provider_succeeded_unassessed_count",
    }.issubset(raw_run)
    run = _load(run_path / "run.yaml", TranslationRun)
    report = _load(run_path / "queue-report.yaml", TranslationQueueReport)
    if run.run_id != (expected_run_id or run_path.name) or report.run_id != run.run_id:
        raise QueueError("translation run identity mismatch")
    if run.queue_engine_version != TRANSLATION_QUEUE_VERSION:
        raise QueueError("translation queue engine version is stale")
    snapshot_path = (project_root / run.snapshot_path).resolve()
    if snapshot_validation is None or snapshot_validation.path != snapshot_path:
        snapshot_validation = validate_snapshot(snapshot_path, project_root=project_root)
    snapshot_valid = snapshot_validation.report.valid and snapshot_validation.lineage_current
    manifest = snapshot_validation.manifest
    if manifest is None:
        raise QueueError("translation run snapshot manifest is unavailable")
    book = _run_book_workspace(run, manifest)
    if (
        run.snapshot_id != snapshot_validation.report.snapshot_id
        or run.snapshot_manifest_sha256
        != (snapshot_validation.snapshot.manifest_sha256 if snapshot_validation.snapshot else None)
    ):
        raise QueueError("translation run snapshot identity mismatch")
    expected = _expected_jobs(run, manifest)
    jobs_root = run_path / "jobs"
    actual_directories = (
        sorted(path for path in jobs_root.iterdir() if path.is_dir()) if jobs_root.is_dir() else []
    )
    expected_names = [item["chunk_id"] for item in expected]
    if [path.name for path in actual_directories] != sorted(expected_names):
        raise QueueError("translation run job directory coverage is incomplete or contains extras")
    jobs_by_chunk: dict[str, TranslationJob] = {}
    for directory in actual_directories:
        job = _load(directory / "job.yaml", TranslationJob)
        if job.chunk_id in jobs_by_chunk:
            raise QueueError("translation run contains duplicate chunk jobs")
        jobs_by_chunk[job.chunk_id] = job
    jobs = tuple(jobs_by_chunk[item["chunk_id"]] for item in expected)
    for job, fields in zip(jobs, expected, strict=True):
        if any(getattr(job, name) != value for name, value in fields.items()):
            raise QueueError(f"stale or inconsistent queue job definition: {job.job_id}")
        _check_attempt_ceiling(run_path, run, job)
    if report.model_dump(exclude={"created_at"}) != TranslationQueueReport(
        schema_version=SCHEMA_VERSION,
        run_id=run.run_id,
        snapshot_id=run.snapshot_id,
        manifest_sha256=run.snapshot_manifest_sha256,
        queue_engine_version=run.queue_engine_version,
        chapter_count=run.chapter_count,
        job_count=run.job_count,
        first_job_id=jobs[0].job_id,
        last_job_id=jobs[-1].job_id,
        pending_count=run.job_count,
        max_attempts=run.max_attempts,
        retry_backoff_seconds=run.retry_backoff_seconds,
        one_to_one_coverage=True,
        ordering_valid=True,
        snapshot_valid=True,
        warnings=report.warnings,
        created_at=report.created_at,
    ).model_dump(exclude={"created_at"}):
        raise QueueError("translation queue report integrity mismatch")
    counters = recompute_counters(jobs)
    if legacy_acceptance_counters:
        run = run.model_copy(
            update={
                "accepted_count": counters.accepted,
                "qa_rejected_count": counters.qa_rejected,
                "provider_succeeded_unassessed_count": counters.provider_succeeded_unassessed,
            }
        )
    derived_status = derive_run_status(jobs, run.status)
    counter_mismatch = counters != _run_counters(run) or counters.total != run.job_count
    status_mismatch = derived_status is not run.status
    validation = TranslationQueueValidation(
        schema_version=SCHEMA_VERSION,
        valid=(not counter_mismatch and not status_mismatch),
        snapshot_valid=snapshot_valid,
        counter_mismatch=counter_mismatch,
        status_mismatch=status_mismatch,
        expected_job_count=len(expected),
        actual_job_count=len(jobs),
        errors=[],
        warnings=[],
    )
    return QueueState(
        run_path, run, report, jobs, validation, snapshot_valid, book, snapshot_validation
    )


def _validate_run_snapshot(run_path: Path, project_root: Path) -> SnapshotValidationResult:
    """Validate the frozen snapshot a run names, from its run record alone.

    This is meant to run *outside* the queue lock. The lock serializes queue mutations; it does
    not protect the snapshot or the canonical workspace its lineage is rebuilt from, which change
    only through stages that never take it. So validating here rather than under the lock gives
    exactly the same guarantee, while a critical section no longer waits on a lineage rebuild.
    """
    run = _load(run_path / "run.yaml", TranslationRun)
    snapshot_path = (project_root / run.snapshot_path).resolve()
    return validate_snapshot(snapshot_path, project_root=project_root)


@contextmanager
def _locked_queue(
    run_path: Path,
    project_root: Path,
    *,
    snapshot_validation: SnapshotValidationResult | None = None,
) -> Iterator[QueueState]:
    """Hold the queue lock around a fresh load of the run, with the snapshot validated first.

    The expensive, read-only snapshot validation happens before the lock is requested (unless the
    caller already has one), so the critical section is the parse, the caller's checks, and its
    writes — never a lineage rebuild that would starve every other execution slot.
    """
    validation = snapshot_validation or _validate_run_snapshot(run_path, project_root)
    with queue_lock(run_path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        yield _load_queue(run_path, project_root, snapshot_validation=validation)


def load_run_summary(run_id_or_path: str | Path, *, project_root: Path) -> QueueState:
    return _load_queue(_resolve_run_path(run_id_or_path, project_root), project_root.resolve())


def load_settled_queue(run_id_or_path: str | Path, *, project_root: Path) -> QueueState:
    """Load the queue for a decision that must not be made on a half-published state.

    Every mutation publishes a job record and then the run aggregate under the queue lock, and a
    full read takes long enough that a reader *outside* the lock can see one without the other
    while another process is publishing. That is not a defect in the queue — it is what the
    counter check is for — but it is not a reason to refuse work either. So an apparent mismatch
    is read once more under the lock, which waits for the publication in progress; only a
    mismatch that survives that consistent read is reported. Callers must not already hold the
    lock: the lock is not reentrant.
    """
    project_root = project_root.resolve()
    path = _resolve_run_path(run_id_or_path, project_root)
    state = _load_queue(path, project_root)
    if state.validation.valid:
        return state
    with _locked_queue(
        path, project_root, snapshot_validation=state.snapshot_validation
    ) as settled:
        return settled


def resolve_run_book(run_id_or_path: str | Path, *, project_root: Path) -> str | None:
    """Return the book workspace a run belongs to, from its own lineage and nothing else.

    This reads only the run record and the frozen manifest it names, so a CLI boundary can pick
    the run's book configuration (``ProjectPaths(project_root, book).book_config``) before it
    loads the whole queue. It is the same derivation ``QueueState.book`` carries.
    """
    project_root = project_root.resolve()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    run = _load(run_path / "run.yaml", TranslationRun)
    manifest = _load(project_root / run.snapshot_path / "manifest.yaml", TranslationManifest)
    return _run_book_workspace(run, manifest)


def list_runs(*, project_root: Path) -> tuple[QueueState, ...]:
    root = ProjectPaths(project_root.resolve()).translation_runs
    if not root.is_dir():
        return ()
    states: list[QueueState] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_dir() and not path.name.startswith("."):
            states.append(_load_queue(path, project_root.resolve()))
    return tuple(states)


def list_jobs(
    run_id_or_path: str | Path,
    *,
    project_root: Path,
    status: TranslationJobStatus | None = None,
    chapter_id: str | None = None,
    limit: int | None = None,
) -> tuple[TranslationJob, ...]:
    if limit is not None and limit < 1:
        raise QueueError("job list limit must be positive")
    state = load_run_summary(run_id_or_path, project_root=project_root)
    jobs = tuple(
        job
        for job in state.jobs
        if (status is None or job.status is status)
        and (chapter_id is None or job.chapter_id == chapter_id)
    )
    return jobs if limit is None else jobs[:limit]


def _persist_job_and_run(
    state: QueueState,
    jobs: tuple[TranslationJob, ...],
    changed_job: TranslationJob,
    now: datetime,
    *,
    force_run_status: TranslationRunStatus | None = None,
) -> TranslationRun:
    atomic_write_text(
        _job_directory(state.path, changed_job.chunk_id) / "job.yaml",
        _serialize(changed_job),
        overwrite=True,
    )
    updated_run = _run_with_aggregates(state.run, jobs, now, force_status=force_run_status)
    atomic_write_text(state.path / "run.yaml", _serialize(updated_run), overwrite=True)
    return updated_run


def _validate_snapshot_for_mutation(state: QueueState, project_root: Path, now: datetime) -> None:
    validation = state.snapshot_validation or validate_snapshot(
        project_root / state.run.snapshot_path, project_root=project_root
    )
    if (
        not validation.report.valid
        or not validation.lineage_current
        or validation.snapshot is None
        or validation.snapshot.snapshot_id != state.run.snapshot_id
        or validation.snapshot.manifest_sha256 != state.run.snapshot_manifest_sha256
    ):
        if validation.report.valid and not validation.lineage_current:
            raise QueueError(
                "translation run snapshot is historical rather than current; claims are blocked"
            )
        invalid = state.run.model_copy(
            update={
                "status": TranslationRunStatus.INVALID,
                "updated_at": now,
                "warnings": [*state.run.warnings, "frozen snapshot validation failed"],
            }
        )
        atomic_write_text(state.path / "run.yaml", _serialize(invalid), overwrite=True)
        raise QueueError("translation run snapshot is invalid; claims are blocked")


def claim_job(
    run_id_or_path: str | Path,
    *,
    project_root: Path,
    worker_id: str,
    lease_seconds: int = 900,
    chapter_id: str | None = None,
    job_id: str | None = None,
    now: Callable[[], datetime] | None = None,
    token_factory: Callable[[], str] | None = None,
) -> ClaimResult:
    if not worker_id.strip():
        raise QueueError("worker ID must not be empty")
    if lease_seconds < 1:
        raise QueueError("lease seconds must be positive")
    project_root = project_root.resolve()
    path = _resolve_run_path(run_id_or_path, project_root)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    with _locked_queue(path, project_root) as state:
        _validate_snapshot_for_mutation(state, project_root, timestamp)
        if state.run.status in {
            TranslationRunStatus.CANCELLED,
            TranslationRunStatus.COMPLETED,
            TranslationRunStatus.COMPLETED_WITH_FAILURES,
            TranslationRunStatus.INVALID,
        }:
            raise QueueError(f"run status {state.run.status.value} does not permit claims")
        eligible = [
            job
            for job in state.jobs
            if is_claimable(job)
            and (chapter_id is None or job.chapter_id == chapter_id)
            and (job_id is None or job.job_id == job_id)
        ]
        if not eligible:
            raise QueueError("no eligible translation job matches the claim request")
        selected = min(eligible, key=claim_priority_key)
        if not can_transition_translation_job(selected.status, TranslationJobStatus.CLAIMED):
            raise QueueError("canonical pending-to-claimed transition is unavailable")
        token = (token_factory or (lambda: secrets.token_urlsafe(32)))()
        claimed = selected.model_copy(
            update={
                "status": TranslationJobStatus.CLAIMED,
                "attempt_count": selected.attempt_count + 1,
                "claimed_by": worker_id,
                "claim_token": token,
                "claimed_at": timestamp,
                "lease_expires_at": timestamp + timedelta(seconds=lease_seconds),
                "updated_at": timestamp,
                "recent_events": _append_event(
                    selected, _event("job_claimed", timestamp, worker_id=worker_id)
                ),
            }
        )
        jobs = tuple(claimed if job.job_id == selected.job_id else job for job in state.jobs)
        _persist_job_and_run(state, jobs, claimed, timestamp)
        return ClaimResult(claimed, token)


def release_job(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
    worker_id: str,
    reason: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> TranslationJob:
    project_root = project_root.resolve()
    path = _resolve_run_path(run_id_or_path, project_root)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    with queue_lock(path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        state = _load_queue(path, project_root)
        selected = next((job for job in state.jobs if job.job_id == job_id), None)
        if selected is None:
            raise QueueError(f"translation job not found: {job_id}")
        if selected.status is not TranslationJobStatus.CLAIMED:
            raise QueueError("only a claimed job may be released")
        if selected.claimed_by != worker_id:
            raise QueueError("worker ID does not own this claim")
        exhausted = attempt_budget_exhausted(selected)
        repair_claim = _is_repair_claim(selected)
        target = (
            TranslationJobStatus.BLOCKED
            if exhausted
            else (TranslationJobStatus.SUCCEEDED if repair_claim else TranslationJobStatus.PENDING)
        )
        released = selected.model_copy(
            update={
                "status": target,
                "completed_at": _released_completion(selected, target, project_root),
                "claimed_by": None,
                "claim_token": None,
                "claimed_at": None,
                "lease_expires_at": None,
                "updated_at": timestamp,
                "recent_events": _append_event(
                    selected,
                    _event(
                        "attempts_exhausted" if exhausted else "claim_released",
                        timestamp,
                        worker_id=worker_id,
                        reason=reason,
                    ),
                ),
                "warnings": (
                    [*selected.warnings, "maximum attempts exhausted after claim release"]
                    if exhausted
                    else selected.warnings
                ),
            }
        )
        jobs = tuple(released if job.job_id == job_id else job for job in state.jobs)
        _persist_job_and_run(state, jobs, released, timestamp)
        return released


def record_claim_error(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
    worker_id: str,
    message: str,
    now: Callable[[], datetime] | None = None,
) -> TranslationJob | None:
    """Record why a worker could not carry its own claim to a provider attempt.

    A failure between the claim and the reservation leaves no attempt directory and no provider
    classification, so without this the job would only ever show a consumed slot. The record is
    diagnostic only: the status, lease, and counters are untouched, so lease expiry, recovery,
    and release behave exactly as they would have. Returns ``None`` without writing when the
    worker no longer holds the claim, because then some other transition already explained the
    job and must not be overwritten.
    """
    if not message.strip():
        raise QueueError("a claim error record requires a message")
    project_root = project_root.resolve()
    path = _resolve_run_path(run_id_or_path, project_root)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    with _locked_queue(path, project_root) as state:
        selected = next((job for job in state.jobs if job.job_id == job_id), None)
        if selected is None:
            raise QueueError(f"translation job not found: {job_id}")
        if (
            selected.status not in {TranslationJobStatus.CLAIMED, TranslationJobStatus.RUNNING}
            or selected.claimed_by != worker_id
        ):
            return None
        recorded = selected.model_copy(
            update={
                "last_error_category": CLAIM_ERROR_CATEGORY,
                "last_error_message": message,
                "updated_at": timestamp,
                "recent_events": _append_event(
                    selected,
                    _event(CLAIM_ERROR_EVENT, timestamp, worker_id=worker_id, reason=message),
                ),
            }
        )
        jobs = tuple(recorded if job.job_id == job_id else job for job in state.jobs)
        _persist_job_and_run(state, jobs, recorded, timestamp)
        return recorded


def retry_job(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
    reason: str,
    eligible_at: datetime | None = None,
    now: Callable[[], datetime] | None = None,
) -> TranslationJob:
    """Schedule a failed job for another attempt.

    By default the job becomes eligible one backoff period from now, which is the right spacing
    for a failure that just happened. A caller that has already established the spacing another
    way — a governed provider recovery, whose policy computes eligibility from when the failure
    actually occurred — may supply ``eligible_at`` instead. It can never bring a retry forward of
    the present moment, so this stays a way of *not re-serving* an elapsed wait rather than a way
    of skipping one.
    """
    if not reason.strip():
        raise QueueError("retry reason must not be empty")
    if eligible_at is not None and eligible_at.tzinfo is None:
        raise QueueError("retry eligibility must be timezone-aware")
    project_root = project_root.resolve()
    path = _resolve_run_path(run_id_or_path, project_root)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    with _locked_queue(path, project_root) as state:
        selected = next((job for job in state.jobs if job.job_id == job_id), None)
        if selected is None:
            raise QueueError(f"translation job not found: {job_id}")
        if selected.status is not TranslationJobStatus.FAILED:
            raise QueueError("only a failed job may be scheduled for retry")
        if attempt_budget_exhausted(selected):
            raise QueueError("translation job has exhausted its maximum attempts")
        if not can_transition_translation_job(selected.status, TranslationJobStatus.RETRY_PENDING):
            raise QueueError("canonical failed-to-retry-pending transition is unavailable")
        if eligible_at is not None and eligible_at < timestamp:
            raise QueueError("retry eligibility must not precede the retry decision")
        scheduled = selected.model_copy(
            update={
                "status": TranslationJobStatus.RETRY_PENDING,
                "next_retry_at": (
                    eligible_at
                    if eligible_at is not None
                    else timestamp + timedelta(seconds=state.run.retry_backoff_seconds)
                ),
                "updated_at": timestamp,
                "recent_events": _append_event(
                    selected, _event("retry_scheduled", timestamp, reason=reason)
                ),
            }
        )
        jobs = tuple(scheduled if job.job_id == job_id else job for job in state.jobs)
        _persist_job_and_run(state, jobs, scheduled, timestamp)
        return scheduled


@dataclass(frozen=True, slots=True)
class RepairTargetRestoration:
    """What restoring one failed repair target would change, or did."""

    run_id: str
    job_id: str
    chunk_id: str
    previous_status: TranslationJobStatus
    resulting_status: TranslationJobStatus
    successful_attempt: int | None
    completed_at: datetime
    acceptance_status: TranslationAcceptanceStatus | None
    accepted_attempt: int | None
    acceptance_origin: TranslationAcceptanceOrigin | None
    carry_forward_number: int | None
    attempt_count: int
    abandoned_attempt_count: int
    provider_attempt_count: int
    max_attempts: int
    last_attempt: int | None
    applied: bool

    @property
    def settlement(self) -> str:
        return "carry-forward" if self.carry_forward_number is not None else "provider attempt"


def _planned_repair_target_restoration(
    state: QueueState, job_id: str, *, successful_attempt: int | None, project_root: Path
) -> tuple[TranslationJob, RepairTargetRestoration]:
    """Verify one restoration completely and derive its projection, writing nothing.

    Both halves of the boundary run this: the plan a dry run prints and the apply that follows are
    the same checks over the same evidence, so what an operator reads is what gets written.
    """
    selected = next((job for job in state.jobs if job.job_id == job_id), None)
    if selected is None:
        raise QueueError(f"translation job not found: {job_id}")
    if not is_failed_repair(selected):
        raise QueueError("only a job whose repair a provider failure ended may be restored")
    if attempt_budget_exhausted(selected):
        raise QueueError("translation job has exhausted its maximum attempts")
    carry_forward_number: int | None = None
    if successful_attempt is None:
        carry_forward_number = _verified_carry_forward_restoration(
            selected, state.path, project_root
        )
    else:
        # A carried-forward draft is numbered by carry-forward, so the number the caller holds is
        # never an attempt number here; writing it would assert a provider success that the failed
        # execution never produced. The two settlements are kept apart rather than guessed between.
        if selected.carry_forward_count > 0:
            raise QueueError(
                "a carry-forward settled repair has no successful provider attempt to restore"
            )
        if selected.accepted_attempt != successful_attempt:
            raise QueueError("restored draft must be the job's standing accepted attempt")
    if not can_transition_translation_job(
        selected.status, TranslationJobStatus.SUCCEEDED, repair_recovery=True
    ):
        raise QueueError("authorized repair-recovery restoration transition is unavailable")
    completed_at = _settled_completion(selected, project_root)
    return selected, RepairTargetRestoration(
        run_id=selected.run_id,
        job_id=selected.job_id,
        chunk_id=selected.chunk_id,
        previous_status=selected.status,
        resulting_status=TranslationJobStatus.SUCCEEDED,
        successful_attempt=successful_attempt,
        completed_at=completed_at,
        acceptance_status=selected.acceptance_status,
        accepted_attempt=selected.accepted_attempt,
        acceptance_origin=selected.acceptance_origin,
        carry_forward_number=carry_forward_number,
        attempt_count=selected.attempt_count,
        abandoned_attempt_count=selected.abandoned_attempt_count,
        provider_attempt_count=selected.provider_attempt_count,
        max_attempts=selected.max_attempts,
        last_attempt=selected.last_attempt,
        applied=False,
    )


def plan_repair_target_restoration(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
    successful_attempt: int | None = None,
) -> RepairTargetRestoration:
    """Describe what restoring this repair target would change, without changing anything."""
    project_root = project_root.resolve()
    path = _resolve_run_path(run_id_or_path, project_root)
    state = _load_queue(path, project_root)
    return _planned_repair_target_restoration(
        state, job_id, successful_attempt=successful_attempt, project_root=project_root
    )[1]


def restore_repair_target(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
    successful_attempt: int | None = None,
    reason: str,
    now: Callable[[], datetime] | None = None,
) -> RepairTargetRestoration:
    """Restore the QA-rejected draft that a failed repair attempt displaced.

    A repair that the provider never delivered still moved the job to ``failed`` and dropped its
    ``successful_attempt`` reference, even though the draft it was repairing is untouched and
    still the job's accepted attempt. This puts the job back into the state that untouched
    rejection occupies, so the ordinary repair path can answer the same rejection again.

    Which draft is being restored decides what ``successful_attempt`` becomes, and the two cases
    are proven apart rather than chosen by the caller. A draft this run produced is a provider
    success and its number goes back. A *carried-forward* draft was inherited: there is no
    successful provider attempt behind it, ``successful_attempt`` stays null, and passing a number
    is refused — the number would be a carry-forward number and writing it as an attempt would
    assert a success that the failed execution never produced.

    ``completed_at`` is not an argument for the same reason: it is read from the job's own
    immutable acceptance evidence, the one value that settled it, exactly as a released repair
    claim is. Nothing else is rewritten — the failed attempt stays on disk and keeps counting
    against the provider budget, ``last_attempt`` keeps naming it, and no acceptance, reservation
    or resolution record is touched.
    """
    if not reason.strip():
        raise QueueError("repair target restoration reason must not be empty")
    project_root = project_root.resolve()
    path = _resolve_run_path(run_id_or_path, project_root)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    with queue_lock(path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        state = _load_queue(path, project_root)
        selected, planned = _planned_repair_target_restoration(
            state, job_id, successful_attempt=successful_attempt, project_root=project_root
        )
        restored = selected.model_copy(
            update={
                "status": planned.resulting_status,
                "successful_attempt": planned.successful_attempt,
                "completed_at": planned.completed_at,
                "next_retry_at": None,
                "updated_at": timestamp,
                "recent_events": _append_event(
                    selected, _event("repair_target_restored", timestamp, reason=reason)
                ),
            }
        )
        jobs = tuple(restored if job.job_id == job_id else job for job in state.jobs)
        _persist_job_and_run(state, jobs, restored, timestamp)
        return replace(planned, applied=True)


def recover_run(
    run_id_or_path: str | Path,
    *,
    project_root: Path,
    now: Callable[[], datetime] | None = None,
    dry_run: bool = False,
) -> RecoveryResult:
    project_root = project_root.resolve()
    path = _resolve_run_path(run_id_or_path, project_root)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    with queue_lock(path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        try:
            state = _load_queue(path, project_root)
        except QueueError as exc:
            return RecoveryResult(path.name, (), (), False, False, (str(exc),), dry_run)
        if not state.snapshot_valid:
            return RecoveryResult(
                state.run.run_id,
                (),
                (),
                state.validation.counter_mismatch,
                state.validation.status_mismatch,
                ("translation snapshot is invalid",),
                dry_run,
            )
        expired = tuple(
            job.job_id
            for job in state.jobs
            if job.status is TranslationJobStatus.CLAIMED
            and job.lease_expires_at is not None
            and job.lease_expires_at <= timestamp
        )
        due = tuple(
            job.job_id
            for job in state.jobs
            if job.status is TranslationJobStatus.RETRY_PENDING
            and job.next_retry_at is not None
            and job.next_retry_at <= timestamp
        )
        prepared: list[str] = []
        execution_blockers: list[str] = []
        reservations: dict[str, Path] = {}
        for job in state.jobs:
            if job.status is not TranslationJobStatus.RUNNING:
                continue
            reservation_path = _reservation_path(state.path, job.chunk_id, job.attempt_count)
            if not reservation_path.is_file():
                execution_blockers.append(f"running job lacks execution reservation: {job.job_id}")
                continue
            reservation = _load(reservation_path, AttemptReservation)
            reservations[job.job_id] = reservation_path
            if reservation.status is TranslationAttemptStatus.PREPARED:
                prepared.append(job.job_id)
            elif _resolution_path(state.path, job.chunk_id, job.attempt_count).is_file():
                execution_blockers.append(
                    "explicit reservation resolution is recorded but the job state is "
                    f"unreconciled; rerun resolve-translation-reservation: {job.job_id}"
                )
            else:
                execution_blockers.append(
                    f"provider execution may have been sent and requires resolution: {job.job_id}"
                )
        # An expired repair claim settles back to the QA rejection it displaced, which is only a
        # valid job state with the timestamp that settled it. Reading it from lineage can fail,
        # and a settlement that cannot be proven is reported rather than guessed: the job keeps
        # its expired claim so an operator can see why, instead of being written back invalid.
        restorations: dict[str, datetime] = {}
        unsettleable: set[str] = set()
        for job in state.jobs:
            if job.job_id not in expired or attempt_budget_exhausted(job):
                continue
            if not _is_repair_claim(job):
                continue
            try:
                restored_at = _released_completion(
                    job, TranslationJobStatus.SUCCEEDED, project_root
                )
            except QueueError as exc:
                unsettleable.add(job.job_id)
                execution_blockers.append(
                    f"expired repair claim cannot be settled from its acceptance lineage: {exc}"
                )
                continue
            if restored_at is not None:
                restorations[job.job_id] = restored_at
        expired = tuple(job_id for job_id in expired if job_id not in unsettleable)
        changed_jobs: list[TranslationJob] = []
        for job in state.jobs:
            if job.job_id in prepared:
                changed_jobs.append(
                    job.model_copy(
                        update={
                            "status": TranslationJobStatus.CLAIMED,
                            "updated_at": timestamp,
                            "warnings": [*job.warnings, "prepared execution recovered before send"],
                            "recent_events": _append_event(
                                job, _event("execution_pre_send_recovered", timestamp)
                            ),
                        }
                    )
                )
            elif job.job_id in expired:
                exhausted = attempt_budget_exhausted(job)
                repair_claim = _is_repair_claim(job)
                changed_jobs.append(
                    job.model_copy(
                        update={
                            "status": (
                                TranslationJobStatus.BLOCKED
                                if exhausted
                                else (
                                    TranslationJobStatus.SUCCEEDED
                                    if repair_claim
                                    else TranslationJobStatus.PENDING
                                )
                            ),
                            "completed_at": restorations.get(job.job_id, job.completed_at),
                            "claimed_by": None,
                            "claim_token": None,
                            "claimed_at": None,
                            "lease_expires_at": None,
                            "updated_at": timestamp,
                            "warnings": [
                                *job.warnings,
                                (
                                    "expired claim exhausted maximum attempts"
                                    if exhausted
                                    else "expired claim recovered"
                                ),
                            ],
                            "recent_events": _append_event(
                                job,
                                _event(
                                    "attempts_exhausted" if exhausted else "lease_expired",
                                    timestamp,
                                ),
                            ),
                        }
                    )
                )
            elif job.job_id in due:
                changed_jobs.append(
                    job.model_copy(
                        update={
                            "status": TranslationJobStatus.PENDING,
                            "next_retry_at": None,
                            "updated_at": timestamp,
                            "recent_events": _append_event(
                                job, _event("retry_became_eligible", timestamp)
                            ),
                        }
                    )
                )
            elif job.status is TranslationJobStatus.PENDING and attempt_budget_exhausted(job):
                changed_jobs.append(
                    job.model_copy(
                        update={
                            "status": TranslationJobStatus.BLOCKED,
                            "updated_at": timestamp,
                            "warnings": [*job.warnings, "maximum attempts exhausted"],
                            "recent_events": _append_event(
                                job, _event("attempts_exhausted", timestamp)
                            ),
                        }
                    )
                )
            else:
                changed_jobs.append(job)
        jobs = tuple(changed_jobs)
        repaired_run = _run_with_aggregates(state.run, jobs, timestamp)
        counter_repair = _run_counters(repaired_run) != _run_counters(state.run)
        status_repair = repaired_run.status is not state.run.status
        if not dry_run:
            for before, after in zip(state.jobs, jobs, strict=True):
                if before != after:
                    atomic_write_text(
                        _job_directory(state.path, after.chunk_id) / "job.yaml",
                        _serialize(after),
                        overwrite=True,
                    )
            if counter_repair or status_repair or expired or due:
                atomic_write_text(state.path / "run.yaml", _serialize(repaired_run), overwrite=True)
            for job_id in prepared:
                reservations[job_id].unlink(missing_ok=True)
        return RecoveryResult(
            state.run.run_id,
            expired,
            due,
            counter_repair or state.validation.counter_mismatch,
            status_repair or state.validation.status_mismatch,
            tuple(execution_blockers),
            dry_run,
            tuple(prepared),
        )
