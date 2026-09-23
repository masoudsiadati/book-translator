"""The only EPUB layer that touches the filesystem.

``plan_epub_build`` verifies a release and renders the whole archive in memory without writing a
byte; ``execute_epub_build`` writes it and reads it back. The split is the ordinary stage
convention, and here the read half is what ``--dry-run`` exposes: the complete answer, including
every internal hash and the archive's content address, before anything exists on disk.

Three rules bound this stage.

**A release is the only input.** Not a translation run, job, attempt, provider artifact, QA record,
or snapshot — a release directory, which the release-verification stage has just proved complete.
The builder holds no path to any of those and no provider import at all.

**An unverified release is not built.** Verification runs inside the plan, and anything short of
``READY_FOR_PUBLICATION`` refuses the build outright rather than producing a book nobody can vouch
for.

**Nothing is invented.** The book identifier is derived from the release's content address, the
modification timestamp is the release's own, archive entry times are fixed, and entry order is the
release's order — so building one release twice produces the same bytes.
"""

import hashlib
import zipfile
from datetime import UTC
from io import BytesIO
from pathlib import Path

import yaml
from pydantic import ValidationError

from book_translator.document_chunking.paragraphs import parse_document
from book_translator.epub.engine import (
    CHAPTER_DIRECTORY,
    CONTAINER_PATH,
    EPUB_BUILDER_VERSION,
    MIMETYPE,
    MIMETYPE_PATH,
    NAVIGATION_PATH,
    PACKAGE_PATH,
    STYLESHEET_PATH,
    EpubBuildError,
    book_identifier,
    chapter_href,
    render_chapter,
    render_container,
    render_navigation,
    render_package,
    render_stylesheet,
)
from book_translator.epub.identity import epub_content_address, epub_id
from book_translator.epub.models import (
    EpubBuildPlan,
    EpubBuildResult,
    EpubChapterRecord,
    EpubFile,
)
from book_translator.epub.validation import validate_archive
from book_translator.models.book import BookWorkspaceMetadata
from book_translator.models.release import ReleaseManifest
from book_translator.models.release_verification import ReleaseVerificationStatus
from book_translator.release_assembly import MANIFEST_FILENAME, METADATA_FILENAME
from book_translator.release_verification import (
    ReleaseVerificationError,
    verify_release_artifact,
)

#: Every archive entry is stamped with this fixed DOS timestamp. A build must not encode when it
#: happened: two builds of one release have to be byte-identical, and a wall clock would make that
#: impossible for no reader's benefit.
FIXED_ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
#: Regular file, mode 0644, recorded the same way on every platform.
FIXED_EXTERNAL_ATTRIBUTES = (0o100644 << 16) | 0o600
DEFAULT_OUTPUT_DIRECTORY = "output"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _release_book_title(metadata: BookWorkspaceMetadata) -> tuple[str, bool]:
    """Return the book's published title and whether it is still the source-language one."""
    target = metadata.title_fa.strip()
    if target:
        return target, False
    source = metadata.title_en.strip()
    if not source:
        raise EpubBuildError("the release's book metadata records no title")
    return source, True


def _resolve_output(
    output: str | Path | None, release_id: str, release_path: Path, project_root: Path
) -> Path:
    candidate = (
        Path(output)
        if output is not None
        else Path(DEFAULT_OUTPUT_DIRECTORY) / f"{release_id}.epub"
    )
    resolved = (candidate if candidate.is_absolute() else project_root / candidate).resolve()
    if resolved.is_dir():
        resolved = resolved / f"{release_id}.epub"
    release = release_path.resolve()
    if resolved == release or release in resolved.parents:
        # A release is content-addressed by its own manifest, so a book written inside it would
        # fail the release's next verification as an unindexed file.
        raise EpubBuildError("an EPUB may not be written inside the release it is built from")
    if resolved.suffix.lower() != ".epub":
        raise EpubBuildError("an EPUB output path must end in .epub")
    return resolved


