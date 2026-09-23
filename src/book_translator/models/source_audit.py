"""Persisted contracts for deterministic source-quality audits."""

from enum import StrEnum

from pydantic import Field

from book_translator.models.base import ContractModel


class AuditSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class AuditConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AuditCategory(StrEnum):
    PAGE_BOUNDARY_CONTINUATION = "page_boundary_continuation"
    JOINED_TOKEN = "joined_token"
    SPLIT_TOKEN = "split_token"
    DIALOGUE_BOUNDARY = "dialogue_boundary"
    PARAGRAPH_BOUNDARY = "paragraph_boundary"
    HEADING_STRUCTURE = "heading_structure"
    CHUNK_BOUNDARY = "chunk_boundary"
    DUPLICATION = "duplication"
    CONSERVATION = "conservation"


class AuditSourceLayer(StrEnum):
    EXTRACTION = "immutable_extraction"
    NORMALIZATION = "normalization"
    CHUNKING = "chunking"
    MANUAL_REVIEW = "manual_source_review"


class AuditResolutionStatus(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVED_BY_NORMALIZATION = "resolved_by_normalization"
    RESOLVED_BY_CHUNKING = "resolved_by_chunking"
    NOT_APPLICABLE_DOWNSTREAM = "not_applicable_downstream"
    CANNOT_EVALUATE = "cannot_evaluate"
    KNOWN_SOURCE_ANOMALY = "known_source_anomaly"


class SourceAuditFinding(ContractModel):
    finding_id: str = Field(pattern=r"^sqf-[0-9a-f]{24}$")
    chapter_id: str
    chunk_id: str | None = None
    page_from: int | None = Field(default=None, ge=1)
    page_to: int | None = Field(default=None, ge=1)
    category: AuditCategory
    severity: AuditSeverity
    confidence: AuditConfidence
    source_layer: AuditSourceLayer
    resolution_status: AuditResolutionStatus
    blocking: bool
    anomaly_id: str | None = Field(default=None, pattern=r"^sa-[0-9a-f]{24}$")
    message: str
    evidence: dict[str, str | int | bool | None] = Field(default_factory=dict)
    suggested_action: str


class SourceAuditIdentity(ContractModel):
    audit_engine_version: str
    extraction_lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalization_lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalization_versions: list[str]
    chunk_engine_versions: list[str]


class SourceAuditReport(ContractModel):
    schema_version: str
    audit_id: str = Field(pattern=r"^source-quality-[0-9a-f]{24}$")
    identity: SourceAuditIdentity
    chapters_scanned: int = Field(ge=0)
    pages_inspected: int = Field(ge=0)
    blocks_inspected: int = Field(ge=0)
    chunks_inspected: int = Field(ge=0)
    blocking_findings: int = Field(ge=0)
    unresolved_high_confidence_findings: int = Field(ge=0)
    resolved_high_confidence_findings: int = Field(ge=0)
    chapters_missing_normalization: int = Field(ge=0)
    chapters_missing_chunks: int = Field(ge=0)
    chapters_translation_ready: int = Field(ge=0)
    findings: list[SourceAuditFinding]
