"""Preflight, publication, and read-only validation for translation snapshots."""

import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.book_metadata import (
    IDENTITY_FIELDS,
    BookMetadataError,
    is_placeholder,
    load_book_metadata,
)
from book_translator.config import BookConfig, load_book_config
from book_translator.document_chunking.engine import reconstruct_document
from book_translator.document_chunking.paragraphs import parse_document
from book_translator.hashing import sha256_file
from book_translator.models import (
    SCHEMA_VERSION,
    ChapterMetadata,
    ChapterStatus,
    ChunkMetadata,
    ChunkStatus,
    ReferenceContextIndex,
    SnapshotBookReference,
    SnapshotChapterReference,
    SnapshotChunkReference,
    SnapshotReferenceCorpusReference,
    SnapshotResourceKind,
    SnapshotResourceReference,
    TerminologyOccurrenceExemptionRegistry,
    TocDocument,
    TranslationManifest,
    TranslationSnapshot,
    TranslationSnapshotStatus,
    TranslationSnapshotValidationReport,
    VersionedResourceMetadata,
)
from book_translator.paths import (
    NARRATIVE_KINDS,
    ProjectPaths,
    book_workspace_of,
    chapter_id_from_selector,
    narrative_chapter_id,
)
from book_translator.status_transitions import can_transition_translation_snapshot
from book_translator.translation_snapshot.identity import (
    ManifestIdentityFormat,
    manifest_identity,
    persisted_manifest_identity,
    persisted_manifest_identity_format,
)

if TYPE_CHECKING:
    from book_translator.series_terminology.workflow import TerminologyLineage

WORKFLOW_ID = "document-translation"
MANIFEST_FILENAME = "manifest.yaml"
SNAPSHOT_FILENAME = "snapshot.yaml"
VALIDATION_REPORT_FILENAME = "validation-report.yaml"


