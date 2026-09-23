"""Pure read-only terminology and translation QA validators."""

import hashlib
import re

from book_translator.models import (
    DecisionState,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyEnforcement,
    TerminologyFinding,
    TerminologyRegistry,
    TerminologyValidationReport,
    TranslationQAFinding,
    TranslationQAReport,
)
from book_translator.terminology.alignment import (
    ParagraphAlignment,
    paragraph_alignment,
    paragraph_count,
)
from book_translator.terminology.composition import (
    contains_coordinated_lexeme,
    coordinated_occurrences,
    coordination_eligible,
)
from book_translator.terminology.enforcement import derivational_relatives, resolve_enforcement
from book_translator.terminology.lexical import (
    contains_canonical_lexeme,
    contains_canonical_realization,
    contains_morphological_realization,
    count_canonical_realizations,
    count_morphological_realizations,
    enforceable_competing_variants,
    recorded_singulars,
)
from book_translator.terminology.registry import (
    ExemptOccurrences,
    canonically_matched_decisions,
    governing_source_matches,
)

#: 1.12.0 honors user-adjudicated occurrence exemptions: an exempt source span selects no
#: decision. Without exemptions every verdict is identical to 1.11.0.
TERMINOLOGY_QA_VERSION = "1.12.0"
STRUCTURAL_QA_VERSION = "1.1.0"

__all__ = [
    "STRUCTURAL_QA_VERSION",
    "TERMINOLOGY_QA_VERSION",
    "authorized_realizations",
    "number_realizations",
    "evidenced_competing_variants",
    "paragraph_count",
    "validate_terminology",
    "validate_translation_qa",
]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def authorized_realizations(decision: TerminologyDecision) -> tuple[str, ...]:
    """The preferred target followed by every further form the decision explicitly authorized.

    This is the whole set of spellings that can prove a decision was honored. Nothing is derived
    here: a decision without authorized forms yields its target alone, which is exactly the literal
    the validator has always looked for.
    """
    return (decision.target_term, *decision.allowed_target_forms)


def number_realizations(decision: TerminologyDecision) -> tuple[str, ...]:
    """The singulars of a plural canonical that the decision itself records (see lexical.py).

    A decision written in the plural records its singular as a competing variant when it wants
    the plural preferred; that spelling is the canonical in the other number, not a rival, so a
    single referent written with it proves the decision honored.
    """
    return recorded_singulars(authorized_realizations(decision), decision.competing_variants)


def _canonical_identity_present(
    registry: TerminologyRegistry,
    translation_text: str,
    candidate: TerminologyCandidate,
    decision: TerminologyDecision,
    enforcement: TerminologyEnforcement,
    coordinated: bool,
) -> bool:
    """Return whether the translation carries the evidence *enforcement* asks for."""
    language = registry.target_language
    # A source coordination the registry itself attests lets the target elide the shared modifier,
    # so the canonical words may be written in order across one conjunct instead of contiguously.
    if (
        coordinated
        and coordination_eligible(decision)
        and contains_coordinated_lexeme(
            translation_text, decision.target_term, target_language=language
        )
    ):
        return True
    # Exact enforcement means "at least one explicitly authorized realization", each matched with
    # the same lexical boundaries and contracted inflections the preferred target alone gets.
    if enforcement is TerminologyEnforcement.EXACT:
        return contains_canonical_realization(
            translation_text,
            authorized_realizations(decision),
            target_language=language,
            singulars=number_realizations(decision),
        )
    if contains_morphological_realization(
        translation_text, authorized_realizations(decision), target_language=language
    ):
        return True
    # A canonical decision the source form is derived from is canonical identity too, so a demonym
    # rendered through its own place name is a match rather than an omission.
    return any(
        contains_morphological_realization(
            translation_text, authorized_realizations(relative), target_language=language
        )
        for relative in derivational_relatives(registry, candidate)
    )


