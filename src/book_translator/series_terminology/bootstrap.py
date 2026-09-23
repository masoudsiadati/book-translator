"""Pure construction of a series policy from explicit decisions and corpus evidence.

Precedence is fixed: an explicit user decision, then the project's mandatory glossary, then
promoted corpus agreement, then unresolved observed variants. Historical evidence is attached to
every term unchanged; when it disagrees with an explicit decision the disagreement is recorded as
a conflict and the decision stays authoritative.
"""

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    ObservedVariant,
    ObservedVariantSupport,
    ReferenceEvidenceQuality,
    ReferenceEvidenceSource,
    SeriesProvenanceKind,
    SeriesTerm,
    SeriesTermConflict,
    SeriesTermConflictKind,
    SeriesTerminologyPolicy,
    SeriesTermProvenance,
    SeriesTermStatus,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.series_terminology.corpus import CorpusConcept, classification_for
from book_translator.series_terminology.identity import concept_identity, normalize_source_term
from book_translator.series_terminology.policy import SeriesTerminologyError, empty_policy
from book_translator.series_terminology.promotion import promote
from book_translator.terminology.concept_precedence import concept_key
from book_translator.terminology.registry import legacy_glossary_decisions


@dataclass(frozen=True, slots=True)
class ExplicitDecision:
    source_term: str
    target_term: str
    classification: TerminologyType
    kind: SeriesProvenanceKind
    origin: str
    rationale: str
    aliases: tuple[str, ...] = ()
    competing_variants: tuple[str, ...] = ()
    allowed_target_forms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PriorVolumeDecision:
    source_term: str
    target_term: str
    classification: TerminologyType
    reference_id: str
    observation_count: int


@dataclass(frozen=True, slots=True)
class PolicyImportReport:
    locked: int
    strong_reference: int
    observed_variants: int
    conflicts: tuple[tuple[str, str], ...]


def _key(term: str) -> str:
    return normalize_source_term(term).casefold()


def explicit_decisions_from_registry(
    registry: TerminologyRegistry, *, origin: str
) -> tuple[ExplicitDecision, ...]:
    """User-owned decisions are explicit; everything else in a registry is not."""
    candidates = {item.candidate_id: item for item in registry.candidates}
    found = []
    for decision in registry.decisions:
        if decision.decision_source is not DecisionSource.USER:
            continue
        candidate = candidates[decision.candidate_id]
        if candidate.status is not CandidateStatus.APPROVED:
            continue
        found.append(
            ExplicitDecision(
                source_term=candidate.source_term,
                target_term=decision.target_term,
                classification=candidate.classification,
                kind=SeriesProvenanceKind.EXPLICIT_USER,
                origin=f"{origin}#{decision.decision_id}",
                rationale=("explicit user decision" + (" (locked)" if decision.locked else "")),
                aliases=tuple(candidate.aliases),
                competing_variants=tuple(decision.competing_variants),
                allowed_target_forms=tuple(decision.allowed_target_forms),
            )
        )
    return tuple(found)


def explicit_decisions_from_glossary(
    raw: Mapping[str, Any], *, origin: str
) -> tuple[ExplicitDecision, ...]:
    kinds = {
        "characters": TerminologyType.PERSON,
        "places": TerminologyType.PLACE,
        "groups": TerminologyType.ORGANIZATION,
    }
    classification_by_term: dict[str, TerminologyType] = {}
    for category, classification in kinds.items():
        values = raw.get(category)
        if isinstance(values, Mapping):
            for source in values:
                classification_by_term[str(source)] = classification
    return tuple(
        ExplicitDecision(
            source_term=source,
            target_term=target,
            classification=classification_by_term.get(source, TerminologyType.STORY_TERM),
            kind=SeriesProvenanceKind.EXPLICIT_PROJECT,
            origin=origin,
            rationale="mandatory project glossary entry",
        )
        for source, target, _ in legacy_glossary_decisions(dict(raw))
    )


def prior_volume_decisions(
    registry: TerminologyRegistry,
) -> tuple[PriorVolumeDecision, ...]:
    """Non-user decisions of an earlier volume: evidence about what this project used before."""
    candidates = {item.candidate_id: item for item in registry.candidates}
    found = []
    for decision in registry.decisions:
        if decision.decision_source is DecisionSource.USER:
            continue
        candidate = candidates[decision.candidate_id]
        if candidate.status is not CandidateStatus.APPROVED or candidate.context_sensitive:
            continue
        found.append(
            PriorVolumeDecision(
                source_term=candidate.source_term,
                target_term=decision.target_term,
                classification=candidate.classification,
                reference_id=decision.decision_id,
                observation_count=sum(item.count for item in candidate.occurrences),
            )
        )
    return tuple(found)


@dataclass
class _Concept:
    source_terms: Counter[str]
    aliases: set[str]
    classifications: Counter[TerminologyType]
    variants: dict[str, list[ObservedVariantSupport]]
    explicit: list[ExplicitDecision]
    prior: list[PriorVolumeDecision]

    def add_support(self, target_form: str, support: ObservedVariantSupport) -> None:
        self.variants.setdefault(target_form, []).append(support)