class SnapshotError(ValueError):
    """Raised when snapshot creation or validation cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class TranslationSnapshotPlan:
    project_root: Path
    output_root: Path
    snapshot_id: str
    manifest_sha256: str
    manifest: TranslationManifest
    label: str | None
    action: str
    warnings: tuple[str, ...]
    book: str | None = None

    @property
    def output_directory(self) -> Path:
        return self.output_root / self.snapshot_id


@dataclass(frozen=True, slots=True)
class SnapshotCreationResult:
    snapshot_id: str
    path: Path
    skipped: bool
    chapter_count: int
    chunk_count: int


@dataclass(frozen=True, slots=True)
class SnapshotValidationResult:
    snapshot: TranslationSnapshot | None
    manifest: TranslationManifest | None
    report: TranslationSnapshotValidationReport
    path: Path
    lineage_current: bool
    lineage_warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ListedSnapshot:
    snapshot: TranslationSnapshot
    path: Path
    validation_state: str


def _load_model(path: Path, model: type[Any]) -> Any:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return model.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise SnapshotError(f"invalid {path.name} at {path}: {exc}") from exc


def _load_manifest(path: Path) -> tuple[dict[str, Any], TranslationManifest]:
    raw = _load_mapping(path, "translation manifest")
    try:
        return raw, TranslationManifest.model_validate(raw)
    except ValidationError as exc:
        raise SnapshotError(f"invalid {path.name} at {path}: {exc}") from exc


def _serialize_model(model: Any) -> str:
    rendered = yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise SnapshotError("could not serialize snapshot contract")
    return rendered


def _relative_file(path: Path, project_root: Path, *, must_exist: bool = True) -> Path:
    resolved = path if path.is_absolute() else project_root / path
    resolved = resolved.resolve()
    try:
        relative = resolved.relative_to(project_root.resolve())
    except ValueError as exc:
        raise SnapshotError(f"artifact path must be inside the project: {path}") from exc
    if must_exist and not resolved.is_file():
        raise SnapshotError(f"required artifact not found: {relative.as_posix()}")
    return Path(relative.as_posix())


def _absolute(relative: Path, project_root: Path) -> Path:
    if relative.is_absolute():
        raise SnapshotError(f"persisted artifact path must be project-relative: {relative}")
    resolved = (project_root / relative).resolve()
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError as exc:
        raise SnapshotError(f"persisted artifact path escapes project root: {relative}") from exc
    return resolved


def _resource_metadata_path(resource: Path) -> Path:
    return resource.with_suffix(".metadata.yaml")


# Every mutable translation input has a durable content-addressed home. A snapshot is only an
# immutable historical record if none of its inputs can be edited out from under it.
FROZEN_RESOURCE_CATEGORIES: dict[SnapshotResourceKind, str] = {
    SnapshotResourceKind.PROMPT: "prompts",
    SnapshotResourceKind.GLOSSARY: "glossaries",
    SnapshotResourceKind.STYLE_GUIDE: "style-guides",
    SnapshotResourceKind.TERMINOLOGY: "terminology",
    SnapshotResourceKind.SOURCE_ANOMALIES: "source-anomalies",
    SnapshotResourceKind.OPEN_QUESTIONS: "open-questions",
    SnapshotResourceKind.SERIES_TERMINOLOGY: "series-terminology",
    SnapshotResourceKind.REFERENCE_CORPUS: "reference-corpus",
    SnapshotResourceKind.REFERENCE_CONTEXT: "reference-context-index",
    SnapshotResourceKind.TERMINOLOGY_EXEMPTIONS: "terminology-exemptions",
}

# Resources that every snapshot must carry, so a manifest can never reference a missing input.
REQUIRED_RESOURCE_KINDS: tuple[SnapshotResourceKind, ...] = (
    SnapshotResourceKind.PROMPT,
    SnapshotResourceKind.GLOSSARY,
    SnapshotResourceKind.STYLE_GUIDE,
)

_RESOURCE_LABELS: dict[str, str] = {
    "prompt": "prompt",
    "glossary": "glossary",
    "style_guide": "style guide",
    "terminology": "terminology",
    "source_anomalies": "source anomaly",
    "open_questions": "open question",
    "series_terminology": "series terminology policy",
    "reference_corpus": "reference corpus configuration",
    "reference_context": "reference context index",
    "terminology_exemptions": "terminology occurrence exemptions",
}


def _manifest_resources(
    manifest: TranslationManifest,
) -> tuple[tuple[str, SnapshotResourceReference], ...]:
    """Return every resource reference the manifest carries, in stable declared order."""
    candidates: tuple[tuple[str, SnapshotResourceReference | None], ...] = (
        ("prompt", manifest.prompt),
        ("glossary", manifest.glossary),
        ("style_guide", manifest.style_guide),
        ("terminology", manifest.terminology),
        ("source_anomalies", manifest.source_anomalies),
        ("open_questions", manifest.open_questions),
        ("series_terminology", manifest.series_terminology),
        ("reference_corpus", manifest.reference_corpus),
        ("reference_context", manifest.reference_context),
        ("terminology_exemptions", manifest.terminology_exemptions),
    )
    return tuple((key, value) for key, value in candidates if value is not None)


def _project_paths(project_root: Path, book: str | None) -> ProjectPaths:
    try:
        return ProjectPaths(project_root, book)
    except ValueError as exc:
        raise SnapshotError(str(exc)) from exc


def _frozen_resource_file(
    kind: SnapshotResourceKind,
    content_sha256: str,
    suffix: str,
    project_root: Path,
    book: str | None = None,
) -> Path:
    category = FROZEN_RESOURCE_CATEGORIES.get(kind)
    if category is None:
        raise SnapshotError(f"no durable frozen location for resource kind {kind.value}")
    absolute = _project_paths(project_root, book).frozen_resource(category, content_sha256, suffix)
    return Path(absolute.relative_to(project_root).as_posix())


def _resource_reference(
    resource: Path,
    expected_kind: SnapshotResourceKind,
    project_root: Path,
    book: str | None = None,
) -> SnapshotResourceReference:
    relative = _relative_file(resource, project_root)
    absolute = project_root / relative
    metadata_path = _resource_metadata_path(absolute)
    metadata = _load_model(metadata_path, VersionedResourceMetadata)
    if metadata.resource_kind is not expected_kind:
        raise SnapshotError(f"resource kind mismatch for {relative.as_posix()}")
    if metadata.resource_file != relative:
        raise SnapshotError(f"resource metadata path mismatch for {relative.as_posix()}")
    resource_hash = sha256_file(absolute)
    try:
        frozen_file = _frozen_resource_file(
            expected_kind, resource_hash, absolute.suffix.lower(), project_root, book
        )
    except ValueError as exc:
        raise SnapshotError(
            f"resource {relative.as_posix()} cannot be frozen durably: {exc}"
        ) from exc
    return SnapshotResourceReference(
        resource_kind=expected_kind,
        file=relative,
        sha256=resource_hash,
        version=metadata.version,
        frozen_file=frozen_file,
    )


def _frozen_copies(manifest: TranslationManifest) -> tuple[tuple[str, Path, Path, str], ...]:
    """Pair every frozen input with (label, source, destination, expected hash)."""
    copies: list[tuple[str, Path, Path, str]] = [
        (_RESOURCE_LABELS[key], reference.file, frozen, reference.sha256)
        for key, reference in _manifest_resources(manifest)
        if (frozen := reference.frozen_file) is not None
    ]
    book = manifest.book
    if book.metadata_file is not None and book.frozen_metadata_file is not None:
        if book.metadata_sha256 is None:
            raise SnapshotError("frozen book metadata requires its hash")
        copies.append(
            ("book metadata", book.metadata_file, book.frozen_metadata_file, book.metadata_sha256)
        )
    return tuple(copies)


def _require_durable_freezing(manifest: TranslationManifest) -> None:
    """Refuse a snapshot whose mutable inputs would not survive a later config edit."""
    present = {reference.resource_kind for _, reference in _manifest_resources(manifest)}
    missing_kinds = [kind.value for kind in REQUIRED_RESOURCE_KINDS if kind not in present]
    if missing_kinds:
        raise SnapshotError(
            f"required translation resource is absent: {', '.join(sorted(missing_kinds))}"
        )
    unfrozen = [
        _RESOURCE_LABELS[key]
        for key, reference in _manifest_resources(manifest)
        if reference.frozen_file is None
    ]
    if manifest.book.metadata_file is not None and manifest.book.frozen_metadata_file is None:
        unfrozen.append("book metadata")
    if unfrozen:
        raise SnapshotError(
            "mutable translation input has no durable frozen artifact: " + ", ".join(unfrozen)
        )


@dataclass(frozen=True, slots=True)
class FrozenResourcePublication:
    label: str
    source_file: Path
    frozen_file: Path
    sha256: str
    action: Literal["publish", "reuse"]


@dataclass(frozen=True, slots=True)
class ResourceFreezePlan:
    """The durable copies the current resource generation needs, independent of publication."""

    project_root: Path
    snapshot_id: str
    publications: tuple[FrozenResourcePublication, ...]
    book: str | None = None


def _classify_frozen_copy(
    label: str, source: Path, frozen: Path, expected: str, project_root: Path
) -> FrozenResourcePublication:
    destination = _absolute(frozen, project_root)
    action: Literal["publish", "reuse"] = "publish"
    if destination.exists():
        if not destination.is_file() or sha256_file(destination) != expected:
            raise SnapshotError(f"conflicting frozen {label} resource exists")
        action = "reuse"
    return FrozenResourcePublication(label, source, frozen, expected, action)


def plan_resource_freeze(*, project_root: Path, book: str | None = None) -> ResourceFreezePlan:
    """Plan durable copies for the current generation without publishing a snapshot."""
    plan = plan_snapshot(project_root=project_root, book=book)
    return ResourceFreezePlan(
        project_root=plan.project_root,
        snapshot_id=plan.snapshot_id,
        publications=tuple(
            _classify_frozen_copy(label, source, frozen, expected, plan.project_root)
            for label, source, frozen, expected in _frozen_copies(plan.manifest)
        ),
        book=plan.book,
    )


def _publish_frozen_copies(
    copies: Iterable[tuple[str, Path, Path, str]], project_root: Path
) -> None:
    for label, source_relative, frozen_relative, expected in copies:
        source = _absolute(source_relative, project_root)
        destination = _absolute(frozen_relative, project_root)
        source_bytes = source.read_bytes()
        if hashlib.sha256(source_bytes).hexdigest() != expected:
            raise SnapshotError(f"frozen {label} resource changed after planning")
        if destination.exists():
            if destination.is_file() and sha256_file(destination) == expected:
                continue
            raise SnapshotError(f"conflicting frozen {label} resource exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(destination, source_bytes.decode("utf-8"))
        if sha256_file(destination) != expected:
            raise SnapshotError(f"frozen {label} resource publication failed")


def execute_resource_freeze(plan: ResourceFreezePlan) -> ResourceFreezePlan:
    """Publish every missing durable copy idempotently; existing matching copies are reused."""
    _publish_frozen_copies(
        (
            (item.label, item.source_file, item.frozen_file, item.sha256)
            for item in plan.publications
        ),
        plan.project_root,
    )
    return plan


def _publish_frozen_resources(plan: TranslationSnapshotPlan) -> None:
    _publish_frozen_copies(_frozen_copies(plan.manifest), plan.project_root)


def _selector_id(selector: str) -> str:
    try:
        return chapter_id_from_selector(selector)
    except ValueError as exc:
        raise SnapshotError(str(exc)) from exc


def _expected_entries(toc: TocDocument) -> list[Any]:
    return [entry for entry in toc.entries if entry.kind in NARRATIVE_KINDS]


def _entry_chapter_id(entry: Any) -> str:
    try:
        return narrative_chapter_id(entry.kind, entry.number)
    except ValueError as exc:
        raise SnapshotError("canonical TOC entry has no workspace chapter identity") from exc


def _selected_entries(
    toc: TocDocument, selectors: Iterable[str] | None
) -> tuple[Literal["full", "subset"], list[Any]]:
    expected = _expected_entries(toc)
    by_id = {_entry_chapter_id(entry): entry for entry in expected}
    if selectors is None:
        return "full", expected
    requested = [_selector_id(selector) for selector in selectors]
    if not requested:
        raise SnapshotError("at least one chapter selector is required")
    if len(requested) != len(set(requested)):
        raise SnapshotError("duplicate chapter selectors are not permitted")
    missing = [chapter_id for chapter_id in requested if chapter_id not in by_id]
    if missing:
        raise SnapshotError(f"selected chapter is absent from canonical TOC: {', '.join(missing)}")
    requested_set = set(requested)
    return "subset", [entry for entry in expected if _entry_chapter_id(entry) in requested_set]


def _load_mapping(path: Path, description: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SnapshotError(f"invalid {description} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SnapshotError(f"invalid {description} at {path}: expected a mapping")
    return value


def _chapter_reference(
    entry: Any, project_root: Path, book: str | None = None
) -> SnapshotChapterReference:
    chapter_id = _entry_chapter_id(entry)
    chapter_dir = _project_paths(project_root, book).chapter_directory(chapter_id)
    metadata = _load_model(chapter_dir / "metadata.yaml", ChapterMetadata)
    if (
        metadata.chapter_id != chapter_id
        or metadata.kind is not entry.kind
        or metadata.number != entry.number
    ):
        raise SnapshotError(f"chapter identity mismatch for {chapter_id}")
    if metadata.status is not ChapterStatus.CHUNKED:
        raise SnapshotError(f"chapter {chapter_id} is not validly chunked")
    if not metadata.cleaned_sha256 or not metadata.normalization_version:
        raise SnapshotError(f"chapter {chapter_id} has incomplete normalization lineage")
    if not metadata.chunk_engine_version or metadata.chunk_count < 1:
        raise SnapshotError(f"chapter {chapter_id} has incomplete chunk lineage")
    cleaned_relative = _relative_file(metadata.cleaned_file, project_root)
    cleaned_path = project_root / cleaned_relative
    if sha256_file(cleaned_path) != metadata.cleaned_sha256:
        raise SnapshotError(f"stale cleaned hash for chapter {chapter_id}")
    cleaned_paragraphs = parse_document(cleaned_path.read_text(encoding="utf-8"))
    if metadata.cleaned_paragraph_count != len(
        cleaned_paragraphs
    ) or metadata.cleaned_word_count != sum(item.word_count for item in cleaned_paragraphs):
        raise SnapshotError(f"cleaned structural counts are inconsistent for chapter {chapter_id}")
    report = _load_mapping(chapter_dir / "chunking-report.yaml", "chunking report")
    required_report = {
        "chapter_id": chapter_id,
        "cleaned_sha256": metadata.cleaned_sha256,
        "chunk_engine_version": metadata.chunk_engine_version,
        "chunk_count": metadata.chunk_count,
        "conservation_validation": True,
        "reconstructed_sha256": metadata.cleaned_sha256,
    }
    if any(report.get(key) != value for key, value in required_report.items()):
        raise SnapshotError(f"invalid conservation or lineage report for chapter {chapter_id}")

    chunks_root = chapter_dir / "chunks"
    expected_directories = [f"{sequence:03d}" for sequence in range(1, metadata.chunk_count + 1)]
    actual_directories = sorted(path.name for path in chunks_root.iterdir() if path.is_dir())
    if actual_directories != expected_directories:
        raise SnapshotError(f"incomplete chunk sequence for chapter {chapter_id}")
    chunks: list[SnapshotChunkReference] = []
    sources: list[str] = []
    for sequence, directory_name in enumerate(expected_directories, start=1):
        directory = chunks_root / directory_name
        chunk = _load_model(directory / "metadata.yaml", ChunkMetadata)
        if chunk.chapter_id != chapter_id or chunk.sequence != sequence:
            raise SnapshotError(f"chunk identity mismatch in chapter {chapter_id}")
        if chunk.status is not ChunkStatus.PENDING:
            raise SnapshotError(f"chunk {chunk.chunk_id} is not pending")
        if chunk.translation_sha256 is not None or (directory / "translation.md").exists():
            raise SnapshotError(f"translation artifact exists for chunk {chunk.chunk_id}")
        if (
            chunk.chunk_engine_version != metadata.chunk_engine_version
            or chunk.cleaned_source_sha256 != metadata.cleaned_sha256
        ):
            raise SnapshotError(f"stale chunk lineage for {chunk.chunk_id}")
        source_relative = _relative_file(chunk.source_file, project_root)
        context_relative = _relative_file(chunk.context_file, project_root)
        source_path = project_root / source_relative
        context_path = project_root / context_relative
        if sha256_file(source_path) != chunk.source_sha256:
            raise SnapshotError(f"stale source hash for chunk {chunk.chunk_id}")
        if not chunk.context_sha256 or sha256_file(context_path) != chunk.context_sha256:
            raise SnapshotError(f"stale context hash for chunk {chunk.chunk_id}")
        source_text = source_path.read_text(encoding="utf-8")
        source_paragraphs = parse_document(source_text)
        context_text = context_path.read_text(encoding="utf-8")
        if (
            chunk.source_paragraph_count != len(source_paragraphs)
            or chunk.source_word_count != sum(item.word_count for item in source_paragraphs)
            or chunk.context_word_count != len(context_text.split())
        ):
            raise SnapshotError(f"structural counts are inconsistent for chunk {chunk.chunk_id}")
        sources.append(source_text)
        chunks.append(
            SnapshotChunkReference(
                chunk_id=chunk.chunk_id,
                chapter_id=chunk.chapter_id,
                sequence=chunk.sequence,
                source_file=source_relative,
                source_sha256=chunk.source_sha256,
                source_word_count=chunk.source_word_count,
                source_paragraph_count=chunk.source_paragraph_count,
                context_file=context_relative,
                context_sha256=chunk.context_sha256,
                context_word_count=chunk.context_word_count,
                oversize=chunk.oversize,
                warnings=chunk.warnings,
            )
        )
    reconstructed = reconstruct_document(tuple(sources))
    if reconstructed.encode("utf-8") != cleaned_path.read_bytes():
        raise SnapshotError(f"chunk conservation failed for chapter {chapter_id}")
    return SnapshotChapterReference(
        chapter_id=metadata.chapter_id,
        kind=metadata.kind,
        number=metadata.number,
        title_en=metadata.title_en or entry.title_en,
        title_fa=metadata.title_fa or entry.title_fa,
        cleaned_file=cleaned_relative,
        cleaned_sha256=metadata.cleaned_sha256,
        normalization_version=metadata.normalization_version,
        chunk_engine_version=metadata.chunk_engine_version,
        chunk_count=metadata.chunk_count,
        chunks=chunks,
    )


def _book_reference(
    *,
    project_root: Path,
    config: BookConfig,
    source_relative: Path,
    source_hash: str,
    toc_path: Path,
    book: str | None = None,
) -> SnapshotBookReference:
    """Prefer the canonical resolved record; fall back to configuration for legacy projects."""
    paths = _project_paths(project_root, book)
    try:
        resolved = load_book_metadata(project_root, book=book)
    except BookMetadataError as exc:
        raise SnapshotError(str(exc)) from exc
    toc_file = Path(toc_path.relative_to(project_root).as_posix())
    toc_hash = sha256_file(toc_path)
    lineage = (
        {"source_pdf": source_relative, "source_pdf_sha256": source_hash}
        if config.source_format == "pdf"
        else {
            "source_document": source_relative,
            "source_document_sha256": source_hash,
            "source_format": "epub",
        }
    )
    if resolved is None:
        return SnapshotBookReference(
            book_id=config.book_id,
            title_en=config.title_en,
            title_fa=config.title_fa,
            author=config.author,
            source_language=config.source_language,
            target_language=config.target_language,
            **lineage,
            toc_file=toc_file,
            toc_sha256=toc_hash,
        )
    if config.source_format != "pdf":
        raise SnapshotError("resolved book metadata currently supports PDF sources only")
    if resolved.source_pdf_sha256 != source_hash:
        raise SnapshotError("resolved book metadata does not describe the configured source PDF")
    metadata_hash = sha256_file(paths.book_metadata)
    return SnapshotBookReference(
        book_id=resolved.book_id,
        title_en=resolved.title_en,
        title_fa=resolved.title_fa,
        author=resolved.author,
        source_language=resolved.source_language,
        target_language=resolved.target_language,
        series_name=resolved.series_name,
        series_index=resolved.series_index,
        **lineage,
        metadata_file=Path(paths.book_metadata.relative_to(project_root).as_posix()),
        metadata_sha256=metadata_hash,
        metadata_version=resolved.metadata_version,
        frozen_metadata_file=Path(
            paths.book_metadata_resource(metadata_hash).relative_to(project_root).as_posix()
        ),
        toc_file=toc_file,
        toc_sha256=toc_hash,
    )


def _build_manifest(
    *,
    project_root: Path,
    config: BookConfig,
    selectors: Iterable[str] | None,
    prompt: Path,
    glossary: Path,
    style_guide: Path,
    terminology: Path | None,
    source_anomalies: Path | None,
    open_questions: Path | None,
    book: str | None = None,
    series_lineage: "TerminologyLineage | None" = None,
    terminology_exemptions: Path | None = None,
) -> TranslationManifest:
    paths = _project_paths(project_root, book)
    toc = _load_model(paths.toc, TocDocument)
    source_relative = _relative_file(config.source_path, project_root)
    source_hash = sha256_file(project_root / source_relative)
    if config.source_format == "pdf":
        if toc.source_pdf_sha256 != source_hash:
            raise SnapshotError("source PDF provenance does not match the canonical TOC")
        if (
            toc.source_pdf is not None
            and _relative_file(toc.source_pdf, project_root) != source_relative
        ):
            raise SnapshotError("source PDF path does not match the canonical TOC")
    else:
        if toc.source != "epub_spine" or toc.source_document_sha256 != source_hash:
            raise SnapshotError("source EPUB provenance does not match the canonical TOC")
        if (
            toc.source_document is None
            or _relative_file(toc.source_document, project_root) != source_relative
        ):
            raise SnapshotError("source EPUB path does not match the canonical TOC")
    selection_mode, entries = _selected_entries(toc, selectors)
    chapters = [_chapter_reference(entry, project_root, book) for entry in entries]
    return TranslationManifest(
        schema_version=SCHEMA_VERSION,
        workflow=WORKFLOW_ID,
        selection_mode=selection_mode,
        book=_book_reference(
            project_root=project_root,
            config=config,
            source_relative=source_relative,
            source_hash=source_hash,
            toc_path=paths.toc,
            book=book,
        ),
        chapters=chapters,
        prompt=_resource_reference(prompt, SnapshotResourceKind.PROMPT, project_root, book),
        glossary=_resource_reference(glossary, SnapshotResourceKind.GLOSSARY, project_root, book),
        style_guide=_resource_reference(
            style_guide, SnapshotResourceKind.STYLE_GUIDE, project_root, book
        ),
        terminology=(
            _resource_reference(terminology, SnapshotResourceKind.TERMINOLOGY, project_root, book)
            if terminology is not None
            else None
        ),
        source_anomalies=(
            _resource_reference(
                source_anomalies, SnapshotResourceKind.SOURCE_ANOMALIES, project_root, book
            )
            if source_anomalies is not None
            else None
        ),
        open_questions=(
            _resource_reference(
                open_questions, SnapshotResourceKind.OPEN_QUESTIONS, project_root, book
            )
            if open_questions is not None
            else None
        ),
        **_series_references(series_lineage, project_root, book),
        terminology_exemptions=(
            _resource_reference(
                terminology_exemptions,
                SnapshotResourceKind.TERMINOLOGY_EXEMPTIONS,
                project_root,
                book,
            )
            if terminology_exemptions is not None
            else None
        ),
    )


def _current_exemptions(paths: ProjectPaths, project_root: Path) -> Path | None:
    """The book's occurrence exemptions, when it has adjudicated any."""
    path = paths.book_terminology_exemptions
    return Path(path.relative_to(project_root).as_posix()) if path.is_file() else None


