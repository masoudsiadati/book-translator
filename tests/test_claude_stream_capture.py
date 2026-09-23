"""Lossless capture of Claude Code output across several assistant turns.

The ``json`` print format's ``result`` field is the last text block of the *last* assistant
message, and Claude Code resets it on every internal user turn, so a session that hit the output
token limit and was nudged to continue lost its first turn.  The adapter now reads the
``stream-json`` transcript and reconstructs the primary session's assistant text itself.  These
tests pin what is collected, what is ignored, what is refused, and that nothing about privacy,
provenance, or rejected-output preservation regressed.  No test launches a real process.
"""

import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest

from book_translator.models import ProviderErrorCategory, ProviderName, TranslationAttemptStatus
from book_translator.paragraph_alignment import EN_FA_V1, MARKED, assess_alignment
from book_translator.paragraph_markers import MarkerFinding, marker_for
from book_translator.prompt_packaging import MARKED_OUTPUT_CONTRACT_VERSION
from book_translator.provider_execution import (
    ExecutorError,
    ProviderExecutionError,
    ProviderResult,
    execute_translation_job,
    list_attempts,
    plan_execution,
    preflight_provider,
)
from book_translator.provider_execution.artifacts import sha256_bytes
from book_translator.provider_execution.providers import (
    CODEX_CLI_ADAPTER_VERSION,
    FAKE_PROVIDER_VERSION,
    OPENAI_RESPONSES_ADAPTER_VERSION,
    FakeTranslationProvider,
)
from book_translator.provider_execution.providers.claude_code import (
    CLAUDE_CODE_ADAPTER_VERSION,
    REQUIRED_CLI_FLAGS,
    ClaudeCodeTranslationProvider,
)
from book_translator.provider_execution.providers.claude_stream import (
    ClaudeStreamError,
    ClaudeStreamResult,
    final_result_object,
    parse_claude_stream,
)
from book_translator.provider_execution.result_validation import (
    candidate_output,
    validate_candidate,
)
from book_translator.translation_queue import claim_job
from tests.test_claude_code_provider import (
    PERSIAN,
    REPORT,
    SESSION_ID,
    FakeClaudeCli,
    _allow_claude_code,
    _assistant,
    _install,
    _payload,
    _request,
    _stream,
    _system_init,
    _user,
)
from tests.test_provider_execution import EXECUTION_NOW, _claimed_execution
from tests.test_rejected_provider_output import _clock, _marked_run

# --------------------------------------------------------------------------------------------
# Fixtures: a 29-paragraph chunk translated across two assistant turns
# --------------------------------------------------------------------------------------------

PARAGRAPHS = 29
CUT_AT = 13
NUDGE = (
    "Output token limit hit. Resume directly — no apology, no recap of what you were doing. "
    "Pick up mid-thought if that is where the cut happened. "
    "Break remaining work into smaller pieces."
)


def _persian(index: int) -> str:
    return f"پاراگراف شمارهٔ {index} با متن فارسی."


def _marked(index: int) -> str:
    return f"{marker_for(index)}\n{_persian(index)}"


SOURCE_29 = "".join(
    f"Paragraph number {index} in English.\n\n" for index in range(1, PARAGRAPHS + 1)
)
FULL_ANSWER = "\n\n".join(_marked(index) for index in range(1, PARAGRAPHS + 1)) + "\n"
# The first turn stops mid-sentence inside paragraph 13; the second resumes exactly there, which is
# what Claude Code's continuation nudge asks the model to do.
_SPLIT = FULL_ANSWER.index(_persian(CUT_AT)) + len(_persian(CUT_AT)) // 2
TURN_ONE = FULL_ANSWER[:_SPLIT]
TURN_TWO = FULL_ANSWER[_SPLIT:]
SHORT_ANSWER = "\n\n".join(_marked(index) for index in range(1, 4)) + "\n"


def _two_turn_events(
    *, second: str = TURN_TWO, num_turns: int = 2, **result_updates: object
) -> list[dict[str, object]]:
    result_updates.setdefault("result", second)
    result_updates.setdefault("num_turns", num_turns)
    return [
        _system_init(),
        _assistant(TURN_ONE, message_id="msg_01", uuid="a-1", stop_reason="max_tokens"),
        _user(NUDGE),
        _assistant(second, message_id="msg_02", uuid="a-2"),
        _payload(**result_updates),
    ]


