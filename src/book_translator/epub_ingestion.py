"""Provider-free EPUB package inspection and narrative-unit discovery."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urldefrag
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.hashing import sha256_file
from book_translator.models import (
    SCHEMA_VERSION,
    ChapterMetadata,
    ChapterStatus,
    TocDocument,
    TocEntry,
    TocKind,
)
from book_translator.paths import ProjectPaths, narrative_chapter_id

_CONTAINER_PATH = PurePosixPath("META-INF/container.xml")
_SPLIT_SUFFIX = re.compile(r"(?:[_ -]split[_ -]?\d+)$", re.IGNORECASE)
_NAMED_CHAPTER_LABEL = re.compile(r"^chapter\s+0*([1-9]\d*)(?:\s*:\s*(\S.*))?$", re.IGNORECASE)
_ORDINAL_CHAPTER_LABEL = re.compile(r"^0*([1-9]\d*)(?:\s+|[.:]\s+)(\S.*)$")
_CHAPTER_STEM = re.compile(r"^chapter[ _-]*0*([1-9]\d*)$", re.IGNORECASE)


class EpubIngestionError(ValueError):
    """Raised when an EPUB package cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class NarrativeResource:
    manifest_id: str
    href: PurePosixPath
    spine_position: int


@dataclass(frozen=True, slots=True)
class NarrativeUnit:
    kind: TocKind
    number: int | None
    title: str
    resources: tuple[NarrativeResource, ...]


@dataclass(frozen=True, slots=True)
class EpubDocument:
    path: Path
    package_path: PurePosixPath
    title: str
    author: str
    language: str
    spine_item_count: int
    narrative_units: tuple[NarrativeUnit, ...]


@dataclass(frozen=True, slots=True)
class EpubUnitPaths:
    directory: Path
    source: Path
    metadata: Path
    cleaned: Path


@dataclass(frozen=True, slots=True)
class PlannedEpubUnit:
    unit: NarrativeUnit
    chapter_id: str
    source_text: str
    word_count: int
    paragraph_count: int
    paths: EpubUnitPaths
    action: str


@dataclass(frozen=True, slots=True)
class EpubImportPlan:
    epub_path: Path
    workspace_path: Path
    toc_path: Path
    document: EpubDocument
    toc: TocDocument
    units: tuple[PlannedEpubUnit, ...]
    force: bool
    book: str | None


@dataclass(frozen=True, slots=True)
class EpubImportResult:
    plan: EpubImportPlan
    imported_ids: tuple[str, ...]
    skipped_ids: tuple[str, ...]
    total_words: int
    total_bytes: int


EPUB_EXTRACTION_VERSION = "epub/1.0.0"
_BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_IGNORED_TAGS = frozenset({"head", "script", "style", "nav", "svg", "math"})


@dataclass(frozen=True, slots=True)
class _ManifestItem:
    item_id: str
    href: PurePosixPath
    media_type: str


@dataclass(frozen=True, slots=True)
class _NarrativeLabel:
    identity: tuple[TocKind, int | None]
    title: str | None = None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element.iter() if _local_name(child.tag) == name]


def _safe_archive_path(value: str, *, base: PurePosixPath | None = None) -> PurePosixPath:
    decoded = unquote(urldefrag(value)[0]).replace("\\", "/")
    candidate = PurePosixPath(decoded)
    if candidate.is_absolute() or not decoded or ":" in candidate.parts[0]:
        raise EpubIngestionError(f"unsafe archive path: {value!r}")
    parts = [] if base is None or base == PurePosixPath(".") else list(base.parts)
    for part in candidate.parts:
        if part == "..":
            if not parts:
                raise EpubIngestionError(f"unsafe archive path: {value!r}")
            parts.pop()
        elif part not in {"", "."}:
            parts.append(part)
    if not parts:
        raise EpubIngestionError(f"unsafe archive path: {value!r}")
    return PurePosixPath(*parts)