def _verify_exemptions(manifest: TranslationManifest, project_root: Path) -> None:
    """Refuse to freeze an exemption that no longer proves against its exact occurrence."""
    reference = manifest.terminology_exemptions
    if reference is None:
        return
    from book_translator.series_terminology.policy import SeriesTerminologyError, load_policy
    from book_translator.terminology.exemptions import resolve_occurrence_exemptions
    from book_translator.terminology.registry import TerminologyError, load_registry

    if manifest.terminology is None:
        raise SnapshotError("occurrence exemptions require a terminology registry")
    exemptions = _load_model(
        _absolute(reference.file, project_root), TerminologyOccurrenceExemptionRegistry
    )
    try:
        registry = load_registry(_absolute(manifest.terminology.file, project_root))
        policy = (
            load_policy(_absolute(manifest.series_terminology.file, project_root))
            if manifest.series_terminology is not None
            else None
        )
        chunks = {
            chunk.chunk_id: chunk for chapter in manifest.chapters for chunk in chapter.chunks
        }
        for chunk_id in sorted({item.chunk_id for item in exemptions.exemptions}):
            chunk = chunks.get(chunk_id)
            if chunk is None:
                raise SnapshotError(
                    f"occurrence exemption names chunk {chunk_id} outside the snapshot"
                )
            source = _absolute(chunk.source_file, project_root).read_text(encoding="utf-8")
            resolve_occurrence_exemptions(
                exemptions, registry=registry, policy=policy, chunk_id=chunk_id, source_text=source
            )
    except (TerminologyError, SeriesTerminologyError) as exc:
        raise SnapshotError(f"occurrence exemptions cannot be frozen: {exc}") from exc


