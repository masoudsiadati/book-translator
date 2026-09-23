"""Claim-safe orchestration and atomic provider-attempt publication."""

import hashlib
import hmac
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from book_translator.config import BookConfig, load_book_config
from book_translator.models import (
    SCHEMA_VERSION,
    AttemptReservation,
    ParagraphAlignmentFinding,
    PromptPackage,
    ProviderErrorCategory,
    ProviderName,
    QueueEvent,
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationAttemptValidationReport,
    TranslationExecutionKind,
    TranslationJob,
    TranslationJobStatus,
)
from book_translator.paths import ProjectPaths
from book_translator.prompt_packaging import validate_prompt_package
from book_translator.provider_execution.artifacts import (
    canonical_json,
    load_yaml,
    private_write,
    project_relative,
    safe_json_data,
    sanitized_request_payload,
    serialize_yaml,
    sha256_bytes,
)
from book_translator.provider_execution.errors import (
    ErrorClassification,
    ExecutorError,
    MarkerContractDiagnostics,
    ProviderExecutionError,
)
from book_translator.provider_execution.protocol import (
    ProviderRequest,
    ProviderResult,
    TranslationProvider,
)
from book_translator.provider_execution.providers import (
    ClaudeCodeTranslationProvider,
    CodexCliTranslationProvider,
    FakeTranslationProvider,
    OpenAIResponsesProvider,
)
from book_translator.provider_execution.result_validation import (
    candidate_output,
    validate_candidate,
)
from book_translator.translation_queue.policy import can_transition_translation_job
from book_translator.translation_queue.workflow import (
    QueueState,
    _append_event,
    _event,
    _load_queue,
    _locked_queue,
    _persist_job_and_run,
    _resolve_run_path,
    load_settled_queue,
)
from book_translator.translation_snapshot.workflow import SnapshotValidationResult

PROVIDER_EXECUTOR_VERSION = "1.0.0"
LEASE_SAFETY_MARGIN_SECONDS = 60
MAX_EXECUTION_LEASE_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    project_root: Path
    run_path: Path
    job: TranslationJob
    package: PromptPackage
    request: ProviderRequest
    request_payload: dict[str, object]
    request_bytes: bytes
    request_sha256: str
    attempt_directory: Path
    reservation_path: Path
    store_raw_response: bool
    execution_kind: TranslationExecutionKind = TranslationExecutionKind.TRANSLATION
    repaired_from_attempt: int | None = None
    system_message_bytes: bytes | None = None
    user_message_bytes: bytes | None = None
    repaired_from_carry_forward: int | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    run_id: str
    job_id: str
    attempt_number: int
    attempt_status: TranslationAttemptStatus | None
    request_id: str
    dry_run: bool
    provider_contacted: bool


@dataclass(frozen=True, slots=True)
class ListedAttempt:
    attempt: TranslationAttempt
    validation: str


def _attempt_id(job_id: str, number: int) -> str:
    return f"{job_id}__attempt-{number:03d}"


def _job_from_state(state: QueueState, job_id: str) -> TranslationJob:
    job = next((item for item in state.jobs if item.job_id == job_id), None)
    if job is None:
        raise ExecutorError(f"translation job not found: {job_id}")
    return job


def _validate_claim(job: TranslationJob, worker_id: str, claim_token: str, now: datetime) -> None:
    if job.status is not TranslationJobStatus.CLAIMED:
        raise ExecutorError("translation job must be claimed before execution")
    if not worker_id.strip() or job.claimed_by != worker_id:
        raise ExecutorError("worker does not own the translation claim")
    if (
        not claim_token
        or not job.claim_token
        or not hmac.compare_digest(job.claim_token, claim_token)
    ):
        raise ExecutorError("translation claim token is invalid")
    if job.lease_expires_at is None or job.lease_expires_at <= now:
        raise ExecutorError("translation claim lease has expired")
    if job.attempt_count < 1 or job.provider_attempt_count > job.max_attempts:
        raise ExecutorError("translation claim attempt number is invalid")


def ensure_provider_model_allowed(
    config: BookConfig, provider: ProviderName, model: str | None
) -> None:
    """Refuse a provider/model pair the book's configuration does not explicitly allow.

    The allow-list is exact: a requested model must be listed under its provider, and a provider
    that is not listed at all is refused even without a model. An empty list is not a wildcard —
    it admits only ``model=None``, which for the CLI session providers means "keep whatever the
    authenticated session already selects". A model alias such as ``opus`` is compared and then
    forwarded as written; nothing here canonicalizes it.
    """
    configured = config.translation_provider_models.get(provider.value)
    if configured is None:
        raise ExecutorError(
            "provider/model is not allowed by project configuration: "
            f"{provider.value} is not a configured translation provider"
        )
    if model is not None and model not in configured:
        # Parentheses rather than brackets: the message crosses Rich boundaries where a bracketed
        # list would be read as markup and silently dropped from the operator's screen.
        allowed = ", ".join(configured) if configured else "none; only the session default"
        raise ExecutorError(
            "provider/model is not allowed by project configuration: "
            f"{provider.value} model {model!r} is not in the allowed list (allowed: {allowed})"
        )


