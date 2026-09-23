"""Occurrence-anchored ``competing_variant`` evidence (terminology QA 1.6.0).

Every fixture here is synthetic.  The *shapes* are the ones a finished book actually produces —
a descriptive reference, an alias, a title with a surname, a collective beside a formal name, an
in-world synonym, a source-provided gloss — because those are what the chunk-wide check mistook
for rival renderings.  What the validator is asked in each case is the same question: does this
translation render *this* governed source occurrence with a variant, or does the variant simply
also stand in the chunk?
"""

import pytest

from book_translator.models import (
    DecisionSource,
    TerminologyEnforcement,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.terminology import set_decision, validate_terminology
from book_translator.terminology.enforcement import resolve_enforcement
from book_translator.terminology.lexical import (
    contains_canonical_lexeme,
    enforceable_competing_variants,
)
from book_translator.terminology.registry import canonically_matched_decisions

# Synthetic canonical terminology.  The Persian strings are invented and only need to be long
# enough that the derivational stemmer treats them as words rather than fragments.
NURSE_CANONICAL = "ایلوارین"
NURSE_DESCRIPTION = "پرستار کهنسال"
ALIAS_CANONICAL = "سیتاری"
ALIAS_OTHER_NAME = "الوندرا"
FULL_NAME_CANONICAL = "دورانث کلوار"
TITLE_AND_SURNAME = "بانو کلوار"
COLLECTIVE_CANONICAL = "لاجوردی‌ها"
FORMAL_ORDER_NAME = "آجای لاجورد"
GUARD_CANONICAL = "نگاهبان"
GUARD_SYNONYM = "کاراییدین"
SPEAR_CANONICAL = "سور ونث"
SPEAR_GLOSS = "نیزهٔ سحر"


def _registry(
    *,
    source_term: str,
    target_term: str,
    variants: tuple[str, ...],
    classification: TerminologyType = TerminologyType.PEOPLE,
    decision_source: DecisionSource = DecisionSource.AI,
    locked: bool = False,
) -> TerminologyRegistry:
    return set_decision(
        TerminologyRegistry(
            schema_version="1.0",
            version="1.0.0",
            book_id="synthetic-book",
            source_language="en",
            target_language="fa",
        ),
        source_term=source_term,
        target_term=target_term,
        classification=classification,
        decision_source=decision_source,
        locked=locked,
        competing_variants=variants,
    )


def _codes(registry: TerminologyRegistry, source: str, translation: str) -> set[str]:
    return {item.code for item in validate_terminology(source, translation, registry).findings}


def _chunk_wide_codes(registry: TerminologyRegistry, source: str, translation: str) -> set[str]:
    """Reproduce the pre-1.6.0 rule: any enforceable variant anywhere in the chunk is a finding."""
    language = registry.target_language
    return {
        "competing_variant"
        for _candidate, decision in canonically_matched_decisions(registry, source)
        for variant in enforceable_competing_variants(
            decision.target_term, decision.competing_variants, target_language=language
        )
        if contains_canonical_lexeme(translation, variant, target_language=language)
    }


# --------------------------------------------------------------------------------------------
# A related expression in the same block is not a rival rendering
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "source_term", "canonical", "variant", "source", "translation"),
    [
        # "Lini … her old nurse": a person and a descriptive reference to that person.
        (
            "descriptive_reference",
            "Ilvarin",
            NURSE_CANONICAL,
            NURSE_DESCRIPTION,
            "Ilvarin spoke sharply, as she would not have dared with her old nurse.\n",
            f"{NURSE_CANONICAL} تند سخن گفت، آنچنان که نزد {NURSE_DESCRIPTION} جرئت نمی‌کرد.\n",
        ),
        # "Min … Elmindreda": one character, two source names, both written in one block.
        (
            "alias",
            "Sethari",
            ALIAS_CANONICAL,
            ALIAS_OTHER_NAME,
            "Sit over there, Sethari. Or are you still Elmindreda?\n",
            f"آنجا بنشین، {ALIAS_CANONICAL}. یا هنوز {ALIAS_OTHER_NAME} هستی؟\n",
        ),
        # "Duranda Tharne … Mistress Tharne": a full name and a title with the surname.
        (
            "title_or_short_name",
            "Duranth Kelvar",
            FULL_NAME_CANONICAL,
            TITLE_AND_SURNAME,
            "Duranth Kelvar had upset her, and Mistress Kelvar's remarks stung.\n",
            f"{FULL_NAME_CANONICAL} آشفته‌اش کرده بود و سخنان {TITLE_AND_SURNAME} می‌گزید.\n",
        ),
        # "Blues … Blue Ajah": a collective and the formal name of the same order.
        (
            "grammatical_form",
            "Azuries",
            COLLECTIVE_CANONICAL,
            FORMAL_ORDER_NAME,
            "Some of the Blue Ajah are gathering. If Azuries have gone, others must have.\n",
            f"برخی از {FORMAL_ORDER_NAME} گرد می‌آیند."
            f" اگر {COLLECTIVE_CANONICAL} رفته‌اند، دیگران هم رفته‌اند.\n",
        ),
        # "Warder … Gaidin": two distinct source terms for the same role.
        (
            "distinct_source_term",
            "Wardsman",
            GUARD_CANONICAL,
            GUARD_SYNONYM,
            "Few men saw a Wardsman who did not wish it, even without the Gaidin cloak.\n",
            f"کمتر کسی {GUARD_CANONICAL}ی را می‌دید که نخواهد، حتی بی‌ردای {GUARD_SYNONYM}.\n",
        ),
        # "He was Cor Darei, a Night Spear": the source supplies the term and its own gloss.
        (
            "translated_gloss",
            "Sor Vaneth",
            SPEAR_CANONICAL,
            SPEAR_GLOSS,
            "He was Sor Vaneth, a Dawn Spear. He wore the red headband.\n",
            f"او {SPEAR_CANONICAL} بود، یک {SPEAR_GLOSS}. سربند سرخ بسته بود.\n",
        ),
    ],
)
def test_a_related_expression_beside_a_canonical_rendering_is_not_a_competing_variant(
    label: str,
    source_term: str,
    canonical: str,
    variant: str,
    source: str,
    translation: str,
) -> None:
    registry = _registry(source_term=source_term, target_term=canonical, variants=(variant,))
    assert _chunk_wide_codes(registry, source, translation) == {"competing_variant"}, label
    assert _codes(registry, source, translation) == set(), label
    assert validate_terminology(source, translation, registry).valid


