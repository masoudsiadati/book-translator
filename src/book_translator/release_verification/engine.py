"""Pure release verification. Every check is a function of bytes already read.

Nothing here opens a file, asks the clock, or contacts anything. The workflow layer reads the
release into memory and hands it over; these functions decide what is provable and what is not.

Two habits run through all of it. A check *counts what it examined* against what the manifest
said to expect, so "passed" can never mean "found nothing to look at". And a finding names a
check, a code, and a subject — a file, a chapter, a chunk — never a line of the book.
"""

import json
from collections.abc import Sequence

from book_translator.document_chunking.paragraphs import DocumentParseError, parse_document
from book_translator.models.release import (
    ReleaseArtifactRole,
    ReleaseChapterRecord,
    ReleaseHeadingSource,
    ReleaseLineageDocument,
    ReleaseManifest,
    ReleaseProviderResolution,
)
from book_translator.models.release_verification import (
    ReleaseCheck,
    ReleaseVerificationFinding,
)
from book_translator.release_assembly import (
    assemble_book,
    release_content_address,
    word_count,
)

RELEASE_VERIFICATION_ENGINE_VERSION = "1.0.0"


class ReleaseVerificationError(ValueError):
    """Raised when verification cannot even begin — a release that is not one."""


def finding(
    check: ReleaseCheck, code: str, message: str, *, subject: str | None = None
) -> ReleaseVerificationFinding:
    return ReleaseVerificationFinding(
        check=check, code=code, severity="error", subject=subject, message=message
    )


def check_manifest(
    *,
    manifest: ReleaseManifest,
    published: Sequence[str],
    file_hashes: dict[str, str],
    file_sizes: dict[str, int],
) -> tuple[tuple[ReleaseVerificationFinding, ...], int]:
    """Prove the manifest indexes exactly the published bytes, and addresses itself correctly."""
    findings: list[ReleaseVerificationFinding] = []
    verified = 0
    indexed = {artifact.file.as_posix() for artifact in manifest.artifacts}
    for artifact in manifest.artifacts:
        name = artifact.file.as_posix()
        if name not in file_hashes:
            findings.append(
                finding(
                    ReleaseCheck.MANIFEST, "missing_file", "indexed file is absent", subject=name
                )
            )
            continue
        if file_hashes[name] != artifact.sha256:
            findings.append(
                finding(
                    ReleaseCheck.MANIFEST,
                    "file_hash_mismatch",
                    "file changed after publication",
                    subject=name,
                )
            )
            continue
        if file_sizes[name] != artifact.byte_count:
            findings.append(
                finding(
                    ReleaseCheck.MANIFEST,
                    "file_size_mismatch",
                    "file size does not match the manifest",
                    subject=name,
                )
            )
            continue
        verified += 1
    for name in sorted(set(published) - indexed):
        # An unindexed file is not harmless: the manifest is the release's inventory, and a
        # publication that ships bytes the inventory never mentioned is not the release it claims.
        findings.append(
            finding(
                ReleaseCheck.MANIFEST,
                "unindexed_file",
                "the release holds a file the manifest never indexed",
                subject=name,
            )
        )
    if release_content_address(tuple(manifest.artifacts)) != manifest.content_sha256:
        findings.append(
            finding(
                ReleaseCheck.MANIFEST,
                "content_address_mismatch",
                "content_sha256 does not address the manifest's own file index",
            )
        )
    return tuple(findings), verified


