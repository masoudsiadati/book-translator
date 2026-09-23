"""Grammatical Persian realizations of a canonical target, and which source spellings govern it
(terminology QA 1.10.0 and 1.11.0).

Two matcher defects rejected valid translations. On the target side, exact enforcement recognized
the canonical spelling with contracted inflections only, so a regular plural (``داستان‌سرایان``) or
a plural on the head of a phrase (``فرزندان نور``) counted as a missing canonical. Since 1.11.0 an
authorized compound verb also conjugates in either voice (``خنثی می‌کردند``), and a plural
canonical's singular is accepted where the decision itself records it (``مطرود`` of
``مطرودین``). On the source
side, the validator governed an alias only when it equalled the source term after case and article
folding, so the English plural ``Warders`` of ``Warder`` was prompted as terminology but never
checked.

Every rule here is generic and is exercised through several synthetic terms. The negative cases
matter as much as the positive ones: a realization always begins with the authorized spelling, so
an unrelated synonym, a rival spelling, and an alias that is a different source expression all stay
exactly as they were. The Persian words are generic vocabulary or glossary terms, not book prose.
"""

import pytest

from book_translator.models import (
    DecisionSource,
    TerminologyEnforcement,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.terminology import set_decision, validate_terminology
from book_translator.terminology.lexical import (
    contains_canonical_lexeme,
    contains_canonical_realization,
    count_canonical_realizations,
    enforceable_competing_variants,
    is_singular_of,
    recorded_singulars,
)
from book_translator.terminology.registry import (
    applicable_decisions,
    canonically_matched_decisions,
    governing_source_matches,
    inflectional_key,
)

ZWNJ = "‌"
KASRA = "ِ"


def _empty() -> TerminologyRegistry:
    return TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )


def _registry(
    source_term: str,
    target_term: str,
    *,
    aliases: tuple[str, ...] = (),
    variants: tuple[str, ...] = (),
    forms: tuple[str, ...] = (),
    registry: TerminologyRegistry | None = None,
) -> TerminologyRegistry:
    return set_decision(
        registry or _empty(),
        source_term=source_term,
        target_term=target_term,
        classification=TerminologyType.STORY_TERM,
        decision_source=DecisionSource.USER,
        locked=True,
        aliases=aliases,
        competing_variants=variants,
        enforcement=TerminologyEnforcement.EXACT,
        allowed_target_forms=forms,
    )


def _codes(source: str, translation: str, registry: TerminologyRegistry) -> set[str]:
    return {item.code for item in validate_terminology(source, translation, registry).findings}


def _realized(written: str, *forms: str) -> bool:
    return contains_canonical_realization(f"او {written} را دید.\n", forms, target_language="fa")


# --------------------------------------------------------------------------------------------
# 1-4. The canonical and its regular surface morphology
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("canonical", ["داستان‌سرا", "نگاهبان", "فرزند نور", "کارانا"])
def test_the_singular_canonical_is_still_its_own_realization(canonical: str) -> None:
    assert _realized(canonical, canonical)


@pytest.mark.parametrize(
    ("canonical", "written"),
    [
        # Attached plural after a consonant, and the hiatus-glide plural after a long vowel.
        ("نگاهبان", "نگاهبانان"),
        ("خردمند", "خردمندان"),
        ("داستان‌سرا", "داستان‌سرایان"),
        ("تورو", "تورویان"),
        # A yeh-final stem takes the plain suffix.
        ("آیزسدای", "آیزسدایان"),
        # The ZWNJ plural, unchanged.
        ("نگاهبان", f"نگاهبان{ZWNJ}ها"),
        # A phrase pluralizes its head, with or without a written ezafe.
        ("فرزند نور", "فرزندان نور"),
        ("فرزند نور", f"فرزندان{KASRA} نور"),
        ("فرزند نور", f"فرزند{ZWNJ}های نور"),
    ],
)
def test_a_regular_persian_plural_realizes_the_canonical(canonical: str, written: str) -> None:
    assert _realized(written, canonical)


