"""Terminology enforcement modes: what target evidence proves a decision was honored."""

import pytest

from book_translator.models import (
    DecisionSource,
    DecisionState,
    TerminologyEnforcement,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.terminology import (
    TerminologyError,
    derivational_relatives,
    resolve_enforcement,
    set_decision,
    validate_terminology,
    validate_translation_qa,
)


def registry() -> TerminologyRegistry:
    return TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )


def _resolved(current: TerminologyRegistry, source_term: str) -> TerminologyEnforcement:
    decisions = {item.candidate_id: item for item in current.decisions}
    candidate = next(item for item in current.candidates if item.source_term == source_term)
    return resolve_enforcement(candidate, decisions[candidate.candidate_id])


def _codes(current: TerminologyRegistry, source: str, translation: str) -> set[str]:
    return {item.code for item in validate_terminology(source, translation, current).findings}


def demonym_registry(
    *,
    place: tuple[str, str] | None = ("Veyrastan", "ویراستان"),
    decision_source: DecisionSource = DecisionSource.AI,
    locked: bool = False,
    enforcement: TerminologyEnforcement | None = None,
) -> TerminologyRegistry:
    """A synthetic nation whose demonym and place name are both canonical terminology."""
    current = set_decision(
        registry(),
        source_term="Veyrastanin",
        target_term="ویراستینی",
        classification=TerminologyType.PEOPLE,
        decision_source=decision_source,
        locked=locked,
        enforcement=enforcement,
    )
    if place is None:
        return current
    return set_decision(
        current,
        source_term=place[0],
        target_term=place[1],
        classification=TerminologyType.PLACE,
        decision_source=DecisionSource.AI,
        locked=False,
    )


# --- exactness that must not be weakened ------------------------------------------------


def test_locked_exact_canonical_term_missing_is_still_an_error() -> None:
    current = set_decision(
        registry(),
        source_term="Ajah",
        target_term="آجای",
        classification=TerminologyType.ORGANIZATION,
        decision_source=DecisionSource.USER,
        locked=True,
    )

    assert _resolved(current, "Ajah") is TerminologyEnforcement.EXACT
    report = validate_terminology("The Ajah gathered.\n", "گروه گرد آمد.\n", current)
    assert {item.code for item in report.findings} == {"canonical_term_missing"}
    assert not report.valid


