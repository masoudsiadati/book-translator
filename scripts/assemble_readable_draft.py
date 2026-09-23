"""Assemble a complete readable Persian draft from one translation run, provider-free.

This is **not** a release and deliberately does not pretend to be one. The governed chain
(``export-translation-run`` -> ``verify-release`` -> ``build-epub``) publishes only a run whose
every chunk is an accepted translation, and it refuses anything less at three independent layers.
That refusal is correct and is left exactly as it is.

What an operator sometimes needs before that bar is met is different: a complete book to *read*,
assembled from translation bytes that already exist, so that reading can start while QA rejections
are still outstanding. This script produces that and nothing else.

Four rules bound it.

**It contacts nothing.** There is no provider import here and no network path through it.

**It writes nothing into ``workspace/``.** The run, its jobs, its attempts, the frozen snapshot and
every translation artifact are opened read-only. Output goes to a draft directory outside the
canonical store.

**It invents nothing.** Chapter and chunk order come from the frozen snapshot manifest; the
paragraph join rule is the release engine's own ``assemble_chapter``/``assemble_book``; headings
come from ``chapter_heading``. Every translation file is proved against the SHA-256 its acceptance
evidence recorded before a byte of it is used.

**It never launders a QA verdict.** A QA-rejected chunk is included, because an unreadable hole is
worse than a flagged paragraph, but it is included *as rejected*: the draft manifest records every
chunk's true acceptance status, and the draft directory carries no ``manifest.yaml`` that release
verification could mistake for a published release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import yaml

from book_translator.epub.engine import (
    CONTAINER_PATH,
    EPUB_BUILDER_VERSION,
    MIMETYPE,
    MIMETYPE_PATH,
    NAVIGATION_PATH,
    PACKAGE_PATH,
    STYLESHEET_PATH,
    book_identifier,
    chapter_href,
    render_chapter,
    render_container,
    render_navigation,
    render_package,
    render_stylesheet,
)
from book_translator.epub.identity import epub_content_address, epub_id
from book_translator.epub.models import EpubFile
from book_translator.epub.validation import validate_archive
from book_translator.epub.workflow import (
    FIXED_ARCHIVE_TIMESTAMP,
    FIXED_EXTERNAL_ATTRIBUTES,
    read_archive,
)
from book_translator.models.book import TocKind
from book_translator.release_assembly.engine import (
    RELEASE_ASSEMBLY_ENGINE_VERSION,
    ReleaseAssemblyError,
    assemble_book,
    assemble_chapter,
    chapter_filename,
    chapter_heading,
    word_count,
)

DRAFT_ASSEMBLER_VERSION = "1.0.0"
BOOK_FILENAME = "book.md"
CHAPTERS_DIRECTORY = "chapters"
DRAFT_MANIFEST_FILENAME = "draft-manifest.yaml"


class DraftAssemblyError(RuntimeError):
    """Raised when the draft cannot be assembled from existing bytes alone."""


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def verified_bytes(path: Path, expected: str, label: str) -> bytes:
    if not path.is_file():
        raise DraftAssemblyError(f"{label} is missing: {path}")
    content = path.read_bytes()
    actual = sha256_bytes(content)
    if actual != expected:
        raise DraftAssemblyError(f"{label} is not the recorded bytes: {path}")
    return content


@dataclass(frozen=True, slots=True)
class ResolvedChunk:
    """One chunk's effective translated bytes and the verdict they stand under."""

    chunk_id: str
    chapter_id: str
    sequence: int
    acceptance_status: str
    acceptance_origin: str
    evidence_file: str
    translation_file: str
    translation_sha256: str
    source_sha256: str
    text: str


