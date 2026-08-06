from pathlib import Path
from typing import Literal

import pymupdf
from pydantic import BaseModel, ConfigDict


class PdfError(ValueError):
    """Raised when a PDF cannot be safely inspected or extracted."""


class PdfMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    author: str | None = None
    subject: str | None = None
    creator: str | None = None


class PdfInspectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    file_size: int
    page_count: int
    encrypted: bool
    metadata: PdfMetadata
    pages_with_text: int
    pages_without_text: int
    total_word_count: int
    average_words_per_page: float
    minimum_words_per_page: int
    maximum_words_per_page: int
    first_page_with_text: int | None
    last_page_with_text: int | None
    classification: Literal["text-based", "mixed", "image-based"]


def validate_pdf_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise PdfError(f"PDF file not found: {resolved}")
    if not resolved.is_file():
        raise PdfError(f"PDF path is not a regular file: {resolved}")
    return resolved


def open_pdf(path: Path) -> tuple[Path, pymupdf.Document]:
    resolved = validate_pdf_path(path)
    try:
        document = pymupdf.open(resolved)  # type: ignore[no-untyped-call]
    except (pymupdf.FileDataError, pymupdf.EmptyFileError, OSError) as exc:
        raise PdfError(f"could not open PDF {resolved}: {exc}") from exc

    if document.needs_pass:
        document.close()  # type: ignore[no-untyped-call]
        raise PdfError(f"PDF is encrypted and requires a password: {resolved}")
    if document.page_count == 0:
        document.close()  # type: ignore[no-untyped-call]
        raise PdfError(f"PDF contains no pages: {resolved}")
    return resolved, document


def _normalized_words(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").split()


def _metadata_value(metadata: dict[str, str | None], key: str) -> str | None:
    value = metadata.get(key)
    return value.strip() if value and value.strip() else None


def inspect_pdf(path: Path) -> PdfInspectionResult:
    resolved, document = open_pdf(path)
    try:
        word_counts: list[int] = []
        pages_with_text: list[int] = []
        meaningful_pages = 0
        for page_number in range(1, document.page_count + 1):
            page: pymupdf.Page = document[page_number - 1]
            count = len(
                _normalized_words(page.get_text("text"))  # type: ignore[no-untyped-call]
            )
            word_counts.append(count)
            if count > 0:
                pages_with_text.append(page_number)
            if count >= 20:
                meaningful_pages += 1

        meaningful_ratio = meaningful_pages / document.page_count
        if meaningful_ratio > 0.5:
            classification: Literal["text-based", "mixed", "image-based"] = "text-based"
        elif meaningful_ratio <= 0.1:
            classification = "image-based"
        else:
            classification = "mixed"

        raw_metadata: dict[str, str | None] = document.metadata or {}
        total_words = sum(word_counts)
        return PdfInspectionResult(
            path=resolved,
            file_size=resolved.stat().st_size,
            page_count=document.page_count,
            encrypted=document.is_encrypted,
            metadata=PdfMetadata(
                title=_metadata_value(raw_metadata, "title"),
                author=_metadata_value(raw_metadata, "author"),
                subject=_metadata_value(raw_metadata, "subject"),
                creator=_metadata_value(raw_metadata, "creator"),
            ),
            pages_with_text=len(pages_with_text),
            pages_without_text=document.page_count - len(pages_with_text),
            total_word_count=total_words,
            average_words_per_page=total_words / document.page_count,
            minimum_words_per_page=min(word_counts),
            maximum_words_per_page=max(word_counts),
            first_page_with_text=pages_with_text[0] if pages_with_text else None,
            last_page_with_text=pages_with_text[-1] if pages_with_text else None,
            classification=classification,
        )
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        if isinstance(exc, PdfError):
            raise
        raise PdfError(f"could not read PDF {resolved}: {exc}") from exc
    finally:
        document.close()  # type: ignore[no-untyped-call]
