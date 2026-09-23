"""Explicit, audited operator resolution of incomplete translation execution reservations.

Provider execution publishes ``attempts/.NNN.reservation.yaml`` before it contacts a provider
because the queue lock is intentionally not held during a network call.  While that marker says
``prepared`` the send provably never happened and ordinary recovery restores the claim.  Once it
says ``requesting``, delivery is unknowable from the workspace alone, so automatic recovery
refuses forever.

Only a person can supply the missing external fact.  This engine records that assertion as
immutable lineage beside the untouched reservation and applies exactly the queue accounting the
assertion justifies — never a generic force reset, and never a fabricated provider attempt.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from book_translator.models import (
    SCHEMA_VERSION,
    AttemptReservation,
    ExecutionReservationResolution,
    ExecutionReservationResolutionKind,
    ExecutionResolutionProvenance,
    PromptPackage,
    ProviderErrorCategory,
    ProviderName,
    TranslationAcceptanceStatus,
    TranslationAttemptStatus,
    TranslationJob,
    TranslationJobStatus,
)
from book_translator.provider_execution.artifacts import (
    private_write,
    project_relative,
    serialize_yaml,
    sha256_bytes,
)
from book_translator.provider_execution.errors import ExecutorError
from book_translator.translation_queue.locking import queue_lock
from book_translator.translation_queue.policy import can_transition_translation_job
from book_translator.translation_queue.workflow import (
    LOCK_TIMEOUT_SECONDS,
    RESOLUTION_FILENAME,
    QueueState,
    _append_event,
    _event,
    _job_directory,
    _load_queue,
    _persist_job_and_run,
    _reservation_path,
    _resolution_path,
    _resolve_run_path,
)
from book_translator.translation_repair import (
    TranslationRepairError,
    load_effective_rejection,
)
from book_translator.translation_repair_policy import is_active_repair

EXECUTION_RESOLUTION_ENGINE_VERSION = "1.1.0"

RESERVATION_FILENAME = re.compile(r"^\.(\d{3})\.reservation\.yaml$")

RESOLVE = "resolve"
RECONCILE = "reconcile"
SKIP = "skip"


class ExecutionResolutionError(ValueError):
    """Raised when an incomplete execution reservation cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class ResolutionOutcome:
    """The queue consequences one asserted provider-contact fact justifies."""

    job_status: TranslationJobStatus
    provider_attempt_consumed: bool
    replay_permitted: bool
    summary: str