def check_book(
    *, manifest: ReleaseManifest, book_bytes: bytes | None, chapter_texts: dict[str, str]
) -> tuple[ReleaseVerificationFinding, ...]:
    """Prove the assembled book is present, canonical, and exactly its chapters joined."""
    findings: list[ReleaseVerificationFinding] = []
    book_artifact = next(
        (item for item in manifest.artifacts if item.role is ReleaseArtifactRole.BOOK), None
    )
    if book_artifact is None:
        return (
            finding(ReleaseCheck.BOOK_INTEGRITY, "missing_book", "no book artifact is indexed"),
        )
    if book_bytes is None:
        return (
            finding(
                ReleaseCheck.BOOK_INTEGRITY,
                "missing_book",
                "the indexed book file is absent",
                subject=book_artifact.file.as_posix(),
            ),
        )
    try:
        book = book_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return (
            finding(ReleaseCheck.BOOK_INTEGRITY, "invalid_utf8", "the book is not valid UTF-8"),
        )
    missing = [
        record.file.as_posix()
        for record in manifest.chapters
        if record.file.as_posix() not in chapter_texts
    ]
    if missing:
        findings.append(
            finding(
                ReleaseCheck.BOOK_INTEGRITY,
                "chapter_unavailable",
                "the book cannot be reconciled while a chapter file is absent",
                subject=missing[0],
            )
        )
        return tuple(findings)
    joined = assemble_book(
        tuple(chapter_texts[record.file.as_posix()] for record in manifest.chapters)
    )
    if book != joined:
        findings.append(
            finding(
                ReleaseCheck.BOOK_INTEGRITY,
                "book_is_not_its_chapters",
                "the book is not byte-identical to its chapter files joined",
            )
        )
    try:
        paragraphs = parse_document(book)
    except DocumentParseError:
        findings.append(
            finding(
                ReleaseCheck.BOOK_INTEGRITY,
                "noncanonical_markdown",
                "the book is not canonical Markdown",
            )
        )
        return tuple(findings)
    if len(paragraphs) != manifest.paragraph_count:
        findings.append(
            finding(
                ReleaseCheck.BOOK_INTEGRITY,
                "paragraph_count_mismatch",
                "the book's paragraph count does not match the manifest",
            )
        )
    if word_count(book) != manifest.word_count:
        findings.append(
            finding(
                ReleaseCheck.BOOK_INTEGRITY,
                "word_count_mismatch",
                "the book's word count does not match the manifest",
            )
        )
    return tuple(findings)


def check_chapters(
    *, manifest: ReleaseManifest, chapter_texts: dict[str, str]
) -> tuple[tuple[ReleaseVerificationFinding, ...], int]:
    """Prove every chapter is present once, in order, and counted as the manifest says."""
    findings: list[ReleaseVerificationFinding] = []
    verified = 0
    identities = [record.chapter_id for record in manifest.chapters]
    files = [record.file.as_posix() for record in manifest.chapters]
    for label, values in (("chapter", identities), ("file", files)):
        seen: set[str] = set()
        for value in values:
            if value in seen:
                findings.append(
                    finding(
                        ReleaseCheck.CHAPTERS,
                        "duplicate_chapter",
                        f"this {label} is published more than once",
                        subject=value,
                    )
                )
            seen.add(value)
    if [record.chapter_sequence for record in manifest.chapters] != list(
        range(1, len(manifest.chapters) + 1)
    ):
        findings.append(
            finding(
                ReleaseCheck.CHAPTERS,
                "chapter_order_broken",
                "chapter sequences are not contiguous and ordered",
            )
        )
    if len(manifest.chapters) != manifest.chapter_count:
        findings.append(
            finding(
                ReleaseCheck.CHAPTERS,
                "chapter_count_mismatch",
                "the chapter records do not match the manifest chapter count",
            )
        )
    for record in manifest.chapters:
        name = record.file.as_posix()
        text = chapter_texts.get(name)
        if text is None:
            findings.append(
                finding(
                    ReleaseCheck.CHAPTERS, "missing_chapter", "chapter file is absent", subject=name
                )
            )
            continue
        findings.extend(_check_one_chapter(record, text))
        verified += 1
    return tuple(findings), verified


