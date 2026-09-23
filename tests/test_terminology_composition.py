"""Coordinate-elided canonical identity and the read-only exact-enforcement audit.

Every fixture is synthetic prose built around canonical terminology strings; no book content is
committed here.  The two halves of the file mirror the policy boundary the pipeline depends on:
a valid canonical term realized through normal target grammar is a QA question, while a canonical
decision that contradicts itself or names a clause is a registry question.
"""

import pytest

from book_translator.models import (
    DecisionSource,
    DecisionState,
    TerminologyEnforcement,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.terminology import (
    ExactnessRisk,
    audit_exact_enforcement,
    canonical_registry_bytes,
    contains_coordinated_lexeme,
    coordinated_occurrences,
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


def _codes(current: TerminologyRegistry, source: str, translation: str) -> set[str]:
    return {item.code for item in validate_terminology(source, translation, current).findings}


def title_registry(
    *,
    lord_source: DecisionSource = DecisionSource.AI,
    locked: bool = False,
    with_lady: bool = True,
) -> TerminologyRegistry:
    """A coordinated title pair: both conjuncts are canonical terminology in their own right."""
    current = set_decision(
        registry(),
        source_term="High Lord",
        target_term="لرد اعظم",
        classification=TerminologyType.TITLE,
        decision_source=lord_source,
        locked=locked,
    )
    if not with_lady:
        return current
    return set_decision(
        current,
        source_term="High Lady",
        target_term="بانوی عالی‌مقام",
        classification=TerminologyType.TITLE,
        decision_source=DecisionSource.AI,
        locked=False,
    )


# --- coordinate ellipsis: valid canonical term met by normal target grammar ---------------


def test_coordinated_title_is_detected_only_when_the_registry_attests_both_conjuncts() -> None:
    current = title_registry()
    source = "A High Lord or Lady may speak.\n"

    occurrences = coordinated_occurrences(current, source)

    assert [(item.source_term, item.conjunct_source_term) for item in occurrences] == [
        ("High Lord", "High Lady")
    ]
    assert coordinated_occurrences(title_registry(with_lady=False), source) == ()


def test_shared_modifier_coordination_satisfies_the_canonical_title() -> None:
    current = title_registry()

    report = validate_terminology(
        "A High Lord or Lady may speak.\n", "لرد یا بانوی اعظم می‌تواند سخن بگوید.\n", current
    )

    assert report.findings == []
    assert report.valid


def test_missing_title_head_still_fails_inside_a_coordination() -> None:
    current = title_registry()

    codes = _codes(current, "A High Lord or Lady may speak.\n", "بانوی اعظم می‌تواند سخن بگوید.\n")

    assert codes == {"canonical_term_missing"}


def test_missing_title_modifier_still_fails_inside_a_coordination() -> None:
    current = title_registry()

    codes = _codes(current, "A High Lord or Lady may speak.\n", "لرد یا بانو می‌تواند سخن بگوید.\n")

    assert codes == {"canonical_term_missing"}


def test_uncoordinated_source_keeps_demanding_the_contiguous_canonical_target() -> None:
    current = title_registry()

    codes = _codes(current, "The High Lord waited.\n", "لرد یا بانوی اعظم منتظر ماند.\n")

    assert codes == {"canonical_term_missing"}


def test_locked_title_stays_strict_through_a_coordination() -> None:
    current = title_registry(lord_source=DecisionSource.USER, locked=True)

    codes = _codes(current, "A High Lord or Lady may speak.\n", "لرد یا بانوی اعظم سخن گفت.\n")

    assert codes == {"canonical_term_missing"}


@pytest.mark.parametrize(
    ("translation", "expected"),
    [
        ("لرد یا بانوی اعظم", True),
        ("لرد و بانوی اعظم", True),
        ("لرد اعظم", False),
        ("اعظم و لرد", False),
        ("لرد بانوی اعظم", False),
        ("لرد\nیا بانوی اعظم", False),
    ],
)
def test_coordinated_lexeme_requires_ordered_words_and_a_target_coordinator(
    translation: str, expected: bool
) -> None:
    assert contains_coordinated_lexeme(translation, "لرد اعظم", target_language="fa") is expected


def test_coordinated_lexeme_is_a_persian_rule_only() -> None:
    assert not contains_coordinated_lexeme("High or Great Lord", "High Lord", target_language="en")


# --- exactness that must not be weakened -------------------------------------------------


def test_locked_organization_remains_byte_exact() -> None:
    current = set_decision(
        registry(),
        source_term="Ajah",
        target_term="آجای",
        classification=TerminologyType.ORGANIZATION,
        decision_source=DecisionSource.USER,
        locked=True,
    )

    assert _codes(current, "The Ajah gathered.\n", "گروه گرد آمد.\n") == {"canonical_term_missing"}
    assert _codes(current, "The Ajah gathered.\n", "آجای گرد آمد.\n") == set()


def test_provisional_person_stays_exact_and_reports_its_provenance() -> None:
    current = set_decision(
        registry(),
        source_term="Aunt Resara",
        target_term="خاله رسارا",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.AI,
        locked=False,
        decision_state=DecisionState.PROVISIONAL,
    )

    honored = validate_terminology("Aunt Resara arrived.\n", "خاله رسارا رسید.\n", current)
    missing = _codes(current, "Aunt Resara arrived.\n", "عمه رسارا رسید.\n")

    assert {item.code for item in honored.findings} == {"provisional_decision_enforced"}
    assert honored.valid
    assert missing == {"provisional_decision_enforced", "canonical_term_missing"}


def test_named_artifact_still_requires_its_canonical_identity() -> None:
    current = set_decision(
        registry(),
        source_term="Horn of Valere",
        target_term="بوق والر",
        classification=TerminologyType.ARTIFACT,
        decision_source=DecisionSource.AI,
        locked=False,
    )

    assert _codes(current, "The Horn of Valere sounded.\n", "شیپور والر نواخته شد.\n") == {
        "canonical_term_missing"
    }
    assert _codes(current, "The Horn of Valere sounded.\n", "بوق والر نواخته شد.\n") == set()


def test_descriptive_artifact_modelled_contextually_reports_a_warning_not_a_defect() -> None:
    """Registry remediation, not a looser matcher, is what stops a description failing QA."""
    current = set_decision(
        registry(),
        source_term="twisted redstone doorframe",
        target_term="چارچوب در سنگ سرخ پیچ‌خورده",
        classification=TerminologyType.ARTIFACT,
        decision_source=DecisionSource.AI,
        locked=False,
        enforcement=TerminologyEnforcement.CONTEXTUAL,
    )

    report = validate_terminology(
        "A twisted redstone doorframe stood there.\n",
        "چارچوب دری از سنگ سرخِ عجیب پیچ‌خورده آنجا بود.\n",
        current,
    )

    assert {item.code for item in report.findings} == {"canonical_term_unverified"}
    assert report.valid


def test_event_verb_phrase_modelled_advisory_stops_failing_qa() -> None:
    current = set_decision(
        registry(),
        source_term="took the Stone",
        target_term="فتح سنگ",
        classification=TerminologyType.HISTORICAL_EVENT,
        decision_source=DecisionSource.AI,
        locked=False,
        enforcement=TerminologyEnforcement.ADVISORY,
    )

    report = validate_terminology(
        "He took the Stone that year.\n", "او در آن سال سنگ را گرفت.\n", current
    )

    assert report.findings == []
    assert report.valid


# --- read-only registry audit -------------------------------------------------------------


def gareth_registry(lord_gareth_target: str) -> TerminologyRegistry:
    """Two canonical spellings of one personal name, one contained in the other."""
    current = set_decision(
        registry(),
        source_term="Lord Gareth Bryne",
        target_term="لرد گرث براین",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.AI,
        locked=False,
    )
    return set_decision(
        current,
        source_term="Lord Gareth",
        target_term=lord_gareth_target,
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.AI,
        locked=False,
    )


def _risks(current: TerminologyRegistry, source_term: str) -> set[ExactnessRisk]:
    return {
        item.risk
        for item in audit_exact_enforcement(current).findings
        if item.source_term == source_term
    }


def test_inconsistent_personal_name_spelling_is_reported_as_a_containment_conflict() -> None:
    inconsistent = audit_exact_enforcement(gareth_registry("لرد گرت"))

    conflicts = [
        item
        for item in inconsistent.findings
        if item.risk is ExactnessRisk.CONTAINMENT_INCONSISTENCY
    ]
    assert [(item.source_term, item.related_source_terms) for item in conflicts] == [
        ("Lord Gareth", ("Lord Gareth Bryne",))
    ]


def test_one_canonical_spelling_of_a_name_leaves_only_the_fragment_finding() -> None:
    assert _risks(gareth_registry("لرد گرث"), "Lord Gareth") == {ExactnessRisk.SOURCE_FRAGMENT}


def test_ezafe_and_plural_morphology_are_not_a_canonical_contradiction() -> None:
    current = set_decision(
        registry(),
        source_term="Maiden of the Spear",
        target_term="دوشیزهٔ نیزه",
        classification=TerminologyType.TITLE,
        decision_source=DecisionSource.AI,
        locked=False,
    )
    current = set_decision(
        current,
        source_term="Maiden",
        target_term="دوشیزه",
        classification=TerminologyType.TITLE,
        decision_source=DecisionSource.AI,
        locked=False,
    )

    assert ExactnessRisk.CONTAINMENT_INCONSISTENCY not in _risks(current, "Maiden")


def test_finite_verb_phrase_is_reported_and_an_attributive_participle_is_not() -> None:
    current = set_decision(
        registry(),
        source_term="took the Stone",
        target_term="فتح سنگ",
        classification=TerminologyType.HISTORICAL_EVENT,
        decision_source=DecisionSource.AI,
        locked=False,
    )
    current = set_decision(
        current,
        source_term="twisted redstone doorframe",
        target_term="چارچوب در سنگ سرخ پیچ‌خورده",
        classification=TerminologyType.ARTIFACT,
        decision_source=DecisionSource.AI,
        locked=False,
    )

    assert _risks(current, "took the Stone") == {ExactnessRisk.VERB_PHRASE}
    assert _risks(current, "twisted redstone doorframe") == {ExactnessRisk.DESCRIPTIVE_PHRASE}


def test_coordinated_source_form_is_reported_as_an_unsafe_exact_candidate() -> None:
    current = set_decision(
        registry(),
        source_term="High Lords and Ladies",
        target_term="لردهای اعظم و بانوان اعظم",
        classification=TerminologyType.TITLE,
        decision_source=DecisionSource.AI,
        locked=False,
    )

    assert ExactnessRisk.COORDINATED_SOURCE in _risks(current, "High Lords and Ladies")


def test_audit_census_counts_exact_multiword_and_locked_decisions() -> None:
    current = set_decision(
        title_registry(),
        source_term="Ajah",
        target_term="آجای",
        classification=TerminologyType.ORGANIZATION,
        decision_source=DecisionSource.USER,
        locked=True,
    )

    report = audit_exact_enforcement(current)

    assert report.total_decisions == 3
    assert report.exact_decisions == 3
    assert report.multiword_exact_decisions == 2
    assert report.locked_exact_decisions == 1


def test_audit_and_validation_leave_the_registry_untouched() -> None:
    current = gareth_registry("لرد گرت")
    before = canonical_registry_bytes(current)

    audit_exact_enforcement(current)
    validate_terminology("Lord Gareth waited.\n", "لرد گرث منتظر ماند.\n", current)

    assert canonical_registry_bytes(current) == before


# --- structural QA stays independent of terminology ---------------------------------------


def test_structural_qa_is_unaffected_by_a_coordinated_terminology_match() -> None:
    report = validate_translation_qa(
        "A High Lord or Lady may speak.\n\nThey did.\n",
        "لرد یا بانوی اعظم می‌تواند سخن بگوید.\n",
        target_language="fa",
    )

    assert {item.code for item in report.findings} == {"paragraph_count_mismatch"}
    assert not report.valid