def _parse(*events: dict[str, object]) -> ClaudeStreamResult:
    return parse_claude_stream(_stream(*events).decode("utf-8"))


def _refused(*events: dict[str, object]) -> str:
    with pytest.raises(ClaudeStreamError) as caught:
        _parse(*events)
    return str(caught.value)


def _markers(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("[[P")]


@pytest.fixture(autouse=True)
def _no_real_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """V. No test here may reach a Claude, Codex, or OpenAI process."""

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("a real provider process was invoked")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


# --------------------------------------------------------------------------------------------
# A. Input integrity: the prompt package reaches the provider whole
# --------------------------------------------------------------------------------------------


def test_user_message_is_loaded_from_the_package_hash_verified_and_sent_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, run_path, job_id, token = _claimed_execution(tmp_path)
    _allow_claude_code(tmp_path)
    plan = plan_execution(
        run_id,
        job_id,
        project_root=tmp_path,
        worker_id="worker",
        claim_token=token,
        provider=ProviderName.CLAUDE_CODE,
        model=None,
        timeout_seconds=120,
        max_output_tokens=1000,
        now=lambda: EXECUTION_NOW,
    )
    user_path = tmp_path / plan.package.user_message.file
    user_bytes = user_path.read_bytes()
    assert sha256_bytes(user_bytes) == plan.package.user_message.sha256
    assert plan.request.user_message == user_bytes.decode("utf-8")
    assert plan.request.system_message == (tmp_path / plan.package.system_message.file).read_text(
        encoding="utf-8"
    )
    assert plan.request_payload["user_message"] == {
        "file": str(plan.package.user_message.file),
        "sha256": plan.package.user_message.sha256,
    }

    fake = _install(monkeypatch, FakeClaudeCli())
    ClaudeCodeTranslationProvider(preflight_report=REPORT).execute(plan.request)
    assert cast(bytes, fake.translation_calls[0]["input"]) == user_bytes

    # A tampered file is refused before any request exists: the package validator's own hash
    # check fires first, and the executor's per-message hash check stands behind it.
    user_path.write_bytes(user_bytes + b"\ntampered\n")
    with pytest.raises(ExecutorError, match="hash mismatch|stale, or invalid"):
        plan_execution(
            run_id,
            job_id,
            project_root=tmp_path,
            worker_id="worker",
            claim_token=token,
            provider=ProviderName.CLAUDE_CODE,
            model=None,
            timeout_seconds=120,
            max_output_tokens=1000,
            now=lambda: EXECUTION_NOW,
        )
    assert run_path.is_dir()


# --------------------------------------------------------------------------------------------
# B / C. One turn and two turns reconstruct every marker once, in order
# --------------------------------------------------------------------------------------------


def test_single_turn_stream_reconstructs_the_one_assistant_message() -> None:
    parsed = _parse(_system_init(), _assistant(SHORT_ANSWER), _payload(result=SHORT_ANSWER))

    assert parsed.text == SHORT_ANSWER
    assert _markers(parsed.text) == [marker_for(1), marker_for(2), marker_for(3)]
    assert parsed.session_id == SESSION_ID
    assert parsed.assistant_message_count == 1 and parsed.user_event_count == 0
    assert [message.message_id for message in parsed.messages] == ["msg_01"]
    assert parsed.messages[0].model == "claude-opus-5"


def test_two_turn_stream_reconstructs_both_turns_in_order() -> None:
    parsed = _parse(*_two_turn_events())

    assert parsed.text == TURN_ONE + TURN_TWO == FULL_ANSWER
    assert _markers(parsed.text) == [marker_for(index) for index in range(1, PARAGRAPHS + 1)]
    assert parsed.assistant_message_count == 2 and parsed.user_event_count == 1
    assert [message.stop_reason for message in parsed.messages] == ["max_tokens", "end_turn"]
    assert parsed.result["num_turns"] == 2


