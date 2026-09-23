"""Pure deterministic diagnosis and repair of a translation's block structure.

``paragraph_count_mismatch`` is a single QA code covering several unrelated defects, and the
difference between them decides whether a provider attempt has to be spent at all. A translation
that broke one source line across two lines, or wrote a blank line where the source has a plain
line break, contains exactly the right prose in exactly the right order: what is wrong is where
the separators are, and no rewrite is the correct remedy for that. A translation that fused two
source paragraphs into one sentence flow, or dropped one, is a content difference, and only a
provider can answer it.

The distinction is drawn at the level of *lines* rather than blocks, which is what makes it
decidable. Both sides are segmented into non-blank lines; the source additionally records which
block each of its lines belongs to. When the two sides have the same number of lines there is
nothing to search for — the correspondence is the identity, every line already exists, and the
only difference between the two documents is the source's blank-line placement, which is frozen
and can simply be re-applied. When they differ, a monotone alignment is searched for, and it is
accepted only when it is bounded, cheap, and *uniquely* the best answer by a margin; anything
else is refused so a provider decides instead of a heuristic.

Nothing here performs I/O, and the repair itself is whitespace-only by construction: it re-emits
the translation's own lines, in their own order, with separators taken from the source. The
renderer asserts that invariant on its own output rather than trusting it.
"""

import math
import re
from dataclasses import dataclass

from book_translator.models import StructuralDefectClass, StructuralOperationKind
from book_translator.terminology.alignment import paragraphs

#: 1.1.0 does not change how a repair is derived; it narrows when one may be *adopted*.  A job
#: under the numbered paragraph contract is refused outright, and a rendered repair that would
#: introduce a blocking alignment finding is refused rather than published.  Derived bytes for
#: every case that still qualifies are unchanged.
STRUCTURAL_REPAIR_ENGINE_VERSION = "1.1.0"

#: How many lines one side of a single alignment group may span. A defect wider than this is not
#: a stray separator any more, and widening the bound would mostly widen what a length heuristic
#: is allowed to assert.
MAX_GROUP_LINES = 4

#: How many non-identity groups one deterministic repair may contain.
MAX_OPERATIONS = 3

#: How far the two line counts may diverge before the difference stops looking like separators.
MAX_LINE_DELTA = 4

#: How much better than every materially different alternative the chosen alignment must be.
MIN_ALIGNMENT_MARGIN = 0.50

#: The most any single group may cost before the alignment is treated as unproven.
MAX_GROUP_COST = 1.25

#: Weight of the paired-quotation-mark signal. Dialogue punctuation survives translation as a
#: count even when nothing else does, and it is what separates two length-wise plausible
#: alignments from each other.
_QUOTE_WEIGHT = 0.35

#: Cost of asserting that one side of a group spans more than one line at all.
_GROUP_PENALTY = 0.30

_SOURCE_QUOTES = re.compile(r"[\"“”]")
_TARGET_QUOTES = re.compile(r"[«»“”]")


@dataclass(frozen=True, slots=True)
class Line:
    """One non-blank line, together with the block it belongs to."""

    block_index: int
    line_index: int
    text: str

    @property
    def starts_block(self) -> bool:
        return self.line_index == 0


@dataclass(frozen=True, slots=True)
class AlignedGroup:
    """One monotone correspondence between a run of source lines and a run of target lines."""

    kind: StructuralOperationKind
    source_index: int
    source_lines: int
    translation_index: int
    translation_lines: int
    cost_milli: int

    @property
    def identity(self) -> bool:
        return self.source_lines == 1 and self.translation_lines == 1


