"""Deterministic classification and repair of paragraph-boundary displacement.

The Phase 2 pilot found a signature. A contiguous run of paragraphs each reported as *both*
``meaning_omitted`` and ``meaning_added`` is not a run of independent losses: content absent from
a paragraph while unsupported content is present in the same paragraph means the paragraph holds
its neighbour's text. The words survived translation; only the separators between them landed in
the wrong places. That is displacement, and a rewrite is the wrong remedy for it, because a
rewrite spends a provider on prose that is already correct.

Two things live here and they are kept apart. The classifier reads judge findings and names the
pattern; it touches no text. The re-segmentation reads the Persian window and tries to put the
separators back where the source has them; it is whitespace-only by construction and proves it.
The proof is the point. A re-segmentation is *deterministic* when the choice of split points is
unique by a margin, and it is *safe* when the concatenated non-whitespace Persian content is the
same before and after. When either fails, this module refuses and a provider decides instead.
Even a successful re-segmentation is a candidate, not a verdict: it is re-judged like any other
revision, because a unique proportional split is evidence of where the boundaries went and not
proof of what the paragraphs mean.
"""

import math
import re
from dataclasses import dataclass

from book_translator.models.semantic_qa import (
    SemanticFindingCode,
    SemanticParagraphOutcome,
    SemanticVerdict,
)
from book_translator.paragraph_alignment.policy import AlignmentPolicy

#: The classifier's version: the pattern it recognizes and the window it derives.
DISPLACEMENT_CLASSIFIER_VERSION = "1.0.0"

SEMANTIC_BOUNDARY_DISPLACEMENT = "semantic_boundary_displacement"

#: At least this many paragraphs in a run must carry the paired signature. One paragraph with
#: both an omission and an addition is a paragraph a provider rewrote; two adjacent ones are a
#: boundary that moved.
MIN_PAIRED_PARAGRAPHS = 2

#: The paired signature must account for at least this share of the run, so a long run of
#: unrelated defects with two paired paragraphs somewhere in it is not read as one displacement.
MIN_PAIRED_SHARE = 0.5

#: How much better the best sentence assignment must be than every alternative for the split to
#: count as determined rather than chosen. Measured in the same log-ratio cost the alignment
#: policy reasons in.
MIN_ASSIGNMENT_MARGIN = 0.25

_SENTENCE_END = re.compile(r"(?<=[.!?…؟»”\"])\s+")

__all__ = [
    "DISPLACEMENT_CLASSIFIER_VERSION",
    "MIN_ASSIGNMENT_MARGIN",
    "MIN_PAIRED_PARAGRAPHS",
    "MIN_PAIRED_SHARE",
    "SEMANTIC_BOUNDARY_DISPLACEMENT",
    "DisplacementWindow",
    "Resegmentation",
    "classify_displacement",
    "lexical_content",
    "resegment_window",
]


@dataclass(frozen=True, slots=True)
class DisplacementWindow:
    """One contiguous run of paragraphs whose findings carry the displacement signature."""

    first_index: int
    last_index: int
    paired_paragraphs: int
    run_length: int
    classification: str = SEMANTIC_BOUNDARY_DISPLACEMENT

    @property
    def indexes(self) -> tuple[int, ...]:
        return tuple(range(self.first_index, self.last_index + 1))


def _codes(outcome: SemanticParagraphOutcome) -> set[SemanticFindingCode]:
    return {finding.code for finding in (*outcome.material_findings, *outcome.advisory_findings)}


def _paired(outcome: SemanticParagraphOutcome) -> bool:
    codes = _codes(outcome)
    return (
        SemanticFindingCode.MEANING_OMITTED in codes and SemanticFindingCode.MEANING_ADDED in codes
    )


def classify_displacement(
    outcomes: tuple[SemanticParagraphOutcome, ...],
) -> tuple[DisplacementWindow, ...]:
    """Name every contiguous run of judged paragraphs that reads as boundary displacement.

    A run is a maximal sequence of consecutive paragraph indexes whose outcome is not ``PASS``.
    It is classified when at least ``MIN_PAIRED_PARAGRAPHS`` of its paragraphs carry both an
    omission and an addition and those paragraphs are at least ``MIN_PAIRED_SHARE`` of the run.
    The classifier reads findings only; it never reads or changes text.
    """
    ordered = sorted(outcomes, key=lambda item: item.paragraph_index)
    windows: list[DisplacementWindow] = []
    run: list[SemanticParagraphOutcome] = []

    def close() -> None:
        if not run:
            return
        paired = sum(1 for item in run if _paired(item))
        if paired >= MIN_PAIRED_PARAGRAPHS and paired / len(run) >= MIN_PAIRED_SHARE:
            windows.append(
                DisplacementWindow(
                    first_index=run[0].paragraph_index,
                    last_index=run[-1].paragraph_index,
                    paired_paragraphs=paired,
                    run_length=len(run),
                )
            )
        run.clear()

    for outcome in ordered:
        flagged = outcome.policy_outcome is not SemanticVerdict.PASS
        contiguous = bool(run) and outcome.paragraph_index == run[-1].paragraph_index + 1
        if not flagged or (run and not contiguous):
            close()
        if flagged:
            run.append(outcome)
    close()
    return tuple(windows)


def lexical_content(text: str) -> str:
    """Return *text* with every whitespace character removed: what a re-segmentation preserves."""
    return "".join(text.split())


def _sentences(text: str) -> tuple[str, ...]:
    """Split a Persian window into sentence units at sentence-final punctuation."""
    parts = [part.strip() for part in _SENTENCE_END.split(text.strip())]
    return tuple(part for part in parts if part)