def test_two_turn_reconstruction_passes_the_marker_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeClaudeCli(stdout=_stream(*_two_turn_events())))
    result = ClaudeCodeTranslationProvider(preflight_report=REPORT).execute(_request())

    assert result.text_outputs == (FULL_ANSWER,)
    validated = validate_candidate(
        candidate_output(result),
        SOURCE_29.encode("utf-8"),
        output_contract=MARKED_OUTPUT_CONTRACT_VERSION,
    )
    assert validated.content.decode("utf-8").count("\n\n") == PARAGRAPHS - 1
    assert "[[P" not in validated.content.decode("utf-8")
    assert result.sanitized_response["num_turns"] == 2
    assert result.sanitized_response["assistant_message_count"] == 2
    assert result.sanitized_response["user_event_count"] == 1


def test_a_second_turn_alone_is_the_lossy_capture_this_adapter_replaces() -> None:
    """The historical failure: only the continuation survives, so the contract is violated."""
    lossy = candidate_output(
        ProviderResult(response_id=None, model=None, status="completed", text_outputs=(TURN_TWO,))
    )
    with pytest.raises(ProviderExecutionError) as caught:
        validate_candidate(
            lossy, SOURCE_29.encode("utf-8"), output_contract=MARKED_OUTPUT_CONTRACT_VERSION
        )
    diagnostics = caught.value.marker_diagnostics
    assert diagnostics is not None
    assert (diagnostics.expected_marker_count, diagnostics.observed_marker_count) == (29, 16)
    assert {finding.code for finding in diagnostics.findings} == {
        "marker_missing",
        "text_before_first_marker",
    }


# --------------------------------------------------------------------------------------------
# D. Deltas beside complete messages never duplicate text
# --------------------------------------------------------------------------------------------


def _delta_events(text: str) -> list[dict[str, object]]:
    def event(payload: dict[str, object], uuid: str) -> dict[str, object]:
        return {
            "type": "stream_event",
            "event": payload,
            "parent_tool_use_id": None,
            "session_id": SESSION_ID,
            "uuid": uuid,
        }

    return [
        event({"type": "message_start", "message": {"id": "msg_01", "content": []}}, "s-1"),
        event(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            "s-2",
        ),
        event(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text[:10]},
            },
            "s-3",
        ),
        event(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text[10:]},
            },
            "s-4",
        ),
        event({"type": "content_block_stop", "index": 0}, "s-5"),
        event(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
            "s-6",
        ),
        event({"type": "message_stop"}, "s-7"),
    ]


def test_partial_message_deltas_are_ignored_in_favour_of_the_complete_message() -> None:
    parsed = _parse(
        _system_init(),
        *_delta_events(SHORT_ANSWER),
        _assistant(SHORT_ANSWER),
        _payload(result=SHORT_ANSWER),
    )

    assert parsed.text == SHORT_ANSWER
    assert parsed.text.count(marker_for(1)) == 1
    assert parsed.assistant_message_count == 1


# --------------------------------------------------------------------------------------------
# E / I. Thinking, tool-use, and tool-result content never enter the translation
# --------------------------------------------------------------------------------------------


def test_thinking_blocks_are_excluded_whether_inline_or_as_their_own_event() -> None:
    thinking: dict[str, object] = {
        "type": "thinking",
        "thinking": "reasoning about paragraph one",
        "signature": "sig",
    }
    redacted: dict[str, object] = {"type": "redacted_thinking", "data": "opaque"}
    parsed = _parse(
        _system_init(),
        # Streaming emits one event per content block, all sharing the API message id.
        _assistant("", message_id="msg_01", uuid="a-1", blocks=[thinking], stop_reason=None),
        _assistant("", message_id="msg_01", uuid="a-2", blocks=[redacted], stop_reason=None),
        _assistant(SHORT_ANSWER, message_id="msg_01", uuid="a-3"),
        _payload(result=SHORT_ANSWER),
    )

    assert parsed.text == SHORT_ANSWER
    assert "reasoning" not in parsed.text and "opaque" not in parsed.text
    assert parsed.assistant_message_count == 1
    assert parsed.messages[0].text_block_count == 1


