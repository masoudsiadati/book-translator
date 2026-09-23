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
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationRun,
    TranslationRunStatus,
    TranslationSnapshot,
    TranslationSnapshotStatus,
)
from book_translator.models.progress import Progress
from book_translator.paths import NARRATIVE_KINDS, ProjectPaths


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


def _canonical_counts(
    project_root: Path,
    book: str | None = None,
) -> tuple[int | None, ...]:
    paths = ProjectPaths(project_root, book)
    toc_path = paths.toc
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
    chunked = 0
    total_chunks = 0
    translated = 0
    approved = 0
    failed_chunks = 0
    translation_snapshots = 0
    frozen_translation_snapshots = 0
    newest_snapshot_chapters = 0
    newest_snapshot_chunks = 0
    translation_runs = 0
    ready_runs = 0
    active_runs = 0
    queued_jobs = 0
    queued_pending = 0
    queued_claimed = 0
    queued_running = 0
    queued_succeeded = 0
    queued_failed = 0
    queued_retry_pending = 0
    prompt_packages_rendered = 0
    prompt_packages_valid = 0
    prompt_packages_invalid = 0
    provider_attempts = 0
    successful_attempts = 0
    failed_attempts = 0
    ambiguous_attempts = 0
    chapters_path = paths.chapters
    if chapters_path.is_dir():
        for metadata_path in chapters_path.glob("*/metadata.yaml"):
            try:
                import yaml

                metadata = ChapterMetadata.model_validate(
                    yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
                )
                if metadata.kind in NARRATIVE_KINDS:
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
                        ChapterStatus.CHUNKED,
                        ChapterStatus.TRANSLATING,
                        ChapterStatus.TRANSLATED,
                        ChapterStatus.REVIEW_REQUIRED,
                        ChapterStatus.REVIEWED,
                        ChapterStatus.APPROVED,
                    }:
                        chunked += 1
                    total_chunks += metadata.chunk_count
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
    runs_root = paths.translation_runs
    if runs_root.is_dir():
        for run_path in runs_root.glob("*/run.yaml"):
            try:
                import yaml

                run = TranslationRun.model_validate(
                    yaml.safe_load(run_path.read_text(encoding="utf-8"))
                )
                translation_runs += 1
                ready_runs += run.status is TranslationRunStatus.READY
                active_runs += run.status is TranslationRunStatus.ACTIVE
                queued_jobs += run.job_count
                queued_pending += run.pending_count
                queued_claimed += run.claimed_count
                queued_running += run.running_count
                queued_succeeded += run.succeeded_count
                queued_failed += run.failed_count
                queued_retry_pending += run.retry_pending_count
                from book_translator.prompt_packaging import list_prompt_packages

                packages = list_prompt_packages(run_path.parent, project_root=project_root)
                prompt_packages_rendered += sum(item.package is not None for item in packages)
                prompt_packages_valid += sum(item.validation_result == "valid" for item in packages)
                prompt_packages_invalid += sum(
                    item.validation_result == "invalid" for item in packages
                )
                for attempt_path in run_path.parent.glob("jobs/*/attempts/*/attempt.yaml"):
                    attempt = TranslationAttempt.model_validate(
                        yaml.safe_load(attempt_path.read_text(encoding="utf-8"))
                    )
                    provider_attempts += 1
                    successful_attempts += attempt.status is TranslationAttemptStatus.SUCCEEDED
                    failed_attempts += attempt.status is TranslationAttemptStatus.FAILED
                    ambiguous_attempts += attempt.status is TranslationAttemptStatus.AMBIGUOUS
            except (OSError, ValidationError, ValueError):
                continue
    newest: TranslationSnapshot | None = None
    snapshot_root = paths.translation_snapshots
    if snapshot_root.is_dir():
        for snapshot_path in snapshot_root.glob("*/snapshot.yaml"):
            try:
                import yaml

                snapshot = TranslationSnapshot.model_validate(
                    yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
                )
                translation_snapshots += 1
                frozen_translation_snapshots += snapshot.status is TranslationSnapshotStatus.FROZEN
                if newest is None or snapshot.created_at > newest.created_at:
                    newest = snapshot
            except (OSError, ValidationError, ValueError):
                continue
    if newest is not None:
        newest_snapshot_chapters = newest.selected_chapter_count
        newest_snapshot_chunks = newest.selected_chunk_count
    if chapters_path.is_dir():
        for metadata_path in chapters_path.glob("*/chunks/*/metadata.yaml"):
            try:
                import yaml

                chunk_metadata = ChunkMetadata.model_validate(
                    yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
                )
                failed_chunks += chunk_metadata.status is ChunkStatus.FAILED
            except (OSError, ValidationError, ValueError):
                continue
    return (
        detected,
        extracted,
        normalized,
        chunked,
        total_chunks,
        translated,
        approved,
        failed_chunks,
        translation_snapshots,
        frozen_translation_snapshots,
        newest_snapshot_chapters,
        newest_snapshot_chunks,
        translation_runs,
        ready_runs,
        active_runs,
        queued_jobs,
        queued_pending,
        queued_claimed,
        queued_running,
        queued_succeeded,
        queued_failed,
        queued_retry_pending,
        prompt_packages_rendered,
        prompt_packages_valid,
        prompt_packages_invalid,
        provider_attempts,
        successful_attempts,
        failed_attempts,
        ambiguous_attempts,
    )


