"""Chapter metadata and lifecycle state."""

from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.book import TocKind


class ChapterStatus(StrEnum):
    DISCOVERED = "discovered"
    EXTRACTED = "extracted"
    NORMALIZED = "normalized"
    VALIDATED = "validated"
    CHUNKED = "chunked"
    TRANSLATING = "translating"
    TRANSLATED = "translated"
    REVIEW_REQUIRED = "review_required"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    FAILED = "failed"


class ChapterMetadata(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    chapter_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    kind: TocKind
    number: int | None = Field(default=None, ge=0)
    title_en: str = ""
    title_fa: str = ""
    symbol: str | None = None
    source_start_page: int = Field(ge=1)
    source_end_page: int = Field(ge=1)
    source_file: Path
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    cleaned_file: Path
    cleaned_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_word_count: int | None = Field(default=None, ge=0)
    cleaned_word_count: int | None = Field(default=None, ge=0)
    source_paragraph_count: int | None = Field(default=None, ge=0)
    cleaned_paragraph_count: int | None = Field(default=None, ge=0)
    chunk_count: int = Field(default=0, ge=0)
    status: ChapterStatus
    warnings: list[str] = Field(default_factory=list)
    extraction_version: str
    normalization_version: str
    chunk_engine_version: str = ""
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def page_range_is_ordered(self) -> "ChapterMetadata":
        if self.source_end_page < self.source_start_page:
            raise ValueError("source_end_page must be greater than or equal to source_start_page")
        return self