def test_inline_thinking_beside_text_keeps_only_the_text() -> None:
    blocks: list[dict[str, object]] = [
        {"type": "thinking", "thinking": "plan", "signature": "sig"},
        {"type": "text", "text": SHORT_ANSWER},
    ]
    parsed = _parse(_system_init(), _assistant("", blocks=blocks), _payload(result=SHORT_ANSWER))
    assert parsed.text == SHORT_ANSWER


def test_tool_use_blocks_and_tool_results_are_ignored() -> None:
    tool_use: dict[str, object] = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "Read",
        "input": {"path": "x"},
    }
    tool_result: list[dict[str, object]] = [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file body"}
    ]
    parsed = _parse(
        _system_init(),
        _assistant("", message_id="msg_01", uuid="a-1", blocks=[tool_use], stop_reason="tool_use"),
        _user("", uuid="u-1", is_meta=False, blocks=tool_result),
        _assistant(SHORT_ANSWER, message_id="msg_02", uuid="a-2"),
        _payload(result=SHORT_ANSWER, num_turns=2),
    )

    assert parsed.text == SHORT_ANSWER
    assert "file body" not in parsed.text and "toolu_1" not in parsed.text
    assert parsed.assistant_message_count == 2


# --------------------------------------------------------------------------------------------
# F / G / H. User, system, and progress events are not translation
# --------------------------------------------------------------------------------------------


def test_user_system_and_progress_events_are_ignored() -> None:
    noise: list[dict[str, object]] = [
        _system_init(),
        {
            "type": "system",
            "subtype": "status",
            "status": "requesting",
            "session_id": SESSION_ID,
            "uuid": "n-1",
        },
        _user("continue please", uuid="u-1", is_meta=False),
        {
            "type": "tool_progress",
            "tool_use_id": "t",
            "tool_name": "Bash",
            "parent_tool_use_id": None,
            "elapsed_time_seconds": 1,
            "session_id": SESSION_ID,
            "uuid": "n-2",
        },
        {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed"},
            "session_id": SESSION_ID,
            "uuid": "n-3",
        },
        {
            "type": "prompt_suggestion",
            "suggestion": "next prompt",
            "session_id": SESSION_ID,
            "uuid": "n-4",
        },
        {"type": "system", "subtype": "compact_boundary", "session_id": SESSION_ID, "uuid": "n-5"},
    ]
    thinking_only = _assistant(
        "", message_id="msg_00", uuid="a-0", blocks=[{"type": "thinking", "thinking": "t"}]
    )
    parsed = _parse(
        *noise[:3],
        thinking_only,
        *noise[3:],
        _assistant(SHORT_ANSWER),
        _payload(result=SHORT_ANSWER, num_turns=2),
    )

    assert parsed.text == SHORT_ANSWER
    assert "continue please" not in parsed.text and "next prompt" not in parsed.text
    assert parsed.user_event_count == 1 and parsed.assistant_message_count == 2
    assert parsed.ignored_event_count == 7


# --------------------------------------------------------------------------------------------
# J / K. Subagent and foreign-session messages are never the translation
# --------------------------------------------------------------------------------------------


def test_subagent_assistant_messages_are_ignored() -> None:
    parsed = _parse(
        _system_init(),
        _assistant(
            "subagent prose", message_id="msg_sub", uuid="a-sub", parent_tool_use_id="toolu_9"
        ),
        _assistant(SHORT_ANSWER, message_id="msg_01", uuid="a-1"),
        _payload(result=SHORT_ANSWER),
    )

    assert parsed.text == SHORT_ANSWER
    assert "subagent prose" not in parsed.text
    assert parsed.assistant_message_count == 1


def test_assistant_message_from_another_session_fails_closed() -> None:
    message = _refused(
        _system_init(),
        _assistant(SHORT_ANSWER, session_id="ffffffff-0000-4000-8000-00000000ffff"),
        _payload(result=SHORT_ANSWER),
    )
    assert "session" in message
    assert SHORT_ANSWER.strip() not in message


def test_assistant_message_without_session_identity_fails_closed() -> None:
    assert "session" in _refused(
        _system_init(), _assistant(SHORT_ANSWER, session_id=None), _payload(result=SHORT_ANSWER)
    )


