"""Precedence between governed concepts whose source forms name one fictional concept.

A series policy keys a concept on its exact source term, because every matcher downstream is
byte-exact and a lost spelling is a lost occurrence. That keying is right, and it has one
consequence it cannot see by itself: an explicit project decision on ``"the Dark One"`` and a
corpus promotion of ``"Dark One"`` are two concepts carrying two targets for one thing in the
story. Nothing downstream reconciles them, so the prompt paths render both instructions and the
enforcement paths honour only the longer span -- a provider told to write the weaker rendering is
then rejected for having written it.

This module is the one place that decides which of two such concepts governs. It is pure: it
reads terms and returns a resolution, and it never edits a policy, a registry, or a file.

The rule is deliberately narrow. Two concepts are the same concept here only when their source
terms are equal after folding case and removing one leading English article. That is enough for
``the Dark One``/``Dark One`` and it deliberately leaves every other nesting alone: ``Dragon``
and ``false Dragon``, ``Two Rivers`` and ``Two Rivers folk``, ``angreal`` and ``ter'angreal`` are
different concepts that merely share a substring, and collapsing them would silently retarget
real decisions. Containment is never sufficient on its own.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum

from book_translator.models import (
    SeriesProvenanceKind,
    SeriesTerm,
    SeriesTerminologyPolicy,
    SeriesTermStatus,
)
from book_translator.series_terminology.identity import normalize_source_term

#: Governs the concept-precedence rule: how concepts are keyed and which decision dominates.
CONCEPT_PRECEDENCE_VERSION = "1.0.0"

#: Leading English articles. An article is only ever removed as a whole leading word.
_ARTICLES = ("the", "a", "an")


class DecisionRank(IntEnum):
    """How strongly a decision binds, highest first. Ordering is the whole contract."""

    EXPLICIT_USER_LOCKED = 5
    EXPLICIT_PROJECT_LOCKED = 4
    OTHER_LOCKED = 3
    STRONG_REFERENCE = 2
    WEAKER_EVIDENCE = 1


def strip_leading_article(term: str) -> str:
    """Remove one leading English article, as a whole word, preserving the rest byte for byte."""
    head, separator, tail = normalize_source_term(term).partition(" ")
    if separator and head.casefold() in _ARTICLES:
        return tail
    return normalize_source_term(term)


def concept_key(term: str) -> str:
    """The identity two source spellings share when they name one concept.

    Case is folded because the policy already treats ``The One Power`` and ``the One Power`` as one
    concept; the leading article is removed because it is grammar, not identity.
    """
    return strip_leading_article(term).casefold()


def decision_rank(status: SeriesTermStatus, provenance: SeriesProvenanceKind) -> DecisionRank:
    """Rank one decision. Provenance separates the three tiers of ``LOCKED``."""
    if status is SeriesTermStatus.LOCKED:
        if provenance is SeriesProvenanceKind.EXPLICIT_USER:
            return DecisionRank.EXPLICIT_USER_LOCKED
        if provenance is SeriesProvenanceKind.EXPLICIT_PROJECT:
            return DecisionRank.EXPLICIT_PROJECT_LOCKED
        return DecisionRank.OTHER_LOCKED
    if status is SeriesTermStatus.STRONG_REFERENCE:
        return DecisionRank.STRONG_REFERENCE
    return DecisionRank.WEAKER_EVIDENCE


def term_rank(term: SeriesTerm) -> DecisionRank:
    return decision_rank(term.status, term.provenance.kind)


@dataclass(frozen=True, slots=True)
class ConceptConflict:
    """One weaker decision that a stronger decision on the same concept displaces."""

    concept_key: str
    dominant_concept_id: str
    dominant_source_term: str
    dominant_target: str
    dominant_rank: DecisionRank
    superseded_concept_id: str
    superseded_source_term: str
    superseded_target: str
    superseded_rank: DecisionRank

    @property
    def detail(self) -> str:
        return (
            f"{self.superseded_source_term!r} ({self.superseded_rank.name}) renders "
            f"{self.superseded_target!r} for the same concept that "
            f"{self.dominant_source_term!r} ({self.dominant_rank.name}) renders "
            f"{self.dominant_target!r}"
        )


@dataclass(frozen=True, slots=True)
class PrecedenceResolution:
    """Which concepts govern, which are displaced, and the spellings each one answers for."""

    #: superseded concept id -> the concept id that displaces it.
    superseded: tuple[tuple[str, str], ...]
    conflicts: tuple[ConceptConflict, ...]
    #: dominant concept id -> every source spelling it now governs, longest first.
    absorbed_forms: tuple[tuple[str, tuple[str, ...]], ...]
    #: source spelling -> the target that now governs it.
    resolved_targets: tuple[tuple[str, str], ...]

    def is_superseded(self, concept_id: str) -> bool:
        return any(item == concept_id for item, _ in self.superseded)

    def dominant_of(self, concept_id: str) -> str | None:
        return next((winner for item, winner in self.superseded if item == concept_id), None)

    def source_forms_of(self, concept_id: str) -> tuple[str, ...]:
        return next((forms for item, forms in self.absorbed_forms if item == concept_id), ())

    def active_target(self, source_term: str) -> str | None:
        key = normalize_source_term(source_term)
        return next((target for form, target in self.resolved_targets if form == key), None)


def _decided(term: SeriesTerm) -> bool:
    return term.status is not SeriesTermStatus.OBSERVED_VARIANTS and bool(term.target_term)


def resolve_precedence(terms: Iterable[SeriesTerm]) -> PrecedenceResolution:
    """Decide, for every group of article/case variants, which decision governs the concept.

    Only decided concepts take part: an ``OBSERVED_VARIANTS`` record carries evidence rather than
    a decision, so it neither displaces anything nor is displaced. A group whose members already
    agree on a target is not a conflict and nothing is superseded.
    """
    decided = sorted((term for term in terms if _decided(term)), key=lambda item: item.concept_id)
    groups: dict[str, list[SeriesTerm]] = {}
    for term in decided:
        groups.setdefault(concept_key(term.source_term), []).append(term)

    superseded: list[tuple[str, str]] = []
    conflicts: list[ConceptConflict] = []
    absorbed: list[tuple[str, tuple[str, ...]]] = []
    targets: dict[str, str] = {}

    for key in sorted(groups):
        group = groups[key]
        for term in group:
            targets[normalize_source_term(term.source_term)] = term.target_term or ""
        if len(group) == 1 or len({term.target_term for term in group}) == 1:
            continue
        # Highest rank wins; ties break on the longer source term, then the concept id, so the
        # result never depends on input order or on dictionary iteration.
        dominant = max(
            group,
            key=lambda item: (term_rank(item), len(item.source_term), item.concept_id),
        )
        forms = {normalize_source_term(form) for form in (dominant.source_term, *dominant.aliases)}
        for term in group:
            if term.concept_id == dominant.concept_id:
                continue
            superseded.append((term.concept_id, dominant.concept_id))
            forms.update(normalize_source_term(form) for form in (term.source_term, *term.aliases))
            targets[normalize_source_term(term.source_term)] = dominant.target_term or ""
            conflicts.append(
                ConceptConflict(
                    concept_key=key,
                    dominant_concept_id=dominant.concept_id,
                    dominant_source_term=dominant.source_term,
                    dominant_target=dominant.target_term or "",
                    dominant_rank=term_rank(dominant),
                    superseded_concept_id=term.concept_id,
                    superseded_source_term=term.source_term,
                    superseded_target=term.target_term or "",
                    superseded_rank=term_rank(term),
                )
            )
        # Longest first so a byte-exact matcher offered these forms prefers the widest span.
        absorbed.append(
            (dominant.concept_id, tuple(sorted(forms, key=lambda form: (-len(form), form))))
        )

    return PrecedenceResolution(
        superseded=tuple(sorted(superseded)),
        conflicts=tuple(sorted(conflicts, key=lambda item: item.superseded_concept_id)),
        absorbed_forms=tuple(sorted(absorbed)),
        resolved_targets=tuple(sorted(targets.items())),
    )


def governing_terms(terms: Sequence[SeriesTerm]) -> tuple[SeriesTerm, ...]:
    """Every term that still carries an active canonical decision after precedence is applied."""
    resolution = resolve_precedence(terms)
    return tuple(term for term in terms if not resolution.is_superseded(term.concept_id))


def policy_concept_conflicts(policy: SeriesTerminologyPolicy) -> tuple[ConceptConflict, ...]:
    """Every place a policy states two rival canonical renderings of one concept.

    This is the detector for the class, not a fix: it reports and never edits. It is conservative
    by construction -- it groups only article and case variants, so two concepts that merely share
    a substring are never reported, and two spellings that already agree on a target are not a
    conflict at all.
    """
    return resolve_precedence(policy.terms).conflicts
