"""Deterministic canonical workspace paths without filesystem side effects."""

from dataclasses import dataclass
from pathlib import Path


def chapter_directory_name(number: int | None = None, *, prologue: bool = False) -> str:
    if prologue:
        if number not in (None, 0):
            raise ValueError("prologue number must be omitted or zero")
        return "000-prologue"
    if number is None or not 1 <= number <= 999:
        raise ValueError("chapter number must be between 1 and 999")
    return f"{number:03d}"


def chunk_directory_name(sequence: int) -> str:
    if not 1 <= sequence <= 999:
        raise ValueError("chunk sequence must be between 1 and 999")
    return f"{sequence:03d}"


def front_matter_directory_name(entry_id: str) -> str:
    if (
        not entry_id.startswith("front-matter-")
        or not entry_id.removeprefix("front-matter-").isdigit()
    ):
        raise ValueError("front matter entry ID must be front-matter- followed by digits")
    return entry_id


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """Derive paths rooted at a project; never create them."""

    project_root: Path

    @property
    def workspace(self) -> Path:
        return self.project_root / "workspace"

    @property
    def book_metadata(self) -> Path:
        return self.workspace / "metadata.yaml"

    @property
    def toc(self) -> Path:
        return self.workspace / "toc.yaml"

    @property
    def snapshot(self) -> Path:
        return self.workspace / "snapshot.yaml"

    def chapter(self, number: int | None = None, *, prologue: bool = False) -> Path:
        return self.workspace / "chapters" / chapter_directory_name(number, prologue=prologue)

    def front_matter(self, entry_id: str) -> Path:
        return self.workspace / "chapters" / front_matter_directory_name(entry_id)

    def chapter_metadata(self, number: int | None = None, *, prologue: bool = False) -> Path:
        return self.chapter(number, prologue=prologue) / "metadata.yaml"

    def chapter_source(self, number: int | None = None, *, prologue: bool = False) -> Path:
        return self.chapter(number, prologue=prologue) / "source.md"

    def chapter_cleaned(self, number: int | None = None, *, prologue: bool = False) -> Path:
        return self.chapter(number, prologue=prologue) / "cleaned.md"

    def chapter_normalization_report(
        self, number: int | None = None, *, prologue: bool = False
    ) -> Path:
        return self.chapter(number, prologue=prologue) / "normalization-report.yaml"

    def chapter_review(self, number: int | None = None, *, prologue: bool = False) -> Path:
        return self.chapter(number, prologue=prologue) / "review"

    def chapter_final(self, number: int | None = None, *, prologue: bool = False) -> Path:
        return self.chapter(number, prologue=prologue) / "final.md"

    def chunk(self, chapter_number: int | None, sequence: int, *, prologue: bool = False) -> Path:
        return (
            self.chapter(chapter_number, prologue=prologue)
            / "chunks"
            / chunk_directory_name(sequence)
        )

    def chunk_metadata(
        self, chapter_number: int | None, sequence: int, *, prologue: bool = False
    ) -> Path:
        return self.chunk(chapter_number, sequence, prologue=prologue) / "metadata.yaml"

    def chunk_source(
        self, chapter_number: int | None, sequence: int, *, prologue: bool = False
    ) -> Path:
        return self.chunk(chapter_number, sequence, prologue=prologue) / "source.md"

    def chunk_context(
        self, chapter_number: int | None, sequence: int, *, prologue: bool = False
    ) -> Path:
        return self.chunk(chapter_number, sequence, prologue=prologue) / "context.md"

    def chunk_translation(
        self, chapter_number: int | None, sequence: int, *, prologue: bool = False
    ) -> Path:
        return self.chunk(chapter_number, sequence, prologue=prologue) / "translation.md"