def test_synthesized_meta_and_api_error_assistant_messages_are_not_translation() -> None:
    parsed = _parse(
        _system_init(),
        _assistant(
            "API Error: something", message_id="msg_e", uuid="a-e", is_api_error_message=True
        ),
        _assistant("notice", message_id="msg_m", uuid="a-m", is_meta=True),
        _assistant(SHORT_ANSWER, message_id="msg_01", uuid="a-1"),
        _payload(result=SHORT_ANSWER),
    )
    assert parsed.text == SHORT_ANSWER
    assert parsed.assistant_message_count == 1


# --------------------------------------------------------------------------------------------
# L. A reported turn that cannot be reconstructed fails closed
# --------------------------------------------------------------------------------------------


def test_missing_first_turn_fails_closed_instead_of_returning_the_continuation() -> None:
    message = _refused(
        _system_init(),
        _user(NUDGE),
        _assistant(TURN_TWO, message_id="msg_02", uuid="a-2"),
        _payload(result=TURN_TWO, num_turns=2),
    )
    assert "2 turns" in message and "1 " in message
    assert _persian(14) not in message


def test_missing_first_turn_is_a_retryable_non_ambiguous_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lossy = _stream(
        _system_init(),
        _user(NUDGE),
        _assistant(TURN_TWO, message_id="msg_02", uuid="a-2"),
        _payload(result=TURN_TWO, num_turns=2),
    )
    _install(monkeypatch, FakeClaudeCli(stdout=lossy))
    with pytest.raises(ProviderExecutionError) as caught:
        ClaudeCodeTranslationProvider(preflight_report=REPORT).execute(_request())

    classification = caught.value.classification
    assert classification.category is ProviderErrorCategory.MALFORMED_RESPONSE
    assert classification.retryable and not classification.ambiguous
    assert _persian(14) not in classification.message
    assert caught.value.marker_diagnostics is None


def test_more_user_turns_than_the_result_reports_fails_closed() -> None:
    assert "user" in _refused(*_two_turn_events(num_turns=1))


def test_result_text_that_contradicts_the_final_assistant_message_fails_closed() -> None:
    assert "does not match" in _refused(*_two_turn_events(result="a different final text"))


# --------------------------------------------------------------------------------------------
# M. Malformed streams fail safely
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "   \n\n",
        "Claude Code banner, not JSON\n",
        json.dumps(_system_init()) + "\nnot json\n" + json.dumps(_payload()) + "\n",
        "[]\n",
        json.dumps(_system_init()) + "\n",  # no result object
        json.dumps({"type": "result"}) + "\n",  # result without turn count
        json.dumps(_payload(num_turns=True)) + "\n",
        json.dumps(_payload(num_turns=-1)) + "\n",
        json.dumps(_payload(session_id=None)) + "\n",
    ],
)
def test_malformed_streams_are_refused(stdout: str) -> None:
    with pytest.raises(ClaudeStreamError):
        parse_claude_stream(stdout)


def test_two_result_objects_or_an_assistant_after_the_result_fail_closed() -> None:
    assert "result" in _refused(
        _system_init(), _assistant(SHORT_ANSWER), _payload(result=SHORT_ANSWER), _payload()
    )
    assert "after" in _refused(
        _system_init(),
        _assistant(SHORT_ANSWER),
        _payload(result=SHORT_ANSWER),
        _assistant("late", message_id="msg_02", uuid="a-2"),
    )


@pytest.mark.parametrize(
    "broken",
    [
        {"message": "not an object"},
        {"message": {"id": "msg_01", "content": "prose"}},  # string content: not the CLI's shape
        {"message": {"id": "msg_01", "content": 5}},
        {"message": {"content": [{"type": "text", "text": "x"}]}},  # no message id
        {"uuid": None},
    ],
)
def test_assistant_events_with_impossible_shapes_fail_closed(broken: dict[str, object]) -> None:
    event = _assistant(SHORT_ANSWER)
    event.update(broken)
    with pytest.raises(ClaudeStreamError):
        _parse(_system_init(), event, _payload(result=SHORT_ANSWER))


def test_stream_refusals_never_carry_prose() -> None:
    event = _assistant(SHORT_ANSWER)
    event["uuid"] = None
    message = _refused(_system_init(), event, _payload(result=SHORT_ANSWER))
    assert _persian(1) not in message and "[[P" not in message


