"""Read-only lineage inspection and explicit mutable-artifact regeneration."""

import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.chapter_extraction import EXTRACTION_VERSION, load_toc
from book_translator.config import BookConfig, ProjectDataError, load_book_config, require_pdf_path
from book_translator.document_chunking.engine import (
    CHUNK_ENGINE_VERSION,
    plan_document,
    reconstruct_document,
)
from book_translator.document_chunking.models import ChunkingConfig, DocumentChunkPlan
from book_translator.document_chunking.paragraphs import parse_document
from book_translator.document_chunking.reporting import (
    build_chunking_report,
)
from book_translator.document_chunking.reporting import (
    serialize_yaml as serialize_chunking_report,
)
from book_translator.document_chunking.workflow import (
    ChunkArtifact,
    build_chunk_artifacts,
)
from book_translator.hashing import sha256_file
from book_translator.models import (
    ChapterMetadata,
    ChapterStatus,
    ChunkMetadata,
    ChunkStatus,
    SnapshotResourceKind,
    TocEntry,
    TranslationManifest,
    TranslationRun,
    TranslationSnapshot,
    TranslationSnapshotStatus,
    TranslationSnapshotValidationReport,
    VersionedResourceMetadata,
)
from book_translator.normalization.engine import NORMALIZATION_VERSION, normalize_text
from book_translator.normalization.models import NormalizationResult
from book_translator.normalization.reporting import (
    build_report as build_normalization_report,
)
from book_translator.normalization.reporting import serialize_yaml as serialize_normalization_report
from book_translator.paths import (
    NARRATIVE_KINDS,
    ProjectPaths,
    chapter_id_from_selector,
    chunk_directory_name,
    narrative_chapter_id,
)
from book_translator.translation_snapshot.identity import persisted_manifest_identity

RegenerationAction = Literal[
    "none", "replace-normalization", "replace-chunks", "replace-normalization-and-chunks"
]


class ArtifactRegenerationError(ValueError):
    """Raised when mutable lineage cannot be inspected or regenerated safely."""


@dataclass(frozen=True, slots=True)
class RegenerationPaths:
    chapter: Path
    source: Path
    cleaned: Path
    normalization_report: Path
    chunks: Path
    chunking_report: Path
    metadata: Path
    review: Path
    final: Path

    def mutable_targets(self, *, normalization: bool, chunks: bool) -> tuple[Path, ...]:
        targets: list[Path] = []
        if normalization:
            targets.extend((self.cleaned, self.normalization_report))
        if chunks:
            targets.extend((self.chunks, self.chunking_report))
        targets.append(self.metadata)
        return tuple(targets)


@dataclass(frozen=True, slots=True)
class ImmutableLineage:
    snapshot_ids: tuple[str, ...]
    current_snapshot_ids: tuple[str, ...]
    run_ids: tuple[str, ...]
    prompt_package_count: int
    attempt_count: int

    @property
    def exists(self) -> bool:
        return bool(self.snapshot_ids or self.run_ids)


@dataclass(frozen=True, slots=True)
class ChapterRegenerationPlan:
    paths: RegenerationPaths
    metadata: ChapterMetadata
    entry: TocEntry
    source_sha256: str
    source_current: bool
    normalization_current: bool
    chunking_current: bool
    stored_cleaned_sha256: str | None
    planned_cleaned_sha256: str
    cleaned_hash_would_change: bool
    stored_chunk_count: int
    planned_chunk_count: int
    chunk_hashes_would_change: bool
    normalization_result: NormalizationResult
    document_plan: DocumentChunkPlan
    chunk_artifacts: tuple[ChunkArtifact, ...]
    normalization_report_text: str
    chunking_report_text: str
    updated_metadata: ChapterMetadata
    action: RegenerationAction
    immutable_lineage: ImmutableLineage
    next_step: str
    blockers: tuple[str, ...]

    @property
    def replace_normalization(self) -> bool:
        return self.action in {"replace-normalization", "replace-normalization-and-chunks"}

    @property
    def replace_chunks(self) -> bool:
        return self.action in {"replace-chunks", "replace-normalization-and-chunks"}


@dataclass(frozen=True, slots=True)
class ArtifactRegenerationPlan:
    project_root: Path
    normalization_version: str
    chunking_config: ChunkingConfig
    chapters: tuple[ChapterRegenerationPlan, ...]
    resource_lineage_changed: bool
    book: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactRegenerationExecution:
    regenerated_ids: tuple[str, ...]
    skipped_ids: tuple[str, ...]
    normalized_ids: tuple[str, ...]
    chunked_ids: tuple[str, ...]