def plan_epub_build(
    release_path: str | Path,
    *,
    project_root: Path,
    output: str | Path | None = None,
    force: bool = False,
) -> EpubBuildPlan:
    """Verify one release and render its entire EPUB in memory. Reads only; writes nothing."""
    project_root = project_root.resolve()
    candidate = Path(release_path)
    release = (candidate if candidate.is_absolute() else project_root / candidate).resolve()
    try:
        verification = verify_release_artifact(release, project_root=project_root)
    except ReleaseVerificationError as exc:
        raise EpubBuildError(f"the release could not be verified: {exc}") from exc
    report = verification.report
    if report.status is not ReleaseVerificationStatus.READY_FOR_PUBLICATION:
        failed = ", ".join(item.value for item in report.failed_checks) or "none"
        raise EpubBuildError(
            f"release {report.release_id} is {report.status.value}; failed checks: {failed}"
        )

    try:
        manifest = ReleaseManifest.model_validate(
            yaml.safe_load((release / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        )
    except (OSError, yaml.YAMLError, ValidationError) as exc:  # pragma: no cover - verified above
        raise EpubBuildError(f"unreadable release manifest: {exc}") from exc
    if manifest.book_metadata_sha256 is None:
        raise EpubBuildError("the release carries no book metadata, so no EPUB can be described")
    try:
        metadata = BookWorkspaceMetadata.model_validate(
            yaml.safe_load((release / METADATA_FILENAME).read_text(encoding="utf-8"))
        )
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise EpubBuildError(f"unreadable release book metadata: {exc}") from exc

    title, _ = _release_book_title(metadata)
    language = manifest.target_language
    modified = (
        manifest.created_at.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    identifier = book_identifier(manifest.content_sha256)

    records: list[EpubChapterRecord] = []
    files: list[EpubFile] = [
        EpubFile(path=MIMETYPE_PATH, content=MIMETYPE.encode("ascii"), compress=False),
        EpubFile(path=CONTAINER_PATH, content=render_container().encode("utf-8")),
    ]
    navigation_entries: list[tuple[str, str]] = []
    for record in manifest.chapters:
        source = release / record.file
        try:
            markdown = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:  # pragma: no cover - verified above
            raise EpubBuildError(f"unreadable chapter {record.chapter_id}") from exc
        if _sha256(markdown.encode("utf-8")) != record.sha256:  # pragma: no cover - verified above
            raise EpubBuildError(f"chapter {record.chapter_id} is not the published bytes")
        document, chapter_title = render_chapter(
            chapter_id=record.chapter_id,
            markdown=markdown,
            language=language,
            expected_heading=record.heading,
        )
        archive_path = f"OEBPS/{chapter_href(record.chapter_id)}"
        content = document.encode("utf-8")
        files.append(EpubFile(path=archive_path, content=content))
        navigation_entries.append((record.chapter_id, chapter_title))
        records.append(
            EpubChapterRecord(
                chapter_id=record.chapter_id,
                chapter_sequence=record.chapter_sequence,
                title=chapter_title,
                source_file=record.file.as_posix(),
                source_sha256=record.sha256,
                archive_path=archive_path,
                sha256=_sha256(content),
                paragraph_count=len(parse_document(markdown)),
            )
        )

    files.append(EpubFile(path=STYLESHEET_PATH, content=render_stylesheet().encode("utf-8")))
    files.append(
        EpubFile(
            path=NAVIGATION_PATH,
            content=render_navigation(
                book_title=title, language=language, chapters=tuple(navigation_entries)
            ).encode("utf-8"),
        )
    )
    files.append(
        EpubFile(
            path=PACKAGE_PATH,
            content=render_package(
                identifier=identifier,
                title=title,
                author=metadata.author,
                language=language,
                modified=modified,
                source=manifest.content_sha256,
                release_id=manifest.release_id,
                series_name=metadata.series_name,
                series_index=metadata.series_index,
                chapters=tuple(record.chapter_id for record in records),
            ).encode("utf-8"),
        )
    )

    ordered = _archive_order(tuple(files))
    content_sha256 = epub_content_address(
        tuple((item.path, _sha256(item.content)) for item in ordered)
    )
    output_path = _resolve_output(output, manifest.release_id, release, project_root)
    return EpubBuildPlan(
        epub_id=epub_id(content_sha256),
        content_sha256=content_sha256,
        output_path=output_path,
        release_id=manifest.release_id,
        release_path=release,
        release_content_sha256=manifest.content_sha256,
        source_translation_run=manifest.source_translation_run,
        book_identifier=identifier,
        title=title,
        author=metadata.author,
        language=language,
        modified=modified,
        chapters=tuple(records),
        files=ordered,
        verification=report,
        destination_exists=output_path.exists(),
        overwrite=force,
    )


def _archive_order(files: tuple[EpubFile, ...]) -> tuple[EpubFile, ...]:
    """Order the archive: mimetype first, then everything else by path.

    The EPUB container format requires ``mimetype`` to come first and be stored uncompressed. Every
    other entry is sorted by path so the order never depends on how a filesystem happened to
    enumerate anything.
    """
    mimetype = [item for item in files if item.path == MIMETYPE_PATH]
    if len(mimetype) != 1:
        raise EpubBuildError("an EPUB archive holds exactly one mimetype entry")
    rest = sorted((item for item in files if item.path != MIMETYPE_PATH), key=lambda x: x.path)
    return (mimetype[0], *rest)


def render_archive(plan: EpubBuildPlan) -> bytes:
    """Serialize one planned EPUB into archive bytes, with no clock and no filesystem."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for item in plan.files:
            info = zipfile.ZipInfo(item.path, date_time=FIXED_ARCHIVE_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED if item.compress else zipfile.ZIP_STORED
            info.external_attr = FIXED_EXTERNAL_ATTRIBUTES
            info.create_system = 3
            archive.writestr(info, item.content)
    return buffer.getvalue()


def read_archive(content: bytes) -> tuple[tuple[str, ...], dict[str, bytes]]:
    """Read an EPUB archive back the way a reading system would."""
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            names = tuple(archive.namelist())
            return names, {name: archive.read(name) for name in names}
    except (zipfile.BadZipFile, OSError) as exc:
        raise EpubBuildError("the EPUB archive could not be read back") from exc


def execute_epub_build(plan: EpubBuildPlan, *, project_root: Path) -> EpubBuildResult:
    """Write one planned EPUB and prove the written archive is the book it claims to be."""
    project_root = project_root.resolve()
    destination = plan.output_path
    replaced = destination.exists()
    if replaced and not plan.overwrite:
        raise EpubBuildError(f"EPUB already exists: {destination}; pass --force to replace it")

    content = render_archive(plan)
    problems = validate_epub_bytes(content, plan)
    if problems:
        raise EpubBuildError("the built EPUB failed its own validation: " + "; ".join(problems))

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.tmp"
    try:
        staging.write_bytes(content)
        staging.replace(destination)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        raise EpubBuildError(f"could not publish {destination.name}: {exc}") from exc

    written = destination.read_bytes()
    if written != content:  # pragma: no cover - a successful replace preserves bytes
        raise EpubBuildError("the written EPUB is not the archive that was validated")
    names, _ = read_archive(written)
    return EpubBuildResult(
        epub_id=plan.epub_id,
        content_sha256=plan.content_sha256,
        archive_sha256=_sha256(written),
        output_path=destination,
        byte_count=len(written),
        entry_count=len(names),
        replaced=replaced,
        dry_run=False,
    )


def validate_epub_bytes(content: bytes, plan: EpubBuildPlan) -> tuple[str, ...]:
    """Read one archive back and check it against the release the plan was built from."""
    names, contents = read_archive(content)
    problems = list(
        validate_archive(
            names,
            contents,
            expected_chapters=tuple((record.chapter_id, record.title) for record in plan.chapters),
            expected_title=plan.title,
            expected_language=plan.language,
        )
    )
    for record in plan.chapters:
        stored = contents.get(record.archive_path)
        if stored is None:
            continue
        if _sha256(stored) != record.sha256:
            problems.append(f"chapter bytes changed during archiving: {record.chapter_id}")
    inventory = epub_content_address(tuple((name, _sha256(contents[name])) for name in names))
    if inventory != plan.content_sha256:
        problems.append("the archive's content address is not the one the plan derived")
    return tuple(problems)


def verify_epub_file(path: Path) -> tuple[str, ...]:
    """Re-verify a built EPUB on disk from its own contents, without the release.

    This is the shallow structural half: it proves the archive is a well-formed EPUB whose package,
    navigation, and chapters agree with each other. Proving it matches a *release* needs the
    release, which is what :func:`validate_epub_bytes` does at build time.
    """
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise EpubBuildError(f"no EPUB at {path}") from exc
    names, contents = read_archive(content)
    package = contents.get(PACKAGE_PATH)
    if package is None:
        return ("required archive entry is missing: OEBPS/content.opf",)
    import xml.etree.ElementTree as ElementTree

    from book_translator.epub.validation import DC_NAMESPACE, OPF_NAMESPACE

    try:
        root = ElementTree.fromstring(package.decode("utf-8"))
    except (ElementTree.ParseError, UnicodeDecodeError):
        return ("archive entry is not well-formed XML: OEBPS/content.opf",)
    manifest = root.find(f"{{{OPF_NAMESPACE}}}manifest")
    metadata = root.find(f"{{{OPF_NAMESPACE}}}metadata")
    if manifest is None or metadata is None:
        return ("the package document has no manifest or metadata",)
    chapters: list[tuple[str, str]] = []
    for item in manifest.findall(f"{{{OPF_NAMESPACE}}}item"):
        href = item.get("href") or ""
        if not href.startswith("chapters/"):
            continue
        entry = contents.get(f"OEBPS/{href}")
        if entry is None:
            return (f"required archive entry is missing: OEBPS/{href}",)
        heading = ElementTree.fromstring(entry.decode("utf-8")).iter(
            "{http://www.w3.org/1999/xhtml}h1"
        )
        chapters.append((Path(href).stem, next(heading).text or ""))
    title = metadata.find(f"{{{DC_NAMESPACE}}}title")
    language = metadata.find(f"{{{DC_NAMESPACE}}}language")
    return validate_archive(
        names,
        contents,
        expected_chapters=tuple(chapters),
        expected_title="" if title is None or title.text is None else title.text,
        expected_language="" if language is None or language.text is None else language.text,
    )


def chapter_archive_directory() -> str:
    """Return the archive directory chapters are written into."""
    return CHAPTER_DIRECTORY


def builder_version() -> str:
    return EPUB_BUILDER_VERSION