@dataclass(frozen=True, slots=True)
class StructuralDiagnosis:
    """Why two block counts differ, and whether that difference is repairable without a provider.

    ``repaired_text`` is present exactly when the defect class is deterministic. It is produced
    during diagnosis on purpose: whether a deterministic repair exists is not a claim to be made
    and checked later, it is the fact that one was rendered and preserved every character.
    """

    source_paragraphs: int
    translation_paragraphs: int
    source_lines: int
    translation_lines: int
    defect_class: StructuralDefectClass
    groups: tuple[AlignedGroup, ...]
    blank_line_artifact: bool
    alignment_cost_milli: int | None
    alignment_margin_milli: int | None
    refusals: tuple[str, ...]
    repaired_text: str | None

    @property
    def paragraph_delta(self) -> int:
        return self.translation_paragraphs - self.source_paragraphs

    @property
    def line_delta(self) -> int:
        return self.translation_lines - self.source_lines

    @property
    def matched(self) -> bool:
        return self.paragraph_delta == 0

    @property
    def deterministic(self) -> bool:
        return self.repaired_text is not None

    @property
    def operations(self) -> tuple[AlignedGroup, ...]:
        return tuple(group for group in self.groups if not group.identity)


def lines(text: str) -> tuple[Line, ...]:
    """Segment *text* into non-blank lines, remembering which block each line came from."""
    found: list[Line] = []
    for block_index, block in enumerate(paragraphs(text)):
        line_index = 0
        for raw in block.text.split("\n"):
            stripped = raw.strip()
            if not stripped:
                continue
            found.append(Line(block_index=block_index, line_index=line_index, text=stripped))
            line_index += 1
    return tuple(found)


def _pair_cost(source: str, target: str, word_ratio: float, char_ratio: float) -> float:
    """Score one candidate correspondence from length and dialogue punctuation alone.

    Both length terms are log-ratios against the *chunk's own* observed expansion, so the score
    says "this pair is proportioned like the rest of this translation" rather than embedding a
    constant about any particular language pair.
    """
    cost = abs(
        math.log((len(target.split()) + 1) / (word_ratio * len(source.split()) + 1))
    ) + 0.5 * abs(math.log((len(target) + 1) / (char_ratio * len(source) + 1)))
    quoted = abs(
        len(_SOURCE_QUOTES.findall(source)) // 2 - len(_TARGET_QUOTES.findall(target)) // 2
    )
    return cost + _QUOTE_WEIGHT * quoted


def _group_cost(
    source: tuple[Line, ...],
    target: tuple[Line, ...],
    word_ratio: float,
    char_ratio: float,
) -> float:
    joined_source = " ".join(item.text for item in source)
    joined_target = " ".join(item.text for item in target)
    span = len(source) + len(target) - 2
    return _pair_cost(joined_source, joined_target, word_ratio, char_ratio) + _GROUP_PENALTY * span


def _kind(source_lines: int, translation_lines: int) -> StructuralOperationKind:
    if source_lines == 1 and translation_lines == 1:
        return StructuralOperationKind.ALIGNED
    if source_lines == 1:
        return StructuralOperationKind.JOIN_TRANSLATION_LINES
    return StructuralOperationKind.SPLIT_TRANSLATION_LINE


