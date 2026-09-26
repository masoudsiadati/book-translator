"""Immutable chapter source extraction driven only by the canonical TOC."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.hashing import sha256_file
from book_translator.line_layout import (
    LINE_LAYOUT_EXTRACTION_VERSION,
    LineLayoutError,
    reconstruct,
    render_source,
)
from book_translator.models import (
    SCHEMA_VERSION,
    ChapterMetadata,
    ChapterStatus,
    TocDocument,
    TocEntry,
    TocKind,
)
from book_translator.paths import (
    NARRATIVE_KINDS,
    ProjectPaths,
    chapter_id_from_selector,
    narrative_chapter_id,
)
from book_translator.pdf_inspector import PdfError, open_pdf
from book_translator.toc_discovery import sorted_text_blocks

EXTRACTION_VERSION = "1.0"


class ChapterExtractionError(ValueError):
    """Raised when chapter extraction cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class EntryPaths:
    directory: Path
    source: Path
    metadata: Path
    cleaned: Path
    chapter_id: str


@dataclass(frozen=True, slots=True)
class PlannedEntry:
    entry: TocEntry
    paths: EntryPaths
    action: str


@dataclass(frozen=True, slots=True)
class ExtractionPlan:
    pdf_path: Path
    toc_path: Path
    workspace_path: Path
    page_count: int
    entries: list[PlannedEntry]
    force: bool
    book: str | None = None
    toc: TocDocument | None = None


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    plan: ExtractionPlan
    extracted_ids: list[str]
    skipped_ids: list[str]
    failed_ids: list[str]
    total_words: int
    total_bytes: int