def _request_identity(
    job: TranslationJob, package: PromptPackage, provider: ProviderName, model: str | None
) -> tuple[str, str]:
    material = canonical_json(
        {
            "executor_version": PROVIDER_EXECUTOR_VERSION,
            "run_id": job.run_id,
            "job_id": job.job_id,
            "attempt_number": job.attempt_count,
            "package_id": package.package_id,
            "package_manifest_sha256": package.package_manifest_sha256,
            "provider": provider.value,
            "model": model,
            "output_contract": package.output_contract_version,
        }
    )
    digest = hashlib.sha256(material).hexdigest()
    return f"preq-{digest[:24]}", digest


def plan_execution(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
    worker_id: str,
    claim_token: str,
    provider: ProviderName,
    model: str | None,
    timeout_seconds: int,
    max_output_tokens: int,
    reasoning_effort: str | None = None,
    store_raw_response: bool = True,
    sdk_retry_count: int = 0,
    execution_kind: TranslationExecutionKind = TranslationExecutionKind.TRANSLATION,
    repaired_from_attempt: int | None = None,
    system_message: str | None = None,
    user_message: str | None = None,
    now: Callable[[], datetime] | None = None,
    repaired_from_carry_forward: int | None = None,
) -> ExecutionPlan:
    if timeout_seconds < 1 or max_output_tokens < 1:
        raise ExecutorError("provider timeout and maximum output tokens must be positive")
    if sdk_retry_count != 0:
        raise ExecutorError("provider SDK retries are disabled in executor version 1")
    project_root = project_root.resolve()
    timestamp = (now or (lambda: datetime.now(UTC)))()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    # Settled rather than plain: this read gates execution on queue validity, and with several
    # execution slots publishing, a plain read outside the lock can catch one mid-publication.
    state = load_settled_queue(run_path, project_root=project_root)
    if not state.validation.valid or not state.snapshot_valid:
        raise ExecutorError("translation run or frozen snapshot is invalid")
    job = _job_from_state(state, job_id)
    _validate_claim(job, worker_id, claim_token, timestamp)
    if execution_kind is TranslationExecutionKind.REPAIR:
        lineage = (repaired_from_attempt is None) != (repaired_from_carry_forward is None)
        if not lineage or system_message is None or user_message is None:
            raise ExecutorError(
                "repair execution requires prior-attempt lineage and repair messages"
            )
    elif any(
        value is not None
        for value in (
            repaired_from_attempt,
            repaired_from_carry_forward,
            system_message,
            user_message,
        )
    ):
        raise ExecutorError("ordinary translation execution cannot carry repair inputs")
    # The book a run belongs to is a property of its frozen lineage, so the configuration that
    # governs its execution (model allow-list, reasoning models) is that book's, never a fixed file.
    config = load_book_config(ProjectPaths(project_root, state.book).book_config)
    ensure_provider_model_allowed(config, provider, model)
    if reasoning_effort is not None and (
        provider is not ProviderName.OPENAI or model not in config.openai_reasoning_models
    ):
        raise ExecutorError("reasoning effort is not enabled for the configured model")
    job_directory = run_path / "jobs" / job.chunk_id
    if any(
        (job_directory / name).exists()
        for name in ("translation.md", "rejected-output.md", "request.json", "response.json")
    ):
        raise ExecutorError("translation or provider artifact exists outside a valid attempt")
    package_report = validate_prompt_package(run_path, job_id, project_root=project_root)
    if not package_report.valid and execution_kind is TranslationExecutionKind.TRANSLATION:
        raise ExecutorError("prompt package is absent, stale, or invalid")
    package_directory = run_path / "jobs" / job.chunk_id / "prompt-package"
    package = load_yaml(package_directory / "package.yaml", PromptPackage)
    if (
        package.job_id != job.job_id
        or package.snapshot_id != job.snapshot_id
        or package.manifest_sha256 != job.manifest_sha256
        or package.chunk_id != job.chunk_id
    ):
        raise ExecutorError("prompt package lineage does not match the queue job")
    attempts = run_path / "jobs" / job.chunk_id / "attempts"
    attempt_directory = attempts / f"{job.attempt_count:03d}"
    reservation_path = attempts / f".{job.attempt_count:03d}.reservation.yaml"
    if attempt_directory.exists() or reservation_path.exists():
        raise ExecutorError("attempt number already has execution evidence")
    if attempts.is_dir():
        for metadata_path in attempts.glob("*/attempt.yaml"):
            existing = load_yaml(metadata_path, TranslationAttempt)
            if execution_kind is TranslationExecutionKind.TRANSLATION and existing.status in {
                TranslationAttemptStatus.SUCCEEDED,
                TranslationAttemptStatus.AMBIGUOUS,
            }:
                raise ExecutorError("successful or ambiguous attempt blocks provider execution")
    system_path = project_root / package.system_message.file
    user_path = project_root / package.user_message.file
    system_bytes = (
        system_message.encode("utf-8") if system_message is not None else system_path.read_bytes()
    )
    user_bytes = (
        user_message.encode("utf-8") if user_message is not None else user_path.read_bytes()
    )
    if system_message is None and sha256_bytes(system_bytes) != package.system_message.sha256:
        raise ExecutorError("prompt package system message hash mismatch")
    if user_message is None and sha256_bytes(user_bytes) != package.user_message.sha256:
        raise ExecutorError("prompt package user message hash mismatch")
    request_id, idempotency_key = _request_identity(job, package, provider, model)
    metadata = {
        "run_id": job.run_id,
        "job_id": job.job_id,
        "attempt": str(job.attempt_count),
        "package_id": package.package_id,
        "provider": provider.value,
        "execution_kind": execution_kind.value,
    }
    request = ProviderRequest(
        request_id,
        idempotency_key,
        job.run_id,
        job.job_id,
        job.attempt_count,
        system_bytes.decode("utf-8"),
        user_bytes.decode("utf-8"),
        job.source_language,
        job.target_language,
        package.output_contract_version,
        model,
        timeout_seconds,
        max_output_tokens,
        reasoning_effort,
        metadata,
        sdk_retry_count,
    )
    payload = sanitized_request_payload(
        request_id=request_id,
        idempotency_key=idempotency_key,
        provider=provider.value,
        model=model,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
        sdk_retry_count=sdk_retry_count,
        reasoning_effort=reasoning_effort,
        system_file=(
            project_relative(attempt_directory / "repair-system.md", project_root)
            if system_message is not None
            else package.system_message.file
        ),
        system_sha256=sha256_bytes(system_bytes),
        user_file=(
            project_relative(attempt_directory / "repair-user.md", project_root)
            if user_message is not None
            else package.user_message.file
        ),
        user_sha256=sha256_bytes(user_bytes),
        output_contract=package.output_contract_version,
        metadata=metadata,
    )
    request_bytes = canonical_json(payload)
    return ExecutionPlan(
        project_root,
        run_path,
        job,
        package,
        request,
        payload,
        request_bytes,
        sha256_bytes(request_bytes),
        attempt_directory,
        reservation_path,
        store_raw_response,
        execution_kind,
        repaired_from_attempt,
        system_bytes if system_message is not None else None,
        user_bytes if user_message is not None else None,
        repaired_from_carry_forward,
    )


