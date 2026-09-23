"""Attached Persian pronominal clitics on an exact canonical target (terminology QA 1.7.0).

A locked canonical is byte-exact, but Persian writes a possessive or object pronoun *onto* the
noun it belongs to: "their <canonical>" is the canonical followed directly by ``شان``. Exact
enforcement must accept that written form without turning into substring matching, so every
positive case here is paired with the negatives that make the boundary visible. The terms and
sentences are synthetic; the matcher is exercised through generic canonical targets, never a
special case for any one term.
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
    carries_canonical_identity,
    contains_canonical_lexeme,
    count_canonical_lexemes,
)

ZWNJ = "‌"
#: A synthetic canonical whose final letter is a glide ``ی``, the shape that attaches clitics
#: directly.
CANONICAL = "آجای"
#: A second, consonant-final synthetic canonical, so the rule is shown to be generic.
OTHER_CANONICAL = "کندارین"


def _locked_registry(source_term: str, target_term: str) -> TerminologyRegistry:
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
        classification=TerminologyType.ORGANIZATION,
        decision_source=DecisionSource.USER,
        locked=True,
    )


@pytest.mark.parametrize(
    "written",
    [
        CANONICAL,
        f"{CANONICAL}م",
        f"{CANONICAL}ت",
        f"{CANONICAL}ش",
        f"{CANONICAL}مان",
        f"{CANONICAL}تان",
        f"{CANONICAL}شان",
        f"{CANONICAL}{ZWNJ}ش",
        f"{CANONICAL}{ZWNJ}مان",
        f"{CANONICAL}{ZWNJ}شان",
    ],
)
def test_attached_pronominal_clitics_match_the_exact_canonical(written: str) -> None:
    text = f"او {written} را نفهمید.\n"
    assert contains_canonical_lexeme(text, CANONICAL, target_language="fa")
    assert count_canonical_lexemes(text, CANONICAL, target_language="fa") == 1


@pytest.mark.parametrize(
    "written",
    [OTHER_CANONICAL, f"{OTHER_CANONICAL}ش", f"{OTHER_CANONICAL}شان", f"{OTHER_CANONICAL}تان"],
)
def test_the_clitic_rule_is_generic_rather_than_bound_to_one_term(written: str) -> None:
    assert contains_canonical_lexeme(f"{written} رفت.\n", OTHER_CANONICAL, target_language="fa")


@pytest.mark.parametrize(
    "written",
    [
        # The canonical followed by a clitic-shaped sequence that keeps going is another word.
        f"{CANONICAL}شگاه",
        f"{CANONICAL}مانند",
        f"{CANONICAL}شانی",
        f"{CANONICAL}تر",
        # Letters that are not a contracted clitic are a different spelling.
        f"{CANONICAL}ر",
        f"{CANONICAL}ن",
        f"{CANONICAL}ا",
        f"{CANONICAL}ها",
        # The canonical as the tail of a longer token is not the canonical either.
        f"نا{CANONICAL}",
        f"نا{CANONICAL}ش",
        f"ب{CANONICAL}شان",
    ],
)
def test_the_canonical_as_an_accidental_substring_of_another_token_never_matches(
    written: str,
) -> None:
    text = f"او {written} را نفهمید.\n"
    assert not contains_canonical_lexeme(text, CANONICAL, target_language="fa")
    assert count_canonical_lexemes(text, CANONICAL, target_language="fa") == 0


def test_clitics_are_a_persian_rule_only() -> None:
    assert not contains_canonical_lexeme("the Warders", "Warder", target_language="en")
    assert not contains_canonical_lexeme("Wardersh", "Warder", target_language="en")
    assert contains_canonical_lexeme("the Warder.", "Warder", target_language="en")


def test_counting_sees_every_clitic_attached_realization() -> None:
    text = f"{CANONICAL}ش و {CANONICAL}شان و {CANONICAL} و نا{CANONICAL}.\n"
    assert count_canonical_lexemes(text, CANONICAL, target_language="fa") == 3


@pytest.mark.parametrize(
    "translation",
    [
        f"او حتی نام یا {CANONICAL}ش را نفهمید.\n",
        f"او حتی نام یا {CANONICAL}{ZWNJ}ش را نفهمید.\n",
        f"آنها {CANONICAL}مان را شناختند.\n",
        f"آنها {CANONICAL}شان را پنهان کردند.\n",
        f"او {CANONICAL} را شناخت.\n",
    ],
)
def test_locked_exact_decision_accepts_a_possessive_clitic(translation: str) -> None:
    registry = _locked_registry("Order", CANONICAL)
    candidate = registry.candidates[0]
    assert resolve_enforcement(candidate, registry.decisions[0]) is TerminologyEnforcement.EXACT

    report = validate_terminology("She named her Order.\n", translation, registry)

    assert report.valid
    assert "canonical_term_missing" not in {item.code for item in report.findings}


@pytest.mark.parametrize(
    "translation",
    [
        f"او حتی نام یا {CANONICAL}شگاه را نفهمید.\n",
        f"او حتی نام یا نا{CANONICAL}ش را نفهمید.\n",
        "او حتی نام را نفهمید.\n",
    ],
)
def test_locked_exact_decision_still_rejects_what_is_not_the_canonical(translation: str) -> None:
    registry = _locked_registry("Order", CANONICAL)

    report = validate_terminology("She named her Order.\n", translation, registry)

    assert not report.valid
    assert "canonical_term_missing" in {item.code for item in report.findings}


def test_a_variant_that_is_the_canonical_with_a_clitic_is_not_a_rival_rendering() -> None:
    assert carries_canonical_identity(CANONICAL, f"{CANONICAL}شان", target_language="fa")
    assert not carries_canonical_identity(CANONICAL, f"{CANONICAL}شگاه", target_language="fa")
