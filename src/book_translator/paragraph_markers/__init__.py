"""The numbered paragraph output contract."""

from book_translator.paragraph_markers.engine import (
    MAX_PARAGRAPHS,
    PARAGRAPH_MARKER_CONTRACT_VERSION,
    ParagraphMarkerError,
    mark_paragraphs,
    marker_for,
    marker_token,
    parse_marked_document,
    parse_marked_window,
    source_paragraphs,
    strip_markers,
)
from book_translator.paragraph_markers.models import MarkerFinding, MarkerParseResult

__all__ = [
    "MAX_PARAGRAPHS",
    "PARAGRAPH_MARKER_CONTRACT_VERSION",
    "MarkerFinding",
    "MarkerParseResult",
    "ParagraphMarkerError",
    "mark_paragraphs",
    "marker_for",
    "marker_token",
    "parse_marked_document",
    "parse_marked_window",
    "source_paragraphs",
    "strip_markers",
]