def _provider_for(plan: ExecutionPlan, scenario: str = "success") -> TranslationProvider:
    if plan.request.metadata["provider"] == ProviderName.FAKE.value:
        return FakeTranslationProvider(scenario)
    if plan.request.metadata["provider"] == ProviderName.CODEX_CLI.value:
        return CodexCliTranslationProvider()
    if plan.request.metadata["provider"] == ProviderName.CLAUDE_CODE.value:
        return ClaudeCodeTranslationProvider()
    return OpenAIResponsesProvider(
        max_retries=plan.request.sdk_retry_count,
        timeout_seconds=plan.request.timeout_seconds,
    )


def _replace_job(state: QueueState, changed: TranslationJob) -> tuple[TranslationJob, ...]:
    return tuple(changed if job.job_id == changed.job_id else job for job in state.jobs)


def _reserve_and_start(
    plan: ExecutionPlan, worker_id: str, claim_token: str, now: datetime
) -> None:
    with _locked_queue(plan.run_path, plan.project_root) as state:
        job = _job_from_state(state, plan.job.job_id)
        _validate_claim(job, worker_id, claim_token, now)
        if plan.attempt_directory.exists() or plan.reservation_path.exists():
            raise ExecutorError("attempt execution evidence appeared after preflight")
        required = plan.request.timeout_seconds + LEASE_SAFETY_MARGIN_SECONDS
        if required > MAX_EXECUTION_LEASE_SECONDS:
            raise ExecutorError("configured timeout exceeds the maximum execution lease")
        lease = max(job.lease_expires_at or now, now + timedelta(seconds=required))
        events = job.recent_events
        if lease != job.lease_expires_at:
            events = _append_event(
                job, _event("execution_lease_extended", now, worker_id=worker_id)
            )
        if not can_transition_translation_job(job.status, TranslationJobStatus.RUNNING):
            raise ExecutorError("canonical claimed-to-running transition is unavailable")
        running = job.model_copy(
            update={
                "status": TranslationJobStatus.RUNNING,
                "lease_expires_at": lease,
                "updated_at": now,
                "last_attempt": job.attempt_count,
                "recent_events": [
                    *events,
                    QueueEvent(
                        event_type="execution_started", occurred_at=now, worker_id=worker_id
                    ),
                ][-20:],
            }
        )
        plan.reservation_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(plan.reservation_path.parent, 0o700)
        reservation = AttemptReservation(
            schema_version=SCHEMA_VERSION,
            attempt_id=_attempt_id(job.job_id, job.attempt_count),
            attempt_number=job.attempt_count,
            run_id=job.run_id,
            job_id=job.job_id,
            prompt_package_id=plan.package.package_id,
            provider=ProviderName(plan.request.metadata["provider"]),
            model=plan.request.model,
            request_sha256=plan.request_sha256,
            status=TranslationAttemptStatus.PREPARED,
            created_at=now,
            updated_at=now,
        )
        private_write(plan.reservation_path, serialize_yaml(reservation).encode())
        _persist_job_and_run(state, _replace_job(state, running), running, now)


