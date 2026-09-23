"""Pure policy deciding what target evidence proves a canonical decision was honored.

``required`` answers whether a decision is in scope for QA; enforcement answers what counts as
proof.  Until these were separated, every approved decision meant "the exact canonical target
string must appear in the translation", which is right for a name and wrong for a source form the
target language legitimately transforms — a demonym rendered through its place name, an adjectival
form becoming a noun inside an ezafe construction, a phrase that survives only as a paraphrase.

The resolution below is deliberately derivable from registry data that already exists, so a frozen
terminology resource can be re-evaluated under corrected semantics without being rewritten.
"""

from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyEnforcement,
    TerminologyRegistry,
    TerminologyType,
)

# Only classifications whose *source* form is systematically transformed by the target language are
# relaxed.  Naming, artifact, title, and event terminology stays exact: those are proper nouns whose
# whole purpose is a stable rendering, and weakening them to fix an unrelated class of false
# positives would trade a visible error for a silent one.
_CLASSIFICATION_ENFORCEMENT = {
    TerminologyType.PEOPLE: TerminologyEnforcement.MORPHOLOGICAL,
    TerminologyType.RECURRING_PHRASE: TerminologyEnforcement.CONTEXTUAL,
    TerminologyType.CONTEXT_SENSITIVE: TerminologyEnforcement.CONTEXTUAL,
}

# Source-side demonym morphology, applied only to a single-word ``people`` term so that a canonical
# place or organization decision can satisfy it.  Stripping is closed over two rounds because forms
# like ``Taraboners`` are a plural of a demonym of a place name.
_SOURCE_DEMONYM_SUFFIXES = (
    "nin",
    "ians",
    "ian",
    "ish",
    "ese",
    "ers",
    "ins",
    "ans",
    "er",
    "in",
    "an",
    "i",
    "n",
    "s",
)
_MIN_SOURCE_STEM_LENGTH = 4
_RELATIVE_CLASSIFICATIONS = frozenset(
    {TerminologyType.PLACE, TerminologyType.ORGANIZATION, TerminologyType.INVENTED}
)


def resolve_enforcement(
    candidate: TerminologyCandidate, decision: TerminologyDecision
) -> TerminologyEnforcement:
    """Return the enforcement mode in effect for one canonical decision.

    An explicitly recorded mode always wins, so a registry can override the policy per decision
    rather than accumulating ad-hoc exceptions in the validator.
    """

    if decision.enforcement is not None:
        return decision.enforcement
    if not decision.required:
        return TerminologyEnforcement.ADVISORY
    # Ownership, not classification, decides a human's terminology: a locked or user-owned decision
    # is a deliberate instruction and stays byte-exact whatever the source form looks like.
    if decision.locked or decision.decision_source is DecisionSource.USER:
        return TerminologyEnforcement.EXACT
    if candidate.context_sensitive:
        return TerminologyEnforcement.CONTEXTUAL
    return _CLASSIFICATION_ENFORCEMENT.get(candidate.classification, TerminologyEnforcement.EXACT)


def _source_stems(source_term: str) -> tuple[str, ...]:
    stems = {source_term}
    for _ in range(2):
        for stem in tuple(stems):
            stems.update(
                stem[: -len(suffix)]
                for suffix in _SOURCE_DEMONYM_SUFFIXES
                if stem.endswith(suffix) and len(stem) - len(suffix) >= _MIN_SOURCE_STEM_LENGTH
            )
    return tuple(sorted(stems - {source_term}))


def derivational_relatives(
    registry: TerminologyRegistry, candidate: TerminologyCandidate
) -> tuple[TerminologyDecision, ...]:
    """Return canonical decisions whose source term is the root *candidate* is derived from.

    "Saldaean" is what Persian renders as "of Saldaea", so the canonical target of the *place* is
    legitimate evidence for the demonym.  The relation is derived from the source language, because
    the two canonical targets need not be lexically related to each other at all.
    """

    if candidate.classification is not TerminologyType.PEOPLE or " " in candidate.source_term:
        return ()
    stems = set(_source_stems(candidate.source_term))
    if not stems:
        return ()
    decisions = {item.candidate_id: item for item in registry.decisions}
    return tuple(
        decisions[item.candidate_id]
        for item in sorted(registry.candidates, key=lambda entry: entry.candidate_id)
        if item.candidate_id != candidate.candidate_id
        and item.source_term in stems
        and item.status is CandidateStatus.APPROVED
        and item.classification in _RELATIVE_CLASSIFICATIONS
        and item.candidate_id in decisions
    )
