"""Pure deterministic analysis of why a final QA rejection exists.

Nothing here performs I/O, contacts a provider, or reads the workspace. Every input is supplied
by the caller: the frozen registry the run was judged against, the effective registry the project
holds now, the immutable source and translation text, and the historical QA reports. Everything
returned is a classification plus the counted evidence that produced it, so the same inputs always
produce the same answer and a reviewer can re-derive it by hand.

The interesting question is never "does the translation contain a competing variant" — the
validator already answered that — but "what does that occurrence *mean*". A recorded competing
variant is a claim about the world made when the decision was written, and by the time a book is
finished that claim can be several different things: the canonical's own plural, a rendering of an
entirely different source expression, a rival the registry has since dropped, or a genuine
disagreement between two decisions. Only the last of those is a translation defect.
"""

from dataclasses import dataclass

from book_translator.models import (
    CompetingVariantClass,
    DecisionSource,
    DecisionState,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyEnforcement,
    TerminologyRegistry,
)
from book_translator.structural_repair.engine import (
    StructuralDiagnosis,
    diagnose_structure,
)
from book_translator.terminology.enforcement import resolve_enforcement
from book_translator.terminology.lexical import (
    carries_canonical_identity,
    contains_canonical_lexeme,
    contains_morphological_realization,
    count_canonical_lexemes,
    count_canonical_realizations,
    valid_competing_variants,
)
from book_translator.terminology.registry import ExemptOccurrences, governing_source_matches
from book_translator.terminology.validation import (
    authorized_realizations,
    evidenced_competing_variants,
    number_realizations,
)

FINAL_QA_ANALYSIS_VERSION = "1.3.0"


@dataclass(frozen=True, slots=True)
class CompetingVariantAnalysis:
    """One recorded competing variant, classified against the translation that triggered it."""

    candidate: TerminologyCandidate
    decision: TerminologyDecision
    variant: str
    variant_class: CompetingVariantClass
    enforcement: TerminologyEnforcement
    canonical_present: bool
    variant_present: bool
    source_occurrences: int
    canonical_occurrences: int
    variant_occurrences: int
    recorded_in_effective_registry: bool
    rival_candidate_id: str | None
    rival_locked: bool
    enforceable: bool

    @property
    def provider_free(self) -> bool:
        from book_translator.models import PROVIDER_FREE_VARIANT_CLASSES

        return self.variant_class in PROVIDER_FREE_VARIANT_CLASSES


def _rival(
    registry: TerminologyRegistry, variant: str, candidate_id: str
) -> tuple[TerminologyCandidate, TerminologyDecision] | None:
    """Return the other active decision claiming *variant* as its own canonical target, if any.

    A decision claims every realization it explicitly authorized, not only its preferred target:
    a variant another decision lists as an allowed form is that decision's rendering, and treating
    it as unclaimed would let the analyzer call a genuine canonical conflict unexplained.
    """
    decisions = {item.candidate_id: item for item in registry.decisions}
    for candidate in sorted(registry.candidates, key=lambda item: item.candidate_id):
        decision = decisions.get(candidate.candidate_id)
        if decision is None or candidate.candidate_id == candidate_id:
            continue
        if variant in authorized_realizations(decision):
            return candidate, decision
    return None


def _classify(
    *,
    decision: TerminologyDecision,
    variant: str,
    canonical_present: bool,
    recorded_in_effective_registry: bool,
    rival: tuple[TerminologyCandidate, TerminologyDecision] | None,
    target_language: str,
) -> CompetingVariantClass:
    """Place one triggered variant in exactly one class, failing closed on the unknown."""
    # The canonical's own lexeme first: it can never evidence a rival, whatever else is true, and
    # deciding it later would let a weaker signal claim it.
    # An explicitly authorized target form is the canonical too: a variant carrying its identity,
    # or occurring inside it, is claimed exactly as the preferred target's own would be.
    if any(
        carries_canonical_identity(form, variant, target_language=target_language)
        for form in authorized_realizations(decision)
    ) or any(
        not valid_competing_variants(form, (variant,)) for form in decision.allowed_target_forms
    ):
        return CompetingVariantClass.CANONICAL_IDENTITY
    if not recorded_in_effective_registry:
        # The registry the project holds now no longer asserts this rivalry. Whether the variant
        # was retired by a correction or only ever existed in an older output, the current
        # governed state does not support the finding.
        return CompetingVariantClass.RETIRED_COMPETITOR
    if rival is not None:
        rival_candidate, rival_decision = rival
        if rival_decision.locked or rival_decision.decision_source is DecisionSource.USER:
            return CompetingVariantClass.LOCKED_CANONICAL_CONFLICT
        if decision.locked or decision.decision_source is DecisionSource.USER:
            return CompetingVariantClass.LOCKED_CANONICAL_CONFLICT
        del rival_candidate
        confirmed = (
            decision.decision_state is DecisionState.CONFIRMED,
            rival_decision.decision_state is DecisionState.CONFIRMED,
        )
        if all(confirmed):
            # Two settled decisions of equal standing render the same string differently. There
            # is no governed tie-break, and inventing one would silently pick a canonical.
            return CompetingVariantClass.GENUINE_CANONICAL_CONFLICT
        if any(confirmed):
            # Exactly one side is still a working choice, so the settled one is a deterministic
            # winner: the rivalry is a registry bookkeeping question, not a translation defect.
            return CompetingVariantClass.RIVAL_AI_DECISIONS
        return CompetingVariantClass.INSUFFICIENT_EVIDENCE
    if not canonical_present:
        # No canonical anywhere and the rival rendering present is the one shape that is
        # unambiguously a translation defect rather than a registry question.
        return CompetingVariantClass.CANONICAL_ABSENT
    return CompetingVariantClass.INSUFFICIENT_EVIDENCE