def _search(
    source: tuple[Line, ...],
    target: tuple[Line, ...],
    *,
    forbidden: frozenset[tuple[int, int, int, int]] = frozenset(),
) -> tuple[float, tuple[AlignedGroup, ...]] | None:
    """Return the cheapest monotone alignment, or ``None`` when none exists under *forbidden*.

    ``forbidden`` names groups the search may not use, which is how the second-best materially
    different alignment is obtained: re-running the search once per chosen group, each time
    without that group, is exactly "what is the best answer that disagrees somewhere".
    """
    rows, columns = len(source), len(target)
    source_words = sum(len(item.text.split()) for item in source) or 1
    target_words = sum(len(item.text.split()) for item in target) or 1
    source_chars = sum(len(item.text) for item in source) or 1
    target_chars = sum(len(item.text) for item in target) or 1
    word_ratio = target_words / source_words
    char_ratio = target_chars / source_chars
    best = [[math.inf] * (columns + 1) for _ in range(rows + 1)]
    back: list[list[tuple[int, int, int, int] | None]] = [
        [None] * (columns + 1) for _ in range(rows + 1)
    ]
    best[0][0] = 0.0
    for row in range(rows + 1):
        for column in range(columns + 1):
            if best[row][column] == math.inf:
                continue
            for taken in range(1, MAX_GROUP_LINES + 1):
                for given in range(1, MAX_GROUP_LINES + 1):
                    # One side of a group is always a single line: a group claiming that several
                    # source lines became several target lines asserts nothing checkable.
                    if taken > 1 and given > 1:
                        continue
                    if row + taken > rows or column + given > columns:
                        continue
                    if (row, taken, column, given) in forbidden:
                        continue
                    candidate = best[row][column] + _group_cost(
                        source[row : row + taken],
                        target[column : column + given],
                        word_ratio,
                        char_ratio,
                    )
                    if candidate < best[row + taken][column + given]:
                        best[row + taken][column + given] = candidate
                        back[row + taken][column + given] = (row, column, taken, given)
    if best[rows][columns] == math.inf:
        return None
    groups: list[AlignedGroup] = []
    row, column = rows, columns
    while (row, column) != (0, 0):
        step = back[row][column]
        assert step is not None
        previous_row, previous_column, taken, given = step
        groups.append(
            AlignedGroup(
                kind=_kind(taken, given),
                source_index=previous_row,
                source_lines=taken,
                translation_index=previous_column,
                translation_lines=given,
                cost_milli=round(
                    1000
                    * _group_cost(
                        source[previous_row : previous_row + taken],
                        target[previous_column : previous_column + given],
                        word_ratio,
                        char_ratio,
                    )
                ),
            )
        )
        row, column = previous_row, previous_column
    return best[rows][columns], tuple(reversed(groups))


def _identity_groups(count: int) -> tuple[AlignedGroup, ...]:
    return tuple(
        AlignedGroup(
            kind=StructuralOperationKind.ALIGNED,
            source_index=index,
            source_lines=1,
            translation_index=index,
            translation_lines=1,
            cost_milli=0,
        )
        for index in range(count)
    )


def render_repair(
    source: tuple[Line, ...], target: tuple[Line, ...], groups: tuple[AlignedGroup, ...]
) -> str:
    """Re-emit the translation's own lines under the source's blank-line placement.

    Every group must contribute exactly one source line, because a source line is what a target
    line corresponds to; a group that merged several source lines has no single place to put the
    separator and never reaches here.
    """
    pieces: list[str] = []
    for group in groups:
        if group.source_lines != 1:
            raise ValueError("a deterministic repair cannot split a merged translation line")
        span = target[group.translation_index : group.translation_index + group.translation_lines]
        merged = " ".join(item.text for item in span)
        pieces.append(("\n\n" if source[group.source_index].starts_block else "\n") + merged)
    repaired = "".join(pieces).lstrip("\n") + "\n"
    expected = "".join("".join(item.text.split()) for item in target)
    if "".join(repaired.split()) != expected:
        raise ValueError("structural repair altered translated content")
    return repaired