def _load_model[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ArtifactRegenerationError(f"invalid {path}: {exc}") from exc


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ArtifactRegenerationError(f"invalid {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArtifactRegenerationError(f"invalid {path}: expected a mapping")
    return value


def _serialize_model(value: ChapterMetadata | ChunkMetadata) -> str:
    rendered = yaml.safe_dump(
        value.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise ArtifactRegenerationError("could not serialize mutable artifact metadata")
    return rendered


def _selector_id(selector: str) -> str:
    try:
        return chapter_id_from_selector(selector)
    except ValueError as exc:
        raise ArtifactRegenerationError(str(exc)) from exc


def _project_paths(project_root: Path, book: str | None) -> ProjectPaths:
    try:
        return ProjectPaths(project_root, book)
    except ValueError as exc:
        raise ArtifactRegenerationError(str(exc)) from exc


def _selected_entries(
    project_root: Path, selectors: Iterable[str], book: str | None = None
) -> tuple[TocEntry, ...]:
    requested = tuple(_selector_id(value) for value in selectors)
    if not requested:
        raise ArtifactRegenerationError("at least one --chapter selector is required")
    if len(requested) != len(set(requested)):
        raise ArtifactRegenerationError("duplicate chapter selectors are not permitted")
    toc = load_toc(_project_paths(project_root, book).toc)
    entries: dict[str, TocEntry] = {}
    for entry in toc.entries:
        if entry.kind in NARRATIVE_KINDS:
            entries[_chapter_id(entry)] = entry
    missing = [chapter_id for chapter_id in requested if chapter_id not in entries]
    if missing:
        raise ArtifactRegenerationError(
            f"selected chapters are absent from the canonical TOC: {', '.join(missing)}"
        )
    requested_set = set(requested)
    return tuple(entry for chapter_id, entry in entries.items() if chapter_id in requested_set)


def _chapter_id(entry: TocEntry) -> str:
    try:
        return narrative_chapter_id(entry.kind, entry.number)
    except ValueError as exc:
        raise ArtifactRegenerationError(str(exc)) from exc


def _paths(project_root: Path, entry: TocEntry, book: str | None = None) -> RegenerationPaths:
    chapter = _project_paths(project_root, book).chapter_directory(_chapter_id(entry))
    return RegenerationPaths(
        chapter=chapter,
        source=chapter / "source.md",
        cleaned=chapter / "cleaned.md",
        normalization_report=chapter / "normalization-report.yaml",
        chunks=chapter / "chunks",
        chunking_report=chapter / "chunking-report.yaml",
        metadata=chapter / "metadata.yaml",
        review=chapter / "review",
        final=chapter / "final.md",
    )


def _relative(path: Path, project_root: Path) -> Path:
    try:
        return Path(path.relative_to(project_root).as_posix())
    except ValueError as exc:
        raise ArtifactRegenerationError(f"artifact path escapes the project: {path}") from exc


def _source_is_current(
    paths: RegenerationPaths,
    metadata: ChapterMetadata,
    entry: TocEntry,
    source_hash: str,
    project_root: Path,
) -> bool:
    return (
        metadata.chapter_id == _chapter_id(entry)
        and metadata.kind is entry.kind
        and metadata.number == entry.number
        and metadata.source_start_page == entry.start_page
        and metadata.source_end_page == entry.end_page
        and metadata.source_file == _relative(paths.source, project_root)
        and metadata.source_sha256 == source_hash
        and metadata.extraction_version == EXTRACTION_VERSION
    )


def _normalization_integrity(
    paths: RegenerationPaths,
    metadata: ChapterMetadata,
    source_hash: str,
) -> tuple[bool, str | None, tuple[str, ...]]:
    cleaned_exists = paths.cleaned.is_file()
    report_exists = paths.normalization_report.is_file()
    if not cleaned_exists and not report_exists and metadata.cleaned_sha256 is None:
        return False, None, ()
    if not cleaned_exists or not report_exists:
        return False, None, ("normalization artifact set is partial",)
    cleaned_hash = sha256_file(paths.cleaned)
    report = _load_mapping(paths.normalization_report)
    problems: list[str] = []
    if metadata.cleaned_sha256 != cleaned_hash:
        problems.append("chapter metadata does not match cleaned.md")
    if report.get("cleaned_sha256") != cleaned_hash:
        problems.append("normalization report does not match cleaned.md")
    if report.get("source_sha256") != source_hash:
        problems.append("normalization report source lineage is stale or corrupt")
    if report.get("chapter_id") != metadata.chapter_id:
        problems.append("normalization report chapter identity is inconsistent")
    if report.get("normalization_version") != metadata.normalization_version:
        problems.append("normalization report version is inconsistent")
    cleaned_text = paths.cleaned.read_text(encoding="utf-8")
    paragraphs = parse_document(cleaned_text)
    if metadata.cleaned_word_count != sum(item.word_count for item in paragraphs):
        problems.append("normalized word count is inconsistent")
    if metadata.cleaned_paragraph_count != len(paragraphs):
        problems.append("normalized paragraph count is inconsistent")
    if report.get("warnings") != metadata.warnings:
        problems.append("normalization warnings are inconsistent")
    return not problems, cleaned_hash, tuple(problems)


def _chunk_signature(artifacts: tuple[ChunkArtifact, ...]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            item.metadata.sequence,
            item.metadata.source_sha256,
            item.metadata.context_sha256,
            item.metadata.source_word_count,
            item.metadata.source_paragraph_count,
            item.metadata.context_word_count,
            item.metadata.context_source_start_paragraph,
            item.metadata.context_source_end_paragraph,
            item.metadata.chunk_engine_version,
            item.metadata.cleaned_source_sha256,
            item.metadata.oversize,
            tuple(item.metadata.warnings),
            item.metadata.source_file,
            item.metadata.context_file,
        )
        for item in artifacts
    )


def _existing_chunk_signature(
    paths: RegenerationPaths,
    metadata: ChapterMetadata,
    project_root: Path,
) -> tuple[bool, tuple[tuple[Any, ...], ...], tuple[str, ...]]:
    chunks_exists = paths.chunks.is_dir()
    report_exists = paths.chunking_report.is_file()
    if not chunks_exists and not report_exists and metadata.chunk_count == 0:
        return False, (), ()
    if not chunks_exists or not report_exists:
        return False, (), ("chunk artifact set is partial",)
    report = _load_mapping(paths.chunking_report)
    problems: list[str] = []
    report_count = report.get("chunk_count")
    if not isinstance(report_count, int) or report_count < 1:
        return False, (), ("chunking report has an invalid chunk count",)
    if metadata.status is not ChapterStatus.CHUNKED or metadata.chunk_count != report_count:
        problems.append("chapter metadata does not match the chunk set")
    if (
        report.get("chapter_id") != metadata.chapter_id
        or report.get("conservation_validation") is not True
        or report.get("reconstructed_sha256") != report.get("cleaned_sha256")
    ):
        problems.append("chunking report identity or conservation evidence is inconsistent")
    expected_directories = [chunk_directory_name(index) for index in range(1, report_count + 1)]
    actual_directories = sorted(path.name for path in paths.chunks.iterdir() if path.is_dir())
    if actual_directories != expected_directories:
        return False, (), ("chunk directory coverage is incomplete or contains extras",)
    signatures: list[tuple[Any, ...]] = []
    source_texts: list[str] = []
    for sequence, name in enumerate(expected_directories, start=1):
        directory = paths.chunks / name
        expected_files = {"metadata.yaml", "source.md", "context.md"}
        actual_files = {path.name for path in directory.iterdir() if path.is_file()}
        if actual_files != expected_files:
            problems.append(f"chunk {sequence:03d} artifact set is inconsistent")
            continue
        chunk = _load_model(directory / "metadata.yaml", ChunkMetadata)
        if chunk.chapter_id != metadata.chapter_id or chunk.sequence != sequence:
            problems.append(f"chunk {sequence:03d} identity is inconsistent")
        if chunk.status is not ChunkStatus.PENDING or chunk.translation_sha256 is not None:
            problems.append(f"chunk {sequence:03d} contains immutable translation state")
        if sha256_file(directory / "source.md") != chunk.source_sha256:
            problems.append(f"chunk {sequence:03d} source hash is inconsistent")
        if chunk.context_sha256 is None or sha256_file(directory / "context.md") != (
            chunk.context_sha256
        ):
            problems.append(f"chunk {sequence:03d} context hash is inconsistent")
        if chunk.cleaned_source_sha256 != report.get("cleaned_sha256"):
            problems.append(f"chunk {sequence:03d} cleaned lineage is inconsistent")
        if chunk.chunk_engine_version != report.get("chunk_engine_version"):
            problems.append(f"chunk {sequence:03d} engine lineage is inconsistent")
        if chunk.source_file != _relative(directory / "source.md", project_root):
            problems.append(f"chunk {sequence:03d} source path is noncanonical")
        if chunk.context_file != _relative(directory / "context.md", project_root):
            problems.append(f"chunk {sequence:03d} context path is noncanonical")
        source_text = (directory / "source.md").read_text(encoding="utf-8")
        source_paragraphs = parse_document(source_text)
        context_text = (directory / "context.md").read_text(encoding="utf-8")
        if (
            chunk.source_word_count != sum(item.word_count for item in source_paragraphs)
            or chunk.source_paragraph_count != len(source_paragraphs)
            or chunk.context_word_count != len(context_text.split())
        ):
            problems.append(f"chunk {sequence:03d} structural counts are inconsistent")
        source_texts.append(source_text)
        signatures.append(
            (
                chunk.sequence,
                chunk.source_sha256,
                chunk.context_sha256,
                chunk.source_word_count,
                chunk.source_paragraph_count,
                chunk.context_word_count,
                chunk.context_source_start_paragraph,
                chunk.context_source_end_paragraph,
                chunk.chunk_engine_version,
                chunk.cleaned_source_sha256,
                chunk.oversize,
                tuple(chunk.warnings),
                chunk.source_file,
                chunk.context_file,
            )
        )
    reconstructed = reconstruct_document(tuple(source_texts))
    if hashlib.sha256(reconstructed.encode("utf-8")).hexdigest() != report.get("cleaned_sha256"):
        problems.append("existing chunks do not conserve their recorded cleaned lineage")
    return not problems, tuple(signatures), tuple(problems)


def _recovery_paths(paths: RegenerationPaths) -> tuple[Path, ...]:
    names = (
        "metadata.yaml",
        "cleaned.md",
        "normalization-report.yaml",
        "chunks",
        "chunking-report.yaml",
    )
    return tuple(paths.chapter / f".regeneration-backup-{name}" for name in names)


def _local_immutable_blockers(paths: RegenerationPaths) -> tuple[str, ...]:
    blockers: list[str] = []
    if paths.final.exists():
        blockers.append("chapter final.md exists")
    if paths.review.exists():
        blockers.append("chapter review evidence exists")
    if paths.chunks.is_dir():
        for candidate in paths.chunks.rglob("*"):
            if candidate.is_file() and (
                candidate.name == "translation.md"
                or "review" in candidate.relative_to(paths.chunks).parts
            ):
                blockers.append("chunk translation or review evidence exists")
                break
    if any(path.exists() for path in _recovery_paths(paths)):
        blockers.append("unresolved regeneration recovery backup exists")
    return tuple(blockers)


def _resource_state(
    project_root: Path, config: BookConfig
) -> dict[SnapshotResourceKind, tuple[Path, str, str]]:
    resources = {
        SnapshotResourceKind.PROMPT: config.translation_prompt,
        SnapshotResourceKind.GLOSSARY: config.glossary,
        SnapshotResourceKind.STYLE_GUIDE: config.style_guide,
    }
    result: dict[SnapshotResourceKind, tuple[Path, str, str]] = {}
    for kind, configured in resources.items():
        path = configured if configured.is_absolute() else project_root / configured
        metadata = _load_model(path.with_suffix(".metadata.yaml"), VersionedResourceMetadata)
        relative = _relative(path, project_root)
        if metadata.resource_kind is not kind or metadata.resource_file != relative:
            raise ArtifactRegenerationError(f"resource metadata mismatch for {relative}")
        result[kind] = (relative, sha256_file(path), metadata.version)
    return result


def _load_immutable_manifests(
    project_root: Path, book: str | None = None
) -> tuple[tuple[str, TranslationManifest], ...]:
    root = _project_paths(project_root, book).translation_snapshots
    if not root.is_dir():
        return ()
    manifests: list[tuple[str, TranslationManifest]] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        snapshot = _load_model(directory / "snapshot.yaml", TranslationSnapshot)
        raw_manifest = _load_mapping(directory / "manifest.yaml")
        try:
            manifest = TranslationManifest.model_validate(raw_manifest)
        except ValidationError as exc:
            raise ArtifactRegenerationError(
                f"invalid {directory / 'manifest.yaml'}: {exc}"
            ) from exc
        report = _load_model(
            directory / "validation-report.yaml", TranslationSnapshotValidationReport
        )
        snapshot_id, manifest_hash = persisted_manifest_identity(raw_manifest, manifest)
        if (
            snapshot.status is TranslationSnapshotStatus.FROZEN
            and snapshot.snapshot_id == snapshot_id == directory.name
            and snapshot.manifest_sha256 == manifest_hash
            and report.valid
            and report.snapshot_id == snapshot_id
        ):
            manifests.append((snapshot_id, manifest))
        else:
            raise ArtifactRegenerationError(
                f"immutable translation snapshot is internally inconsistent: {directory.name}"
            )
    return tuple(manifests)


def _current_resource_match(
    manifest: TranslationManifest,
    resources: dict[SnapshotResourceKind, tuple[Path, str, str]],
) -> bool:
    for reference in (manifest.prompt, manifest.glossary, manifest.style_guide):
        if resources[reference.resource_kind] != (
            reference.file,
            reference.sha256,
            reference.version,
        ):
            return False
    return True


def _chapter_manifest_match(
    manifest: TranslationManifest,
    chapter_id: str,
    cleaned_hash: str,
    normalization_version: str,
    artifacts: tuple[ChunkArtifact, ...],
) -> bool:
    chapter = next((item for item in manifest.chapters if item.chapter_id == chapter_id), None)
    if chapter is None:
        return False
    if (
        chapter.cleaned_sha256 != cleaned_hash
        or chapter.normalization_version != normalization_version
        or chapter.chunk_engine_version != CHUNK_ENGINE_VERSION
        or chapter.chunk_count != len(artifacts)
    ):
        return False
    return all(
        reference.source_sha256 == artifact.metadata.source_sha256
        and reference.context_sha256 == artifact.metadata.context_sha256
        and reference.source_word_count == artifact.metadata.source_word_count
        and reference.source_paragraph_count == artifact.metadata.source_paragraph_count
        and reference.context_word_count == artifact.metadata.context_word_count
        for reference, artifact in zip(chapter.chunks, artifacts, strict=True)
    )


def _runs_by_snapshot(
    project_root: Path, book: str | None = None
) -> dict[str, list[tuple[str, Path]]]:
    root = _project_paths(project_root, book).translation_runs
    result: dict[str, list[tuple[str, Path]]] = {}
    if not root.is_dir():
        return result
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        run = _load_model(directory / "run.yaml", TranslationRun)
        result.setdefault(run.snapshot_id, []).append((run.run_id, directory))
    return result


def _immutable_lineage(
    *,
    chapter_id: str,
    cleaned_hash: str,
    normalization_version: str,
    artifacts: tuple[ChunkArtifact, ...],
    manifests: tuple[tuple[str, TranslationManifest], ...],
    resources: dict[SnapshotResourceKind, tuple[Path, str, str]],
    runs_by_snapshot: dict[str, list[tuple[str, Path]]],
) -> ImmutableLineage:
    snapshot_ids = tuple(
        snapshot_id
        for snapshot_id, manifest in manifests
        if any(chapter.chapter_id == chapter_id for chapter in manifest.chapters)
    )
    current_snapshot_ids = tuple(
        snapshot_id
        for snapshot_id, manifest in manifests
        if _current_resource_match(manifest, resources)
        and _chapter_manifest_match(
            manifest, chapter_id, cleaned_hash, normalization_version, artifacts
        )
    )
    run_entries = [
        entry for snapshot_id in snapshot_ids for entry in runs_by_snapshot.get(snapshot_id, [])
    ]
    prompt_packages = sum(
        1
        for _, directory in run_entries
        for _ in directory.glob(f"jobs/{chapter_id}-*/prompt-package/package.yaml")
    )
    attempts = sum(
        1
        for _, directory in run_entries
        for _ in directory.glob(f"jobs/{chapter_id}-*/attempts/*/attempt.yaml")
    )
    return ImmutableLineage(
        snapshot_ids=snapshot_ids,
        current_snapshot_ids=current_snapshot_ids,
        run_ids=tuple(run_id for run_id, _ in run_entries),
        prompt_package_count=prompt_packages,
        attempt_count=attempts,
    )


def _next_step(
    *,
    chapter_id: str,
    source_current: bool,
    normalization_current: bool,
    chunking_current: bool,
    lineage: ImmutableLineage,
    runs_by_snapshot: dict[str, list[tuple[str, Path]]],
) -> str:
    if not source_current:
        return "inspect-or-regenerate-extraction"
    if not normalization_current:
        return "regenerate-normalization"
    if not chunking_current:
        return "regenerate-chunks"
    if not lineage.current_snapshot_ids:
        return "create-new-snapshot"
    current_runs = [
        entry
        for snapshot_id in lineage.current_snapshot_ids
        for entry in runs_by_snapshot.get(snapshot_id, [])
    ]
    if not current_runs:
        return "create-new-run"
    current_package_count = sum(
        1
        for _, directory in current_runs
        for _ in directory.glob(f"jobs/{chapter_id}-*/prompt-package/package.yaml")
    )
    if current_package_count == 0:
        return "render-new-prompt-packages"
    return "current"


def plan_artifact_regeneration(
    *,
    project_root: Path,
    selectors: Iterable[str],
    normalization_version: str = NORMALIZATION_VERSION,
    now: Callable[[], datetime] | None = None,
    book: str | None = None,
) -> ArtifactRegenerationPlan:
    """Inspect selected chapters and build a write-free deterministic replacement plan."""
    project_root = project_root.resolve()
    if not normalization_version.strip():
        raise ArtifactRegenerationError("normalization version must not be empty")
    project_paths = _project_paths(project_root, book)
    config = load_book_config(project_paths.book_config)
    chunking_config = ChunkingConfig(
        config.chunk_target_words,
        config.chunk_max_words,
        config.previous_context_words,
    )
    toc = load_toc(project_paths.toc)
    try:
        configured_pdf = require_pdf_path(config)
    except ProjectDataError as exc:
        raise ArtifactRegenerationError(str(exc)) from exc
    pdf_path = configured_pdf if configured_pdf.is_absolute() else project_root / configured_pdf
    if toc.source_pdf_sha256 is None or sha256_file(pdf_path) != toc.source_pdf_sha256:
        raise ArtifactRegenerationError("configured PDF does not match canonical TOC provenance")
    if toc.source_pdf is not None and toc.source_pdf != _relative(pdf_path, project_root):
        raise ArtifactRegenerationError(
            "configured PDF path does not match canonical TOC provenance"
        )
    resources = _resource_state(project_root, config)
    manifests = _load_immutable_manifests(project_root, book)
    runs_by_snapshot = _runs_by_snapshot(project_root, book)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    planned: list[ChapterRegenerationPlan] = []
    for entry in _selected_entries(project_root, selectors, book):
        paths = _paths(project_root, entry, book)
        if not paths.source.is_file() or not paths.metadata.is_file():
            raise ArtifactRegenerationError(
                f"incomplete extracted chapter workspace: {paths.chapter}"
            )
        metadata = _load_model(paths.metadata, ChapterMetadata)
        source_hash = sha256_file(paths.source)
        source_current = _source_is_current(paths, metadata, entry, source_hash, project_root)
        source_text = paths.source.read_text(encoding="utf-8")
        normalization_result = normalize_text(source_text)
        planned_cleaned_hash = hashlib.sha256(
            normalization_result.cleaned_text.encode("utf-8")
        ).hexdigest()
        normalization_integrity, stored_cleaned_hash, normalization_problems = (
            _normalization_integrity(paths, metadata, source_hash)
        )
        normalization_current = (
            normalization_integrity
            and stored_cleaned_hash == planned_cleaned_hash
            and metadata.normalization_version == normalization_version
        )
        document_plan = plan_document(normalization_result.cleaned_text, chunking_config)
        artifacts = build_chunk_artifacts(
            project_root=project_root,
            metadata=metadata,
            plan=document_plan,
            cleaned_hash=planned_cleaned_hash,
            created_at=timestamp,
            book=book,
        )
        chunks_integrity, existing_signature, chunk_problems = _existing_chunk_signature(
            paths, metadata, project_root
        )
        desired_signature = _chunk_signature(artifacts)
        chunking_current = chunks_integrity and existing_signature == desired_signature
        if chunking_current:
            report = _load_mapping(paths.chunking_report)
            chunking_current = all(
                (
                    report.get("cleaned_sha256") == planned_cleaned_hash,
                    report.get("chunk_engine_version") == CHUNK_ENGINE_VERSION,
                    report.get("target_words") == chunking_config.target_words,
                    report.get("maximum_words") == chunking_config.max_words,
                    report.get("context_words") == chunking_config.context_words,
                )
            )
        blockers = [*_local_immutable_blockers(paths)]
        if metadata.status not in {
            ChapterStatus.EXTRACTED,
            ChapterStatus.NORMALIZED,
            ChapterStatus.VALIDATED,
            ChapterStatus.CHUNKED,
        }:
            blockers.append(f"chapter lifecycle status is {metadata.status.value}")
        if not source_current:
            blockers.append("extraction/source lineage is not current")
        blockers.extend(normalization_problems)
        blockers.extend(chunk_problems)
        replace_normalization = not normalization_current
        replace_chunks = not chunking_current
        if replace_normalization and replace_chunks:
            action: RegenerationAction = "replace-normalization-and-chunks"
        elif replace_normalization:
            action = "replace-normalization"
        elif replace_chunks:
            action = "replace-chunks"
        else:
            action = "none"
        normalization_report = build_normalization_report(
            chapter_id=metadata.chapter_id,
            source_sha256=source_hash,
            cleaned_sha256=planned_cleaned_hash,
            normalization_version=normalization_version,
            result=normalization_result,
            created_at=timestamp,
        )
        chunking_report = build_chunking_report(
            chapter_id=metadata.chapter_id,
            cleaned_sha256=planned_cleaned_hash,
            plan=document_plan,
            created_at=timestamp,
        )
        updates: dict[str, Any] = {"updated_at": timestamp}
        if replace_normalization:
            updates.update(
                {
                    "cleaned_file": _relative(paths.cleaned, project_root),
                    "cleaned_sha256": planned_cleaned_hash,
                    "cleaned_word_count": len(normalization_result.cleaned_text.split()),
                    "cleaned_paragraph_count": len(normalization_result.document.blocks),
                    "normalization_version": normalization_version,
                    "warnings": list(normalization_result.warnings),
                }
            )
        if replace_chunks:
            updates.update(
                {
                    "chunk_count": len(artifacts),
                    "chunk_engine_version": CHUNK_ENGINE_VERSION,
                }
            )
        if action != "none":
            updates["status"] = ChapterStatus.CHUNKED
        updated_metadata = metadata.model_copy(update=updates)
        lineage = _immutable_lineage(
            chapter_id=metadata.chapter_id,
            cleaned_hash=planned_cleaned_hash,
            normalization_version=normalization_version,
            artifacts=artifacts,
            manifests=manifests,
            resources=resources,
            runs_by_snapshot=runs_by_snapshot,
        )
        planned.append(
            ChapterRegenerationPlan(
                paths=paths,
                metadata=metadata,
                entry=entry,
                source_sha256=source_hash,
                source_current=source_current,
                normalization_current=normalization_current,
                chunking_current=chunking_current,
                stored_cleaned_sha256=stored_cleaned_hash,
                planned_cleaned_sha256=planned_cleaned_hash,
                cleaned_hash_would_change=stored_cleaned_hash != planned_cleaned_hash,
                stored_chunk_count=metadata.chunk_count,
                planned_chunk_count=len(artifacts),
                chunk_hashes_would_change=existing_signature != desired_signature,
                normalization_result=normalization_result,
                document_plan=document_plan,
                chunk_artifacts=artifacts,
                normalization_report_text=serialize_normalization_report(normalization_report),
                chunking_report_text=serialize_chunking_report(chunking_report),
                updated_metadata=updated_metadata,
                action=action,
                immutable_lineage=lineage,
                next_step=_next_step(
                    chapter_id=metadata.chapter_id,
                    source_current=source_current,
                    normalization_current=normalization_current,
                    chunking_current=chunking_current,
                    lineage=lineage,
                    runs_by_snapshot=runs_by_snapshot,
                ),
                blockers=tuple(dict.fromkeys(blockers)),
            )
        )
    resource_lineage_changed = any(
        item.immutable_lineage.snapshot_ids and not item.immutable_lineage.current_snapshot_ids
        for item in planned
    )
    return ArtifactRegenerationPlan(
        project_root,
        normalization_version,
        chunking_config,
        tuple(planned),
        resource_lineage_changed,
        book,
    )


def _write_staging(item: ChapterRegenerationPlan) -> Path:
    staging = Path(tempfile.mkdtemp(prefix=".regeneration-staging.", dir=item.paths.chapter))
    try:
        if item.replace_normalization:
            atomic_write_text(staging / "cleaned.md", item.normalization_result.cleaned_text)
            atomic_write_text(staging / "normalization-report.yaml", item.normalization_report_text)
            if sha256_file(staging / "cleaned.md") != item.planned_cleaned_sha256:
                raise ArtifactRegenerationError("staged cleaned hash verification failed")
        if item.replace_chunks:
            for artifact in item.chunk_artifacts:
                directory = staging / "chunks" / chunk_directory_name(artifact.metadata.sequence)
                atomic_write_text(directory / "source.md", artifact.source_text)
                atomic_write_text(directory / "context.md", artifact.context_text)
                atomic_write_text(directory / "metadata.yaml", artifact.metadata_text)
            atomic_write_text(staging / "chunking-report.yaml", item.chunking_report_text)
            for artifact in item.chunk_artifacts:
                directory = staging / "chunks" / chunk_directory_name(artifact.metadata.sequence)
                persisted = _load_model(directory / "metadata.yaml", ChunkMetadata)
                if persisted != artifact.metadata:
                    raise ArtifactRegenerationError("staged chunk metadata verification failed")
                if sha256_file(directory / "source.md") != artifact.metadata.source_sha256:
                    raise ArtifactRegenerationError("staged chunk source verification failed")
                if sha256_file(directory / "context.md") != artifact.metadata.context_sha256:
                    raise ArtifactRegenerationError("staged chunk context verification failed")
        atomic_write_text(staging / "metadata.yaml", _serialize_model(item.updated_metadata))
        if _load_model(staging / "metadata.yaml", ChapterMetadata) != item.updated_metadata:
            raise ArtifactRegenerationError("staged chapter metadata verification failed")
        return staging
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _backup_path(paths: RegenerationPaths, target: Path) -> Path:
    return paths.chapter / f".regeneration-backup-{target.name}"


def _remove_target(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _publish_item(
    item: ChapterRegenerationPlan,
    staging: Path,
    *,
    after_publish: Callable[[str], None] | None,
) -> None:
    targets = item.paths.mutable_targets(
        normalization=item.replace_normalization, chunks=item.replace_chunks
    )
    backups = {target: _backup_path(item.paths, target) for target in targets}
    if any(path.exists() for path in backups.values()):
        raise ArtifactRegenerationError("regeneration recovery backup already exists")
    published: list[Path] = []
    metadata = item.paths.metadata
    os.replace(metadata, backups[metadata])
    try:
        for target in targets:
            if target == metadata:
                continue
            if target.exists():
                os.replace(target, backups[target])
            os.replace(staging / target.name, target)
            published.append(target)
            if after_publish is not None:
                after_publish(target.name)
        os.replace(staging / "metadata.yaml", metadata)
        published.append(metadata)
        if after_publish is not None:
            after_publish("metadata.yaml")
    except Exception:
        for target in reversed(published):
            _remove_target(target)
        for target in reversed(targets):
            backup = backups[target]
            if backup.exists():
                os.replace(backup, target)
        raise
    for backup in backups.values():
        _remove_target(backup)


def execute_artifact_regeneration(
    plan: ArtifactRegenerationPlan,
    *,
    after_publish: Callable[[str], None] | None = None,
) -> ArtifactRegenerationExecution:
    """Apply a fully preflighted plan; immutable snapshot/run trees are never touched."""
    blocked = [item for item in plan.chapters if item.blockers]
    if blocked:
        details = "; ".join(
            f"{item.metadata.chapter_id}: {', '.join(item.blockers)}" for item in blocked
        )
        raise ArtifactRegenerationError(f"regeneration preflight blocked: {details}")
    completed: list[str] = []
    normalized: list[str] = []
    chunked: list[str] = []
    skipped = tuple(item.metadata.chapter_id for item in plan.chapters if item.action == "none")
    for item in plan.chapters:
        if item.action == "none":
            continue
        staging: Path | None = None
        try:
            staging = _write_staging(item)
            _publish_item(item, staging, after_publish=after_publish)
            if item.replace_normalization:
                normalized.append(item.metadata.chapter_id)
            if item.replace_chunks:
                chunked.append(item.metadata.chapter_id)
            completed.append(item.metadata.chapter_id)
        except Exception as exc:
            raise ArtifactRegenerationError(
                f"regeneration failed for {item.metadata.chapter_id}; completed: "
                f"{', '.join(completed) or 'none'}; prior canonical artifacts were restored: {exc}"
            ) from exc
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    return ArtifactRegenerationExecution(
        tuple(completed), skipped, tuple(normalized), tuple(chunked)
    )
