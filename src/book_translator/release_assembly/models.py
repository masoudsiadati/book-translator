"""Typed plans and results for release assembly."""

from dataclasses import dataclass
from pathlib import Path

from book_translator.models.release import (
    ReleaseArtifactRole,
    ReleaseLineageDocument,
    ReleaseManifest,
    ReleaseTranslationReport,
)


@dataclass(frozen=True, slots=True)
class ReleaseFile:
    """One file the plan would publish, already rendered.

    ``content`` is the only field in the stage that holds book prose. It exists to be written to
    the release directory and nowhere else: it is never logged, printed, or summarized.
    """

    relative_path: str
    role: ReleaseArtifactRole
    content: bytes


@dataclass(frozen=True, slots=True)
class ReleaseAssemblyPlan:
    """The complete answer for one run, with nothing written yet."""

    release_id: str
    output_path: Path
    run_id: str
    snapshot_id: str
    manifest: ReleaseManifest
    report: ReleaseTranslationReport
    lineage: ReleaseLineageDocument
    files: tuple[ReleaseFile, ...]
    destination_exists: bool
    overwrite: bool

    @property
    def chapter_count(self) -> int:
        return self.manifest.chapter_count

    @property
    def chunk_count(self) -> int:
        return self.manifest.chunk_count


@dataclass(frozen=True, slots=True)
class ReleaseAssemblyResult:
    """What one export actually published."""

    release_id: str
    output_path: Path
    manifest_file: Path | None
    written_files: tuple[Path, ...]
    replaced: bool
    dry_run: bool
