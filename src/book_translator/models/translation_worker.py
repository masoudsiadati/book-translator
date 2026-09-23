"""Durable translation acceptance evidence produced by queue workers."""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.paragraph_alignment import TranslationAlignmentReport
from book_translator.models.terminology import (
    TerminologyValidationReport,
    TranslationQAReport,
)


class TranslationAcceptanceStatus(StrEnum):
    ACCEPTED = "accepted"
    QA_REJECTED = "qa_rejected"


class TranslationAcceptanceOrigin(StrEnum):
    INITIAL_QA = "initial_qa"
    QA_REEVALUATION = "qa_reevaluation"
    REUSE_QA = "reuse_qa"
    HISTORICAL_REEVALUATION = "historical_reevaluation"
    STRUCTURAL_REPAIR = "structural_repair"


class ReuseCompatibilityTier(StrEnum):
    """How strongly a carried-forward translation's provider-facing request was proven."""

    EXACT_RENDERED_REQUEST = "exact_rendered_request"
    INCOMPATIBLE = "incompatible"


class TranslationReuseCompatibility(ContractModel):
    """Effective-request identity proof recorded alongside a carried-forward translation."""

    policy_version: str
    tier: ReuseCompatibilityTier
    provider_contacted: Literal[False] = False
    source_snapshot_id: str
    target_snapshot_id: str
    source_prompt_package_id: str = Field(pattern=r"^pp-[0-9a-f]{24}$")
    target_prompt_package_id: str = Field(pattern=r"^pp-[0-9a-f]{24}$")
    source_system_message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_system_message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_user_message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_user_message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified_identity_checks: list[str]
    waived_resource_drift: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def tier_is_proven(self) -> "TranslationReuseCompatibility":
        exact = self.tier is ReuseCompatibilityTier.EXACT_RENDERED_REQUEST
        identical = (
            self.source_system_message_sha256 == self.target_system_message_sha256
            and self.source_user_message_sha256 == self.target_user_message_sha256
        )
        if exact and not identical:
            raise ValueError("exact rendered-request reuse requires identical message digests")
        if not exact and self.waived_resource_drift:
            raise ValueError("only proven request identity may waive resource drift")
        return self


class TranslationReuseEvidence(ContractModel):
    """Immutable new-run provenance for one carried-forward translation."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    reuse_engine_version: str
    execution_kind: Literal["reuse"] = "reuse"
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    job_id: str
    reuse_number: int = Field(ge=1)
    source_run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    source_job_id: str
    source_attempt_number: int = Field(ge=1)
    source_attempt_file: Path
    source_attempt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_translation_file: Path
    source_translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_acceptance_file: Path
    source_acceptance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_acceptance_origin: TranslationAcceptanceOrigin
    source_reevaluation_file: Path | None = None
    source_reevaluation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    compatibility: dict[str, str]
    rendered_request_compatibility: TranslationReuseCompatibility | None = None
    translation_file: Path
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def lineage_is_consistent(self) -> "TranslationReuseEvidence":
        if self.source_translation_sha256 != self.translation_sha256:
            raise ValueError("reused translation must preserve the source translation hash")
        if (self.source_reevaluation_file is None) != (self.source_reevaluation_sha256 is None):
            raise ValueError("re-evaluation file and hash must be present together")
        return self


class TranslationAcceptanceEvidence(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    acceptance_engine_version: str
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    job_id: str
    attempt_id: str
    attempt_number: int = Field(ge=1)
    attempt_file: Path
    attempt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file: Path
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_file: Path
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminology_file: Path | None = None
    terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    terminology_qa_version: str | None = None
    structural_qa_version: str | None = None
    #: Present from alignment QA onwards.  Absent on evidence published before the engine
    #: existed, which is why every reader treats absence as "not assessed" rather than "passed":
    #: a historical acceptance was never judged against a gate that did not exist.
    alignment_qa_version: str | None = None
    terminology_report: TerminologyValidationReport
    structural_report: TranslationQAReport
    alignment_report: TranslationAlignmentReport | None = None
    status: TranslationAcceptanceStatus
    accepted: bool
    created_at: AwareDatetime

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> "TranslationAcceptanceEvidence":
        expected = (
            self.terminology_report.valid
            and self.structural_report.valid
            and (self.alignment_report is None or self.alignment_report.valid)
        )
        if self.accepted != expected:
            raise ValueError("accepted must equal the combined QA validity")
        if (self.status is TranslationAcceptanceStatus.ACCEPTED) != self.accepted:
            raise ValueError("acceptance status must match accepted")
        if (self.terminology_file is None) != (self.terminology_sha256 is None):
            raise ValueError("terminology file and hash must be present together")
        return self


class TranslationQAReevaluationEvidence(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    reevaluation_engine_version: str
    terminology_qa_version: str
    structural_qa_version: str
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    job_id: str
    attempt_id: str
    attempt_number: int = Field(ge=1)
    reevaluation_id: str = Field(pattern=r"^qre-[0-9a-f]{24}$")
    reevaluation_number: int = Field(ge=1)
    reevaluated_from_file: Path
    reevaluated_from_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_file: Path
    attempt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file: Path
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_file: Path
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminology_file: Path | None = None
    terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    prompt_file: Path
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    style_file: Path
    style_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminology_report: TerminologyValidationReport
    structural_report: TranslationQAReport
    previous_status: TranslationAcceptanceStatus
    resulting_status: TranslationAcceptanceStatus
    accepted: bool
    origin: Literal["qa_reevaluation"] = "qa_reevaluation"
    created_at: AwareDatetime

    @model_validator(mode="after")
    def outcome_and_lineage_are_consistent(self) -> "TranslationQAReevaluationEvidence":
        expected = self.terminology_report.valid and self.structural_report.valid
        if self.accepted != expected:
            raise ValueError("accepted must equal the combined QA validity")
        if (self.resulting_status is TranslationAcceptanceStatus.ACCEPTED) != self.accepted:
            raise ValueError("re-evaluation status must match accepted")
        if (self.terminology_file is None) != (self.terminology_sha256 is None):
            raise ValueError("terminology file and hash must be present together")
        if (
            self.terminology_report.source_sha256 != self.source_sha256
            or self.structural_report.source_sha256 != self.source_sha256
            or self.terminology_report.translation_sha256 != self.translation_sha256
            or self.structural_report.translation_sha256 != self.translation_sha256
        ):
            raise ValueError("QA report hashes must match re-evaluation lineage")
        return self
