"""Fail-closed parsing for literary editor, correction, and continuity responses."""

import json
import re

from pydantic import ValidationError

from book_translator.models import (
    ContinuityReviewResponse,
    LiteraryCorrectionResponse,
    LiteraryEditorResponse,
)

EDITOR_SCHEMA_VERSION = "1.0.1"
CORRECTION_SCHEMA_VERSION = "1.0.0"
CONTINUITY_SCHEMA_VERSION = "1.0.1"

_FENCE = re.compile(r"^```(?:json)?\s*\n(?P<body>.*)\n```\s*$", re.DOTALL)


class LiteraryResponseError(ValueError):
    """Raised when provider output cannot be attached to the requested identities."""

    def __init__(self, message: str, *, code: str = "contract_violation") -> None:
        super().__init__(message)
        self.code = code


def _payload(text: str) -> object:
    stripped = text.strip()
    match = _FENCE.fullmatch(stripped)
    if match is not None:
        stripped = match.group("body")
    if not stripped:
        raise LiteraryResponseError("provider returned an empty response", code="empty_response")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise LiteraryResponseError(
            "provider response was not strict JSON", code="malformed_json"
        ) from exc


def parse_editor_response(
    text: str,
    *,
    expected_ids: tuple[str, ...],
    faithful_texts: tuple[str, ...],
) -> LiteraryEditorResponse:
    """Parse a whole editor answer and prove identity plus ``changed`` truthfulness."""
    try:
        response = LiteraryEditorResponse.model_validate(_payload(text))
    except ValidationError as exc:
        raise LiteraryResponseError(f"editor response violated its contract: {exc}") from exc
    if response.editor_schema_version != EDITOR_SCHEMA_VERSION:
        raise LiteraryResponseError(
            "editor schema version mismatch", code="schema_version_mismatch"
        )
    observed = tuple(item.paragraph_id for item in response.paragraphs)
    if observed != expected_ids:
        raise LiteraryResponseError(
            "editor response paragraph identities are missing, extra, or out of order",
            code="paragraph_identity_mismatch",
        )
    if len(faithful_texts) != len(expected_ids):
        raise LiteraryResponseError("internal faithful paragraph mapping mismatch")
    normalized = []
    for item, faithful in zip(response.paragraphs, faithful_texts, strict=True):
        actual_changed = item.edited_text != faithful
        if item.changed == actual_changed:
            normalized.append(item)
            continue
        normalized.append(
            item.model_copy(
                update={
                    "changed": actual_changed,
                    "reasons": [] if actual_changed else ["no_change"],
                    "changed_flag_normalized": True,
                }
            )
        )
    if any(item.changed_flag_normalized for item in normalized):
        response = response.model_copy(update={"paragraphs": normalized})
    expected = set(expected_ids)
    for request in response.multi_paragraph_edit_requests:
        if not set(request.paragraph_ids).issubset(expected):
            raise LiteraryResponseError(
                "governed multi-paragraph request names an unknown identity",
                code="paragraph_identity_mismatch",
            )
    return response


def parse_correction_response(
    text: str,
    *,
    expected_ids: tuple[str, ...],
) -> LiteraryCorrectionResponse:
    try:
        response = LiteraryCorrectionResponse.model_validate(_payload(text))
    except ValidationError as exc:
        raise LiteraryResponseError(f"correction response violated its contract: {exc}") from exc
    if response.correction_schema_version != CORRECTION_SCHEMA_VERSION:
        raise LiteraryResponseError(
            "correction schema version mismatch", code="schema_version_mismatch"
        )
    if tuple(item.paragraph_id for item in response.paragraphs) != expected_ids:
        raise LiteraryResponseError(
            "correction response changed its governed identity set",
            code="paragraph_identity_mismatch",
        )
    return response


def parse_continuity_response(
    text: str,
    *,
    known_ids: frozenset[str],
) -> ContinuityReviewResponse:
    try:
        response = ContinuityReviewResponse.model_validate(_payload(text))
    except ValidationError as exc:
        raise LiteraryResponseError(f"continuity response violated its contract: {exc}") from exc
    if response.continuity_schema_version != CONTINUITY_SCHEMA_VERSION:
        raise LiteraryResponseError(
            "continuity schema version mismatch", code="schema_version_mismatch"
        )
    if any(not set(item.paragraph_ids).issubset(known_ids) for item in response.findings):
        raise LiteraryResponseError(
            "continuity response names an unknown paragraph identity",
            code="paragraph_identity_mismatch",
        )
    return response