def _cost(target_words: int, source_words: int, expansion: float) -> float:
    """Log-ratio distance of one block's proportion from the window's own expansion."""
    if source_words == 0 or target_words == 0:
        return math.inf
    return abs(math.log((target_words / source_words) / expansion))


@dataclass(frozen=True, slots=True)
class Resegmentation:
    """The answer to "can these separators be put back deterministically?"."""

    window_first_index: int
    blocks: tuple[str, ...]
    deterministic: bool
    lexical_content_preserved: bool
    cost: float | None
    margin: float | None
    refusals: tuple[str, ...]

    @property
    def applicable(self) -> bool:
        return self.deterministic and self.lexical_content_preserved and not self.refusals


def resegment_window(
    source_blocks: tuple[str, ...],
    target_blocks: tuple[str, ...],
    *,
    first_index: int,
    policy: AlignmentPolicy,
) -> Resegmentation:
    """Re-split the Persian content of a displaced window to mirror the source's block sizes.

    The window's Persian text is joined, cut into sentence units, and every monotone assignment
    of consecutive units to the window's paragraphs is scored by how far each resulting block's
    word proportion sits from the window's own overall expansion. The assignment is accepted only
    when it is uniquely best by ``MIN_ASSIGNMENT_MARGIN``, every block lands inside the alignment
    band, and the concatenated non-whitespace content is unchanged. Anything else is refused.
    """
    count = len(source_blocks)
    refusals: list[str] = []
    if count < 2 or len(target_blocks) != count:
        return Resegmentation(first_index, (), False, False, None, None, ("window_not_aligned",))
    joined = "\n\n".join(target_blocks)
    units = _sentences(joined)
    if len(units) < count:
        refusals.append("fewer_sentence_units_than_paragraphs")
        return Resegmentation(first_index, (), False, False, None, None, tuple(refusals))
    source_words = [len(block.split()) for block in source_blocks]
    unit_words = [len(unit.split()) for unit in units]
    total_source = sum(source_words)
    total_target = sum(unit_words)
    if not total_source or not total_target:
        return Resegmentation(first_index, (), False, False, None, None, ("empty_window",))
    expansion = total_target / total_source
    # Dynamic program over cut positions: best[k][j] is the cheapest way to assign the first j
    # units to the first k paragraphs, remembering the previous cut so the split can be replayed
    # and the runner-up so uniqueness can be measured.
    prefix = [0]
    for words in unit_words:
        prefix.append(prefix[-1] + words)
    infinity = math.inf
    best: list[list[float]] = [[infinity] * (len(units) + 1) for _ in range(count + 1)]
    back: list[list[int]] = [[-1] * (len(units) + 1) for _ in range(count + 1)]
    best[0][0] = 0.0
    for k in range(1, count + 1):
        for j in range(k, len(units) + 1):
            for i in range(k - 1, j):
                if best[k - 1][i] == infinity:
                    continue
                candidate = best[k - 1][i] + _cost(
                    prefix[j] - prefix[i], source_words[k - 1], expansion
                )
                if candidate < best[k][j]:
                    best[k][j] = candidate
                    back[k][j] = i
    optimum = best[count][len(units)]
    if optimum == infinity:
        return Resegmentation(first_index, (), False, False, None, None, ("no_assignment",))
    cuts: list[int] = []
    j = len(units)
    for k in range(count, 0, -1):
        cuts.append(j)
        j = back[k][j]
    cuts.reverse()
    starts = [0, *cuts[:-1]]
    blocks = tuple(" ".join(units[start:end]) for start, end in zip(starts, cuts, strict=True))
    # Uniqueness: forbid each chosen cut in turn and re-solve; the best alternative bounds how
    # close a materially different split comes to the optimum.
    runner_up = infinity
    for forbidden in cuts[:-1]:
        alternative = _solve_without(count, units, unit_words, source_words, expansion, forbidden)
        runner_up = min(runner_up, alternative)
    margin = runner_up - optimum if runner_up != infinity else infinity
    if margin < MIN_ASSIGNMENT_MARGIN:
        refusals.append("assignment_not_unique")
    for source, block in zip(source_blocks, blocks, strict=True):
        ratio = len(block.split()) / len(source.split()) if source.split() else 0.0
        if policy.measurable(len(source.split())) and not policy.in_band(ratio):
            refusals.append("resegmented_block_outside_band")
            break
    preserved = lexical_content(joined) == lexical_content("\n\n".join(blocks))
    if not preserved:
        refusals.append("lexical_content_changed")
    return Resegmentation(
        first_index,
        blocks if not refusals else (),
        not refusals,
        preserved,
        optimum,
        None if margin == infinity else margin,
        tuple(refusals),
    )


def _solve_without(
    count: int,
    units: tuple[str, ...],
    unit_words: list[int],
    source_words: list[int],
    expansion: float,
    forbidden_cut: int,
) -> float:
    """Best assignment cost when one specific cut position may not be used."""
    prefix = [0]
    for words in unit_words:
        prefix.append(prefix[-1] + words)
    infinity = math.inf
    best = [[infinity] * (len(units) + 1) for _ in range(count + 1)]
    best[0][0] = 0.0
    for k in range(1, count + 1):
        for j in range(k, len(units) + 1):
            if j == forbidden_cut and k < count:
                continue
            for i in range(k - 1, j):
                if best[k - 1][i] == infinity:
                    continue
                candidate = best[k - 1][i] + _cost(
                    prefix[j] - prefix[i], source_words[k - 1], expansion
                )
                best[k][j] = min(best[k][j], candidate)
    return best[count][len(units)]
