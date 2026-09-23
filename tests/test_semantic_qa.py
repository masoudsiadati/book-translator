"""The Semantic Coverage Judge: task construction, strict parsing, policy, and isolation."""

import json
from pathlib import Path

import pytest

from book_translator.models import SCHEMA_VERSION
from book_translator.models.semantic_qa import (
    SemanticFinding,
    SemanticFindingCode,
    SemanticJudgeResponse,
    SemanticParagraphVerdict,
    SemanticSeverity,
    SemanticVerdict,
)
from book_translator.provider_execution.protocol import ProviderRequest, ProviderResult
from book_translator.semantic_qa import (
    EN_FA_JUDGE_V1,
    JUDGE_SCHEMA_VERSION,
    SYSTEM_MESSAGE,
    FakeSemanticJudgeProvider,
    JudgeCall,
    SemanticJudgeError,
    SemanticResponseError,
    SemanticTaskError,
    TransportSemanticJudge,
    apply_policy,
    build_batches,
    build_tasks,
    parse_judge_response,
    passing_response,
    render_user_message,
)

SOURCE = (
    "The tower stood empty for nine years.\n\n"
    "She said she would not go back.\n\n"
    "He counted three horses in the yard.\n"
)
TARGET = "برج نه سال خالی ماند.\n\nاو گفت که برنمی‌گردد.\n\nاو سه اسب را در حیاط شمرد.\n"
IDS = ("P0001", "P0002", "P0003")


def _verdict(
    paragraph_id: str,
    verdict: str,
    findings: list[dict[str, object]] | None = None,
    confidence: float = 0.9,
) -> dict[str, object]:
    return {
        "paragraph_id": paragraph_id,
        "verdict": verdict,
        "confidence": confidence,
        "findings": findings or [],
    }


def _finding(
    code: str = "meaning_omitted",
    severity: str = "MEDIUM",
    confidence: float = 0.9,
) -> dict[str, object]:
    return {
        "code": code,
        "severity": severity,
        "source_evidence": "nine years",
        "explanation": "the duration has no counterpart in the target paragraph",
        "confidence": confidence,
    }


def _response(verdicts: list[dict[str, object]], **overrides: object) -> str:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "judge_schema_version": JUDGE_SCHEMA_VERSION,
        "verdicts": verdicts,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------------------------
# Task construction
# --------------------------------------------------------------------------------------------


def test_each_paragraph_becomes_one_task_carrying_bounded_neighbours() -> None:
    tasks = build_tasks(SOURCE, TARGET)

    assert [task.paragraph_id for task in tasks] == list(IDS)
    assert tasks[0].previous_source is None and tasks[0].next_source is not None
    assert tasks[1].previous_source is not None and tasks[1].next_source is not None
    assert tasks[2].next_source is None
    assert tasks[1].source_text == "She said she would not go back."


def test_paragraph_identity_survives_task_construction_and_batching() -> None:
    batches = build_batches(build_tasks(SOURCE, TARGET), batch_size=2)

    assert [batch.paragraph_ids for batch in batches] == [("P0001", "P0002"), ("P0003",)]
    assert all(
        task.paragraph_index == int(task.paragraph_id[1:])
        for batch in batches
        for task in batch.tasks
    )


def test_a_selection_still_receives_its_real_neighbours() -> None:
    tasks = build_tasks(SOURCE, TARGET, paragraph_indexes=(2,))

    assert len(tasks) == 1
    assert tasks[0].paragraph_id == "P0002"
    assert tasks[0].previous_source is not None
    assert tasks[0].next_source is not None


def test_an_unalignable_chunk_is_refused_rather_than_guessed() -> None:
    with pytest.raises(SemanticTaskError):
        build_tasks(SOURCE, "یک بند.\n")


