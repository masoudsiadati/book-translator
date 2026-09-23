"""Contracts for the deterministic release assembly of a completed translation run.

A release is *derived* state: every byte in it is a function of one frozen snapshot and the
accepted translations of one completed run, so these records carry no judgement of their own.
They exist to make the assembly checkable by a third party who has the workspace and nothing
else — which is why every reference here is a project- or release-relative POSIX path beside an
exact SHA-256, and why the only timestamp in the whole set lives on the manifest.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.book import TocKind
from book_translator.models.translation_worker import (
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
)


class ReleaseArtifactRole(StrEnum):
    """What one file in a release directory is."""

    BOOK = "book"
    CHAPTER = "chapter"
    METADATA = "metadata"
    TERMINOLOGY = "terminology"
    TRANSLATION_REPORT = "translation_report"
    LINEAGE = "lineage"


class ReleaseHeadingSource(StrEnum):
    """Which recorded chapter title the assembled heading was taken from.

    The assembly never invents or translates a title. When the target-language title is still
    blank the source-language one is used verbatim and that substitution is recorded, because a
    reader of the release is entitled to know which headings are not yet translated.
    """

    #: The workspace's own recorded target-language chapter title.
    TITLE_TARGET = "title_target"
    #: A target-language title supplied by a governed chapter-title artifact.
    TITLE_TARGET_ARTIFACT = "title_target_artifact"
    #: No target-language title exists anywhere, so the source title is used verbatim.
    TITLE_SOURCE = "title_source"


class ReleaseProviderResolution(StrEnum):
    """How a chunk's originating provider was established, or why it could not be."""

    ATTEMPT = "attempt"
    CARRY_FORWARD_SOURCE = "carry_forward_source"
    REUSE_SOURCE = "reuse_source"
    UNRESOLVED = "unresolved"


class ReleaseArtifactReference(ContractModel):
    """One published release file, addressed by its own bytes."""

    file: Path
    role: ReleaseArtifactRole
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=0)


