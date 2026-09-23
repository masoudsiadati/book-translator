"""Strict, fail-closed validation of a judge response against the paragraphs it was asked about.

Everything here exists because a structured-output contract that is only *mostly* enforced is
worse than none: a response answering a paragraph nobody asked about, or silently dropping one,
would attach a verdict to the wrong identity, which is the same class of defect the numbered
paragraph contract was built to remove one layer down.

The parser therefore refuses rather than repairs. It does not guess which paragraph an unknown id
meant, does not treat a missing verdict as a pass, and does not accept a second answer for an
identity already answered.
"""

import json
import re

from pydantic import ValidationError

from book_translator.models import SCHEMA_VERSION
from book_translator.models.semantic_qa import SemanticJudgeResponse
from book_translator.semantic_qa.rendering import JUDGE_SCHEMA_VERSION

__all__ = ["SemanticResponseError", "parse_judge_response"]

_FENCE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL)


class SemanticResponseError(ValueError):
    """Raised when a judge response cannot be trusted to answer the question that was asked."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _decode(text: str) -> object:
    """Return the JSON object a response carries, tolerating only a wrapping code fence.

    A fence is tolerated because it is a formatting habit rather than a content defect. Anything
    else — a preamble, a trailing remark, two objects — is refused, because a response that
    decided to say more than the contract allows has already stopped following it.
    """
    candidate = text.strip()
    fenced = _FENCE.match(candidate)
    if fenced is not None:
        candidate = fenced.group("body").strip()
    if not candidate:
        raise SemanticResponseError("judge response was empty", code="empty_response")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise SemanticResponseError(
            f"judge response was not a single JSON object ({exc.msg})", code="malformed_json"
        ) from exc


def parse_judge_response(text: str, *, expected_ids: tuple[str, ...]) -> SemanticJudgeResponse:
    """Validate one judge response against the exact paragraph identities it was asked about."""
    if not expected_ids:
        raise SemanticResponseError("no paragraph was requested", code="empty_request")
    payload = _decode(text)
    if not isinstance(payload, dict):
        raise SemanticResponseError("judge response was not a JSON object", code="malformed_json")
    try:
        response = SemanticJudgeResponse.model_validate(payload)
    except ValidationError as exc:
        raise SemanticResponseError(
            f"judge response violated the output contract ({exc.error_count()} errors)",
            code="contract_violation",
        ) from exc
    if response.schema_version != SCHEMA_VERSION:
        raise SemanticResponseError(
            "judge response declared an unexpected schema version", code="schema_version_mismatch"
        )
    if response.judge_schema_version != JUDGE_SCHEMA_VERSION:
        raise SemanticResponseError(
            "judge response declared an unexpected judge schema version",
            code="judge_schema_version_mismatch",
        )
    answered = [item.paragraph_id for item in response.verdicts]
    expected = set(expected_ids)
    unknown = sorted(set(answered) - expected)
    if unknown:
        raise SemanticResponseError(
            f"judge answered {len(unknown)} paragraph identity it was not given",
            code="unknown_paragraph",
        )
    missing = sorted(expected - set(answered))
    if missing:
        raise SemanticResponseError(
            f"judge did not answer {len(missing)} requested paragraph",
            code="missing_paragraph",
        )
    if len(answered) != len(set(answered)):
        raise SemanticResponseError(
            "judge answered a paragraph more than once", code="duplicate_paragraph"
        )
    return response