def _concepts(
    corpus_concepts: Iterable[CorpusConcept],
    explicit: Iterable[ExplicitDecision],
    prior: Iterable[PriorVolumeDecision],
) -> dict[str, _Concept]:
    concepts: dict[str, _Concept] = {}

    def get(term: str) -> _Concept:
        return concepts.setdefault(_key(term), _Concept(Counter(), set(), Counter(), {}, [], []))

    for concept in corpus_concepts:
        primary, *aliases = concept.source_forms
        record = get(primary)
        record.source_terms[normalize_source_term(primary)] += 1
        record.aliases.update(normalize_source_term(alias) for alias in aliases)
        record.classifications[classification_for(concept.category)] += 1
        for form in concept.forms:
            record.add_support(
                form.target_form,
                ObservedVariantSupport(
                    corpus_id=form.corpus_id,
                    source=form.source,
                    quality=form.quality,
                    observation_count=form.observation_count,
                    reference_id=form.reference_id,
                    observed_surfaces=dict(form.observed_surfaces),
                ),
            )
    for decision in explicit:
        record = get(decision.source_term)
        record.source_terms[normalize_source_term(decision.source_term)] += 3
        record.aliases.update(normalize_source_term(alias) for alias in decision.aliases)
        record.classifications[decision.classification] += 3
        record.explicit.append(decision)
    for item in prior:
        record = get(item.source_term)
        record.source_terms[normalize_source_term(item.source_term)] += 1
        record.classifications[item.classification] += 1
        record.prior.append(item)
        record.add_support(
            item.target_term,
            ObservedVariantSupport(
                corpus_id="prior-volume",
                source=ReferenceEvidenceSource.PRIOR_VOLUME_REGISTRY,
                quality=ReferenceEvidenceQuality.CLEAN,
                observation_count=item.observation_count,
                reference_id=item.reference_id,
            ),
        )
    return concepts


def _variants(record: _Concept) -> list[ObservedVariant]:
    return [
        ObservedVariant(
            target_form=target,
            support=sorted(
                supports,
                key=lambda item: (item.corpus_id, item.source.value, item.reference_id or ""),
            ),
        )
        for target, supports in sorted(record.variants.items())
    ]


def _contains_term(text: str, term: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text) is not None


def _explicit_choice(record: _Concept) -> tuple[ExplicitDecision, list[SeriesTermConflict]]:
    ranked = sorted(
        record.explicit, key=lambda item: item.kind is not SeriesProvenanceKind.EXPLICIT_USER
    )
    chosen = ranked[0]
    conflicts = [
        SeriesTermConflict(
            kind=SeriesTermConflictKind.EXPLICIT_SOURCES_DISAGREE,
            detail=(
                f"{other.kind.value} source {other.origin} records {other.target_term!r}; "
                f"{chosen.kind.value} decision {chosen.target_term!r} is authoritative"
            ),
            competing_forms=[other.target_term],
        )
        for other in ranked[1:]
        if other.target_term != chosen.target_term
    ]
    return chosen, conflicts


def _corpus_conflicts(
    target_term: str, variants: list[ObservedVariant], kind: SeriesTermConflictKind
) -> list[SeriesTermConflict]:
    rivals = [
        variant.target_form
        for variant in variants
        if variant.clean
        and variant.target_form != target_term
        and any(
            item.source
            in {ReferenceEvidenceSource.NARRATIVE, ReferenceEvidenceSource.PRINTED_GLOSSARY}
            for item in variant.support
        )
    ]
    if not rivals:
        return []
    return [
        SeriesTermConflict(
            kind=kind,
            detail=(
                f"clean corpus evidence records {', '.join(rivals)}; the explicit decision "
                f"{target_term!r} stays authoritative and the evidence is preserved as observed"
            ),
            competing_forms=rivals,
        )
    ]


def _prior_conflicts(target_term: str, record: _Concept) -> list[SeriesTermConflict]:
    other = sorted({item.target_term for item in record.prior if item.target_term != target_term})
    if not other:
        return []
    return [
        SeriesTermConflict(
            kind=SeriesTermConflictKind.PRIOR_VOLUME_DISAGREES,
            detail=f"the prior volume rendered this term as {', '.join(other)}",
            competing_forms=other,
        )
    ]


