"""Read and normalize embedded PyMuPDF TOC data, or discover a TOC from heading lines."""

from dataclasses import dataclass
from pathlib import Path

import pymupdf
from pydantic import ValidationError

from book_translator.hashing import sha256_file
from book_translator.line_layout import LineLayoutError, discover_headings, letter_key
from book_translator.models import SCHEMA_VERSION, TocDocument, TocEntry, TocKind
from book_translator.pdf_inspector import PdfError, open_pdf
from book_translator.toc_parser import RawTocEntry, TocNormalizationError, normalize_toc_entries


class TocDiscoveryError(ValueError):
    """Raised when TOC discovery cannot complete safely."""


@dataclass(frozen=True, slots=True)
class TocDiscoveryResult:
    path: Path
    page_count: int
    raw_entry_count: int
    document: TocDocument


def discover_embedded_toc(path: Path, *, source_pdf: Path | None = None) -> TocDiscoveryResult:
    try:
        resolved, pdf = open_pdf(path)
    except PdfError as exc:
        raise TocDiscoveryError(str(exc)) from exc
    try:
        try:
            raw_data = pdf.get_toc()
        except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
            raise TocDiscoveryError(f"could not read embedded TOC from {resolved}: {exc}") from exc
        if not raw_data:
            raise TocDiscoveryError(
                "PDF has no embedded TOC; text-based fallback discovery is not implemented yet"
            )
        raw_entries: list[RawTocEntry] = []
        for position, item in enumerate(raw_data, 1):
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                raise TocDiscoveryError(f"embedded TOC entry {position} is malformed")
            raw_entries.append(RawTocEntry(level=item[0], title=item[1], page=item[2]))
        try:
            entries = normalize_toc_entries(raw_entries, pdf.page_count)
            document = TocDocument(
                schema_version=SCHEMA_VERSION,
                source="embedded",
                source_pdf=source_pdf if source_pdf is not None else path,
                source_pdf_sha256=sha256_file(resolved),
                pdf_page_count=pdf.page_count,
                entry_count=len(entries),
                entries=entries,
            )
        except (TocNormalizationError, ValidationError) as exc:
            raise TocDiscoveryError(f"invalid embedded TOC: {exc}") from exc
        return TocDiscoveryResult(resolved, pdf.page_count, len(raw_entries), document)
    finally:
        pdf.close()  # type: ignore[no-untyped-call]


def sorted_text_blocks(page: pymupdf.Page) -> list[str]:
    """The page's text blocks in PyMuPDF sorted order: the index space of TOC block anchors."""
    blocks = page.get_text("blocks", sort=True)  # type: ignore[no-untyped-call]
    return [str(block[4]) for block in blocks if len(block) > 6 and block[6] == 0]


def discover_text_heading_toc(
    path: Path, *, book_title: str, source_pdf: Path | None = None
) -> TocDiscoveryResult:
    """Discover a line-layout PDF's narrative structure from its heading lines.

    Used only when a PDF has no embedded TOC. Entries carry sub-page block anchors because such
    a PDF begins chapters mid-page; text before the first heading is one front-matter entry and
    the glossary is an appendix running to the last page.
    """
    try:
        resolved, pdf = open_pdf(path)
    except PdfError as exc:
        raise TocDiscoveryError(str(exc)) from exc
    try:
        try:
            pages = [(index + 1, sorted_text_blocks(pdf[index])) for index in range(pdf.page_count)]
            headings = discover_headings(pages)
        except (pymupdf.FileDataError, RuntimeError, LineLayoutError) as exc:
            raise TocDiscoveryError(f"text heading discovery failed for {resolved}: {exc}") from exc
        if not headings or headings[0].word == "GLOSSARY":
            raise TocDiscoveryError("no narrative headings were found in the PDF text")
        page_count = pdf.page_count
        entries: list[TocEntry] = []
        first = headings[0]
        if first.page > 1 or first.block > 0:
            entries.append(
                TocEntry(
                    entry_id="front-matter-001",
                    level=1,
                    kind=TocKind.FRONT_MATTER,
                    title_en="Front Matter",
                    start_page=1,
                    end_page=first.page if first.block > 0 else first.page - 1,
                    source_toc_title="text before the first heading",
                    start_block=0,
                    end_block=first.block if first.block > 0 else None,
                    heading_group_count=0,
                )
            )
        kinds = {
            "PROLOGUE": TocKind.PROLOGUE,
            "CHAPTER": TocKind.CHAPTER,
            "EPILOGUE": TocKind.EPILOGUE,
            "GLOSSARY": TocKind.APPENDIX,
        }
        for index, heading in enumerate(headings):
            following = headings[index + 1] if index + 1 < len(headings) else None
            kind = kinds[heading.word]
            if kind is TocKind.CHAPTER:
                assert heading.number is not None
                entry_id = f"chapter-{heading.number:03d}"
            elif kind is TocKind.APPENDIX:
                entry_id = "appendix-001"
            else:
                entry_id = heading.word.lower()
            if following is None:
                end_page, end_block = page_count, None
            elif following.block > 0:
                end_page, end_block = following.page, following.block
            else:
                end_page, end_block = following.page - 1, None
            source_title = " ".join(part for part in (heading.source_text, heading.title) if part)
            if heading.number_inferred:
                source_title += f" [number {heading.number} inferred from sequence]"
            entries.append(
                TocEntry(
                    entry_id=entry_id,
                    level=1,
                    kind=kind,
                    number=heading.number,
                    title_en=heading.title if heading.title else heading.word.title(),
                    start_page=heading.page,
                    end_page=end_page,
                    source_toc_title=source_title,
                    start_block=heading.block,
                    end_block=end_block,
                    heading_group_count=heading.group_count,
                )
            )
        keys = {letter_key(book_title)} | {letter_key(entry.title_en) for entry in entries}
        keys.discard("")
        keys.discard(letter_key("Front Matter"))
        try:
            document = TocDocument(
                schema_version=SCHEMA_VERSION,
                source="text_headings",
                layout="line_blocks",
                running_head_keys=sorted(keys),
                source_pdf=source_pdf if source_pdf is not None else path,
                source_pdf_sha256=sha256_file(resolved),
                pdf_page_count=page_count,
                entry_count=len(entries),
                entries=entries,
            )
        except ValidationError as exc:
            raise TocDiscoveryError(f"invalid text-heading TOC: {exc}") from exc
        return TocDiscoveryResult(resolved, page_count, len(headings), document)
    finally:
        pdf.close()  # type: ignore[no-untyped-call]
