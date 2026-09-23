"""Pure parsing and normalization of embedded PDF table-of-contents entries."""

import re
from dataclasses import dataclass
from typing import cast

from pydantic import ValidationError

from book_translator.models import TocEntry, TocKind


class TocNormalizationError(ValueError):
    """Raised when embedded TOC data cannot form a valid normalized document."""


@dataclass(frozen=True, slots=True)
class RawTocEntry:
    level: object
    title: object
    page: object


_CHAPTER_RE = re.compile(r"^chapter\s+(?P<number>\d+)\s+(?P<remainder>\S.*)$", re.IGNORECASE)
_NUMBERED_TITLE_RE = re.compile(r"^(?P<number>\d+)\s+(?P<remainder>\S.*)$")
_PROLOGUE_RE = re.compile(r"^prologue(?:\s*:\s*|\s+)(?P<remainder>\S.*)$", re.IGNORECASE)
_EPILOGUE_RE = re.compile(r"^epilogue\s*:\s*(?P<remainder>\S.*)$", re.IGNORECASE)
_APPENDIX_RE = re.compile(r"^appendix(?:\s+[A-Za-z0-9]+)?$", re.IGNORECASE)
_GLOSSARY_RE = re.compile(r"^glossary$", re.IGNORECASE)


def _collapsed(value: str) -> str:
    return " ".join(value.split())


def _symbol_and_title(remainder: str) -> tuple[str | None, str] | None:
    if not remainder.startswith("("):
        return None, remainder
    closing = remainder.find(")")
    if closing < 2:
        return None
    symbol = remainder[1:closing]
    title = remainder[closing + 1 :].strip()
    if not title:
        return None
    return symbol, title


def _later_distinct_boundary(entries: list[RawTocEntry], index: int, page_count: int) -> int:
    start = entries[index].page
    assert isinstance(start, int)
    for later in entries[index + 1 :]:
        if isinstance(later.page, int) and later.page > start:
            return later.page - 1
    return page_count


def normalize_toc_entries(raw_entries: list[RawTocEntry], page_count: int) -> list[TocEntry]:
    """Normalize entries in source order; equal start pages share the next distinct boundary."""
    if not raw_entries:
        raise TocNormalizationError("embedded PDF TOC is empty")
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise TocNormalizationError("PDF page count must be a positive integer")

    for position, raw in enumerate(raw_entries, 1):
        if not isinstance(raw.level, int) or isinstance(raw.level, bool) or raw.level < 1:
            raise TocNormalizationError(f"TOC entry {position} has an invalid level")
        if not isinstance(raw.title, str) or not raw.title.strip():
            raise TocNormalizationError(f"TOC entry {position} has an empty title")
        if not isinstance(raw.page, int) or isinstance(raw.page, bool):
            raise TocNormalizationError(f"TOC entry {position} page must be an integer")
        if not 1 <= raw.page <= page_count:
            raise TocNormalizationError(
                f"TOC entry {position} start page {raw.page} is outside 1..{page_count}"
            )

    seen_narrative = False
    counters = {TocKind.FRONT_MATTER: 0, TocKind.APPENDIX: 0, TocKind.UNKNOWN: 0}
    seen_ids: set[str] = set()
    chapter_numbers: set[int] = set()
    entries: list[TocEntry] = []

    for index, raw in enumerate(raw_entries):
        source_title = str(raw.title).strip()
        parsed_title = _collapsed(source_title)
        chapter = _CHAPTER_RE.fullmatch(parsed_title) or _NUMBERED_TITLE_RE.fullmatch(parsed_title)
        prologue = _PROLOGUE_RE.fullmatch(parsed_title)
        epilogue = _EPILOGUE_RE.fullmatch(parsed_title)
        chapter_parts = _symbol_and_title(chapter.group("remainder")) if chapter else None
        prologue_parts = _symbol_and_title(prologue.group("remainder")) if prologue else None
        epilogue_parts = _symbol_and_title(epilogue.group("remainder")) if epilogue else None

        number: int | None = None
        symbol: str | None = None
        title_en = parsed_title
        if chapter and chapter_parts is not None:
            number = int(chapter.group("number"))
            if number <= 0:
                raise TocNormalizationError("chapter numbers must be positive")
            if number in chapter_numbers:
                raise TocNormalizationError(f"duplicate chapter number: {number}")
            chapter_numbers.add(number)
            kind = TocKind.CHAPTER
            entry_id = f"chapter-{number:03d}"
            symbol, title_en = chapter_parts
            seen_narrative = True
        elif prologue and prologue_parts is not None:
            kind = TocKind.PROLOGUE
            number = 0
            entry_id = "prologue"
            symbol, title_en = prologue_parts
            seen_narrative = True
        elif epilogue and epilogue_parts is not None:
            kind = TocKind.EPILOGUE
            entry_id = "epilogue"
            symbol, title_en = epilogue_parts
            seen_narrative = True
        elif _APPENDIX_RE.fullmatch(parsed_title) or _GLOSSARY_RE.fullmatch(parsed_title):
            kind = TocKind.APPENDIX
            counters[kind] += 1
            entry_id = f"appendix-{counters[kind]:03d}"
        elif not seen_narrative and not re.match(
            r"^(?:chapter|prologue|epilogue)\b", parsed_title, re.I
        ):
            kind = TocKind.FRONT_MATTER
            counters[kind] += 1
            entry_id = f"front-matter-{counters[kind]:03d}"
        else:
            kind = TocKind.UNKNOWN
            counters[kind] += 1
            entry_id = f"unknown-{counters[kind]:03d}"

        if entry_id in seen_ids:
            raise TocNormalizationError(f"duplicate normalized entry ID: {entry_id}")
        seen_ids.add(entry_id)
        end_page = _later_distinct_boundary(raw_entries, index, page_count)
        start_page = cast(int, raw.page)
        if end_page < start_page:
            end_page = start_page
        try:
            entries.append(
                TocEntry(
                    entry_id=entry_id,
                    level=cast(int, raw.level),
                    kind=kind,
                    number=number,
                    title_en=title_en,
                    title_fa="",
                    symbol=symbol,
                    start_page=start_page,
                    end_page=end_page,
                    source_toc_title=source_title,
                )
            )
        except ValidationError as exc:
            raise TocNormalizationError(f"invalid normalized TOC entry {index + 1}: {exc}") from exc

    if len(entries) != len(raw_entries):
        raise TocNormalizationError("normalization lost one or more embedded TOC entries")
    return entries