def _check_one_chapter(
    record: ReleaseChapterRecord, text: str
) -> tuple[ReleaseVerificationFinding, ...]:
    findings: list[ReleaseVerificationFinding] = []
    subject = record.chapter_id
    try:
        paragraphs = parse_document(text)
    except DocumentParseError:
        return (
            finding(
                ReleaseCheck.CHAPTERS,
                "noncanonical_chapter",
                "chapter is not canonical Markdown",
                subject=subject,
            ),
        )
    if len(paragraphs) != record.paragraph_count:
        findings.append(
            finding(
                ReleaseCheck.CHAPTERS,
                "chapter_paragraph_mismatch",
                "chapter paragraph count does not match the manifest",
                subject=subject,
            )
        )
    if word_count(text) != record.word_count:
        findings.append(
            finding(
                ReleaseCheck.CHAPTERS,
                "chapter_word_mismatch",
                "chapter word count does not match the manifest",
                subject=subject,
            )
        )
    if paragraphs[0].text != record.heading:
        findings.append(
            finding(
                ReleaseCheck.CHAPTERS,
                "heading_mismatch",
                "the chapter does not open with the heading the manifest recorded",
                subject=subject,
            )
        )
    return tuple(findings)


def check_translations(
    *, manifest: ReleaseManifest, lineage: ReleaseLineageDocument | None
) -> tuple[tuple[ReleaseVerificationFinding, ...], int]:
    """Prove every frozen chunk is present, accepted, and tiles its chapter without a gap."""
    if lineage is None:
        return (
            (
                finding(
                    ReleaseCheck.TRANSLATIONS,
                    "missing_lineage",
                    "the release carries no chunk lineage",
                ),
            ),
            0,
        )
    findings: list[ReleaseVerificationFinding] = []
    if lineage.chunk_count != manifest.chunk_count or len(lineage.chunks) != manifest.chunk_count:
        findings.append(
            finding(
                ReleaseCheck.TRANSLATIONS,
                "chunk_count_mismatch",
                "the lineage does not cover exactly the released chunks",
            )
        )
    if manifest.accepted_translation_count != manifest.chunk_count:
        findings.append(
            finding(
                ReleaseCheck.TRANSLATIONS,
                "unaccepted_chunk_released",
                "the manifest does not claim every released chunk was accepted",
            )
        )
    seen: set[str] = set()
    verified = 0
    by_chapter: dict[str, list[tuple[int, int, int]]] = {}
    for chunk in lineage.chunks:
        if chunk.chunk_id in seen:
            findings.append(
                finding(
                    ReleaseCheck.TRANSLATIONS,
                    "duplicate_chunk",
                    "this chunk appears more than once",
                    subject=chunk.chunk_id,
                )
            )
        seen.add(chunk.chunk_id)
        if chunk.acceptance_status.value != "accepted":
            findings.append(
                finding(
                    ReleaseCheck.TRANSLATIONS,
                    "chunk_not_accepted",
                    "a released chunk is not an accepted translation",
                    subject=chunk.chunk_id,
                )
            )
            continue
        by_chapter.setdefault(chunk.chapter_id, []).append(
            (chunk.chunk_sequence, chunk.paragraph_offset, chunk.translation_paragraph_count)
        )
        verified += 1

    for record in manifest.chapters:
        entries = sorted(by_chapter.get(record.chapter_id, ()))
        if len(entries) != record.chunk_count:
            findings.append(
                finding(
                    ReleaseCheck.TRANSLATIONS,
                    "chapter_chunk_count_mismatch",
                    "the chapter's chunks do not match the count the manifest recorded",
                    subject=record.chapter_id,
                )
            )
            continue
        if [sequence for sequence, _, _ in entries] != list(range(1, len(entries) + 1)):
            findings.append(
                finding(
                    ReleaseCheck.TRANSLATIONS,
                    "chunk_order_broken",
                    "the chapter's chunk sequences are not contiguous",
                    subject=record.chapter_id,
                )
            )
            continue
        # Paragraph 1 of every chapter file is its heading, so the chunks must tile paragraphs
        # 2..n with no gap and no overlap. This is what makes "nothing is missing" checkable
        # from the release alone rather than trusted.
        offset = 2
        for _, start, count in entries:
            if start != offset:
                findings.append(
                    finding(
                        ReleaseCheck.TRANSLATIONS,
                        "paragraph_coverage_gap",
                        "the chapter's chunks do not tile its paragraphs without a gap",
                        subject=record.chapter_id,
                    )
                )
                break
            offset += count
        else:
            if offset - 1 != record.paragraph_count:
                findings.append(
                    finding(
                        ReleaseCheck.TRANSLATIONS,
                        "paragraph_coverage_short",
                        "the chapter's chunks do not account for all of its paragraphs",
                        subject=record.chapter_id,
                    )
                )
    return tuple(findings), verified


