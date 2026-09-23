"""Read-only unified canonical terminology and conservative proposal constraints."""

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    TerminologyCandidate,
    TerminologyProposal,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.terminology.lexical import valid_competing_variants
from book_translator.terminology.registry import legacy_glossary_decisions

CanonicalOrigin = Literal["registry", "legacy_glossary"]


@dataclass(frozen=True, slots=True)
class CanonicalTerm:
    source_term: str
    target_term: str
    origin: CanonicalOrigin
    decision_source: DecisionSource | None
    locked: bool
    source_variants: tuple[str, ...] = ()
    competing_variants: tuple[str, ...] = ()

    @property
    def priority(self) -> int:
        if self.origin == "legacy_glossary":
            return 1
        if self.decision_source is DecisionSource.USER and self.locked:
            return 5
        if self.decision_source is DecisionSource.USER:
            return 4
        return 3


def _source_variants(term: str, source_language: str) -> tuple[str, ...]:
    """Return only language-adapter variants whose equivalence is narrowly defined."""
    if source_language.casefold().split("-", 1)[0] == "en" and term.startswith("the "):
        return (term[4:],)
    return ()


def canonical_view(
    registry: TerminologyRegistry, legacy_glossary: Any
) -> tuple[CanonicalTerm, ...]:
    candidates = {item.candidate_id: item for item in registry.candidates}
    terms: list[CanonicalTerm] = []
    for decision in registry.decisions:
        candidate = candidates[decision.candidate_id]
        terms.append(
            CanonicalTerm(
                candidate.source_term,
                decision.target_term,
                "registry",
                decision.decision_source,
                decision.locked,
                tuple(candidate.aliases),
                valid_competing_variants(decision.target_term, decision.competing_variants),
            )
        )
    terms.extend(
        CanonicalTerm(
            source,
            target,
            "legacy_glossary",
            None,
            False,
            _source_variants(source, registry.source_language),
        )
        for source, target, _context_sensitive in legacy_glossary_decisions(legacy_glossary)
    )
    selected: dict[str, CanonicalTerm] = {}
    for term in terms:
        for source in (term.source_term, *term.source_variants):
            existing = selected.get(source)
            if existing is None or term.priority > existing.priority:
                selected[source] = term
    return tuple(
        sorted(set(selected.values()), key=lambda item: (-item.priority, item.source_term))
    )


def canonical_view_identity(view: tuple[CanonicalTerm, ...]) -> str:
    payload = [
        {
            "source_term": item.source_term,
            "target_term": item.target_term,
            "origin": item.origin,
            "decision_source": item.decision_source,
            "locked": item.locked,
            "source_variants": item.source_variants,
            "competing_variants": item.competing_variants,
        }
        for item in view
    ]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def exact_canonical(
    candidate: TerminologyCandidate, view: tuple[CanonicalTerm, ...]
) -> CanonicalTerm | None:
    matches = [
        term
        for term in view
        if candidate.source_term == term.source_term
        or candidate.source_term in term.source_variants
    ]
    return max(matches, key=lambda item: item.priority, default=None)


def _bounded(term: str, text: str) -> bool:
    normalized_term = _normalized(term)
    normalized_text = _normalized(text)
    return (
        re.search(r"(?<!\w)" + re.escape(normalized_term) + r"(?!\w)", normalized_text) is not None
    )


def _elided_remainder(shorter: str, longer: str) -> tuple[str, ...] | None:
    """Return the words *longer* adds around *shorter*, or None on an unclean word split.

    Whole words only: ``Asmodean`` is not a word-boundary subsequence of ``Asmodean's``, so a
    possessive or otherwise glued form never reads as an elision.
    """
    short_words = _normalized(shorter).split(" ")
    long_words = _normalized(longer).split(" ")
    span = len(short_words)
    for start in range(len(long_words) - span + 1):
        if long_words[start : start + span] == short_words:
            return tuple(long_words[:start] + long_words[start + span :])
    return None