@pytest.mark.parametrize(
    ("canonical", "written"),
    [
        ("داستان‌سرا", "داستان‌سرایی"),
        ("نگاهبان", "نگاهبانی"),
        ("نگاهبان", "نگاهبانانی"),
        ("فرزند نور", "فرزند نوری"),
    ],
)
def test_the_indefinite_realizes_the_canonical(canonical: str, written: str) -> None:
    assert _realized(written, canonical)


@pytest.mark.parametrize(
    ("canonical", "written"),
    [
        ("نگاهبان", "نگاهبانش"),
        ("نگاهبان", "نگاهبانانشان"),
        ("داستان‌سرا", "داستان‌سرایانش"),
        ("داستان‌سرا", "داستان‌سرای"),
        ("رند ال ثور", f"رند{KASRA} ال ثور"),
        ("فرزند نور", "فرزندان نورش"),
    ],
)
def test_clitics_and_ezafe_realize_the_canonical(canonical: str, written: str) -> None:
    assert _realized(written, canonical)


@pytest.mark.parametrize(
    "written",
    [
        "خنثی شد",
        "خنثی شدند",
        "خنثی شود",
        "خنثی کرد",
        f"خنثی می{ZWNJ}کردند",
        f"خنثی نمی{ZWNJ}شد",
        f"خنثی{ZWNJ}اش کرده",
        f"خنثی{ZWNJ}شدن",
        "خنثی شده",
    ],
)
def test_an_authorized_compound_verb_realizes_in_every_conjugation(written: str) -> None:
    assert _realized(written, f"خنثی{ZWNJ}شده", "خنثی شدن", "خنثی شده")


def test_a_compound_verb_keeps_only_the_separators_its_decision_uses() -> None:
    # Only the ZWNJ spelling is authorized, so the spaced conjugation is a different spelling.
    assert _realized(f"خنثی{ZWNJ}شد", f"خنثی{ZWNJ}شده")
    assert not _realized("خنثی شد", f"خنثی{ZWNJ}شده")


def _singular(written: str) -> bool:
    return contains_canonical_realization(
        f"او {written} را دید.\n", ("مطرودین",), target_language="fa", singulars=("مطرود",)
    )


@pytest.mark.parametrize("written", ["مطرود", "مطرودی", "مطرودش", f"مطرود{ZWNJ}ای"])
def test_a_recorded_singular_realizes_a_plural_canonical(written: str) -> None:
    assert _singular(written)


@pytest.mark.parametrize("written", ["مطرودان", "مطرودها", "مطرودیان", "نامطرود"])
def test_the_recorded_singular_never_takes_a_rival_plural(written: str) -> None:
    assert not _singular(written)


@pytest.mark.parametrize(
    ("canonical", "variant", "singular"),
    [
        ("مطرودین", "مطرود", True),
        ("نگاهبانان", "نگاهبان", True),
        (f"کاپار{ZWNJ}ها", "کاپار", True),
        ("مطرودین", "مطرودان", False),
        # A transliterated name that happens to end in the letters of a plural suffix is only
        # affected when its registry records that exact shorter spelling, which Saidin's does not.
        ("ساییدین", "سایدین", False),
    ],
)
def test_the_singular_is_proven_by_the_record_not_guessed(
    canonical: str, variant: str, singular: bool
) -> None:
    assert is_singular_of(canonical, variant) is singular
    assert recorded_singulars((canonical,), (variant,)) == ((variant,) if singular else ())


@pytest.mark.parametrize(
    ("canonical", "written"),
    [
        # The element alone, or with a verb that is not its light verb, is not the compound.
        (f"خنثی{ZWNJ}شده", "خنثی"),
        (f"خنثی{ZWNJ}شده", "خنثی بود"),
        (f"خنثی{ZWNJ}شده", f"خنثی{ZWNJ}سازی"),
        # A plural on the final word of a phrase is not Persian, and neither is a longer token.
        ("فرزندان نور", "فرزند نورها"),
        ("فرزند نور", "فرزند نورها"),
        ("نگاهبان", "نگاهبانانه"),
        ("نگاهبان", "نانگاهبان"),
        # A vowel-final stem never takes the bare suffix, a heh-final stem never takes either.
        ("داستان‌سرا", "داستان‌سراان"),
        ("ماریه", "ماریهان"),
        # The ZWNJ-free plural stays outside policy, as pinned since 1.7.0.
        ("تورو", "توروها"),
        # The singular of a plural canonical is not derived from the surface.
        ("مطرودین", "مطرود"),
        ("مطرودین", "مطرودان"),
    ],
)
def test_morphology_never_becomes_a_different_word(canonical: str, written: str) -> None:
    assert not _realized(written, canonical)


