"""Immutable contracts for explicit operator resolution of incomplete executions."""

from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.provider_execution import ProviderName, TranslationAttemptStatus
from book_translator.models.translation_queue import TranslationJobStatus


class ExecutionResolutionProvenance(StrEnum):
    """Who asserted the external fact recorded by a resolution."""

    OPERATOR = "operator"


class ExecutionReservationResolutionKind(StrEnum):
    """The external provider-contact fact an operator asserts about a reservation."""

    PROVIDER_NOT_CONTACTED = "provider_not_contacted"
    PROVIDER_CONTACT_UNKNOWN = "provider_contact_unknown"
    PROVIDER_CONTACTED_NO_RESULT = "provider_contacted_no_result"


class ExecutionReservationResolution(ContractModel):
    """Durable evidence that an operator resolved one incomplete execution reservation.

    A reservation records only that a local worker was about to contact a provider.  When the
    worker dies after the send marker is published, delivery is unknowable from the workspace
    alone and automatic recovery refuses.  This record preserves the operator's explicit external
    assertion, the reservation it applies to, and the exact queue accounting it produced, so the
    reservation itself never has to be edited or deleted.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    resolution_id: str = Field(pattern=r"^xrr-[0-9a-f]{24}$")
    resolver_engine_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    provenance: ExecutionResolutionProvenance
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    job_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    attempt_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*__attempt-\d{3,}$")
    attempt_number: int = Field(ge=1)
    reservation_file: Path
    reservation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reservation_status: TranslationAttemptStatus
    provider: ProviderName
    prompt_package_id: str = Field(min_length=1)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution: ExecutionReservationResolutionKind
    reason: str = Field(min_length=1)
    previous_job_status: TranslationJobStatus
    previous_attempt_count: int = Field(ge=1)
    previous_abandoned_attempt_count: int = Field(ge=0)
    resulting_job_status: TranslationJobStatus
    resulting_attempt_count: int = Field(ge=1)
    resulting_abandoned_attempt_count: int = Field(ge=0)
    provider_attempt_consumed: bool
    replay_permitted: bool
    created_at: AwareDatetime

    @model_validator(mode="after")
    def resolution_accounting_is_consistent(self) -> "ExecutionReservationResolution":
        if self.reservation_status is not TranslationAttemptStatus.REQUESTING:
            raise ValueError("only an incomplete requesting reservation is resolved this way")
        if self.attempt_id != f"{self.job_id}__attempt-{self.attempt_number:03d}":
            raise ValueError("attempt identity must match the job and attempt number")
        if self.previous_job_status is self.resulting_job_status:
            raise ValueError("a resolution must move the job out of its incomplete state")
        if self.resulting_attempt_count != self.previous_attempt_count:
            raise ValueError("a resolution must not rewrite the issued attempt-slot sequence")
        abandoned = self.resulting_abandoned_attempt_count - self.previous_abandoned_attempt_count
        if abandoned not in {0, 1}:
            raise ValueError("a resolution abandons at most one attempt slot")
        if self.provider_attempt_consumed is bool(abandoned):
            raise ValueError("provider attempt consumption must agree with the abandoned slot")
        if self.resulting_abandoned_attempt_count > self.resulting_attempt_count:
            raise ValueError("abandoned attempt slots cannot exceed issued attempt slots")
        if self.replay_permitted and self.provider_attempt_consumed:
            raise ValueError("a consumed provider attempt never permits automatic replay")
        return self