def resolve_chunk(job: dict, project_root: Path) -> ResolvedChunk:
    """Resolve one job's effective translation, accepted or rejected, and prove its bytes.

    This mirrors ``release_assembly.workflow.load_effective_acceptance`` — the same supersession
    chain of re-evaluation, structural repair and adopted semantic revision, each link proved
    against the hash that named it — with exactly one difference: the effective status is recorded
    rather than required to be ``accepted``.
    """
    evidence_file = job["acceptance_file"]
    evidence_sha256 = job["acceptance_sha256"]
    if evidence_file is None or evidence_sha256 is None:
        raise DraftAssemblyError(f"job {job['job_id']} carries no acceptance evidence")
    path = project_root / evidence_file
    verified_bytes(path, evidence_sha256, "acceptance evidence")
    evidence = load_yaml(path)
    status = evidence["status"]

    for file_key, hash_key, status_key, label in (
        (
            "latest_reevaluation_file",
            "latest_reevaluation_sha256",
            "resulting_status",
            "re-evaluation",
        ),
        (
            "latest_structural_repair_file",
            "latest_structural_repair_sha256",
            "resulting_status",
            "structural repair",
        ),
    ):
        if not job.get(file_key):
            continue
        superseding = project_root / job[file_key]
        verified_bytes(superseding, job[hash_key], f"{label} evidence")
        evidence = load_yaml(superseding)
        evidence_file = job[file_key]
        evidence_sha256 = job[hash_key]
        status = evidence[status_key]

    translation_file = evidence["translation_file"]
    translation_sha256 = evidence["translation_sha256"]
    if job.get("latest_semantic_revision_file"):
        revision_path = project_root / job["latest_semantic_revision_file"]
        verified_bytes(
            revision_path, job["latest_semantic_revision_sha256"], "semantic revision evidence"
        )
        revision = load_yaml(revision_path)
        if revision.get("adopted") and revision.get("translation_file"):
            translation_file = revision["translation_file"]
            translation_sha256 = revision["translation_sha256"]

    content = verified_bytes(project_root / translation_file, translation_sha256, "translated text")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DraftAssemblyError(f"translation for {job['chunk_id']} is not valid UTF-8") from exc

    return ResolvedChunk(
        chunk_id=job["chunk_id"],
        chapter_id=job["chapter_id"],
        sequence=job["chunk_sequence"],
        acceptance_status=status,
        acceptance_origin=job["acceptance_origin"],
        evidence_file=evidence_file,
        translation_file=translation_file,
        translation_sha256=translation_sha256,
        source_sha256=evidence["source_sha256"],
        text=text,
    )


