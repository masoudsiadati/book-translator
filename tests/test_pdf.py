import os
from pathlib import Path

import pymupdf
import pytest
from typer.testing import CliRunner

from book_translator.atomic_write import atomic_write_text
from book_translator.cli import app
from book_translator.pdf_extractor import extract_pages
from book_translator.pdf_inspector import PdfError, inspect_pdf

runner = CliRunner()


def create_pdf(
    path: Path,
    page_texts: list[str | None],
    *,
    metadata: dict[str, str] | None = None,
) -> None:
    document = pymupdf.open()  # type: ignore[no-untyped-call]
    for text in page_texts:
        page = document.new_page()
        if text:
            text_area = pymupdf.Rect(72, 72, 540, 760)  # type: ignore[no-untyped-call]
            page.insert_textbox(
                text_area,
                text,
                fontsize=11,
            )
    if metadata:
        document.set_metadata(metadata)
    document.save(path)  # type: ignore[no-untyped-call]
    document.close()  # type: ignore[no-untyped-call]


def meaningful_text(label: str) -> str:
    return " ".join([label, *[f"word{number}" for number in range(1, 25)]])


def test_inspection_of_text_based_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "text.pdf"
    create_pdf(
        pdf_path,
        [meaningful_text("alpha"), meaningful_text("beta")],
        metadata={"title": "Test title", "author": "Test author"},
    )

    result = inspect_pdf(pdf_path)

    assert result.path == pdf_path.resolve()
    assert result.page_count == 2
    assert result.pages_with_text == 2
    assert result.pages_without_text == 0
    assert result.total_word_count == 50
    assert result.minimum_words_per_page == 25
    assert result.maximum_words_per_page == 25
    assert result.first_page_with_text == 1
    assert result.last_page_with_text == 2
    assert result.classification == "text-based"
    assert result.metadata.title == "Test title"
    assert result.metadata.author == "Test author"


def test_inspection_classifies_blank_pdf_as_image_based(tmp_path: Path) -> None:
    pdf_path = tmp_path / "blank.pdf"
    create_pdf(pdf_path, [None, None])

    result = inspect_pdf(pdf_path)

    assert result.pages_with_text == 0
    assert result.pages_without_text == 2
    assert result.total_word_count == 0
    assert result.first_page_with_text is None
    assert result.last_page_with_text is None
    assert result.classification == "image-based"


def test_missing_pdf_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PdfError, match="PDF file not found"):
        inspect_pdf(tmp_path / "missing.pdf")


def test_invalid_pdf_is_rejected(tmp_path: Path) -> None:
    invalid_pdf = tmp_path / "invalid.pdf"
    invalid_pdf.write_text("this is not a PDF", encoding="utf-8")

    result = runner.invoke(app, ["inspect-pdf", str(invalid_pdf)])

    assert result.exit_code == 1
    assert "Error:" in result.stderr
    assert "could not open PDF" in result.stderr


def test_successful_inclusive_page_range_extraction_is_one_based(tmp_path: Path) -> None:
    pdf_path = tmp_path / "three-pages.pdf"
    output_path = tmp_path / "nested" / "pages.txt"
    create_pdf(pdf_path, ["FIRST_PAGE_SECRET", "SECOND_PAGE_SECRET", "THIRD_PAGE_SECRET"])

    result = extract_pages(
        pdf_path,
        from_page=2,
        to_page=3,
        output_path=output_path,
    )
    output = output_path.read_text(encoding="utf-8")

    assert result.page_count == 2
    assert result.word_count == 2
    assert "FIRST_PAGE_SECRET" not in output
    assert "SECOND_PAGE_SECRET" in output
    assert "THIRD_PAGE_SECRET" in output
    assert "<!-- source-page: 2 -->" in output
    assert "<!-- source-page: 3 -->" in output
    assert output.index("source-page: 2") < output.index("source-page: 3")


@pytest.mark.parametrize(
    ("from_page", "to_page", "message"),
    [
        (0, 1, "from-page must be at least 1"),
        (2, 1, "to-page must be greater than or equal"),
        (1, 3, "outside PDF page range 1-2"),
        (3, 3, "outside PDF page range 1-2"),
    ],
)
def test_invalid_page_ranges(tmp_path: Path, from_page: int, to_page: int, message: str) -> None:
    pdf_path = tmp_path / "two-pages.pdf"
    create_pdf(pdf_path, ["one", "two"])

    with pytest.raises(PdfError, match=message):
        extract_pages(
            pdf_path,
            from_page=from_page,
            to_page=to_page,
            output_path=tmp_path / "output.txt",
        )


def test_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    pdf_path = tmp_path / "book.pdf"
    output_path = tmp_path / "existing.txt"
    create_pdf(pdf_path, ["new content"])
    output_path.write_text("preserve me", encoding="utf-8")

    with pytest.raises(PdfError, match="output file already exists"):
        extract_pages(pdf_path, from_page=1, to_page=1, output_path=output_path)

    assert output_path.read_text(encoding="utf-8") == "preserve me"


def test_force_allows_overwrite(tmp_path: Path) -> None:
    pdf_path = tmp_path / "book.pdf"
    output_path = tmp_path / "existing.txt"
    create_pdf(pdf_path, ["replacement content"])
    output_path.write_text("old content", encoding="utf-8")

    extract_pages(
        pdf_path,
        from_page=1,
        to_page=1,
        output_path=output_path,
        force=True,
    )

    output = output_path.read_text(encoding="utf-8")
    assert "replacement content" in output
    assert "old content" not in output


def test_atomic_write_replaces_from_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "output.txt"
    calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source: str | Path, target: str | Path) -> None:
        source_path = Path(source)
        calls.append((source_path, Path(target)))
        assert source_path.parent == destination.parent
        assert source_path.exists()
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", recording_replace)

    atomic_write_text(destination, "UTF-8: فارسی\n")

    assert calls and calls[0][1] == destination
    assert destination.read_text(encoding="utf-8") == "UTF-8: فارسی\n"
    assert not calls[0][0].exists()


def test_extract_command_never_prints_extracted_text(tmp_path: Path) -> None:
    pdf_path = tmp_path / "book.pdf"
    output_path = tmp_path / "output.txt"
    secret = "NEVER_PRINT_THIS_BOOK_TEXT"
    create_pdf(pdf_path, [secret])

    result = runner.invoke(
        app,
        [
            "extract-pages",
            str(pdf_path),
            "--from-page",
            "1",
            "--to-page",
            "1",
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert secret in output_path.read_text(encoding="utf-8")