def _series_references(
    lineage: "TerminologyLineage | None", project_root: Path, book: str | None
) -> dict[str, Any]:
    """The series lineage block: policy, reference corpus identities, and the context index."""
    if lineage is None or lineage.kind != "series":
        return {}
    if (
        lineage.series_policy is None
        or lineage.reference_corpus is None
        or lineage.reference_context_index is None
        or lineage.retrieval is None
    ):
        raise SnapshotError("series terminology lineage is incomplete")
    corpus = _resource_reference(
        lineage.reference_corpus, SnapshotResourceKind.REFERENCE_CORPUS, project_root, book
    )
    return {
        "series_terminology": _resource_reference(
            lineage.series_policy, SnapshotResourceKind.SERIES_TERMINOLOGY, project_root, book
        ),
        "reference_corpus": SnapshotReferenceCorpusReference(
            **corpus.model_dump(),
            retriever_path=lineage.retrieval.retriever_path,
            retriever_sha256=lineage.retrieval.retriever_sha256,
            retriever_schema_version=lineage.retrieval.retriever_schema_version,
            corpora=lineage.retrieval.corpora,
        ),
        "reference_context": _resource_reference(
            lineage.reference_context_index,
            SnapshotResourceKind.REFERENCE_CONTEXT,
            project_root,
            book,
        ),
        "terminology_context_contract_version": lineage.context_contract_version,
    }