def _mark_requesting(plan: ExecutionPlan, now: datetime) -> None:
    reservation = load_yaml(plan.reservation_path, AttemptReservation)
    requesting = reservation.model_copy(
        update={"status": TranslationAttemptStatus.REQUESTING, "updated_at": now}
    )
    private_write(plan.reservation_path, serialize_yaml(requesting).encode(), overwrite=True)


def _build_attempt(
    plan: ExecutionPlan,
    *,
    provider: TranslationProvider,
    started_at: datetime,
    completed_at: datetime,
    latency_ms: int,
    result: ProviderResult | None,
    translation: bytes | None,
    rejected: bytes | None,
    diagnostics: MarkerContractDiagnostics | None,
    classification: ErrorClassification | None,
    warnings: tuple[str, ...],
) -> tuple[TranslationAttempt, TranslationAttemptValidationReport, bytes | None]:
    status = TranslationAttemptStatus.SUCCEEDED
    if classification is not None:
        status = (
            TranslationAttemptStatus.AMBIGUOUS
            if classification.ambiguous
            else TranslationAttemptStatus.FAILED
        )
    response_bytes = None
    if result is not None and plan.store_raw_response:
        response_bytes = canonical_json(safe_json_data(result.sanitized_response))
    request_path = plan.attempt_directory / "request.json"
    response_path = plan.attempt_directory / "response.json" if response_bytes is not None else None
    translation_path = (
        plan.attempt_directory / "translation.md" if translation is not None else None
    )
    rejected_path = plan.attempt_directory / "rejected-output.md" if rejected is not None else None
    attempt = TranslationAttempt(
        schema_version=SCHEMA_VERSION,
        attempt_id=_attempt_id(plan.job.job_id, plan.job.attempt_count),
        attempt_number=plan.job.attempt_count,
        execution_kind=plan.execution_kind,
        repaired_from_attempt=plan.repaired_from_attempt,
        repaired_from_carry_forward=plan.repaired_from_carry_forward,
        run_id=plan.job.run_id,
        job_id=plan.job.job_id,
        snapshot_id=plan.job.snapshot_id,
        prompt_package_id=plan.package.package_id,
        provider=ProviderName(provider.provider_name),
        provider_adapter_version=provider.adapter_version,
        provider_executor_version=PROVIDER_EXECUTOR_VERSION,
        model_requested=plan.request.model,
        model_returned=result.model if result else None,
        provider_response_id=result.response_id if result else None,
        request_file=project_relative(request_path, plan.project_root),
        request_sha256=plan.request_sha256,
        response_file=project_relative(response_path, plan.project_root) if response_path else None,
        response_sha256=sha256_bytes(response_bytes) if response_bytes else None,
        raw_response_stored=response_bytes is not None,
        translation_file=(
            project_relative(translation_path, plan.project_root) if translation_path else None
        ),
        translation_sha256=sha256_bytes(translation) if translation else None,
        output_contract=plan.package.output_contract_version,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        latency_milliseconds=latency_ms,
        input_tokens=result.usage.input_tokens if result else None,
        output_tokens=result.usage.output_tokens if result else None,
        total_tokens=result.usage.total_tokens if result else None,
        estimated_input_tokens=plan.package.estimated_input_tokens,
        max_output_tokens=plan.request.max_output_tokens,
        sdk_retry_count=plan.request.sdk_retry_count,
        stop_reason=result.incomplete_reason if result else None,
        error_category=classification.category if classification else None,
        retryable=classification.retryable if classification else False,
        ambiguous=classification.ambiguous if classification else False,
        sanitized_error_message=classification.message if classification else None,
        warnings=list(warnings),
        rejected_output_file=(
            project_relative(rejected_path, plan.project_root) if rejected_path else None
        ),
        rejected_output_sha256=sha256_bytes(rejected) if rejected is not None else None,
        marker_findings=[
            ParagraphAlignmentFinding(
                code=finding.code,
                severity="error" if finding.severity == "error" else "warning",
                source_paragraph_index=finding.source_paragraph_index,
                expected_marker=finding.expected_marker,
                observed_marker=finding.observed_marker,
                message=finding.message,
            )
            for finding in (diagnostics.findings if diagnostics else ())
        ],
        expected_marker_count=diagnostics.expected_marker_count if diagnostics else None,
        observed_marker_count=diagnostics.observed_marker_count if diagnostics else None,
    )
    report = TranslationAttemptValidationReport(
        schema_version=SCHEMA_VERSION,
        attempt_id=attempt.attempt_id,
        valid=True,
        job_lineage_valid=True,
        package_lineage_valid=True,
        request_hash_valid=True,
        response_hash_valid=True,
        translation_hash_valid=True,
        status_consistent=True,
        permissions_valid=True,
        rejected_output_hash_valid=True,
        checks_performed=[
            "job_and_package_lineage",
            "request_hash_and_privacy",
            "response_hash",
            "translation_hash_and_structure",
            "rejected_output_hash_and_privacy",
            "attempt_status",
            "artifact_status_consistency",
            "private_permissions",
        ],
        warnings=list(warnings),
        errors=[],
        created_at=completed_at,
    )
    return attempt, report, response_bytes