class ReleaseChapterRecord(ContractModel):
    """One assembled chapter, and the frozen chapter it was assembled from."""

    chapter_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    chapter_sequence: int = Field(ge=1)
    kind: TocKind
    number: int | None = Field(default=None, ge=0)
    title_en: str
    title_fa: str
    heading: str = Field(min_length=1)
    heading_source: ReleaseHeadingSource
    file: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_count: int = Field(ge=1)
    paragraph_count: int = Field(ge=1)
    word_count: int = Field(ge=0)
    source_cleaned_file: Path
    source_cleaned_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReleaseSemanticWaiver(ContractModel):
    """One operator's explicit, content-addressed authorization to publish unjudged work.

    A run created under a semantic gate policy is releasable only when every job holds a semantic
    PASS, and "not judged" is not a pass. This record does not change that: it authorizes exactly
    one release, of exactly one run against exactly one snapshot, to proceed while a stated number
    of jobs remain *unjudged*. It never turns an unjudged job into a passed one, and it may not
    cover a judgement that failed — a job the judge actually refused is a defect to fix, not a
    formality to waive.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    waiver_engine_version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    waiver_id: str = Field(pattern=r"^rw-[0-9a-f]{24}$")
    waiver_kind: Literal["semantic_gate"] = "semantic_gate"
    #: The exact scope. A waiver naming another run or another snapshot is refused outright.
    translation_run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    snapshot_id: str = Field(min_length=1)
    snapshot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_gate_policy_version: str = Field(min_length=1)
    job_count: int = Field(ge=1)
    #: The semantic state this waiver was authorized against, re-derived and compared at release.
    judged_pass: int = Field(ge=0)
    judged_not_pass: Literal[0] = 0
    not_judged: int = Field(ge=1)
    accepted: int = Field(ge=1)
    qa_rejected: Literal[0] = 0
    reason: str = Field(min_length=1, max_length=500)
    decided_by: Literal["user"] = "user"
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def counts_describe_one_run(self) -> "ReleaseSemanticWaiver":
        if self.judged_pass + self.judged_not_pass + self.not_judged != self.job_count:
            raise ValueError("every job must be counted exactly once by its semantic state")
        if self.accepted + self.qa_rejected != self.job_count:
            raise ValueError("a waiver only describes a fully accepted run")
        return self


class ReleaseSemanticWaiverReference(ContractModel):
    """What a release states, in its own bytes, about the coverage it was published without."""

    waiver_id: str = Field(pattern=r"^rw-[0-9a-f]{24}$")
    waiver_file: Path
    waiver_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_gate_policy_version: str = Field(min_length=1)
    judged_pass: int = Field(ge=0)
    not_judged: int = Field(ge=1)
    reason: str = Field(min_length=1)
    decided_by: Literal["user"] = "user"
    #: Stated in the release's own words so no reader has to infer it from the counts.
    semantic_coverage: Literal["waived_not_judged"] = "waived_not_judged"


class ReleaseManifest(ContractModel):
    """The content-addressed index of one release directory."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    release_assembly_engine_version: str = Field(
        pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
    )
    release_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    book_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    source_translation_run: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    snapshot_id: str = Field(min_length=1)
    snapshot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    book_metadata_file: Path | None = None
    book_metadata_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    terminology_file: Path | None = None
    terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    terminology_version: str | None = None
    chapter_title_artifact_id: str | None = Field(default=None, pattern=r"^ct-[0-9a-f]{24}$")
    chapter_title_artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    #: Present only on a release published under an explicit semantic-gate waiver; omitted, not
    #: null, everywhere else, so every release published before waivers existed keeps its bytes.
    semantic_waiver: ReleaseSemanticWaiverReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    chapter_count: int = Field(ge=1)
    chunk_count: int = Field(ge=1)
    accepted_translation_count: int = Field(ge=1)
    paragraph_count: int = Field(ge=1)
    word_count: int = Field(ge=0)
    chapters: list[ReleaseChapterRecord]
    artifacts: list[ReleaseArtifactReference]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def index_is_complete_and_consistent(self) -> "ReleaseManifest":
        if len(self.chapters) != self.chapter_count:
            raise ValueError("chapter_count must equal the number of chapter records")
        if [chapter.chapter_sequence for chapter in self.chapters] != list(
            range(1, self.chapter_count + 1)
        ):
            raise ValueError("chapter sequences must be contiguous and ordered")
        if sum(chapter.chunk_count for chapter in self.chapters) != self.chunk_count:
            raise ValueError("chunk_count must equal the sum of the chapters' chunk counts")
        if self.accepted_translation_count != self.chunk_count:
            raise ValueError("every released chunk must be an accepted translation")
        if sum(chapter.paragraph_count for chapter in self.chapters) != self.paragraph_count:
            raise ValueError("paragraph_count must equal the sum of the chapters' paragraphs")
        files = [artifact.file for artifact in self.artifacts]
        if len(files) != len(set(files)):
            raise ValueError("artifact files must be unique")
        if sorted(files) != files:
            raise ValueError("artifacts must be ordered by path")
        indexed = {artifact.file: artifact for artifact in self.artifacts}
        for chapter in self.chapters:
            artifact = indexed.get(chapter.file)
            if artifact is None or artifact.sha256 != chapter.sha256:
                raise ValueError("every chapter file must be indexed with a matching hash")
            if artifact.role is not ReleaseArtifactRole.CHAPTER:
                raise ValueError("a chapter file must be indexed as a chapter artifact")
        roles = [artifact.role for artifact in self.artifacts]
        if roles.count(ReleaseArtifactRole.BOOK) != 1:
            raise ValueError("a release indexes exactly one assembled book")
        if roles.count(ReleaseArtifactRole.CHAPTER) != self.chapter_count:
            raise ValueError("a release indexes exactly one file per chapter")
        for present, role in (
            (self.book_metadata_sha256 is not None, ReleaseArtifactRole.METADATA),
            (self.terminology_sha256 is not None, ReleaseArtifactRole.TERMINOLOGY),
        ):
            if present != (role in roles):
                raise ValueError(f"the {role.value} reference and its artifact must agree")
        if (self.book_metadata_file is None) != (self.book_metadata_sha256 is None):
            raise ValueError("book metadata file and hash must be present together")
        if (self.terminology_file is None) != (self.terminology_sha256 is None):
            raise ValueError("terminology file and hash must be present together")
        if (self.chapter_title_artifact_id is None) != (self.chapter_title_artifact_sha256 is None):
            raise ValueError("a chapter title artifact reference requires its ID and hash")
        if self.chapter_title_artifact_id is None and any(
            chapter.heading_source is ReleaseHeadingSource.TITLE_TARGET_ARTIFACT
            for chapter in self.chapters
        ):
            raise ValueError("an artifact heading requires the artifact to be referenced")
        return self


class ReleaseChunkLineage(ContractModel):
    """Everything a third party needs to re-verify one released chunk.

    It deliberately holds no prose and no provider prompt or response: the translated bytes are
    named and hashed, never quoted.
    """

    chapter_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    chapter_sequence: int = Field(ge=1)
    chunk_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    chunk_sequence: int = Field(ge=1)
    paragraph_offset: int = Field(ge=1)
    job_id: str = Field(min_length=1)
    source_file: Path
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_paragraph_count: int = Field(ge=1)
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_file: Path
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_paragraph_count: int = Field(ge=1)
    acceptance_status: TranslationAcceptanceStatus
    acceptance_origin: TranslationAcceptanceOrigin
    acceptance_evidence_file: Path
    acceptance_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    attempt_file: Path
    attempt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: str | None = None
    model: str | None = None
    provider_resolution: ReleaseProviderResolution
    provider_attempts: int = Field(ge=0)
    terminology_qa_version: str | None = None
    structural_qa_version: str | None = None
    lineage_source_run_id: str | None = None
    lineage_source_job_id: str | None = None

    @model_validator(mode="after")
    def lineage_is_self_consistent(self) -> "ReleaseChunkLineage":
        if self.acceptance_status is not TranslationAcceptanceStatus.ACCEPTED:
            raise ValueError("a release only carries accepted translations")
        if (self.provider_resolution is ReleaseProviderResolution.UNRESOLVED) != (
            self.provider is None
        ):
            raise ValueError("an unresolved provider must be recorded as unresolved, and only then")
        if (self.lineage_source_run_id is None) != (self.lineage_source_job_id is None):
            raise ValueError("a lineage source run and job must be present together")
        return self