#: Block key for an authoritative source match no alignment block contains.  Its presence in a
#: decision's occurrence map means that decision cannot be judged by locality at all.
_UNPLACED = -1


def _governed_occurrences(
    registry: TerminologyRegistry,
    source_text: str,
    alignment: ParagraphAlignment,
    exemptions: ExemptOccurrences | None = None,
) -> dict[str, dict[int, int]]:
    """Count each decision's authoritative source occurrences per source block.

    Only governing matches count, which is the same rule that selects the decision at all: the
    source term and its own inflections, never an alias that is a different source expression,
    because treating a different source expression as evidence about this one is the defect this
    locality exists to prevent.  A match the alignment cannot place is recorded under a sentinel
    block so the caller can fail closed on it rather than silently drop an occurrence it was
    supposed to account for.
    """

    counts: dict[str, dict[int, int]] = {}
    for match in governing_source_matches(registry, source_text, exemptions):
        index = alignment.source_index(match.start)
        blocks = counts.setdefault(match.candidate_id, {})
        key = _UNPLACED if index is None else index
        blocks[key] = blocks.get(key, 0) + 1
    return counts


def _canonical_realizations(
    registry: TerminologyRegistry,
    text: str,
    candidate: TerminologyCandidate,
    decision: TerminologyDecision,
    enforcement: TerminologyEnforcement,
) -> int:
    """Count the canonical realizations *text* carries for one decision.

    This is the counting form of :func:`_canonical_identity_present` and recognizes exactly the
    same spellings, so "the canonical is present" and "the canonical is present *n* times" can
    never disagree about what a canonical realization looks like.
    """

    language = registry.target_language
    forms = authorized_realizations(decision)
    if enforcement is TerminologyEnforcement.EXACT:
        return count_canonical_realizations(
            text, forms, target_language=language, singulars=number_realizations(decision)
        )
    return count_morphological_realizations(text, forms, target_language=language) + sum(
        count_morphological_realizations(
            text, authorized_realizations(relative), target_language=language
        )
        for relative in derivational_relatives(registry, candidate)
    )


def _variant_realizes_a_governed_occurrence(
    registry: TerminologyRegistry,
    alignment: ParagraphAlignment | None,
    occurrences: dict[int, int],
    candidate: TerminologyCandidate,
    decision: TerminologyDecision,
    enforcement: TerminologyEnforcement,
    variant: str,
    coordinated: bool,
) -> bool:
    """Return whether *variant* is evidenced as the rendering of a governed source occurrence.

    Presence of the variant *somewhere* in the translation is not that evidence and never was.  A
    finished book is full of expressions that legitimately look like a rival rendering and are not
    one: an alias, a title, a short or full name, a descriptive reference, a source-provided gloss,
    a plural, or simply a different source term whose canonical target happens to be related.  The
    old check saw all of them, because it asked whether the string occurred in the chunk rather
    than whether it stood where the governed occurrence was translated.

    Positive evidence requires two things at once, in one aligned block:

    * the block is the translation of a source block that actually contains an authoritative
      governing occurrence of this decision's source term — locality; and
    * that block carries fewer canonical realizations than it has governed occurrences to
      realize — a deficit, meaning at least one governed occurrence there is *not* canonically
      rendered and the variant is a candidate for what replaced it.

    Without a deficit the governed occurrences in that block are all accounted for canonically, so
    whatever the variant is rendering, it is not one of them.

    When no alignment exists the question is undecidable and the answer is ``True``: the finding
    stands.  That is the pre-1.6.0 behavior, and it is reached only for a translation whose block
    structure already differs from its source, which the structural validator reports as a blocking
    finding of its own.
    """

    if alignment is None or _UNPLACED in occurrences:
        return True
    language = registry.target_language
    for index, demand in sorted(occurrences.items()):
        block = alignment.translated(index)
        if not contains_canonical_lexeme(block, variant, target_language=language):
            continue
        if (
            coordinated
            and coordination_eligible(decision)
            and contains_coordinated_lexeme(block, decision.target_term, target_language=language)
        ):
            # A coordinate-elided realization is one canonical rendering written discontinuously;
            # counting its words would understate what the block actually carries.
            continue
        if _canonical_realizations(registry, block, candidate, decision, enforcement) >= demand:
            continue
        return True
    return False


