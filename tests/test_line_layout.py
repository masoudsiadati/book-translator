"""Line-layout reconstruction and text-heading discovery, on synthetic page blocks only."""

import pytest

from book_translator.line_layout import (
    LineLayoutError,
    discover_headings,
    is_running_head,
    letter_key,
    page_groups,
    reconstruct,
    render_source,
)
from book_translator.models import TocEntry, TocKind
from book_translator.normalization import normalize_text

BLANK = "  \n"
KEYS = sorted({letter_key("A Tale of Lamps"), letter_key("First Light"), letter_key("Second Road")})


def _page(*lines: str, footer: int) -> list[str]:
    blocks: list[str] = []
    for line in lines:
        blocks.append(BLANK if line == "" else f" {line}\n")
    blocks.append(f"Page {footer}\n")
    return blocks


def test_page_groups_split_on_blank_blocks_and_drop_the_footer() -> None:
    groups, footers, footer_words = page_groups(
        3, _page("alpha one", "alpha two", "", "beta", footer=3)
    )
    assert [group.lines for group in groups] == [("alpha one", "alpha two"), ("beta",)]
    assert [group.first_block for group in groups] == [0, 3]
    assert (footers, footer_words) == (1, 2)


def test_headings_infer_a_lost_number_and_read_a_number_on_its_own_line() -> None:
    pages = [
        (1, _page("front words", "", "PROLOGUE", "", "First Light", "", "text", footer=1)),
        (2, _page("CHAPTER", "", "Second Road", "", "text", footer=2)),
        (3, _page("more", "", "CHAPTER", "", "2", "", "Third Gate", "", "text", footer=3)),
        (4, _page("end", "", "GLOSSARY", "", "entries", footer=4)),
    ]
    headings = discover_headings(pages)
    assert [(h.word, h.number, h.number_inferred, h.title, h.group_count) for h in headings] == [
        ("PROLOGUE", 0, False, "First Light", 2),
        ("CHAPTER", 1, True, "Second Road", 2),
        ("CHAPTER", 2, False, "Third Gate", 3),
        ("GLOSSARY", None, False, "", 1),
    ]
    assert (headings[0].page, headings[0].block) == (1, 2)


def test_an_out_of_sequence_explicit_chapter_number_fails_closed() -> None:
    pages = [(1, _page("CHAPTER 2", "", "Wrong", "", "text", footer=1))]
    with pytest.raises(LineLayoutError, match="expected 1"):
        discover_headings(pages)


def test_running_heads_tolerate_one_ocr_edit_but_not_short_keys() -> None:
    assert is_running_head("12   A  TALE  OF  LAMPS", KEYS)
    assert is_running_head("A Tale of Lamqs", KEYS)
    assert not is_running_head("A Tale of Lanterns", KEYS)
    assert not is_running_head("", KEYS)


def test_reconstruct_removes_furniture_and_joins_only_clear_seams() -> None:
    pages = [
        (
            5,
            _page(
                "noise before the heading",
                "",
                "CHAPTER 1",
                "",
                "Second Road",
                "",
                "The road ran on and the lamps were",
                "",
                "12",
                "",
                "A TALE OF LAMPS",
                "",
                "lit by the keeper.",
                "",
                "She stopped.",
                footer=5,
            ),
        ),
        (6, _page("Nobody followed. The long hyphen-", footer=6)),
        (7, _page("ated word ended.", "", "13", "", "Second Road", "", "CHAPTER 2", footer=7)),
    ]
    rebuilt = reconstruct(
        pages, start_block=2, end_block=6, running_head_keys=KEYS, heading_groups=2
    )
    assert [paragraph.lines for paragraph in rebuilt.paragraphs] == [
        ("CHAPTER 1",),
        ("Second Road",),
        ("The road ran on and the lamps were", "lit by the keeper."),
        ("She stopped.",),
        ("Nobody followed. The long hyphen-", "ated word ended."),
    ]
    assert rebuilt.furniture.footers == 3
    assert rebuilt.furniture.print_page_numbers == 2
    assert rebuilt.furniture.running_heads == 2
    assert rebuilt.seam_joins == 2
    assert rebuilt.seams_kept_separate == 1


def test_rendered_source_normalizes_with_strictly_increasing_markers() -> None:
    pages = [
        (1, _page("PROLOGUE", "", "First Light", "", "Words run", footer=1)),
        (2, _page("on and on.", "", "Next one.", footer=2)),
    ]
    rebuilt = reconstruct(
        pages, start_block=None, end_block=None, running_head_keys=KEYS, heading_groups=2
    )
    source = render_source(rebuilt.paragraphs)
    assert source.count("<!-- source-page: 1 -->") == 1
    assert source.count("<!-- source-page: 2 -->") == 1
    cleaned = normalize_text(source).cleaned_text
    assert cleaned.split("\n\n")[2] == "Words run on and on."


def test_toc_entry_block_anchors_must_be_ordered_on_one_page() -> None:
    fields: dict[str, object] = {
        "entry_id": "chapter-001",
        "level": 1,
        "kind": TocKind.CHAPTER,
        "number": 1,
        "start_page": 4,
        "end_page": 4,
        "source_toc_title": "CHAPTER 1 Title",
    }
    assert TocEntry.model_validate({**fields, "start_block": 2, "end_block": 9}).end_block == 9
    with pytest.raises(ValueError, match="end_block"):
        TocEntry.model_validate({**fields, "start_block": 5, "end_block": 5})
