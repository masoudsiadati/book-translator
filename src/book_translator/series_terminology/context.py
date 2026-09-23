"""Effective translation constraints for one chunk, and their deterministic rendering.

The rendered section separates four tiers so retrieved evidence can never be mistaken for policy:
locked terms are mandatory, strong reference terms are strongly preferred, observed variants are
evidence only, and reference context is analogy rather than text to copy.

Observed variants are evidence for a question the passage actually raises. Three relevance rules
keep the tier to that (contract 1.1.0): the term must occur in the passage itself rather than
only in its untranslated context window; a spelling a locked or strong decision already claims is
not restated as an open question; and a compound's corpus form that spells a locked component
another way is withheld, because it is exactly what the lock forbids and what terminology QA
rejects. The decided tiers are untouched by these rules.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from book_translator.models import (
    ReferenceContextPackage,
    ReferenceTermEvidence,
    SeriesTerm,
    SeriesTerminologyPolicy,
    SeriesTermStatus,
)
from book_translator.series_terminology.derivation import count_occurrences
from book_translator.series_terminology.identity import (
    TERMINOLOGY_CONTEXT_CONTRACT_VERSION,
    typographic_variants,
)
from book_translator.series_terminology.promotion import describe_support
from book_translator.terminology.registry import (
    ExemptOccurrences,
    authorized_form_lines,
    boundary_spans,
)


@dataclass(frozen=True, slots=True)
class EffectiveTerminologyContext:
    contract_version: str
    locked: tuple[SeriesTerm, ...]
    strong: tuple[SeriesTerm, ...]
    observed: tuple[SeriesTerm, ...]
    reference: ReferenceContextPackage | None = None
    #: Corpus terminology whose source term the policy already holds; kept out of the reference
    #: tier so neither a decision nor an unresolved variant set is restated as fresh evidence.
    superseded_reference_terms: tuple[str, ...] = field(default=())


def term_mentioned(term: SeriesTerm, source_text: str, context_text: str = "") -> bool:
    """Whether the term or any alias occurs, in any typographic form, in the passage or context."""
    return _mentioned(term, source_text, context_text)


def _mentioned(
    term: SeriesTerm,
    source_text: str,
    context_text: str,
    exemptions: ExemptOccurrences | None = None,
) -> bool:
    if exemptions is None or not exemptions.concept_spans:
        return any(
            count_occurrences(variant, source_text) or count_occurrences(variant, context_text)
            for base in (term.source_term, *term.aliases)
            for variant in typographic_variants(base)
        )
    # A user-adjudicated occurrence of this concept in the source is not a mention of it; every
    # other occurrence, and every occurrence in the context window, still is.
    exemptions.require_text(source_text)
    return any(
        (term.concept_id, start, end) not in exemptions.concept_spans
        for base in (term.source_term, *term.aliases)
        for variant in typographic_variants(base)
        for start, end in boundary_spans(variant, source_text)
    ) or any(
        count_occurrences(variant, context_text)
        for base in (term.source_term, *term.aliases)
        for variant in typographic_variants(base)
    )


def _honouring_locked_components(
    term: SeriesTerm, locked: Sequence[SeriesTerm]
) -> SeriesTerm | None:
    """The observed term with only the forms that contain the locked rendering of its components.

    This is the policy builder's own promotion rule applied to evidence: a corpus form of "Red
    Ajah" that spells "Ajah" another way is not an open variant but a rejected spelling carried
    by a longer term. None is returned when no form survives; the locked tier already covers the
    component and the policy itself is never edited.
    """
    components = [item for item in locked if count_occurrences(item.source_term, term.source_term)]
    if not components:
        return term
    kept = [
        variant
        for variant in term.variants
        if all(
            count_occurrences(item.target_term or "", variant.target_form) for item in components
        )
    ]
    if len(kept) == len(term.variants):
        return term
    if not kept:
        return None
    withheld = len(term.variants) - len(kept)
    summary = (
        "; ".join(describe_support(variant) for variant in kept)
        + f" ({withheld} form(s) withheld: they do not contain the locked rendering of "
        + ", ".join(repr(item.source_term) for item in components)
        + ")"
    )
    return term.model_copy(
        update={
            "variants": kept,
            "provenance": term.provenance.model_copy(update={"evidence_summary": summary}),
        }
    )


def build_effective_context(
    policy: SeriesTerminologyPolicy,
    *,
    source_text: str,
    context_text: str = "",
    reference: ReferenceContextPackage | None = None,
    exemptions: ExemptOccurrences | None = None,
) -> EffectiveTerminologyContext:
    """Select the policy terms this passage raises; *exemptions* withhold adjudicated spans."""
    relevant = [
        term
        for term in sorted(policy.terms, key=lambda item: item.concept_id)
        if _mentioned(term, source_text, context_text, exemptions)
    ]
    locked = tuple(t for t in relevant if t.status is SeriesTermStatus.LOCKED)
    strong = tuple(t for t in relevant if t.status is SeriesTermStatus.STRONG_REFERENCE)
    # A spelling a decision claims (its source term or an alias) is settled for this passage; an
    # observed term for the same spelling would reopen it as evidence a few lines below.
    claimed = {
        form.casefold() for term in (*locked, *strong) for form in (term.source_term, *term.aliases)
    }
    all_locked = tuple(t for t in policy.terms if t.status is SeriesTermStatus.LOCKED)
    observed: list[SeriesTerm] = []
    for term in relevant:
        if term.status is not SeriesTermStatus.OBSERVED_VARIANTS:
            continue
        if (
            not _mentioned(term, source_text, "", exemptions)
            or term.source_term.casefold() in claimed
        ):
            continue
        honouring = _honouring_locked_components(term, all_locked)
        if honouring is not None:
            observed.append(honouring)
    # Every relevant policy term already carries the corpus evidence for its source term, so a
    # reference item for the same term would restate a decision, or an unresolved variant set,
    # as if it were independent evidence. The reference tier keeps only terms the policy does
    # not know.
    governed = {form.casefold() for term in relevant for form in (term.source_term, *term.aliases)}
    superseded: tuple[str, ...] = ()
    if reference is not None:
        superseded = tuple(
            sorted(
                {
                    item.source_term
                    for item in (*reference.terminology, *reference.entities)
                    if item.source_term.casefold() in governed
                }
            )
        )
    return EffectiveTerminologyContext(
        contract_version=TERMINOLOGY_CONTEXT_CONTRACT_VERSION,
        locked=locked,
        strong=strong,
        observed=tuple(observed),
        reference=reference,
        superseded_reference_terms=superseded,
    )


def _q(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


LOCKED_INSTRUCTION = (
    "MANDATORY. Use exactly these Persian forms for these source terms, allowing only the\n"
    "grammatical attachment Persian requires (plural, ezafe, clitics). Do not re-transliterate,\n"
    "paraphrase, modernize, or reinterpret them. A translation that renders a locked term another\n"
    "way is rejected mechanically."
)
STRONG_INSTRUCTION = (
    "STRONGLY PREFERRED. The published Persian translations of earlier volumes agree on these\n"
    "forms across independent clean sources. Use them unless the immediate grammar or meaning of\n"
    "the sentence genuinely requires another form; a divergence is reviewed, not rejected."
)
OBSERVED_INSTRUCTION = (
    "EVIDENCE ONLY. Earlier volumes rendered these terms inconsistently or with too little\n"
    "evidence to settle them. Weigh the evidence and its provenance in context; do not choose a\n"
    "form merely because it is the most frequent, and do not copy a printed-glossary headword as\n"
    "if it were in-sentence prose."
)
REFERENCE_INSTRUCTION = (
    "ANALOGY ONLY. These are clean bilingual excerpts, recurring phrases, character voice, and\n"
    "style samples from the published Persian translations of Book 1 and Book 2, selected for\n"
    "this passage. Use them for terminology, phrase choice, literary register, and character\n"
    "voice. Never copy a reference sentence: the passage below is different text, and the\n"
    "references may carry OCR noise (broken letters, spacing, or ZWNJ). Anomalous and\n"
    "reference-only material was excluded before this section was written."
)


def _variant_lines(term: SeriesTerm, *, indent: str = "      ") -> list[str]:
    return [f"{indent}- {_q(describe_support(variant))}" for variant in term.variants]


def _reference_term_lines(item: ReferenceTermEvidence) -> list[str]:
    lines = [
        f"  - source: {_q(item.source_term)}",
        f"    kind: {item.kind}",
        f"    cross_book_observation: {item.cross_book_observation}",
        "    forms:",
    ]
    for form in item.forms:
        support = ", ".join(
            f"{s.corpus_id} {s.source.value}"
            + ("" if s.quality.value == "clean" else f" [{s.quality.value}]")
            + f" x{s.observation_count}"
            for s in form.support
        )
        lines.append(f"      - {_q(form.target_form)}: {_q(support)}")
    return lines


def render_effective_context(context: EffectiveTerminologyContext) -> str:
    lines = [
        f"# Series terminology policy ({context.contract_version})",
        "Four tiers follow. Their force differs and the headings say which applies.",
        "",
        "## 1. LOCKED CONSISTENCY TERMS",
        LOCKED_INSTRUCTION,
        "locked_terms:",
    ]
    if not context.locked:
        lines.append("  []")
    for term in context.locked:
        lines.extend(
            [
                f"  - source: {_q(term.source_term)}",
                f"    target: {_q(term.target_term)}",
                *authorized_form_lines(term.allowed_target_forms, indent="    "),
                f"    classification: {term.classification.value}",
                f"    provenance: {term.provenance.kind.value}",
            ]
        )
        if term.aliases:
            lines.append(f"    aliases: {_q(term.aliases)}")
    lines += ["", "## 2. STRONG REFERENCE EVIDENCE", STRONG_INSTRUCTION, "strong_reference_terms:"]
    if not context.strong:
        lines.append("  []")
    for term in context.strong:
        lines.extend(
            [
                f"  - source: {_q(term.source_term)}",
                f"    target: {_q(term.target_term)}",
                *authorized_form_lines(term.allowed_target_forms, indent="    "),
                f"    classification: {term.classification.value}",
                f"    evidence: {_q(term.provenance.evidence_summary)}",
            ]
        )
    lines += ["", "## 3. OBSERVED VARIANTS", OBSERVED_INSTRUCTION, "observed_variants:"]
    if not context.observed:
        lines.append("  []")
    for term in context.observed:
        lines.extend(
            [
                f"  - source: {_q(term.source_term)}",
                f"    classification: {term.classification.value}",
                f"    evidence: {_q(term.provenance.evidence_summary)}",
                "    variants:",
                *_variant_lines(term),
            ]
        )
    lines += ["", "## 4. REFERENCE CONTEXT", REFERENCE_INSTRUCTION]
    reference = context.reference
    if reference is None:
        lines.append("reference_context: none")
        return "\n".join(lines).rstrip("\n") + "\n"
    lines.append("reference_terminology:")
    items = [
        item
        for item in (*reference.terminology, *reference.entities)
        if item.source_term not in context.superseded_reference_terms
    ]
    if not items:
        lines.append("  []")
    for item in items:
        lines.extend(_reference_term_lines(item))
    lines.append("recurring_phrases:")
    if not reference.phrases:
        lines.append("  []")
    for phrase in reference.phrases:
        lines.append(f"  - source: {_q(phrase.source_phrase)}")
        lines.append(f"    categories: {_q(phrase.categories)}")
        lines.append("    forms:")
        for form in phrase.forms:
            lines.append(
                f"      - {_q(form.target_form)}: "
                f"{_q(', '.join(f'{s.corpus_id} x{s.observation_count}' for s in form.support))}"
            )
    lines.append("translation_memory:")
    if not reference.translation_memory:
        lines.append("  []")
    for segment in reference.translation_memory:
        prov = segment.provenance
        lines.extend(
            [
                f"  - reference: {prov.book_label}, chapter {prov.chapter}, {prov.reference_id}"
                f" (clean narrative; alignment confidence {segment.minimum_alignment_confidence})",
                f"    en: {_q(segment.source_text)}",
                f"    fa: {_q(segment.target_text)}",
            ]
        )
    lines.append("character_voice:")
    if not reference.character_voice:
        lines.append("  []")
    for sample in reference.character_voice:
        prov = sample.provenance
        lines.extend(
            [
                f"  - character: {_q(sample.character)}"
                f" ({prov.book_label}, chapter {prov.chapter}, {prov.reference_id})",
                f"    en: {_q(sample.source_dialogue)}",
                f"    fa: {_q(sample.target_dialogue)}",
            ]
        )
    lines.append("style_samples:")
    if not reference.style:
        lines.append("  []")
    for style in reference.style:
        prov = style.provenance
        lines.extend(
            [
                f"  - reference: {prov.book_label}, chapter {prov.chapter}, {prov.reference_id};"
                f" tags: {_q(style.tags)}",
                f"    en: {_q(style.source_text)}",
                f"    fa: {_q(style.target_text)}",
            ]
        )
    excluded = reference.exclusions
    lines.append(
        "excluded_from_context: "
        + _q(
            f"reference-only TM {excluded.reference_only_tm}, anomalies {excluded.anomalies}, "
            f"uncertain glossary forms {excluded.uncertain_forms_withheld}, "
            f"ambiguous matches {excluded.ambiguous_matches}"
        )
    )
    return "\n".join(lines).rstrip("\n") + "\n"
