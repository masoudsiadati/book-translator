"""Contracts for the governed translation of chapter titles.

A chapter title is book *metadata*, not prose. It lives in the canonical TOC, in chapter metadata,
and in every snapshot manifest, and it is the one piece of a chapter that a release needs but the
translation run never produced: chunks carry paragraphs, not headings. This stage answers that gap
with its own immutable artifact rather than by touching anything the run owns.

The records here keep three things apart on purpose. ``ChapterTitleDocument`` is the *answer* and
carries no timestamp, so the same titles always serialize to the same bytes and the same content
address — that is what a release consumes and what makes a release reproducible.
``ChapterTitleManifest`` is the *index* and holds the single timestamp. ``ChapterTitleProvenance``
is *history*: what was sent where, by which provider, when, and what QA said. History is not
reproducible and is not meant to be.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.book import TocKind

#: Identity of one published chapter-title artifact, addressed by the inputs it translated.
CHAPTER_TITLE_ARTIFACT_ID_PATTERN = r"^ct-[0-9a-f]{24}$"


class ChapterTitleOrigin(StrEnum):
    """Where one released Persian chapter title actually came from."""

    #: The provider translated this title in this artifact.
    PROVIDER_TRANSLATION = "provider_translation"
    #: The workspace already recorded a target-language title; no provider was asked.
    RECORDED_TITLE = "recorded_title"
    #: An identical English title was translated once and reused, so duplicates cannot diverge.
    DEDUPLICATED = "deduplicated"
    #: Carried byte-for-byte from a parent artifact by a provider-free revision.
    INHERITED = "inherited_title"
    #: Replaced by an explicit user decision in a provider-free revision of a parent artifact.
    OPERATOR_OVERRIDE = "operator_override"


class ChapterTitleRecord(ContractModel):
    """One chapter's title pair, addressed by the bytes of both sides."""

    chapter_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    chapter_sequence: int = Field(ge=1)
    kind: TocKind
    number: int | None = Field(default=None, ge=0)
    title_en: str = Field(min_length=1)
    title_fa: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    origin: ChapterTitleOrigin

    @model_validator(mode="after")
    def a_title_is_one_line_and_actually_translated(self) -> "ChapterTitleRecord":
        for value in (self.title_en, self.title_fa):
            if value != value.strip() or "\n" in value or "\r" in value:
                raise ValueError("a chapter title must be one trimmed line")
        if self.title_fa == self.title_en:
            raise ValueError("a translated chapter title must differ from its source title")
        return self