def _stage_attempt(
    plan: ExecutionPlan,
    attempt: TranslationAttempt,
    report: TranslationAttemptValidationReport,
    response_bytes: bytes | None,
    translation: bytes | None,
    rejected: bytes | None,
) -> Path:
    parent = plan.attempt_directory.parent
    staging = Path(tempfile.mkdtemp(prefix=f".{plan.job.attempt_count:03d}.staging.", dir=parent))
    os.chmod(staging, 0o700)
    try:
        private_write(staging / "request.json", plan.request_bytes)
        if plan.system_message_bytes is not None:
            private_write(staging / "repair-system.md", plan.system_message_bytes)
        if plan.user_message_bytes is not None:
            private_write(staging / "repair-user.md", plan.user_message_bytes)
        if response_bytes is not None:
            private_write(staging / "response.json", response_bytes)
        if translation is not None:
            private_write(staging / "translation.md", translation)
        if rejected is not None:
            private_write(staging / "rejected-output.md", rejected)
        private_write(staging / "validation-report.yaml", serialize_yaml(report).encode())
        private_write(staging / "attempt.yaml", serialize_yaml(attempt).encode())
        return staging
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _finalize(
    plan: ExecutionPlan,
    staging: Path,
    attempt: TranslationAttempt,
    worker_id: str,
    claim_token: str,
    now: datetime,
) -> None:
    with _locked_queue(plan.run_path, plan.project_root) as state:
        job = _job_from_state(state, plan.job.job_id)
        if job.status is not TranslationJobStatus.RUNNING:
            raise ExecutorError("job state changed during provider execution")
        if (
            job.claimed_by != worker_id
            or not job.claim_token
            or not hmac.compare_digest(job.claim_token, claim_token)
        ):
            raise ExecutorError("execution ownership changed during provider call")
        final_status = TranslationJobStatus.SUCCEEDED
        if attempt.status is TranslationAttemptStatus.FAILED:
            final_status = TranslationJobStatus.FAILED
        elif attempt.status is TranslationAttemptStatus.AMBIGUOUS:
            final_status = TranslationJobStatus.BLOCKED
        elif job.lease_expires_at is None or job.lease_expires_at <= now:
            raise ExecutorError("execution lease expired before final publication")
        if not can_transition_translation_job(job.status, final_status):
            raise ExecutorError("canonical provider-result transition is unavailable")
        if plan.attempt_directory.exists():
            raise ExecutorError("attempt directory appeared during provider execution")
        os.rename(staging, plan.attempt_directory)
        updated = job.model_copy(
            update={
                "status": final_status,
                "claimed_by": None,
                "claim_token": None,
                "claimed_at": None,
                "lease_expires_at": None,
                "provider": attempt.provider.value,
                "model": attempt.model_returned or attempt.model_requested,
                "last_attempt": attempt.attempt_number,
                "successful_attempt": (
                    attempt.attempt_number
                    if final_status is TranslationJobStatus.SUCCEEDED
                    else None
                ),
                "acceptance_status": (
                    None
                    if final_status is TranslationJobStatus.SUCCEEDED
                    and plan.execution_kind is TranslationExecutionKind.REPAIR
                    else job.acceptance_status
                ),
                "accepted_attempt": (
                    None
                    if final_status is TranslationJobStatus.SUCCEEDED
                    and plan.execution_kind is TranslationExecutionKind.REPAIR
                    else job.accepted_attempt
                ),
                "acceptance_file": (
                    None
                    if final_status is TranslationJobStatus.SUCCEEDED
                    and plan.execution_kind is TranslationExecutionKind.REPAIR
                    else job.acceptance_file
                ),
                "acceptance_sha256": (
                    None
                    if final_status is TranslationJobStatus.SUCCEEDED
                    and plan.execution_kind is TranslationExecutionKind.REPAIR
                    else job.acceptance_sha256
                ),
                # A delivered repair replaces the draft every derived verdict was about, so the
                # origin and the structural-repair pointer are dropped with the verdict itself;
                # leaving either behind would let a superseded record answer for a new draft.
                "acceptance_origin": (
                    None
                    if final_status is TranslationJobStatus.SUCCEEDED
                    and plan.execution_kind is TranslationExecutionKind.REPAIR
                    else job.acceptance_origin
                ),
                "structural_repair_count": (
                    0
                    if final_status is TranslationJobStatus.SUCCEEDED
                    and plan.execution_kind is TranslationExecutionKind.REPAIR
                    else job.structural_repair_count
                ),
                "latest_structural_repair_file": (
                    None
                    if final_status is TranslationJobStatus.SUCCEEDED
                    and plan.execution_kind is TranslationExecutionKind.REPAIR
                    else job.latest_structural_repair_file
                ),
                "latest_structural_repair_sha256": (
                    None
                    if final_status is TranslationJobStatus.SUCCEEDED
                    and plan.execution_kind is TranslationExecutionKind.REPAIR
                    else job.latest_structural_repair_sha256
                ),
                # The same holds for a carried-forward draft: once a repair of it is delivered,
                # the carry-forward no longer defines this job's draft. Its immutable record stays
                # on disk and the new attempt names it through ``repaired_from_carry_forward``.
                "carry_forward_count": (
                    0
                    if final_status is TranslationJobStatus.SUCCEEDED
                    and plan.execution_kind is TranslationExecutionKind.REPAIR
                    else job.carry_forward_count
                ),
                "latest_carry_forward_file": (
                    None
                    if final_status is TranslationJobStatus.SUCCEEDED
                    and plan.execution_kind is TranslationExecutionKind.REPAIR
                    else job.latest_carry_forward_file
                ),
                "latest_carry_forward_sha256": (
                    None
                    if final_status is TranslationJobStatus.SUCCEEDED
                    and plan.execution_kind is TranslationExecutionKind.REPAIR
                    else job.latest_carry_forward_sha256
                ),
                # A re-evaluation judged the draft this repair replaces, so it stops defining the
                # verdict too. Its record stays on disk and the count keeps numbering that history.
                "latest_reevaluation_file": (
                    None
                    if final_status is TranslationJobStatus.SUCCEEDED
                    and plan.execution_kind is TranslationExecutionKind.REPAIR
                    else job.latest_reevaluation_file
                ),
                "latest_reevaluation_sha256": (
                    None
                    if final_status is TranslationJobStatus.SUCCEEDED
                    and plan.execution_kind is TranslationExecutionKind.REPAIR
                    else job.latest_reevaluation_sha256
                ),
                "last_error_category": (
                    attempt.error_category.value if attempt.error_category else None
                ),
                "last_error_message": attempt.sanitized_error_message,
                "completed_at": now if final_status is TranslationJobStatus.SUCCEEDED else None,
                "updated_at": now,
                "recent_events": _append_event(
                    job,
                    _event(
                        "execution_succeeded"
                        if final_status is TranslationJobStatus.SUCCEEDED
                        else (
                            "execution_ambiguous"
                            if final_status is TranslationJobStatus.BLOCKED
                            else "execution_failed"
                        ),
                        now,
                        worker_id=worker_id,
                    ),
                ),
            }
        )
        _persist_job_and_run(state, _replace_job(state, updated), updated, now)
        plan.reservation_path.unlink(missing_ok=True)


