"""Strict contracts for explicitly registered source-edition anomalies."""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel


class SourceAnomalyCategory(StrEnum):
    TRUNCATED_TOKEN = "truncated_token"
    SOURCE_TYPO = "source_typo"
    MISSING_TEXT = "missing_text"
    DUPLICATED_TEXT = "duplicated_text"
    GARBLED_TEXT = "garbled_text"
    LAYOUT_ARTIFACT = "layout_artifact"
    UNKNOWN = "unknown"


class SourceAnomalyStatus(StrEnum):
    CONFIRMED_SOURCE_DEFECT = "confirmed_source_defect"
    SUSPECTED_SOURCE_DEFECT = "suspected_source_defect"
    RESOLVED_BY_AUTHORITATIVE_SOURCE = "resolved_by_authoritative_source"
    ACCEPTED_AS_SOURCE = "accepted_as_source"


class SourceAnomalySeverity(StrEnum):
    LOCAL = "local"
    MATERIAL = "material"
    CRITICAL = "critical"


class SourceAnomalyEvidence(ContractModel):
    rendered_pdf_checked: bool
    pdf_text_layer_matches: bool
    immutable_source_matches: bool
    extraction_at_fault: bool
    replacement_known: bool
    evidence_files: list[Path] = Field(default_factory=list)
    evidence_sha256: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def file_hashes_align(self) -> "SourceAnomalyEvidence":
        if len(self.evidence_files) != len(self.evidence_sha256):
            raise ValueError("evidence files and hashes must align")
        return self


class SourceAnomaly(ContractModel):
    anomaly_id: str = Field(pattern=r"^sa-[0-9a-f]{24}$")
    book_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    chapter_id: str = Field(pattern=r"^(?:000-prologue|[0-9]{3}|999-epilogue)$")
    page_from: int = Field(ge=1)
    page_to: int = Field(ge=1)
    source_location: str = Field(min_length=1)
    category: SourceAnomalyCategory
    status: SourceAnomalyStatus
    severity: SourceAnomalySeverity
    blocking: bool
    original_text: str = Field(min_length=1)
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file: Path
    source_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: SourceAnomalyEvidence
    operator_note: str
    resolution: str | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def coherent(self) -> "SourceAnomaly":
        if self.page_to < self.page_from:
            raise ValueError("page_to must not precede page_from")
        if self.evidence.replacement_known and self.resolution is None:
            raise ValueError("known replacement requires an authoritative resolution")
        if not self.evidence.replacement_known and self.resolution is not None:
            raise ValueError("unknown replacement cannot carry resolution text")
        return self


class SourceAnomalyRegistry(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    registry_kind: Literal["source_anomalies"] = "source_anomalies"
    registry_version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    anomalies: list[SourceAnomaly] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> "SourceAnomalyRegistry":
        ids = [item.anomaly_id for item in self.anomalies]
        if len(ids) != len(set(ids)):
            raise ValueError("source anomaly IDs must be unique")
        return self
