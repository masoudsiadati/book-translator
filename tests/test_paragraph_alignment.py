"""Deterministic paragraph alignment QA: measurements, policy, and the gates it feeds."""

from pathlib import Path

from book_translator.models import RepairFindingClass, TranslationAlignmentReport
from book_translator.paragraph_alignment import (
    ADJACENT_BOUNDARY_DRIFT_SUSPECTED,
    ALIGNMENT_NOT_EVALUABLE,
    ALIGNMENT_QA_VERSION,
    DIALOGUE_MARKS_ABSENT,
    EN_FA_V1,
    LEGACY_POSITIONAL,
    MARKED,
    NUMERAL_NOT_PRESERVED,
    PARAGRAPH_WORD_RATIO_OUT_OF_BAND,
    AlignmentAssessment,
    assess_alignment,
    build_alignment_report,
    measure_pair,
    policy_for,
)
from book_translator.translation_repair_policy import classify_alignment_finding, classify_findings

#: Synthetic filler. Word counts are what the policy reads, so the tokens themselves are inert.
SOURCE_WORD = "alpha"
TARGET_WORD = "آلفا"


def _source(words: int) -> str:
    return " ".join([SOURCE_WORD] * words) + "."


def _target(words: int) -> str:
    return " ".join([TARGET_WORD] * words) + "."


def _document(*blocks: str) -> str:
    return "\n\n".join(blocks) + "\n"


def _assess(source: str, target: str, *, mode: str = LEGACY_POSITIONAL) -> AlignmentAssessment:
    return assess_alignment(source, target, policy=EN_FA_V1, contract_mode=mode)


def _codes(source: str, target: str) -> list[str]:
    return [finding.code for finding in _assess(source, target).findings]


# --------------------------------------------------------------------------------------------
# Measurements
# --------------------------------------------------------------------------------------------


def test_every_contracted_measurement_is_taken_for_each_pair() -> None:
    metrics = measure_pair(1, 'He said "one two" now.', "او گفت «یک دو» حالا. باشد؟")

    assert metrics.source_words == 5
    assert metrics.target_words == 6
    assert metrics.source_characters and metrics.target_characters
    assert metrics.source_sentences == 1
    assert metrics.target_sentences == 2
    assert metrics.source_dialogue_marks == 2
    assert metrics.target_dialogue_marks == 2
    assert metrics.word_ratio == 6 / 5
    assert metrics.character_ratio > 0


def test_persian_digits_count_as_the_numbers_they_are() -> None:
    metrics = measure_pair(1, "He waited 12 days.", "او ۱۲ روز صبر کرد.")

    assert metrics.source_numerals == ("12",)
    assert metrics.target_numerals == ("12",)
    assert metrics.missing_numerals == ()


def test_word_counting_is_whitespace_separated_so_persian_compounds_stay_one_word() -> None:
    # A Unicode word scan would split this at the zero-width non-joiner and report two words,
    # which is what pushed a well-translated pilot paragraph over the band during calibration.
    assert measure_pair(1, "book", "کتاب‌ها").target_words == 1


# --------------------------------------------------------------------------------------------
# The ratio band
# --------------------------------------------------------------------------------------------


def test_a_ratio_inside_the_band_passes() -> None:
    assessment = _assess(_document(_source(40)), _document(_target(40)))

    assert assessment.evaluated
    assert assessment.valid
    assert assessment.findings == ()
    assert assessment.measured_pairs == 1


def test_a_ratio_below_the_band_on_a_long_paragraph_is_detected() -> None:
    codes = _codes(_document(_source(40)), _document(_target(20)))

    assert codes == [PARAGRAPH_WORD_RATIO_OUT_OF_BAND]


def test_a_ratio_above_the_band_is_detected() -> None:
    codes = _codes(_document(_source(40)), _document(_target(80)))

    assert codes == [PARAGRAPH_WORD_RATIO_OUT_OF_BAND]


