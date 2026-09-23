"""The Persian indefinite marker on an exact canonical target (terminology QA 1.9.0).

Persian marks a noun indefinite ("a <canonical>") with a suffixed ``ی``. After a consonant-final
stem it is written as one attached ``ی``, which exact enforcement already contracted. After a stem
ending in the long vowel ``ا`` or ``و`` a hiatus glide is written first, so the same marker is
``یی``; after a final ``ه`` or ``ی`` the orthography inserts a ZWNJ (``ه‌ای``), which a ZWNJ boundary
already admits. A canonical written with the vowel-final allomorph was reported as
``canonical_term_missing``. These cases pin exactly which written forms are the canonical and, just
as importantly, which longer tokens are not. Every term here is synthetic, and the rule is exercised
through several stem shapes so that no one term is special-cased.
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
    contains_canonical_realization,
    count_canonical_lexemes,
)

ZWNJ = "‌"
#: Synthetic canonicals, one per final-letter shape that selects an indefinite allomorph.
ALEF_FINAL = "کارانا"
VAV_FINAL = "تورو"
#: A synthetic compound whose interior ZWNJ must not disturb the rule at its end.
COMPOUND_ALEF_FINAL = f"نیلو{ZWNJ}سارا"
CONSONANT_FINAL = "کندارین"
HEH_FINAL = "ماریه"
VOWEL_FINAL = (ALEF_FINAL, VAV_FINAL, COMPOUND_ALEF_FINAL)


def _in_sentence(written: str) -> str:
    return f"او مانند {written} ایستاده بود.\n"


def _locked_registry(target_term: str) -> TerminologyRegistry:
    return set_decision(
        TerminologyRegistry(
            schema_version="1.0",
            version="1.0.0",
            book_id="synthetic-book",
            source_language="en",
            target_language="fa",
        ),
        source_term="Wanderer",
        target_term=target_term,
        classification=TerminologyType.TITLE,
        decision_source=DecisionSource.USER,
        locked=True,
    )


@pytest.mark.parametrize("canonical", VOWEL_FINAL)
def test_indefinite_after_a_long_vowel_is_the_exact_canonical(canonical: str) -> None:
    text = _in_sentence(f"{canonical}یی")
    assert contains_canonical_lexeme(text, canonical, target_language="fa")
    assert count_canonical_lexemes(text, canonical, target_language="fa") == 1


@pytest.mark.parametrize(
    ("canonical", "written"),
    [
        # A consonant-final stem takes the marker as one attached letter (contracted before 1.9.0).
        (CONSONANT_FINAL, f"{CONSONANT_FINAL}ی"),
        # A final heh or yeh separates the marker with a ZWNJ, already a non-word boundary.
        (HEH_FINAL, f"{HEH_FINAL}{ZWNJ}ای"),
        (f"{CONSONANT_FINAL}ی", f"{CONSONANT_FINAL}ی{ZWNJ}ای"),
        # A vowel-final stem written with the glide behind a ZWNJ is bounded the same way.
        (ALEF_FINAL, f"{ALEF_FINAL}{ZWNJ}یی"),
        # The pre-existing single attached yeh after a vowel-final stem (ezafe) is unchanged.
        (ALEF_FINAL, f"{ALEF_FINAL}ی"),
        # Plural plus indefinite behind a ZWNJ.
        (VAV_FINAL, f"{VAV_FINAL}{ZWNJ}هایی"),
    ],
)
def test_every_other_indefinite_allomorph_still_matches(canonical: str, written: str) -> None:
    text = _in_sentence(written)
    assert contains_canonical_lexeme(text, canonical, target_language="fa")
    assert count_canonical_lexemes(text, canonical, target_language="fa") == 1


@pytest.mark.parametrize("canonical", VOWEL_FINAL)
@pytest.mark.parametrize("clitic", ["م", "ت", "ش", "مان", "تان", "شان"])
def test_the_pronominal_clitics_are_preserved_on_vowel_final_canonicals(
    canonical: str, clitic: str
) -> None:
    for written in (f"{canonical}{clitic}", f"{canonical}{ZWNJ}{clitic}"):
        assert contains_canonical_lexeme(_in_sentence(written), canonical, target_language="fa")


@pytest.mark.parametrize(
    ("canonical", "written"),
    [
        # The supported suffix followed by further letters is a longer, different lexical token.
        (ALEF_FINAL, f"{ALEF_FINAL}ییگاه"),
        (COMPOUND_ALEF_FINAL, f"{COMPOUND_ALEF_FINAL}ییگاه"),
        (VAV_FINAL, f"{VAV_FINAL}ییها"),
        (ALEF_FINAL, f"{ALEF_FINAL}ییی"),
        # Letter sequences that are not one of the contracted forms.
        (ALEF_FINAL, f"{ALEF_FINAL}یین"),
        (ALEF_FINAL, f"{ALEF_FINAL}یان"),
        (ALEF_FINAL, f"{ALEF_FINAL}یها"),
        (VAV_FINAL, f"{VAV_FINAL}ها"),
        # The glide allomorph belongs to a vowel-final stem only.
        (CONSONANT_FINAL, f"{CONSONANT_FINAL}یی"),
        (HEH_FINAL, f"{HEH_FINAL}یی"),
        # A heh-final stem joins the marker only across a ZWNJ; attached, it is a malformed token.
        (HEH_FINAL, f"{HEH_FINAL}ای"),
        # Other letters that look like the glide are not it.
        (ALEF_FINAL, f"{ALEF_FINAL}ئی"),
        (ALEF_FINAL, f"{ALEF_FINAL}يي"),
        # The canonical merely prefixing a longer token.
        (ALEF_FINAL, f"{ALEF_FINAL}نی"),
        (VAV_FINAL, f"{VAV_FINAL}دی"),
        # The canonical inside, or at the tail of, a larger word.
        (ALEF_FINAL, f"نا{ALEF_FINAL}یی"),
        (VAV_FINAL, f"ب{VAV_FINAL}یی"),
        (ALEF_FINAL, f"نا{ALEF_FINAL}ییگاه"),
    ],
)
def test_the_indefinite_rule_never_becomes_substring_matching(canonical: str, written: str) -> None:
    text = _in_sentence(written)
    assert not contains_canonical_lexeme(text, canonical, target_language="fa")
    assert count_canonical_lexemes(text, canonical, target_language="fa") == 0


def test_the_indefinite_rule_is_persian_only() -> None:
    assert not contains_canonical_lexeme("a Tauroyy stood.", "Tauro", target_language="en")
    assert not contains_canonical_lexeme(
        _in_sentence(f"{ALEF_FINAL}یی"), ALEF_FINAL, target_language="en"
    )


def test_counting_sees_each_indefinite_realization_once() -> None:
    text = (
        f"{ALEF_FINAL}یی و {ALEF_FINAL} و {ALEF_FINAL}ش و {ALEF_FINAL}ییگاه و نا{ALEF_FINAL}یی.\n"
    )
    assert count_canonical_lexemes(text, ALEF_FINAL, target_language="fa") == 3


def test_an_authorized_form_gets_the_same_indefinite_allomorph() -> None:
    text = _in_sentence(f"{VAV_FINAL}یی")
    assert contains_canonical_realization(text, (ALEF_FINAL, VAV_FINAL), target_language="fa")
    assert not contains_canonical_realization(
        _in_sentence(f"{VAV_FINAL}ییگاه"), (ALEF_FINAL, VAV_FINAL), target_language="fa"
    )


@pytest.mark.parametrize("canonical", VOWEL_FINAL)
def test_locked_exact_decision_accepts_the_indefinite_allomorph(canonical: str) -> None:
    registry = _locked_registry(canonical)
    candidate = registry.candidates[0]
    assert resolve_enforcement(candidate, registry.decisions[0]) is TerminologyEnforcement.EXACT

    report = validate_terminology(
        "like a Wanderer juggling fire\n", f"همچون {canonical}یی که با آتش بازی می‌کند\n", registry
    )

    assert report.valid
    assert "canonical_term_missing" not in {item.code for item in report.findings}


@pytest.mark.parametrize(
    "written", [f"{ALEF_FINAL}ییگاه", f"نا{ALEF_FINAL}یی", f"{ALEF_FINAL}یین", "رهگذری"]
)
def test_locked_exact_decision_still_rejects_what_is_not_the_canonical(written: str) -> None:
    registry = _locked_registry(ALEF_FINAL)

    report = validate_terminology(
        "like a Wanderer juggling fire\n", f"همچون {written} که با آتش بازی می‌کند\n", registry
    )

    assert not report.valid
    assert "canonical_term_missing" in {item.code for item in report.findings}