def _book_terminology_lineage(
    project_root: Path, book: str | None, config: BookConfig
) -> "TerminologyLineage":
    """A book-scoped workspace resolves terminology only from its own configuration."""
    # Imported here because the terminology package reaches back into snapshot validation.
    from book_translator.series_terminology.policy import SeriesTerminologyError
    from book_translator.series_terminology.retrieval import ReferenceRetrievalError
    from book_translator.series_terminology.workflow import resolve_terminology_lineage

    try:
        return resolve_terminology_lineage(project_root=project_root, book=book, config=config)
    except (SeriesTerminologyError, ReferenceRetrievalError) as exc:
        raise SnapshotError(f"terminology lineage is not ready: {exc}") from exc


def _placeholder_book_fields(book: SnapshotBookReference) -> tuple[str, ...]:
    return tuple(name for name in IDENTITY_FIELDS if is_placeholder(getattr(book, name, None)))


def plan_snapshot(
    *,
    project_root: Path,
    selectors: Iterable[str] | None = None,
    label: str | None = None,
    prompt: Path | None = None,
    glossary: Path | None = None,
    style_guide: Path | None = None,
    terminology: Path | None = None,
    source_anomalies: Path | None = None,
    open_questions: Path | None = None,
    output_root: Path | None = None,
    force: bool = False,
    book: str | None = None,
) -> TranslationSnapshotPlan:
    """Plan a snapshot of the selected book workspace; every canonical input is read from it."""
    project_root = project_root.resolve()
    paths = _project_paths(project_root, book)
    config = load_book_config(paths.book_config)
    output = (output_root or paths.translation_snapshots).resolve()
    try:
        output.relative_to(project_root)
    except ValueError as exc:
        raise SnapshotError("snapshot output root must be inside the project") from exc
    series_lineage: TerminologyLineage | None = None
    if book is None:
        # The legacy root keeps its historical fallbacks byte-for-byte.
        terminology_path = terminology or config.terminology
        if terminology_path is None and (project_root / "config/terminology.yaml").is_file():
            terminology_path = Path("config/terminology.yaml")
        questions_path = open_questions or config.terminology_questions
        if (
            questions_path is None
            and (project_root / "config/terminology-questions.yaml").is_file()
        ):
            questions_path = Path("config/terminology-questions.yaml")
        anomalies_path = (
            source_anomalies or Path("config/source-anomalies.yaml")
            if (project_root / "config/source-anomalies.yaml").is_file()
            else None
        )
    else:
        # A book-scoped workspace never probes the root registries of another book: its
        # terminology comes from its own configuration or it has none, and a series policy
        # must be proven current before it is frozen.
        series_lineage = _book_terminology_lineage(project_root, book, config)
        if series_lineage.kind == "series" and terminology is not None:
            raise SnapshotError(
                "a book with a series terminology policy freezes its derived registry; "
                "an explicit terminology override is not permitted"
            )
        terminology_path = terminology or series_lineage.terminology
        questions_path = open_questions or series_lineage.open_questions
        anomalies_path = source_anomalies
    manifest = _build_manifest(
        project_root=project_root,
        config=config,
        selectors=selectors,
        prompt=prompt or config.translation_prompt,
        glossary=glossary or config.glossary,
        style_guide=style_guide or config.style_guide,
        terminology=terminology_path,
        source_anomalies=anomalies_path,
        open_questions=questions_path,
        book=book,
        series_lineage=series_lineage,
        terminology_exemptions=_current_exemptions(paths, project_root),
    )
    _require_durable_freezing(manifest)
    _verify_exemptions(manifest, project_root)
    warnings: list[str] = []
    if manifest.selection_mode == "full":
        placeholders = _placeholder_book_fields(manifest.book)
        if placeholders:
            raise SnapshotError(
                "an authoritative full-book snapshot refuses placeholder book metadata: "
                + ", ".join(placeholders)
                + "; resolve real metadata with resolve-book-metadata --apply"
            )
        if manifest.book.metadata_file is None:
            warnings.append(
                f"book identity came from {paths.book_config.relative_to(project_root).as_posix()};"
                " no canonical resolved book metadata record exists at "
                f"{paths.book_metadata.relative_to(project_root).as_posix()}"
            )
    snapshot_id, manifest_hash = manifest_identity(manifest)
    destination = output / snapshot_id
    action = "create"
    if destination.exists():
        existing = validate_snapshot(destination, project_root=project_root, book=book)
        if existing.report.valid and existing.snapshot is not None:
            if existing.snapshot.manifest_sha256 != manifest_hash:
                raise SnapshotError("conflicting content exists at the deterministic snapshot path")
            action = "skip"
            if label != existing.snapshot.label:
                warnings.append(
                    "label differs from the existing frozen snapshot; "
                    "labels are not identity metadata"
                )
        elif force:
            action = "replace-incomplete"
        else:
            action = "conflict"
    return TranslationSnapshotPlan(
        project_root=project_root,
        output_root=output,
        snapshot_id=snapshot_id,
        manifest_sha256=manifest_hash,
        manifest=manifest,
        label=label,
        action=action,
        warnings=tuple(warnings),
        book=book,
    )


