"""Persistent provider-independent translation queue contracts."""

from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.semantic_gate import SemanticGateStatus
from book_translator.models.translation_worker import (
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
)


class TranslationRunStatus(StrEnum):
    CREATED = "created"
    READY = "ready"
    ACTIVE = "active"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    CANCELLED = "cancelled"
    INVALID = "invalid"


class TranslationJobStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY_PENDING = "retry_pending"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class QueueEvent(ContractModel):
    event_type: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    occurred_at: AwareDatetime
    worker_id: str | None = None
    reason: str | None = None


class TranslationJob(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    job_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*__[a-z0-9._-]+$")
    run_id: str = Field(min_length=1, pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    snapshot_id: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chapter_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    chapter_sequence: int = Field(ge=1)
    chunk_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    chunk_sequence: int = Field(ge=1)
    chunk_source_file: Path
    chunk_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_file: Path
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_word_count: int = Field(ge=0)
    context_word_count: int = Field(ge=0)
    prompt_version: str
    glossary_version: str
    style_guide_version: str
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    queue_version: str
    status: TranslationJobStatus
    priority: int = Field(ge=1)
    attempt_count: int = Field(ge=0)
    abandoned_attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(ge=1)
    next_retry_at: AwareDatetime | None = None
    claimed_by: str | None = None
    claim_token: str | None = None
    claimed_at: AwareDatetime | None = None
    lease_expires_at: AwareDatetime | None = None
    last_error_category: str | None = None
    last_error_message: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    provider: str | None = None
    model: str | None = None
    last_attempt: int | None = Field(default=None, ge=1)
    successful_attempt: int | None = Field(default=None, ge=1)
    acceptance_status: TranslationAcceptanceStatus | None = None
    accepted_attempt: int | None = Field(default=None, ge=1)
    acceptance_file: Path | None = None
    acceptance_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    acceptance_origin: TranslationAcceptanceOrigin | None = None
    reevaluation_count: int = Field(default=0, ge=0)
    latest_reevaluation_file: Path | None = None
    latest_reevaluation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reuse_count: int = Field(default=0, ge=0)
    latest_reuse_file: Path | None = None
    latest_reuse_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    carry_forward_count: int = Field(default=0, ge=0)
    latest_carry_forward_file: Path | None = None
    latest_carry_forward_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    structural_repair_count: int = Field(default=0, ge=0)
    latest_structural_repair_file: Path | None = None
    latest_structural_repair_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    #: The semantic gate's own dimension of acceptance. ``None`` means the job was never judged,
    #: which every reader must treat as "not judged" and never as "passed".
    semantic_status: SemanticGateStatus | None = None
    semantic_evidence_count: int = Field(default=0, ge=0)
    latest_semantic_file: Path | None = None
    latest_semantic_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    semantic_revision_count: int = Field(default=0, ge=0)
    latest_semantic_revision_file: Path | None = None
    latest_semantic_revision_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    warnings: list[str] = Field(default_factory=list)
    recent_events: list[QueueEvent] = Field(default_factory=list, max_length=20)

    @property
    def provider_attempt_count(self) -> int:
        """Return the issued attempt slots that still count against the provider budget.

        ``attempt_count`` is the attempt-slot sequence: it advances on every claim so each
        execution gets its own immutable attempt directory.  An explicitly resolved reservation
        that never reached a provider abandons its slot without spending a provider attempt.
        """
        return self.attempt_count - self.abandoned_attempt_count

    @model_validator(mode="after")
    def lifecycle_fields_are_consistent(self) -> "TranslationJob":
        if self.abandoned_attempt_count > self.attempt_count:
            raise ValueError("abandoned_attempt_count must not exceed attempt_count")
        if self.provider_attempt_count > self.max_attempts:
            raise ValueError("provider attempts must not exceed max_attempts")
        claim_values = (
            self.claimed_by,
            self.claim_token,
            self.claimed_at,
            self.lease_expires_at,
        )
        if self.status in {TranslationJobStatus.CLAIMED, TranslationJobStatus.RUNNING}:
            if any(value is None for value in claim_values):
                raise ValueError("claimed and running jobs require complete claim fields")
        elif any(value is not None for value in claim_values):
            raise ValueError("inactive jobs must not retain claim fields")
        if self.status is TranslationJobStatus.RETRY_PENDING:
            if self.next_retry_at is None:
                raise ValueError("retry-pending jobs require next_retry_at")
        elif self.next_retry_at is not None:
            raise ValueError("only retry-pending jobs may have next_retry_at")
        if self.status is TranslationJobStatus.SUCCEEDED and self.completed_at is None:
            raise ValueError("succeeded jobs require completed_at")
        acceptance_values = (self.accepted_attempt, self.acceptance_file, self.acceptance_sha256)
        if self.acceptance_status is None:
            if any(value is not None for value in acceptance_values):
                raise ValueError("unassessed jobs must not reference acceptance evidence")
            if self.acceptance_origin is TranslationAcceptanceOrigin.QA_REEVALUATION:
                raise ValueError("unassessed jobs must not have a re-evaluation origin")
        elif any(value is None for value in acceptance_values):
            raise ValueError("assessed jobs require complete acceptance evidence")
        reevaluation_values = (
            self.latest_reevaluation_file,
            self.latest_reevaluation_sha256,
        )
        if self.reevaluation_count == 0:
            if any(value is not None for value in reevaluation_values):
                raise ValueError(
                    "job without re-evaluations must not reference re-evaluation evidence"
                )
            if self.acceptance_origin is TranslationAcceptanceOrigin.QA_REEVALUATION:
                raise ValueError("re-evaluation acceptance origin requires evidence")
        elif all(value is None for value in reevaluation_values):
            # A delivered provider repair supersedes every re-evaluation of the draft it replaced.
            # The count stays, because it numbers the job's immutable re-evaluation history, but
            # no re-evaluation defines the verdict any more.
            if self.acceptance_origin is TranslationAcceptanceOrigin.QA_REEVALUATION:
                raise ValueError("re-evaluation acceptance origin requires evidence")
        elif any(value is None for value in reevaluation_values):
            raise ValueError("re-evaluated job requires complete latest evidence metadata")
        elif self.acceptance_status is not None and self.acceptance_origin not in {
            TranslationAcceptanceOrigin.QA_REEVALUATION,
            TranslationAcceptanceOrigin.STRUCTURAL_REPAIR,
        }:
            raise ValueError("latest re-evaluation evidence must define effective acceptance")
        if self.acceptance_status is not None and self.status not in {
            TranslationJobStatus.SUCCEEDED,
            TranslationJobStatus.CLAIMED,
            TranslationJobStatus.RUNNING,
            TranslationJobStatus.FAILED,
            TranslationJobStatus.BLOCKED,
        }:
            raise ValueError("acceptance evidence is incompatible with this job state")
        reuse_values = (self.latest_reuse_file, self.latest_reuse_sha256)
        if self.reuse_count == 0 and any(value is not None for value in reuse_values):
            raise ValueError("job without reuse must not reference reuse evidence")
        if self.reuse_count > 0 and any(value is None for value in reuse_values):
            raise ValueError("reused job requires complete reuse evidence metadata")
        carry_forward_values = (
            self.latest_carry_forward_file,
            self.latest_carry_forward_sha256,
        )
        if self.carry_forward_count == 0:
            if any(value is not None for value in carry_forward_values):
                raise ValueError(
                    "job without carry-forward must not reference carry-forward evidence"
                )
            if self.acceptance_origin is TranslationAcceptanceOrigin.HISTORICAL_REEVALUATION:
                raise ValueError("historical re-evaluation acceptance origin requires evidence")
        elif any(value is None for value in carry_forward_values):
            raise ValueError("carried-forward job requires complete carry-forward metadata")
        elif self.acceptance_status is not None and self.acceptance_origin not in {
            TranslationAcceptanceOrigin.HISTORICAL_REEVALUATION,
            TranslationAcceptanceOrigin.STRUCTURAL_REPAIR,
            # A carried draft may be re-judged by current QA; the re-evaluation names the
            # carry-forward acceptance it re-judged, so the carry-forward still roots the lineage.
            *((TranslationAcceptanceOrigin.QA_REEVALUATION,) if self.reevaluation_count else ()),
        }:
            raise ValueError("carry-forward evidence must define effective acceptance")
        if self.reuse_count and self.carry_forward_count:
            raise ValueError("a job is completed by reuse or by carry-forward, never by both")
        structural_values = (
            self.latest_structural_repair_file,
            self.latest_structural_repair_sha256,
        )
        if self.structural_repair_count == 0:
            if any(value is not None for value in structural_values):
                raise ValueError(
                    "job without a structural repair must not reference structural repair evidence"
                )
            if self.acceptance_origin is TranslationAcceptanceOrigin.STRUCTURAL_REPAIR:
                raise ValueError("structural repair acceptance origin requires evidence")
        elif any(value is None for value in structural_values):
            raise ValueError("structurally repaired job requires complete evidence metadata")
        elif (
            self.acceptance_status is not None
            and self.acceptance_origin is not TranslationAcceptanceOrigin.STRUCTURAL_REPAIR
        ):
            raise ValueError("latest structural repair must define effective acceptance")
        semantic_values = (self.latest_semantic_file, self.latest_semantic_sha256)
        if self.semantic_status is None:
            if self.semantic_evidence_count or any(value is not None for value in semantic_values):
                raise ValueError("an unjudged job must not reference semantic evidence")
        elif self.semantic_evidence_count == 0 or any(value is None for value in semantic_values):
            raise ValueError("a semantic status requires the evidence that established it")
        revision_values = (
            self.latest_semantic_revision_file,
            self.latest_semantic_revision_sha256,
        )
        if self.semantic_revision_count == 0:
            if any(value is not None for value in revision_values):
                raise ValueError("job without a semantic revision must not reference one")
        elif any(value is None for value in revision_values):
            raise ValueError("a semantically revised job requires complete revision metadata")
        return self


class TranslationRun(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    label: str | None = None
    snapshot_id: str
    snapshot_path: Path
    snapshot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    queue_engine_version: str
    #: The output contract every job in this run is rendered and judged against, frozen when the
    #: run is created.  ``None`` is the pre-contract default and means the original unmarked
    #: prose contract, which is what keeps an existing run's packages, attempts, and reuse
    #: eligibility byte-identical after a newer contract becomes the default for new runs.
    output_contract_version: str | None = None
    #: The production semantic gate policy this run is held to, frozen at creation. ``None`` is
    #: the pre-gate default and means no semantic requirement exists for the run, which is what
    #: keeps a historical run releasable exactly as it was.
    semantic_gate_policy_version: str | None = None
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    chapter_count: int = Field(ge=1)
    job_count: int = Field(ge=1)
    pending_count: int = Field(ge=0)
    claimed_count: int = Field(ge=0)
    running_count: int = Field(ge=0)
    succeeded_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    retry_pending_count: int = Field(ge=0)
    cancelled_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    accepted_count: int = Field(default=0, ge=0)
    qa_rejected_count: int = Field(default=0, ge=0)
    provider_succeeded_unassessed_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(ge=1)
    retry_backoff_seconds: int = Field(ge=0)
    status: TranslationRunStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    warnings: list[str] = Field(default_factory=list)


class TranslationQueueReport(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    run_id: str
    snapshot_id: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    queue_engine_version: str
    chapter_count: int = Field(ge=1)
    job_count: int = Field(ge=1)
    first_job_id: str
    last_job_id: str
    pending_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    retry_backoff_seconds: int = Field(ge=0)
    one_to_one_coverage: bool
    ordering_valid: bool
    snapshot_valid: bool
    warnings: list[str] = Field(default_factory=list)
    created_at: AwareDatetime


class TranslationQueueValidation(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    valid: bool
    snapshot_valid: bool
    counter_mismatch: bool
    status_mismatch: bool
    expected_job_count: int = Field(ge=0)
    actual_job_count: int = Field(ge=0)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