def _read_xml(archive: ZipFile, path: PurePosixPath, label: str) -> ElementTree.Element:
    try:
        payload = archive.read(path.as_posix())
    except KeyError as exc:
        raise EpubIngestionError(f"{label} not found in EPUB: {path}") from exc
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise EpubIngestionError(f"invalid {label}: {exc}") from exc


def _package_path(archive: ZipFile) -> PurePosixPath:
    container = _read_xml(archive, _CONTAINER_PATH, "container document")
    rootfiles = _children(container, "rootfile")
    if not rootfiles or not rootfiles[0].get("full-path"):
        raise EpubIngestionError("container document has no package rootfile")
    return _safe_archive_path(rootfiles[0].get("full-path", ""))


def _metadata_text(package: ElementTree.Element, name: str) -> str:
    for element in _children(package, name):
        text = "".join(element.itertext()).strip()
        if text:
            return text
    return ""


def _manifest(
    package: ElementTree.Element, package_path: PurePosixPath
) -> dict[str, _ManifestItem]:
    base = package_path.parent
    result: dict[str, _ManifestItem] = {}
    manifests = [element for element in package if _local_name(element.tag) == "manifest"]
    if len(manifests) != 1:
        raise EpubIngestionError("OPF package must contain exactly one manifest")
    for element in manifests[0]:
        if _local_name(element.tag) != "item":
            continue
        item_id = element.get("id", "").strip()
        href = element.get("href", "").strip()
        if not item_id or not href:
            raise EpubIngestionError("manifest item is missing id or href")
        if item_id in result:
            raise EpubIngestionError(f"duplicate manifest item: {item_id}")
        result[item_id] = _ManifestItem(
            item_id,
            _safe_archive_path(href, base=base),
            element.get("media-type", "").strip(),
        )
    return result


def _spine(
    package: ElementTree.Element, manifest: dict[str, _ManifestItem]
) -> tuple[list[_ManifestItem], str | None]:
    spines = [element for element in package if _local_name(element.tag) == "spine"]
    if len(spines) != 1:
        raise EpubIngestionError("OPF package must contain exactly one spine")
    result: list[_ManifestItem] = []
    for element in spines[0]:
        if _local_name(element.tag) != "itemref":
            continue
        item_id = element.get("idref", "").strip()
        try:
            result.append(manifest[item_id])
        except KeyError as exc:
            raise EpubIngestionError(f"spine references missing manifest item: {item_id}") from exc
    if not result:
        raise EpubIngestionError("OPF spine contains no items")
    return result, spines[0].get("toc")


def _ncx_labels(archive: ZipFile, item: _ManifestItem | None) -> dict[PurePosixPath, list[str]]:
    if item is None:
        return {}
    root = _read_xml(archive, item.href, "NCX document")
    labels: dict[PurePosixPath, list[str]] = {}
    for point in _children(root, "navPoint"):
        content = next((child for child in point if _local_name(child.tag) == "content"), None)
        label = next((child for child in point if _local_name(child.tag) == "navLabel"), None)
        if content is None or label is None or not content.get("src"):
            continue
        text = " ".join("".join(label.itertext()).split())
        if not text:
            continue
        href = _safe_archive_path(content.get("src", ""), base=item.href.parent)
        labels.setdefault(href, []).append(text)
    return labels


def _parse_narrative_label(label: str) -> _NarrativeLabel | None:
    normalized = " ".join(label.split())
    if normalized.casefold() == "prologue":
        return _NarrativeLabel((TocKind.PROLOGUE, 0))
    if normalized.casefold() == "epilogue":
        return _NarrativeLabel((TocKind.EPILOGUE, None))
    for prefix, identity in (
        ("prologue:", (TocKind.PROLOGUE, 0)),
        ("epilogue:", (TocKind.EPILOGUE, None)),
    ):
        if normalized.casefold().startswith(prefix):
            title = normalized[len(prefix) :].strip()
            return _NarrativeLabel(identity, title) if title else None
    chapter = _NAMED_CHAPTER_LABEL.fullmatch(normalized)
    if chapter:
        return _NarrativeLabel((TocKind.CHAPTER, int(chapter.group(1))), chapter.group(2))
    ordinal = _ORDINAL_CHAPTER_LABEL.fullmatch(normalized)
    if ordinal:
        return _NarrativeLabel((TocKind.CHAPTER, int(ordinal.group(1))), ordinal.group(2))
    return None


