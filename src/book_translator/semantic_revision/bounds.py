"""Splicing a revised window back into its chunk, and measuring exactly what changed.

A revision is allowed to change the paragraphs inside its window and nothing else. That rule is
enforced twice, on purpose. The splice is built so that paragraphs outside the window are copied
from the standing translation rather than from the provider's answer, which makes an outside
change impossible by construction; the bounds then re-derive the fact from the bytes and record
it, so the invariant is measured rather than assumed and a future change to the splice cannot
silently weaken it.
"""

import hashlib
from dataclasses import dataclass

from book_translator.models.semantic_gate import SemanticRevisionBounds
from book_translator.paragraph_markers import (
    MarkerParseResult,
    ParagraphMarkerError,
    parse_marked_window,
    source_paragraphs,
)

__all__ = ["SplicedRevision", "measure_bounds", "splice_window"]


class RevisionScopeError(ValueError):
    """Raised when a revision touched something outside the scope it was granted."""


@dataclass(frozen=True, slots=True)
class SplicedRevision:
    """A whole-chunk candidate built from the standing translation plus one revised window."""

    text: str
    blocks: tuple[str, ...]
    parse: MarkerParseResult


def _digest(blocks: tuple[str, ...]) -> str:
    return hashlib.sha256("\n\x00\n".join(blocks).encode("utf-8")).hexdigest()


def splice_window(
    standing_text: str, response_text: str, *, window_indexes: tuple[int, ...]
) -> SplicedRevision:
    """Replace exactly the window's paragraphs of *standing_text* with the parsed response."""
    parse = parse_marked_window(response_text, expected_indexes=window_indexes)
    if not parse.valid:
        raise ParagraphMarkerError(
            "revision response violated the marker window: "
            + ", ".join(sorted({finding.code for finding in parse.findings}))
        )
    standing = list(source_paragraphs(standing_text))
    if any(index < 1 or index > len(standing) for index in window_indexes):
        raise RevisionScopeError("revision window falls outside the standing translation")
    for index, block in zip(window_indexes, parse.blocks, strict=True):
        standing[index - 1] = block
    blocks = tuple(standing)
    return SplicedRevision("\n\n".join(blocks) + "\n", blocks, parse)


def measure_bounds(
    before_text: str,
    after_text: str,
    *,
    window_indexes: tuple[int, ...],
    terminology_blocking_before: tuple[str, ...],
    terminology_blocking_after: tuple[str, ...],
    lexical_content_preserved: bool | None = None,
) -> SemanticRevisionBounds:
    """Measure a candidate against its scope and refuse anything outside it."""
    before = source_paragraphs(before_text)
    after = source_paragraphs(after_text)
    window = set(window_indexes)
    marker_identity_preserved = len(before) == len(after)
    outside_before = tuple(block for index, block in enumerate(before, 1) if index not in window)
    outside_after = tuple(block for index, block in enumerate(after, 1) if index not in window)
    changed = [
        index for index, (old, new) in enumerate(zip(before, after, strict=False), 1) if old != new
    ]
    bounds = SemanticRevisionBounds(
        paragraphs_in_window=len(window_indexes),
        changed_paragraph_indexes=changed,
        unchanged_outside_window=outside_before == outside_after,
        marker_identity_preserved=marker_identity_preserved,
        character_delta=len(after_text) - len(before_text),
        word_delta=len(after_text.split()) - len(before_text.split()),
        outside_window_sha256_before=_digest(outside_before),
        outside_window_sha256_after=_digest(outside_after),
        terminology_blocking_before=sorted(terminology_blocking_before),
        terminology_blocking_after=sorted(terminology_blocking_after),
        lexical_content_preserved=lexical_content_preserved,
    )
    if not bounds.unchanged_outside_window or any(index not in window for index in changed):
        raise RevisionScopeError("revision changed a paragraph outside its window")
    if not marker_identity_preserved:
        raise RevisionScopeError("revision changed the number of paragraph identities")
    return bounds
