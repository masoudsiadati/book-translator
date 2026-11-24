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
    EPILOGUE = "epilogue"
    APPENDIX = "appendix"
    UNKNOWN = "unknown"


class BookMetadataProvenance(StrEnum):
    """Where a resolved book metadata value actually came from."""

    PDF_METADATA = "pdf_metadata"
    FRONT_MATTER = "front_matter"
    CONFIGURATION = "configuration"
    EXTERNAL_RESEARCH = "external_research"
    UNRESOLVED = "unresolved"


BOOK_METADATA_FIELDS: tuple[str, ...] = (
    "author",
    "book_id",
    "edition",
    "isbn",
    "publication_year",
    "publisher",
    "series_index",
    "series_name",
    "source_language",
    "target_language",
    "title_en",
    "title_fa",
)


class BookMetadataFieldProvenance(ContractModel):
    """Per-field provenance; `evidence` is a locator and never carries book prose."""

    field: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    provenance: BookMetadataProvenance
    evidence: str | None = None


class BookWorkspaceMetadata(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    metadata_version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    book_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    source_pdf: Path
    source_pdf_name: str = Field(min_length=1)
    source_pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_pdf_page_count: int | None = Field(default=None, ge=1)
    title_en: str = Field(min_length=1)
    # Blank means "not resolved yet"; a target title is a human translation decision, never a guess.
    title_fa: str = ""
    author: str = Field(min_length=1)
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    series_name: str | None = Field(default=None, min_length=1)
    series_index: int | None = Field(default=None, ge=1)
    edition: str | None = Field(default=None, min_length=1)
    publisher: str | None = Field(default=None, min_length=1)
    publication_year: int | None = Field(default=None, ge=1000, le=9999)
    isbn: str | None = Field(default=None, pattern=r"^(?:\d{9}[\dX]|\d{13})$")
    expected_output_format: Literal["epub3"]
    provenance: list[BookMetadataFieldProvenance]
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def provenance_covers_every_field(self) -> "BookWorkspaceMetadata":
        recorded = [item.field for item in self.provenance]
        if recorded != sorted(recorded):
            raise ValueError("provenance entries must be ordered by field name")
        if len(recorded) != len(set(recorded)):
            raise ValueError("provenance entries must be unique per field")
        if tuple(recorded) != BOOK_METADATA_FIELDS:
            raise ValueError("provenance must record exactly the resolvable book metadata fields")
        for item in self.provenance:
            value = getattr(self, item.field)
            resolved = value is not None and value != ""
            if resolved == (item.provenance is BookMetadataProvenance.UNRESOLVED):
                raise ValueError(f"provenance for {item.field} contradicts its resolved value")
        return self


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
    # Sub-page boundaries, recorded only by text-heading discovery of a line-layout PDF whose
    # entries begin mid-page: the entry starts at this index of the start page's sorted text
    # blocks and stops before `end_block` on the end page. Absent means whole pages.
    start_block: int | None = Field(default=None, ge=0)
    end_block: int | None = Field(default=None, ge=0)
    # The heading's own one-line groups (word, optional number line, title) in a line layout.
    heading_group_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def page_range_is_ordered(self) -> "TocEntry":
        if self.end_page < self.start_page:
            raise ValueError("end_page must be greater than or equal to start_page")
        if (
            self.start_page == self.end_page
            and self.start_block is not None
            and self.end_block is not None
            and self.end_block <= self.start_block
        ):
            raise ValueError("end_block must follow start_block on a single-page entry")
        return self


class TocDocument(ContractModel):
    """Versioned normalized embedded TOC persisted as workspace/toc.yaml."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    source: Literal["embedded", "text_headings", "epub_spine"] | None = None
    # `line_blocks`: one text block per printed line, blank blocks between paragraphs, print
    # furniture inside pages (see `book_translator.line_layout`). Absent means ordinary blocks.
    layout: Literal["line_blocks"] | None = None
    # Letter-only keys of the running heads a line-layout extraction removes as page furniture.
    running_head_keys: list[str] | None = None
    source_pdf: Path | None = None
    source_pdf_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pdf_page_count: int | None = Field(default=None, ge=1)
    # EPUB import records generic source-document lineage without re-labeling the archive as a PDF.
    # The existing entry page fields carry 1-based spine positions for this source kind.
    source_document: Path | None = None
    source_document_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_item_count: int | None = Field(default=None, ge=1)
    entry_count: int | None = Field(default=None, ge=0)
    entries: list[TocEntry]

    @model_validator(mode="after")
    def entry_count_matches_entries(self) -> "TocDocument":
        if self.entry_count is not None and self.entry_count != len(self.entries):
            raise ValueError("entry_count must equal the number of entries")
        epub_values = (
            self.source_document,
            self.source_document_sha256,
            self.source_item_count,
        )
        if self.source == "epub_spine":
            if any(value is None for value in epub_values):
                raise ValueError("EPUB spine TOC requires complete source-document lineage")
            if any(
                value is not None
                for value in (self.source_pdf, self.source_pdf_sha256, self.pdf_page_count)
            ):
                raise ValueError("EPUB spine TOC cannot carry PDF lineage")
        elif any(value is not None for value in epub_values):
            raise ValueError("source-document lineage is only valid for an EPUB spine TOC")
        return self
