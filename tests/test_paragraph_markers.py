"""The numbered paragraph output contract: rendering, parsing, and failing closed."""

import pytest

from book_translator.models import ProviderErrorCategory
from book_translator.paragraph_markers import (
    ParagraphMarkerError,
    mark_paragraphs,
    marker_for,
    parse_marked_document,
    source_paragraphs,
    strip_markers,
)
from book_translator.prompt_packaging.identity import (
    MARKED_OUTPUT_CONTRACT_VERSION,
    OUTPUT_CONTRACT_VERSION,
    uses_paragraph_markers,
)
from book_translator.prompt_packaging.rendering import PARAGRAPH_MARKER_RULES
from book_translator.provider_execution.errors import ProviderExecutionError
from book_translator.provider_execution.protocol import ProviderResult, ProviderUsage
from book_translator.provider_execution.result_validation import extract_translation

SOURCE = "Alpha beta gamma.\n\nDelta epsilon.\n\nZeta eta theta.\n"

# Synthetic Persian blocks. They carry no meaning and translate nothing; they exist so the parser
# is exercised against right-to-left text with a zero-width non-joiner in it.
FIRST = "آلفا بتا گاما."
SECOND = "دلتا اپسیلون‌ها."
THIRD = "زتا اتا تتا."
TRANSLATION = f"{FIRST}\n\n{SECOND}\n\n{THIRD}\n"


def _marked(*blocks: str) -> str:
    return "\n\n".join(f"{marker_for(index)}\n{block}" for index, block in enumerate(blocks, 1))


def _codes(text: str, *, expected: int = 3) -> set[str]:
    return {
        finding.code for finding in parse_marked_document(text, expected_count=expected).findings
    }


# --------------------------------------------------------------------------------------------
# Rendering and reconstruction
# --------------------------------------------------------------------------------------------


def test_every_source_paragraph_receives_exactly_one_stable_marker() -> None:
    marked = mark_paragraphs(SOURCE)

    assert marked.count("[[P") == 3
    assert marked.startswith("[[P0001]]\nAlpha beta gamma.")
    assert "[[P0003]]\nZeta eta theta." in marked
    assert mark_paragraphs(SOURCE) == marked


def test_marking_is_byte_exactly_invertible() -> None:
    assert strip_markers(mark_paragraphs(SOURCE)) == SOURCE


def test_persian_paragraph_text_survives_marker_removal_byte_for_byte() -> None:
    result = parse_marked_document(_marked(FIRST, SECOND, THIRD) + "\n", expected_count=3)

    assert result.valid
    assert result.blocks == (FIRST, SECOND, THIRD)
    assert result.text == TRANSLATION
    assert result.text.encode("utf-8") == TRANSLATION.encode("utf-8")


def test_a_source_that_already_contains_marker_syntax_is_refused() -> None:
    with pytest.raises(ParagraphMarkerError):
        mark_paragraphs("Alpha.\n\n[[P0002]] beta.\n")


def test_source_segmentation_matches_the_projects_block_rule() -> None:
    assert source_paragraphs("A.\n\n\n\nB.\n") == ("A.", "B.")


# --------------------------------------------------------------------------------------------
# Conformance
# --------------------------------------------------------------------------------------------


def test_a_perfect_marker_preserving_translation_passes() -> None:
    result = parse_marked_document(_marked(FIRST, SECOND, THIRD), expected_count=3)

    assert result.valid
    assert result.findings == ()
    assert result.observed_markers == ("[[P0001]]", "[[P0002]]", "[[P0003]]")


def test_a_missing_marker_fails() -> None:
    text = f"{marker_for(1)}\n{FIRST}\n\n{marker_for(3)}\n{THIRD}"

    assert _codes(text) == {"marker_missing"}


def test_a_duplicated_marker_fails() -> None:
    text = _marked(FIRST, SECOND) + f"\n\n{marker_for(2)}\n{THIRD}"

    assert "marker_duplicated" in _codes(text)


def test_an_unknown_marker_fails() -> None:
    text = _marked(FIRST, SECOND) + f"\n\n{marker_for(9)}\n{THIRD}"

    assert _codes(text) == {"marker_unknown", "marker_missing"}


def test_reordered_markers_fail() -> None:
    text = f"{marker_for(1)}\n{FIRST}\n\n{marker_for(3)}\n{THIRD}\n\n{marker_for(2)}\n{SECOND}"

    assert "marker_out_of_order" in _codes(text)


def test_merged_paragraph_identities_fail() -> None:
    # The provider wrote two source paragraphs under one marker and dropped the other marker.
    text = f"{marker_for(1)}\n{FIRST} {SECOND}\n\n{marker_for(3)}\n{THIRD}"

    assert _codes(text) == {"marker_missing"}


