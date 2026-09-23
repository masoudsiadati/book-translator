"""Typed plans and results for the EPUB builder."""

from dataclasses import dataclass
from pathlib import Path

from book_translator.models.release_verification import ReleaseVerificationReport


@dataclass(frozen=True, slots=True)
class EpubFile:
    """One entry of the archive, already rendered.

    ``content`` is the only field carrying book text. It exists to be written into the archive and
    nowhere else: it is never logged, printed, or summarized.
    """

    path: str
    content: bytes
    compress: bool = True

    @property
    def byte_count(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class EpubChapterRecord:
    """One chapter as it appears in the built book."""

    chapter_id: str
    chapter_sequence: int
    title: str
    source_file: str
    source_sha256: str
    archive_path: str
    sha256: str
    paragraph_count: int


@dataclass(frozen=True, slots=True)
class EpubBuildPlan:
    """Everything one build would write, with nothing written yet."""

    epub_id: str
    content_sha256: str
    output_path: Path
    release_id: str
    release_path: Path
    release_content_sha256: str
    source_translation_run: str | None
    book_identifier: str
    title: str
    author: str
    language: str
    modified: str
    chapters: tuple[EpubChapterRecord, ...]
    files: tuple[EpubFile, ...]
    verification: ReleaseVerificationReport
    destination_exists: bool
    overwrite: bool

    @property
    def chapter_count(self) -> int:
        return len(self.chapters)

    @property
    def paragraph_count(self) -> int:
        return sum(record.paragraph_count for record in self.chapters)


@dataclass(frozen=True, slots=True)
class EpubBuildResult:
    """What one build produced, and what re-reading the archive proved."""

    epub_id: str
    content_sha256: str
    archive_sha256: str
    output_path: Path
    byte_count: int
    entry_count: int
    replaced: bool
    dry_run: bool
    validation: tuple[str, ...] = ()