def test_a_short_source_paragraph_produces_no_noisy_ratio_finding() -> None:
    # Ten source words against one target word is a ratio of 0.1 and still says nothing.
    assessment = _assess(_document(_source(10)), _document(_target(1)))

    assert assessment.findings == ()
    assert assessment.measured_pairs == 0
    assert len(assessment.metrics) == 1


def test_the_band_edges_are_inclusive_and_taken_from_the_policy() -> None:
    assert EN_FA_V1.minimum_source_words == 25
    assert (EN_FA_V1.low_word_ratio, EN_FA_V1.high_word_ratio) == (0.60, 1.60)
    assert _assess(_document(_source(100)), _document(_target(60))).findings == ()
    assert _assess(_document(_source(100)), _document(_target(160))).findings == ()
    assert _codes(_document(_source(100)), _document(_target(59))) == [
        PARAGRAPH_WORD_RATIO_OUT_OF_BAND
    ]


def test_a_lone_ratio_outlier_is_a_warning_and_never_blocks_acceptance() -> None:
    assessment = _assess(_document(_source(40)), _document(_target(20)))

    assert [finding.severity for finding in assessment.findings] == ["warning"]
    assert assessment.valid


# --------------------------------------------------------------------------------------------
# Adjacent drift
# --------------------------------------------------------------------------------------------


def test_adjacent_complementary_ratios_produce_a_blocking_drift_finding() -> None:
    # Forty words of the first paragraph's content were written under the second marker.
    source = _document(_source(40), _source(40))
    target = _document(_target(10), _target(70))

    assessment = _assess(source, target)
    drift = [item for item in assessment.findings if item.code == ADJACENT_BOUNDARY_DRIFT_SUSPECTED]
    assert len(drift) == 1
    assert drift[0].source_paragraph_index == 1
    assert drift[0].related_paragraph_index == 2
    assert drift[0].severity == "error"
    assert not assessment.valid


def test_two_outliers_that_do_not_conserve_content_are_not_called_drift() -> None:
    # Both paragraphs are short of content: nothing migrated, something is simply missing, and
    # this detector deliberately refuses to name that.
    source = _document(_source(40), _source(40))
    target = _document(_target(10), _target(12))

    codes = _codes(source, target)
    assert ADJACENT_BOUNDARY_DRIFT_SUSPECTED not in codes
    assert codes.count(PARAGRAPH_WORD_RATIO_OUT_OF_BAND) == 2


def test_drift_is_not_claimed_across_paragraphs_too_short_to_measure() -> None:
    source = _document(_source(40), _source(5))
    target = _document(_target(10), _target(30))

    assert ADJACENT_BOUNDARY_DRIFT_SUSPECTED not in _codes(source, target)


# --------------------------------------------------------------------------------------------
# Content signals
# --------------------------------------------------------------------------------------------


def test_preserved_numerals_produce_no_finding() -> None:
    source = _document(f"{_source(30)} It was 1877 and 12 more.")
    target = _document(f"{_target(30)} سال ۱۸۷۷ و ۱۲ دیگر.")

    assert NUMERAL_NOT_PRESERVED not in _codes(source, target)


def test_a_dropped_numeral_is_detected_as_a_warning() -> None:
    source = _document(f"{_source(30)} It was 1877.")
    target = _document(f"{_target(30)} سال بود.")

    findings = _assess(source, target).findings
    numeral = [item for item in findings if item.code == NUMERAL_NOT_PRESERVED]
    assert len(numeral) == 1
    assert numeral[0].severity == "warning"
    assert numeral[0].source_paragraph_index == 1


def test_dialogue_marks_are_only_reported_when_the_target_has_none_at_all() -> None:
    quoted_source = _document(f'"{_source(30)}"')
    assert DIALOGUE_MARKS_ABSENT in _codes(quoted_source, _document(_target(30)))
    assert DIALOGUE_MARKS_ABSENT not in _codes(quoted_source, _document(f"«{_target(30)}»"))


# --------------------------------------------------------------------------------------------
# Unevaluable input
# --------------------------------------------------------------------------------------------