def load_toc(path: Path) -> TocDocument:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ChapterExtractionError(f"TOC file not found: {path}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ChapterExtractionError(f"could not read TOC file {path}: {exc}") from exc
    try:
        return TocDocument.model_validate(raw)
    except ValidationError as exc:
        raise ChapterExtractionError(f"invalid canonical TOC {path}: {exc}") from exc


def _entry_paths(entry: TocEntry, project_paths: ProjectPaths) -> EntryPaths:
    if entry.kind in NARRATIVE_KINDS:
        try:
            chapter_id = narrative_chapter_id(entry.kind, entry.number)
        except ValueError as exc:
            raise ChapterExtractionError(
                f"entry {entry.entry_id} is not extractable: {exc}"
            ) from exc
        directory = project_paths.chapter_directory(chapter_id)
    elif entry.kind is TocKind.FRONT_MATTER:
        directory = project_paths.front_matter(entry.entry_id)
        chapter_id = entry.entry_id
    else:
        raise ChapterExtractionError(f"entry {entry.entry_id} is not extractable")
    return EntryPaths(
        directory=directory,
        source=directory / "source.md",
        metadata=directory / "metadata.yaml",
        cleaned=directory / "cleaned.md",
        chapter_id=chapter_id,
    )


def _resolve_selector(
    entries: list[TocEntry], selector: str | None, include_front_matter: bool
) -> list[TocEntry]:
    allowed = [
        entry
        for entry in entries
        if entry.kind in NARRATIVE_KINDS
        or (include_front_matter and entry.kind is TocKind.FRONT_MATTER)
    ]
    if selector is None:
        return allowed
    normalized = selector.strip().lower()
    matches: list[TocEntry]
    try:
        chapter_id = chapter_id_from_selector(normalized)
    except ValueError:
        matches = [entry for entry in allowed if entry.entry_id.lower() == normalized]
    else:
        matches = [
            entry
            for entry in allowed
            if entry.kind in NARRATIVE_KINDS
            and narrative_chapter_id(entry.kind, entry.number) == chapter_id
        ]
    if not matches:
        raise ChapterExtractionError(
            f"chapter selector {selector!r} did not resolve to an extractable TOC entry"
        )
    if len(matches) != 1:
        raise ChapterExtractionError(f"chapter selector {selector!r} is ambiguous")
    return matches


def _load_metadata(path: Path) -> ChapterMetadata:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return ChapterMetadata.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ChapterExtractionError(f"invalid chapter metadata {path}: {exc}") from exc


def _relative_posix(path: Path, root: Path) -> Path:
    try:
        return Path(path.relative_to(root).as_posix())
    except ValueError as exc:
        raise ChapterExtractionError(f"chapter destination escapes project root: {path}") from exc


def _validate_existing(entry: TocEntry, paths: EntryPaths, project_root: Path) -> str:
    source_exists = paths.source.exists()
    metadata_exists = paths.metadata.exists()
    if source_exists != metadata_exists:
        raise ChapterExtractionError(
            f"inconsistent workspace for {entry.entry_id}: source.md and metadata.yaml "
            "must either both exist or both be absent"
        )
    if not source_exists:
        return "extract"
    metadata = _load_metadata(paths.metadata)
    expected_source = _relative_posix(paths.source, project_root)
    conflicts = (
        metadata.chapter_id != paths.chapter_id
        or metadata.kind is not entry.kind
        or metadata.number != entry.number
        or metadata.source_start_page != entry.start_page
        or metadata.source_end_page != entry.end_page
        or metadata.source_file != expected_source
    )
    if conflicts:
        raise ChapterExtractionError(f"stale or conflicting extraction for {entry.entry_id}")
    actual_hash = sha256_file(paths.source)
    if metadata.source_sha256 != actual_hash:
        raise ChapterExtractionError(f"source hash integrity error for {entry.entry_id}")
    return "skip"


def _existing_action(entry: TocEntry, paths: EntryPaths, project_root: Path, *, force: bool) -> str:
    if force:
        return "replace" if paths.source.exists() or paths.metadata.exists() else "extract"
    return _validate_existing(entry, paths, project_root)


def _has_downstream_artifacts(paths: EntryPaths) -> bool:
    if not paths.directory.exists():
        return False
    for candidate in paths.directory.rglob("*"):
        if not candidate.is_file() or candidate in {paths.source, paths.metadata}:
            continue
        relative = candidate.relative_to(paths.directory)
        if (
            relative == Path("cleaned.md")
            or relative == Path("final.md")
            or "chunks" in relative.parts
            or "review" in relative.parts
            or candidate.name == "translation.md"
        ):
            return True
    return False


def plan_extraction(
    pdf_path: Path,
    toc_path: Path,
    *,
    project_root: Path,
    selector: str | None = None,
    include_front_matter: bool = False,
    force: bool = False,
    book: str | None = None,
) -> ExtractionPlan:
    """Plan extraction into the selected book workspace; `book=None` is the legacy root."""
    toc = load_toc(toc_path)
    try:
        resolved_pdf, document = open_pdf(pdf_path)
    except PdfError as exc:
        raise ChapterExtractionError(str(exc)) from exc
    try:
        if toc.source_pdf_sha256 is None or toc.source_pdf_sha256 != sha256_file(resolved_pdf):
            raise ChapterExtractionError("TOC source PDF hash does not match the selected PDF")
        if toc.pdf_page_count is None or toc.pdf_page_count != document.page_count:
            raise ChapterExtractionError("TOC PDF page count does not match the selected PDF")
        selected = _resolve_selector(toc.entries, selector, include_front_matter)
        if not selected:
            raise ChapterExtractionError("TOC contains no entries selected for extraction")
        if len({entry.entry_id for entry in selected}) != len(selected):
            raise ChapterExtractionError("selected TOC entry IDs are not unique")
        try:
            project_paths = ProjectPaths(project_root, book)
        except ValueError as exc:
            raise ChapterExtractionError(str(exc)) from exc
        planned: list[PlannedEntry] = []
        destinations: set[Path] = set()
        for entry in selected:
            if not 1 <= entry.start_page <= entry.end_page <= document.page_count:
                raise ChapterExtractionError(f"invalid page range for {entry.entry_id}")
            paths = _entry_paths(entry, project_paths)
            if paths.directory in destinations:
                raise ChapterExtractionError("selected chapter destination paths are not unique")
            destinations.add(paths.directory)
            existing_action = _existing_action(entry, paths, project_root, force=force)
            if force:
                if _has_downstream_artifacts(paths):
                    raise ChapterExtractionError(
                        f"refusing forced extraction for {entry.entry_id}: "
                        "downstream artifacts exist"
                    )
                action = existing_action
            else:
                action = existing_action
            planned.append(PlannedEntry(entry, paths, action))
        return ExtractionPlan(
            resolved_pdf,
            toc_path.resolve(),
            project_paths.workspace,
            document.page_count,
            planned,
            force,
            book,
            toc,
        )
    finally:
        document.close()  # type: ignore[no-untyped-call]


def _normalize_block(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    return re.sub(r"\n{4,}", "\n\n\n", normalized).strip("\n")


def extract_line_layout_entry(
    document: pymupdf.Document, entry: TocEntry, toc: TocDocument
) -> tuple[str, int, int]:
    """Rebuild a line-layout entry's paragraphs between its sub-page anchors."""
    pages = [
        (page_number, sorted_text_blocks(document[page_number - 1]))
        for page_number in range(entry.start_page, entry.end_page + 1)
    ]
    try:
        rebuilt = reconstruct(
            pages,
            start_block=entry.start_block,
            end_block=entry.end_block,
            running_head_keys=toc.running_head_keys or [],
            heading_groups=entry.heading_group_count or 0,
        )
        content = render_source(rebuilt.paragraphs)
    except LineLayoutError as exc:
        raise ChapterExtractionError(f"line layout of {entry.entry_id}: {exc}") from exc
    words = sum(len(line.split()) for paragraph in rebuilt.paragraphs for line in paragraph.lines)
    return content, words, len(rebuilt.paragraphs)


def extract_entry_content(document: pymupdf.Document, entry: TocEntry) -> tuple[str, int, int]:
    sections: list[str] = []
    word_count = 0
    paragraph_count = 0
    for page_number in range(entry.start_page, entry.end_page + 1):
        blocks = document[page_number - 1].get_text(  # type: ignore[no-untyped-call]
            "blocks", sort=True
        )
        text_blocks = [
            _normalize_block(str(block[4]))
            for block in blocks
            if len(block) > 6 and block[6] == 0 and _normalize_block(str(block[4]))
        ]
        word_count += sum(len(block.split()) for block in text_blocks)
        paragraph_count += len(text_blocks)
        marker = f"<!-- source-page: {page_number} -->"
        page_text = "\n\n".join(text_blocks)
        sections.append(f"{marker}\n\n{page_text}" if page_text else marker)
    content = re.sub(r"\n{4,}", "\n\n\n", "\n\n".join(sections)).rstrip() + "\n"
    return content, word_count, paragraph_count


def _metadata_for(
    planned: PlannedEntry,
    project_root: Path,
    source_hash: str,
    words: int,
    paragraphs: int,
    *,
    line_layout: bool = False,
) -> ChapterMetadata:
    now = datetime.now(UTC)
    entry = planned.entry
    return ChapterMetadata(
        schema_version=SCHEMA_VERSION,
        chapter_id=planned.paths.chapter_id,
        kind=entry.kind,
        number=entry.number,
        title_en=entry.title_en,
        title_fa=entry.title_fa,
        symbol=entry.symbol,
        source_start_page=entry.start_page,
        source_end_page=entry.end_page,
        source_file=_relative_posix(planned.paths.source, project_root),
        source_sha256=source_hash,
        cleaned_file=_relative_posix(planned.paths.cleaned, project_root),
        cleaned_sha256=None,
        source_word_count=words,
        cleaned_word_count=None,
        source_paragraph_count=paragraphs,
        cleaned_paragraph_count=None,
        chunk_count=0,
        status=ChapterStatus.EXTRACTED,
        warnings=[],
        extraction_version=LINE_LAYOUT_EXTRACTION_VERSION if line_layout else EXTRACTION_VERSION,
        normalization_version="",
        created_at=now,
        updated_at=now,
    )


def _serialize_metadata(metadata: ChapterMetadata) -> str:
    dumped = metadata.model_dump(mode="json", exclude_none=False)
    result = yaml.safe_dump(dumped, allow_unicode=True, sort_keys=False, default_flow_style=False)
    if not isinstance(result, str):
        raise ChapterExtractionError("could not serialize chapter metadata")
    return result


def execute_extraction(plan: ExtractionPlan, *, project_root: Path) -> ExtractionResult:
    extracted: list[str] = []
    skipped = [item.entry.entry_id for item in plan.entries if item.action == "skip"]
    failed: list[str] = []
    total_words = 0
    total_bytes = 0
    _, document = open_pdf(plan.pdf_path)
    try:
        for planned in plan.entries:
            if planned.action == "skip":
                continue
            source_existed = planned.paths.source.exists()
            try:
                if plan.toc is not None and plan.toc.layout == "line_blocks":
                    content, words, paragraphs = extract_line_layout_entry(
                        document, planned.entry, plan.toc
                    )
                else:
                    content, words, paragraphs = extract_entry_content(document, planned.entry)
                planned.paths.directory.mkdir(parents=True, exist_ok=True)
                if plan.force and planned.paths.metadata.exists():
                    planned.paths.metadata.unlink()
                atomic_write_text(planned.paths.source, content, overwrite=plan.force)
                persisted_hash = sha256_file(planned.paths.source)
                metadata = _metadata_for(
                    planned,
                    project_root,
                    persisted_hash,
                    words,
                    paragraphs,
                    line_layout=plan.toc is not None and plan.toc.layout == "line_blocks",
                )
                atomic_write_text(
                    planned.paths.metadata,
                    _serialize_metadata(metadata),
                    overwrite=plan.force,
                )
                extracted.append(planned.entry.entry_id)
                total_words += words
                total_bytes += planned.paths.source.stat().st_size
            except Exception as exc:
                planned.paths.metadata.unlink(missing_ok=True)
                if not source_existed:
                    planned.paths.source.unlink(missing_ok=True)
                failed.append(planned.entry.entry_id)
                raise ChapterExtractionError(
                    f"extraction failed for {planned.entry.entry_id}; completed: "
                    f"{', '.join(extracted) or 'none'}; failed: {planned.entry.entry_id}: {exc}"
                ) from exc
    finally:
        document.close()  # type: ignore[no-untyped-call]
    return ExtractionResult(plan, extracted, skipped, failed, total_words, total_bytes)