@lru_cache(maxsize=8)
def elliptical_shorthand_sources(view: tuple[CanonicalTerm, ...]) -> frozenset[str]:
    """Return source terms whose canonical target renders a longer name they elide.

    ``Red -> آجای سرخ`` is the shorthand a reader uses for ``Red Ajah -> آجای سرخ``: the target
    spells out the elided head ``Ajah``, so it translates the longer name rather than the word
    ``Red`` itself.  Such a term must not impose its target on every other name that merely reuses
    the ordinary word, which is why ``Red Eagle`` or ``Red Shields`` are not Red Ajah compounds.

    The evidence required is deliberately narrow: a longer canonical with a byte-identical target,
    a clean whole-word split, and a canonical decision for *every* elided word whose own target is
    present in the shared target on safe lexical boundaries.  A component such as ``Whitecloak``
    has no such longer twin and keeps its constraint.
    """
    canonical_targets: dict[str, str] = {}
    by_target: dict[str, list[CanonicalTerm]] = defaultdict(list)
    for term in view:
        canonical_targets.setdefault(term.source_term, term.target_term)
        by_target[term.target_term].append(term)
    shorthands: set[str] = set()
    for target, group in by_target.items():
        if len(group) < 2:
            continue
        for term in group:
            for other in group:
                if other.source_term == term.source_term:
                    continue
                remainder = _elided_remainder(term.source_term, other.source_term)
                if not remainder:
                    continue
                if all(
                    (elided := canonical_targets.get(word)) is not None and _bounded(elided, target)
                    for word in remainder
                ):
                    shorthands.add(term.source_term)
                    break
    return frozenset(shorthands)


def canonical_constraints(
    candidate: TerminologyCandidate, view: tuple[CanonicalTerm, ...]
) -> tuple[CanonicalTerm, ...]:
    exact = exact_canonical(candidate, view)
    shorthands = elliptical_shorthand_sources(view)
    return tuple(
        term
        for term in view
        if term is not exact
        and term.source_term not in shorthands
        and any(
            _bounded(source, candidate.source_term)
            for source in (term.source_term, *term.source_variants)
        )
    )


def _target_identity_present(target: str, proposed: str, target_language: str) -> bool:
    if _bounded(target, proposed):
        return True
    if target_language.casefold().split("-", 1)[0] == "fa":
        normalized_target = _normalized(target)
        normalized_proposed = _normalized(proposed)
        if (
            re.search(
                r"(?<!\w)" + re.escape(normalized_target) + r"(?:\u200c\w+)(?!\w)",
                normalized_proposed,
            )
            is not None
        ):
            return True
        # Ezafe-like compounds commonly pluralize the head noun before the
        # remaining canonical words (for example X Y -> Xان Y).  This is a
        # bounded grammatical inflection, not permission for fuzzy matching.
        words = normalized_target.split(" ")
        for index, word in enumerate(words):
            inflected = [*words]
            inflected[index] = word + "ان"
            if _bounded(" ".join(inflected), normalized_proposed):
                return True
    return False


def target_identity_present(target: str, proposed: str, target_language: str) -> bool:
    """Public boundary-safe target containment used by validation and remediation."""
    return _target_identity_present(target, proposed, target_language)


def _normalized(value: str) -> str:
    apostrophes = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "＇": "'"})
    dashes = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"})
    return " ".join(
        unicodedata.normalize("NFKC", value).translate(apostrophes).translate(dashes).split()
    )


def proposal_conflicts(
    proposal: TerminologyProposal,
    candidate: TerminologyCandidate,
    view: tuple[CanonicalTerm, ...],
    target_language: str,
) -> tuple[str, ...]:
    conflicts: list[str] = []
    exact = exact_canonical(candidate, view)
    if exact is not None:
        if any(
            _target_identity_present(value, proposal.proposed_target, target_language)
            for value in exact.competing_variants
        ):
            conflicts.append("known_competing_variant")
        if not _target_identity_present(
            exact.target_term, proposal.proposed_target, target_language
        ):
            conflicts.append("exact_canonical_conflict")
    for constraint in canonical_constraints(candidate, view):
        if any(
            _target_identity_present(value, proposal.proposed_target, target_language)
            for value in constraint.competing_variants
        ):
            conflicts.append("known_competing_variant")
        if not _target_identity_present(
            constraint.target_term, proposal.proposed_target, target_language
        ):
            conflicts.append("canonical_subterm_conflict")
    return tuple(sorted(set(conflicts)))


_SAFE_GLOBAL_TYPES = frozenset(
    {
        TerminologyType.PERSON,
        TerminologyType.PLACE,
        TerminologyType.ORGANIZATION,
        TerminologyType.TITLE,
        TerminologyType.PEOPLE,
        TerminologyType.INVENTED,
        TerminologyType.ARTIFACT,
        TerminologyType.HISTORICAL_EVENT,
    }
)


def canonicalization_confidence(candidate: TerminologyCandidate) -> float:
    if candidate.context_sensitive or candidate.classification not in _SAFE_GLOBAL_TYPES:
        return 0.0
    if candidate.status is CandidateStatus.REJECTED or candidate.classification_conflicts:
        return 0.0
    return candidate.confidence
