"""Pure reconstruction of a Claude Code ``stream-json`` transcript into one assistant text.

Why this exists.  In ``--output-format json`` the CLI's final ``result`` field is not the
session's answer: it is the text of the last content block of the *last* assistant message, and
the CLI's engine resets it to the empty string on every ``user`` message that flows through the
turn loop.  When a response hits the output token limit, Claude Code injects a synthetic user
nudge ("Output token limit hit. Resume directly ...") and asks the model to continue, which is
reported as ``num_turns: 2`` — and the earlier turn's text is gone from ``result``.  The
``stream-json`` format writes every message the engine yields, one JSON object per line, so the
whole session can be reconstructed here without trusting ``result``.

Verified against the installed CLI (2.1.272), each line is one object with a ``type``:

- ``system`` (``init``, status, compaction boundaries, ...): never translation.
- ``user``: the initial prompt is not echoed; what appears are the engine's own injected turns
  (continuation nudges, tool results).  Never translation, but each one is one turn.
- ``assistant``: one *complete* content-block snapshot.  While streaming, the CLI emits one
  ``assistant`` event per finished content block, all sharing ``message.id``; the non-streaming
  fallback emits one event with every block.  Text blocks are the answer; ``thinking``,
  ``redacted_thinking``, and ``tool_use`` blocks are not.  ``parent_tool_use_id`` is ``null`` for
  the primary conversation and set for subagent output; ``session_id`` is stamped on every event.
- ``stream_event``: raw API deltas, only with ``--include-partial-messages``; ignored so nothing is
  ever counted twice.
- ``result``: exactly one, last; carries ``num_turns`` (1 plus the number of ``user`` events the
  engine saw), usage, model usage, and the lossy ``result`` text used here only as a cross-check.

The parser is pure: no I/O, no clock, no provider knowledge.  It fails closed on anything it
cannot reconstruct deterministically and never quotes prose in an error.
"""

import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ClaudeAssistantMessage",
    "ClaudeStreamError",
    "ClaudeStreamResult",
    "final_result_object",
    "parse_claude_stream",
]

_TEXT_BLOCK = "text"


class ClaudeStreamError(ValueError):
    """The transcript cannot be reconstructed safely.  Messages are structural and prose-free."""


@dataclass(frozen=True, slots=True)
class ClaudeAssistantMessage:
    """One primary-session API message: its text blocks joined in stream order."""

    message_id: str
    text: str
    model: str | None
    stop_reason: str | None
    text_block_count: int


@dataclass(frozen=True, slots=True)
class ClaudeStreamResult:
    """A reconstructed session: the final result object plus the primary assistant text."""

    result: dict[str, Any]
    session_id: str
    messages: tuple[ClaudeAssistantMessage, ...]
    text: str
    user_event_count: int
    event_count: int
    ignored_event_count: int
    duplicate_event_count: int

    @property
    def assistant_message_count(self) -> int:
        return len(self.messages)


@dataclass(slots=True)
class _Group:
    message_id: str
    texts: list[str]
    model: str | None
    stop_reason: str | None


def _events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise ClaudeStreamError(f"stream line {number} was not valid JSON") from exc
        if not isinstance(event, dict):
            raise ClaudeStreamError(f"stream line {number} was not a JSON object")
        events.append(event)
    return events


def final_result_object(stdout: str) -> dict[str, Any] | None:
    """Return the CLI's final ``result`` object from a stream or a bare JSON document, or None.

    A failing exit may leave a partial stream, a single result object, or nothing structured at
    all; this never raises, so classification of a failing exit can fall through to stderr.
    """
    try:
        events = _events(stdout)
    except ClaudeStreamError:
        events = []
    if not events:
        try:
            document = json.loads(stdout)
        except ValueError:
            return None
        events = [document] if isinstance(document, dict) else []
    results = [event for event in events if event.get("type") == "result"]
    return results[-1] if results else None


def _is_primary(event: dict[str, Any]) -> bool:
    return event.get("parent_tool_use_id") is None


def _is_synthetic(event: dict[str, Any]) -> bool:
    """A meta or API-error-carrier assistant event is the CLI talking, not the model answering."""
    return any(
        event.get(flag) is True for flag in ("is_meta", "is_api_error_message", "isApiErrorMessage")
    )


def _turn_count(result: dict[str, Any]) -> int:
    turns = result.get("num_turns")
    if isinstance(turns, bool) or not isinstance(turns, int) or turns < 0:
        raise ClaudeStreamError("result object lacks a usable num_turns")
    return turns


def _session_id(result: dict[str, Any]) -> str:
    session = result.get("session_id")
    if not isinstance(session, str) or not session:
        raise ClaudeStreamError("result object lacks a session id")
    return session