def test_a_split_that_duplicates_an_identity_fails() -> None:
    text = (
        f"{marker_for(1)}\n{FIRST}\n\n{marker_for(2)}\n{SECOND}\n\n"
        f"{marker_for(3)}\n{THIRD}\n\n{marker_for(3)}\nادامه."
    )

    assert "marker_duplicated" in _codes(text)


def test_prose_without_markers_fails_for_a_new_contract_job() -> None:
    assert _codes(TRANSLATION) == {"markers_absent"}


def test_text_before_the_first_marker_fails() -> None:
    text = "Here is the translation you asked for:\n\n" + _marked(FIRST, SECOND, THIRD)

    assert "text_before_first_marker" in _codes(text)


def test_a_malformed_marker_fails_rather_than_being_absorbed_into_prose() -> None:
    text = f"[[P1]]\n{FIRST}\n\n{marker_for(2)}\n{SECOND}\n\n{marker_for(3)}\n{THIRD}"

    codes = _codes(text)
    assert "marker_malformed" in codes and "marker_missing" in codes


def test_an_inline_marker_inside_a_paragraph_fails() -> None:
    text = _marked(f"{FIRST} [[P0002]] ادامه.", SECOND, THIRD)

    assert "marker_malformed" in _codes(text)


def test_an_empty_marker_block_fails() -> None:
    text = _marked(FIRST, "", THIRD)

    assert "empty_marker_block" in _codes(text)


def test_a_findings_record_names_the_expected_and_observed_marker() -> None:
    text = _marked(FIRST, SECOND) + f"\n\n{marker_for(9)}\n{THIRD}"

    findings = parse_marked_document(text, expected_count=3).findings
    unknown = next(item for item in findings if item.code == "marker_unknown")
    missing = next(item for item in findings if item.code == "marker_missing")
    assert unknown.observed_marker == "[[P0009]]"
    assert missing.expected_marker == "[[P0003]]"
    assert missing.source_paragraph_index == 3
    assert all(item.severity == "error" for item in findings)


def test_a_nonconformant_document_refuses_to_produce_marker_free_text() -> None:
    result = parse_marked_document(TRANSLATION, expected_count=3)

    assert result.blocks == ()
    with pytest.raises(ValueError):
        _ = result.text


# --------------------------------------------------------------------------------------------
# The provider boundary
# --------------------------------------------------------------------------------------------


def _result(text: str) -> ProviderResult:
    return ProviderResult(
        response_id="resp-1",
        model="fake-v1",
        status="completed",
        text_outputs=(text,),
        usage=ProviderUsage(10, 10, 20),
    )


def test_the_provider_boundary_strips_markers_from_a_conformant_answer() -> None:
    validated = extract_translation(
        _result(_marked(FIRST, SECOND, THIRD)),
        SOURCE.encode("utf-8"),
        output_contract=MARKED_OUTPUT_CONTRACT_VERSION,
    )

    assert validated.content.decode("utf-8") == TRANSLATION
    assert "[[P" not in validated.content.decode("utf-8")


def test_the_provider_boundary_refuses_a_nonconformant_answer_before_any_artifact_exists() -> None:
    with pytest.raises(ProviderExecutionError) as raised:
        extract_translation(
            _result(TRANSLATION),
            SOURCE.encode("utf-8"),
            output_contract=MARKED_OUTPUT_CONTRACT_VERSION,
        )

    classification = raised.value.classification
    assert classification.category is ProviderErrorCategory.MALFORMED_RESPONSE
    assert "markers_absent" in classification.message
    assert classification.retryable and not classification.ambiguous


def test_a_marked_echo_of_the_source_is_still_caught_as_an_echo() -> None:
    with pytest.raises(ProviderExecutionError) as raised:
        extract_translation(
            _result(mark_paragraphs(SOURCE)),
            SOURCE.encode("utf-8"),
            output_contract=MARKED_OUTPUT_CONTRACT_VERSION,
        )

    assert "echoed the source" in raised.value.classification.message


def test_an_unmarked_contract_is_unaffected_by_the_marker_parser() -> None:
    validated = extract_translation(
        _result(TRANSLATION), SOURCE.encode("utf-8"), output_contract=OUTPUT_CONTRACT_VERSION
    )

    assert validated.content.decode("utf-8") == TRANSLATION
    assert not uses_paragraph_markers(OUTPUT_CONTRACT_VERSION)
    assert not uses_paragraph_markers(None)


def test_the_prompt_rules_state_every_prohibition_the_contract_rests_on() -> None:
    lowered = PARAGRAPH_MARKER_RULES.lower()

    assert "never merge two source paragraphs" in lowered
    assert "never split one source paragraph" in lowered
    assert "never move a sentence" in lowered
    assert "exactly once" in lowered
    assert "ascending source order" in lowered