def test_locked_exact_canonical_target_present_is_valid_and_byte_identical() -> None:
    current = set_decision(
        registry(),
        source_term="Ajah",
        target_term="آجای",
        classification=TerminologyType.ORGANIZATION,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    translation = "آجای گرد آمد.\n"

    report = validate_terminology("The Ajah gathered.\n", translation, current)

    assert report.valid
    assert not report.findings
    assert translation == "آجای گرد آمد.\n"
    assert current.decisions[0].target_term == "آجای"


def test_a_locked_decision_cannot_be_given_a_relaxed_enforcement_mode() -> None:
    with pytest.raises(TerminologyError, match="must be enforced exactly"):
        set_decision(
            registry(),
            source_term="Ajah",
            target_term="آجای",
            classification=TerminologyType.ORGANIZATION,
            decision_source=DecisionSource.USER,
            locked=True,
            enforcement=TerminologyEnforcement.CONTEXTUAL,
        )


def test_a_locked_demonym_stays_exact_despite_its_classification() -> None:
    """Ownership outranks classification: a human's demonym choice is an instruction."""
    current = demonym_registry(decision_source=DecisionSource.USER, locked=True)

    assert _resolved(current, "Veyrastanin") is TerminologyEnforcement.EXACT
    assert _codes(current, "The Veyrastanin throne stood.\n", "تخت ویراستان بود.\n") == {
        "canonical_term_missing"
    }


def test_provisional_person_enforcement_is_unchanged_by_the_enforcement_model() -> None:
    current = set_decision(
        registry(),
        source_term="Aunt Veyra",
        target_term="خاله ویرا",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.AI,
        locked=False,
        decision_state=DecisionState.PROVISIONAL,
    )

    assert _resolved(current, "Aunt Veyra") is TerminologyEnforcement.EXACT
    present = validate_terminology("Aunt Veyra spoke.\n", "خاله ویرا صحبت کرد.\n", current)
    assert present.valid
    assert {item.code for item in present.findings} == {"provisional_decision_enforced"}
    assert present.findings[0].enforcement is TerminologyEnforcement.EXACT

    missing = validate_terminology("Aunt Veyra spoke.\n", "او صحبت کرد.\n", current)
    assert not missing.valid
    assert {item.code for item in missing.findings} == {
        "provisional_decision_enforced",
        "canonical_term_missing",
    }


@pytest.mark.parametrize(
    ("classification", "source_term", "target_term", "source", "translation"),
    [
        (TerminologyType.PERSON, "Veyra", "ویرا", "Veyra arrived.\n", "کسی آمد.\n"),
        (TerminologyType.PLACE, "Veyrahold", "ویراهولد", "He left Veyrahold.\n", "او رفت.\n"),
        (
            TerminologyType.HISTORICAL_EVENT,
            "Veyran War",
            "جنگ ویران",
            "The Veyran War ended.\n",
            "آن نبرد پایان یافت.\n",
        ),
        (
            TerminologyType.ARTIFACT,
            "silver arrow",
            "تیر نقره‌ای",
            "the silver arrow gleamed.\n",
            "آن چیز درخشید.\n",
        ),
    ],
)
def test_genuine_omission_still_fails_across_exact_classifications(
    classification: TerminologyType,
    source_term: str,
    target_term: str,
    source: str,
    translation: str,
) -> None:
    current = set_decision(
        registry(),
        source_term=source_term,
        target_term=target_term,
        classification=classification,
        decision_source=DecisionSource.AI,
        locked=False,
    )

    assert _resolved(current, source_term) is TerminologyEnforcement.EXACT
    assert _codes(current, source, translation) == {"canonical_term_missing"}


# --- morphological demonyms -------------------------------------------------------------


def test_demonym_rendered_through_its_place_name_is_canonical_identity() -> None:
    """ "Saldaean army" naturally becomes "army of Saldaea"; the place name is the evidence."""
    current = demonym_registry()

    assert _resolved(current, "Veyrastanin") is TerminologyEnforcement.MORPHOLOGICAL
    relatives = derivational_relatives(
        current, next(item for item in current.candidates if item.source_term == "Veyrastanin")
    )
    assert [item.target_term for item in relatives] == ["ویراستان"]
    report = validate_terminology(
        "the Veyrastanin army marched.\n", "سپاه ویراستان رفت.\n", current
    )
    assert report.valid
    assert not report.findings


def test_adjectival_demonym_may_become_the_place_name_inside_an_ezafe_construction() -> None:
    current = set_decision(
        set_decision(
            registry(),
            source_term="Veyrastanin",
            target_term="ویراستانی",
            classification=TerminologyType.PEOPLE,
            decision_source=DecisionSource.AI,
            locked=False,
        ),
        source_term="Veyrastan",
        target_term="ویراستان",
        classification=TerminologyType.PLACE,
        decision_source=DecisionSource.AI,
        locked=False,
    )

    report = validate_terminology(
        "claim to the Veyrastanin throne.\n", "ادعایش بر تخت ویراستان.\n", current
    )

    assert report.valid
    assert not report.findings


def test_a_demonym_without_a_canonical_root_still_requires_its_own_lexical_identity() -> None:
    current = demonym_registry(place=None)

    assert _resolved(current, "Veyrastanin") is TerminologyEnforcement.MORPHOLOGICAL
    assert _codes(current, "the Veyrastanin army marched.\n", "سپاه بیگانه رفت.\n") == {
        "canonical_term_missing"
    }


def test_morphological_matching_accepts_the_canonical_target_derivational_stem() -> None:
    current = set_decision(
        registry(),
        source_term="Veyrastani",
        target_term="ویراستانی‌ها",
        classification=TerminologyType.PEOPLE,
        decision_source=DecisionSource.AI,
        locked=False,
    )

    assert validate_terminology("The Veyrastani rode.\n", "ویراستانی تاخت.\n", current).valid


def test_an_unrelated_place_decision_is_not_evidence_for_a_demonym() -> None:
    current = demonym_registry(place=("Tarmonhold", "تارمون‌هولد"))

    assert _codes(current, "the Veyrastanin army marched.\n", "سپاه تارمون‌هولد رفت.\n") == {
        "canonical_term_missing"
    }


# --- contextual and advisory ------------------------------------------------------------


def test_contextual_enforcement_downgrades_absence_to_a_nonblocking_warning() -> None:
    """A verbal event phrase survives as a paraphrase, so absence is review evidence."""
    current = set_decision(
        registry(),
        source_term="took the Stone",
        target_term="فتح سنگ",
        classification=TerminologyType.HISTORICAL_EVENT,
        decision_source=DecisionSource.AI,
        locked=False,
        enforcement=TerminologyEnforcement.CONTEXTUAL,
    )

    report = validate_terminology(
        "the man who took the Stone of Veyr.\n", "همان که سنگ ویر را فتح کرد.\n", current
    )

    assert report.valid
    assert [item.code for item in report.findings] == ["canonical_term_unverified"]
    assert report.findings[0].severity == "warning"
    assert report.findings[0].enforcement is TerminologyEnforcement.CONTEXTUAL


def test_an_unenforced_decision_resolves_to_advisory_and_reports_nothing() -> None:
    current = set_decision(
        registry(),
        source_term="the Stone",
        target_term="سنگ",
        classification=TerminologyType.PLACE,
        decision_source=DecisionSource.AI,
        locked=False,
        context_sensitive=True,
    )
    relaxed = current.model_copy(
        update={
            "candidates": [current.candidates[0].model_copy(update={"context_sensitive": False})]
        }
    )

    assert not relaxed.decisions[0].required
    assert _resolved(relaxed, "the Stone") is TerminologyEnforcement.ADVISORY
    assert validate_terminology("he held the Stone.\n", "او آن را داشت.\n", relaxed).valid


def test_an_explicit_mode_overrides_the_derived_policy_in_both_directions() -> None:
    strict = demonym_registry(enforcement=TerminologyEnforcement.EXACT)
    assert _resolved(strict, "Veyrastanin") is TerminologyEnforcement.EXACT
    assert _codes(strict, "the Veyrastanin army marched.\n", "سپاه ویراستان رفت.\n") == {
        "canonical_term_missing"
    }

    advisory = demonym_registry(enforcement=TerminologyEnforcement.ADVISORY)
    assert _resolved(advisory, "Veyrastanin") is TerminologyEnforcement.ADVISORY
    assert validate_terminology("the Veyrastanin army marched.\n", "سپاهی رفت.\n", advisory).valid


# --- interactions that must stay intact -------------------------------------------------


@pytest.mark.parametrize(
    "translation",
    ["ویراستینی‌ها آمدند.\n", "ویراستینی‌های شمال آمدند.\n", "ویراستینی‌تر شد.\n"],
)
def test_persian_zwnj_morphology_remains_supported_under_relaxed_enforcement(
    translation: str,
) -> None:
    current = demonym_registry(place=None)

    assert validate_terminology("The Veyrastanin rode.\n", translation, current).valid


def test_a_competing_variant_is_still_an_error_under_morphological_enforcement() -> None:
    current = set_decision(
        registry(),
        source_term="Veyrastanin",
        target_term="ویراستینی",
        classification=TerminologyType.PEOPLE,
        decision_source=DecisionSource.AI,
        locked=False,
        competing_variants=("ویرستونی",),
    )

    # Two governed source occurrences and one canonical rendering: the block is one realization
    # short, so the rival standing in it is what rendered the occurrence that is missing.
    assert _codes(
        current,
        "The Veyrastanin rode. The Veyrastanin sang.\n",
        "ویراستینی سوار شد. ویرستونی آواز خواند.\n",
    ) == {"competing_variant"}


def test_structural_paragraph_mismatch_stays_independent_of_terminology_enforcement() -> None:
    current = demonym_registry()
    source = "the Veyrastanin army marched.\n\nIt halted.\n"
    translation = "سپاه ویراستان رفت.\n\nایستاد.\n\nو ماند.\n"

    terminology = validate_terminology(source, translation, current)
    structural = validate_translation_qa(source, translation, target_language="fa")

    assert terminology.valid
    assert structural.valid is False
    assert [item.code for item in structural.findings] == ["paragraph_count_mismatch"]
    assert structural.source_paragraph_count == 2
    assert structural.translation_paragraph_count == 3
