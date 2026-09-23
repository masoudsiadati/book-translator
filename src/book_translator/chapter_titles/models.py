"""Typed plans and results for chapter-title translation."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from book_translator.models.book import TocKind
from book_translator.models.chapter_title import (
    ChapterTitleDocument,
    ChapterTitleManifest,
    ChapterTitleOrigin,
    ChapterTitleProvenance,
    ChapterTitleQAReport,
    ChapterTitleRecord,
)


@dataclass(frozen=True, slots=True)
class PlannedTitle:
    """One chapter's title as the plan sees it, before any provider is asked."""

    chapter_id: str
    chapter_sequence: int
    kind: TocKind
    number: int | None
    title_en: str
    recorded_title_fa: str
    enforced_decision_ids: tuple[str, ...]
    system_message: str
    user_message: str
    request_sha256: str

    @property
    def already_recorded(self) -> bool:
        """Whether the workspace already holds a target-language title for this chapter."""
        return bool(self.recorded_title_fa.strip())


@dataclass(frozen=True, slots=True)
class ChapterTitlePlan:
    """Everything one execution would do, with nothing sent and nothing written."""

    artifact_id: str
    artifact_digest: str
    output_path: Path
    run_id: str | None
    snapshot_id: str
    snapshot_manifest_sha256: str
    book_id: str
    source_language: str
    target_language: str
    terminology_file: Path | None
    terminology_sha256: str | None
    terminology_version: str | None
    titles: tuple[PlannedTitle, ...]
    distinct_source_titles: tuple[str, ...]
    planned_provider_calls: int
    destination_exists: bool
    overwrite: bool

    @property
    def chapter_count(self) -> int:
        return len(self.titles)


@dataclass(frozen=True, slots=True)
class TranslatedTitle:
    """One produced title and the history behind it."""

    chapter_id: str
    title_fa: str
    origin: ChapterTitleOrigin
    provider: str | None
    provider_adapter_version: str | None
    model: str | None
    provider_response_id: str | None
    translated_at: datetime | None
    deduplicated_from_chapter_id: str | None


@dataclass(frozen=True, slots=True)
class ChapterTitleArtifactFiles:
    """The three rendered files of one artifact, ready to publish."""

    document: ChapterTitleDocument
    manifest: ChapterTitleManifest
    provenance: ChapterTitleProvenance
    files: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True, slots=True)
class ChapterTitleResult:
    """What one execution produced."""

    artifact_id: str
    output_path: Path
    records: tuple[ChapterTitleRecord, ...]
    qa: ChapterTitleQAReport
    provider_calls: int
    written_files: tuple[Path, ...]
    replaced: bool
    dry_run: bool