def build_policy(
    *,
    series_id: str,
    source_language: str,
    target_language: str,
    corpus_concepts: Iterable[CorpusConcept],
    explicit: Iterable[ExplicitDecision],
    prior: Iterable[PriorVolumeDecision] = (),
) -> tuple[SeriesTerminologyPolicy, PolicyImportReport]:
    policy = empty_policy(
        series_id=series_id, source_language=source_language, target_language=target_language
    )
    concepts = _concepts(corpus_concepts, explicit, prior)
    locked_targets: dict[str, str] = {}
    terms: list[SeriesTerm] = []
    conflicts: list[tuple[str, str]] = []
    # Explicit decisions first so promoted terms can be checked against locked components.
    for key in sorted(concepts, key=lambda item: (not concepts[item].explicit, item)):
        record = concepts[key]
        source_term = record.source_terms.most_common(1)[0][0]
        # Every other attested spelling (a case variant, a glossary alias) stays matchable: the
        # matchers downstream are byte-exact, so dropping "saidar" beside "Saidar" would hide
        # every lowercase occurrence from enforcement.
        aliases = sorted((record.aliases | set(record.source_terms)) - {source_term})
        classification = record.classifications.most_common(1)[0][0]
        variants = _variants(record)
        concept_id = concept_identity(source_term, source_language)
        if record.explicit:
            chosen, term_conflicts = _explicit_choice(record)
            term_conflicts += _corpus_conflicts(
                chosen.target_term,
                # Corpus evidence for a form the decision authorized agrees with it.
                [v for v in variants if v.target_form not in chosen.allowed_target_forms],
                SeriesTermConflictKind.CORPUS_DISAGREES_WITH_EXPLICIT_DECISION,
            )
            term_conflicts += _prior_conflicts(chosen.target_term, record)
            locked_targets[source_term] = chosen.target_term
            terms.append(
                SeriesTerm(
                    concept_id=concept_id,
                    source_term=source_term,
                    aliases=aliases,
                    classification=chosen.classification,
                    status=SeriesTermStatus.LOCKED,
                    target_term=chosen.target_term,
                    allowed_target_forms=list(chosen.allowed_target_forms),
                    provenance=SeriesTermProvenance(
                        kind=chosen.kind, origin=chosen.origin, rationale=chosen.rationale
                    ),
                    variants=variants,
                    conflicts=term_conflicts,
                )
            )
            conflicts.extend((source_term, item.detail) for item in term_conflicts)
            continue
        outcome = promote(variants)
        status, target, summary = outcome.status, outcome.target_term, outcome.evidence_summary
        notes: list[str] = []
        if status is SeriesTermStatus.STRONG_REFERENCE and target is not None:
            # A promoted term must honour a locked decision on the same concept, and containment
            # runs both ways. A *longer* corpus form would smuggle a rejected spelling back in
            # through a compound; a *shorter* one -- the same concept without its article -- would
            # stand beside the locked term as a second canonical rendering of one thing in the
            # story, which every prompt path would then render and no enforcement path would
            # reconcile. Neither may become a decision; both keep their evidence.
            for locked_source, locked_target in locked_targets.items():
                if concept_key(source_term) == concept_key(locked_source):
                    if target == locked_target:
                        continue
                    status, target = SeriesTermStatus.OBSERVED_VARIANTS, None
                    detail = (
                        f"corpus form {outcome.target_term!r} rivals the locked rendering "
                        f"{locked_target!r} of the same concept, named {locked_source!r}"
                    )
                elif _contains_term(source_term, locked_source) and not _contains_term(
                    target, locked_target
                ):
                    status, target = SeriesTermStatus.OBSERVED_VARIANTS, None
                    detail = (
                        f"corpus form {outcome.target_term!r} does not contain the locked "
                        f"rendering {locked_target!r} of its component {locked_source!r}"
                    )
                else:
                    continue
                notes.append(detail)
                conflicts.append((source_term, detail))
                break
        if status is SeriesTermStatus.STRONG_REFERENCE:
            provenance = SeriesTermProvenance(
                kind=SeriesProvenanceKind.PROMOTED_REFERENCE,
                origin=policy.promotion_rule_version,
                rationale="independent clean corpus sources agree",
                evidence_summary=summary,
            )
            term_conflicts = _prior_conflicts(target or "", record)
        else:
            provenance = SeriesTermProvenance(
                kind=(
                    SeriesProvenanceKind.PRIOR_VOLUME
                    if record.prior
                    and not any(
                        item.source is not ReferenceEvidenceSource.PRIOR_VOLUME_REGISTRY
                        for variant in variants
                        for item in variant.support
                    )
                    else SeriesProvenanceKind.UNRESOLVED
                ),
                origin=policy.promotion_rule_version,
                rationale="evidence does not justify a decision",
                evidence_summary=summary,
            )
            term_conflicts = []
        terms.append(
            SeriesTerm(
                concept_id=concept_id,
                source_term=source_term,
                aliases=aliases,
                classification=classification,
                status=status,
                target_term=target,
                provenance=provenance,
                variants=variants,
                conflicts=term_conflicts,
                notes=notes,
            )
        )
        conflicts.extend((source_term, item.detail) for item in term_conflicts)
    if not terms:
        raise SeriesTerminologyError("no terminology evidence or explicit decision was provided")
    policy = policy.model_copy(update={"terms": sorted(terms, key=lambda item: item.concept_id)})
    counts = Counter(term.status for term in policy.terms)
    return policy, PolicyImportReport(
        locked=counts[SeriesTermStatus.LOCKED],
        strong_reference=counts[SeriesTermStatus.STRONG_REFERENCE],
        observed_variants=counts[SeriesTermStatus.OBSERVED_VARIANTS],
        conflicts=tuple(conflicts),
    )