def test_a_variant_in_a_block_with_no_governed_occurrence_is_not_evidence() -> None:
    """Locality alone settles this one: the variant is nowhere near the occurrence."""
    registry = _registry(
        source_term="Wardsman", target_term=GUARD_CANONICAL, variants=(GUARD_SYNONYM,)
    )
    source = "The Wardsman waited.\n\nThe Gaidin cloak lay folded.\n"
    translation = f"{GUARD_CANONICAL} منتظر ماند.\n\nردای {GUARD_SYNONYM} تا شده بود.\n"
    assert _chunk_wide_codes(registry, source, translation) == {"competing_variant"}
    assert _codes(registry, source, translation) == set()


# --------------------------------------------------------------------------------------------
# Real substitutions still block
# --------------------------------------------------------------------------------------------


def test_the_governed_occurrence_rendered_with_the_variant_still_blocks() -> None:
    """The canonical stands elsewhere, so only locality can tell this from the cases above."""
    registry = _registry(
        source_term="Wardsman", target_term=GUARD_CANONICAL, variants=(GUARD_SYNONYM,)
    )
    source = "The Wardsman waited.\n\nA quiet street.\n"
    translation = f"{GUARD_SYNONYM} منتظر ماند.\n\n{GUARD_CANONICAL} در کوچه‌ای خلوت.\n"
    report = validate_terminology(source, translation, registry)
    assert {item.code for item in report.findings} == {"competing_variant"}
    assert not report.valid


def test_one_of_several_occurrences_rendered_with_the_variant_still_blocks() -> None:
    """A block one canonical realization short is a block whose deficit the variant fills."""
    registry = _registry(
        source_term="Wardsman", target_term=GUARD_CANONICAL, variants=(GUARD_SYNONYM,)
    )
    source = "The Wardsman waited. The Wardsman did not move.\n"
    translation = f"{GUARD_CANONICAL} منتظر ماند. {GUARD_SYNONYM} تکان نخورد.\n"
    assert _codes(registry, source, translation) == {"competing_variant"}


def test_a_canonical_missing_at_the_occurrence_is_still_reported() -> None:
    registry = _registry(
        source_term="Wardsman", target_term=GUARD_CANONICAL, variants=(GUARD_SYNONYM,)
    )
    source = "The Wardsman waited.\n"
    translation = f"{GUARD_SYNONYM} منتظر ماند.\n"
    assert _codes(registry, source, translation) == {"competing_variant", "canonical_term_missing"}