def assemble(run_id: str, project_root: Path) -> tuple[dict, list[tuple[str, str, str, int]]]:
    """Render every chapter of one run in memory. Returns the draft manifest and the chapters."""
    run_path = project_root / "workspace" / "translation-runs" / run_id
    if not run_path.is_dir():
        raise DraftAssemblyError(f"no such translation run: {run_id}")
    run = load_yaml(run_path / "run.yaml")

    snapshot = load_yaml(project_root / run["snapshot_path"] / "manifest.yaml")
    jobs = {
        directory.name: load_yaml(directory / "job.yaml")
        for directory in sorted(path for path in (run_path / "jobs").iterdir() if path.is_dir())
    }

    chapters: list[tuple[str, str, str, int]] = []
    chapter_records: list[dict] = []
    chunk_records: list[dict] = []
    chapter_markdown: list[str] = []
    expected_chunks: list[str] = []
    assembled_chunks: list[str] = []

    for chapter_sequence, chapter in enumerate(snapshot["chapters"], start=1):
        heading, heading_source = chapter_heading(
            title_target=chapter["title_fa"], title_source=chapter["title_en"]
        )
        chunk_texts: list[tuple[str, str]] = []
        for chunk in chapter["chunks"]:
            expected_chunks.append(chunk["chunk_id"])
            job = jobs.get(chunk["chunk_id"])
            if job is None:
                raise DraftAssemblyError(f"no translation job exists for chunk {chunk['chunk_id']}")
            resolved = resolve_chunk(job, project_root)
            if resolved.source_sha256 != chunk["source_sha256"]:
                raise DraftAssemblyError(
                    f"the translation for chunk {chunk['chunk_id']} answers a different source"
                )
            assembled_chunks.append(resolved.chunk_id)
            chunk_texts.append((resolved.chunk_id, resolved.text))
            chunk_records.append(
                {
                    "chunk_id": resolved.chunk_id,
                    "chapter_id": resolved.chapter_id,
                    "sequence": resolved.sequence,
                    "acceptance_status": resolved.acceptance_status,
                    "acceptance_origin": resolved.acceptance_origin,
                    "acceptance_evidence_file": resolved.evidence_file,
                    "translation_file": resolved.translation_file,
                    "translation_sha256": resolved.translation_sha256,
                }
            )

        markdown, contributions = assemble_chapter(
            chapter_id=chapter["chapter_id"], heading=heading, chunk_texts=tuple(chunk_texts)
        )
        filename = chapter_filename(
            kind=TocKind(chapter["kind"]),
            number=chapter["number"],
            chapter_id=chapter["chapter_id"],
        )
        chapter_markdown.append(markdown)
        chapters.append((chapter["chapter_id"], filename, markdown, chapter_sequence))
        chapter_records.append(
            {
                "chapter_id": chapter["chapter_id"],
                "chapter_sequence": chapter_sequence,
                "file": f"{CHAPTERS_DIRECTORY}/{filename}",
                "heading": heading,
                "heading_source": heading_source.value,
                "kind": chapter["kind"],
                "number": chapter["number"],
                "chunk_count": len(contributions),
                "paragraph_count": sum(len(texts) for _, texts in contributions) + 1,
                "word_count": word_count(markdown),
                "sha256": sha256_bytes(markdown.encode("utf-8")),
            }
        )

    if sorted(assembled_chunks) != sorted(expected_chunks):
        raise DraftAssemblyError("the draft does not cover exactly the frozen chunks")
    if len(set(assembled_chunks)) != len(assembled_chunks):
        raise DraftAssemblyError("the draft repeats a chunk")
    if assembled_chunks != expected_chunks:
        raise DraftAssemblyError("the draft does not follow frozen chunk order")

    book = assemble_book(tuple(chapter_markdown))
    rejected = [record for record in chunk_records if record["acceptance_status"] != "accepted"]
    manifest = {
        "schema_version": "1.0",
        "artifact_kind": "readable_draft",
        "is_release": False,
        "draft_assembler_version": DRAFT_ASSEMBLER_VERSION,
        "release_assembly_engine_version": RELEASE_ASSEMBLY_ENGINE_VERSION,
        "source_translation_run": run["run_id"],
        "source_translation_run_status": run["status"],
        "snapshot_id": run["snapshot_id"],
        "snapshot_manifest_sha256": run["snapshot_manifest_sha256"],
        "book_id": snapshot["book"]["book_id"],
        "title_en": snapshot["book"]["title_en"],
        "title_fa": snapshot["book"]["title_fa"],
        "author": snapshot["book"]["author"],
        "source_language": run["source_language"],
        "target_language": run["target_language"],
        "chapter_count": len(chapter_records),
        "chunk_count": len(chunk_records),
        "accepted_chunk_count": len(chunk_records) - len(rejected),
        "qa_rejected_chunk_count": len(rejected),
        "qa_rejected_chunks": [record["chunk_id"] for record in rejected],
        "provider_calls": 0,
        "book_word_count": word_count(book),
        "book_sha256": sha256_bytes(book.encode("utf-8")),
        "chapters": chapter_records,
        "chunks": chunk_records,
    }
    chapters.append(("__book__", BOOK_FILENAME, book, 0))
    return manifest, chapters