def _stem_label(href: PurePosixPath) -> str:
    stem = _SPLIT_SUFFIX.sub("", href.stem)
    return " ".join(stem.split())


def _identity_from_stem(href: PurePosixPath) -> tuple[TocKind, int | None] | None:
    stem = _stem_label(href)
    if stem.casefold() == "prologue":
        return TocKind.PROLOGUE, 0
    if stem.casefold() == "epilogue":
        return TocKind.EPILOGUE, None
    match = _CHAPTER_STEM.fullmatch(stem)
    if match:
        return TocKind.CHAPTER, int(match.group(1))
    return None


def _resource_identity(item: _ManifestItem, labels: list[str]) -> tuple[TocKind, int | None] | None:
    for label in labels:
        if parsed := _parse_narrative_label(label):
            return parsed.identity
    return _identity_from_stem(item.href)


def _unit_title(labels: list[str]) -> str:
    for label in labels:
        parsed = _parse_narrative_label(label)
        if parsed is not None and parsed.title is not None:
            return parsed.title
    for label in labels:
        if _parse_narrative_label(label) is None:
            return label
    return ""


def _discover_units(
    spine: list[_ManifestItem], labels: dict[PurePosixPath, list[str]]
) -> tuple[NarrativeUnit, ...]:
    units: list[NarrativeUnit] = []
    for position, item in enumerate(spine, start=1):
        item_labels = labels.get(item.href, [])
        identity = _resource_identity(item, item_labels)
        if identity is None:
            continue
        resource = NarrativeResource(item.item_id, item.href, position)
        title = _unit_title(item_labels)
        if units and (units[-1].kind, units[-1].number) == identity:
            previous = units[-1]
            units[-1] = NarrativeUnit(
                previous.kind,
                previous.number,
                previous.title or title,
                previous.resources + (resource,),
            )
        else:
            units.append(NarrativeUnit(identity[0], identity[1], title, (resource,)))
    return tuple(units)


def inspect_epub(path: Path) -> EpubDocument:
    """Inspect one EPUB without extracting prose or mutating the filesystem."""
    resolved = path.resolve()
    try:
        with ZipFile(resolved) as archive:
            package_path = _package_path(archive)
            package = _read_xml(archive, package_path, "OPF package document")
            manifest = _manifest(package, package_path)
            spine, ncx_id = _spine(package, manifest)
            ncx = (
                manifest.get(ncx_id)
                if ncx_id
                else next(
                    (
                        item
                        for item in manifest.values()
                        if item.media_type == "application/x-dtbncx+xml"
                    ),
                    None,
                )
            )
            labels = _ncx_labels(archive, ncx)
            units = _discover_units(spine, labels)
            if not units:
                raise EpubIngestionError("EPUB spine contains no discoverable narrative units")
            return EpubDocument(
                path=resolved,
                package_path=package_path,
                title=_metadata_text(package, "title"),
                author=_metadata_text(package, "creator"),
                language=_metadata_text(package, "language"),
                spine_item_count=len(spine),
                narrative_units=units,
            )
    except FileNotFoundError as exc:
        raise EpubIngestionError(f"EPUB file not found: {path}") from exc
    except BadZipFile as exc:
        raise EpubIngestionError(f"invalid EPUB ZIP archive: {path}") from exc