class ReleaseLineageDocument(ContractModel):
    """The complete chunk-level provenance of one release."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    release_assembly_engine_version: str = Field(
        pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
    )
    release_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    book_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    source_translation_run: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    snapshot_id: str = Field(min_length=1)
    snapshot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_count: int = Field(ge=1)
    chunks: list[ReleaseChunkLineage]

    @model_validator(mode="after")
    def every_chunk_is_present_once_and_in_order(self) -> "ReleaseLineageDocument":
        if len(self.chunks) != self.chunk_count:
            raise ValueError("chunk_count must equal the number of lineage records")
        identities = [chunk.chunk_id for chunk in self.chunks]
        if len(identities) != len(set(identities)):
            raise ValueError("chunk lineage identities must be unique")
        ordering = [(chunk.chapter_sequence, chunk.chunk_sequence) for chunk in self.chunks]
        if ordering != sorted(ordering):
            raise ValueError("chunk lineage must follow chapter and chunk order")
        return self


class ReleaseTranslationReport(ContractModel):
    """What the run that produced this release actually cost and how it was resolved."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    release_assembly_engine_version: str = Field(
        pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
    )
    release_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    snapshot_id: str = Field(min_length=1)
    snapshot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    queue_engine_version: str
    run_status: str
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    chapter_count: int = Field(ge=1)
    total_chunks: int = Field(ge=1)
    accepted_chunks: int = Field(ge=1)
    qa_rejected_chunks: Literal[0] = 0
    provider_attempts: int = Field(ge=0)
    abandoned_attempts: int = Field(ge=0)
    provider_repairs: int = Field(ge=0)
    provider_recoveries: int = Field(ge=0)
    repair_escalations: int = Field(ge=0)
    structural_repairs: int = Field(ge=0)
    qa_reevaluations: int = Field(ge=0)
    cross_run_reuses: int = Field(ge=0)
    historical_carry_forwards: int = Field(ge=0)
    acceptance_origins: dict[str, int]
    providers: dict[str, int]
    models: dict[str, int]
    terminology_qa_versions: list[str]
    structural_qa_versions: list[str]
    prompt_version: str
    glossary_version: str
    style_guide_version: str
    terminology_version: str | None = None
    source_anomalies_version: str | None = None
    open_questions_version: str | None = None
    #: The semantic dimension of the run this release published, stated rather than implied.
    #: A run under no gate policy simply has none of this judged, and needs no waiver for that.
    semantic_gate_policy_version: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    semantic_judged_pass: int = Field(default=0, ge=0)
    semantic_not_judged: int = Field(default=0, ge=0)
    semantic_waiver: ReleaseSemanticWaiverReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    final_status: Literal["released"] = "released"

    @model_validator(mode="after")
    def totals_agree(self) -> "ReleaseTranslationReport":
        if self.accepted_chunks != self.total_chunks:
            raise ValueError("a release report only describes a fully accepted run")
        if (
            self.semantic_gate_policy_version is not None
            and self.semantic_not_judged
            and self.semantic_waiver is None
        ):
            raise ValueError("unjudged work is released only under a recorded waiver")
        if self.semantic_waiver is not None:
            if self.semantic_waiver.not_judged != self.semantic_not_judged:
                raise ValueError("the waiver and the report must agree on what went unjudged")
            if self.semantic_waiver.judged_pass != self.semantic_judged_pass:
                raise ValueError("the waiver and the report must agree on what was judged")
        if sum(self.acceptance_origins.values()) != self.total_chunks:
            raise ValueError("acceptance origins must account for every chunk")
        if sorted(self.acceptance_origins) != list(self.acceptance_origins):
            raise ValueError("acceptance origins must be ordered by key")
        for counter in (self.providers, self.models):
            if sorted(counter) != list(counter):
                raise ValueError("provider and model counters must be ordered by key")
        for versions in (self.terminology_qa_versions, self.structural_qa_versions):
            if sorted(versions) != versions or len(set(versions)) != len(versions):
                raise ValueError("QA version inventories must be sorted and distinct")
        return self
