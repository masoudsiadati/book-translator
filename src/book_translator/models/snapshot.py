"""Immutable extraction snapshot contract."""

from pydantic import AwareDatetime, Field

from book_translator.models.base import ContractModel


class ChapterSnapshotHashes(ContractModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cleaned_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExtractionSnapshot(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    snapshot_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    book_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    source_pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    toc_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extraction_version: str
    normalization_version: str
    chapter_count: int = Field(ge=0)
    chapter_hashes: dict[str, ChapterSnapshotHashes]
    created_at: AwareDatetime
    validated: bool
    warnings: list[str] = Field(default_factory=list)