class _HtmlBlocks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self._active_tag: str | None = None
        self._buffer: list[str] = []
        self._ignored_depth = 0
        self._emphasis_depth = 0
        self._orphan_buffer: list[str] = []
        self._orphan_emphasis_depth = 0

    def _flush_orphan(self) -> None:
        if self._orphan_emphasis_depth:
            self._orphan_buffer.append("*")
            self._orphan_emphasis_depth = 0
        text = " ".join("".join(self._orphan_buffer).replace("\u00a0", " ").split())
        if text:
            self.blocks.append(("p", text))
        self._orphan_buffer = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        lowered = tag.casefold()
        if lowered in _IGNORED_TAGS:
            if not self._ignored_depth:
                self._flush_orphan()
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if lowered in _BLOCK_TAGS and self._active_tag is None:
            self._flush_orphan()
            self._active_tag = lowered
            self._buffer = []
            self._emphasis_depth = 0
        elif lowered in {"em", "i"} and self._active_tag is not None:
            if self._emphasis_depth == 0:
                self._buffer.append("*")
            self._emphasis_depth += 1
        elif lowered in {"em", "i"}:
            if self._orphan_emphasis_depth == 0:
                self._orphan_buffer.append("*")
            self._orphan_emphasis_depth += 1
        elif lowered == "br" and self._active_tag is not None:
            self._buffer.append("\n")
        elif lowered == "br":
            self._orphan_buffer.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and self._active_tag is not None:
            self._buffer.append(data)
        elif not self._ignored_depth:
            self._orphan_buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in _IGNORED_TAGS:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if lowered in {"em", "i"} and self._active_tag is not None and self._emphasis_depth:
            self._emphasis_depth -= 1
            if self._emphasis_depth == 0:
                self._buffer.append("*")
        elif lowered in {"em", "i"} and self._orphan_emphasis_depth:
            self._orphan_emphasis_depth -= 1
            if self._orphan_emphasis_depth == 0:
                self._orphan_buffer.append("*")
        elif lowered == self._active_tag:
            if self._emphasis_depth:
                self._buffer.append("*")
                self._emphasis_depth = 0
            text = " ".join("".join(self._buffer).replace("\u00a0", " ").split())
            if text:
                self.blocks.append((lowered, text))
            self._active_tag = None
            self._buffer = []
        elif lowered in {"body", "div", "section", "article", "blockquote"}:
            self._flush_orphan()

    def finish(self) -> None:
        self._flush_orphan()


def _render_html(payload: bytes, unit: NarrativeUnit) -> tuple[str, ...]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EpubIngestionError("EPUB HTML resource is not valid UTF-8") from exc
    parser = _HtmlBlocks()
    parser.feed(text)
    parser.close()
    parser.finish()
    removable = {
        "prologue" if unit.kind is TocKind.PROLOGUE else "",
        "epilogue" if unit.kind is TocKind.EPILOGUE else "",
        f"chapter {unit.number}" if unit.kind is TocKind.CHAPTER else "",
        " ".join(unit.title.casefold().split()),
    }
    blocks = list(parser.blocks)
    while blocks and blocks[0][0] in _HEADING_TAGS:
        normalized = " ".join(blocks[0][1].casefold().split())
        if normalized not in removable:
            break
        blocks.pop(0)
    return tuple(text for _, text in blocks)


def _extract_unit(archive: ZipFile, unit: NarrativeUnit) -> tuple[str, int, int]:
    sections: list[str] = []
    words = 0
    paragraphs = 0
    for resource in unit.resources:
        try:
            payload = archive.read(resource.href.as_posix())
        except KeyError as exc:
            raise EpubIngestionError(f"spine resource not found: {resource.href}") from exc
        blocks = _render_html(payload, unit)
        marker = f"<!-- source-page: {resource.spine_position} -->"
        body = "\n\n".join(blocks)
        sections.append(f"{marker}\n\n{body}" if body else marker)
        words += sum(len(block.split()) for block in blocks)
        paragraphs += len(blocks)
    if paragraphs == 0:
        raise EpubIngestionError("narrative unit contains no extractable text")
    return "\n\n".join(sections).rstrip() + "\n", words, paragraphs


def _relative(path: Path, project_root: Path) -> Path:
    try:
        return path.resolve().relative_to(project_root.resolve())
    except ValueError as exc:
        raise EpubIngestionError(f"path is outside the project root: {path}") from exc