def test_the_rendered_request_names_exactly_the_paragraphs_it_asks_about() -> None:
    batch = build_batches(build_tasks(SOURCE, TARGET), batch_size=3)[0]

    message = render_user_message(batch, chapter_id="001", chunk_id="001-001")

    assert '"paragraph_ids":["P0001","P0002","P0003"]' in message
    assert message.count('<paragraph id="') == 3
    assert "<candidate>" in message and "<context-next-source>" in message


def test_the_judge_prompt_forbids_the_style_question_and_lists_the_tolerances() -> None:
    lowered = SYSTEM_MESSAGE.lower()

    assert "you judge meaning only" in lowered
    assert "never rewrite" in lowered
    assert "not asked whether the persian is elegant" in lowered
    assert "shorter persian paragraph is not evidence of omission" in lowered
    for tolerance in ("written as one persian sentence", "reordered", "implicit", "idiomatic"):
        assert tolerance in lowered


# --------------------------------------------------------------------------------------------
# Strict parsing
# --------------------------------------------------------------------------------------------


def test_a_conformant_all_pass_response_parses() -> None:
    parsed = parse_judge_response(passing_response(IDS), expected_ids=IDS)

    assert [item.verdict for item in parsed.verdicts] == [SemanticVerdict.PASS] * 3


def test_a_response_naming_an_unknown_paragraph_fails_closed() -> None:
    text = _response([_verdict("P0001", "PASS"), _verdict("P0009", "PASS")])

    with pytest.raises(SemanticResponseError) as raised:
        parse_judge_response(text, expected_ids=("P0001",))

    assert raised.value.code == "unknown_paragraph"


def test_a_response_missing_a_paragraph_verdict_fails_closed() -> None:
    text = _response([_verdict("P0001", "PASS")])

    with pytest.raises(SemanticResponseError) as raised:
        parse_judge_response(text, expected_ids=IDS)

    assert raised.value.code == "missing_paragraph"


def test_a_response_answering_a_paragraph_twice_fails_closed() -> None:
    text = _response([_verdict("P0001", "PASS"), _verdict("P0001", "REVISE", [_finding()])])

    with pytest.raises(SemanticResponseError) as raised:
        parse_judge_response(text, expected_ids=("P0001",))

    assert raised.value.code == "contract_violation"


def test_malformed_structured_output_fails_closed() -> None:
    for text in ("not json at all", "", "{", '{"schema_version": "1.0"}'):
        with pytest.raises(SemanticResponseError):
            parse_judge_response(text, expected_ids=("P0001",))


def test_an_unknown_finding_code_or_severity_fails_closed() -> None:
    for bad in (_finding(code="vibes_changed"), _finding(severity="CATASTROPHIC")):
        text = _response([_verdict("P0001", "REVISE", [bad])])
        with pytest.raises(SemanticResponseError) as raised:
            parse_judge_response(text, expected_ids=("P0001",))
        assert raised.value.code == "contract_violation"


def test_a_revise_verdict_without_a_finding_fails_closed() -> None:
    text = _response([_verdict("P0001", "REVISE")])

    with pytest.raises(SemanticResponseError):
        parse_judge_response(text, expected_ids=("P0001",))


def test_a_pass_verdict_carrying_a_finding_fails_closed() -> None:
    text = _response([_verdict("P0001", "PASS", [_finding()])])

    with pytest.raises(SemanticResponseError):
        parse_judge_response(text, expected_ids=("P0001",))


def test_a_wrapping_code_fence_is_tolerated_but_a_preamble_is_not() -> None:
    body = passing_response(("P0001",))

    assert parse_judge_response(f"```json\n{body}\n```", expected_ids=("P0001",))
    with pytest.raises(SemanticResponseError):
        parse_judge_response(f"Here is my analysis:\n{body}", expected_ids=("P0001",))


def test_a_response_declaring_a_different_judge_schema_fails_closed() -> None:
    text = _response([_verdict("P0001", "PASS")], judge_schema_version="9.9.9")

    with pytest.raises(SemanticResponseError) as raised:
        parse_judge_response(text, expected_ids=("P0001",))

    assert raised.value.code == "judge_schema_version_mismatch"


