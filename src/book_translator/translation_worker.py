"""Provider-neutral translation queue worker: one sequential pass, or a coordinated process pool.

The per-job path is one and the same in both modes. A sequential pass runs it in this process;
``--workers N`` runs it in N spawned execution slots that the coordinator in
``translation_worker_pool`` feeds one work item at a time. Everything that must happen exactly once
per pass — validation, provider preflight, queue recovery — happens here before either mode starts.
"""

import os
import pickle
import socket
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from book_translator.config import load_book_config
from book_translator.models import (
    SCHEMA_VERSION,
    ProviderName,
    ProviderPreflightRecord,
    ProviderRecoveryVerdict,
    TerminologyRegistry,
    TranslationAcceptanceEvidence,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationExecutionKind,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
    TranslationQAReevaluationEvidence,
    TranslationRepairInitiator,
)
from book_translator.paragraph_alignment import (
    ALIGNMENT_QA_VERSION,
)
from book_translator.paths import ProjectPaths
from book_translator.provider_execution import (
    ExecutionResult,
    ExecutorError,
    ProviderExecutionError,
    ProviderPreflightReport,
    TranslationProvider,
    ensure_provider_model_allowed,
    execute_translation_job,
    plan_execution,
    preflight_provider,
    validate_attempt,
)
from book_translator.provider_execution.artifacts import (
    load_yaml,
    private_write,
    project_relative,
    serialize_yaml,
    sha256_bytes,
)
from book_translator.provider_recovery_policy import (
    DEFAULT_MAX_PROVIDER_RECOVERY_ATTEMPTS,
)
from book_translator.terminology import (
    TerminologyError,
    load_registry,
    validate_terminology,
    validate_translation_qa,
)
from book_translator.terminology.exemption_workflow import frozen_occurrence_exemptions
from book_translator.translation_acceptance_qa import assess_translation
from book_translator.translation_auto_repair import (
    ACCEPTED,
    PERMANENT_FAILURE,
    PROVIDER_FAILED,
    QA_REJECTED,
    AutoRepairError,
    AutoRepairOutcome,
    auto_repair_job,
    plan_auto_repair,
)
from book_translator.translation_provider_recovery import (
    ProviderRecoveryError,
    ProviderRecoveryPlan,
    authorize_provider_recovery,
    is_provider_recovery_candidate,
    is_repair_recovery_candidate,
    plan_provider_recovery,
    record_preflight,
)
from book_translator.translation_queue import (
    QueueError,
    claim_ineligibility_reason,
    claim_job,
    claim_priority_key,
    is_claimable,
    load_run_summary,
    record_claim_error,
    recover_run,
    retry_job,
)
from book_translator.translation_queue.policy import attempt_budget_exhausted
from book_translator.translation_queue.workflow import (
    ClaimResult,
    QueueState,
    _append_event,
    _event,
    _load_queue,
    _locked_queue,
    _persist_job_and_run,
    _resolve_run_path,
    _validate_run_snapshot,
)
from book_translator.translation_repair import (
    RepairResult,
    TranslationRepairError,
    repair_translation_job,
)
from book_translator.translation_repair_policy import (
    DEFAULT_MAX_REPAIR_ATTEMPTS,
    is_auto_repair_candidate,
)

TRANSLATION_WORKER_VERSION = "1.0.0"


class TranslationWorkerError(ValueError):
    """Raised when a worker cannot safely continue."""