# --------------------------------------------------------------------------------------------
# N. Duplicate assistant events: reconciled only when byte-identical
# --------------------------------------------------------------------------------------------


def test_an_identical_replayed_assistant_event_is_counted_once() -> None:
    parsed = _parse(
        _system_init(),
        _assistant(SHORT_ANSWER),
        _assistant(SHORT_ANSWER),
        _payload(result=SHORT_ANSWER),
    )
    assert parsed.text == SHORT_ANSWER
    assert parsed.duplicate_event_count == 1


def test_conflicting_events_that_share_an_identity_fail_closed() -> None:
    assert "identity" in _refused(
        _system_init(),
        _assistant(SHORT_ANSWER),
        _assistant(SHORT_ANSWER + "extra\n"),
        _payload(result=SHORT_ANSWER + "extra\n"),
    )


def test_interleaved_message_ids_fail_closed() -> None:
    assert "order" in _refused(
        _system_init(),
        _assistant("one ", message_id="msg_01", uuid="a-1"),
        _assistant("two ", message_id="msg_02", uuid="a-2"),
        _assistant("three", message_id="msg_01", uuid="a-3"),
        _payload(result="three", num_turns=1),
    )


def test_per_block_events_of_one_message_are_joined_not_deduplicated() -> None:
    """Two text blocks of one API message may even be byte-identical; both are real output."""
    parsed = _parse(
        _system_init(),
        _assistant("same\n", message_id="msg_01", uuid="a-1", stop_reason=None),
        _assistant("same\n", message_id="msg_01", uuid="a-2"),
        _payload(result="same\n"),
    )
    assert parsed.text == "same\nsame\n"
    assert parsed.duplicate_event_count == 0


# --------------------------------------------------------------------------------------------
# O / P / Q. Provenance is intact and the sanitized response stays prose-free
# --------------------------------------------------------------------------------------------


def test_model_and_usage_provenance_survive_the_stream_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _two_turn_events(
        usage={
            "input_tokens": 6,
            "output_tokens": 16231,
            "cache_creation_input_tokens": 12400,
            "cache_read_input_tokens": 46834,
        },
        modelUsage={
            "claude-opus-5": {
                "inputTokens": 6,
                "outputTokens": 16231,
                "cacheCreationInputTokens": 12400,
                "cacheReadInputTokens": 46834,
                "canonicalModel": "claude-opus-5",
                "provider": "firstParty",
            },
            "claude-haiku-4-5-20251001": {
                "inputTokens": 900,
                "outputTokens": 40,
                "cacheCreationInputTokens": 0,
                "cacheReadInputTokens": 0,
                "canonicalModel": "claude-haiku-4-5-20251001",
                "provider": "firstParty",
            },
        },
        stop_reason="end_turn",
        duration_api_ms=313082,
    )
    _install(monkeypatch, FakeClaudeCli(stdout=_stream(*events)))
    result = ClaudeCodeTranslationProvider(preflight_report=REPORT).execute(_request(model="opus"))

    assert result.model == "claude-opus-5"
    assert result.response_id == SESSION_ID
    assert (result.usage.input_tokens, result.usage.output_tokens) == (6, 16231)
    assert result.usage.total_tokens == 6 + 16231 + 12400 + 46834
    sanitized = result.sanitized_response
    assert sanitized["model_requested"] == "opus"
    assert sanitized["model_returned"] == "claude-opus-5"
    assert sanitized["canonical_model"] == "claude-opus-5"
    assert sanitized["model_inference_provider"] == "firstParty"
    assert sanitized["stop_reason"] == "end_turn"
    assert sanitized["num_turns"] == 2
    assert sanitized["duration_api_ms"] == 313082
    assert sanitized["adapter_version"] == CLAUDE_CODE_ADAPTER_VERSION
    assert sanitized["auth_method"] == "claude.ai" and sanitized["api_provider"] == "firstParty"