def render_status(
    config: BookConfig,
    progress: Progress,
    console: Console,
    project_root: Path = Path("."),
    book: str | None = None,
) -> None:
    (
        canonical_detected,
        extracted,
        normalized,
        chunked,
        total_chunks,
        translated,
        approved,
        failed_chunks,
        translation_snapshots,
        frozen_translation_snapshots,
        newest_snapshot_chapters,
        newest_snapshot_chunks,
        translation_runs,
        ready_runs,
        active_runs,
        queued_jobs,
        queued_pending,
        queued_claimed,
        queued_running,
        queued_succeeded,
        queued_failed,
        queued_retry_pending,
        prompt_packages_rendered,
        prompt_packages_valid,
        prompt_packages_invalid,
        provider_attempts,
        successful_attempts,
        failed_attempts,
        ambiguous_attempts,
    ) = _canonical_counts(project_root, book)
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
    table.add_row("Chunked chapters", str(chunked))
    table.add_row("Total chunks", str(total_chunks))
    table.add_row("Translated chapters", str(translated))
    table.add_row("Approved chapters", str(approved))
    table.add_row("Failed chunks", str(failed_chunks))
    table.add_row("Translation snapshots", str(translation_snapshots))
    table.add_row("Frozen translation snapshots", str(frozen_translation_snapshots))
    table.add_row("Newest snapshot chapters", str(newest_snapshot_chapters))
    table.add_row("Newest snapshot chunks", str(newest_snapshot_chunks))
    table.add_row("Translation runs", str(translation_runs))
    table.add_row("Ready translation runs", str(ready_runs))
    table.add_row("Active translation runs", str(active_runs))
    table.add_row("Queued jobs", str(queued_jobs))
    table.add_row("Pending queue jobs", str(queued_pending))
    table.add_row("Claimed queue jobs", str(queued_claimed))
    table.add_row("Running queue jobs", str(queued_running))
    table.add_row("Succeeded queue jobs", str(queued_succeeded))
    table.add_row("Failed queue jobs", str(queued_failed))
    table.add_row("Retry-pending queue jobs", str(queued_retry_pending))
    table.add_row("Prompt packages rendered", str(prompt_packages_rendered))
    table.add_row("Prompt packages valid", str(prompt_packages_valid))
    table.add_row("Prompt packages stale or invalid", str(prompt_packages_invalid))
    table.add_row("Provider attempts", str(provider_attempts))
    table.add_row("Successful provider attempts", str(successful_attempts))
    table.add_row("Failed provider attempts", str(failed_attempts))
    table.add_row("Ambiguous provider attempts", str(ambiguous_attempts))
    table.add_row("Legacy translated progress", str(len(progress.translated_chapters)))
    table.add_row("Legacy approved progress", str(len(progress.approved_chapters)))
    table.add_row("Legacy failed chunk progress", str(len(progress.failed_chunks)))
    console.print(table)
