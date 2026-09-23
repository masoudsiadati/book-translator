"""The numbered paragraph contract: pure rendering, parsing, and verification of markers.

Blank lines are a formatting convention, not an identity.  Two source paragraphs fused into one
target block and one source paragraph broken into two produce a document with the *same* number of
blocks as the source, so a count comparison reports nothing while every paragraph after the first
defect is the translation of a different source paragraph than the reader is looking at.  That is
not a hypothetical: it is the confirmed defect class this module exists to remove.

The remedy is to stop inferring identity from geometry.  Each source paragraph is given an explicit
marker before it is sent, the provider is required to return the same markers, and the returned
markers are checked as a permutation-free, gap-free, strictly increasing sequence.  A document that
fails that check is refused whole; this module never tries to work out which paragraph a provider
meant, because a wrong answer there silently moves prose between identities, which is worse than
the defect being detected.

Marker syntax is deliberately hostile to prose: a double-bracketed ASCII token on a line of its
own.  It cannot occur in Persian narrative text by accident, it survives a round trip through a
provider that reflows whitespace, and it is trivially removable, which is what lets the accepted
translation stay pure prose.
"""

import re

from book_translator.paragraph_markers.models import MarkerFinding, MarkerParseResult

#: The contract's own version.  A change to marker syntax, to the conformance rules, or to the
#: stripped-text reconstruction is a change to what a provider was asked for and what its answer
#: was taken to mean, so it is versioned separately from every engine that consumes it.
PARAGRAPH_MARKER_CONTRACT_VERSION = "1.0.0"

#: Widest paragraph index the fixed-width marker can express.  A chunk holds tens of paragraphs,
#: so this bound is never approached in practice; it exists so the format stays fixed-width and a
#: marker is therefore a constant-length token that cannot be confused with a longer one.
MAX_PARAGRAPHS = 9999

_MARKER_LINE = re.compile(r"^\[\[P(\d{4})\]\]$")
#: Anything that merely *looks* like a marker.  Used to refuse near-misses loudly rather than
#: letting an unrecognized token be silently absorbed into a paragraph's prose.
_MARKER_LIKE = re.compile(r"\[\[\s*[Pp]?\s*\d*\s*\]\]|\[\[P\d+\]\]")

_BLOCK_SEPARATOR = re.compile(r"\n[^\S\n]*\n\s*")

__all__ = [
    "MAX_PARAGRAPHS",
    "PARAGRAPH_MARKER_CONTRACT_VERSION",
    "ParagraphMarkerError",
    "mark_paragraphs",
    "marker_for",
    "marker_token",
    "parse_marked_document",
    "parse_marked_window",
    "source_paragraphs",
    "strip_markers",
]


class ParagraphMarkerError(ValueError):
    """Raised when a document cannot be marked or a marked document cannot be reconstructed."""


def marker_for(index: int) -> str:
    """Return the stable marker for the 1-based source paragraph *index*."""
    if index < 1 or index > MAX_PARAGRAPHS:
        raise ParagraphMarkerError(f"paragraph index outside the marker contract: {index}")
    return f"[[P{index:04d}]]"


def marker_token(text: str) -> str | None:
    """Return the first marker-like token in *text*, or ``None`` when it carries none.

    A finding about a malformed marker line may quote the offending line, and that line can carry
    prose beside the token.  Anything that persists such a finding outside the private attempt
    must keep the token alone, which is all this returns.
    """
    match = _MARKER_LIKE.search(text)
    return match.group(0) if match is not None else None


def source_paragraphs(text: str) -> tuple[str, ...]:
    """Segment *text* into paragraph blocks using the project's canonical block rule."""
    stripped = text.strip()
    if not stripped:
        return ()
    return tuple(block for block in _BLOCK_SEPARATOR.split(stripped) if block.strip())