def test_sanitized_response_carries_no_translated_prose(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeClaudeCli(stdout=_stream(*_two_turn_events())))
    result = ClaudeCodeTranslationProvider(preflight_report=REPORT).execute(_request())

    serialized = json.dumps(result.sanitized_response, ensure_ascii=False)
    assert "result" not in result.sanitized_response
    assert "[[P" not in serialized and NUDGE[:20] not in serialized
    assert not any("؀" <= character <= "ۿ" for character in serialized)
    assert all(
        isinstance(value, (str, int, bool)) or value is None
        for value in result.sanitized_response.values()
    )


# --------------------------------------------------------------------------------------------
# R / S. Executor: reconstructed output is judged exactly like any other candidate
# --------------------------------------------------------------------------------------------

FIRST = "آلفا بتا."
SECOND = "گاما دلتا."
EXEC_TURN_ONE = f"{marker_for(1)}\n{FIRST[:4]}"
EXEC_TURN_TWO = f"{FIRST[4:]}\n\n{marker_for(2)}\n{SECOND}\n"
EXEC_TURN_TWO_UNMARKED = f"{FIRST[4:]}\n\n{SECOND}\n"


def _claude_marked_plan(root: Path) -> tuple[Any, str]:
    run_id = _marked_run(root)
    _allow_claude_code(root)
    claim = claim_job(
        run_id,
        project_root=root,
        worker_id="worker",
        lease_seconds=900,
        now=_clock(0),
        token_factory=lambda: "secret-claim-token",
    )
    plan = plan_execution(
        run_id,
        claim.job.job_id,
        project_root=root,
        worker_id="worker",
        claim_token=claim.claim_token,
        provider=ProviderName.CLAUDE_CODE,
        model=None,
        timeout_seconds=120,
        max_output_tokens=1000,
        now=_clock(10),
    )
    return plan, claim.claim_token


def _execute_stream(root: Path, monkeypatch: pytest.MonkeyPatch, second: str) -> tuple[Any, Any]:
    plan, token = _claude_marked_plan(root)
    stream = _stream(
        _system_init(),
        _assistant(EXEC_TURN_ONE, message_id="msg_01", uuid="a-1", stop_reason="max_tokens"),
        _user(NUDGE),
        _assistant(second, message_id="msg_02", uuid="a-2"),
        _payload(result=second, num_turns=2),
    )
    _install(monkeypatch, FakeClaudeCli(stdout=stream))
    execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        now=_clock(10),
        monotonic=lambda: 1.0,
    )
    attempt = list_attempts(plan.job.run_id, plan.job.job_id, project_root=root)[0].attempt
    return plan, attempt


def test_conformant_multi_turn_output_becomes_the_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, attempt = _execute_stream(tmp_path / "project", monkeypatch, EXEC_TURN_TWO)

    assert attempt.status is TranslationAttemptStatus.SUCCEEDED
    assert attempt.provider is ProviderName.CLAUDE_CODE
    assert attempt.provider_adapter_version == CLAUDE_CODE_ADAPTER_VERSION
    translation = (plan.attempt_directory / "translation.md").read_bytes()
    assert translation == f"{FIRST}\n\n{SECOND}\n".encode()
    assert not (plan.attempt_directory / "rejected-output.md").exists()
    assert attempt.marker_findings == []
    response = json.loads((plan.attempt_directory / "response.json").read_text(encoding="utf-8"))
    assert response["num_turns"] == 2 and response["assistant_message_count"] == 2
    assert FIRST not in json.dumps(response, ensure_ascii=False)


def test_multi_turn_output_that_breaks_the_contract_is_kept_as_rejected_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    plan, attempt = _execute_stream(root, monkeypatch, EXEC_TURN_TWO_UNMARKED)
    rejected = plan.attempt_directory / "rejected-output.md"

    assert attempt.status is TranslationAttemptStatus.FAILED
    assert attempt.error_category is ProviderErrorCategory.MALFORMED_RESPONSE
    assert not (plan.attempt_directory / "translation.md").exists()
    assert rejected.read_bytes() == (EXEC_TURN_ONE + EXEC_TURN_TWO_UNMARKED).encode("utf-8")
    assert attempt.rejected_output_sha256 == sha256_bytes(rejected.read_bytes())
    assert [(finding.code, finding.expected_marker) for finding in attempt.marker_findings] == [
        ("marker_missing", marker_for(2))
    ]
    assert attempt.expected_marker_count == 2 and attempt.observed_marker_count == 1
    private = "".join(
        path.read_text(encoding="utf-8")
        for path in plan.attempt_directory.iterdir()
        if path.name != "rejected-output.md"
    )
    assert FIRST not in private and SECOND not in private


