"""Contracts for immutable content-addressed translation snapshots."""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.book import TocKind
from book_translator.models.series_terminology import ReferenceCorpusIdentity


class TranslationSnapshotStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    FROZEN = "frozen"
    INVALID = "invalid"


class SnapshotResourceKind(StrEnum):
    PROMPT = "prompt"
    GLOSSARY = "glossary"
    STYLE_GUIDE = "style_guide"
    TERMINOLOGY = "terminology"
    SOURCE_ANOMALIES = "source_anomalies"
    OPEN_QUESTIONS = "open_questions"
    SERIES_TERMINOLOGY = "series_terminology"
    REFERENCE_CORPUS = "reference_corpus"
    REFERENCE_CONTEXT = "reference_context"
    TERMINOLOGY_EXEMPTIONS = "terminology_exemptions"


class VersionedResourceMetadata(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    resource_kind: SnapshotResourceKind
    resource_file: Path
    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class SnapshotResourceReference(ContractModel):
    resource_kind: SnapshotResourceKind
    file: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    frozen_file: Path | None = None


class SnapshotReferenceCorpusReference(SnapshotResourceReference):
    """The frozen reference-corpus configuration plus the verified external corpus identities.

    The corpora themselves are external frozen evidence and are never copied into the workspace;
    what the snapshot pins is their identity, so lineage can prove which bytes were retrieved
    from without owning them.
    """

    retriever_path: str = Field(min_length=1)
    retriever_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retriever_schema_version: str = Field(min_length=1)
    corpora: list[ReferenceCorpusIdentity] = Field(min_length=1)


class SnapshotBookReference(ContractModel):
    book_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    title_en: str
    title_fa: str
    # Optional so manifests published before resolved book metadata keep loading unchanged.
    author: str = ""
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    series_name: str | None = None
    series_index: int | None = Field(default=None, ge=1)
    source_pdf: Path
    source_pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_file: Path | None = None
    metadata_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    metadata_version: str | None = Field(
        default=None, pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
    )
    frozen_metadata_file: Path | None = None
    toc_file: Path
    toc_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def metadata_reference_is_complete(self) -> "SnapshotBookReference":
        parts = (self.metadata_file, self.metadata_sha256, self.metadata_version)
        if any(part is not None for part in parts) and not all(part is not None for part in parts):
            raise ValueError("a book metadata reference requires its path, hash, and version")
        if self.frozen_metadata_file is not None and self.metadata_sha256 is None:
            raise ValueError("a frozen book metadata copy requires the metadata hash")
        return self


class SnapshotChunkReference(ContractModel):
    chunk_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    chapter_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    sequence: int = Field(ge=1)
    source_file: Path
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_word_count: int = Field(ge=0)
    source_paragraph_count: int = Field(ge=0)
    context_file: Path
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_word_count: int = Field(ge=0)
    oversize: bool
    warnings: list[str] = Field(default_factory=list)


class SnapshotChapterReference(ContractModel):
    chapter_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    kind: TocKind
    number: int | None = Field(default=None, ge=0)
    title_en: str
    title_fa: str
    cleaned_file: Path
    cleaned_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalization_version: str = Field(min_length=1)
    chunk_engine_version: str = Field(min_length=1)
    chunk_count: int = Field(ge=1)
    chunks: list[SnapshotChunkReference]

    @model_validator(mode="after")
    def chunks_match_chapter(self) -> "SnapshotChapterReference":
        if self.chunk_count != len(self.chunks):
            raise ValueError("chunk_count must equal the number of chunks")
        if any(chunk.chapter_id != self.chapter_id for chunk in self.chunks):
            raise ValueError("every chunk must reference its containing chapter")
        if [chunk.sequence for chunk in self.chunks] != list(range(1, self.chunk_count + 1)):
            raise ValueError("chunk sequences must be contiguous and ordered")
        return self


class TranslationManifest(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    snapshot_kind: Literal["translation"] = "translation"
    workflow: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    selection_mode: Literal["full", "subset"]
    book: SnapshotBookReference
    chapters: list[SnapshotChapterReference]
    prompt: SnapshotResourceReference
    glossary: SnapshotResourceReference
    style_guide: SnapshotResourceReference
    terminology: SnapshotResourceReference | None = None
    source_anomalies: SnapshotResourceReference | None = None
    open_questions: SnapshotResourceReference | None = None
    # Series-aware lineage. Absent on every manifest that predates it and on legacy books; when
    # present, all three travel together with the derived book terminology resource.
    series_terminology: SnapshotResourceReference | None = None
    reference_corpus: SnapshotReferenceCorpusReference | None = None
    reference_context: SnapshotResourceReference | None = None
    terminology_context_contract_version: str | None = None
    #: User-adjudicated occurrence exemptions. Omitted, not null, when a book has none, so every
    #: manifest written before they existed keeps its bytes and its identity.
    terminology_exemptions: SnapshotResourceReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def series_lineage_is_complete(self) -> "TranslationManifest":
        series_parts = (
            self.series_terminology,
            self.reference_corpus,
            self.reference_context,
            self.terminology_context_contract_version,
        )
        if any(part is not None for part in series_parts):
            if not all(part is not None for part in series_parts):
                raise ValueError(
                    "series terminology lineage requires the policy, reference corpus, "
                    "reference context, and context contract version together"
                )
            if self.terminology is None:
                raise ValueError("series terminology lineage requires the derived book terminology")
        return self

    @model_validator(mode="after")
    def identities_are_unique(self) -> "TranslationManifest":
        chapter_ids = [chapter.chapter_id for chapter in self.chapters]
        chunk_ids = [chunk.chunk_id for chapter in self.chapters for chunk in chapter.chunks]
        if len(chapter_ids) != len(set(chapter_ids)):
            raise ValueError("chapter IDs must be unique")
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("chunk IDs must be unique")
        return self


class TranslationSnapshot(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    snapshot_id: str = Field(pattern=r"^translation-[a-z0-9._-]+-[0-9a-f]{24}$")
    snapshot_kind: Literal["translation"] = "translation"
    label: str | None = None
    manifest_file: Path
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    book_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    selection_mode: Literal["full", "subset"]
    selected_chapter_count: int = Field(ge=1)
    selected_chunk_count: int = Field(ge=1)
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    prompt_version: str
    glossary_version: str
    style_guide_version: str
    terminology_version: str | None = None
    source_anomalies_version: str | None = None
    open_questions_version: str | None = None
    series_terminology_version: str | None = None
    reference_corpus_version: str | None = None
    reference_context_version: str | None = None
    terminology_exemptions_version: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    status: TranslationSnapshotStatus
    created_at: AwareDatetime
    frozen_at: AwareDatetime | None = None
    warnings: list[str] = Field(default_factory=list)


class TranslationSnapshotValidationReport(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    snapshot_id: str
    valid: bool
    checks_performed: list[str]
    selected_chapter_count: int = Field(ge=0)
    selected_chunk_count: int = Field(ge=0)
    artifact_hashes_validated: int = Field(ge=0)
    conservation_checks: int = Field(ge=0)
    stale_artifact_checks: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    created_at: AwareDatetime