def _text_blocks(event: dict[str, Any], number: int) -> tuple[list[str], bool, dict[str, Any]]:
    """Return (text blocks, whether the last block is text, message) or refuse the shape."""
    message = event.get("message")
    if not isinstance(message, dict):
        raise ClaudeStreamError(f"assistant event {number} carries no message object")
    if not isinstance(message.get("id"), str) or not message["id"]:
        raise ClaudeStreamError(f"assistant event {number} lacks a message id")
    content = message.get("content")
    if not isinstance(content, list):
        raise ClaudeStreamError(f"assistant event {number} content is not a block list")
    texts: list[str] = []
    last_is_text = False
    for block in content:
        if not isinstance(block, dict):
            raise ClaudeStreamError(f"assistant event {number} carries a malformed content block")
        last_is_text = block.get("type") == _TEXT_BLOCK
        if last_is_text:
            text = block.get("text")
            if not isinstance(text, str):
                raise ClaudeStreamError(f"assistant event {number} text block is not a string")
            texts.append(text)
    return texts, last_is_text, message


def _fingerprint(message: dict[str, Any]) -> str:
    return json.dumps(message, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def parse_claude_stream(stdout: str) -> ClaudeStreamResult:
    """Reconstruct the primary session's assistant text from a ``stream-json`` transcript.

    Every text block of every primary, non-synthetic assistant event is appended once, in stream
    order, with no separator: a continuation after an output-limit nudge resumes exactly where
    the cut happened, and inserting anything would invent bytes the model never produced.
    Whether the joined text is a valid translation is the structural validator's question.
    """
    events = _events(stdout)
    if not events:
        raise ClaudeStreamError("stream carried no events")
    results = [index for index, event in enumerate(events) if event.get("type") == "result"]
    if len(results) != 1:
        raise ClaudeStreamError(f"stream carried {len(results)} result objects, expected one")
    result_index = results[0]
    result = events[result_index]
    turns = _turn_count(result)
    session_id = _session_id(result)

    groups: list[_Group] = []
    seen: dict[str, str] = {}
    user_events = ignored = duplicates = 0
    final_text: str | None = None
    for number, event in enumerate(events, start=1):
        kind = event.get("type")
        if kind == "result":
            continue
        if kind == "user":
            user_events += 1 if _is_primary(event) else 0
            ignored += 1
            continue
        if kind != "assistant" or not _is_primary(event) or _is_synthetic(event):
            ignored += 1
            continue
        if number - 1 > result_index:
            raise ClaudeStreamError("assistant message arrived after the result object")
        if event.get("session_id") != session_id:
            raise ClaudeStreamError(f"assistant event {number} does not belong to the session")
        uuid = event.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            raise ClaudeStreamError(f"assistant event {number} lacks an event identity")
        texts, last_is_text, message = _text_blocks(event, number)
        fingerprint = _fingerprint(message)
        if uuid in seen:
            if seen[uuid] != fingerprint:
                raise ClaudeStreamError("conflicting assistant events share one identity")
            duplicates += 1
            continue
        seen[uuid] = fingerprint
        message_id: str = message["id"]
        model = message.get("model")
        stop_reason = message.get("stop_reason")
        if groups and groups[-1].message_id == message_id:
            group = groups[-1]
            group.texts.extend(texts)
            group.model = model if isinstance(model, str) else group.model
            group.stop_reason = stop_reason if isinstance(stop_reason, str) else group.stop_reason
        elif any(group.message_id == message_id for group in groups):
            raise ClaudeStreamError("assistant message sequence is out of order")
        else:
            groups.append(
                _Group(
                    message_id,
                    list(texts),
                    model if isinstance(model, str) else None,
                    stop_reason if isinstance(stop_reason, str) else None,
                )
            )
        final_text = texts[-1] if last_is_text and texts else ""

    if len(groups) < turns:
        raise ClaudeStreamError(
            f"result reports {turns} turns but the stream carries {len(groups)} "
            "primary assistant messages"
        )
    if user_events > max(turns - 1, 0):
        raise ClaudeStreamError(
            f"stream carries {user_events} user turns but the result reports {turns} turns"
        )
    reported = result.get("result")
    if not isinstance(reported, str):
        raise ClaudeStreamError("result object lacks result text")
    # The engine derives ``result`` from the last text block of the last assistant event, so a
    # non-empty value that differs from what was reconstructed means the transcript is not the
    # session the result describes.  An empty value carries no evidence either way.
    if reported and reported != final_text:
        raise ClaudeStreamError("result text does not match the final assistant message")
    messages = tuple(
        ClaudeAssistantMessage(
            group.message_id,
            "".join(group.texts),
            group.model,
            group.stop_reason,
            len(group.texts),
        )
        for group in groups
    )
    return ClaudeStreamResult(
        result=result,
        session_id=session_id,
        messages=messages,
        text="".join(message.text for message in messages),
        user_event_count=user_events,
        event_count=len(events),
        ignored_event_count=ignored,
        duplicate_event_count=duplicates,
    )