def diagnose_structure(source_text: str, translation_text: str) -> StructuralDiagnosis:
    """Classify why two block counts differ and render the repair when one is deterministic."""
    source_blocks = paragraphs(source_text)
    target_blocks = paragraphs(translation_text)
    source = lines(source_text)
    target = lines(translation_text)
    naive_source = len(source_text.strip().split("\n\n")) if source_text.strip() else 0
    naive_target = len(translation_text.strip().split("\n\n")) if translation_text.strip() else 0
    artifact = (naive_source, naive_target) != (len(source_blocks), len(target_blocks))

    def answer(
        defect_class: StructuralDefectClass,
        *,
        groups: tuple[AlignedGroup, ...] = (),
        cost_milli: int | None = None,
        margin_milli: int | None = None,
        refusals: tuple[str, ...] = (),
        repaired_text: str | None = None,
    ) -> StructuralDiagnosis:
        return StructuralDiagnosis(
            source_paragraphs=len(source_blocks),
            translation_paragraphs=len(target_blocks),
            source_lines=len(source),
            translation_lines=len(target),
            defect_class=defect_class,
            groups=groups,
            blank_line_artifact=artifact,
            alignment_cost_milli=cost_milli,
            alignment_margin_milli=margin_milli,
            refusals=refusals,
            repaired_text=repaired_text,
        )

    if not source or not target:
        return answer(StructuralDefectClass.CONTENT_DIVERGENCE, refusals=("empty_text",))
    if len(source_blocks) == len(target_blocks):
        return answer(StructuralDefectClass.NONE)
    if len(source) == len(target):
        # Nothing is missing, nothing is extra, and no search can improve on the identity: the
        # only difference between the two documents is where the blank lines are.
        groups = _identity_groups(len(source))
        return answer(
            StructuralDefectClass.BLANK_LINE_GEOMETRY,
            groups=groups,
            cost_milli=0,
            repaired_text=render_repair(source, target, groups),
        )
    if abs(len(target) - len(source)) > MAX_LINE_DELTA:
        return answer(
            StructuralDefectClass.CONTENT_DIVERGENCE,
            refusals=(f"line_delta_exceeds_{MAX_LINE_DELTA}",),
        )
    found = _search(source, target)
    if found is None:
        return answer(StructuralDefectClass.CONTENT_DIVERGENCE, refusals=("no_monotone_alignment",))
    cost, groups = found
    operations = tuple(group for group in groups if not group.identity)
    alternatives = [
        _search(
            source,
            target,
            forbidden=frozenset(
                {
                    (
                        group.source_index,
                        group.source_lines,
                        group.translation_index,
                        group.translation_lines,
                    )
                }
            ),
        )
        for group in operations
    ]
    margins = [alternative[0] - cost for alternative in alternatives if alternative is not None]
    margin = min(margins) if margins else math.inf
    cost_milli = round(1000 * cost)
    margin_milli = None if margin == math.inf else round(1000 * margin)
    refusals: list[str] = []
    if len(operations) > MAX_OPERATIONS:
        refusals.append(f"operations_exceed_{MAX_OPERATIONS}")
    if margin < MIN_ALIGNMENT_MARGIN:
        refusals.append("alignment_is_not_uniquely_best")
    if any(group.cost_milli > round(1000 * MAX_GROUP_COST) for group in operations):
        refusals.append("group_correspondence_is_unproven")
    merged = tuple(
        group
        for group in operations
        if group.kind is StructuralOperationKind.SPLIT_TRANSLATION_LINE
    )
    if merged:
        # The translation wrote several source lines as one line. Where the break belongs inside
        # translated prose is a judgement about meaning, and this engine never makes one.
        refusals.append("translation_merged_source_lines")
        return answer(
            StructuralDefectClass.TRANSLATION_LINE_MERGE,
            groups=groups,
            cost_milli=cost_milli,
            margin_milli=margin_milli,
            refusals=tuple(refusals),
        )
    if refusals:
        return answer(
            StructuralDefectClass.LINE_COUNT_DIVERGENCE,
            groups=groups,
            cost_milli=cost_milli,
            margin_milli=margin_milli,
            refusals=tuple(refusals),
        )
    return answer(
        StructuralDefectClass.TRANSLATION_LINE_SPLIT,
        groups=groups,
        cost_milli=cost_milli,
        margin_milli=margin_milli,
        repaired_text=render_repair(source, target, groups),
    )


__all__ = [
    "MAX_GROUP_LINES",
    "MAX_LINE_DELTA",
    "MAX_OPERATIONS",
    "MIN_ALIGNMENT_MARGIN",
    "STRUCTURAL_REPAIR_ENGINE_VERSION",
    "AlignedGroup",
    "Line",
    "StructuralDiagnosis",
    "diagnose_structure",
    "lines",
    "render_repair",
]
