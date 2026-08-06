import json
from pathlib import Path

from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from book_translator.config import BookConfig, ProjectDataError
from book_translator.models import (
    ChapterMetadata,
    ChapterStatus,
    ChunkMetadata,
    ChunkStatus,
    TocDocument,
    TocKind,
)
from book_translator.models.progress import Progress


def load_progress(path: Path = Path("state/progress.json")) -> Progress:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectDataError(f"progress file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectDataError(f"could not read progress file {path}: {exc}") from exc

    try:
        return Progress.model_validate(raw)
    except ValidationError as exc:
        raise ProjectDataError(f"invalid progress data in {path}: {exc}") from exc


def _canonical_counts(project_root: Path) -> tuple[int | None, int, int, int, int, int]:
    toc_path = project_root / "workspace/toc.yaml"
    detected: int | None = None
    if toc_path.is_file():
        try:
            import yaml

            toc = TocDocument.model_validate(yaml.safe_load(toc_path.read_text(encoding="utf-8")))
            detected = sum(entry.kind is TocKind.CHAPTER for entry in toc.entries)
        except (OSError, ValidationError, ValueError):
            detected = None
    extracted = 0
    normalized = 0
    translated = 0
    approved = 0
    failed_chunks = 0
    chapters_path = project_root / "workspace/chapters"
    if chapters_path.is_dir():
        for metadata_path in chapters_path.glob("*/metadata.yaml"):
            try:
                import yaml

                metadata = ChapterMetadata.model_validate(
                    yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
                )
                if metadata.kind in {TocKind.PROLOGUE, TocKind.CHAPTER}:
                    extracted += 1
                    if metadata.status in {
                        ChapterStatus.NORMALIZED,
                        ChapterStatus.VALIDATED,
                        ChapterStatus.CHUNKED,
                        ChapterStatus.TRANSLATING,
                        ChapterStatus.TRANSLATED,
                        ChapterStatus.REVIEW_REQUIRED,
                        ChapterStatus.REVIEWED,
                        ChapterStatus.APPROVED,
                    }:
                        normalized += 1
                    if metadata.status in {
                        ChapterStatus.TRANSLATED,
                        ChapterStatus.REVIEW_REQUIRED,
                        ChapterStatus.REVIEWED,
                        ChapterStatus.APPROVED,
                    }:
                        translated += 1
                    if metadata.status is ChapterStatus.APPROVED:
                        approved += 1
            except (OSError, ValidationError, ValueError):
                continue
        for metadata_path in chapters_path.glob("*/chunks/*/metadata.yaml"):
            try:
                import yaml

                chunk_metadata = ChunkMetadata.model_validate(
                    yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
                )
                failed_chunks += chunk_metadata.status is ChunkStatus.FAILED
            except (OSError, ValidationError, ValueError):
                continue
    return detected, extracted, normalized, translated, approved, failed_chunks


def render_status(
    config: BookConfig, progress: Progress, console: Console, project_root: Path = Path(".")
) -> None:
    canonical_detected, extracted, normalized, translated, approved, failed_chunks = (
        _canonical_counts(project_root)
    )
    table = Table(title="Book Translator Status", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Book title", f"{config.title_en} / {config.title_fa}")
    table.add_row("Languages", f"{config.source_language} → {config.target_language}")
    table.add_row("Source PDF exists", "yes" if config.pdf_path.is_file() else "no")
    detected = len(progress.detected_chapters) if canonical_detected is None else canonical_detected
    table.add_row("Detected chapters", str(detected))
    table.add_row("Extracted chapters", str(extracted))
    table.add_row("Normalized chapters", str(normalized))
    table.add_row("Translated chapters", str(translated))
    table.add_row("Approved chapters", str(approved))
    table.add_row("Failed chunks", str(failed_chunks))
    table.add_row("Legacy translated progress", str(len(progress.translated_chapters)))
    table.add_row("Legacy approved progress", str(len(progress.approved_chapters)))
    table.add_row("Legacy failed chunk progress", str(len(progress.failed_chunks)))
    console.print(table)
