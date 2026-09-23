"""Workspace preflight and transactional publication for document chunks."""

import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.document_chunking.engine import CHUNK_ENGINE_VERSION, plan_document
from book_translator.document_chunking.models import ChunkingConfig, DocumentChunkPlan
from book_translator.document_chunking.reporting import build_chunking_report, serialize_yaml
from book_translator.hashing import sha256_file
from book_translator.models import (
    SCHEMA_VERSION,
    ChapterMetadata,
    ChapterStatus,
    ChunkMetadata,
    ChunkStatus,
)
from book_translator.paths import (
    ProjectPaths,
    chapter_id_from_selector,
    chunk_directory_name,
    is_narrative_chapter_id,
    parse_narrative_chapter_id,
)
from book_translator.status_transitions import can_transition_chapter


class ChapterChunkingError(ValueError):
    """Raised when chapter chunking cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class ChapterChunkingPaths:
    chapter: Path
    metadata: Path
    cleaned: Path
    chunks: Path
    report: Path
    final: Path
    review: Path
    backup_metadata: Path
    backup_chunks: Path
    backup_report: Path
    failed_chunks: Path
    failed_report: Path


@dataclass(frozen=True, slots=True)
class ChunkArtifact:
    metadata: ChunkMetadata
    source_text: str
    context_text: str
    metadata_text: str


@dataclass(frozen=True, slots=True)
class PlannedChapterChunking:
    paths: ChapterChunkingPaths
    metadata: ChapterMetadata
    action: str
    document_plan: DocumentChunkPlan
    cleaned_sha256: str
    artifacts: tuple[ChunkArtifact, ...]
    report_data: dict[str, Any]
    report_text: str
    updated_metadata: ChapterMetadata


@dataclass(frozen=True, slots=True)
class ChapterChunkingPlan:
    project_root: Path
    config: ChunkingConfig
    chapters: tuple[PlannedChapterChunking, ...]
    force: bool
    book: str | None = None


@dataclass(frozen=True, slots=True)
class ChunkingExecution:
    chunked_ids: tuple[str, ...]
    skipped_ids: tuple[str, ...]
    total_output_bytes: int


def _load_chapter_metadata(path: Path) -> ChapterMetadata:
    try:
        return ChapterMetadata.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ChapterChunkingError(f"invalid chapter metadata {path}: {exc}") from exc


def _load_chunk_metadata(path: Path) -> ChunkMetadata:
    try:
        return ChunkMetadata.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ChapterChunkingError(f"invalid chunk metadata {path}: {exc}") from exc


def _serialize_contract(model: ChapterMetadata | ChunkMetadata) -> str:
    rendered = yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise ChapterChunkingError("could not serialize chunking metadata")
    return rendered


def _chapter_paths(directory: Path) -> ChapterChunkingPaths:
    return ChapterChunkingPaths(
        chapter=directory,
        metadata=directory / "metadata.yaml",
        cleaned=directory / "cleaned.md",
        chunks=directory / "chunks",
        report=directory / "chunking-report.yaml",
        final=directory / "final.md",
        review=directory / "review",
        backup_metadata=directory / ".chunking-backup-metadata.yaml",
        backup_chunks=directory / ".chunking-backup-chunks",
        backup_report=directory / ".chunking-backup-report.yaml",
        failed_chunks=directory / ".chunking-failed-chunks",
        failed_report=directory / ".chunking-failed-report.yaml",
    )


def _chapters_root(project_root: Path, book: str | None) -> Path:
    try:
        return ProjectPaths(project_root, book).chapters
    except ValueError as exc:
        raise ChapterChunkingError(str(exc)) from exc


def _selector_directory(project_root: Path, selector: str, book: str | None) -> Path:
    try:
        directory = _chapters_root(project_root, book) / chapter_id_from_selector(selector)
    except ValueError as exc:
        raise ChapterChunkingError(str(exc)) from exc
    if not directory.is_dir():
        raise ChapterChunkingError(f"selected chapter workspace not found: {directory}")
    return directory


def _selected_chapters(
    project_root: Path, selector: str | None, book: str | None = None
) -> list[tuple[Path, ChapterMetadata]]:
    if selector is not None:
        directory = _selector_directory(project_root, selector, book)
        return [(directory, _load_chapter_metadata(directory / "metadata.yaml"))]
    root = _chapters_root(project_root, book)
    if not root.is_dir():
        raise ChapterChunkingError(f"chapter workspace not found: {root}")
    selected: list[tuple[Path, ChapterMetadata]] = []
    for directory in sorted(root.iterdir(), key=lambda path: path.name):
        if not directory.is_dir() or not is_narrative_chapter_id(directory.name):
            continue
        metadata = _load_chapter_metadata(directory / "metadata.yaml")
        if metadata.status in {
            ChapterStatus.NORMALIZED,
            ChapterStatus.VALIDATED,
            ChapterStatus.CHUNKED,
        }:
            selected.append((directory, metadata))
    if not selected:
        raise ChapterChunkingError(
            "no normalized prologue, numbered chapters, or epilogue were found"
        )
    return selected


def _validate_identity(
    paths: ChapterChunkingPaths, metadata: ChapterMetadata, project_root: Path
) -> None:
    expected_id = paths.chapter.name
    expected_kind, expected_number = parse_narrative_chapter_id(expected_id)
    expected_cleaned = Path(paths.cleaned.relative_to(project_root).as_posix())
    if (
        metadata.chapter_id != expected_id
        or metadata.number != expected_number
        or metadata.kind is not expected_kind
        or metadata.cleaned_file != expected_cleaned
    ):
        raise ChapterChunkingError(f"chapter identity or cleaned path mismatch for {expected_id}")


def _validate_chunking_transition(status: ChapterStatus) -> None:
    if status is ChapterStatus.NORMALIZED:
        if not can_transition_chapter(
            status, ChapterStatus.VALIDATED
        ) or not can_transition_chapter(ChapterStatus.VALIDATED, ChapterStatus.CHUNKED):
            raise ChapterChunkingError(
                "canonical normalized-to-chunked transition path is unavailable"
            )
    elif status is ChapterStatus.VALIDATED:
        if not can_transition_chapter(status, ChapterStatus.CHUNKED):
            raise ChapterChunkingError("canonical validated-to-chunked transition is unavailable")
    elif status is not ChapterStatus.CHUNKED:
        raise ChapterChunkingError(f"chapter status {status.value} does not permit chunking")


def _has_downstream(paths: ChapterChunkingPaths) -> bool:
    if paths.final.is_file() or paths.review.exists():
        return True
    if not paths.chunks.is_dir():
        return False
    for candidate in paths.chunks.rglob("*"):
        if not candidate.is_file():
            continue
        if (
            candidate.name == "translation.md"
            or "review" in candidate.relative_to(paths.chunks).parts
        ):
            return True
        if candidate.name == "metadata.yaml":
            metadata = _load_chunk_metadata(candidate)
            if (
                metadata.status is not ChunkStatus.PENDING
                or metadata.translation_sha256 is not None
            ):
                return True
    return False


def _chunk_id(chapter_id: str, sequence: int) -> str:
    return f"{chapter_id}-{sequence:03d}"


def build_chunk_artifacts(
    *,
    project_root: Path,
    metadata: ChapterMetadata,
    plan: DocumentChunkPlan,
    cleaned_hash: str,
    created_at: datetime,
    book: str | None = None,
) -> tuple[ChunkArtifact, ...]:
    chunks_root = ProjectPaths(project_root, book).chapter_directory(metadata.chapter_id) / "chunks"
    artifacts: list[ChunkArtifact] = []
    for chunk in plan.chunks:
        chunk_directory = chunks_root / chunk_directory_name(chunk.sequence)
        source_path = chunk_directory / "source.md"
        context_path = chunk_directory / "context.md"
        translation_path = chunk_directory / "translation.md"
        source_hash = hashlib.sha256(chunk.source_text.encode("utf-8")).hexdigest()
        context_hash = hashlib.sha256(chunk.context.text.encode("utf-8")).hexdigest()
        chunk_metadata = ChunkMetadata(
            schema_version=SCHEMA_VERSION,
            chunk_id=_chunk_id(metadata.chapter_id, chunk.sequence),
            chapter_id=metadata.chapter_id,
            sequence=chunk.sequence,
            source_file=Path(source_path.relative_to(project_root).as_posix()),
            source_sha256=source_hash,
            context_file=Path(context_path.relative_to(project_root).as_posix()),
            context_sha256=context_hash,
            context_word_count=chunk.context.word_count,
            context_source_start_paragraph=chunk.context.source_start_paragraph,
            context_source_end_paragraph=chunk.context.source_end_paragraph,
            translation_file=Path(translation_path.relative_to(project_root).as_posix()),
            translation_sha256=None,
            source_word_count=chunk.word_count,
            translation_word_count=None,
            source_paragraph_count=len(chunk.paragraphs),
            translation_paragraph_count=None,
            status=ChunkStatus.PENDING,
            translation_attempts=0,
            review_attempts=0,
            provider=None,
            model=None,
            prompt_version=None,
            glossary_version=None,
            chunk_engine_version=CHUNK_ENGINE_VERSION,
            cleaned_source_sha256=cleaned_hash,
            oversize=chunk.oversize,
            warnings=list(chunk.warnings),
            error=None,
            created_at=created_at,
            updated_at=created_at,
        )
        artifacts.append(
            ChunkArtifact(
                metadata=chunk_metadata,
                source_text=chunk.source_text,
                context_text=chunk.context.text,
                metadata_text=_serialize_contract(chunk_metadata),
            )
        )
    return tuple(artifacts)


def _load_report(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ChapterChunkingError(f"invalid chunking report {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ChapterChunkingError(f"invalid chunking report {path}: expected a mapping")
    return raw


def _validate_existing_chunks(
    *,
    paths: ChapterChunkingPaths,
    metadata: ChapterMetadata,
    artifacts: tuple[ChunkArtifact, ...],
    report: dict[str, Any],
    config: ChunkingConfig,
    cleaned_hash: str,
) -> None:
    if metadata.status is not ChapterStatus.CHUNKED or metadata.chunk_count != len(artifacts):
        raise ChapterChunkingError(
            f"chunked chapter metadata integrity error for {metadata.chapter_id}"
        )
    expected_report = {
        "chapter_id": metadata.chapter_id,
        "cleaned_sha256": cleaned_hash,
        "chunk_engine_version": CHUNK_ENGINE_VERSION,
        "target_words": config.target_words,
        "maximum_words": config.max_words,
        "context_words": config.context_words,
        "chunk_count": len(artifacts),
        "conservation_validation": True,
        "reconstructed_sha256": cleaned_hash,
    }
    if any(report.get(key) != value for key, value in expected_report.items()):
        raise ChapterChunkingError(f"chunking report integrity error for {metadata.chapter_id}")
    expected_directories = {chunk_directory_name(index) for index in range(1, len(artifacts) + 1)}
    actual_directories = {path.name for path in paths.chunks.iterdir() if path.is_dir()}
    if actual_directories != expected_directories:
        raise ChapterChunkingError(f"chunk directory set integrity error for {metadata.chapter_id}")
    for artifact in artifacts:
        directory = paths.chunks / chunk_directory_name(artifact.metadata.sequence)
        expected_files = {"metadata.yaml", "source.md", "context.md"}
        actual_files = {path.name for path in directory.iterdir() if path.is_file()}
        if actual_files != expected_files:
            raise ChapterChunkingError(
                f"chunk artifact set integrity error for {artifact.metadata.chunk_id}"
            )
        persisted = _load_chunk_metadata(directory / "metadata.yaml")
        if persisted.model_dump(
            exclude={"created_at", "updated_at"}
        ) != artifact.metadata.model_dump(exclude={"created_at", "updated_at"}):
            raise ChapterChunkingError(
                f"chunk metadata integrity error for {artifact.metadata.chunk_id}"
            )
        if sha256_file(directory / "source.md") != artifact.metadata.source_sha256:
            raise ChapterChunkingError(
                f"chunk source hash integrity error for {artifact.metadata.chunk_id}"
            )
        if sha256_file(directory / "context.md") != artifact.metadata.context_sha256:
            raise ChapterChunkingError(
                f"chunk context hash integrity error for {artifact.metadata.chunk_id}"
            )


def _existing_action(
    *,
    paths: ChapterChunkingPaths,
    metadata: ChapterMetadata,
    artifacts: tuple[ChunkArtifact, ...],
    config: ChunkingConfig,
    cleaned_hash: str,
    force: bool,
) -> str:
    recovery_paths = (
        paths.backup_metadata,
        paths.backup_chunks,
        paths.backup_report,
    )
    if any(path.exists() for path in recovery_paths):
        return "conflict"
    chunks_exists = paths.chunks.exists()
    report_exists = paths.report.exists()
    if force:
        if _has_downstream(paths):
            return "conflict"
        return "replace" if chunks_exists or report_exists else "chunk"
    if chunks_exists != report_exists:
        return "conflict"
    if not chunks_exists:
        return "conflict" if metadata.status is ChapterStatus.CHUNKED else "chunk"
    if not paths.chunks.is_dir():
        return "conflict"
    report = _load_report(paths.report)
    stale = (
        report.get("cleaned_sha256") != cleaned_hash
        or report.get("chunk_engine_version") != CHUNK_ENGINE_VERSION
        or report.get("target_words") != config.target_words
        or report.get("maximum_words") != config.max_words
        or report.get("context_words") != config.context_words
    )
    if stale:
        return "stale"
    _validate_existing_chunks(
        paths=paths,
        metadata=metadata,
        artifacts=artifacts,
        report=report,
        config=config,
        cleaned_hash=cleaned_hash,
    )
    return "skip"


def plan_chapter_chunking(
    *,
    project_root: Path,
    config: ChunkingConfig,
    selector: str | None = None,
    force: bool = False,
    now: Callable[[], datetime] | None = None,
    book: str | None = None,
) -> ChapterChunkingPlan:
    timestamp = (now or (lambda: datetime.now(UTC)))()
    planned: list[PlannedChapterChunking] = []
    destinations: set[Path] = set()
    for directory, metadata in _selected_chapters(project_root, selector, book):
        paths = _chapter_paths(directory)
        _validate_identity(paths, metadata, project_root)
        _validate_chunking_transition(metadata.status)
        if not paths.cleaned.is_file():
            raise ChapterChunkingError(f"cleaned chapter artifact not found: {paths.cleaned}")
        if not metadata.normalization_version:
            raise ChapterChunkingError(
                f"normalization version is missing for {metadata.chapter_id}"
            )
        cleaned_hash = sha256_file(paths.cleaned)
        if metadata.cleaned_sha256 != cleaned_hash:
            raise ChapterChunkingError(f"cleaned hash integrity error for {metadata.chapter_id}")
        cleaned_text = paths.cleaned.read_text(encoding="utf-8")
        document_plan = plan_document(cleaned_text, config)
        if (
            hashlib.sha256(document_plan.reconstructed_text.encode("utf-8")).hexdigest()
            != cleaned_hash
        ):
            raise ChapterChunkingError(
                f"conservation hash validation failed for {metadata.chapter_id}"
            )
        artifacts = build_chunk_artifacts(
            project_root=project_root,
            metadata=metadata,
            plan=document_plan,
            cleaned_hash=cleaned_hash,
            created_at=timestamp,
            book=book,
        )
        report_data = build_chunking_report(
            chapter_id=metadata.chapter_id,
            cleaned_sha256=cleaned_hash,
            plan=document_plan,
            created_at=timestamp,
        )
        action = _existing_action(
            paths=paths,
            metadata=metadata,
            artifacts=artifacts,
            config=config,
            cleaned_hash=cleaned_hash,
            force=force,
        )
        if paths.chunks in destinations:
            raise ChapterChunkingError("selected chunk output paths are not unique")
        destinations.add(paths.chunks)
        updated = metadata.model_copy(
            update={
                "chunk_count": len(artifacts),
                "status": ChapterStatus.CHUNKED,
                "chunk_engine_version": CHUNK_ENGINE_VERSION,
                "updated_at": timestamp,
            }
        )
        planned.append(
            PlannedChapterChunking(
                paths=paths,
                metadata=metadata,
                action=action,
                document_plan=document_plan,
                cleaned_sha256=cleaned_hash,
                artifacts=artifacts,
                report_data=report_data,
                report_text=serialize_yaml(report_data),
                updated_metadata=updated,
            )
        )
    return ChapterChunkingPlan(project_root, config, tuple(planned), force, book)


def _write_staging(item: PlannedChapterChunking) -> Path:
    staging = Path(tempfile.mkdtemp(prefix=".chunks.staging.", dir=item.paths.chapter))
    try:
        for artifact in item.artifacts:
            directory = staging / chunk_directory_name(artifact.metadata.sequence)
            atomic_write_text(directory / "source.md", artifact.source_text)
            atomic_write_text(directory / "context.md", artifact.context_text)
            atomic_write_text(directory / "metadata.yaml", artifact.metadata_text)
        for artifact in item.artifacts:
            directory = staging / chunk_directory_name(artifact.metadata.sequence)
            persisted = _load_chunk_metadata(directory / "metadata.yaml")
            if persisted != artifact.metadata:
                raise ChapterChunkingError("staged chunk metadata validation failed")
            if sha256_file(directory / "source.md") != artifact.metadata.source_sha256:
                raise ChapterChunkingError("staged source hash validation failed")
            if sha256_file(directory / "context.md") != artifact.metadata.context_sha256:
                raise ChapterChunkingError("staged context hash validation failed")
        return staging
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _clear_failed_evidence(paths: ChapterChunkingPaths) -> None:
    if paths.failed_chunks.exists():
        shutil.rmtree(paths.failed_chunks)
    paths.failed_report.unlink(missing_ok=True)


def _publish_replacement(item: PlannedChapterChunking, staging: Path) -> None:
    paths = item.paths
    _clear_failed_evidence(paths)
    os.replace(paths.metadata, paths.backup_metadata)
    try:
        if paths.report.exists():
            os.replace(paths.report, paths.backup_report)
        if paths.chunks.exists():
            os.replace(paths.chunks, paths.backup_chunks)
        os.replace(staging, paths.chunks)
        atomic_write_text(paths.report, item.report_text)
        atomic_write_text(paths.metadata, _serialize_contract(item.updated_metadata))
    except Exception:
        if paths.chunks.exists() and not paths.failed_chunks.exists():
            os.replace(paths.chunks, paths.failed_chunks)
        if paths.report.exists() and not paths.failed_report.exists():
            os.replace(paths.report, paths.failed_report)
        if paths.backup_chunks.exists():
            os.replace(paths.backup_chunks, paths.chunks)
        if paths.backup_report.exists():
            os.replace(paths.backup_report, paths.report)
        if paths.backup_metadata.exists():
            os.replace(paths.backup_metadata, paths.metadata)
        raise
    if paths.backup_chunks.exists():
        shutil.rmtree(paths.backup_chunks)
    paths.backup_report.unlink(missing_ok=True)
    paths.backup_metadata.unlink(missing_ok=True)


def execute_chapter_chunking(plan: ChapterChunkingPlan) -> ChunkingExecution:
    blockers = [item for item in plan.chapters if item.action in {"conflict", "stale"}]
    if blockers:
        details = ", ".join(f"{item.metadata.chapter_id} ({item.action})" for item in blockers)
        raise ChapterChunkingError(f"chunking preflight blocked: {details}")
    completed: list[str] = []
    skipped = tuple(item.metadata.chapter_id for item in plan.chapters if item.action == "skip")
    total_bytes = 0
    for item in plan.chapters:
        if item.action == "skip":
            continue
        staging: Path | None = None
        try:
            staging = _write_staging(item)
            if item.action == "replace":
                _publish_replacement(item, staging)
            else:
                os.replace(staging, item.paths.chunks)
                atomic_write_text(item.paths.report, item.report_text)
                atomic_write_text(
                    item.paths.metadata,
                    _serialize_contract(item.updated_metadata),
                    overwrite=True,
                )
            staging = None
            completed.append(item.metadata.chapter_id)
            total_bytes += sum(
                len(artifact.source_text.encode("utf-8"))
                + len(artifact.context_text.encode("utf-8"))
                + len(artifact.metadata_text.encode("utf-8"))
                for artifact in item.artifacts
            ) + len(item.report_text.encode("utf-8"))
        except Exception as exc:
            raise ChapterChunkingError(
                f"chunking failed for {item.metadata.chapter_id}; completed: "
                f"{', '.join(completed) or 'none'}; failed artifacts remain recoverable: {exc}"
            ) from exc
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    return ChunkingExecution(tuple(completed), skipped, total_bytes)
