import re
from pathlib import Path

import pymupdf
from pydantic import BaseModel, ConfigDict

from book_translator.atomic_write import atomic_write_text
from book_translator.pdf_inspector import PdfError, open_pdf


class PdfExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_path: Path
    page_count: int
    word_count: int
    byte_size: int


def normalize_extracted_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{4,}", "\n\n\n", text)


def _extract_page_blocks(page: pymupdf.Page) -> str:
    blocks = page.get_text("blocks", sort=True)  # type: ignore[no-untyped-call]
    text_blocks = [str(block[4]) for block in blocks if len(block) > 6 and block[6] == 0]
    return normalize_extracted_text("\n\n".join(text_blocks)).strip("\n")


def extract_pages(
    pdf_path: Path,
    *,
    from_page: int,
    to_page: int,
    output_path: Path,
    force: bool = False,
) -> PdfExtractionResult:
    output_path = output_path.expanduser()
    if from_page < 1:
        raise PdfError("from-page must be at least 1")
    if to_page < from_page:
        raise PdfError("to-page must be greater than or equal to from-page")
    if output_path.exists() and not force:
        raise PdfError(f"output file already exists: {output_path}")

    resolved, document = open_pdf(pdf_path)
    try:
        if from_page > document.page_count or to_page > document.page_count:
            raise PdfError(
                f"page range {from_page}-{to_page} is outside PDF page range "
                f"1-{document.page_count}"
            )

        sections: list[str] = []
        extracted_word_count = 0
        for user_page_number in range(from_page, to_page + 1):
            page_text = _extract_page_blocks(document[user_page_number - 1])
            extracted_word_count += len(page_text.split())
            marker = f"<!-- source-page: {user_page_number} -->"
            sections.append(f"{marker}\n\n{page_text}" if page_text else marker)

        output_text = normalize_extracted_text("\n\n".join(sections)).rstrip() + "\n"
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        if isinstance(exc, PdfError):
            raise
        raise PdfError(f"could not extract PDF {resolved}: {exc}") from exc
    finally:
        document.close()  # type: ignore[no-untyped-call]

    try:
        atomic_write_text(output_path, output_text, overwrite=force)
    except (FileExistsError, OSError) as exc:
        raise PdfError(f"could not write output {output_path}: {exc}") from exc

    resolved_output = output_path.expanduser().resolve()
    return PdfExtractionResult(
        output_path=resolved_output,
        page_count=to_page - from_page + 1,
        word_count=extracted_word_count,
        byte_size=resolved_output.stat().st_size,
    )