class ChapterTitleDocument(ContractModel):
    """The answer: every chapter of one snapshot with its target-language title.

    It carries no timestamp. Given the same titles it serializes to the same bytes, which is what
    lets a release name a title artifact by hash and stay reproducible.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    chapter_title_engine_version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    artifact_id: str = Field(pattern=CHAPTER_TITLE_ARTIFACT_ID_PATTERN)
    book_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    snapshot_id: str = Field(min_length=1)
    snapshot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminology_file: Path | None = None
    terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    terminology_version: str | None = None
    chapter_count: int = Field(ge=1)
    chapters: list[ChapterTitleRecord]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: The artifact this one revised, when it is a provider-free revision of another.
    parent_artifact_id: str | None = Field(
        default=None,
        pattern=CHAPTER_TITLE_ARTIFACT_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def coverage_is_complete_and_consistent(self) -> "ChapterTitleDocument":
        if len(self.chapters) != self.chapter_count:
            raise ValueError("chapter_count must equal the number of title records")
        if [record.chapter_sequence for record in self.chapters] != list(
            range(1, self.chapter_count + 1)
        ):
            raise ValueError("chapter sequences must be contiguous and ordered")
        identities = [record.chapter_id for record in self.chapters]
        if len(identities) != len(set(identities)):
            raise ValueError("chapter title identities must be unique")
        # The same English title must never receive two different Persian titles: a book whose
        # headings disagree with themselves is worse than one with no headings at all.
        by_source: dict[str, str] = {}
        for record in self.chapters:
            existing = by_source.setdefault(record.title_en, record.title_fa)
            if existing != record.title_fa:
                raise ValueError("one English chapter title must map to one Persian title")
        if (self.terminology_file is None) != (self.terminology_sha256 is None):
            raise ValueError("terminology file and hash must be present together")
        return self


class ChapterTitleArtifactReference(ContractModel):
    """One published file of a chapter-title artifact."""

    file: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=0)


class ChapterTitleManifest(ContractModel):
    """The content-addressed index of one chapter-title artifact directory."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    chapter_title_engine_version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    chapter_title_prompt_version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    chapter_title_qa_version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    artifact_id: str = Field(pattern=CHAPTER_TITLE_ARTIFACT_ID_PATTERN)
    book_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    source_translation_run: str | None = Field(default=None, pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    snapshot_id: str = Field(min_length=1)
    snapshot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    terminology_version: str | None = None
    chapter_count: int = Field(ge=1)
    translated_title_count: int = Field(ge=0)
    deduplicated_title_count: int = Field(ge=0)
    recorded_title_count: int = Field(ge=0)
    #: Titles a revision carried unchanged from its parent, and titles a person replaced there.
    inherited_title_count: int = Field(default=0, ge=0, exclude_if=lambda value: not value)
    override_title_count: int = Field(default=0, ge=0, exclude_if=lambda value: not value)
    parent_artifact_id: str | None = Field(
        default=None,
        pattern=CHAPTER_TITLE_ARTIFACT_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )
    chapter_title_revision_version: str | None = Field(
        default=None,
        pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$",
        exclude_if=lambda value: value is None,
    )
    provider_calls: int = Field(ge=0)
    providers: dict[str, int]
    models: dict[str, int]
    qa_valid: Literal[True] = True
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: list[ChapterTitleArtifactReference]
    created_at: AwareDatetime

    @model_validator(mode="after")
    def index_is_complete_and_ordered(self) -> "ChapterTitleManifest":
        totals = (
            self.translated_title_count
            + self.deduplicated_title_count
            + self.recorded_title_count
            + self.inherited_title_count
            + self.override_title_count
        )
        if totals != self.chapter_count:
            raise ValueError("every chapter title must have exactly one recorded origin")
        revised = self.inherited_title_count or self.override_title_count
        if bool(revised) != (self.parent_artifact_id is not None):
            raise ValueError("a revised artifact is the one that names its parent")
        if (self.parent_artifact_id is not None) != (
            self.chapter_title_revision_version is not None
        ):
            raise ValueError("a revision records the engine version that produced it")
        if self.parent_artifact_id is not None:
            if self.parent_artifact_id == self.artifact_id:
                raise ValueError("an artifact cannot revise itself")
            if self.provider_calls:
                raise ValueError("a chapter-title revision contacts no provider")
        if self.provider_calls != self.translated_title_count:
            raise ValueError("one provider call is spent per distinct translated title")
        files = [artifact.file for artifact in self.artifacts]
        if len(files) != len(set(files)) or sorted(files) != files:
            raise ValueError("artifact files must be unique and ordered by path")
        for counter in (self.providers, self.models):
            if sorted(counter) != list(counter):
                raise ValueError("provider and model counters must be ordered by key")
        return self


class ChapterTitleProvenanceRecord(ContractModel):
    """The history of one released chapter title. Digests only — never prompts or responses."""

    chapter_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    chapter_sequence: int = Field(ge=1)
    title_en: str = Field(min_length=1)
    title_fa: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    origin: ChapterTitleOrigin
    provider: str | None = None
    provider_adapter_version: str | None = None
    model: str | None = None
    provider_response_id: str | None = None
    request_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    snapshot_id: str = Field(min_length=1)
    terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    terminology_version: str | None = None
    enforced_terminology_decisions: list[str] = Field(default_factory=list)
    qa_valid: bool
    qa_finding_codes: list[str] = Field(default_factory=list)
    translated_at: AwareDatetime | None = None
    deduplicated_from_chapter_id: str | None = None
    #: Revision lineage. A carried title names where it came from; an override additionally
    #: records the title it replaced and the person's reason for replacing it.
    parent_artifact_id: str | None = Field(
        default=None,
        pattern=CHAPTER_TITLE_ARTIFACT_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )
    superseded_title_fa: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    superseded_translation_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )
    override_reason: str | None = Field(
        default=None, min_length=1, max_length=500, exclude_if=lambda value: value is None
    )
    decided_by: Literal["user"] | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def history_matches_its_origin(self) -> "ChapterTitleProvenanceRecord":
        if self.origin is ChapterTitleOrigin.PROVIDER_TRANSLATION:
            if self.provider is None or self.translated_at is None:
                raise ValueError("a provider translation must record its provider and time")
            if self.deduplicated_from_chapter_id is not None:
                raise ValueError("a provider translation is not a deduplicated reuse")
        if self.origin is ChapterTitleOrigin.DEDUPLICATED and (
            self.deduplicated_from_chapter_id is None
        ):
            raise ValueError("a deduplicated title must name the chapter it reused")
        if self.origin is ChapterTitleOrigin.RECORDED_TITLE and self.provider is not None:
            raise ValueError("a recorded workspace title was never sent to a provider")
        revised = self.origin in {
            ChapterTitleOrigin.INHERITED,
            ChapterTitleOrigin.OPERATOR_OVERRIDE,
        }
        if revised != (self.parent_artifact_id is not None):
            raise ValueError("a revised title is the one that names its parent artifact")
        override = self.origin is ChapterTitleOrigin.OPERATOR_OVERRIDE
        if override:
            if self.provider is not None or self.translated_at is not None:
                raise ValueError("an operator override was never sent to a provider")
            if (
                self.superseded_title_fa is None
                or self.superseded_translation_sha256 is None
                or self.override_reason is None
                or self.decided_by is None
            ):
                raise ValueError("an override records what it replaced, why, and who decided it")
            if self.superseded_translation_sha256 == self.translation_sha256:
                raise ValueError("an override must change the title it replaces")
        elif (
            self.superseded_title_fa is not None
            or self.superseded_translation_sha256 is not None
            or self.override_reason is not None
            or self.decided_by is not None
        ):
            raise ValueError("only an override supersedes a title")
        if sorted(self.enforced_terminology_decisions) != self.enforced_terminology_decisions:
            raise ValueError("enforced terminology decisions must be ordered")
        if sorted(self.qa_finding_codes) != self.qa_finding_codes:
            raise ValueError("QA finding codes must be ordered")
        return self