def _artifact_hash_count(manifest: TranslationManifest) -> int:
    """Count every artifact this snapshot pins: PDF, TOC, resources, and chunk lineage."""
    chunk_count = sum(chapter.chunk_count for chapter in manifest.chapters)
    resource_hashes = sum(
        2 if reference.frozen_file is not None else 1
        for _, reference in _manifest_resources(manifest)
    )
    book_hashes = 2 if manifest.book.metadata_file is not None else 0
    book_hashes += 1 if manifest.book.frozen_metadata_file is not None else 0
    return 2 + resource_hashes + book_hashes + len(manifest.chapters) + 2 * chunk_count


def _publication_models(
    plan: TranslationSnapshotPlan, timestamp: datetime
) -> tuple[TranslationSnapshot, TranslationSnapshotValidationReport]:
    chunk_count = sum(chapter.chunk_count for chapter in plan.manifest.chapters)
    if not can_transition_translation_snapshot(
        TranslationSnapshotStatus.DRAFT, TranslationSnapshotStatus.VALIDATED
    ) or not can_transition_translation_snapshot(
        TranslationSnapshotStatus.VALIDATED, TranslationSnapshotStatus.FROZEN
    ):
        raise SnapshotError("canonical snapshot publication lifecycle is unavailable")
    snapshot = TranslationSnapshot(
        schema_version=SCHEMA_VERSION,
        snapshot_id=plan.snapshot_id,
        label=plan.label,
        manifest_file=Path(
            (plan.output_directory / MANIFEST_FILENAME).relative_to(plan.project_root).as_posix()
        ),
        manifest_sha256=plan.manifest_sha256,
        book_id=plan.manifest.book.book_id,
        selection_mode=plan.manifest.selection_mode,
        selected_chapter_count=len(plan.manifest.chapters),
        selected_chunk_count=chunk_count,
        source_language=plan.manifest.book.source_language,
        target_language=plan.manifest.book.target_language,
        prompt_version=plan.manifest.prompt.version,
        glossary_version=plan.manifest.glossary.version,
        style_guide_version=plan.manifest.style_guide.version,
        terminology_version=(
            plan.manifest.terminology.version if plan.manifest.terminology is not None else None
        ),
        open_questions_version=(
            plan.manifest.open_questions.version
            if plan.manifest.open_questions is not None
            else None
        ),
        source_anomalies_version=(
            plan.manifest.source_anomalies.version
            if plan.manifest.source_anomalies is not None
            else None
        ),
        series_terminology_version=(
            plan.manifest.series_terminology.version
            if plan.manifest.series_terminology is not None
            else None
        ),
        reference_corpus_version=(
            plan.manifest.reference_corpus.version
            if plan.manifest.reference_corpus is not None
            else None
        ),
        reference_context_version=(
            plan.manifest.reference_context.version
            if plan.manifest.reference_context is not None
            else None
        ),
        terminology_exemptions_version=(
            plan.manifest.terminology_exemptions.version
            if plan.manifest.terminology_exemptions is not None
            else None
        ),
        status=TranslationSnapshotStatus.FROZEN,
        created_at=timestamp,
        frozen_at=timestamp,
        warnings=list(plan.warnings),
    )
    report = TranslationSnapshotValidationReport(
        schema_version=SCHEMA_VERSION,
        snapshot_id=plan.snapshot_id,
        valid=True,
        checks_performed=[
            "manifest_identity",
            "project_relative_paths",
            "resource_hashes_and_versions",
            "durable_frozen_resources",
            "resolved_book_metadata",
            "chapter_and_chunk_lineage",
            "chunk_sequence_and_status",
            "chunk_conservation",
        ],
        selected_chapter_count=len(plan.manifest.chapters),
        selected_chunk_count=chunk_count,
        artifact_hashes_validated=_artifact_hash_count(plan.manifest),
        conservation_checks=len(plan.manifest.chapters),
        stale_artifact_checks=len(plan.manifest.chapters) + chunk_count,
        warnings=list(plan.warnings),
        created_at=timestamp,
    )
    return snapshot, report