@dataclass(frozen=True, slots=True)
class WorkerOptions:
    provider: ProviderName
    model: str | None
    worker_id: str
    max_jobs: int
    lease_seconds: int
    timeout_seconds: int
    max_output_tokens: int
    reasoning_effort: str | None = None
    store_raw_response: bool = True
    retry_provider_failures: bool = True
    stop_on_error: bool = False
    dry_run: bool = False
    job_id: str | None = None
    chapter_id: str | None = None
    auto_repair: bool = False
    include_qa_rejected: bool = False
    max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS
    auto_recover_provider_failures: bool = False
    include_failed: bool = False
    max_provider_recovery_attempts: int = DEFAULT_MAX_PROVIDER_RECOVERY_ATTEMPTS
    #: Execution slots. One is the sequential worker; more start a coordinated process pool whose
    #: slots each process one job at a time, all bounded by the one global ``max_jobs``.
    workers: int = 1


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One unit the coordinator hands to an execution slot.

    ``assess`` reconciles an already provider-successful draft, ``claim`` takes the next eligible
    pending job from the queue's own canonical ordering, and ``remediate`` runs bounded automatic
    repair on a job that QA already rejected. ``job_id`` is ``None`` only for a claim, whose job
    the queue chooses under its lock.
    """

    kind: Literal["assess", "claim", "remediate"]
    job_id: str | None


@dataclass(frozen=True, slots=True)
class WorkerJobResult:
    """What one job's whole pass through the worker came to, however many attempts it took.

    The three attempt-ish counters are deliberately not interchangeable. ``repair_attempts``
    counts repair *executions* — provider calls carrying a repair request. ``repair_decisions``
    counts the QA judgements that a repair was warranted, which a recovery replay never adds to.
    ``provider_recovery_attempts`` counts re-entries authorized after a provider failure, of
    either kind.
    """

    job_id: str
    outcome: str
    attempt_number: int | None = None
    repair_attempts: int = 0
    auto_repair_verdict: str | None = None
    auto_repair_findings: tuple[str, ...] = ()
    provider_recovery_attempts: int = 0
    provider_recovery_verdict: str | None = None
    provider_failure_class: str | None = None
    repair_decisions: int = 0
    repair_recovery_attempts: int = 0
    recovered_execution_kind: str | None = None
    repair_root_attempt: int | None = None
    provider_contacted: bool = False
    #: Why an ``error`` outcome happened: the failure class and its structural message. Never
    #: book content or claim material — every failure the worker records is of that kind.
    error: str | None = None
    #: Which execution slot produced this result; ``None`` for the sequential worker.
    worker_slot: int | None = None


@dataclass(frozen=True, slots=True)
class WorkerResult:
    run_id: str
    worker_id: str
    processed: int
    accepted: int
    qa_rejected: int
    provider_failed: int
    retry_pending: int
    permanent_failure: int
    errors: int
    dry_run: bool
    jobs: tuple[WorkerJobResult, ...]
    auto_repaired: int = 0
    repair_attempts: int = 0
    provider_recovered: int = 0
    provider_recovery_attempts: int = 0
    repair_decisions: int = 0
    repair_recovered: int = 0
    repair_recovery_attempts: int = 0
    #: Effective execution slots: one for a sequential pass, otherwise the pool size actually used.
    workers: int = 1
    #: Execution slots that died or failed outside the per-job path; each is also an ``error`` row.
    worker_failures: int = 0


@dataclass(frozen=True, slots=True)
class WorkerProgress:
    """A compact snapshot the coordinator reports after every finished work item.

    It is what a terminal renders instead of polling the run summary, which is expensive: the
    counters here come from results the slots returned, not from re-reading the queue.
    """

    workers: int
    active: int
    completed: int
    planned: int
    accepted: int
    qa_rejected: int
    provider_failed: int
    retry_pending: int
    permanent_failure: int
    errors: int
    worker_failures: int
    stopping: bool
    latest: WorkerJobResult | None = None


@dataclass(slots=True)
class _RecoverySession:
    """One worker pass's provider-health evidence for provider-recovery decisions.

    The preflight is taken lazily and at most once per pass, because a recovery decision needs
    *current* health rather than a fresh subprocess per job. It is dropped again the moment a
    recovered attempt fails, so a provider that lapsed mid-pass can never authorize a second
    recovery on evidence that predates the lapse.
    """

    provider: ProviderName
    preflight: Callable[[ProviderName], ProviderPreflightReport] | None = None
    record: ProviderPreflightRecord | None = None

    def evidence(self, now: datetime) -> ProviderPreflightRecord:
        if self.record is None:
            self.record = record_preflight(
                self.provider, performed_at=now, preflight=self.preflight
            )
        return self.record

    def invalidate(self) -> None:
        self.record = None


def generated_worker_id() -> str:
    host = "".join(character if character.isalnum() else "-" for character in socket.gethostname())
    return f"book-translator-worker-{host}-{os.getpid()}"


def _acceptance_path(run_path: Path, job: TranslationJob, attempt_number: int) -> Path:
    expected = ProjectPaths(run_path.parents[2]).translation_acceptance(
        job.run_id, job.chunk_id, attempt_number
    )
    if expected.parent.parent.parent.parent != run_path:
        raise TranslationWorkerError("translation acceptance path does not match run lineage")
    return expected


def _replace_job(
    jobs: tuple[TranslationJob, ...], changed: TranslationJob
) -> tuple[TranslationJob, ...]:
    return tuple(changed if item.job_id == changed.job_id else item for item in jobs)


def _validate_evidence_files(
    evidence: TranslationAcceptanceEvidence, *, project_root: Path
) -> None:
    references = (
        (evidence.attempt_file, evidence.attempt_sha256, "attempt"),
        (evidence.source_file, evidence.source_sha256, "source"),
        (evidence.translation_file, evidence.translation_sha256, "translation"),
    )
    for relative, expected_hash, label in references:
        path = project_root / relative
        if not path.is_file() or sha256_bytes(path.read_bytes()) != expected_hash:
            raise TranslationWorkerError(f"acceptance {label} evidence hash mismatch")
    if evidence.terminology_file is not None and evidence.terminology_sha256 is not None:
        terminology_path = project_root / evidence.terminology_file
        if (
            not terminology_path.is_file()
            or sha256_bytes(terminology_path.read_bytes()) != evidence.terminology_sha256
        ):
            raise TranslationWorkerError("acceptance terminology evidence hash mismatch")
    if (
        evidence.terminology_report.source_sha256 != evidence.source_sha256
        or evidence.structural_report.source_sha256 != evidence.source_sha256
        or evidence.terminology_report.translation_sha256 != evidence.translation_sha256
        or evidence.structural_report.translation_sha256 != evidence.translation_sha256
    ):
        raise TranslationWorkerError("acceptance QA report hash lineage mismatch")


def _assessable_job(state: QueueState, job_id: str) -> TranslationJob:
    job = next((item for item in state.jobs if item.job_id == job_id), None)
    if job is None:
        raise TranslationWorkerError(f"translation job not found: {job_id}")
    if job.status is not TranslationJobStatus.SUCCEEDED:
        raise TranslationWorkerError("only provider-succeeded jobs can be assessed")
    return job


@dataclass(frozen=True, slots=True)
class _PreparedAcceptance:
    """Acceptance QA computed outside the queue lock, with the lineage it was computed against.

    ``evidence`` is ``None`` when evidence for the attempt was already published — by an earlier
    pass that stopped before recording it on the job — so there was nothing to compute.
    """

    attempt_number: int
    manifest_sha256: str
    evidence: TranslationAcceptanceEvidence | None


def _prepare_acceptance(
    state: QueueState,
    job: TranslationJob,
    *,
    project_root: Path,
    now: datetime,
) -> _PreparedAcceptance:
    """Validate one provider-successful draft and compute its QA verdict, holding no lock.

    Everything here only reads immutable lineage — the attempt, the frozen source, the frozen
    terminology and anomaly resources — and computes. Persisted attempt validation reloads the
    whole run, and on a real book that alone takes tens of seconds, so none of it may run inside
    the queue's critical section. What it produces is published only after
    :func:`_publish_acceptance` has re-checked, under the lock, that it still describes the job.
    """
    run_path = state.path
    if job.successful_attempt is None:
        raise TranslationWorkerError("provider-succeeded job lacks a successful attempt reference")
    attempt_number = job.successful_attempt
    prepared = _PreparedAcceptance(attempt_number, state.run.snapshot_manifest_sha256, None)
    if _acceptance_path(run_path, job, attempt_number).exists():
        return prepared
    attempt_directory = run_path / "jobs" / job.chunk_id / "attempts" / f"{attempt_number:03d}"
    attempt_path = attempt_directory / "attempt.yaml"
    attempt = load_yaml(attempt_path, TranslationAttempt)
    validation = validate_attempt(
        run_path, job.job_id, attempt_number, project_root=project_root, now=lambda: now
    )
    if not validation.valid or attempt.status is not TranslationAttemptStatus.SUCCEEDED:
        raise TranslationWorkerError("successful provider attempt failed persisted validation")
    if attempt.translation_file is None or attempt.translation_sha256 is None:
        raise TranslationWorkerError("successful provider attempt lacks translation evidence")
    source_path = project_root / job.chunk_source_file
    translation_path = project_root / attempt.translation_file
    source_bytes = source_path.read_bytes()
    translation_bytes = translation_path.read_bytes()
    if sha256_bytes(source_bytes) != job.chunk_source_sha256:
        raise TranslationWorkerError("frozen chunk source hash mismatch during acceptance")
    if sha256_bytes(translation_bytes) != attempt.translation_sha256:
        raise TranslationWorkerError("translation hash mismatch during acceptance")
    source_text = source_bytes.decode("utf-8")
    translation_text = translation_bytes.decode("utf-8")
    snapshot_directory = project_root / state.run.snapshot_path
    manifest = load_yaml(snapshot_directory / "manifest.yaml", TranslationManifest)
    terminology_file = (
        (manifest.terminology.frozen_file or manifest.terminology.file)
        if manifest.terminology is not None
        else None
    )
    terminology_hash = manifest.terminology.sha256 if manifest.terminology is not None else None
    if terminology_file is None:
        registry = TerminologyRegistry(
            schema_version=SCHEMA_VERSION,
            version="1.0.0",
            book_id=manifest.book.book_id,
            source_language=manifest.book.source_language,
            target_language=manifest.book.target_language,
        )
    else:
        terminology_path = project_root / terminology_file
        if sha256_bytes(terminology_path.read_bytes()) != terminology_hash:
            raise TranslationWorkerError("frozen terminology resource hash mismatch")
        registry = load_registry(terminology_path)
    anomaly_registry = None
    if manifest.source_anomalies is not None:
        from book_translator.models import SourceAnomalyRegistry

        anomaly_path = project_root / (
            manifest.source_anomalies.frozen_file or manifest.source_anomalies.file
        )
        if sha256_bytes(anomaly_path.read_bytes()) != manifest.source_anomalies.sha256:
            raise TranslationWorkerError("frozen source anomaly resource hash mismatch")
        anomaly_registry = load_yaml(anomaly_path, SourceAnomalyRegistry)
    # Paragraph identity is judged after the structural and terminology validators and before
    # acceptance.  Which mode it runs in is read from the attempt's own recorded output contract
    # rather than from current configuration, so an attempt is always assessed under the contract
    # it was actually produced against.
    assessment = assess_translation(
        source_text,
        translation_text,
        registry=registry,
        target_language=job.target_language,
        output_contract=attempt.output_contract,
        anomaly_registry=anomaly_registry,
        chapter_id=job.chapter_id,
        # Resolved from this module at call time, which is the seam lock-scope tests replace.
        terminology_validator=validate_terminology,
        structural_validator=validate_translation_qa,
        occurrence_exemptions=frozen_occurrence_exemptions(
            manifest,
            project_root,
            registry=registry,
            chunk_id=job.chunk_id,
            source_text=source_text,
        ),
    )
    terminology_report = assessment.terminology_report
    structural_report = assessment.structural_report
    alignment_report = assessment.alignment_report
    accepted = assessment.accepted
    status = (
        TranslationAcceptanceStatus.ACCEPTED
        if accepted
        else TranslationAcceptanceStatus.QA_REJECTED
    )
    evidence = TranslationAcceptanceEvidence(
        schema_version=SCHEMA_VERSION,
        acceptance_engine_version=TRANSLATION_WORKER_VERSION,
        run_id=job.run_id,
        job_id=job.job_id,
        attempt_id=attempt.attempt_id,
        attempt_number=attempt_number,
        attempt_file=project_relative(attempt_path, project_root),
        attempt_sha256=sha256_bytes(attempt_path.read_bytes()),
        source_file=job.chunk_source_file,
        source_sha256=job.chunk_source_sha256,
        translation_file=attempt.translation_file,
        translation_sha256=attempt.translation_sha256,
        terminology_file=terminology_file,
        terminology_sha256=terminology_hash,
        # The terminology and structural QA versions stay unrecorded here, as they have always
        # been: recording them now would make every new rejection look like engine drift to the
        # auto-repair policy.  The alignment version is recorded because its absence is what
        # distinguishes evidence this gate never judged.
        alignment_qa_version=ALIGNMENT_QA_VERSION,
        terminology_report=terminology_report,
        structural_report=structural_report,
        alignment_report=alignment_report,
        status=status,
        accepted=accepted,
        created_at=now,
    )
    return replace(prepared, evidence=evidence)


def _publish_acceptance(
    state: QueueState,
    job: TranslationJob,
    prepared: _PreparedAcceptance,
    *,
    project_root: Path,
    now: datetime,
) -> TranslationAcceptanceStatus:
    """Record a verdict computed outside the lock, if it still describes the job.

    ``state`` is the queue as the caller reloaded it under the queue lock it still holds, and
    ``job`` is unassessed and provider-successful in it. This is compare-and-revalidate: the QA
    was computed from a job and from input bytes read earlier, so before anything is written the
    job must still name the same successful attempt under the same frozen snapshot, and every
    input the evidence hashes — attempt record, source, translation, terminology — must still
    hash as it did. Anything else means the verdict describes a draft this job no longer stands
    on, and nothing is published. Only cheap re-hashing and the writes happen here.
    """
    run_path = state.path
    if (
        job.successful_attempt != prepared.attempt_number
        or state.run.snapshot_manifest_sha256 != prepared.manifest_sha256
    ):
        raise TranslationWorkerError(
            "translation job changed while its acceptance was assessed; nothing was published"
        )
    attempt_number = prepared.attempt_number
    destination = _acceptance_path(run_path, job, attempt_number)
    evidence_path = destination / "evidence.yaml"
    if destination.exists():
        # Published by an earlier pass that stopped before recording it on the job. It is adopted
        # exactly as it always was, after the same lineage and hash checks below.
        evidence = load_yaml(evidence_path, TranslationAcceptanceEvidence)
    else:
        if prepared.evidence is None:
            raise TranslationWorkerError(
                "acceptance evidence changed while it was assessed; nothing was published"
            )
        evidence = prepared.evidence
        try:
            _validate_evidence_files(evidence, project_root=project_root)
        except TranslationWorkerError as exc:
            raise TranslationWorkerError(
                f"acceptance inputs changed while QA was computed; nothing was published: {exc}"
            ) from exc
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{attempt_number:03d}.staging.", dir=destination.parent)
        )
        os.chmod(staging, 0o700)
        try:
            private_write(staging / "evidence.yaml", serialize_yaml(evidence).encode())
            os.rename(staging, destination)
        finally:
            if staging.exists():
                staging.rmdir()
    attempt_file = project_relative(
        run_path / "jobs" / job.chunk_id / "attempts" / f"{attempt_number:03d}" / "attempt.yaml",
        project_root,
    )
    if (
        evidence.run_id != job.run_id
        or evidence.job_id != job.job_id
        or evidence.attempt_number != attempt_number
        or evidence.source_sha256 != job.chunk_source_sha256
        # A slot's evidence must describe that slot's attempt; a verdict about any other draft
        # (a reuse or carry-forward sidecar, say) is never adopted for it.
        or evidence.attempt_file != attempt_file
    ):
        raise TranslationWorkerError("acceptance evidence lineage mismatch")
    _validate_evidence_files(evidence, project_root=project_root)
    persisted_bytes = evidence_path.read_bytes()
    changed = job.model_copy(
        update={
            "acceptance_status": evidence.status,
            "acceptance_origin": TranslationAcceptanceOrigin.INITIAL_QA,
            "accepted_attempt": attempt_number,
            "acceptance_file": project_relative(evidence_path, project_root),
            "acceptance_sha256": sha256_bytes(persisted_bytes),
            "updated_at": now,
            "recent_events": _append_event(
                job,
                _event(
                    "translation_accepted" if evidence.accepted else "translation_qa_rejected",
                    now,
                ),
            ),
        }
    )
    _persist_job_and_run(state, _replace_job(state.jobs, changed), changed, now)
    return evidence.status


def accept_provider_success(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
    now: Callable[[], datetime] | None = None,
) -> TranslationAcceptanceStatus:
    """Assess one provider-successful draft and record the verdict on the job.

    Two phases, so that a slow assessment never holds up another execution slot. The first holds
    no lock: it validates the snapshot, the attempt, and every frozen input, and computes the QA
    verdict. The second takes the queue lock only to reload the run, confirm the job is still the
    unassessed draft that verdict was computed for, and publish it. A job another process assessed
    in between is answered with the verdict already recorded, never assessed twice.
    """
    project_root = project_root.resolve()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    snapshot_validation = _validate_run_snapshot(run_path, project_root)
    observed = _load_queue(run_path, project_root, snapshot_validation=snapshot_validation)
    job = _assessable_job(observed, job_id)
    if job.acceptance_status is not None:
        return job.acceptance_status
    prepared = _prepare_acceptance(observed, job, project_root=project_root, now=timestamp)
    with _locked_queue(run_path, project_root, snapshot_validation=snapshot_validation) as state:
        current = next((item for item in state.jobs if item.job_id == job_id), None)
        if current is None:
            raise TranslationWorkerError(f"translation job not found: {job_id}")
        if current.acceptance_status is not None:
            return current.acceptance_status
        if current.status is not TranslationJobStatus.SUCCEEDED:
            raise TranslationWorkerError(
                "translation job changed while its acceptance was assessed; nothing was published"
            )
        return _publish_acceptance(
            state, current, prepared, project_root=project_root, now=timestamp
        )


def _is_unassessed_draft(job: TranslationJob) -> bool:
    return job.status is TranslationJobStatus.SUCCEEDED and job.acceptance_status is None


def _matches_selection(job: TranslationJob, options: WorkerOptions) -> bool:
    return (options.job_id is None or job.job_id == options.job_id) and (
        options.chapter_id is None or job.chapter_id == options.chapter_id
    )


def _validate_selection(jobs: tuple[TranslationJob, ...], options: WorkerOptions) -> None:
    if options.job_id is not None and not any(job.job_id == options.job_id for job in jobs):
        raise TranslationWorkerError(
            f"targeted translation job is not part of this run: {options.job_id}"
        )
    if options.chapter_id is not None and not any(
        job.chapter_id == options.chapter_id for job in jobs
    ):
        raise TranslationWorkerError(
            f"targeted chapter has no jobs in this run: {options.chapter_id}"
        )


def _targeted_selection_error(
    job: TranslationJob, options: WorkerOptions
) -> TranslationWorkerError:
    reason = claim_ineligibility_reason(job) or "job is eligible"
    hint = ""
    if is_auto_repair_candidate(job) and not _repairs_enabled(options):
        hint = (
            "; it is a QA-rejected job, which this worker selects only with"
            " --auto-repair --include-qa-rejected"
        )
    elif is_provider_recovery_candidate(job) and not _recoveries_enabled(options):
        hint = (
            "; it is a provider-failed job, which this worker selects only with"
            " --auto-recover-provider-failures --include-failed"
        )
    elif is_repair_recovery_candidate(job) and not _repair_recoveries_enabled(options):
        hint = (
            "; it is a job whose repair a provider failure ended, which this worker selects only"
            " with --auto-recover-provider-failures --include-failed --auto-repair"
        )
    return TranslationWorkerError(
        f"targeted translation job {job.job_id} is not processable: {reason}{hint}"
    )


def _repairs_enabled(options: WorkerOptions) -> bool:
    return options.auto_repair and options.include_qa_rejected


def _is_repair_selection(job: TranslationJob, options: WorkerOptions) -> bool:
    return _repairs_enabled(options) and is_auto_repair_candidate(job)


def _recoveries_enabled(options: WorkerOptions) -> bool:
    return options.auto_recover_provider_failures and options.include_failed


def _is_recovery_selection(job: TranslationJob, options: WorkerOptions) -> bool:
    return _recoveries_enabled(options) and is_provider_recovery_candidate(job)


def _repair_recoveries_enabled(options: WorkerOptions) -> bool:
    """Return whether this worker may recover a repair a provider failure ended.

    Both gates are required and each authorizes a different thing. The recovery flags say the
    worker may re-enter a job after a provider failure; ``--auto-repair`` says it may spend
    provider attempts on repair requests at all. Recovering a failed repair does both, so an
    operator who enabled only one has not asked for it. ``--include-qa-rejected`` is deliberately
    *not* required: that flag admits new QA repair decisions, and a recovery makes none.
    """
    return _recoveries_enabled(options) and options.auto_repair


def _is_repair_recovery_selection(job: TranslationJob, options: WorkerOptions) -> bool:
    return _repair_recoveries_enabled(options) and is_repair_recovery_candidate(job)


def _is_any_recovery_selection(job: TranslationJob, options: WorkerOptions) -> bool:
    return _is_recovery_selection(job, options) or _is_repair_recovery_selection(job, options)


def _selectable_jobs(state: QueueState, options: WorkerOptions) -> tuple[TranslationJob, ...]:
    ordered = sorted(state.jobs, key=claim_priority_key)
    selectable = tuple(
        job
        for job in ordered
        if _matches_selection(job, options) and (_is_unassessed_draft(job) or is_claimable(job))
    )
    # Repair candidates form their own class *after* the canonical queue rather than inside it.
    # Pending translation ordering is a contract; remediation of already-drafted work must never
    # displace, reorder, or starve it.
    selectable += tuple(
        job
        for job in ordered
        if _matches_selection(job, options) and _is_repair_selection(job, options)
    )
    # Provider-recovery candidates form a third class after both of those, for the same reason
    # and one more: a recovered job re-enters the canonical queue, so selecting it before the
    # queue is drained would let remediation decide what gets translated next.
    selectable += tuple(
        job
        for job in ordered
        if _matches_selection(job, options) and _is_recovery_selection(job, options)
    )
    # Failed repairs come last of all. They are remediation of remediation: the job already has a
    # draft, that draft already has a repair decision, and only the provider call was lost. That
    # makes them the least urgent work in the run — nothing downstream is waiting on them that is
    # not already waiting — and putting them behind translation recovery keeps the cheaper,
    # simpler remedy first. Ordering within the class stays the canonical queue order.
    selectable += tuple(
        job
        for job in ordered
        if _matches_selection(job, options) and _is_repair_recovery_selection(job, options)
    )
    if options.job_id is not None and not selectable:
        target = next(job for job in state.jobs if job.job_id == options.job_id)
        raise _targeted_selection_error(target, options)
    return selectable[: options.max_jobs]


def _validate_existing_acceptance(state_path: Path, project_root: Path) -> None:
    state = load_run_summary(state_path, project_root=project_root)
    for job in state.jobs:
        if job.acceptance_status is None:
            continue
        if job.acceptance_file is None or job.acceptance_sha256 is None:
            raise TranslationWorkerError("assessed job lacks acceptance evidence metadata")
        evidence_path = project_root / job.acceptance_file
        if (
            not evidence_path.is_file()
            or sha256_bytes(evidence_path.read_bytes()) != job.acceptance_sha256
        ):
            raise TranslationWorkerError("persisted acceptance evidence hash mismatch")
        evidence = load_yaml(evidence_path, TranslationAcceptanceEvidence)
        if (
            evidence.run_id != job.run_id
            or evidence.job_id != job.job_id
            or evidence.attempt_number != job.accepted_attempt
        ):
            raise TranslationWorkerError("persisted acceptance evidence lineage mismatch")
        _validate_evidence_files(evidence, project_root=project_root)
        if job.acceptance_origin is TranslationAcceptanceOrigin.QA_REEVALUATION:
            if (
                job.latest_reevaluation_file is None
                or job.latest_reevaluation_sha256 is None
                or job.reevaluation_count < 1
            ):
                raise TranslationWorkerError("re-evaluated job lacks effective evidence metadata")
            reevaluation_path = project_root / job.latest_reevaluation_file
            reevaluation_bytes = reevaluation_path.read_bytes()
            if sha256_bytes(reevaluation_bytes) != job.latest_reevaluation_sha256:
                raise TranslationWorkerError("effective re-evaluation evidence hash mismatch")
            reevaluation = load_yaml(reevaluation_path, TranslationQAReevaluationEvidence)
            if (
                reevaluation.run_id != job.run_id
                or reevaluation.job_id != job.job_id
                or reevaluation.attempt_number != job.accepted_attempt
                or reevaluation.reevaluation_number != job.reevaluation_count
                or reevaluation.resulting_status is not job.acceptance_status
                or reevaluation.reevaluated_from_file != job.acceptance_file
                or reevaluation.reevaluated_from_sha256 != job.acceptance_sha256
            ):
                raise TranslationWorkerError("effective re-evaluation lineage mismatch")
        elif evidence.status is not job.acceptance_status:
            raise TranslationWorkerError("persisted acceptance status mismatch")


#: Upper bound of a recorded per-job diagnostic; long enough for any structural message.
DIAGNOSTIC_LIMIT = 500


def _diagnostic(exc: BaseException) -> str:
    """Render one bounded line naming the failure class and its message."""
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text if len(text) <= DIAGNOSTIC_LIMIT else text[: DIAGNOSTIC_LIMIT - 1] + "…"


def _ensure_model_policy(state: QueueState, options: WorkerOptions, project_root: Path) -> None:
    """Refuse a provider/model the run's own book configuration disallows, before any claim.

    The executor asks the same question when it plans, but by then a claim has already spent an
    attempt slot. Asking first — like the provider preflight — keeps a misconfigured command from
    consuming anything, and makes a dry run a faithful rehearsal of the configuration. The
    configuration is the run's book's, resolved from the run's frozen lineage.
    """
    config = load_book_config(ProjectPaths(project_root, state.book).book_config)
    try:
        ensure_provider_model_allowed(config, options.provider, options.model)
    except ExecutorError as exc:
        raise TranslationWorkerError(str(exc)) from exc


#: Per-job failures a sequential worker records and moves past, rather than aborting the run.
_JOB_FAILURES = (
    AutoRepairError,
    ExecutorError,
    ProviderRecoveryError,
    QueueError,
    TerminologyError,
    TranslationRepairError,
    TranslationWorkerError,
    OSError,
    UnicodeError,
)


def _assess_recovery(
    run_path: Path,
    job: TranslationJob,
    options: WorkerOptions,
    session: _RecoverySession,
    *,
    project_root: Path,
    now: Callable[[], datetime] | None,
) -> ProviderRecoveryPlan:
    """Plan one recovery, supplying provider health only when the policy asks for it.

    The two-step is the whole point of ``preflight_required``: the first plan is free and answers
    most cases, and only a family whose recovery rests on the provider being usable again pays
    for a preflight subprocess. Which kind of request is being recovered is read from the job by
    the planner, never chosen here.
    """
    clock = now or (lambda: datetime.now(UTC))
    plan = plan_provider_recovery(
        run_path,
        job.job_id,
        project_root=project_root,
        max_provider_recovery_attempts=options.max_provider_recovery_attempts,
        max_repair_attempts=options.max_repair_attempts,
        now=clock,
    )
    if plan.verdict is not ProviderRecoveryVerdict.PREFLIGHT_REQUIRED:
        return plan
    return plan_provider_recovery(
        run_path,
        job.job_id,
        project_root=project_root,
        max_provider_recovery_attempts=options.max_provider_recovery_attempts,
        max_repair_attempts=options.max_repair_attempts,
        preflight=session.evidence(clock()),
        now=clock,
    )


def _planned_recovery_result(
    run_path: Path,
    job: TranslationJob,
    options: WorkerOptions,
    session: _RecoverySession,
    *,
    project_root: Path,
    now: Callable[[], datetime] | None,
) -> WorkerJobResult:
    """Describe what provider recovery would do to one already-failed job.

    Unlike every other dry-run answer this one may run the provider preflight, because the
    recovery verdict for a health-dependent failure is not knowable without current health
    evidence and reporting ``preflight_required`` would answer a different question than the one
    asked. The preflight performs no model call, sends no content, and writes nothing.

    A failed repair is answered in the same terms, plus the lineage it would preserve: the repair
    root it would still repair from, and the fact that the recovered attempt stays a repair.
    Planning one re-derives the repair request to prove it is reproducible, which reads the
    workspace and hashes it — it writes nothing and sends nothing.
    """
    try:
        plan = _assess_recovery(run_path, job, options, session, project_root=project_root, now=now)
    except _JOB_FAILURES as exc:
        return WorkerJobResult(
            job.job_id, "would_refuse_recovery", provider_recovery_verdict=str(exc)
        )
    repairing = plan.execution_kind is TranslationExecutionKind.REPAIR
    if not plan.recoverable:
        outcome = "would_refuse_repair_recovery" if repairing else "would_refuse_recovery"
    else:
        outcome = "would_recover_failed_repair" if repairing else "would_recover_provider_failure"
    return WorkerJobResult(
        job.job_id,
        outcome,
        plan.authorized_attempt_number if plan.recoverable else job.last_attempt,
        provider_recovery_verdict=plan.verdict.value,
        provider_failure_class=plan.failure_class.value,
        recovered_execution_kind=plan.execution_kind.value,
        repair_root_attempt=plan.repair_root_attempt,
    )


def _planned_job_result(
    run_path: Path,
    job: TranslationJob,
    options: WorkerOptions,
    session: _RecoverySession,
    *,
    project_root: Path,
    now: Callable[[], datetime] | None,
) -> WorkerJobResult:
    """Describe one selected job for a dry run, including the verdict where knowable.

    A pending job's QA outcome does not exist yet and is not guessed. A job that is already
    QA-rejected or already provider-failed has real evidence, so the governing policy can be
    evaluated against it without executing anything.
    """
    if _is_any_recovery_selection(job, options):
        return _planned_recovery_result(
            run_path, job, options, session, project_root=project_root, now=now
        )
    if not _is_repair_selection(job, options):
        return WorkerJobResult(job.job_id, "would_process")
    try:
        plan = plan_auto_repair(
            run_path,
            job.job_id,
            project_root=project_root,
            max_repair_attempts=options.max_repair_attempts,
        )
    except _JOB_FAILURES as exc:
        return WorkerJobResult(job.job_id, "would_refuse_repair", auto_repair_verdict=str(exc))
    return WorkerJobResult(
        job.job_id,
        "would_auto_repair" if plan.repairable else "would_refuse_repair",
        plan.repair_attempt_number if plan.repairable else None,
        auto_repair_verdict=plan.assessment.verdict.value,
        auto_repair_findings=plan.assessment.triggering_codes,
    )


def _repair_result(outcome: AutoRepairOutcome) -> WorkerJobResult:
    """Report one bounded auto-repair cycle.

    Every repair it performed was authorized by its own QA decision, so executions and decisions
    are the same number here. They diverge only where a recovery replays an execution.
    """
    return WorkerJobResult(
        outcome.job_id,
        outcome.outcome,
        outcome.attempt_number,
        outcome.repair_attempts,
        outcome.verdict.value,
        outcome.triggering_codes,
        repair_decisions=outcome.repair_attempts,
        provider_contacted=outcome.provider_contacted,
    )


def _continued(first: WorkerJobResult, second: WorkerJobResult) -> WorkerJobResult:
    """Fold a continuation of the same job into a single result.

    A job is reported once however many remedies it took, so the later outcome wins and the
    effort adds up. Verdicts and lineage keep the most recent non-empty answer: the earlier
    verdict described a situation the continuation has since moved past.
    """
    return replace(
        second,
        repair_attempts=first.repair_attempts + second.repair_attempts,
        repair_decisions=first.repair_decisions + second.repair_decisions,
        provider_recovery_attempts=(
            first.provider_recovery_attempts + second.provider_recovery_attempts
        ),
        repair_recovery_attempts=(first.repair_recovery_attempts + second.repair_recovery_attempts),
        auto_repair_verdict=second.auto_repair_verdict or first.auto_repair_verdict,
        auto_repair_findings=second.auto_repair_findings or first.auto_repair_findings,
        provider_recovery_verdict=(
            second.provider_recovery_verdict or first.provider_recovery_verdict
        ),
        provider_failure_class=second.provider_failure_class or first.provider_failure_class,
        recovered_execution_kind=(
            second.recovered_execution_kind or first.recovered_execution_kind
        ),
        repair_root_attempt=(
            second.repair_root_attempt
            if second.repair_root_attempt is not None
            else first.repair_root_attempt
        ),
        provider_contacted=first.provider_contacted or second.provider_contacted,
    )


def _auto_repair(
    run_path: Path,
    job_id: str,
    options: WorkerOptions,
    *,
    project_root: Path,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
) -> AutoRepairOutcome:
    return auto_repair_job(
        run_path,
        job_id,
        project_root=project_root,
        provider=options.provider,
        model=options.model,
        worker_id=options.worker_id,
        lease_seconds=options.lease_seconds,
        timeout_seconds=options.timeout_seconds,
        max_output_tokens=options.max_output_tokens,
        reasoning_effort=options.reasoning_effort,
        store_raw_response=options.store_raw_response,
        max_repair_attempts=options.max_repair_attempts,
        provider_factory=provider_factory,
        now=now,
    )


def _assessed_result(
    run_path: Path,
    job_id: str,
    status: TranslationAcceptanceStatus,
    attempt_number: int | None,
    options: WorkerOptions,
    session: _RecoverySession,
    *,
    project_root: Path,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
) -> WorkerJobResult:
    """Report one freshly assessed job, continuing into automatic repair when QA rejected it.

    The job is reported once with its final outcome. A rejection that automatic repair resolves
    is an accepted job that took two provider attempts, not two jobs.

    A repair the provider failed to deliver is the one case where this does not end the job. The
    QA decision to repair is still standing and unspent, so — when the operator enabled recovery
    as well as repair — the failure continues into bounded recovery of that same repair rather
    than being reported as the end of the line.
    """
    if status is not TranslationAcceptanceStatus.QA_REJECTED or not options.auto_repair:
        return WorkerJobResult(job_id, status.value, attempt_number)
    repaired = _repair_result(
        _auto_repair(
            run_path,
            job_id,
            options,
            project_root=project_root,
            provider_factory=provider_factory,
            now=now,
        )
    )
    if repaired.outcome != PROVIDER_FAILED or not _repair_recoveries_enabled(options):
        return repaired
    state = load_run_summary(run_path, project_root=project_root)
    failed = next((item for item in state.jobs if item.job_id == job_id), None)
    if failed is None or not is_repair_recovery_candidate(failed):
        return repaired
    return _continued(
        repaired,
        _recover_failed_repair(
            state.path,
            failed,
            options,
            session,
            project_root=project_root,
            provider_factory=provider_factory,
            now=now,
        ),
    )


def _recorded_claim_error(
    run_path: Path,
    job: TranslationJob,
    options: WorkerOptions,
    exc: BaseException,
    *,
    project_root: Path,
    now: Callable[[], datetime] | None,
) -> str:
    """Persist a claim-time failure on the job the worker still holds and return the diagnostic.

    A failure between the claim and the reservation would otherwise leave nothing but a consumed
    slot. If the record itself cannot be written — the queue may be what failed — the result
    still carries the diagnostic, extended with why it was not persisted.
    """
    diagnostic = _diagnostic(exc)
    try:
        record_claim_error(
            run_path,
            job.job_id,
            project_root=project_root,
            worker_id=options.worker_id,
            message=diagnostic,
            now=now,
        )
    except _JOB_FAILURES as record_exc:
        return f"{diagnostic} (not recorded on the job: {_diagnostic(record_exc)})"
    return diagnostic


def _execute_claim(
    run_path: Path,
    claim_token: str,
    job: TranslationJob,
    options: WorkerOptions,
    *,
    project_root: Path,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
) -> ExecutionResult:
    """Run one claimed job through the single governed provider-execution path."""
    plan = plan_execution(
        run_path,
        job.job_id,
        project_root=project_root,
        worker_id=options.worker_id,
        claim_token=claim_token,
        provider=options.provider,
        model=options.model,
        timeout_seconds=options.timeout_seconds,
        max_output_tokens=options.max_output_tokens,
        reasoning_effort=options.reasoning_effort,
        store_raw_response=options.store_raw_response,
        sdk_retry_count=0,
        now=now,
    )
    return execute_translation_job(
        plan,
        worker_id=options.worker_id,
        claim_token=claim_token,
        provider=provider_factory(job) if provider_factory is not None else None,
        now=now,
    )


def _refused_recovery(plan: ProviderRecoveryPlan, recoveries: int) -> WorkerJobResult:
    """Report a job the recovery policy declined, naming the verdict rather than hiding it.

    A declined *repair* recovery is reported as ``qa_rejected`` rather than ``provider_failed``,
    because that is what the job is: the rejection it carries is the standing fact about it, and
    the lost repair call did not change it. Reporting a provider failure would tell an operator
    to look at the provider when what is waiting is a rejected draft.
    """
    repairing = plan.execution_kind is TranslationExecutionKind.REPAIR
    return WorkerJobResult(
        plan.job_id,
        QA_REJECTED if repairing else PROVIDER_FAILED,
        plan.failed_attempt.attempt_number,
        provider_recovery_attempts=recoveries,
        provider_recovery_verdict=plan.verdict.value,
        provider_failure_class=plan.failure_class.value,
        recovered_execution_kind=plan.execution_kind.value,
        repair_root_attempt=plan.repair_root_attempt,
    )


def _recover_failed_repair(
    run_path: Path,
    job: TranslationJob,
    options: WorkerOptions,
    session: _RecoverySession,
    *,
    project_root: Path,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
) -> WorkerJobResult:
    """Recover one repair a provider failure ended, for as long as policy and budget allow.

    Each cycle authorizes at most one new attempt and publishes why before the queue is touched,
    exactly as translation recovery does. What differs is what happens next: the authorization
    restores the QA-rejected draft the failed attempt displaced, and the job then goes back
    through the *same* repair mechanism an operator would drive by hand, carrying the same
    ``execution_kind`` and the same ``repaired_from_attempt``. There is no second repair path and
    no way for a recovered repair to be sent as a translation.

    No new QA repair decision is made by the recovery itself, and that is the point: the decision
    being replayed is the one already on record. A recovered repair that succeeds but is *still*
    rejected is a different matter — QA has now looked at new output — so it continues into the
    ordinary auto-repair boundary, which decides for itself whether the repair budget permits
    another. If *that* repair's provider call fails in turn, the job is once again a failed repair
    and re-enters this same loop rather than being stranded a step further along.

    The loop is bounded from three directions at once and needs no counter of its own to stop:
    every iteration publishes an authorization before it spends anything, so the recovery budget
    the policy re-evaluates against strictly shrinks; every iteration spends an attempt slot, so
    the job's provider budget shrinks too; and each new repair decision is charged against the
    repair allowance. Whichever runs out first ends it with a verdict rather than a guess.
    """
    # Per-iteration results carry increments of one, and `carried` sums them, so a job that took
    # several cycles is still reported once with honest totals.
    carried = WorkerJobResult(job.job_id, QA_REJECTED)
    while True:
        plan = _assess_recovery(run_path, job, options, session, project_root=project_root, now=now)
        if plan.execution_kind is not TranslationExecutionKind.REPAIR:
            raise TranslationWorkerError("repair recovery planned a non-repair recovery")
        if not plan.recoverable:
            return _continued(carried, _refused_recovery(plan, 0))
        spent = carried.provider_recovery_attempts
        if spent >= plan.assessment.budget.max_provider_recovery_attempts:
            # Unreachable while the budget accounting is sound; kept so that an anomaly can only
            # ever stop this loop, never let it spend without bound.
            raise TranslationWorkerError("repair recovery exceeded its own attempt budget")
        authorize_provider_recovery(run_path, plan, project_root=project_root, now=now)
        result: RepairResult = repair_translation_job(
            run_path,
            job.job_id,
            project_root=project_root,
            provider=options.provider,
            model=options.model,
            worker_id=options.worker_id,
            lease_seconds=options.lease_seconds,
            timeout_seconds=options.timeout_seconds,
            max_output_tokens=options.max_output_tokens,
            reasoning_effort=options.reasoning_effort,
            store_raw_response=options.store_raw_response,
            initiator=TranslationRepairInitiator.AUTOMATIC,
            provider_adapter=(provider_factory(job) if provider_factory is not None else None),
            now=now,
        )
        recovered = WorkerJobResult(
            job.job_id,
            QA_REJECTED,
            result.attempt_number,
            repair_attempts=1,
            provider_recovery_attempts=1,
            provider_recovery_verdict=plan.verdict.value,
            provider_failure_class=plan.failure_class.value,
            repair_recovery_attempts=1,
            recovered_execution_kind=plan.execution_kind.value,
            repair_root_attempt=plan.repair_root_attempt,
            provider_contacted=result.provider_contacted,
        )
        if result.attempt_status is not TranslationAttemptStatus.SUCCEEDED:
            carried = _continued(carried, recovered)
            if result.attempt_status is not TranslationAttemptStatus.FAILED:
                return replace(carried, outcome=PERMANENT_FAILURE)
            # The recovered repair failed too. Whatever the preflight established no longer
            # describes the provider, so the next cycle must re-establish it from scratch.
            session.invalidate()
            continue
        if result.acceptance_status is TranslationAcceptanceStatus.ACCEPTED:
            return replace(_continued(carried, recovered), outcome=ACCEPTED)
        carried = _continued(
            carried,
            _continued(
                recovered,
                _repair_result(
                    _auto_repair(
                        run_path,
                        job.job_id,
                        options,
                        project_root=project_root,
                        provider_factory=provider_factory,
                        now=now,
                    )
                ),
            ),
        )
        if carried.outcome != PROVIDER_FAILED:
            return carried
        # QA rejected the recovered output, a further repair decision was authorized, and *its*
        # provider call failed. That is a fresh failed repair, not a dead end.
        state = load_run_summary(run_path, project_root=project_root)
        current = next((item for item in state.jobs if item.job_id == job.job_id), None)
        if current is None or not is_repair_recovery_candidate(current):
            return carried
        job = current
        session.invalidate()


def _recover_provider_failure(
    run_path: Path,
    job: TranslationJob,
    options: WorkerOptions,
    session: _RecoverySession,
    *,
    project_root: Path,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
) -> WorkerJobResult:
    """Recover one provider-failed job for as long as the policy and budget allow.

    Each cycle authorizes at most one new attempt, publishes why before touching the queue, and
    then hands the job to the ordinary translation path by claiming it explicitly by ID, so no
    pending job is displaced and no second execution path exists. A recovered attempt that
    succeeds continues into ordinary QA acceptance and, when enabled, ordinary automatic repair:
    recovery restores the job to the pipeline rather than replacing what happens next.
    """
    recoveries = 0
    while True:
        plan = _assess_recovery(run_path, job, options, session, project_root=project_root, now=now)
        if not plan.recoverable:
            return _refused_recovery(plan, recoveries)
        if recoveries >= plan.assessment.budget.max_provider_recovery_attempts:
            # Unreachable while the budget accounting is sound; kept so that an anomaly can only
            # ever stop this loop, never let it spend without bound.
            raise TranslationWorkerError("provider recovery exceeded its own attempt budget")
        authorize_provider_recovery(run_path, plan, project_root=project_root, now=now)
        recoveries += 1
        claim = claim_job(
            run_path,
            project_root=project_root,
            worker_id=options.worker_id,
            lease_seconds=options.lease_seconds,
            job_id=job.job_id,
            now=now,
        )
        execution = _execute_claim(
            run_path,
            claim.claim_token,
            claim.job,
            options,
            project_root=project_root,
            provider_factory=provider_factory,
            now=now,
        )
        attempt_number = claim.job.attempt_count
        if execution.attempt_status is TranslationAttemptStatus.SUCCEEDED:
            status = accept_provider_success(
                run_path, job.job_id, project_root=project_root, now=now
            )
            assessed = _assessed_result(
                run_path,
                job.job_id,
                status,
                attempt_number,
                options,
                session,
                project_root=project_root,
                provider_factory=provider_factory,
                now=now,
            )
            return replace(
                assessed,
                provider_recovery_attempts=recoveries,
                provider_recovery_verdict=plan.verdict.value,
                provider_failure_class=plan.failure_class.value,
            )
        if execution.attempt_status is not TranslationAttemptStatus.FAILED:
            return WorkerJobResult(
                job.job_id,
                "permanent_failure",
                attempt_number,
                provider_recovery_attempts=recoveries,
                provider_recovery_verdict=plan.verdict.value,
                provider_failure_class=plan.failure_class.value,
            )
        # The recovered attempt failed too. Whatever the preflight established no longer
        # describes the provider, so the next cycle must re-establish it from scratch.
        session.invalidate()


def _failed_execution_result(
    run_path: Path,
    job: TranslationJob,
    options: WorkerOptions,
    session: _RecoverySession,
    *,
    project_root: Path,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
) -> WorkerJobResult:
    """Decide what a fresh provider failure means for the job that just suffered it.

    The three answers are deliberately ordered by cost. A failure the executor itself marked
    retryable is rescheduled by the queue, which spends no recovery budget and makes no claim
    about provider health. Only a failure that ordinary retry cannot answer reaches the bounded
    recovery boundary, and only when the operator asked for it.
    """
    current = load_run_summary(run_path, project_root=project_root)
    failed = next(item for item in current.jobs if item.job_id == job.job_id)
    attempt = load_yaml(
        current.path
        / "jobs"
        / failed.chunk_id
        / "attempts"
        / f"{failed.last_attempt:03d}"
        / "attempt.yaml",
        TranslationAttempt,
    )
    if (
        options.retry_provider_failures
        and attempt.retryable
        and not attempt_budget_exhausted(failed)
    ):
        category = attempt.error_category.value if attempt.error_category else "unknown"
        retry_job(
            current.path,
            failed.job_id,
            project_root=project_root,
            reason=f"retryable provider failure: {category}",
            now=now,
        )
        return WorkerJobResult(job.job_id, "retry_pending", job.attempt_count)
    if options.auto_recover_provider_failures and is_provider_recovery_candidate(failed):
        return _recover_provider_failure(
            current.path,
            failed,
            options,
            session,
            project_root=project_root,
            provider_factory=provider_factory,
            now=now,
        )
    return WorkerJobResult(job.job_id, "provider_failed", job.attempt_count)


#: The remediation classes, in the order the worker offers them, each with the predicate that
#: recognizes a job belonging to it. The order is the contract: repair before translation
#: recovery, because the cheaper remedy for work that already exists is exhausted before a new
#: provider attempt is authorized for work that does not; and repair recovery last, because it is
#: remediation of remediation. All three sit *after* the canonical pending queue, which no
#: remediation class may reorder.
_REMEDIATION_CLASSES: tuple[Callable[[TranslationJob, WorkerOptions], bool], ...] = (
    _is_repair_selection,
    _is_recovery_selection,
    _is_repair_recovery_selection,
)


def _next_remediation(
    state: QueueState, options: WorkerOptions, seen: set[str]
) -> TranslationJob | None:
    """Return the next already-assessed job a remediation class may act on."""
    ordered = sorted(state.jobs, key=claim_priority_key)
    for belongs in _REMEDIATION_CLASSES:
        candidate = next(
            (
                job
                for job in ordered
                if job.job_id not in seen
                and _matches_selection(job, options)
                and belongs(job, options)
            ),
            None,
        )
        if candidate is not None:
            return candidate
    return None


def _remediation_result(
    run_path: Path,
    job: TranslationJob,
    options: WorkerOptions,
    session: _RecoverySession,
    *,
    project_root: Path,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
) -> WorkerJobResult:
    """Remediate one already-assessed job.

    Which remedy applies is a property of the job, not of the call site: a QA-rejected draft goes
    to bounded automatic repair, a failed translation goes to bounded provider recovery, and a
    job whose repair a provider failure ended goes to bounded recovery *of that repair*. The
    three never substitute for each other, which is why the dispatch reads the job's own class
    rather than an argument.
    """
    try:
        if _is_repair_selection(job, options):
            return _repair_result(
                _auto_repair(
                    run_path,
                    job.job_id,
                    options,
                    project_root=project_root,
                    provider_factory=provider_factory,
                    now=now,
                )
            )
        if _is_repair_recovery_selection(job, options):
            return _recover_failed_repair(
                run_path,
                job,
                options,
                session,
                project_root=project_root,
                provider_factory=provider_factory,
                now=now,
            )
        return _recover_provider_failure(
            run_path,
            job,
            options,
            session,
            project_root=project_root,
            provider_factory=provider_factory,
            now=now,
        )
    except _JOB_FAILURES as exc:
        return WorkerJobResult(job.job_id, "error", job.attempt_count, error=_diagnostic(exc))


def _append_remediation_result(
    results: list[WorkerJobResult],
    run_path: Path,
    job: TranslationJob,
    options: WorkerOptions,
    session: _RecoverySession,
    seen: set[str],
    *,
    project_root: Path,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
) -> bool:
    """Remediate one already-assessed job; return whether the worker may keep processing."""
    seen.add(job.job_id)
    result = _remediation_result(
        run_path,
        job,
        options,
        session,
        project_root=project_root,
        provider_factory=provider_factory,
        now=now,
    )
    results.append(result)
    return not (result.outcome == "error" and options.stop_on_error)


def _assess_draft_result(
    run_path: Path,
    job: TranslationJob,
    options: WorkerOptions,
    session: _RecoverySession,
    *,
    project_root: Path,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
) -> WorkerJobResult:
    """Reconcile one provider-successful draft that has no acceptance verdict yet."""
    try:
        status = accept_provider_success(run_path, job.job_id, project_root=project_root, now=now)
        return _assessed_result(
            run_path,
            job.job_id,
            status,
            job.successful_attempt,
            options,
            session,
            project_root=project_root,
            provider_factory=provider_factory,
            now=now,
        )
    except _JOB_FAILURES as exc:
        return WorkerJobResult(job.job_id, "error", job.successful_attempt, error=_diagnostic(exc))


def _claimed_job_result(
    run_path: Path,
    claim: ClaimResult,
    options: WorkerOptions,
    session: _RecoverySession,
    *,
    project_root: Path,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
) -> WorkerJobResult:
    """Carry one claim the worker holds through execution, acceptance, and failure policy."""
    job = claim.job
    try:
        execution = _execute_claim(
            run_path,
            claim.claim_token,
            job,
            options,
            project_root=project_root,
            provider_factory=provider_factory,
            now=now,
        )
        if execution.attempt_status is TranslationAttemptStatus.SUCCEEDED:
            status = accept_provider_success(
                run_path, job.job_id, project_root=project_root, now=now
            )
            return _assessed_result(
                run_path,
                job.job_id,
                status,
                job.attempt_count,
                options,
                session,
                project_root=project_root,
                provider_factory=provider_factory,
                now=now,
            )
        if execution.attempt_status is TranslationAttemptStatus.FAILED:
            return _failed_execution_result(
                run_path,
                job,
                options,
                session,
                project_root=project_root,
                provider_factory=provider_factory,
                now=now,
            )
        return WorkerJobResult(job.job_id, "permanent_failure", job.attempt_count)
    except _JOB_FAILURES as exc:
        return WorkerJobResult(
            job.job_id,
            "error",
            job.attempt_count,
            error=_recorded_claim_error(
                run_path, job, options, exc, project_root=project_root, now=now
            ),
        )


def _is_queue_drained(exc: QueueError) -> bool:
    """Return whether a claim failed because nothing is claimable rather than because of a fault."""
    message = str(exc)
    return "no eligible translation job" in message or "does not permit claims" in message


def process_work_item(
    run_path: Path,
    item: WorkItem,
    options: WorkerOptions,
    session: _RecoverySession,
    *,
    project_root: Path,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
) -> WorkerJobResult | None:
    """Process one coordinator-dispatched work item through the ordinary per-job path.

    The item names *what kind* of work to do; the queue still decides whether it may be done. A
    claim is the queue's own atomic next-eligible claim, so two slots asking at once get two
    different jobs or one gets nothing. An assessment or a remediation names its job, and is
    dropped — ``None`` — when the job has since left the class it was selected for. ``None`` for
    a claim means the queue has no more claimable work for this selection.
    """
    if item.kind == "claim":
        try:
            claim = claim_job(
                run_path,
                project_root=project_root,
                worker_id=options.worker_id,
                lease_seconds=options.lease_seconds,
                chapter_id=options.chapter_id,
                now=now,
            )
        except QueueError as exc:
            if _is_queue_drained(exc):
                return None
            raise
        return _claimed_job_result(
            run_path,
            claim,
            options,
            session,
            project_root=project_root,
            provider_factory=provider_factory,
            now=now,
        )
    state = load_run_summary(run_path, project_root=project_root)
    job = next((candidate for candidate in state.jobs if candidate.job_id == item.job_id), None)
    if job is None:
        raise TranslationWorkerError(f"dispatched translation job is not part of this run: {item}")
    if item.kind == "assess":
        if not _is_unassessed_draft(job):
            return None
        return _assess_draft_result(
            state.path,
            job,
            options,
            session,
            project_root=project_root,
            provider_factory=provider_factory,
            now=now,
        )
    if not _is_repair_selection(job, options):
        return None
    return _remediation_result(
        state.path,
        job,
        options,
        session,
        project_root=project_root,
        provider_factory=provider_factory,
        now=now,
    )


def build_work_plan(state: QueueState, options: WorkerOptions) -> tuple[WorkItem, ...]:
    """Plan a pass over the queue as the sequential worker would order it, bounded by max_jobs.

    Unassessed drafts come first because reconciling them costs no provider call; then one
    claim per currently claimable job, each of which the queue resolves to the next eligible job
    in canonical order at claim time; then QA-rejected drafts automatic repair may act on. The
    provider-recovery classes are absent by construction: a multi-slot pass refuses those
    options, because recovering a job re-runs global queue recovery, which cannot tell a live
    concurrent execution from a crashed one.
    """
    ordered = sorted(state.jobs, key=claim_priority_key)
    selected = [job for job in ordered if _matches_selection(job, options)]
    plan: list[WorkItem] = [
        WorkItem("assess", job.job_id) for job in selected if _is_unassessed_draft(job)
    ]
    plan.extend(WorkItem("claim", None) for job in selected if is_claimable(job))
    plan.extend(
        WorkItem("remediate", job.job_id) for job in selected if _is_repair_selection(job, options)
    )
    return tuple(plan[: options.max_jobs])


def _preflight_selected_provider(
    provider: ProviderName, preflight: Callable[[ProviderName], None] | None
) -> None:
    """Refuse an unusable provider before recovery, claiming, or any other queue mutation."""
    try:
        if preflight is None:
            preflight_provider(provider)
        else:
            preflight(provider)
    except ProviderExecutionError as exc:
        raise TranslationWorkerError(
            f"provider preflight failed for {provider.value}: {exc.classification.message}"
        ) from exc


def _validate_options(options: WorkerOptions) -> None:
    if options.max_jobs < 1:
        raise TranslationWorkerError("maximum jobs must be positive")
    if options.workers < 1:
        raise TranslationWorkerError("workers must be positive")
    if not options.worker_id.strip():
        raise TranslationWorkerError("worker ID must not be empty")
    if options.job_id is not None and options.chapter_id is not None:
        raise TranslationWorkerError(
            "a targeted job ID already identifies exactly one job; do not combine it with a chapter"
        )
    if options.include_qa_rejected and not options.auto_repair:
        raise TranslationWorkerError(
            "including QA-rejected jobs only selects work automatic repair may act on;"
            " enable --auto-repair as well"
        )
    if options.max_repair_attempts < 0:
        raise TranslationWorkerError("maximum repair attempts must not be negative")
    if options.include_failed and not options.auto_recover_provider_failures:
        raise TranslationWorkerError(
            "including failed jobs only selects work provider recovery may act on;"
            " enable --auto-recover-provider-failures as well"
        )
    if options.max_provider_recovery_attempts < 0:
        raise TranslationWorkerError("maximum provider recovery attempts must not be negative")
    if options.workers > 1 and options.auto_recover_provider_failures:
        raise TranslationWorkerError(
            "provider recovery re-runs global queue recovery for every job it re-enters, which"
            " cannot tell a live concurrent execution from a crashed one; run"
            " --auto-recover-provider-failures with --workers 1"
        )


def _ensure_picklable(
    options: WorkerOptions,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
) -> None:
    """Refuse, before any claim, an injection a spawned execution slot could not reconstruct."""
    try:
        pickle.dumps((options, provider_factory, now))
    except (pickle.PicklingError, AttributeError, TypeError) as exc:
        raise TranslationWorkerError(
            "multi-worker execution needs picklable options, provider factory, and clock:"
            f" {type(exc).__name__}: {exc}"
        ) from exc


def _effective_workers(options: WorkerOptions, planned: int) -> int:
    """Return how many execution slots a pass will actually use.

    One explicitly targeted job is one job, so it never gets a pool; and a pool never has more
    slots than there is work for, which also keeps a nearly drained queue from paying for idle
    processes.
    """
    if options.job_id is not None:
        return 1
    return max(1, min(options.workers, planned))


def aggregate_results(
    run_id: str,
    worker_id: str,
    results: tuple[WorkerJobResult, ...],
    *,
    workers: int = 1,
    worker_failures: int = 0,
) -> WorkerResult:
    """Sum the per-job results of one pass, whichever mode produced them."""
    return WorkerResult(
        run_id,
        worker_id,
        len(results),
        sum(item.outcome == "accepted" for item in results),
        sum(item.outcome == "qa_rejected" for item in results),
        sum(item.outcome == "provider_failed" for item in results),
        sum(item.outcome == "retry_pending" for item in results),
        sum(item.outcome == "permanent_failure" for item in results),
        sum(item.outcome == "error" for item in results),
        False,
        results,
        # A job is one job however many attempts it took: `accepted` already counted it once, and
        # these two only describe how much repair work that took.
        sum(item.outcome == "accepted" and item.repair_attempts > 0 for item in results),
        sum(item.repair_attempts for item in results),
        # Recovery is counted the same way, and counted as recovered once the provider answered:
        # what QA then made of that answer is the acceptance counters' business, not recovery's.
        sum(
            item.provider_recovery_attempts > 0 and item.outcome in {"accepted", "qa_rejected"}
            for item in results
        ),
        sum(item.provider_recovery_attempts for item in results),
        # Repair *decisions* are counted apart from repair executions on purpose. A repair whose
        # provider call was recovered is one decision and two executions, and collapsing them
        # would report a QA judgement that was never made.
        sum(item.repair_decisions for item in results),
        sum(
            item.repair_recovery_attempts > 0 and item.outcome in {"accepted", "qa_rejected"}
            for item in results
        ),
        sum(item.repair_recovery_attempts for item in results),
        workers,
        worker_failures,
    )


def run_translation_worker(
    run_id_or_path: str | Path,
    *,
    project_root: Path,
    options: WorkerOptions,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None = None,
    preflight: Callable[[ProviderName], None] | None = None,
    recovery_preflight: Callable[[ProviderName], ProviderPreflightReport] | None = None,
    now: Callable[[], datetime] | None = None,
    stop_requested: Callable[[], bool] | None = None,
    on_progress: Callable[[WorkerProgress], None] | None = None,
) -> WorkerResult:
    """Run one worker pass over a translation run.

    Validation, the provider preflight, and queue recovery happen exactly once here, before any
    claim, in both modes. With ``options.workers`` above one and work for more than one slot, the
    pass is handed to the process coordinator; otherwise it runs sequentially in this process.
    ``stop_requested`` and ``on_progress`` are coordinator-only: the first is polled between
    dispatches (the CLI wires it to SIGINT/SIGTERM), the second reports each finished job.
    """
    _validate_options(options)
    session = _RecoverySession(options.provider, recovery_preflight)
    project_root = project_root.resolve()
    initial = load_run_summary(run_id_or_path, project_root=project_root)
    _validate_existing_acceptance(initial.path, project_root)
    _validate_selection(initial.jobs, options)
    _ensure_model_policy(initial, options, project_root)
    if options.dry_run:
        planned = _selectable_jobs(initial, options)
        return WorkerResult(
            initial.run.run_id,
            options.worker_id,
            len(planned),
            0,
            0,
            0,
            0,
            0,
            0,
            True,
            tuple(
                _planned_job_result(
                    initial.path, job, options, session, project_root=project_root, now=now
                )
                for job in planned
            ),
            workers=_effective_workers(options, len(planned)),
        )
    pooled = options.workers > 1 and options.job_id is None
    if pooled:
        _ensure_picklable(options, provider_factory, now)
    _preflight_selected_provider(options.provider, preflight)
    recovery = recover_run(initial.path, project_root=project_root, now=now)
    if recovery.blockers:
        raise TranslationWorkerError("translation run recovery has blockers")
    if pooled:
        recovered = load_run_summary(initial.path, project_root=project_root)
        plan = build_work_plan(recovered, options)
        workers = _effective_workers(options, len(plan))
        if workers > 1:
            # Imported here because the pool is built on this module's per-item path and
            # imports it; the dependency runs one way at module level and the other way only
            # at this call.
            from book_translator.translation_worker_pool import run_worker_pool

            return run_worker_pool(
                recovered,
                plan,
                options,
                workers=workers,
                project_root=project_root,
                provider_factory=provider_factory,
                now=now,
                stop_requested=stop_requested,
                on_progress=on_progress,
            )
    return _run_sequential(
        initial,
        options,
        session,
        project_root=project_root,
        provider_factory=provider_factory,
        now=now,
    )


def _run_sequential(
    initial: QueueState,
    options: WorkerOptions,
    session: _RecoverySession,
    *,
    project_root: Path,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
) -> WorkerResult:
    results: list[WorkerJobResult] = []
    seen: set[str] = set()
    while len(results) < options.max_jobs:
        if options.job_id is not None and options.job_id in seen:
            break
        state = load_run_summary(initial.path, project_root=project_root)
        unassessed = next(
            (
                job
                for job in state.jobs
                if _is_unassessed_draft(job)
                and job.job_id not in seen
                and _matches_selection(job, options)
            ),
            None,
        )
        if unassessed is not None:
            seen.add(unassessed.job_id)
            results.append(
                _assess_draft_result(
                    state.path,
                    unassessed,
                    options,
                    session,
                    project_root=project_root,
                    provider_factory=provider_factory,
                    now=now,
                )
            )
            if results[-1].outcome == "error" and options.stop_on_error:
                break
            continue
        remediation = _next_remediation(state, options, seen)
        if options.job_id is not None:
            target = next((job for job in state.jobs if job.job_id == options.job_id), None)
            if target is None:
                raise TranslationWorkerError(
                    f"targeted translation job is not part of this run: {options.job_id}"
                )
            if not is_claimable(target):
                if remediation is not None and remediation.job_id == target.job_id:
                    if not _append_remediation_result(
                        results,
                        state.path,
                        remediation,
                        options,
                        session,
                        seen,
                        project_root=project_root,
                        provider_factory=provider_factory,
                        now=now,
                    ):
                        break
                    continue
                raise _targeted_selection_error(target, options)
        try:
            claim = claim_job(
                state.path,
                project_root=project_root,
                worker_id=options.worker_id,
                lease_seconds=options.lease_seconds,
                chapter_id=options.chapter_id,
                job_id=options.job_id,
                now=now,
            )
        except QueueError as exc:
            if options.job_id is not None:
                raise TranslationWorkerError(
                    f"targeted translation job {options.job_id} could not be claimed: {exc}"
                ) from exc
            if _is_queue_drained(exc):
                if remediation is None:
                    break
                if not _append_remediation_result(
                    results,
                    state.path,
                    remediation,
                    options,
                    session,
                    seen,
                    project_root=project_root,
                    provider_factory=provider_factory,
                    now=now,
                ):
                    break
                continue
            raise
        seen.add(claim.job.job_id)
        results.append(
            _claimed_job_result(
                state.path,
                claim,
                options,
                session,
                project_root=project_root,
                provider_factory=provider_factory,
                now=now,
            )
        )
        if results[-1].outcome == "error" and options.stop_on_error:
            break
    return aggregate_results(initial.run.run_id, options.worker_id, tuple(results))