def _unit_paths(paths: ProjectPaths, chapter_id: str) -> EpubUnitPaths:
    directory = paths.chapter_directory(chapter_id)
    return EpubUnitPaths(
        directory=directory,
        source=directory / "source.md",
        metadata=directory / "metadata.yaml",
        cleaned=directory / "cleaned.md",
    )


def _source_toc_title(unit: NarrativeUnit) -> str:
    if unit.kind is TocKind.PROLOGUE:
        return "Prologue"
    if unit.kind is TocKind.EPILOGUE:
        return "Epilogue"
    return f"Chapter {unit.number}"


def _toc_for(document: EpubDocument, project_root: Path) -> TocDocument:
    entries = [
        TocEntry(
            entry_id=(
                "prologue"
                if unit.kind is TocKind.PROLOGUE
                else "epilogue"
                if unit.kind is TocKind.EPILOGUE
                else f"chapter-{unit.number:03d}"
            ),
            level=1,
            kind=unit.kind,
            number=unit.number,
            title_en=unit.title,
            start_page=unit.resources[0].spine_position,
            end_page=unit.resources[-1].spine_position,
            source_toc_title=_source_toc_title(unit),
        )
        for unit in document.narrative_units
    ]
    return TocDocument(
        schema_version=SCHEMA_VERSION,
        source="epub_spine",
        source_document=_relative(document.path, project_root),
        source_document_sha256=sha256_file(document.path),
        source_item_count=document.spine_item_count,
        entry_count=len(entries),
        entries=entries,
    )


def _load_metadata(path: Path) -> ChapterMetadata:
    try:
        return ChapterMetadata.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise EpubIngestionError(f"invalid chapter metadata: {path}: {exc}") from exc


def _has_downstream(paths: EpubUnitPaths) -> bool:
    if not paths.directory.exists():
        return False
    for candidate in paths.directory.rglob("*"):
        if not candidate.is_file() or candidate in {paths.source, paths.metadata}:
            continue
        relative = candidate.relative_to(paths.directory)
        if (
            relative in {Path("cleaned.md"), Path("final.md")}
            or "chunks" in relative.parts
            or "review" in relative.parts
            or candidate.name == "translation.md"
        ):
            return True
    return False


def _existing_action(item: PlannedEpubUnit, project_root: Path, *, force: bool) -> str:
    exists = item.paths.source.exists() or item.paths.metadata.exists()
    if force:
        if _has_downstream(item.paths):
            raise EpubIngestionError(
                f"refusing forced EPUB import for {item.chapter_id}: downstream artifacts exist"
            )
        return "replace" if exists else "import"
    if not exists:
        return "import"
    if not item.paths.source.is_file() or not item.paths.metadata.is_file():
        raise EpubIngestionError(f"partial EPUB import for {item.chapter_id}")
    metadata = _load_metadata(item.paths.metadata)
    expected_hash = sha256_file(item.paths.source)
    if (
        item.paths.source.read_text(encoding="utf-8") != item.source_text
        or metadata.chapter_id != item.chapter_id
        or metadata.kind is not item.unit.kind
        or metadata.number != item.unit.number
        or metadata.title_en != item.unit.title
        or metadata.source_sha256 != expected_hash
        or metadata.source_file != _relative(item.paths.source, project_root)
        or metadata.extraction_version != EPUB_EXTRACTION_VERSION
    ):
        raise EpubIngestionError(f"stale or conflicting EPUB import for {item.chapter_id}")
    return "skip"


def _serialize(model: TocDocument | ChapterMetadata) -> str:
    dumped = model.model_dump(mode="json", exclude_none=False)
    rendered = yaml.safe_dump(dumped, allow_unicode=True, sort_keys=False, default_flow_style=False)
    if not isinstance(rendered, str):
        raise EpubIngestionError("could not serialize EPUB import metadata")
    return rendered


