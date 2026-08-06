"""Canonical source-page marker parsing with block and line provenance."""

import re

from book_translator.normalization.models import (
    ChangeRecord,
    ParsedDocument,
    PhysicalLine,
    TextBlock,
)

PASS_NAME = "page-markers"
PASS_VERSION = "1.0"
_MARKER = re.compile(r"^<!-- source-page: ([1-9]\d*) -->$")
_CANONICAL_LOOKING = re.compile(r"<!--\s*source-page\s*:", re.IGNORECASE)


class MarkerError(ValueError):
    """Raised when source page marker syntax is malformed or unsafe."""


def parse_source(source: str) -> tuple[ParsedDocument, tuple[ChangeRecord, ...]]:
    """Parse source pages, blocks and physical lines without changing source text."""
    if "\r" in source:
        raise MarkerError("source contains CR characters")
    page: int | None = None
    transitions: list[int] = []
    changes: list[ChangeRecord] = []
    block_lines: list[tuple[str, int]] = []
    blocks: list[TextBlock] = []
    physical_count = 0

    def flush() -> None:
        nonlocal block_lines
        if not block_lines:
            return
        if page is None:
            raise MarkerError("text appears before the first canonical source-page marker")
        block_index = len(blocks)
        lines = tuple(
            PhysicalLine(text, page, block_index, source_line) for text, source_line in block_lines
        )
        blocks.append(TextBlock(page, block_index, lines, "\n".join(x[0] for x in block_lines)))
        block_lines = []

    for source_line, line in enumerate(source.splitlines(), start=1):
        marker = _MARKER.fullmatch(line)
        if marker:
            flush()
            page = int(marker.group(1))
            if transitions and page <= transitions[-1]:
                raise MarkerError("source-page markers must be strictly increasing")
            transitions.append(page)
            changes.append(
                ChangeRecord(
                    PASS_NAME,
                    "page_marker_removed",
                    page,
                    line_index=source_line,
                    description="removed canonical page marker",
                )
            )
        elif _CANONICAL_LOOKING.search(line):
            raise MarkerError(
                f"malformed canonical source-page marker at physical line {source_line}"
            )
        elif not line.strip():
            flush()
        else:
            if page is None:
                raise MarkerError("text appears before the first canonical source-page marker")
            physical_count += 1
            block_lines.append((line, source_line))
    flush()
    if not transitions:
        raise MarkerError("source contains no canonical source-page markers")
    return ParsedDocument(tuple(blocks), tuple(transitions), physical_count), tuple(changes)