def build_epub(
    manifest: dict, chapters: list[tuple[str, str, str, int]], metadata: dict, modified: str
) -> tuple[bytes, dict]:
    """Render the draft EPUB from the assembled chapters, with no clock and no filesystem."""
    title = (manifest["title_fa"] or "").strip() or manifest["title_en"].strip()
    language = manifest["target_language"]
    content_address = manifest["book_sha256"]
    identifier = book_identifier(content_address)

    files = [
        EpubFile(path=MIMETYPE_PATH, content=MIMETYPE.encode("ascii"), compress=False),
        EpubFile(path=CONTAINER_PATH, content=render_container().encode("utf-8")),
    ]
    navigation: list[tuple[str, str]] = []
    ordered = [item for item in chapters if item[0] != "__book__"]
    for record, (chapter_id, _filename, markdown, _sequence) in zip(
        manifest["chapters"], ordered, strict=True
    ):
        document, chapter_title = render_chapter(
            chapter_id=chapter_id,
            markdown=markdown,
            language=language,
            expected_heading=record["heading"],
        )
        files.append(
            EpubFile(path=f"OEBPS/{chapter_href(chapter_id)}", content=document.encode("utf-8"))
        )
        navigation.append((chapter_id, chapter_title))

    files.append(EpubFile(path=STYLESHEET_PATH, content=render_stylesheet().encode("utf-8")))
    files.append(
        EpubFile(
            path=NAVIGATION_PATH,
            content=render_navigation(
                book_title=title, language=language, chapters=tuple(navigation)
            ).encode("utf-8"),
        )
    )
    files.append(
        EpubFile(
            path=PACKAGE_PATH,
            content=render_package(
                identifier=identifier,
                title=title,
                author=metadata["author"],
                language=language,
                modified=modified,
                source=content_address,
                release_id=manifest["draft_id"],
                series_name=metadata.get("series_name"),
                series_index=metadata.get("series_index"),
                chapters=tuple(chapter_id for chapter_id, _, _, _ in ordered),
            ).encode("utf-8"),
        )
    )

    mimetype = [item for item in files if item.path == MIMETYPE_PATH]
    rest = sorted((item for item in files if item.path != MIMETYPE_PATH), key=lambda x: x.path)
    archive_files = (mimetype[0], *rest)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for item in archive_files:
            info = zipfile.ZipInfo(item.path, date_time=FIXED_ARCHIVE_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED if item.compress else zipfile.ZIP_STORED
            info.external_attr = FIXED_EXTERNAL_ATTRIBUTES
            info.create_system = 3
            archive.writestr(info, item.content)
    content = buffer.getvalue()

    names, stored = read_archive(content)
    problems = validate_archive(
        names,
        stored,
        expected_chapters=tuple(navigation),
        expected_title=title,
        expected_language=language,
    )
    if problems:
        raise DraftAssemblyError("the draft EPUB failed its own validation: " + "; ".join(problems))

    address = epub_content_address(
        tuple((item.path, sha256_bytes(item.content)) for item in archive_files)
    )
    inventory = epub_content_address(tuple((name, sha256_bytes(stored[name])) for name in names))
    if inventory != address:
        raise DraftAssemblyError("the draft EPUB's content address is not the one it derived")
    return content, {
        "epub_builder_version": EPUB_BUILDER_VERSION,
        "epub_id": epub_id(address),
        "epub_content_sha256": address,
        "book_identifier": identifier,
        "entry_count": len(archive_files),
        "title": title,
        "chapter_count": len(navigation),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_id")
    parser.add_argument("--output", required=True, help="draft directory, outside workspace/")
    parser.add_argument("--epub", help="draft EPUB path; omit to skip the EPUB")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    destination = (project_root / args.output).resolve()
    if destination.is_relative_to(project_root / "workspace"):
        raise DraftAssemblyError("a readable draft may never be written into workspace/")
    if destination.exists() and not (args.force or args.dry_run):
        raise DraftAssemblyError(f"draft already exists: {destination}; pass --force to replace it")

    manifest, chapters = assemble(args.run_id, project_root)
    manifest["draft_id"] = destination.name

    run = load_yaml(project_root / "workspace" / "translation-runs" / args.run_id / "run.yaml")
    snapshot = load_yaml(project_root / run["snapshot_path"] / "manifest.yaml")
    metadata = load_yaml(project_root / snapshot["book"]["metadata_file"])
    modified = (
        datetime.fromisoformat(run["completed_at"])
        .astimezone(UTC)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    epub_content: bytes | None = None
    epub_report: dict = {}
    if args.epub:
        epub_content, epub_report = build_epub(manifest, chapters, metadata, modified)
        manifest["epub"] = dict(epub_report)
        manifest["epub"]["file"] = args.epub
        manifest["epub"]["archive_sha256"] = sha256_bytes(epub_content)
        manifest["epub"]["byte_count"] = len(epub_content)

    if args.dry_run:
        print(
            json.dumps(
                {**{k: v for k, v in manifest.items() if k not in ("chapters", "chunks")}},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if destination.exists():
        shutil.rmtree(destination)
    (destination / CHAPTERS_DIRECTORY).mkdir(parents=True)
    for chapter_id, filename, markdown, _sequence in chapters:
        target = (
            destination / filename
            if chapter_id == "__book__"
            else destination / CHAPTERS_DIRECTORY / filename
        )
        target.write_text(markdown, encoding="utf-8")
    (destination / DRAFT_MANIFEST_FILENAME).write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    if epub_content is not None:
        epub_path = (project_root / args.epub).resolve()
        epub_path.parent.mkdir(parents=True, exist_ok=True)
        epub_path.write_bytes(epub_content)

    print(
        json.dumps(
            {
                "draft": str(destination.relative_to(project_root)),
                "source_translation_run": manifest["source_translation_run"],
                "chapters": manifest["chapter_count"],
                "chunks": manifest["chunk_count"],
                "accepted": manifest["accepted_chunk_count"],
                "qa_rejected_included": manifest["qa_rejected_chunk_count"],
                "provider_calls": 0,
                "book_sha256": manifest["book_sha256"],
                "epub": manifest.get("epub", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (DraftAssemblyError, ReleaseAssemblyError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