_OUTCOMES: dict[ExecutionReservationResolutionKind, ResolutionOutcome] = {
    ExecutionReservationResolutionKind.PROVIDER_NOT_CONTACTED: ResolutionOutcome(
        TranslationJobStatus.PENDING,
        provider_attempt_consumed=False,
        replay_permitted=True,
        summary=(
            "no inference started, so the abandoned slot is not a provider attempt and the job "
            "returns to the queue"
        ),
    ),
    ExecutionReservationResolutionKind.PROVIDER_CONTACT_UNKNOWN: ResolutionOutcome(
        TranslationJobStatus.BLOCKED,
        provider_attempt_consumed=True,
        replay_permitted=False,
        summary=(
            "delivery stays unknown, so the attempt is charged and the job is blocked until a "
            "separate deliberate decision moves it"
        ),
    ),
    ExecutionReservationResolutionKind.PROVIDER_CONTACTED_NO_RESULT: ResolutionOutcome(
        TranslationJobStatus.FAILED,
        provider_attempt_consumed=True,
        replay_permitted=False,
        summary=(
            "inference may have run and cost money, so the attempt is charged and only an "
            "explicit retry-translation-job decision can schedule another one"
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class ReservationResolutionPlan:
    """A validated, fully derived resolution that has not been applied."""

    run_path: Path
    run_id: str
    job_id: str
    chunk_id: str
    attempt_number: int
    resolution_id: str
    resolution: ExecutionReservationResolutionKind
    reservation_file: Path
    reservation_sha256: str
    provider: ProviderName
    previous_job_status: TranslationJobStatus
    previous_attempt_count: int
    previous_abandoned_attempt_count: int
    previous_provider_attempt_count: int
    resulting_job_status: TranslationJobStatus
    resulting_attempt_count: int
    resulting_abandoned_attempt_count: int
    resulting_provider_attempt_count: int
    provider_attempt_consumed: bool
    replay_permitted: bool
    max_attempts: int
    record: ExecutionReservationResolution
    resolution_file: Path
    resolution_record_file: Path
    action: str
    warnings: tuple[str, ...]

    @property
    def semantics(self) -> str:
        return _OUTCOMES[self.resolution].summary


@dataclass(frozen=True, slots=True)
class ReservationResolutionResult:
    """What an applied — or planned — resolution changed."""

    run_id: str
    job_id: str
    attempt_number: int
    resolution_id: str
    resolution: ExecutionReservationResolutionKind
    previous_job_status: TranslationJobStatus
    resulting_job_status: TranslationJobStatus
    previous_provider_attempt_count: int
    resulting_provider_attempt_count: int
    replay_permitted: bool
    resolution_file: Path
    reservation_file: Path
    reservation_sha256: str
    lineage_published: bool
    job_reconciled: bool
    dry_run: bool


@dataclass(frozen=True, slots=True)
class InspectedReservation:
    """One execution reservation and its resolution state, without any claim material."""

    job_id: str
    chunk_id: str
    attempt_number: int
    job_status: TranslationJobStatus
    reservation_status: TranslationAttemptStatus
    provider: ProviderName
    reservation_file: Path
    reservation_sha256: str
    automatically_recoverable: bool
    resolved: bool
    resolution: ExecutionReservationResolutionKind | None
    resolution_id: str | None
    resolution_file: Path | None
    replay_permitted: bool
    integrity: str


def resolution_outcome(resolution: ExecutionReservationResolutionKind) -> ResolutionOutcome:
    """Return the pure queue consequences of one asserted provider-contact fact."""
    return _OUTCOMES[resolution]


def _load_model[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ExecutionResolutionError(f"invalid execution artifact {path.name}: {exc}") from exc


def _identity(payload: dict[str, Any]) -> str:
    canonical = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return f"xrr-{hashlib.sha256(canonical).hexdigest()[:24]}"


def _resolution_identity(
    *,
    run_id: str,
    job_id: str,
    attempt_id: str,
    attempt_number: int,
    reservation_sha256: str,
    request_sha256: str,
    resolution: ExecutionReservationResolutionKind,
    resolver_engine_version: str = EXECUTION_RESOLUTION_ENGINE_VERSION,
) -> str:
    """Derive the content address of one resolution.

    The engine version is part of the address and therefore a parameter rather than a constant
    read at call time. A published record is immutable and must stay verifiable by the engine
    that *wrote* it: re-deriving its identity under whichever version happens to be installed
    would make every historical resolution fail to verify the first time this engine is bumped,
    which would break exactly the idempotent rerun the lineage exists to support.
    """
    return _identity(
        {
            "resolver_engine_version": resolver_engine_version,
            "run_id": run_id,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "attempt_number": attempt_number,
            "reservation_sha256": reservation_sha256,
            "request_sha256": request_sha256,
            "resolution": resolution.value,
        }
    )


def _selected_job(state: QueueState, job_id: str) -> TranslationJob:
    job = next((item for item in state.jobs if item.job_id == job_id), None)
    if job is None:
        raise ExecutionResolutionError(f"translation job not found in this run: {job_id}")
    return job


def _replace_job(
    jobs: tuple[TranslationJob, ...], changed: TranslationJob
) -> tuple[TranslationJob, ...]:
    return tuple(changed if item.job_id == changed.job_id else item for item in jobs)


def _verified_reservation(
    state: QueueState, job: TranslationJob, attempt_number: int
) -> tuple[AttemptReservation, bytes, Path]:
    path = _reservation_path(state.path, job.chunk_id, attempt_number)
    if not path.is_file():
        raise ExecutionResolutionError(
            f"attempt {attempt_number:03d} has no execution reservation to resolve"
        )
    content = path.read_bytes()
    reservation = _load_model(path, AttemptReservation)
    if serialize_yaml(reservation).encode("utf-8") != content:
        raise ExecutionResolutionError("execution reservation bytes are not canonical")
    if (
        reservation.run_id != job.run_id
        or reservation.job_id != job.job_id
        or reservation.attempt_number != attempt_number
        or reservation.attempt_id != f"{job.job_id}__attempt-{attempt_number:03d}"
    ):
        raise ExecutionResolutionError(
            "execution reservation does not belong to the selected run, job, and attempt"
        )
    return reservation, content, path


def _refuse_superseding_evidence(
    state: QueueState, job: TranslationJob, attempt_number: int, project_root: Path
) -> None:
    """Refuse a reservation whose attempt slot some other evidence has already answered.

    What counts as "already answered" depends on what the interrupted execution *was*, and the
    two kinds are told apart structurally by :func:`is_active_repair` rather than by reading an
    attempt that may never have been written.

    For an ordinary translation the rule is unchanged and deliberately blunt: any acceptance,
    re-evaluation or reuse at all means some draft already settled this job, so the unknown
    attempt is moot and resolving it would rewrite a settled outcome.

    For a repair the same rule is wrong, and was the reason a genuinely stranded repair could not
    be resolved at all. A repair exists *because* a QA rejection stands; that rejection describes
    the draft being repaired, not the attempt in flight, so it cannot supersede it. Only evidence
    about this attempt slot, or a settlement reached some other way, can.
    """
    if is_active_repair(job):
        _refuse_superseded_repair(state, job, attempt_number, project_root)
        return
    if (
        job.acceptance_status is not None
        or job.accepted_attempt is not None
        or job.successful_attempt is not None
        or job.reuse_count
        or job.reevaluation_count
    ):
        raise ExecutionResolutionError(
            "an accepted, re-evaluated, or reused translation supersedes this attempt"
        )
    directory = _job_directory(state.path, job.chunk_id)
    for name in ("acceptance", "reevaluations", "reuse"):
        if (directory / name).exists():
            raise ExecutionResolutionError(
                f"{name} evidence exists for this job and supersedes this attempt"
            )


def _refuse_superseded_repair(
    state: QueueState, job: TranslationJob, attempt_number: int, project_root: Path
) -> None:
    """Refuse a repair reservation that something other than its own rejection has settled.

    Everything here is keyed on *this* attempt slot, because the standing rejection is the repair
    target and is expected to be present. The last check is the fail-closed one: the rejection
    lineage is hash-verified end to end, so a reservation is only ever exempted from the blunt
    rule once the repair it belongs to has actually been proven.
    """
    if job.acceptance_status is not TranslationAcceptanceStatus.QA_REJECTED:
        raise ExecutionResolutionError("an accepted translation supersedes this repair attempt")
    # `accepted_attempt` indexes *attempts* for a draft this run produced but *carry-forwards* for
    # one it inherited, and the two numbering spaces collide at 1: a carried-forward rejection's
    # first repair attempt shares its number, which is why `is_failed_repair` identifies that case
    # by its carry-forward instead. Comparing the two here would refuse precisely the stranded
    # repair this guard exists to let through, so the standing draft is only read as an attempt
    # number when there is no carry-forward. What a carried-forward repair *is* superseded by is
    # published evidence for this slot, which the two directory checks below still cover.
    standing_attempt = job.accepted_attempt if job.carry_forward_count == 0 else None
    if standing_attempt == attempt_number or job.successful_attempt == attempt_number:
        raise ExecutionResolutionError(
            "this attempt already defines the job's standing translation and supersedes itself"
        )
    if job.reuse_count:
        raise ExecutionResolutionError("a reused translation supersedes this attempt")
    directory = _job_directory(state.path, job.chunk_id)
    if (directory / "acceptance" / f"{attempt_number:03d}").exists():
        raise ExecutionResolutionError(
            "acceptance evidence exists for this attempt and supersedes it"
        )
    if (directory / "reuse").exists():
        raise ExecutionResolutionError(
            "reuse evidence exists for this job and supersedes this attempt"
        )
    try:
        load_effective_rejection(job, project_root=project_root, allow_active_repair=True)
    except (TranslationRepairError, OSError) as exc:
        raise ExecutionResolutionError(
            f"the repair lineage this reservation answers does not verify: {exc}"
        ) from exc


def _refuse_conflicting_attempts(
    state: QueueState, job: TranslationJob, attempt_number: int
) -> None:
    attempts = _job_directory(state.path, job.chunk_id) / "attempts"
    if not attempts.is_dir():
        return
    for entry in sorted(attempts.iterdir()):
        if entry.is_dir() and entry.name.isdigit():
            number = int(entry.name)
            if number == attempt_number:
                raise ExecutionResolutionError(
                    "the attempt already published provider evidence and is immutable history"
                )
            if number > attempt_number:
                raise ExecutionResolutionError(
                    f"a later attempt {number:03d} already exists for this job"
                )
            continue
        match = RESERVATION_FILENAME.match(entry.name)
        if match is not None and int(match.group(1)) > attempt_number:
            raise ExecutionResolutionError(
                f"a later execution reservation {match.group(1)} already exists for this job"
            )


def _verified_request_identity(
    state: QueueState, job: TranslationJob, reservation: AttemptReservation
) -> PromptPackage:
    path = _job_directory(state.path, job.chunk_id) / "prompt-package" / "package.yaml"
    if not path.is_file():
        raise ExecutionResolutionError(
            "the prompt package is missing, so the reserved request identity cannot be verified"
        )
    package = _load_model(path, PromptPackage)
    if (
        package.package_id != reservation.prompt_package_id
        or package.job_id != job.job_id
        or package.chunk_id != job.chunk_id
        or package.snapshot_id != job.snapshot_id
        or package.manifest_sha256 != job.manifest_sha256
    ):
        raise ExecutionResolutionError(
            "the reserved request identity does not match this job's prompt package"
        )
    return package


def _plan_from_record(
    state: QueueState,
    job: TranslationJob,
    record: ExecutionReservationResolution,
    destination: Path,
    *,
    project_root: Path,
    warnings: tuple[str, ...],
) -> ReservationResolutionPlan:
    """Re-derive a plan from already published lineage so reruns never duplicate it."""
    _, content, path = _verified_reservation(state, job, record.attempt_number)
    reservation_hash = sha256_bytes(content)
    if reservation_hash != record.reservation_sha256:
        raise ExecutionResolutionError(
            "the recorded resolution no longer matches the reservation it resolved"
        )
    identity = _resolution_identity(
        run_id=record.run_id,
        job_id=record.job_id,
        attempt_id=record.attempt_id,
        attempt_number=record.attempt_number,
        reservation_sha256=record.reservation_sha256,
        request_sha256=record.request_sha256,
        resolution=record.resolution,
        resolver_engine_version=record.resolver_engine_version,
    )
    if identity != record.resolution_id:
        raise ExecutionResolutionError("the recorded resolution identity does not verify")
    reconciled = (
        job.status is record.resulting_job_status
        and job.attempt_count == record.resulting_attempt_count
        and job.abandoned_attempt_count == record.resulting_abandoned_attempt_count
    )
    unapplied = (
        job.status is record.previous_job_status
        and job.attempt_count == record.previous_attempt_count
        and job.abandoned_attempt_count == record.previous_abandoned_attempt_count
    )
    if not reconciled and not unapplied:
        raise ExecutionResolutionError(
            "the job state matches neither side of the recorded resolution; diagnose it manually"
        )
    return ReservationResolutionPlan(
        state.path,
        record.run_id,
        record.job_id,
        record.chunk_id,
        record.attempt_number,
        record.resolution_id,
        record.resolution,
        project_relative(path, project_root),
        reservation_hash,
        record.provider,
        record.previous_job_status,
        record.previous_attempt_count,
        record.previous_abandoned_attempt_count,
        record.previous_attempt_count - record.previous_abandoned_attempt_count,
        record.resulting_job_status,
        record.resulting_attempt_count,
        record.resulting_abandoned_attempt_count,
        record.resulting_attempt_count - record.resulting_abandoned_attempt_count,
        record.provider_attempt_consumed,
        record.replay_permitted,
        job.max_attempts,
        record,
        destination,
        project_relative(destination, project_root),
        SKIP if reconciled else RECONCILE,
        warnings,
    )


def _plan(
    state: QueueState,
    job_id: str,
    *,
    attempt_number: int,
    resolution: ExecutionReservationResolutionKind,
    reason: str,
    project_root: Path,
    now: datetime,
) -> ReservationResolutionPlan:
    if attempt_number < 1 or attempt_number > 999:
        raise ExecutionResolutionError("attempt number must be between 1 and 999")
    if not reason.strip():
        raise ExecutionResolutionError("an operator reason must be supplied")
    if not state.validation.valid:
        raise ExecutionResolutionError(
            "translation run structure is invalid; recover the run before resolving an attempt"
        )
    warnings: tuple[str, ...] = (
        () if state.snapshot_valid else ("frozen translation snapshot no longer validates",)
    )
    job = _selected_job(state, job_id)
    if job.run_id != state.run.run_id:
        raise ExecutionResolutionError("translation job does not belong to this run")
    destination_directory = _resolution_path(state.path, job.chunk_id, attempt_number).parent
    destination = destination_directory / RESOLUTION_FILENAME
    if destination.is_file():
        recorded = _load_model(destination, ExecutionReservationResolution)
        if recorded.resolution is not resolution:
            raise ExecutionResolutionError(
                f"attempt {attempt_number:03d} is already resolved as "
                f"{recorded.resolution.value}; a conflicting resolution is refused"
            )
        if recorded.job_id != job.job_id or recorded.attempt_number != attempt_number:
            raise ExecutionResolutionError("the recorded resolution lineage does not match")
        return _plan_from_record(
            state, job, recorded, destination, project_root=project_root, warnings=warnings
        )

    _refuse_superseding_evidence(state, job, attempt_number, project_root)
    if job.status is not TranslationJobStatus.RUNNING:
        raise ExecutionResolutionError(
            "only a running job holds an incomplete execution reservation "
            f"(job status is {job.status.value})"
        )
    if job.attempt_count != attempt_number:
        raise ExecutionResolutionError(
            f"attempt number does not match the job's current attempt slot ({job.attempt_count})"
        )
    _refuse_conflicting_attempts(state, job, attempt_number)
    reservation, content, path = _verified_reservation(state, job, attempt_number)
    if reservation.status is TranslationAttemptStatus.PREPARED:
        raise ExecutionResolutionError(
            "a prepared reservation provably never reached a provider; "
            "recover-translation-run restores it automatically"
        )
    if reservation.status is not TranslationAttemptStatus.REQUESTING:
        raise ExecutionResolutionError(
            f"reservation status {reservation.status.value} is not an incomplete execution"
        )
    _verified_request_identity(state, job, reservation)
    outcome = resolution_outcome(resolution)
    if not can_transition_translation_job(job.status, outcome.job_status):
        raise ExecutionResolutionError("canonical resolution transition is unavailable")
    reservation_hash = sha256_bytes(content)
    abandoned = job.abandoned_attempt_count + (0 if outcome.provider_attempt_consumed else 1)
    record = ExecutionReservationResolution(
        schema_version=SCHEMA_VERSION,
        resolution_id=_resolution_identity(
            run_id=job.run_id,
            job_id=job.job_id,
            attempt_id=reservation.attempt_id,
            attempt_number=attempt_number,
            reservation_sha256=reservation_hash,
            request_sha256=reservation.request_sha256,
            resolution=resolution,
        ),
        resolver_engine_version=EXECUTION_RESOLUTION_ENGINE_VERSION,
        provenance=ExecutionResolutionProvenance.OPERATOR,
        run_id=job.run_id,
        job_id=job.job_id,
        chunk_id=job.chunk_id,
        attempt_id=reservation.attempt_id,
        attempt_number=attempt_number,
        reservation_file=project_relative(path, project_root),
        reservation_sha256=reservation_hash,
        reservation_status=reservation.status,
        provider=reservation.provider,
        prompt_package_id=reservation.prompt_package_id,
        request_sha256=reservation.request_sha256,
        resolution=resolution,
        reason=reason.strip(),
        previous_job_status=job.status,
        previous_attempt_count=job.attempt_count,
        previous_abandoned_attempt_count=job.abandoned_attempt_count,
        resulting_job_status=outcome.job_status,
        resulting_attempt_count=job.attempt_count,
        resulting_abandoned_attempt_count=abandoned,
        provider_attempt_consumed=outcome.provider_attempt_consumed,
        replay_permitted=outcome.replay_permitted,
        created_at=now,
    )
    return ReservationResolutionPlan(
        state.path,
        job.run_id,
        job.job_id,
        job.chunk_id,
        attempt_number,
        record.resolution_id,
        resolution,
        record.reservation_file,
        reservation_hash,
        reservation.provider,
        job.status,
        job.attempt_count,
        job.abandoned_attempt_count,
        job.provider_attempt_count,
        outcome.job_status,
        job.attempt_count,
        abandoned,
        job.attempt_count - abandoned,
        outcome.provider_attempt_consumed,
        outcome.replay_permitted,
        job.max_attempts,
        record,
        destination,
        project_relative(destination, project_root),
        RESOLVE,
        warnings,
    )


def plan_reservation_resolution(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    attempt_number: int,
    resolution: ExecutionReservationResolutionKind,
    reason: str,
    project_root: Path,
    now: Callable[[], datetime] | None = None,
) -> ReservationResolutionPlan:
    """Validate and derive a resolution without writing anything."""
    project_root = project_root.resolve()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    return _plan(
        _load_queue(run_path, project_root),
        job_id,
        attempt_number=attempt_number,
        resolution=resolution,
        reason=reason,
        project_root=project_root,
        now=timestamp,
    )


def _publish(record: ExecutionReservationResolution, destination: Path) -> None:
    root = destination.parent.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".resolution.staging.", dir=root))
    os.chmod(staging, 0o700)
    try:
        private_write(staging / RESOLUTION_FILENAME, serialize_yaml(record).encode("utf-8"))
        os.rename(staging, destination.parent)
    except (OSError, ExecutorError) as exc:
        raise ExecutionResolutionError(
            "resolution lineage publication failed; the reservation is unchanged"
        ) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _resolved_job(
    job: TranslationJob, plan: ReservationResolutionPlan, timestamp: datetime
) -> TranslationJob:
    update: dict[str, Any] = {
        "status": plan.resulting_job_status,
        "claimed_by": None,
        "claim_token": None,
        "claimed_at": None,
        "lease_expires_at": None,
        "abandoned_attempt_count": plan.resulting_abandoned_attempt_count,
        "updated_at": timestamp,
        "warnings": [
            *job.warnings,
            f"attempt {plan.attempt_number:03d} reservation resolved as {plan.resolution.value}",
        ],
        "recent_events": _append_event(
            job,
            _event("execution_reservation_resolved", timestamp, reason=plan.resolution.value),
        ),
    }
    if plan.resulting_job_status is not TranslationJobStatus.PENDING:
        update["last_error_category"] = ProviderErrorCategory.UNKNOWN.value
        update["last_error_message"] = (
            "operator resolved an incomplete execution reservation as "
            f"{plan.resolution.value}; no provider result exists"
        )
    return job.model_copy(update=update)


def execute_reservation_resolution(
    plan: ReservationResolutionPlan,
    *,
    project_root: Path,
    now: Callable[[], datetime] | None = None,
) -> ReservationResolutionResult:
    """Publish immutable resolution lineage and apply exactly the accounting it justifies."""
    project_root = project_root.resolve()
    timestamp = (now or (lambda: datetime.now(UTC)))()
    if plan.action == SKIP:
        return _result(plan, published=False, reconciled=False, dry_run=False)
    with queue_lock(plan.run_path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        state = _load_queue(plan.run_path, project_root)
        current = _plan(
            state,
            plan.job_id,
            attempt_number=plan.attempt_number,
            resolution=plan.resolution,
            reason=plan.record.reason,
            project_root=project_root,
            now=timestamp,
        )
        if (
            current.resolution_id != plan.resolution_id
            or current.action != plan.action
            or current.reservation_sha256 != plan.reservation_sha256
            or current.previous_job_status is not plan.previous_job_status
            or current.previous_attempt_count != plan.previous_attempt_count
            or current.previous_abandoned_attempt_count != plan.previous_abandoned_attempt_count
        ):
            raise ExecutionResolutionError(
                "the queue changed after planning; replan before applying this resolution"
            )
        published = False
        if current.action == RESOLVE:
            _publish(current.record, current.resolution_file)
            published = True
        job = _selected_job(state, current.job_id)
        if not can_transition_translation_job(job.status, current.resulting_job_status):
            raise ExecutionResolutionError("canonical resolution transition is unavailable")
        updated = _resolved_job(job, current, timestamp)
        _persist_job_and_run(state, _replace_job(state.jobs, updated), updated, timestamp)
        return _result(current, published=published, reconciled=True, dry_run=False)


def _result(
    plan: ReservationResolutionPlan, *, published: bool, reconciled: bool, dry_run: bool
) -> ReservationResolutionResult:
    return ReservationResolutionResult(
        plan.run_id,
        plan.job_id,
        plan.attempt_number,
        plan.resolution_id,
        plan.resolution,
        plan.previous_job_status,
        plan.resulting_job_status,
        plan.previous_provider_attempt_count,
        plan.resulting_provider_attempt_count,
        plan.replay_permitted,
        plan.resolution_record_file,
        plan.reservation_file,
        plan.reservation_sha256,
        published,
        reconciled,
        dry_run,
    )


def planned_result(plan: ReservationResolutionPlan) -> ReservationResolutionResult:
    """Describe what applying this plan would change, without changing anything."""
    return _result(
        plan, published=plan.action == RESOLVE, reconciled=plan.action != SKIP, dry_run=True
    )


def inspect_execution_reservations(
    run_id_or_path: str | Path,
    *,
    project_root: Path,
    job_id: str | None = None,
) -> tuple[InspectedReservation, ...]:
    """List every execution reservation with its resolution state; never claim material."""
    project_root = project_root.resolve()
    state = _load_queue(_resolve_run_path(run_id_or_path, project_root), project_root)
    inspected: list[InspectedReservation] = []
    for job in state.jobs:
        if job_id is not None and job.job_id != job_id:
            continue
        attempts = _job_directory(state.path, job.chunk_id) / "attempts"
        if not attempts.is_dir():
            continue
        for entry in sorted(attempts.iterdir()):
            match = RESERVATION_FILENAME.match(entry.name)
            if match is None or not entry.is_file():
                continue
            number = int(match.group(1))
            content = entry.read_bytes()
            reservation = _load_model(entry, AttemptReservation)
            reservation_hash = sha256_bytes(content)
            destination = _resolution_path(state.path, job.chunk_id, number)
            record = (
                _load_model(destination, ExecutionReservationResolution)
                if destination.is_file()
                else None
            )
            integrity = "valid"
            if record is not None and record.reservation_sha256 != reservation_hash:
                integrity = "reservation drift"
            elif record is not None and record.attempt_id != reservation.attempt_id:
                integrity = "lineage mismatch"
            inspected.append(
                InspectedReservation(
                    job.job_id,
                    job.chunk_id,
                    number,
                    job.status,
                    reservation.status,
                    reservation.provider,
                    project_relative(entry, project_root),
                    reservation_hash,
                    reservation.status is TranslationAttemptStatus.PREPARED
                    and job.status is TranslationJobStatus.RUNNING
                    and job.attempt_count == number,
                    record is not None,
                    record.resolution if record else None,
                    record.resolution_id if record else None,
                    project_relative(destination, project_root) if record else None,
                    record.replay_permitted if record else False,
                    integrity,
                )
            )
    return tuple(inspected)