def create_snapshot(
    plan: TranslationSnapshotPlan,
    *,
    now: Callable[[], datetime] | None = None,
) -> SnapshotCreationResult:
    if plan.action == "conflict":
        raise SnapshotError("snapshot publication is incomplete; inspect it and rerun with --force")
    _require_durable_freezing(plan.manifest)
    _publish_frozen_resources(plan)
    chunk_count = sum(chapter.chunk_count for chapter in plan.manifest.chapters)
    if plan.action == "skip":
        return SnapshotCreationResult(
            plan.snapshot_id,
            plan.output_directory,
            True,
            len(plan.manifest.chapters),
            chunk_count,
        )
    timestamp = (now or (lambda: datetime.now(UTC)))()
    snapshot, report = _publication_models(plan, timestamp)
    plan.output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{plan.snapshot_id}.staging.", dir=plan.output_root))
    backup = plan.output_root / f".{plan.snapshot_id}.incomplete-backup"
    try:
        atomic_write_text(staging / MANIFEST_FILENAME, _serialize_model(plan.manifest))
        atomic_write_text(staging / VALIDATION_REPORT_FILENAME, _serialize_model(report))
        atomic_write_text(staging / SNAPSHOT_FILENAME, _serialize_model(snapshot))
        staged_manifest = _load_model(staging / MANIFEST_FILENAME, TranslationManifest)
        staged_snapshot = _load_model(staging / SNAPSHOT_FILENAME, TranslationSnapshot)
        staged_report = _load_model(
            staging / VALIDATION_REPORT_FILENAME, TranslationSnapshotValidationReport
        )
        staged_id, staged_hash = manifest_identity(staged_manifest)
        if (
            staged_id != plan.snapshot_id
            or staged_hash != staged_snapshot.manifest_sha256
            or not staged_report.valid
            or staged_snapshot.status is not TranslationSnapshotStatus.FROZEN
        ):
            raise SnapshotError("staged snapshot validation failed")
        if plan.action == "replace-incomplete":
            if backup.exists():
                raise SnapshotError(f"snapshot recovery backup already exists: {backup}")
            os.replace(plan.output_directory, backup)
        try:
            os.replace(staging, plan.output_directory)
        except Exception:
            if backup.exists() and not plan.output_directory.exists():
                os.replace(backup, plan.output_directory)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return SnapshotCreationResult(
        plan.snapshot_id,
        plan.output_directory,
        False,
        len(plan.manifest.chapters),
        chunk_count,
    )


def _frozen_artifact_error(
    label: str, frozen: Path, expected: str, project_root: Path
) -> str | None:
    """Prove the durable copy still exists and still holds exactly the manifested bytes."""
    absolute = _absolute(frozen, project_root)
    if not absolute.is_file() or sha256_file(absolute) != expected:
        return f"frozen {label} resource is missing or stale"
    return None


def _frozen_resource_errors(
    manifest: TranslationManifest,
    project_root: Path,
    identity_format: ManifestIdentityFormat,
    *,
    checks: list[str],
) -> list[str]:
    """Validate durable frozen bytes for every mutable input the manifest references."""
    durable_required = identity_format is ManifestIdentityFormat.FROZEN_RESOURCES_V1
    errors: list[str] = []
    for key, reference in _manifest_resources(manifest):
        label = _RESOURCE_LABELS[key]
        if reference.frozen_file is None:
            if durable_required:
                errors.append(f"frozen {label} resource reference is missing")
            continue
        error = _frozen_artifact_error(label, reference.frozen_file, reference.sha256, project_root)
        if error is not None:
            errors.append(error)
        else:
            checks.append(f"frozen_{key}_resource")
    book = manifest.book
    if book.frozen_metadata_file is not None and book.metadata_sha256 is not None:
        error = _frozen_artifact_error(
            "book metadata", book.frozen_metadata_file, book.metadata_sha256, project_root
        )
        if error is not None:
            errors.append(error)
        else:
            checks.append("frozen_book_metadata")
    elif durable_required and book.metadata_file is not None:
        errors.append("frozen book metadata resource reference is missing")
    if (
        manifest.reference_context is not None
        and manifest.reference_context.frozen_file is not None
    ):
        errors.extend(
            _frozen_package_errors(manifest.reference_context.frozen_file, project_root, checks)
        )
    return errors


def _frozen_package_errors(frozen_index: Path, project_root: Path, checks: list[str]) -> list[str]:
    """Every per-chunk reference package the frozen index names must still hash as recorded."""
    try:
        index = _load_model(_absolute(frozen_index, project_root), ReferenceContextIndex)
    except SnapshotError as exc:
        return [f"frozen reference context index is unreadable: {exc}"]
    errors = [
        f"reference package for chunk {entry.chunk_id} is missing or stale"
        for entry in index.entries
        if _frozen_artifact_error(
            "reference package", entry.package_file, entry.package_sha256, project_root
        )
        is not None
    ]
    if not errors:
        checks.append("frozen_reference_context_packages")
    return errors


def _current_terminology_fallback(
    project_root: Path,
    book: str | None,
    config: BookConfig,
    lineage: "TerminologyLineage | None",
) -> Path | None:
    if book is not None:
        return lineage.terminology if lineage is not None else None
    if config.terminology is not None:
        return config.terminology
    if (project_root / "config/terminology.yaml").is_file():
        return Path("config/terminology.yaml")
    return None


def _current_questions_fallback(
    project_root: Path,
    book: str | None,
    config: BookConfig,
    lineage: "TerminologyLineage | None",
) -> Path | None:
    if book is not None:
        return lineage.open_questions if lineage is not None else None
    if config.terminology_questions is not None:
        return config.terminology_questions
    if (project_root / "config/terminology-questions.yaml").is_file():
        return Path("config/terminology-questions.yaml")
    return None


def _resolve_snapshot_path(value: str | Path, project_root: Path, book: str | None = None) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or len(candidate.parts) > 1:
        return candidate.resolve()
    return _project_paths(project_root, book).translation_snapshots / candidate


