"""Pure reconstruction of line-per-block PDF layouts (no I/O, no PyMuPDF).

Some text PDFs (OCR conversions) emit every printed line as its own text block, separate
paragraphs with whitespace-only blocks, repeat print-edition page furniture (running heads and
print page numbers) in the middle of a PDF page, append a ``Page N`` footer to every PDF page, and
begin chapters mid-page. This module turns the sorted text blocks of such pages into paragraph
blocks and discovers narrative headings from the text itself, because such a PDF has no embedded
table of contents.

Everything here is a deterministic function of the block texts: dropped furniture is counted, never
guessed at silently, and a paragraph is only rejoined across a physical seam (a PDF page boundary or
removed furniture) when the seam is structurally a continuation. Ambiguous seams stay separate
paragraphs, which loses no text.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

LINE_LAYOUT_VERSION = "1.0.0"
LINE_LAYOUT_EXTRACTION_VERSION = "line-layout/1.0.0"

_FOOTER = re.compile(r"^Page \d+$")
_PRINT_PAGE_NUMBER = re.compile(r"^[^\w]*\d{1,4}[^\w]*$")
_HEADING = re.compile(r"^(?P<word>PROLOGUE|EPILOGUE|GLOSSARY|CHAPTER)(?:\s+(?P<number>\d{1,3}))?$")
_HEADING_NUMBER = re.compile(r"^\d{1,3}$")
_TERMINAL_ENDINGS = (".", "!", "?", '"', "”", "’", "'", "…", ":", ";", ")")
_QUOTE_OPENERS = ('"', "“", "‘", "'")


class LineLayoutError(ValueError):
    """Raised when a line layout cannot be reconstructed or discovered safely."""


@dataclass(frozen=True, slots=True)
class LayoutGroup:
    """Consecutive non-blank line blocks of one PDF page, delimited by blank blocks."""

    page: int
    first_block: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LayoutHeading:
    page: int
    block: int
    word: str
    number: int | None
    number_inferred: bool
    title: str
    source_text: str
    group_count: int


@dataclass(frozen=True, slots=True)
class Paragraph:
    page: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FurnitureCounts:
    footers: int = 0
    print_page_numbers: int = 0
    running_heads: int = 0

    def plus(self, other: "FurnitureCounts") -> "FurnitureCounts":
        return FurnitureCounts(
            self.footers + other.footers,
            self.print_page_numbers + other.print_page_numbers,
            self.running_heads + other.running_heads,
        )


@dataclass(frozen=True, slots=True)
class Reconstruction:
    paragraphs: tuple[Paragraph, ...]
    furniture: FurnitureCounts
    furniture_words: int
    seam_joins: int
    seams_kept_separate: int


def letter_key(text: str) -> str:
    """Letters only, case-folded: the identity of a running head across OCR spacing and digits."""
    return "".join(character for character in text.casefold() if character.isalpha())


def _within_one_edit(left: str, right: str) -> bool:
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right, strict=True)) == 1
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    return any(longer[:index] + longer[index + 1 :] == shorter for index in range(len(longer)))


def is_running_head(line: str, running_head_keys: Sequence[str]) -> bool:
    key = letter_key(line)
    if not key:
        return False
    for candidate in running_head_keys:
        if key == candidate or (len(candidate) >= 8 and _within_one_edit(key, candidate)):
            return True
    return False


def page_groups(page: int, blocks: Sequence[str]) -> tuple[list[LayoutGroup], int, int]:
    """Group one page's sorted text blocks; return groups, footer count and footer words."""
    groups: list[LayoutGroup] = []
    current: list[str] = []
    first_block = 0
    footers = 0
    footer_words = 0
    for index, block in enumerate(blocks):
        stripped = block.strip()
        if _FOOTER.fullmatch(stripped):
            footers += 1
            footer_words += len(stripped.split())
            if current:
                groups.append(LayoutGroup(page, first_block, tuple(current)))
                current = []
            continue
        if not stripped:
            if current:
                groups.append(LayoutGroup(page, first_block, tuple(current)))
                current = []
            continue
        if not current:
            first_block = index
        current.extend(line.strip() for line in block.split("\n") if line.strip())
    if current:
        groups.append(LayoutGroup(page, first_block, tuple(current)))
    return groups, footers, footer_words


def _heading_match(group: LayoutGroup) -> re.Match[str] | None:
    if len(group.lines) != 1:
        return None
    return _HEADING.fullmatch(" ".join(group.lines[0].split()))