def plan_epub_import(
    epub_path: Path,
    *,
    project_root: Path,
    book: str | None = None,
    force: bool = False,
) -> EpubImportPlan:
    document = inspect_epub(epub_path)
    paths = ProjectPaths(project_root.resolve(), book)
    toc = _toc_for(document, project_root)
    planned: list[PlannedEpubUnit] = []
    with ZipFile(document.path) as archive:
        for unit in document.narrative_units:
            chapter_id = narrative_chapter_id(unit.kind, unit.number)
            source, words, paragraphs = _extract_unit(archive, unit)
            unit_paths = _unit_paths(paths, chapter_id)
            provisional = PlannedEpubUnit(
                unit,
                chapter_id,
                source,
                words,
                paragraphs,
                unit_paths,
                "",
            )
            action = _existing_action(provisional, project_root, force=force)
            planned.append(
                PlannedEpubUnit(
                    unit,
                    chapter_id,
                    source,
                    words,
                    paragraphs,
                    unit_paths,
                    action,
                )
            )
    if paths.toc.exists() and not force:
        try:
            existing = TocDocument.model_validate(
                yaml.safe_load(paths.toc.read_text(encoding="utf-8"))
            )
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise EpubIngestionError(f"invalid existing TOC: {paths.toc}: {exc}") from exc
        if existing != toc:
            raise EpubIngestionError(f"stale or conflicting EPUB TOC: {paths.toc}")
    return EpubImportPlan(
        document.path,
        paths.workspace,
        paths.toc,
        document,
        toc,
        tuple(planned),
        force,
        book,
    )


def _chapter_metadata(
    item: PlannedEpubUnit, project_root: Path, source_hash: str
) -> ChapterMetadata:
    now = datetime.now(UTC)
    return ChapterMetadata(
        schema_version=SCHEMA_VERSION,
        chapter_id=item.chapter_id,
        kind=item.unit.kind,
        number=item.unit.number,
        title_en=item.unit.title,
        source_start_page=item.unit.resources[0].spine_position,
        source_end_page=item.unit.resources[-1].spine_position,
        source_file=_relative(item.paths.source, project_root),
        source_sha256=source_hash,
        cleaned_file=_relative(item.paths.cleaned, project_root),
        source_word_count=item.word_count,
        source_paragraph_count=item.paragraph_count,
        status=ChapterStatus.EXTRACTED,
        extraction_version=EPUB_EXTRACTION_VERSION,
        normalization_version="",
        created_at=now,
        updated_at=now,
    )


def execute_epub_import(plan: EpubImportPlan, *, project_root: Path) -> EpubImportResult:
    if plan.toc_path.exists():
        if plan.force:
            atomic_write_text(plan.toc_path, _serialize(plan.toc), overwrite=True)
    else:
        atomic_write_text(plan.toc_path, _serialize(plan.toc))
    imported: list[str] = []
    skipped: list[str] = []
    total_words = 0
    total_bytes = 0
    for item in plan.units:
        if item.action == "skip":
            skipped.append(item.chapter_id)
            continue
        source_existed = item.paths.source.exists()
        try:
            item.paths.directory.mkdir(parents=True, exist_ok=True)
            if plan.force:
                item.paths.metadata.unlink(missing_ok=True)
            atomic_write_text(item.paths.source, item.source_text, overwrite=plan.force)
            source_hash = sha256_file(item.paths.source)
            metadata = _chapter_metadata(item, project_root, source_hash)
            atomic_write_text(item.paths.metadata, _serialize(metadata), overwrite=plan.force)
        except Exception as exc:
            item.paths.metadata.unlink(missing_ok=True)
            if not source_existed:
                item.paths.source.unlink(missing_ok=True)
            raise EpubIngestionError(f"EPUB import failed for {item.chapter_id}: {exc}") from exc
        imported.append(item.chapter_id)
        total_words += item.word_count
        total_bytes += item.paths.source.stat().st_size
    return EpubImportResult(plan, tuple(imported), tuple(skipped), total_words, total_bytes)