class ChapterTitleQAFinding(ContractModel):
    """One title QA finding. It names a chapter and a code, never a title's text."""

    code: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    severity: Literal["warning", "error"]
    chapter_id: str | None = None
    candidate_id: str | None = Field(default=None, pattern=r"^tc-[0-9a-f]{24}$")
    message: str = Field(min_length=1)


class ChapterTitleQAReport(ContractModel):
    """The provider-free verdict on one set of chapter titles."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    chapter_title_qa_version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    valid: bool
    provider_contacted: Literal[False] = False
    checked_chapters: int = Field(ge=0)
    checked_terminology_decisions: int = Field(ge=0)
    findings: list[ChapterTitleQAFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def validity_follows_the_findings(self) -> "ChapterTitleQAReport":
        blocking = any(finding.severity == "error" for finding in self.findings)
        if self.valid == blocking:
            raise ValueError("valid must be true exactly when no error finding is present")
        return self


class ChapterTitleProvenance(ContractModel):
    """The complete history of one chapter-title artifact."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    chapter_title_engine_version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    chapter_title_prompt_version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    artifact_id: str = Field(pattern=CHAPTER_TITLE_ARTIFACT_ID_PATTERN)
    book_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    source_translation_run: str | None = Field(default=None, pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    snapshot_id: str = Field(min_length=1)
    snapshot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chapter_count: int = Field(ge=1)
    titles: list[ChapterTitleProvenanceRecord]
    qa: ChapterTitleQAReport
    created_at: AwareDatetime

    @model_validator(mode="after")
    def every_chapter_has_history_once_and_in_order(self) -> "ChapterTitleProvenance":
        if len(self.titles) != self.chapter_count:
            raise ValueError("chapter_count must equal the number of provenance records")
        sequences = [record.chapter_sequence for record in self.titles]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            raise ValueError("title provenance must follow chapter order, once per chapter")
        known = {record.chapter_id for record in self.titles}
        for record in self.titles:
            source = record.deduplicated_from_chapter_id
            if source is not None and source not in known:
                raise ValueError("a deduplicated title must reuse a chapter in this artifact")
            if source == record.chapter_id:
                raise ValueError("a title cannot be deduplicated from itself")
        return self