def discover_headings(pages: Sequence[tuple[int, Sequence[str]]]) -> list[LayoutHeading]:
    """Find PROLOGUE / CHAPTER [n] / EPILOGUE headings and the first GLOSSARY, in source order.

    A heading is a one-line group; its title is the next one-line group. A missing chapter number
    (an OCR loss) is inferred as the previous number plus one and reported; an explicit number that
    breaks the sequence fails closed. Scanning stops at the first GLOSSARY after a chapter, because
    later GLOSSARY lines are that appendix's own running heads.
    """
    ordered: list[LayoutGroup] = []
    for page, blocks in pages:
        ordered.extend(page_groups(page, blocks)[0])
    headings: list[LayoutHeading] = []
    last_number = 0
    seen_chapter = False
    for index, group in enumerate(ordered):
        match = _heading_match(group)
        if match is None:
            continue
        word = match.group("word")
        if word == "GLOSSARY" and not seen_chapter:
            continue
        title = ""
        explicit = match.group("number")
        title_index = index + 1
        if (
            word == "CHAPTER"
            and explicit is None
            and title_index < len(ordered)
            and len(ordered[title_index].lines) == 1
            and _HEADING_NUMBER.fullmatch(ordered[title_index].lines[0].strip())
        ):
            # The number was printed on its own line under the word.
            explicit = ordered[title_index].lines[0].strip()
            title_index += 1
        if word != "GLOSSARY":
            if title_index >= len(ordered) or len(ordered[title_index].lines) != 1:
                raise LineLayoutError(f"{word} heading on page {group.page} has no one-line title")
            title = " ".join(ordered[title_index].lines[0].split())
        number: int | None = None
        inferred = False
        if word == "CHAPTER":
            expected = last_number + 1
            if explicit is None:
                number, inferred = expected, True
            else:
                number = int(explicit)
                if number != expected:
                    raise LineLayoutError(
                        f"chapter heading on page {group.page} is {number}, expected {expected}"
                    )
            last_number = number
            seen_chapter = True
        elif word == "PROLOGUE":
            number = 0
        headings.append(
            LayoutHeading(
                group.page,
                group.first_block,
                word,
                number,
                inferred,
                title,
                " ".join(group.lines[0].split()),
                title_index - index + 1 if word != "GLOSSARY" else 1,
            )
        )
        if word == "GLOSSARY":
            break
    return headings


def _is_furniture(line: str, running_head_keys: Sequence[str]) -> str | None:
    collapsed = " ".join(line.split())
    if _PRINT_PAGE_NUMBER.fullmatch(collapsed):
        return "print_page_number"
    if is_running_head(collapsed, running_head_keys):
        return "running_head"
    return None


def _continues(previous: Paragraph, following: Paragraph) -> bool:
    last = previous.lines[-1].rstrip()
    first = following.lines[0].lstrip()
    if not last or not first:
        return False
    if first[0].islower():
        return True
    if last.endswith("-") and not last.endswith("--"):
        return first[0].isalpha()
    if last.endswith(("—", "–")):
        return False
    return not last.endswith(_TERMINAL_ENDINGS) and not first.startswith(_QUOTE_OPENERS)


def reconstruct(
    pages: Sequence[tuple[int, Sequence[str]]],
    *,
    start_block: int | None,
    end_block: int | None,
    running_head_keys: Sequence[str],
    heading_groups: int,
) -> Reconstruction:
    """Rebuild one entry's paragraphs from its pages.

    ``pages`` are the entry's PDF pages in order with their sorted text blocks. ``start_block``
    trims the first page, ``end_block`` (exclusive) trims the last. The first ``heading_groups``
    kept groups are the entry heading and title: they are never treated as furniture and never
    joined to prose.
    """
    if not pages:
        raise LineLayoutError("an entry needs at least one page")
    paragraphs: list[Paragraph] = []
    footers = numbers = heads = 0
    furniture_words = 0
    seam_joins = kept_separate = 0
    kept_groups = 0
    seam = False
    last_index = len(pages) - 1
    for position, (page, blocks) in enumerate(pages):
        groups, page_footers, footer_words = page_groups(page, blocks)
        footers += page_footers
        furniture_words += footer_words
        if position > 0:
            seam = True
        for group in groups:
            if position == 0 and start_block is not None and group.first_block < start_block:
                continue
            if position == last_index and end_block is not None and group.first_block >= end_block:
                continue
            if kept_groups >= heading_groups and len(group.lines) == 1:
                kind = _is_furniture(group.lines[0], running_head_keys)
                if kind is not None:
                    furniture_words += len(group.lines[0].split())
                    if kind == "print_page_number":
                        numbers += 1
                    else:
                        heads += 1
                    seam = True
                    continue
            paragraph = Paragraph(group.page, group.lines)
            kept_groups += 1
            if (
                seam
                and paragraphs
                and kept_groups > heading_groups + 1
                and _continues(paragraphs[-1], paragraph)
            ):
                previous = paragraphs.pop()
                paragraphs.append(Paragraph(previous.page, previous.lines + paragraph.lines))
                seam_joins += 1
            else:
                if seam and paragraphs and kept_groups > heading_groups + 1:
                    kept_separate += 1
                paragraphs.append(paragraph)
            seam = False
    return Reconstruction(
        tuple(paragraphs),
        FurnitureCounts(footers, numbers, heads),
        furniture_words,
        seam_joins,
        kept_separate,
    )


def render_source(paragraphs: Sequence[Paragraph]) -> str:
    """Render paragraphs as canonical ``source.md`` with strictly increasing page markers."""
    sections: list[str] = []
    last_page: int | None = None
    for paragraph in paragraphs:
        if last_page is None or paragraph.page > last_page:
            sections.append(f"<!-- source-page: {paragraph.page} -->")
            last_page = paragraph.page
        sections.append("\n".join(paragraph.lines))
    if not sections:
        raise LineLayoutError("entry reconstructed to no text")
    return "\n\n".join(sections).rstrip() + "\n"