def test_counting_sees_each_realization_once() -> None:
    text = "نگاهبانان و نگاهبان و فرزندان نور و نگاهبانانه.\n"
    assert count_canonical_realizations(text, ("نگاهبان",), target_language="fa") == 2
    assert count_canonical_realizations(text, ("فرزند نور",), target_language="fa") == 1


def test_the_morphology_is_persian_only() -> None:
    assert not contains_canonical_realization("Wardersan", ("Warders",), target_language="en")
    assert contains_canonical_realization("the Warders", ("Warders",), target_language="en")


# --------------------------------------------------------------------------------------------
# 5. Which source spellings govern
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("alias", "source_term"),
    [
        ("Warders", "Warder"),
        ("Halfmen", "Halfman"),
        ("false Dragons", "false Dragon"),
        ("Aes Sedai’s", "Aes Sedai"),
        ("Wise Ones", "Wise One"),
        ("Dark One", "the Dark One"),
        ("stilled", "Stilled"),
    ],
)
def test_an_inflection_of_the_source_term_shares_its_key(alias: str, source_term: str) -> None:
    assert inflectional_key(alias) == inflectional_key(source_term)


@pytest.mark.parametrize(
    ("alias", "source_term"),
    [
        ("Gaidin", "Warder"),
        ("her old nurse", "Lini"),
        ("Mistress Tharne", "Duranda Tharne"),
        ("the Breaking", "Breaking of the World"),
        ("stilled", "stilling"),
        ("gentle", "gentling"),
    ],
)
def test_a_different_source_expression_never_shares_the_key(alias: str, source_term: str) -> None:
    assert inflectional_key(alias) != inflectional_key(source_term)


def test_an_inflectional_alias_the_prompt_selects_is_enforced_by_qa() -> None:
    registry = _registry("Warder", "نگاهبان", aliases=("Warders",))
    source = "Two Warders stood guard.\n"

    assert canonically_matched_decisions(registry, source) == applicable_decisions(registry, source)
    assert "canonical_term_missing" in _codes(source, "دو محافظ نگهبانی می‌دادند.\n", registry)
    assert _codes(source, "دو نگاهبان نگهبانی می‌دادند.\n", registry) == set()
    assert _codes(source, "نگاهبانان نگهبانی می‌دادند.\n", registry) == set()


def test_a_different_expression_alias_is_prompted_but_never_enforced() -> None:
    registry = _registry("Lini", "لینی", aliases=("her old nurse",), variants=("دایهٔ پیرش",))
    source = "Then her old nurse was waiting.\n"

    assert applicable_decisions(registry, source)
    assert canonically_matched_decisions(registry, source) == ()
    assert _codes(source, "دایهٔ پیرش منتظر بود.\n", registry) == set()


def test_qa_selection_is_always_within_prompt_selection() -> None:
    registry = _registry("Warder", "نگاهبان", aliases=("Warders", "Gaidin"))
    registry = _registry("Lini", "لینی", aliases=("her old nurse",), registry=registry)
    for source in ("Warders and Gaidin.\n", "her old nurse and a Warder.\n", "Gaidin.\n"):
        enforced = {
            item.candidate_id for item, _ in canonically_matched_decisions(registry, source)
        }
        prompted = {item.candidate_id for item, _ in applicable_decisions(registry, source)}
        assert enforced <= prompted


# --------------------------------------------------------------------------------------------
# 6-8. What must not change
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("written", ["نگهبان", "محافظ", "پاسدار", "نگهبانان"])
def test_an_unrelated_synonym_is_still_rejected(written: str) -> None:
    registry = _registry("Warder", "نگاهبان", aliases=("Warders",))
    translation = f"دو {written} ایستاده بودند.\n"
    assert "canonical_term_missing" in _codes("Two Warders stood.\n", translation, registry)


