"""Fixed-order deterministic normalization engine.

Ordering is marker-aware parsing, physical-boundary hyphen repair, wrapped-line
joining, paragraph rendering, then validation. Hyphens are therefore inspected
before physical lines are flattened.
"""

from book_translator.normalization.hyphenation import PASS_NAME as HYPHEN_PASS
from book_translator.normalization.hyphenation import repair_hyphenation
from book_translator.normalization.line_joining import PASS_NAME as JOIN_PASS
from book_translator.normalization.line_joining import join_wrapped_lines
from book_translator.normalization.markers import PASS_NAME as MARKER_PASS
from book_translator.normalization.markers import parse_source
from book_translator.normalization.models import NormalizationResult
from book_translator.normalization.page_continuations import PASS_NAME as PAGE_CONTINUATION_PASS
from book_translator.normalization.page_continuations import join_page_continuations
from book_translator.normalization.paragraphs import PASS_NAME as PARAGRAPH_PASS
from book_translator.normalization.paragraphs import render_paragraphs
from book_translator.normalization.validation import PASS_NAME as VALIDATION_PASS
from book_translator.normalization.validation import validate_output

NORMALIZATION_VERSION = "1.6.0"
PASS_ORDER = (
    MARKER_PASS,
    HYPHEN_PASS,
    JOIN_PASS,
    PAGE_CONTINUATION_PASS,
    PARAGRAPH_PASS,
    VALIDATION_PASS,
)


def normalize_text(source: str) -> NormalizationResult:
    document, marker_changes = parse_source(source)
    hyphen_result = repair_hyphenation(document)
    joining_result = join_wrapped_lines(hyphen_result.document)
    continuation_result = join_page_continuations(joining_result.document)
    cleaned = render_paragraphs(continuation_result.document)
    validate_output(source, continuation_result.document, cleaned)
    return NormalizationResult(
        cleaned,
        continuation_result.document,
        marker_changes
        + hyphen_result.changes
        + joining_result.changes
        + continuation_result.changes,
        joining_result.warnings,
        PASS_ORDER,
    )
