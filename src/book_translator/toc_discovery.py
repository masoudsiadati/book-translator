"""Read and normalize embedded PyMuPDF table-of-contents data."""

from dataclasses import dataclass
from pathlib import Path

import pymupdf
from pydantic import ValidationError

from book_translator.hashing import sha256_file
from book_translator.models import SCHEMA_VERSION, TocDocument
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