def evidenced_competing_variants(
    source_text: str,
    translation_text: str,
    registry: TerminologyRegistry,
    exemptions: ExemptOccurrences | None = None,
) -> dict[str, tuple[str, ...]]:
    """Return, per candidate, the recorded variants evidenced as rendering a governed occurrence.

    This is the single source of truth for the ``competing_variant`` finding.  The validator raises
    exactly what this returns, and the final-QA analyzer reports enforceability from it, so an
    audit can never tell a reviewer that the installed validator would fire on something it would
    not — which is how a variant nothing tied to a source occurrence came to read like a defect.
    """

    relevant = canonically_matched_decisions(registry, source_text, exemptions)
    coordinated_ids = {
        occurrence.candidate_id
        for occurrence in coordinated_occurrences(registry, source_text, exemptions)
    }
    alignment = paragraph_alignment(source_text, translation_text)
    governed = (
        _governed_occurrences(registry, source_text, alignment, exemptions)
        if alignment is not None
        else {}
    )
    evidenced: dict[str, tuple[str, ...]] = {}
    for candidate, decision in relevant:
        enforcement = resolve_enforcement(candidate, decision)
        found = tuple(
            variant
            for variant in enforceable_competing_variants(
                decision.target_term,
                decision.competing_variants,
                target_language=registry.target_language,
                authorized_forms=decision.allowed_target_forms,
            )
            if contains_canonical_lexeme(
                translation_text, variant, target_language=registry.target_language
            )
            and _variant_realizes_a_governed_occurrence(
                registry,
                alignment,
                governed.get(candidate.candidate_id, {}),
                candidate,
                decision,
                enforcement,
                variant,
                candidate.candidate_id in coordinated_ids,
            )
        )
        if found:
            evidenced[candidate.candidate_id] = found
    return evidenced


def validate_terminology(
    source_text: str,
    translation_text: str,
    registry: TerminologyRegistry,
    exemptions: ExemptOccurrences | None = None,
) -> TerminologyValidationReport:
    """Judge one translation against every decision a governing source occurrence selects.

    *exemptions* withholds user-adjudicated occurrences of *source_text* from selection; it is
    proven against that exact text and refused for any other.
    """
    findings: list[TerminologyFinding] = []
    relevant = canonically_matched_decisions(registry, source_text, exemptions)
    coordinated_ids = {
        occurrence.candidate_id
        for occurrence in coordinated_occurrences(registry, source_text, exemptions)
    }
    evidenced = evidenced_competing_variants(source_text, translation_text, registry, exemptions)
    for candidate, decision in relevant:
        enforcement = resolve_enforcement(candidate, decision)
        if decision.decision_state is DecisionState.PROVISIONAL:
            # Enforcement is identical for a provisional decision — a book must not drift term by
            # term while a question is open — but the report records which findings rest on a
            # working choice so a later resolution can be scoped to exactly those translations.
            findings.append(
                TerminologyFinding(
                    code="provisional_decision_enforced",
                    severity="warning",
                    candidate_id=candidate.candidate_id,
                    source_term=candidate.source_term,
                    enforcement=enforcement,
                    message="canonical enforcement came from a provisional decision",
                )
            )
        for _variant in evidenced.get(candidate.candidate_id, ()):
            findings.append(
                TerminologyFinding(
                    code="competing_variant",
                    severity="error",
                    candidate_id=candidate.candidate_id,
                    source_term=candidate.source_term,
                    enforcement=enforcement,
                    message=(
                        "a governed source occurrence is rendered with a known competing"
                        " terminology variant"
                    ),
                )
            )
        if enforcement is TerminologyEnforcement.ADVISORY or _canonical_identity_present(
            registry,
            translation_text,
            candidate,
            decision,
            enforcement,
            candidate.candidate_id in coordinated_ids,
        ):
            continue
        # A contextual decision cannot be judged by target presence alone, so its absence is
        # recorded for review rather than treated as a mechanical defect.  Everything else keeps
        # the blocking error it has always produced.
        contextual = enforcement is TerminologyEnforcement.CONTEXTUAL
        findings.append(
            TerminologyFinding(
                code="canonical_term_unverified" if contextual else "canonical_term_missing",
                severity="warning" if contextual else "error",
                candidate_id=candidate.candidate_id,
                source_term=candidate.source_term,
                enforcement=enforcement,
                message=(
                    "source occurrence has no canonical target terminology match; this decision is"
                    " not decidable by exact target presence"
                    if contextual
                    else "source occurrence has no canonical target terminology match"
                ),
            )
        )
    return TerminologyValidationReport(
        schema_version="1.0",
        # Warnings record provenance; only errors can invalidate. An open semantic question must
        # never fail QA on its own, or a provisional decision would block the pipeline it exists
        # to keep moving.
        valid=not any(item.severity == "error" for item in findings),
        source_sha256=_hash(source_text),
        translation_sha256=_hash(translation_text),
        checked_decisions=len(relevant),
        findings=findings,
    )


