import hashlib
import os
from pathlib import Path

import pymupdf
import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import TocDocument, TocKind
from book_translator.toc_discovery import TocDiscoveryError, discover_embedded_toc
from book_translator.toc_parser import RawTocEntry, TocNormalizationError, normalize_toc_entries
from book_translator.toc_writer import serialize_toc, write_toc

runner = CliRunner()


def make_pdf(path: Path, pages: int, toc: list[list[object]] | None = None) -> None:
    document = pymupdf.open()  # type: ignore[no-untyped-call]
    for _ in range(pages):
        document.new_page()
    if toc is not None:
        document.set_toc(toc)
    document.save(path)  # type: ignore[no-untyped-call]
    document.close()  # type: ignore[no-untyped-call]


def representative_raw() -> list[RawTocEntry]:
    return [
        RawTocEntry(1, "  Copyright  ", 1),
        RawTocEntry(1, "Prologue (Serpent and Wheel) The First Sparks Fall", 2),
        RawTocEntry(1, 'Chapter 1 (Lion Rampant) "The Fifth, I Give You"', 4),
        RawTocEntry(1, "Chapter 2 A King's Promise, Kept", 7),
        RawTocEntry(1, "Afterword (not a symbol)", 9),
        RawTocEntry(1, "Appendix A", 10),
    ]


def test_normalizes_classifications_symbols_titles_and_ranges() -> None:
    entries = normalize_toc_entries(representative_raw(), 12)

    assert [entry.kind for entry in entries] == [
        TocKind.FRONT_MATTER,
        TocKind.PROLOGUE,
        TocKind.CHAPTER,
        TocKind.CHAPTER,
        TocKind.UNKNOWN,
        TocKind.APPENDIX,
    ]
    assert entries[0].entry_id == "front-matter-001"
    assert entries[0].source_toc_title == "Copyright"
    assert entries[1].number == 0
    assert entries[1].symbol == "Serpent and Wheel"
    assert entries[2].entry_id == "chapter-001"
    assert entries[2].symbol == "Lion Rampant"
    assert entries[2].title_en == '"The Fifth, I Give You"'
    assert entries[3].symbol is None
    assert entries[3].title_en == "A King's Promise, Kept"
    assert entries[4].title_en == "Afterword (not a symbol)"
    assert entries[-1].end_page == 12


def test_book_five_style_level_two_chapters_remain_unchanged() -> None:
    entries = normalize_toc_entries(
        [
            RawTocEntry(1, "Contents", 1),
            RawTocEntry(2, "Prologue (Wheel) First Light", 2),
            RawTocEntry(2, "Chapter 1 (Lion) Hilltop", 4),
            RawTocEntry(2, "Chapter 2 New Arrival", 7),
        ],
        9,
    )

    assert [entry.kind for entry in entries] == [
        TocKind.FRONT_MATTER,
        TocKind.PROLOGUE,
        TocKind.CHAPTER,
        TocKind.CHAPTER,
    ]
    assert [entry.level for entry in entries] == [1, 2, 2, 2]
    assert [entry.number for entry in entries] == [None, 0, 1, 2]
    assert [entry.title_en for entry in entries[1:]] == ["First Light", "Hilltop", "New Arrival"]
    assert [entry.symbol for entry in entries[1:]] == ["Wheel", "Lion", None]


def test_book_six_style_level_one_entries_are_classified_by_title_prefix() -> None:
    entries = normalize_toc_entries(
        [
            RawTocEntry(1, "Contents", 1),
            RawTocEntry(1, "Prologue: First Message", 2),
            RawTocEntry(1, "1 Lion on the Ridge", 4),
            RawTocEntry(1, "2 A New Visitor", 7),
            RawTocEntry(1, "55 Wells at Dusk", 10),
            RawTocEntry(1, "Epilogue: The Reply", 12),
        ],
        13,
    )

    assert [entry.kind for entry in entries] == [
        TocKind.FRONT_MATTER,
        TocKind.PROLOGUE,
        TocKind.CHAPTER,
        TocKind.CHAPTER,
        TocKind.CHAPTER,
        TocKind.EPILOGUE,
    ]
    assert [entry.level for entry in entries] == [1, 1, 1, 1, 1, 1]
    assert [entry.entry_id for entry in entries[1:]] == [
        "prologue",
        "chapter-001",
        "chapter-002",
        "chapter-055",
        "epilogue",
    ]
    assert [entry.number for entry in entries[1:]] == [0, 1, 2, 55, None]
    assert [entry.title_en for entry in entries[1:]] == [
        "First Message",
        "Lion on the Ridge",
        "A New Visitor",
        "Wells at Dusk",
        "The Reply",
    ]