def analyze_competing_variants(
    *,
    source_text: str,
    translation_text: str,
    frozen_registry: TerminologyRegistry,
    effective_registry: TerminologyRegistry | None = None,
    historical_variants: dict[str, tuple[str, ...]] | None = None,
    exemptions: ExemptOccurrences | None = None,
) -> tuple[CompetingVariantAnalysis, ...]:
    """Classify every competing variant the frozen registry would fire on this translation.

    ``effective_registry`` is the registry the project holds *now*; when it no longer records a
    variant the frozen one did, the finding rests on retired governance rather than on the
    translation. ``historical_variants`` lets a caller assert what an older output attested,
    which is how a competitor that only ever existed in provider output is told apart from one
    the registry itself introduced.
    """
    language = frozen_registry.target_language
    effective = effective_registry if effective_registry is not None else frozen_registry
    effective_decisions = {item.candidate_id: item for item in effective.decisions}
    candidates = {item.candidate_id: item for item in frozen_registry.candidates}
    decisions = {item.candidate_id: item for item in frozen_registry.decisions}
    source_counts: dict[str, int] = {}
    # Counted over the governing matches, exactly as the validator counts them.
    for match in governing_source_matches(frozen_registry, source_text, exemptions):
        source_counts[match.candidate_id] = source_counts.get(match.candidate_id, 0) + 1
    # Asked of the validator itself rather than recomputed, so "the installed validator would
    # still raise this" is a fact about the installed validator and not a second opinion that can
    # drift away from it.
    evidenced = evidenced_competing_variants(
        source_text, translation_text, frozen_registry, exemptions
    )

    results: list[CompetingVariantAnalysis] = []
    for candidate_id in sorted(source_counts):
        candidate = candidates.get(candidate_id)
        decision = decisions.get(candidate_id)
        if candidate is None or decision is None:
            continue
        enforcement = resolve_enforcement(candidate, decision)
        canonical_present = contains_morphological_realization(
            translation_text, authorized_realizations(decision), target_language=language
        )
        canonical_occurrences = count_canonical_realizations(
            translation_text,
            authorized_realizations(decision),
            target_language=language,
            singulars=number_realizations(decision),
        )
        for variant in valid_competing_variants(decision.target_term, decision.competing_variants):
            if not contains_canonical_lexeme(translation_text, variant, target_language=language):
                continue
            effective_decision = effective_decisions.get(candidate_id)
            recorded = effective_decision is not None and variant in valid_competing_variants(
                effective_decision.target_term, effective_decision.competing_variants
            )
            variant_class = _classify(
                decision=decision,
                variant=variant,
                canonical_present=canonical_present,
                recorded_in_effective_registry=recorded,
                rival=_rival(effective, variant, candidate_id),
                target_language=language,
            )
            if (
                variant_class is CompetingVariantClass.RETIRED_COMPETITOR
                and historical_variants is not None
                and variant in historical_variants.get(candidate_id, ())
            ):
                # The registry never asserted this rivalry; an older provider output did. Saying
                # so keeps "governance changed its mind" distinct from "governance never agreed".
                variant_class = CompetingVariantClass.HISTORICAL_OUTPUT_ONLY
            rival = _rival(effective, variant, candidate_id)
            results.append(
                CompetingVariantAnalysis(
                    candidate=candidate,
                    decision=decision,
                    variant=variant,
                    variant_class=variant_class,
                    enforcement=enforcement,
                    canonical_present=canonical_present,
                    variant_present=True,
                    source_occurrences=source_counts[candidate_id],
                    canonical_occurrences=canonical_occurrences,
                    variant_occurrences=count_canonical_lexemes(
                        translation_text, variant, target_language=language
                    ),
                    recorded_in_effective_registry=recorded,
                    rival_candidate_id=rival[0].candidate_id if rival else None,
                    rival_locked=bool(rival and rival[1].locked),
                    # Whether the *installed* validator would still raise this finding. The
                    # analyzer deliberately looks at every recorded variant, including the ones
                    # enforcement now disregards, so the audit can explain a finding's absence
                    # as well as its presence. Since 1.1.0 that includes a variant the validator
                    # sees in the chunk but cannot tie to any governed source occurrence: the
                    # string is there and the finding is not, and an audit that said otherwise
                    # sent reviewers looking for a defect nothing had claimed.
                    enforceable=variant in evidenced.get(candidate_id, ()),
                )
            )
    return tuple(results)


#: The structural half of a final-QA diagnosis is the structural-repair engine's answer, not a
#: second opinion about it. Since 1.2.0 the two are the same object, so "final QA thinks the
#: paragraphs diverged" and "a deterministic repair exists" can never drift apart.
ParagraphDiagnosis = StructuralDiagnosis


def diagnose_paragraphs(source_text: str, translation_text: str) -> ParagraphDiagnosis:
    """Separate a typographic paragraph mismatch from a content one.

    The distinction is worth making before spending a provider attempt: a misplaced separator is
    a defect in how the text was written down, and no rewrite of the translation is the right
    remedy for it. The engine decides which of the two this is, renders the repair when the
    answer is the first, and fails closed otherwise.
    """
    return diagnose_structure(source_text, translation_text)