def check_provenance(
    *, manifest: ReleaseManifest, lineage: ReleaseLineageDocument | None
) -> tuple[ReleaseVerificationFinding, ...]:
    """Prove every released chunk carries lineage a third party could follow."""
    if lineage is None:
        return (
            finding(
                ReleaseCheck.PROVENANCE, "missing_lineage", "the release carries no chunk lineage"
            ),
        )
    findings: list[ReleaseVerificationFinding] = []
    if len(lineage.chunks) != manifest.chunk_count:
        # Provenance that covers fewer chunks than the release publishes is missing provenance,
        # even when every record it does carry is impeccable.
        findings.append(
            finding(
                ReleaseCheck.PROVENANCE,
                "incomplete_provenance",
                "the lineage does not carry a record for every released chunk",
            )
        )
    if (
        lineage.release_id != manifest.release_id
        or lineage.source_translation_run != manifest.source_translation_run
        or lineage.snapshot_id != manifest.snapshot_id
        or lineage.snapshot_manifest_sha256 != manifest.snapshot_manifest_sha256
    ):
        findings.append(
            finding(
                ReleaseCheck.PROVENANCE,
                "lineage_identity_mismatch",
                "the lineage does not describe this release's run and snapshot",
            )
        )
    for chunk in lineage.chunks:
        subject = chunk.chunk_id
        if not chunk.source_sha256 or not chunk.translation_sha256:
            findings.append(
                finding(
                    ReleaseCheck.PROVENANCE,
                    "missing_hashes",
                    "the chunk does not carry both source and translation hashes",
                    subject=subject,
                )
            )
        if not chunk.acceptance_evidence_sha256 or not str(chunk.acceptance_evidence_file):
            findings.append(
                finding(
                    ReleaseCheck.PROVENANCE,
                    "missing_acceptance_evidence",
                    "the chunk does not name the acceptance evidence it rests on",
                    subject=subject,
                )
            )
        if not chunk.attempt_id or not chunk.attempt_sha256:
            findings.append(
                finding(
                    ReleaseCheck.PROVENANCE,
                    "missing_attempt_lineage",
                    "the chunk does not name the attempt its bytes descend from",
                    subject=subject,
                )
            )
        resolved = chunk.provider_resolution is not ReleaseProviderResolution.UNRESOLVED
        if resolved != (chunk.provider is not None):
            findings.append(
                finding(
                    ReleaseCheck.PROVENANCE,
                    "invalid_provider_resolution",
                    "the provider resolution contradicts the recorded provider",
                    subject=subject,
                )
            )
        if chunk.provider_resolution in {
            ReleaseProviderResolution.CARRY_FORWARD_SOURCE,
            ReleaseProviderResolution.REUSE_SOURCE,
        } and (chunk.lineage_source_run_id is None or chunk.lineage_source_job_id is None):
            findings.append(
                finding(
                    ReleaseCheck.PROVENANCE,
                    "missing_cross_run_lineage",
                    "a carried-forward or reused chunk does not name its source run and job",
                    subject=subject,
                )
            )
    return tuple(findings)