def validate_snapshot(
    snapshot_id_or_path: str | Path,
    *,
    project_root: Path,
    now: Callable[[], datetime] | None = None,
    book: str | None = None,
) -> SnapshotValidationResult:
    """Validate historical integrity, then current lineage against the snapshot's own workspace.

    A manifest records the workspace it was frozen from through its project-relative paths, so
    lineage is rebuilt from that workspace even when the caller selects none; an explicit `book`
    that names a different workspace is refused rather than silently compared against it.
    """
    project_root = project_root.resolve()
    path = _resolve_snapshot_path(snapshot_id_or_path, project_root, book)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    errors: list[str] = []
    snapshot: TranslationSnapshot | None = None
    manifest: TranslationManifest | None = None
    checks: list[str] = []
    lineage_warnings: list[str] = []
    lineage_current = False
    try:
        snapshot = _load_model(path / SNAPSHOT_FILENAME, TranslationSnapshot)
        raw_manifest, manifest = _load_manifest(path / MANIFEST_FILENAME)
        persisted_report = _load_model(
            path / VALIDATION_REPORT_FILENAME, TranslationSnapshotValidationReport
        )
        checks.append("contracts")
        snapshot_id, manifest_hash = persisted_manifest_identity(raw_manifest, manifest)
        if snapshot.snapshot_id != snapshot_id or path.name != snapshot_id:
            errors.append("snapshot identity mismatch")
        if snapshot.manifest_sha256 != manifest_hash:
            errors.append("manifest hash mismatch")
        if snapshot.status is not TranslationSnapshotStatus.FROZEN or snapshot.frozen_at is None:
            errors.append("snapshot is not frozen")
        manifest_chunk_count = sum(chapter.chunk_count for chapter in manifest.chapters)
        if (
            snapshot.selected_chapter_count != len(manifest.chapters)
            or snapshot.selected_chunk_count != manifest_chunk_count
            or persisted_report.snapshot_id != snapshot.snapshot_id
            or not persisted_report.valid
        ):
            errors.append("snapshot counts or persisted validation report mismatch")
        expected_manifest_path = Path(
            (path / MANIFEST_FILENAME).relative_to(project_root).as_posix()
        )
        if snapshot.manifest_file != expected_manifest_path:
            errors.append("manifest path mismatch")
        checks.append("manifest_identity")
        identity_format = persisted_manifest_identity_format(raw_manifest)
        errors.extend(
            _frozen_resource_errors(manifest, project_root, identity_format, checks=checks)
        )
        recorded_book = book_workspace_of(manifest.book.toc_file)
        if book is not None and recorded_book != book:
            errors.append(
                f"snapshot was frozen from book workspace {recorded_book or 'legacy'}, not {book}"
            )
        if not errors:
            try:
                config = load_book_config(_project_paths(project_root, recorded_book).book_config)
                rebuilt_lineage = (
                    _book_terminology_lineage(project_root, recorded_book, config)
                    if recorded_book is not None
                    else None
                )
                rebuilt = _build_manifest(
                    project_root=project_root,
                    config=config,
                    book=recorded_book,
                    series_lineage=rebuilt_lineage,
                    selectors=(
                        None
                        if manifest.selection_mode == "full"
                        else [chapter.chapter_id for chapter in manifest.chapters]
                    ),
                    prompt=manifest.prompt.file,
                    glossary=manifest.glossary.file,
                    style_guide=manifest.style_guide.file,
                    terminology=(
                        manifest.terminology.file
                        if manifest.terminology is not None
                        else _current_terminology_fallback(
                            project_root, recorded_book, config, rebuilt_lineage
                        )
                    ),
                    open_questions=(
                        manifest.open_questions.file
                        if manifest.open_questions is not None
                        else _current_questions_fallback(
                            project_root, recorded_book, config, rebuilt_lineage
                        )
                    ),
                    source_anomalies=(
                        manifest.source_anomalies.file
                        if manifest.source_anomalies is not None
                        else None
                    ),
                    terminology_exemptions=(
                        manifest.terminology_exemptions.file
                        if manifest.terminology_exemptions is not None
                        else _current_exemptions(
                            _project_paths(project_root, recorded_book), project_root
                        )
                    ),
                )
                lineage_current = rebuilt.model_dump(exclude={"selection_mode"}) == (
                    manifest.model_dump(exclude={"selection_mode"})
                )
                if not lineage_current:
                    lineage_warnings.append(
                        "canonical artifacts or resources no longer match this historical lineage"
                    )
            except (SnapshotError, OSError, ValueError) as exc:
                lineage_warnings.append(f"canonical lineage is not current: {exc}")
        checks.extend(["historical_integrity", "current_artifact_lineage", "resource_versions"])
    except (SnapshotError, OSError, ValueError) as exc:
        errors.append(str(exc))
    chapter_count = len(manifest.chapters) if manifest else 0
    chunk_count = sum(chapter.chunk_count for chapter in manifest.chapters) if manifest else 0
    report = TranslationSnapshotValidationReport(
        schema_version=SCHEMA_VERSION,
        snapshot_id=snapshot.snapshot_id if snapshot else path.name,
        valid=not errors,
        checks_performed=checks,
        selected_chapter_count=chapter_count,
        selected_chunk_count=chunk_count,
        artifact_hashes_validated=(
            _artifact_hash_count(manifest) if manifest and not errors and lineage_current else 0
        ),
        conservation_checks=chapter_count if not errors and lineage_current else 0,
        stale_artifact_checks=chapter_count + chunk_count,
        warnings=lineage_warnings,
        errors=errors,
        created_at=timestamp,
    )
    return SnapshotValidationResult(
        snapshot,
        manifest,
        report,
        path,
        lineage_current,
        tuple(lineage_warnings),
    )


def list_snapshots(*, project_root: Path, book: str | None = None) -> tuple[ListedSnapshot, ...]:
    root = _project_paths(project_root.resolve(), book).translation_snapshots
    if not root.is_dir():
        return ()
    listed: list[ListedSnapshot] = []
    for path in sorted(root.iterdir(), key=lambda candidate: candidate.name):
        if not path.is_dir() or path.name.startswith("."):
            continue
        result = validate_snapshot(path, project_root=project_root, book=book)
        if result.snapshot is not None:
            listed.append(
                ListedSnapshot(
                    snapshot=result.snapshot,
                    path=path,
                    validation_state=(
                        "current"
                        if result.report.valid and result.lineage_current
                        else "historical"
                        if result.report.valid
                        else "invalid"
                    ),
                )
            )
    return tuple(listed)