def validate_translation_qa(
    source_text: str,
    translation_text: str,
    *,
    target_language: str,
    source_anomaly_registry: object | None = None,
    chapter_id: str | None = None,
) -> TranslationQAReport:
    findings: list[TranslationQAFinding] = []
    if source_anomaly_registry is not None and chapter_id is not None:
        from book_translator.models import SourceAnomalyRegistry
        from book_translator.source_anomalies import relevant_anomalies

        registry = SourceAnomalyRegistry.model_validate(source_anomaly_registry)
        for _anomaly in relevant_anomalies(registry, chapter_id, source_text):
            findings.append(
                TranslationQAFinding(
                    code="known_source_anomaly",
                    severity="warning",
                    message="registered source uncertainty is scoped to this source span",
                )
            )
    source_paragraphs = paragraph_count(source_text)
    target_paragraphs = paragraph_count(translation_text)
    if source_paragraphs != target_paragraphs:
        findings.append(
            TranslationQAFinding(
                code="paragraph_count_mismatch",
                severity="error",
                message="source and translation paragraph counts differ",
            )
        )
    checks = {
        "trailing_double_space": any(line.endswith("  ") for line in translation_text.splitlines()),
        "tab": "\t" in translation_text,
        "replacement_character": "\ufffd" in translation_text,
        "doubled_zwnj": "\u200c\u200c" in translation_text,
    }
    for code, found in checks.items():
        if found:
            findings.append(
                TranslationQAFinding(code=code, severity="warning", message=code.replace("_", " "))
            )
    if target_language.lower().split("-")[0] == "fa":
        for code, character in (("arabic_yeh", "\u064a"), ("arabic_kaf", "\u0643")):
            if character in translation_text:
                findings.append(
                    TranslationQAFinding(
                        code=code,
                        severity="warning",
                        message=f"translation contains {code.replace('_', ' ')}",
                    )
                )
        if re.search(r"[\u0600-\u06ff]\u200c و[\u0600-\u06ff]", translation_text):
            findings.append(
                TranslationQAFinding(
                    code="suspicious_persian_conjunction_spacing",
                    severity="warning",
                    message=(
                        "translation contains a safely detectable suspicious conjunction boundary"
                    ),
                )
            )
    return TranslationQAReport(
        schema_version="1.0",
        valid=not any(item.severity == "error" for item in findings),
        source_sha256=_hash(source_text),
        translation_sha256=_hash(translation_text),
        source_paragraph_count=source_paragraphs,
        translation_paragraph_count=target_paragraphs,
        findings=findings,
    )