def execute_translation_job(
    plan: ExecutionPlan,
    *,
    worker_id: str,
    claim_token: str,
    provider: TranslationProvider | None = None,
    fake_scenario: str = "success",
    dry_run: bool = False,
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ExecutionResult:
    if dry_run:
        return ExecutionResult(
            plan.job.run_id,
            plan.job.job_id,
            plan.job.attempt_count,
            None,
            plan.request.request_id,
            True,
            False,
        )
    clock = now or (lambda: datetime.now(UTC))
    started_at = clock()
    _reserve_and_start(plan, worker_id, claim_token, started_at)
    _mark_requesting(plan, clock())
    adapter = provider or _provider_for(plan, fake_scenario)
    result: ProviderResult | None = None
    translation: bytes | None = None
    # The provider's single whole answer, held from the moment it is known to exist until
    # structural validation either turns it into the translation or refuses it. A refusal
    # leaves it here, and it is published privately as ``rejected-output.md`` so an expensive
    # answer that broke the paragraph contract can be inspected rather than lost.
    rejected: bytes | None = None
    diagnostics: MarkerContractDiagnostics | None = None
    warnings: tuple[str, ...] = ()
    classification: ErrorClassification | None = None
    timer = monotonic()
    try:
        result = adapter.execute(plan.request)
        candidate = candidate_output(result)
        rejected = candidate.encode("utf-8")
        validated = validate_candidate(
            candidate,
            (plan.project_root / plan.job.chunk_source_file).read_bytes(),
            output_contract=plan.package.output_contract_version,
        )
        translation = validated.content
        warnings = validated.warnings
        rejected = None
    except ProviderExecutionError as exc:
        classification = exc.classification
        diagnostics = exc.marker_diagnostics
        if classification.ambiguous:
            # Only a conclusive refusal keeps its candidate; an ambiguous attempt must publish
            # nothing that could later be mistaken for evidence of what the provider delivered.
            rejected = None
            diagnostics = None
    except Exception as exc:
        # An unexpected failure is ambiguous by definition, and an ambiguous attempt vouches for
        # nothing it may have received.
        rejected = None
        classification = ErrorClassification(
            ProviderErrorCategory.UNKNOWN,
            retryable=False,
            ambiguous=True,
            safe_automatic_retry=False,
            message=f"unexpected provider adapter failure ({type(exc).__name__})",
        )
    completed_at = clock()
    latency = max(0, round((monotonic() - timer) * 1000))
    current = _load_queue(plan.run_path, plan.project_root)
    running = _job_from_state(current, plan.job.job_id)
    if running.lease_expires_at is None or running.lease_expires_at <= completed_at:
        classification = ErrorClassification(
            ProviderErrorCategory.LEASE_INVALID,
            retryable=False,
            ambiguous=True,
            safe_automatic_retry=False,
            message="execution lease expired during provider operation",
        )
        translation = None
        rejected = None
        diagnostics = None
    attempt, report, response_bytes = _build_attempt(
        plan,
        provider=adapter,
        started_at=started_at,
        completed_at=completed_at,
        latency_ms=latency,
        result=result,
        translation=translation,
        rejected=rejected,
        diagnostics=diagnostics,
        classification=classification,
        warnings=warnings,
    )
    staging = _stage_attempt(plan, attempt, report, response_bytes, translation, rejected)
    try:
        _finalize(plan, staging, attempt, worker_id, claim_token, completed_at)
        staging = Path()
    finally:
        if staging != Path() and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return ExecutionResult(
        plan.job.run_id,
        plan.job.job_id,
        plan.job.attempt_count,
        attempt.status,
        plan.request.request_id,
        False,
        True,
    )


def _artifact_status_errors(attempt: TranslationAttempt) -> list[str]:
    """Refuse artifact combinations no honest execution can produce.

    A rejected answer is only ever published by a conclusively *failed* attempt: a succeeded one
    turned its candidate into ``translation.md``, and an ambiguous one vouches for nothing it may
    have received. Marker findings describe a refused answer, so they belong to a failed attempt
    too. The wording avoids the artifact labels other report booleans key on, so a status defect
    is reported as a status defect and nothing else.
    """
    errors: list[str] = []
    has_rejected = attempt.rejected_output_file is not None
    if has_rejected and attempt.status is not TranslationAttemptStatus.FAILED:
        errors.append("attempt status does not permit a rejected-output artifact")
    if has_rejected and attempt.translation_file is not None:
        errors.append("attempt status cannot publish both a draft and a rejected-output artifact")
    if (
        attempt.marker_findings
        or attempt.expected_marker_count is not None
        or attempt.observed_marker_count is not None
    ) and attempt.status is TranslationAttemptStatus.SUCCEEDED:
        errors.append("attempt status does not permit marker-contract findings")
    return errors


def validate_attempt(
    run_id_or_path: str | Path,
    job_id: str,
    attempt_number: int,
    *,
    project_root: Path,
    now: Callable[[], datetime] | None = None,
    snapshot_validation: SnapshotValidationResult | None = None,
) -> TranslationAttemptValidationReport:
    """Validate one persisted attempt against its run.

    ``snapshot_validation`` is an already performed validation of the run's frozen snapshot; a
    batch caller passes it so validating many attempts does not rebuild the lineage each time.
    """
    timestamp = (now or (lambda: datetime.now(UTC)))()
    errors: list[str] = []
    attempt_id = _attempt_id(job_id, attempt_number)
    try:
        state = _load_queue(
            _resolve_run_path(run_id_or_path, project_root),
            project_root.resolve(),
            snapshot_validation=snapshot_validation,
        )
        job = _job_from_state(state, job_id)
        directory = state.path / "jobs" / job.chunk_id / "attempts" / f"{attempt_number:03d}"
        attempt = load_yaml(directory / "attempt.yaml", TranslationAttempt)
        if attempt.attempt_id != attempt_id or attempt.job_id != job.job_id:
            errors.append("attempt identity or job lineage mismatch")
        package = load_yaml(
            state.path / "jobs" / job.chunk_id / "prompt-package/package.yaml", PromptPackage
        )
        if attempt.prompt_package_id != package.package_id:
            errors.append("prompt package lineage mismatch")
        for path_value, expected, label in (
            (attempt.request_file, attempt.request_sha256, "request"),
            (attempt.response_file, attempt.response_sha256, "response"),
            (attempt.translation_file, attempt.translation_sha256, "translation"),
            (attempt.rejected_output_file, attempt.rejected_output_sha256, "rejected output"),
        ):
            if path_value is None:
                if expected is not None:
                    errors.append(f"{label} hash exists without file reference")
                continue
            path = project_root.resolve() / path_value
            if not path.is_file():
                # Reported by label rather than by path: a path would name the run directory,
                # which the per-artifact report booleans key on by substring.
                errors.append(f"{label} file is missing")
                continue
            if sha256_bytes(path.read_bytes()) != expected:
                errors.append(f"{label} file hash mismatch")
            if os.name == "posix" and path.stat().st_mode & 0o077:
                errors.append(f"{label} file permissions are not private")
        if attempt.attempt_number > job.attempt_count:
            errors.append("attempt number exceeds queue attempt count")
        errors.extend(_artifact_status_errors(attempt))
    except (ExecutorError, OSError, ValueError) as exc:
        errors.append(str(exc))
    return TranslationAttemptValidationReport(
        schema_version=SCHEMA_VERSION,
        attempt_id=attempt_id,
        valid=not errors,
        job_lineage_valid=not any("job lineage" in error for error in errors),
        package_lineage_valid=not any("package lineage" in error for error in errors),
        request_hash_valid=not any("request" in error for error in errors),
        response_hash_valid=not any("response" in error for error in errors),
        translation_hash_valid=not any("translation" in error for error in errors),
        status_consistent=not any(
            "attempt number" in error or "attempt status" in error for error in errors
        ),
        permissions_valid=not any("permissions" in error for error in errors),
        # Presence and hash only; a permission defect is reported by ``permissions_valid``.
        rejected_output_hash_valid=not any(
            "rejected output" in error and "permissions" not in error for error in errors
        ),
        checks_performed=[
            "job_and_package_lineage",
            "request_response_translation_hashes",
            "rejected_output_hash_and_privacy",
            "attempt_and_job_status",
            "artifact_status_consistency",
            "private_permissions",
        ],
        warnings=[],
        errors=errors,
        created_at=timestamp,
    )


def list_attempts(
    run_id_or_path: str | Path, job_id: str, *, project_root: Path
) -> tuple[ListedAttempt, ...]:
    state = _load_queue(_resolve_run_path(run_id_or_path, project_root), project_root.resolve())
    job = _job_from_state(state, job_id)
    root = state.path / "jobs" / job.chunk_id / "attempts"
    if not root.is_dir():
        return ()
    listed: list[ListedAttempt] = []
    for directory in sorted(
        path for path in root.iterdir() if path.is_dir() and path.name.isdigit()
    ):
        attempt = load_yaml(directory / "attempt.yaml", TranslationAttempt)
        report = validate_attempt(
            state.path, job_id, attempt.attempt_number, project_root=project_root
        )
        listed.append(ListedAttempt(attempt, "valid" if report.valid else "invalid"))
    return tuple(listed)