def check_titles(
    *,
    manifest: ReleaseManifest,
    artifact_content_sha256: str | None,
    title_overlay: dict[str, str] | None,
    title_qa_error_codes: Sequence[str],
) -> tuple[tuple[ReleaseVerificationFinding, ...], int]:
    """Prove the headings the release claims came from a title artifact really did."""
    findings: list[ReleaseVerificationFinding] = []
    expected = [
        record
        for record in manifest.chapters
        if record.heading_source is ReleaseHeadingSource.TITLE_TARGET_ARTIFACT
    ]
    if manifest.chapter_title_artifact_id is None:
        if expected:
            findings.append(
                finding(
                    ReleaseCheck.TITLES,
                    "unreferenced_title_artifact",
                    "a heading came from a title artifact the manifest does not reference",
                )
            )
        return tuple(findings), 0
    if title_overlay is None:
        return (
            (
                finding(
                    ReleaseCheck.TITLES,
                    "title_artifact_unavailable",
                    "the referenced chapter-title artifact could not be read",
                    subject=manifest.chapter_title_artifact_id,
                ),
            ),
            0,
        )
    if artifact_content_sha256 != manifest.chapter_title_artifact_sha256:
        findings.append(
            finding(
                ReleaseCheck.TITLES,
                "title_artifact_hash_mismatch",
                "the title artifact's content address is not the one the release recorded",
                subject=manifest.chapter_title_artifact_id,
            )
        )
    for code in sorted(set(title_qa_error_codes)):
        findings.append(
            finding(
                ReleaseCheck.TITLES,
                "title_qa_failed",
                f"the title artifact no longer passes title QA ({code})",
            )
        )
    verified = 0
    for record in expected:
        title = title_overlay.get(record.chapter_id)
        if not title:
            findings.append(
                finding(
                    ReleaseCheck.TITLES,
                    "missing_title",
                    "the title artifact carries no title for this chapter",
                    subject=record.chapter_id,
                )
            )
            continue
        if record.heading != f"# {title}":
            findings.append(
                finding(
                    ReleaseCheck.TITLES,
                    "heading_is_not_its_title",
                    "the published heading is not the title the artifact holds",
                    subject=record.chapter_id,
                )
            )
            continue
        verified += 1
    return tuple(findings), verified


def check_terminology(
    *,
    manifest: ReleaseManifest,
    released_terminology_sha256: str | None,
    frozen_terminology_sha256: str | None,
    released_metadata_sha256: str | None,
    frozen_metadata_sha256: str | None,
) -> tuple[ReleaseVerificationFinding, ...]:
    """Prove the release's copies of the frozen resources are those exact frozen bytes."""
    findings: list[ReleaseVerificationFinding] = []
    for label, declared, released, frozen in (
        (
            "terminology",
            manifest.terminology_sha256,
            released_terminology_sha256,
            frozen_terminology_sha256,
        ),
        (
            "book_metadata",
            manifest.book_metadata_sha256,
            released_metadata_sha256,
            frozen_metadata_sha256,
        ),
    ):
        if declared is None:
            continue
        if released is None:
            findings.append(
                finding(
                    ReleaseCheck.TERMINOLOGY,
                    f"missing_{label}",
                    f"the release references {label} it does not contain",
                )
            )
            continue
        if released != declared:
            findings.append(
                finding(
                    ReleaseCheck.TERMINOLOGY,
                    f"{label}_hash_mismatch",
                    f"the released {label} copy is not the resource the manifest names",
                )
            )
            continue
        if frozen is not None and frozen != declared:
            # The frozen resource is immutable by contract. If it no longer hashes to what the
            # release named, the release is not the one this workspace can still account for.
            findings.append(
                finding(
                    ReleaseCheck.TERMINOLOGY,
                    f"frozen_{label}_drift",
                    f"the workspace's frozen {label} no longer matches the release",
                )
            )
    return tuple(findings)


def report_content_address(payload: dict[str, object]) -> str:
    """Hash the whole verdict except when it was produced."""
    import hashlib

    volatile = {"created_at", "content_sha256"}
    stable = {key: value for key, value in payload.items() if key not in volatile}
    canonical = json.dumps(
        stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