@pytest.mark.parametrize(
    ("raw_title", "title", "symbol"),
    [
        ("chapter   42   (Dice)   Before the Arrow", "Before the Arrow", "Dice"),
        ("CHAPTER 56 Glowing Embers", "Glowing Embers", None),
        ("prologue   The Beginning", "The Beginning", None),
        ("Prologue (Wheel) A Title (Later)", "A Title (Later)", "Wheel"),
    ],
)
def test_structural_matching_is_tolerant(raw_title: str, title: str, symbol: str | None) -> None:
    entry = normalize_toc_entries([RawTocEntry(1, raw_title, 1)], 1)[0]
    assert entry.title_en == title
    assert entry.symbol == symbol


@pytest.mark.parametrize("title", ["Chapter", "Chapter Seven", "Chapter 7", "Chapter 7 () Title"])
def test_malformed_chapter_like_entry_is_unknown(title: str) -> None:
    entries = normalize_toc_entries(
        [RawTocEntry(1, "Chapter 1 Valid", 1), RawTocEntry(1, title, 2)], 2
    )
    assert entries[1].kind is TocKind.UNKNOWN


@pytest.mark.parametrize("title", ["Appendix", "Appendix 1", "Glossary"])
def test_appendix_variants(title: str) -> None:
    entry = normalize_toc_entries([RawTocEntry(1, title, 1)], 1)[0]
    assert entry.kind is TocKind.APPENDIX
    assert entry.entry_id == "appendix-001"


def test_duplicate_start_pages_share_next_distinct_boundary() -> None:
    entries = normalize_toc_entries(
        [
            RawTocEntry(1, "Preface", 5),
            RawTocEntry(1, "Prologue Start", 5),
            RawTocEntry(1, "Chapter 1 Start", 10),
        ],
        15,
    )
    assert [entry.end_page for entry in entries] == [9, 9, 15]


def test_duplicate_chapter_number_is_rejected() -> None:
    with pytest.raises(TocNormalizationError, match="duplicate chapter number"):
        normalize_toc_entries(
            [RawTocEntry(1, "Chapter 1 One", 1), RawTocEntry(1, "Chapter 1 Again", 2)], 2
        )


@pytest.mark.parametrize("page", [0, 3, "1", True])
def test_invalid_start_page_is_rejected(page: object) -> None:
    with pytest.raises(TocNormalizationError, match="page"):
        normalize_toc_entries([RawTocEntry(1, "Chapter 1 One", page)], 2)


def test_discovery_and_yaml_round_trip_include_hash(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    make_pdf(pdf, 6, [[1, "Front", 1], [1, "Prologue Start", 2], [1, "Chapter 1 One", 4]])

    result = discover_embedded_toc(pdf, source_pdf=Path("input/book.pdf"))
    serialized = serialize_toc(result.document)
    restored = TocDocument.model_validate(yaml.safe_load(serialized))

    assert restored == result.document
    assert result.document.source_pdf_sha256 == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert result.document.entry_count == 3
    assert "!!python" not in serialized


def test_missing_embedded_toc_is_clear(tmp_path: Path) -> None:
    pdf = tmp_path / "no-toc.pdf"
    make_pdf(pdf, 1)
    with pytest.raises(TocDiscoveryError, match="fallback discovery is not implemented"):
        discover_embedded_toc(pdf)


@pytest.mark.parametrize("mode", ["missing", "invalid"])
def test_cli_rejects_missing_or_invalid_pdf(tmp_path: Path, mode: str) -> None:
    pdf = tmp_path / "bad.pdf"
    if mode == "invalid":
        pdf.write_text("not pdf", encoding="utf-8")
    result = runner.invoke(app, ["discover-toc", str(pdf), "--dry-run"])
    assert result.exit_code == 1
    assert "Error:" in result.stderr


def test_cli_overwrite_force_and_dry_run(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    output = tmp_path / "toc.yaml"
    make_pdf(pdf, 2, [[1, "Chapter 1 A Secret Title", 1]])
    output.write_text("preserve", encoding="utf-8")

    refused = runner.invoke(app, ["discover-toc", str(pdf), "--output", str(output)])
    assert refused.exit_code == 1
    assert output.read_text(encoding="utf-8") == "preserve"
    dry = runner.invoke(
        app, ["discover-toc", str(pdf), "--output", str(output), "--dry-run", "--force"]
    )
    assert dry.exit_code == 0
    assert "not written (dry run)" in dry.stdout
    assert "A Secret Title" not in dry.stdout + dry.stderr
    assert output.read_text(encoding="utf-8") == "preserve"
    forced = runner.invoke(app, ["discover-toc", str(pdf), "--output", str(output), "--force"])
    assert forced.exit_code == 0
    assert yaml.safe_load(output.read_text(encoding="utf-8"))["entry_count"] == 1


def test_writer_uses_atomic_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "book.pdf"
    output = tmp_path / "nested/toc.yaml"
    make_pdf(pdf, 1, [[1, "Chapter 1 One", 1]])
    document = discover_embedded_toc(pdf).document
    calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def record(source: str | Path, target: str | Path) -> None:
        calls.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", record)
    write_toc(document, output)
    assert calls[0][0].parent == output.parent
    assert calls[0][1] == output