def test_a_competing_variant_is_still_detected() -> None:
    canonical, rival = "رند ال ثور", f"رند ال{ZWNJ}ثور"
    registry = _registry("Rand al’Thor", canonical, variants=(rival,))
    source = "Rand al’Thor rode on.\n"

    assert enforceable_competing_variants(canonical, (rival,), target_language="fa") == (rival,)
    assert _codes(source, f"{rival} پیش رفت.\n", registry) == {
        "competing_variant",
        "canonical_term_missing",
    }
    assert _codes(source, f"{canonical} پیش رفت.\n", registry) == set()


def test_a_variant_that_is_a_realization_of_the_canonical_is_not_a_rival() -> None:
    kept = enforceable_competing_variants("نگاهبان", ("نگاهبانان", "نگهبان"), target_language="fa")
    assert kept == ("نگهبان",)


def test_the_plural_of_a_rival_is_not_recognized_as_the_canonical() -> None:
    # Morphology widens the canonical only; the rival keeps its own contracted matching.
    assert not contains_canonical_lexeme("شنل‌سفیدان آمدند.\n", "شنل‌سفید", target_language="fa")
    assert not _realized("شنل‌سفیدان", "فرزندان نور")


def test_the_longer_overlapping_term_still_wins() -> None:
    registry = _registry("Children", "فرزندان")
    registry = _registry("Children of the Light", "فرزندان نور", registry=registry)
    source = "The Children of the Light rode in.\n"

    governing = governing_source_matches(registry, source)
    assert [match.term for match in governing] == ["Children of the Light"]
    assert [
        candidate.source_term for candidate, _ in canonically_matched_decisions(registry, source)
    ] == ["Children of the Light"]


# --------------------------------------------------------------------------------------------
# 9. The Book 6 false-rejection shapes, on synthetic sentences
# --------------------------------------------------------------------------------------------

GENTLED_FORMS = ("خنثی شدن", "خنثی شده")


@pytest.mark.parametrize(
    ("source", "translation"),
    [
        ("They caught and gentled men.\n", f"مردان را می{ZWNJ}گرفتند و خنثی می{ZWNJ}کردند.\n"),
        ("Before he can be gentled.\n", "پیش از آنکه خنثی شود.\n"),
        ("They had gentled him.\n", f"خنثی{ZWNJ}اش کرده بودند.\n"),
    ],
)
def test_a_conjugated_gentled_is_accepted(source: str, translation: str) -> None:
    registry = _registry("gentled", f"خنثی{ZWNJ}شده", forms=GENTLED_FORMS)
    assert validate_terminology(source, translation, registry).valid


def test_a_plural_gleeman_is_accepted() -> None:
    registry = _registry("gleeman", "داستان‌سرا")
    report = validate_terminology(
        "It was a gleeman’s tale.\n", "قصه‌ای از داستان‌سرایان بود.\n", registry
    )
    assert report.valid


def test_a_whitecloak_written_through_the_group_plural_is_accepted() -> None:
    registry = _registry("Whitecloak", "فرزند نور")
    report = validate_terminology(
        "A Whitecloak rode past.\n", "یکی از فرزندان نور گذشت.\n", registry
    )
    assert report.valid


def test_the_ungrammatical_final_word_plural_of_whitecloaks_stays_rejected() -> None:
    registry = _registry("Whitecloaks", "فرزندان نور")
    report = validate_terminology("That for Whitecloaks!\n", "این برای فرزند نورها!\n", registry)
    assert not report.valid


def test_a_single_forsaken_written_in_the_singular_is_accepted() -> None:
    registry = _registry("Forsaken", "مطرودین", variants=("مطرود", "مطرودان"))
    source = "One of the Forsaken. The Forsaken fled. No Forsaken remained.\n"
    translation = "یکی از مطرودین. آن مطرود گریخت. هیچ مطرودی نماند.\n"
    assert _codes(source, translation, registry) == set()
    assert _codes("The Forsaken fled.\n", "آن مطرود گریخت.\n", registry) == set()


def test_the_rival_plural_of_forsaken_is_still_a_competing_variant() -> None:
    registry = _registry("Forsaken", "مطرودین", variants=("مطرود", "مطرودان"))
    codes = _codes("The Forsaken fled.\n", "مطرودان گریختند.\n", registry)
    assert codes == {"competing_variant", "canonical_term_missing"}