def test_evidence_and_explanation_length_is_bounded() -> None:
    long_evidence = _finding()
    long_evidence["source_evidence"] = "x" * 400
    text = _response([_verdict("P0001", "REVISE", [long_evidence])])

    with pytest.raises(SemanticResponseError):
        parse_judge_response(text, expected_ids=("P0001",))


# --------------------------------------------------------------------------------------------
# The acceptance policy
# --------------------------------------------------------------------------------------------


def _parsed(
    verdict: str, findings: list[dict[str, object]] | None = None
) -> SemanticParagraphVerdict:
    response = SemanticJudgeResponse.model_validate(
        json.loads(_response([_verdict("P0001", verdict, findings)]))
    )
    return response.verdicts[0]


def test_a_faithful_paragraph_passes() -> None:
    outcome, material, advisory = apply_policy(_parsed("PASS"))

    assert outcome is SemanticVerdict.PASS
    assert material == () and advisory == ()


def test_a_confident_high_finding_becomes_revise() -> None:
    outcome, material, _ = apply_policy(
        _parsed("REVISE", [_finding(code="negation_changed", severity="HIGH", confidence=0.8)])
    )

    assert outcome is SemanticVerdict.REVISE
    assert material[0].code is SemanticFindingCode.NEGATION_CHANGED


def test_a_low_severity_finding_is_advisory_and_never_blocks() -> None:
    outcome, material, advisory = apply_policy(
        _parsed("REVISE", [_finding(severity="LOW", confidence=1.0)])
    )

    assert outcome is SemanticVerdict.PASS
    assert material == () and len(advisory) == 1


def test_a_medium_finding_below_the_confidence_floor_is_advisory() -> None:
    outcome, material, advisory = apply_policy(
        _parsed("REVISE", [_finding(severity="MEDIUM", confidence=0.5)])
    )

    assert outcome is SemanticVerdict.PASS
    assert material == () and len(advisory) == 1
    assert EN_FA_JUDGE_V1.medium_confidence == 0.70


def test_inconclusive_is_never_converted_into_a_pass() -> None:
    outcome, _, _ = apply_policy(_parsed("INCONCLUSIVE"))

    assert outcome is SemanticVerdict.INCONCLUSIVE
    assert outcome.value != SemanticVerdict.PASS.value


def test_a_judge_pass_is_never_promoted_into_a_revise() -> None:
    # The policy holds no evidence the judge lacked, so it may only ever be more permissive.
    assert apply_policy(_parsed("PASS"))[0] is SemanticVerdict.PASS


def test_the_policy_thresholds_are_versioned() -> None:
    assert EN_FA_JUDGE_V1.policy_version == "1.0.0"
    assert EN_FA_JUDGE_V1.low_confidence is None
    assert EN_FA_JUDGE_V1.high_confidence == 0.60


# --------------------------------------------------------------------------------------------
# The provider boundary
# --------------------------------------------------------------------------------------------


def _judge(texts: tuple[str, ...]) -> TransportSemanticJudge:
    return TransportSemanticJudge(FakeSemanticJudgeProvider(texts), model="fake-judge-v1")


def _call(judge: TransportSemanticJudge, ids: tuple[str, ...] = ("P0001",)) -> JudgeCall:
    return judge.judge(
        run_id="tr-0123456789ab-001",
        job_id="tr-0123456789ab-001__001-001",
        chunk_id="001-001",
        user_message="<judge-task>{}</judge-task>",
        paragraph_ids=ids,
        source_language="en",
        target_language="fa",
        repetition=1,
        batch_size=len(ids),
    )


def test_provider_and_model_identity_are_recorded_from_the_transport() -> None:
    call = _call(_judge((passing_response(("P0001",)),)))

    assert call.record.provider == "fake"
    assert call.record.adapter_version == "1.0.0"
    assert call.record.model_requested == "fake-judge-v1"
    assert call.record.model_returned == "fake-judge-v1"
    assert call.record.request_id.startswith("sjq-")


