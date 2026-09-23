"""The pure rule that decides whether corpus evidence justifies a ``STRONG_REFERENCE`` status.

Promotion is never raw frequency. A form is promoted only when independent clean evidence sources
agree on it and no rival clean form carries a material share of its support. Everything else stays
``OBSERVED_VARIANTS`` so the translator sees the evidence with its tiers rather than a decision
nobody made.
"""

from dataclasses import dataclass

from book_translator.models import (
    ObservedVariant,
    ReferenceEvidenceQuality,
    ReferenceEvidenceSource,
    SeriesTermStatus,
)

#: Independent sources: one book's narrative or one book's printed glossary each count once.
MIN_INDEPENDENT_SOURCES = 2
#: A rival clean form blocks promotion when it reaches this share of the leader's clean support.
RIVAL_SHARE = 0.25
#: ...and never blocks on a single stray observation, which is where OCR noise lives.
RIVAL_MIN_OBSERVATIONS = 2

_CORPUS_SOURCES = frozenset(
    {ReferenceEvidenceSource.NARRATIVE, ReferenceEvidenceSource.PRINTED_GLOSSARY}
)


@dataclass(frozen=True, slots=True)
class PromotionOutcome:
    status: SeriesTermStatus
    target_term: str | None
    evidence_summary: str


@dataclass(frozen=True, slots=True)
class _Support:
    target_form: str
    sources: frozenset[tuple[str, ReferenceEvidenceSource]]
    clean_count: int


def _corpus_support(variant: ObservedVariant) -> _Support:
    sources = frozenset(
        (item.corpus_id, item.source)
        for item in variant.support
        if item.source in _CORPUS_SOURCES
        and item.quality is ReferenceEvidenceQuality.CLEAN
        and item.observation_count > 0
    )
    count = sum(
        item.observation_count
        for item in variant.support
        if item.source in _CORPUS_SOURCES and item.quality is ReferenceEvidenceQuality.CLEAN
    )
    return _Support(variant.target_form, sources, count)


def describe_support(variant: ObservedVariant) -> str:
    parts = [
        f"{item.corpus_id} {item.source.value}"
        + ("" if item.quality is ReferenceEvidenceQuality.CLEAN else f" [{item.quality.value}]")
        + f" x{item.observation_count}"
        for item in sorted(
            variant.support,
            key=lambda item: (item.corpus_id, item.source.value, item.quality.value),
        )
    ]
    return f"{variant.target_form}: " + ", ".join(parts)


def promote(variants: list[ObservedVariant]) -> PromotionOutcome:
    """Apply the cross-source-agreement rule to a concept's observed variants."""
    supports = [_corpus_support(variant) for variant in variants]
    clean = [item for item in supports if item.sources]
    if not clean:
        return PromotionOutcome(
            SeriesTermStatus.OBSERVED_VARIANTS, None, "no clean corpus evidence"
        )
    leader = max(clean, key=lambda item: (len(item.sources), item.clean_count, item.target_form))
    rivals = [item for item in clean if item.target_form != leader.target_form]
    threshold = max(RIVAL_MIN_OBSERVATIONS, int(RIVAL_SHARE * leader.clean_count))
    material = [item for item in rivals if item.clean_count >= threshold]
    detail = "; ".join(describe_support(variant) for variant in variants)
    if len(leader.sources) < MIN_INDEPENDENT_SOURCES:
        return PromotionOutcome(
            SeriesTermStatus.OBSERVED_VARIANTS,
            None,
            f"single clean evidence source only ({detail})",
        )
    if not any(source is ReferenceEvidenceSource.NARRATIVE for _, source in leader.sources):
        # Printed glossaries agreeing with each other attest a headword, not a prose form.
        return PromotionOutcome(
            SeriesTermStatus.OBSERVED_VARIANTS,
            None,
            f"printed-glossary evidence only, never observed in narrative prose ({detail})",
        )
    if material:
        return PromotionOutcome(
            SeriesTermStatus.OBSERVED_VARIANTS,
            None,
            "clean sources disagree: "
            + ", ".join(item.target_form for item in material)
            + f" rival the leader at >= {threshold} observations ({detail})",
        )
    return PromotionOutcome(
        SeriesTermStatus.STRONG_REFERENCE,
        leader.target_form,
        f"{len(leader.sources)} independent clean sources agree; "
        f"no clean rival >= {threshold} observations ({detail})",
    )