def test_several_rival_renderings_of_one_occurrence_each_block() -> None:
    registry = _registry(
        source_term="Wardsman",
        target_term=GUARD_CANONICAL,
        variants=(GUARD_SYNONYM, SPEAR_GLOSS),
    )
    source = "The Wardsman waited.\n\nA quiet street.\n"
    translation = f"{GUARD_SYNONYM} و {SPEAR_GLOSS} منتظر ماندند.\n\n{GUARD_CANONICAL} آمد.\n"
    report = validate_terminology(source, translation, registry)
    findings = [item for item in report.findings if item.code == "competing_variant"]
    assert len(findings) == 2
    assert not report.valid


# --------------------------------------------------------------------------------------------
# Undecidable alignment, and human-owned decisions
# --------------------------------------------------------------------------------------------


def test_an_unalignable_translation_fails_closed_rather_than_clearing_the_finding() -> None:
    """No block correspondence means no occurrence to anchor to, so the finding stands."""
    registry = _registry(
        source_term="Wardsman", target_term=GUARD_CANONICAL, variants=(GUARD_SYNONYM,)
    )
    source = "The Wardsman waited.\n\nA quiet street.\n"
    merged = f"{GUARD_CANONICAL} منتظر ماند و ردای {GUARD_SYNONYM} آنجا بود.\n"
    codes = _codes(registry, source, merged)
    assert "competing_variant" in codes
    assert not validate_terminology(source, merged, registry).valid


def test_a_locked_user_decision_keeps_exact_enforcement_at_the_occurrence() -> None:
    registry = _registry(
        source_term="Wardsman",
        target_term=GUARD_CANONICAL,
        variants=(GUARD_SYNONYM,),
        classification=TerminologyType.STORY_TERM,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    assert (
        resolve_enforcement(registry.candidates[0], registry.decisions[0])
        is TerminologyEnforcement.EXACT
    )
    source = "The Wardsman waited.\n\nA quiet street.\n"
    substituted = f"{GUARD_SYNONYM} منتظر ماند.\n\n{GUARD_CANONICAL} آمد.\n"
    assert _codes(registry, source, substituted) == {"competing_variant"}
    # Locality never becomes a way past a locked decision: a chunk with no canonical at all is
    # still both a missing canonical and a rival rendering.
    absent = f"{GUARD_SYNONYM} منتظر ماند.\n\nکوچه‌ای خلوت.\n"
    assert _codes(registry, source, absent) == {"competing_variant", "canonical_term_missing"}


# --------------------------------------------------------------------------------------------
# Monotonicity: 1.6.0 only ever removes findings
# --------------------------------------------------------------------------------------------


def test_the_new_rule_never_raises_a_finding_the_chunk_wide_rule_did_not() -> None:
    """Why no already-accepted translation can regress.

    ``competing_variant`` is now the old condition plus a conjunctive gate, so its finding set is
    a subset of the pre-1.6.0 one for every input.  A translation that carried no terminology
    error cannot acquire one, which is what makes the version bump safe to install over a run
    that has already accepted 179 jobs.
    """

    registry = _registry(
        source_term="Wardsman",
        target_term=GUARD_CANONICAL,
        variants=(GUARD_SYNONYM, SPEAR_GLOSS),
    )
    cases = (
        ("The Wardsman waited.\n", f"{GUARD_CANONICAL} منتظر ماند.\n"),
        ("The Wardsman waited.\n", f"{GUARD_SYNONYM} منتظر ماند.\n"),
        ("The Wardsman waited.\n", f"{GUARD_CANONICAL} و {GUARD_SYNONYM} منتظر ماندند.\n"),
        (
            "The Wardsman waited.\n\nA quiet street.\n",
            f"{GUARD_CANONICAL} منتظر ماند.\n\nردای {GUARD_SYNONYM} آنجا بود.\n",
        ),
        (
            "The Wardsman waited.\n\nA quiet street.\n",
            f"{GUARD_SYNONYM} منتظر ماند.\n\n{GUARD_CANONICAL} آمد.\n",
        ),
        ("The Wardsman waited.\n\nA quiet street.\n", f"{GUARD_SYNONYM} تنها.\n"),
        ("A quiet street.\n", f"{GUARD_SYNONYM} آنجا بود.\n"),
    )
    for source, translation in cases:
        assert _codes(registry, source, translation) & {"competing_variant"} <= (
            _chunk_wide_codes(registry, source, translation)
        ), (source, translation)