# --------------------------------------------------------------------------------------------
# T / U. Preflight and the other providers
# --------------------------------------------------------------------------------------------


def test_preflight_requires_the_verbose_stream_json_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "--verbose" in REQUIRED_CLI_FLAGS
    monkeypatch.setattr(subprocess, "run", FakeClaudeCli())
    assert preflight_provider(ProviderName.CLAUDE_CODE).passed

    fake = FakeClaudeCli()
    fake.help_output = fake.help_output.replace(b"stream-json", b"")
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(ProviderExecutionError) as caught:
        preflight_provider(ProviderName.CLAUDE_CODE)
    assert caught.value.classification.category is (
        ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER
    )
    assert "stream-json" in caught.value.classification.message


def test_other_providers_are_untouched() -> None:
    assert (CODEX_CLI_ADAPTER_VERSION, OPENAI_RESPONSES_ADAPTER_VERSION, FAKE_PROVIDER_VERSION) == (
        "1.0.0",
        "1.0.0",
        "1.0.0",
    )
    fake_result = FakeTranslationProvider().execute(_request())
    assert len(fake_result.text_outputs) == 1 and fake_result.status == "completed"
    for module in ("codex_cli", "openai_responses", "fake"):
        source = (
            Path(__file__).parents[1]
            / "src/book_translator/provider_execution/providers"
            / f"{module}.py"
        ).read_text(encoding="utf-8")
        assert "claude_stream" not in source and "stream-json" not in source


# --------------------------------------------------------------------------------------------
# Failing exits still classify from the final result object of a stream
# --------------------------------------------------------------------------------------------


def test_final_result_object_is_found_in_a_stream_or_a_bare_document() -> None:
    stream = _stream(
        _system_init(),
        _assistant(PERSIAN),
        _payload(is_error=True, subtype="error_during_execution"),
    )
    found = final_result_object(stream.decode("utf-8"))
    assert found is not None and found["subtype"] == "error_during_execution"
    assert final_result_object(json.dumps(_payload(), indent=2)) is not None
    assert final_result_object(json.dumps(_system_init())) is None
    assert final_result_object("banner\n") is None


def test_failing_exit_classifies_from_the_stream_result_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _stream(
        _system_init(),
        _payload(
            is_error=True,
            subtype="error_during_execution",
            api_error_status=429,
            result="",
            errors=["rate_limit_error: slow down"],
        ),
    )
    _install(monkeypatch, FakeClaudeCli(returncode=1, stdout=stream, stderr=b"noise"))
    with pytest.raises(ProviderExecutionError) as caught:
        ClaudeCodeTranslationProvider(preflight_report=REPORT).execute(_request())

    classification = caught.value.classification
    assert classification.category is ProviderErrorCategory.RATE_LIMIT
    assert classification.retryable
    assert "api_error_status=429" in classification.message
    assert "rate_limit_error" in classification.message


# --------------------------------------------------------------------------------------------
# Alignment QA: a carried marker finding keeps only the bare token
# --------------------------------------------------------------------------------------------


def test_alignment_reduces_a_quoted_malformed_marker_line_to_its_token() -> None:
    finding = MarkerFinding(
        code="marker_malformed",
        severity="error",
        expected_marker=marker_for(2),
        observed_marker="[[P2]] گاما دلتا و متن",
        source_paragraph_index=2,
        message="a marker-like token shares its line with other text",
    )
    assessment = assess_alignment(
        "Alpha beta.\n\nGamma delta.\n",
        "آلفا بتا.\n\nگاما دلتا.\n",
        policy=EN_FA_V1,
        contract_mode=MARKED,
        marker_findings=(finding,),
    )
    carried = [item for item in assessment.findings if item.code == "marker_malformed"]
    assert [item.observed_marker for item in carried] == ["[[P2]]"]
    assert "گاما" not in json.dumps([asdict(item) for item in carried], ensure_ascii=False)