def test_a_transport_that_reports_no_model_records_none_rather_than_the_requested_one() -> None:
    class Silent(FakeSemanticJudgeProvider):
        def execute(self, request: ProviderRequest) -> ProviderResult:
            result = super().execute(request)
            return ProviderResult(
                response_id=result.response_id,
                model=None,
                status="completed",
                text_outputs=result.text_outputs,
            )

    judge = TransportSemanticJudge(Silent((passing_response(("P0001",)),)), model="asked-for")

    assert _call(judge).record.model_returned is None


def test_a_malformed_answer_is_a_judge_failure_not_a_verdict() -> None:
    with pytest.raises(SemanticJudgeError) as raised:
        _call(_judge(("this is not json",)))

    assert raised.value.code == "malformed_json"
    assert raised.value.retryable


def test_an_identity_failure_is_not_retryable() -> None:
    text = _response([_verdict("P0009", "PASS")])

    with pytest.raises(SemanticJudgeError) as raised:
        _call(_judge((text,)))

    assert raised.value.code in {"unknown_paragraph", "missing_paragraph"}
    assert not raised.value.retryable


def test_a_transport_exception_becomes_a_retryable_judge_failure() -> None:
    with pytest.raises(SemanticJudgeError) as raised:
        _call(_judge(("__raise__",)))

    assert raised.value.code == "provider_failure"
    assert raised.value.retryable


def test_two_repetitions_of_the_same_paragraphs_get_distinct_request_identities() -> None:
    from book_translator.semantic_qa import judge_request_id

    first = judge_request_id(
        run_id="tr-0123456789ab-001",
        chunk_id="001-001",
        paragraph_ids=("P0001",),
        repetition=1,
        system_message=SYSTEM_MESSAGE,
        user_message="body",
    )
    second = judge_request_id(
        run_id="tr-0123456789ab-001",
        chunk_id="001-001",
        paragraph_ids=("P0001",),
        repetition=2,
        system_message=SYSTEM_MESSAGE,
        user_message="body",
    )

    assert first != second


# --------------------------------------------------------------------------------------------
# What the judge is not allowed to reach
# --------------------------------------------------------------------------------------------


def test_no_release_or_publication_stage_imports_the_semantic_judge() -> None:
    root = Path(__file__).parents[1] / "src/book_translator"
    for package in ("release_assembly", "release_verification", "epub"):
        for module in (root / package).glob("*.py"):
            text = module.read_text(encoding="utf-8")
            assert "semantic_qa" not in text


def test_the_judge_never_receives_the_translator_role_or_earlier_judgements() -> None:
    from book_translator.prompt_packaging.rendering import SYSTEM_MESSAGE as TRANSLATOR

    batch = build_batches(build_tasks(SOURCE, TARGET), batch_size=3)[0]
    message = render_user_message(batch, chapter_id="001", chunk_id="001-001")

    assert SYSTEM_MESSAGE != TRANSLATOR
    assert "professional literary book translator" not in SYSTEM_MESSAGE
    for leak in ("translation-prompt-rules", "style-guide", "previous judge", "verdict:"):
        assert leak not in message


def test_a_persisted_finding_carries_evidence_but_never_a_whole_paragraph() -> None:
    finding = SemanticFinding(
        code=SemanticFindingCode.MEANING_OMITTED,
        severity=SemanticSeverity.MEDIUM,
        source_evidence="nine years",
        explanation="the duration is absent",
        confidence=0.8,
    )

    assert len(finding.source_evidence) < len(SOURCE)
    with pytest.raises(ValueError):
        SemanticFinding(
            code=SemanticFindingCode.MEANING_OMITTED,
            severity=SemanticSeverity.MEDIUM,
            source_evidence=SOURCE * 10,
            explanation="too much",
            confidence=0.8,
        )
