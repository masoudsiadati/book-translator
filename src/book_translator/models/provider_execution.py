"""Persistent and provider-neutral contracts for translation attempts."""

from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.paragraph_alignment import ParagraphAlignmentFinding


class ProviderName(StrEnum):
    FAKE = "fake"
    OPENAI = "openai"
    CODEX_CLI = "codex-cli"
    CLAUDE_CODE = "claude-code"


class TranslationExecutionKind(StrEnum):
    TRANSLATION = "translation"
    REPAIR = "repair"


class TranslationAttemptStatus(StrEnum):
    PREPARED = "prepared"
    REQUESTING = "requesting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"


class ProviderErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    TIMEOUT_PRE_SEND = "timeout_pre_send"
    TIMEOUT_AMBIGUOUS = "timeout_ambiguous"
    CONNECTION_PRE_SEND = "connection_pre_send"
    CONNECTION_AMBIGUOUS = "connection_ambiguous"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_MODEL_OR_PARAMETER = "unsupported_model_or_parameter"
    CONTEXT_LENGTH = "context_length"
    CONTENT_POLICY = "content_policy"
    SERVER_ERROR = "server_error"
    MALFORMED_RESPONSE = "malformed_response"
    EMPTY_RESPONSE = "empty_response"
    OUTPUT_TRUNCATED = "output_truncated"
    PACKAGE_INVALID = "package_invalid"
    CLAIM_INVALID = "claim_invalid"
    LEASE_INVALID = "lease_invalid"
    LOCAL_IO = "local_io"
    EXECUTABLE_NOT_FOUND = "executable_not_found"
    TIMEOUT = "timeout"
    NONZERO_EXIT = "nonzero_exit"
    EMPTY_OUTPUT = "empty_output"
    INVOCATION_ERROR = "invocation_error"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class TranslationAttempt(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    attempt_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*__attempt-\d{3,}$")
    attempt_number: int = Field(ge=1)
    execution_kind: TranslationExecutionKind = TranslationExecutionKind.TRANSLATION
    repaired_from_attempt: int | None = Field(default=None, ge=1)
    #: A repair of a draft this run never produced itself: the historical output a governed
    #: carry-forward brought into the job, named by carry-forward number. Omitted when absent so
    #: every attempt written before it existed keeps its bytes.
    repaired_from_carry_forward: int | None = Field(
        default=None, ge=1, exclude_if=lambda value: value is None
    )
    run_id: str
    job_id: str
    snapshot_id: str
    prompt_package_id: str
    provider: ProviderName
    provider_adapter_version: str
    provider_executor_version: str
    model_requested: str | None
    model_returned: str | None = None
    provider_response_id: str | None = None
    request_file: Path
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_file: Path | None = None
    response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    raw_response_stored: bool
    translation_file: Path | None = None
    translation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_contract: str
    status: TranslationAttemptStatus
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    latency_milliseconds: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_input_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(gt=0)
    sdk_retry_count: int = Field(ge=0)
    stop_reason: str | None = None
    error_category: ProviderErrorCategory | None = None
    retryable: bool = False
    ambiguous: bool = False
    sanitized_error_message: str | None = None
    warnings: list[str] = Field(default_factory=list)
    # A candidate answer that arrived whole but was refused as a translation is kept privately
    # beside the attempt for diagnosis. It is never a translation: acceptance, QA, repair, reuse,
    # and release read ``translation_file`` only. Attempts written before these fields existed
    # carry their defaults, which is the truthful record: their prose was not retained.
    rejected_output_file: Path | None = None
    rejected_output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # Structural evidence of a broken numbered paragraph contract: codes, expected and observed
    # marker tokens, paragraph indexes, and marker counts. Never a snippet of the answer.
    marker_findings: list[ParagraphAlignmentFinding] = Field(default_factory=list)
    expected_marker_count: int | None = Field(default=None, ge=0)
    observed_marker_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def repair_lineage_is_consistent(self) -> "TranslationAttempt":
        if self.execution_kind is TranslationExecutionKind.REPAIR:
            if self.repaired_from_carry_forward is not None:
                if self.repaired_from_attempt is not None:
                    raise ValueError("a repair answers one draft: an attempt or a carry-forward")
            elif (
                self.repaired_from_attempt is None
                or self.repaired_from_attempt >= self.attempt_number
            ):
                raise ValueError("repair attempts require an earlier repaired-from attempt")
        elif self.repaired_from_attempt is not None or self.repaired_from_carry_forward is not None:
            raise ValueError("translation attempts must not carry repair lineage")
        return self


class TranslationAttemptValidationReport(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    attempt_id: str
    valid: bool
    job_lineage_valid: bool
    package_lineage_valid: bool
    request_hash_valid: bool
    response_hash_valid: bool
    translation_hash_valid: bool
    status_consistent: bool
    permissions_valid: bool
    # ``None`` on reports written before the rejected-output artifact existed: the check was not
    # performed, which is different from having passed.
    rejected_output_hash_valid: bool | None = None
    checks_performed: list[str]
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    created_at: AwareDatetime


class AttemptReservation(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    attempt_id: str
    attempt_number: int = Field(ge=1)
    run_id: str
    job_id: str
    prompt_package_id: str
    provider: ProviderName
    model: str | None
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: TranslationAttemptStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