def test_a_block_count_difference_is_reported_as_unevaluable_rather_than_measured() -> None:
    assessment = _assess(_document(_source(30), _source(30)), _document(_target(30)))

    assert not assessment.evaluated
    assert assessment.metrics == ()
    assert [finding.code for finding in assessment.findings] == [ALIGNMENT_NOT_EVALUABLE]
    # The structural validator already blocks on this; alignment does not block on it twice.
    assert assessment.valid


# --------------------------------------------------------------------------------------------
# The persisted report
# --------------------------------------------------------------------------------------------


def test_the_report_records_the_policy_the_mode_and_the_texts_it_judged() -> None:
    source = _document(_source(40))
    target = _document(_target(40))

    report = build_alignment_report(
        _assess(source, target, mode=MARKED), source_text=source, translation_text=target
    )

    assert isinstance(report, TranslationAlignmentReport)
    assert report.alignment_qa_version == ALIGNMENT_QA_VERSION
    assert report.policy_id == "en-fa"
    assert report.contract_mode == "marked"
    assert report.marker_contract_version is not None
    assert report.valid and report.evaluated
    assert report.aligned_pairs == 1 and report.measured_pairs == 1


def test_a_legacy_report_names_no_marker_contract() -> None:
    source = _document(_source(40))
    report = build_alignment_report(
        _assess(source, _document(_target(40))),
        source_text=source,
        translation_text=_document(_target(40)),
    )

    assert report.contract_mode == "legacy_positional"
    assert report.marker_contract_version is None


def test_an_unmeasured_language_pair_never_claims_the_english_persian_evidence() -> None:
    assert policy_for("fa").policy_id == "en-fa"
    assert policy_for("fr").policy_id == "unmeasured-pair"


# --------------------------------------------------------------------------------------------
# What the repair policy is allowed to do with a finding
# --------------------------------------------------------------------------------------------


def test_suspected_drift_is_never_handed_to_a_provider_to_guess_at() -> None:
    assert (
        classify_alignment_finding(ADJACENT_BOUNDARY_DRIFT_SUSPECTED)
        is RepairFindingClass.OPERATOR_REQUIRED
    )
    assert classify_alignment_finding("a_code_no_engine_has_emitted_yet") is (
        RepairFindingClass.OPERATOR_REQUIRED
    )


def test_alignment_findings_reach_the_repair_policy_as_their_own_origin() -> None:
    from book_translator.models import TerminologyValidationReport, TranslationQAReport

    empty_hash = "0" * 64
    terminology = TerminologyValidationReport(
        schema_version="1.0",
        valid=True,
        source_sha256=empty_hash,
        translation_sha256=empty_hash,
        checked_decisions=0,
        findings=[],
    )
    structural = TranslationQAReport(
        schema_version="1.0",
        valid=True,
        source_sha256=empty_hash,
        translation_sha256=empty_hash,
        source_paragraph_count=2,
        translation_paragraph_count=2,
        findings=[],
    )
    source = _document(_source(40), _source(40))
    target = _document(_target(10), _target(70))
    alignment = build_alignment_report(
        _assess(source, target), source_text=source, translation_text=target
    )

    classified = classify_findings(terminology, structural, alignment_report=alignment)

    drift = next(item for item in classified if item.code == ADJACENT_BOUNDARY_DRIFT_SUSPECTED)
    assert drift.origin == "alignment"
    assert drift.blocking
    assert classify_findings(terminology, structural) == ()


# --------------------------------------------------------------------------------------------
# Boundaries this phase deliberately did not touch
# --------------------------------------------------------------------------------------------


def test_release_assembly_verification_and_epub_hold_no_alignment_dependency() -> None:
    root = Path(__file__).parents[1] / "src/book_translator"
    for package in ("release_assembly", "release_verification", "epub"):
        for module in (root / package).glob("*.py"):
            text = module.read_text(encoding="utf-8")
            assert "paragraph_alignment" not in text
            assert "paragraph_markers" not in text