def mark_paragraphs(text: str) -> str:
    """Return *text* with one marker line placed above each of its paragraphs.

    The result is canonical block form — blocks separated by exactly one blank line, one trailing
    newline — so that :func:`strip_markers` inverts it exactly.  A caller that must prove byte
    conservation against a frozen artifact compares the stripped result to the frozen bytes rather
    than trusting this function, which is the check the prompt renderer performs.
    """
    blocks = source_paragraphs(text)
    if not blocks:
        raise ParagraphMarkerError("cannot mark a document with no paragraphs")
    if len(blocks) > MAX_PARAGRAPHS:
        raise ParagraphMarkerError("document exceeds the marker contract paragraph limit")
    if any(_MARKER_LIKE.search(block) for block in blocks):
        # A source that already contains marker-like syntax would make the provider's answer
        # ambiguous, and resolving that ambiguity is exactly what this contract refuses to do.
        raise ParagraphMarkerError("source text already contains paragraph marker syntax")
    return (
        "\n\n".join(f"{marker_for(index)}\n{block}" for index, block in enumerate(blocks, start=1))
        + "\n"
    )


def strip_markers(text: str) -> str:
    """Return the marker-free canonical block form of a well-formed marked document."""
    result = parse_marked_document(text, expected_count=None)
    if not result.valid:
        raise ParagraphMarkerError(result.findings[0].message)
    return result.text


def _finding(
    code: str,
    message: str,
    *,
    expected: str | None = None,
    observed: str | None = None,
    index: int | None = None,
) -> MarkerFinding:
    return MarkerFinding(
        code=code,
        severity="error",
        expected_marker=expected,
        observed_marker=observed,
        source_paragraph_index=index,
        message=message,
    )


def _scan(text: str) -> tuple[list[tuple[int, str]], list[MarkerFinding], list[str]]:
    """Split *text* into (marker number, block text) pairs, collecting syntax findings.

    The scan is line-based because the contract is line-based: a marker owns its own line, and a
    marker-like token anywhere else is a violation rather than a marker.  Preamble lines are
    returned separately so the caller can decide what a document without a leading marker means.
    """
    findings: list[MarkerFinding] = []
    preamble: list[str] = []
    entries: list[tuple[int, list[str]]] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        match = _MARKER_LINE.match(stripped)
        if match is not None:
            if line != stripped:
                findings.append(
                    _finding(
                        "marker_malformed",
                        "marker line carries surrounding whitespace",
                        observed=stripped,
                    )
                )
            entries.append((int(match.group(1)), []))
            continue
        if _MARKER_LIKE.search(line):
            findings.append(
                _finding(
                    "marker_malformed",
                    "line contains marker syntax that is not a well-formed marker line",
                    observed=stripped[:24],
                )
            )
            # The line is still recorded as content so that no prose is silently discarded from
            # a reconstruction the caller may never see; the document is already invalid.
        if entries:
            entries[-1][1].append(line)
        elif stripped:
            preamble.append(line)
    blocks = [(number, "\n".join(lines).strip("\n")) for number, lines in entries]
    return blocks, findings, preamble


