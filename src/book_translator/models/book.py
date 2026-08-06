"""Book and table-of-contents contracts."""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel


class TocKind(StrEnum):
    FRONT_MATTER = "front_matter"
    PROLOGUE = "prologue"
    CHAPTER = "chapter"
    APPENDIX = "appendix"
    UNKNOWN = "unknown"


class BookWorkspaceMetadata(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    book_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    source_pdf: Path
    source_pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    title_en: str = Field(min_length=1)
    title_fa: str = Field(min_length=1)
    author: str = Field(min_length=1)
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    expected_output_format: Literal["epub3"]
    created_at: AwareDatetime
    updated_at: AwareDatetime


class TocEntry(ContractModel):
    entry_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    level: int = Field(ge=1)
    kind: TocKind
    number: int | None = Field(default=None, ge=0)
    title_en: str = ""
    title_fa: str = ""
    symbol: str | None = None
    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)
    source_toc_title: str = Field(min_length=1)

    @model_validator(mode="after")
    def page_range_is_ordered(self) -> "TocEntry":
        if self.end_page < self.start_page:
            raise ValueError("end_page must be greater than or equal to start_page")
        return self


class TocDocument(ContractModel):
    """Versioned normalized embedded TOC persisted as workspace/toc.yaml."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    source: Literal["embedded"] | None = None
    source_pdf: Path | None = None
    source_pdf_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pdf_page_count: int | None = Field(default=None, ge=1)
    entry_count: int | None = Field(default=None, ge=0)
    entries: list[TocEntry]

    @model_validator(mode="after")
    def entry_count_matches_entries(self) -> "TocDocument":
        if self.entry_count is not None and self.entry_count != len(self.entries):
            raise ValueError("entry_count must equal the number of entries")
        return self
