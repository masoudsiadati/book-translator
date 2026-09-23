"""Pure policy for a canonical term realized through target-language coordinate ellipsis.

Exact enforcement asks the translation to carry the canonical target as one contiguous lexeme.
That is right for every ordinary occurrence and wrong for exactly one construction: a source
coordination that shares the canonical term's modifier.  ``High Lord or Lady`` is a single noun
phrase with two heads, and Persian writes the shared modifier once — «لرد یا بانوی اعظم» — so the
canonical target «لرد اعظم» survives as an ordered discontinuous realization instead of a
contiguous string.  Failing that translation reports a defect the translator did not commit.

The relaxation is deliberately the narrowest one that covers the construction, and it is gated on
evidence the frozen registry already holds rather than on a phrase list:

* the *source* occurrence must sit next to an English coordinator, and
* the registry must independently attest the second conjunct as its own canonical decision.

Nothing here accepts reordered modifiers, unbounded insertions, a coordination the registry cannot
corroborate, or a human-owned decision.  Those cases are bad registry data or a real defect, and
relaxing the matcher to absorb them would trade a visible error for a silent one.
"""

import re
from dataclasses import dataclass

from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    TerminologyDecision,
    TerminologyRegistry,
)
from book_translator.terminology.lexical import PERSIAN_INFLECTION_PATTERN
from book_translator.terminology.registry import ExemptOccurrences, source_term_matches

# A coordinated conjunct is the only material allowed to interrupt the canonical target, so the gap
# is bounded to what one conjunct plus its coordinator can occupy and may never cross a line.
_MAX_ELIDED_WORDS = 3
_SOURCE_COORDINATION = re.compile(
    r"[,;]?[^\S\n]+(?:and|or)[^\S\n]+(?P<conjunct>[A-Za-z][A-Za-z'’-]*)"
)
_TARGET_COORDINATORS = frozenset({"و", "یا"})
_GAP = rf"([^\S\n]+(?:\S+[^\S\n]+){{0,{_MAX_ELIDED_WORDS}}})"


@dataclass(frozen=True, slots=True)
class CoordinatedOccurrence:
    """One source occurrence whose canonical term is the first head of a shared-modifier phrase."""

    candidate_id: str
    source_term: str
    conjunct_source_term: str
    start: int
    end: int


def coordinated_occurrences(
    registry: TerminologyRegistry, source_text: str, exemptions: ExemptOccurrences | None = None
) -> tuple[CoordinatedOccurrence, ...]:
    """Return authoritative source matches that are the leading conjunct of a coordination.

    The second conjunct is reconstructed by substituting its head into the matched term, so the
    relation is only recognized when the registry already treats that substitution as terminology
    in its own right.  A coordination the registry cannot corroborate stays an ordinary occurrence.
    """

    decided = {item.candidate_id for item in registry.decisions}
    attested = {
        candidate.source_term
        for candidate in registry.candidates
        if candidate.status is CandidateStatus.APPROVED and candidate.candidate_id in decided
    }
    found: list[CoordinatedOccurrence] = []
    for match in source_term_matches(registry, source_text, exemptions):
        if match.is_alias:
            continue
        words = match.term.split()
        if len(words) < 2:
            continue
        coordination = _SOURCE_COORDINATION.match(source_text, match.end)
        if coordination is None:
            continue
        conjunct = " ".join([*words[:-1], coordination.group("conjunct")])
        if conjunct == match.term or conjunct not in attested:
            continue
        found.append(
            CoordinatedOccurrence(
                candidate_id=match.candidate_id,
                source_term=match.term,
                conjunct_source_term=conjunct,
                start=match.start,
                end=match.end,
            )
        )
    return tuple(found)


def coordination_eligible(decision: TerminologyDecision) -> bool:
    """Return whether *decision* may be proved by a coordinate-elided realization.

    A locked or user-owned decision is a deliberate human instruction about the written form, so it
    keeps demanding the contiguous target however the source is constructed.
    """

    return not decision.locked and decision.decision_source is not DecisionSource.USER


def contains_coordinated_lexeme(text: str, term: str, *, target_language: str) -> bool:
    """Return whether *text* carries *term*'s words in order across one coordinated gap.

    Order is preserved and every gap is bounded, so this proves the canonical target was written
    with a conjunct folded into it — not that its words happen to occur somewhere nearby.
    """

    if target_language.lower().split("-", maxsplit=1)[0] != "fa":
        return False
    words = term.split()
    if len(words) < 2:
        return False
    lexeme = [
        r"(?<!\w)" + re.escape(word) + rf"(?:{PERSIAN_INFLECTION_PATTERN})?(?!\w)" for word in words
    ]
    pattern = re.compile(_GAP.join(lexeme))
    return any(
        any(_TARGET_COORDINATORS.intersection(gap.split()) for gap in match.groups())
        for match in pattern.finditer(text)
    )