def parse_marked_document(text: str, *, expected_count: int | None) -> MarkerParseResult:
    """Parse and verify one marked document against the numbered paragraph contract.

    ``expected_count`` is the number of source paragraphs the document must account for.  Passing
    ``None`` verifies only internal well-formedness — that the markers present are syntactically
    valid, unique, ordered, and start at one — which is what reconstructing a document the caller
    itself produced requires.
    """
    entries, findings, preamble = _scan(text)
    # Preamble text is only its own defect when markers exist; a document with none is reported
    # as unmarked, which says everything the second finding would have said.
    if preamble and entries:
        findings.append(
            _finding(
                "text_before_first_marker",
                "the document carries text before its first paragraph marker",
                expected=marker_for(1),
            )
        )
    if not entries:
        findings.append(
            _finding(
                "markers_absent",
                "the document carries no paragraph markers at all",
                expected=marker_for(1) if expected_count is None or expected_count else None,
            )
        )
        return MarkerParseResult(False, expected_count or 0, (), (), tuple(findings))

    seen: dict[int, int] = {}
    previous = 0
    for position, (number, block) in enumerate(entries, start=1):
        observed = f"[[P{number:04d}]]"
        if number in seen:
            findings.append(
                _finding(
                    "marker_duplicated",
                    "a paragraph marker appears more than once",
                    observed=observed,
                    index=number,
                )
            )
        else:
            seen[number] = position
        if expected_count is not None and not 1 <= number <= expected_count:
            findings.append(
                _finding(
                    "marker_unknown",
                    "the document carries a marker no source paragraph defines",
                    observed=observed,
                )
            )
        elif number <= previous:
            findings.append(
                _finding(
                    "marker_out_of_order",
                    "paragraph markers are not in source order",
                    expected=marker_for(previous + 1) if previous < MAX_PARAGRAPHS else None,
                    observed=observed,
                    index=number,
                )
            )
        previous = max(previous, number)
        if not block.strip():
            findings.append(
                _finding(
                    "empty_marker_block",
                    "a paragraph marker carries no translated text",
                    expected=observed,
                    observed=observed,
                    index=number,
                )
            )
    expected = expected_count if expected_count is not None else max(seen, default=0)
    for index in range(1, expected + 1):
        if index not in seen:
            findings.append(
                _finding(
                    "marker_missing",
                    "an expected paragraph marker is absent from the document",
                    expected=marker_for(index),
                    index=index,
                )
            )
    ordered = tuple(f"[[P{number:04d}]]" for number, _ in entries)
    valid = not findings
    blocks = tuple(block for _, block in entries) if valid else ()
    return MarkerParseResult(valid, expected, ordered, blocks, tuple(findings))


def parse_marked_window(text: str, *, expected_indexes: tuple[int, ...]) -> MarkerParseResult:
    """Parse a marked document that must carry exactly one explicit run of paragraph identities.

    A revision answers a bounded window such as paragraphs 15 through 20 rather than a whole
    chunk, so the identities it must return are an explicit ascending run that does not start at
    one. The conformance rules are otherwise the marker contract's: every expected identity
    exactly once, in order, nothing unknown, nothing before the first marker, no empty block.
    """
    if not expected_indexes:
        raise ParagraphMarkerError("a marked window must expect at least one paragraph")
    if list(expected_indexes) != sorted(set(expected_indexes)):
        raise ParagraphMarkerError("expected window indexes must be strictly ascending")
    entries, findings, preamble = _scan(text)
    if preamble and entries:
        findings.append(
            _finding(
                "text_before_first_marker",
                "the document carries text before its first paragraph marker",
                expected=marker_for(expected_indexes[0]),
            )
        )
    if not entries:
        findings.append(
            _finding(
                "markers_absent",
                "the document carries no paragraph markers at all",
                expected=marker_for(expected_indexes[0]),
            )
        )
        return MarkerParseResult(False, len(expected_indexes), (), (), tuple(findings))
    expected_set = set(expected_indexes)
    seen: set[int] = set()
    previous = 0
    for number, block in entries:
        observed = marker_for(number) if 1 <= number <= MAX_PARAGRAPHS else f"[[P{number}]]"
        if number not in expected_set:
            findings.append(
                _finding(
                    "marker_unknown",
                    "the document carries a marker outside the revision window",
                    observed=observed,
                )
            )
        elif number in seen:
            findings.append(
                _finding(
                    "marker_duplicated",
                    "a paragraph marker appears more than once",
                    observed=observed,
                    index=number,
                )
            )
        elif number <= previous:
            findings.append(
                _finding(
                    "marker_out_of_order",
                    "paragraph markers are not in source order",
                    observed=observed,
                    index=number,
                )
            )
        seen.add(number)
        previous = max(previous, number)
        if not block.strip():
            findings.append(
                _finding(
                    "empty_marker_block",
                    "a paragraph marker carries no translated text",
                    expected=observed,
                    observed=observed,
                    index=number,
                )
            )
    for index in expected_indexes:
        if index not in seen:
            findings.append(
                _finding(
                    "marker_missing",
                    "an expected paragraph marker is absent from the document",
                    expected=marker_for(index),
                    index=index,
                )
            )
    ordered = tuple(f"[[P{number:04d}]]" for number, _ in entries)
    valid = not findings
    blocks = tuple(block for _, block in entries) if valid else ()
    return MarkerParseResult(valid, len(expected_indexes), ordered, blocks, tuple(findings))
