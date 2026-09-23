"""Bounded local PDF evidence collection for book metadata resolution.

Only embedded document information and short front/back matter lines are read. Prose is never
retained, printed, or persisted: long lines are discarded before the pure engine sees them.
"""

from pathlib import Path

import pymupdf

from book_translator.book_metadata.engine import (
    MAXIMUM_EVIDENCE_LINE_LENGTH,
    EvidenceLine,
    PdfBookEvidence,
)
from book_translator.pdf_inspector import PdfError, open_pdf

FRONT_MATTER_PAGES = 12
BACK_MATTER_PAGES = 3


def _document_value(metadata: dict[str, str | None], key: str) -> str | None:
    value = metadata.get(key)
    return value.strip() if value and value.strip() else None


def _evidence_pages(page_count: int) -> list[int]:
    front = range(1, min(FRONT_MATTER_PAGES, page_count) + 1)
    back = range(max(1, page_count - BACK_MATTER_PAGES + 1), page_count + 1)
    return sorted(set(front) | set(back))


def collect_pdf_evidence(path: Path) -> PdfBookEvidence:
    """Read document information plus short bibliographic lines from bounded matter pages."""
    _, document = open_pdf(path)
    try:
        lines: list[EvidenceLine] = []
        for page_number in _evidence_pages(document.page_count):
            page: pymupdf.Page = document[page_number - 1]
            text: str = page.get_text("text")  # type: ignore[no-untyped-call]
            for raw in text.replace(" ", " ").splitlines():
                candidate = " ".join(raw.split())
                if candidate and len(candidate) <= MAXIMUM_EVIDENCE_LINE_LENGTH:
                    lines.append(EvidenceLine(page_number, candidate))
        raw_metadata: dict[str, str | None] = document.metadata or {}
        return PdfBookEvidence(
            page_count=document.page_count,
            title=_document_value(raw_metadata, "title"),
            author=_document_value(raw_metadata, "author"),
            subject=_document_value(raw_metadata, "subject"),
            keywords=_document_value(raw_metadata, "keywords"),
            matter_lines=tuple(lines),
        )
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        raise PdfError(f"could not read book metadata evidence from {path}: {exc}") from exc
    finally:
        document.close()  # type: ignore[no-untyped-call]
