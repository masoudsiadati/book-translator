"""Role-separated structured literary calls over the audited translation transport."""

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from book_translator.models import (
    ContinuityReviewResponse,
    LiteraryCallRole,
    LiteraryCorrectionResponse,
    LiteraryEditorResponse,
    LiteraryProviderCall,
)
from book_translator.provider_execution.errors import ProviderExecutionError
from book_translator.provider_execution.protocol import (
    ProviderRequest,
    ProviderResult,
    TranslationProvider,
)

from .parsing import (
    CONTINUITY_SCHEMA_VERSION,
    CORRECTION_SCHEMA_VERSION,
    EDITOR_SCHEMA_VERSION,
    LiteraryResponseError,
    parse_continuity_response,
    parse_correction_response,
    parse_editor_response,
)
from .policy import LITERARY_CORRECTION_POLICY_VERSION, LITERARY_EDIT_POLICY_VERSION
from .rendering import (
    CONTINUITY_PROMPT_VERSION,
    CONTINUITY_SYSTEM_MESSAGE,
    CORRECTION_PROMPT_VERSION,
    CORRECTION_SYSTEM_MESSAGE,
    EDITOR_PROMPT_VERSION,
    EDITOR_SYSTEM_MESSAGE,
)

EDITOR_OUTPUT_CONTRACT = "persian-literary-edit/1.0.0"
CORRECTION_OUTPUT_CONTRACT = "persian-literary-correction/1.0.0"
CONTINUITY_OUTPUT_CONTRACT = "persian-literary-continuity-findings/1.0.0"


class LiteraryProviderError(RuntimeError):
    """Raised when a literary role did not return a trustworthy structured result."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        record: LiteraryProviderCall | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.record = record


@dataclass(frozen=True, slots=True)
class LiteraryEditorCall:
    response: LiteraryEditorResponse
    record: LiteraryProviderCall


@dataclass(frozen=True, slots=True)
class LiteraryCorrectionCall:
    response: LiteraryCorrectionResponse
    record: LiteraryProviderCall


@dataclass(frozen=True, slots=True)
class ContinuityCall:
    response: ContinuityReviewResponse
    record: LiteraryProviderCall


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _request_id(prefix: str, *parts: str) -> str:
    material = "\n".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:24]}"


class TransportLiteraryRoles:
    """Ask editor, correction, and continuity questions without merging their contracts."""

    def __init__(
        self,
        provider: TranslationProvider,
        *,
        model: str | None = None,
        timeout_seconds: int = 1200,
        max_output_tokens: int = 32000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._monotonic = monotonic

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name

    @property
    def adapter_version(self) -> str:
        return self._provider.adapter_version

    @property
    def model_requested(self) -> str | None:
        return self._model

    def _execute(
        self,
        *,
        prefix: str,
        role: LiteraryCallRole,
        run_id: str,
        chapter_id: str,
        sequence: int,
        system_message: str,
        user_message: str,
        output_contract: str,
        prompt_version: str,
        schema_version: str,
        policy_version: str,
    ) -> tuple[str, LiteraryProviderCall]:
        request_id = _request_id(
            prefix,
            run_id,
            chapter_id,
            str(sequence),
            role.value,
            _digest(system_message),
            _digest(user_message),
        )
        request = ProviderRequest(
            request_id=request_id,
            idempotency_key=request_id,
            run_id=run_id,
            job_id=f"{run_id}__chapter-{chapter_id}",
            attempt_number=sequence,
            system_message=system_message,
            user_message=user_message,
            source_language="en",
            target_language="fa",
            output_contract=output_contract,
            model=self._model,
            timeout_seconds=self._timeout_seconds,
            max_output_tokens=self._max_output_tokens,
            metadata={
                "run_id": run_id,
                "chapter_id": chapter_id,
                "execution_kind": role.value,
                "provider": self.provider_name,
            },
        )
        started = self._monotonic()
        try:
            result: ProviderResult = self._provider.execute(request)
        except ProviderExecutionError as exc:
            category = exc.classification.category.value
            raise LiteraryProviderError(
                f"{role.value} provider call failed: {category}: {exc.classification.message}",
                code=category,
                retryable=exc.classification.retryable and exc.classification.safe_automatic_retry,
            ) from exc
        except Exception as exc:
            raise LiteraryProviderError(
                f"{role.value} provider call failed ({type(exc).__name__})",
                code="provider_failure",
                retryable=True,
            ) from exc
        latency = max(0, round((self._monotonic() - started) * 1000))
        if result.status != "completed" or result.incomplete_reason:
            raise LiteraryProviderError(
                f"{role.value} provider response was incomplete",
                code="incomplete",
                retryable=True,
            )
        outputs = tuple(item for item in result.text_outputs if item.strip())
        if len(outputs) != 1:
            raise LiteraryProviderError(
                f"{role.value} returned no single text output",
                code="malformed_response",
                retryable=True,
            )
        record = LiteraryProviderCall(
            role=role,
            request_id=request_id,
            provider=self.provider_name,
            adapter_version=self.adapter_version,
            model_requested=self._model,
            model_returned=result.model,
            system_message_sha256=_digest(system_message),
            user_message_sha256=_digest(user_message),
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            total_tokens=result.usage.total_tokens,
            latency_milliseconds=latency,
            prompt_version=prompt_version,
            schema_version_used=schema_version,
            policy_version=policy_version,
        )
        return outputs[0], record

    def edit(
        self,
        *,
        run_id: str,
        chapter_id: str,
        sequence: int,
        user_message: str,
        expected_ids: tuple[str, ...],
        faithful_texts: tuple[str, ...],
    ) -> LiteraryEditorCall:
        text, record = self._execute(
            prefix="leq",
            role=LiteraryCallRole.EDITOR,
            run_id=run_id,
            chapter_id=chapter_id,
            sequence=sequence,
            system_message=EDITOR_SYSTEM_MESSAGE,
            user_message=user_message,
            output_contract=EDITOR_OUTPUT_CONTRACT,
            prompt_version=EDITOR_PROMPT_VERSION,
            schema_version=EDITOR_SCHEMA_VERSION,
            policy_version=LITERARY_EDIT_POLICY_VERSION,
        )
        try:
            response = parse_editor_response(
                text, expected_ids=expected_ids, faithful_texts=faithful_texts
            )
        except LiteraryResponseError as exc:
            rejected = record.model_copy(
                update={"status": "output_rejected", "error_code": exc.code}
            )
            raise LiteraryProviderError(
                f"literary editor output was rejected: {exc}",
                code=exc.code,
                retryable=exc.code in {"empty_response", "malformed_json", "contract_violation"},
                record=rejected,
            ) from exc
        return LiteraryEditorCall(response, record)

    def correct(
        self,
        *,
        run_id: str,
        chapter_id: str,
        sequence: int,
        user_message: str,
        expected_ids: tuple[str, ...],
    ) -> LiteraryCorrectionCall:
        text, record = self._execute(
            prefix="crq",
            role=LiteraryCallRole.CORRECTION,
            run_id=run_id,
            chapter_id=chapter_id,
            sequence=sequence,
            system_message=CORRECTION_SYSTEM_MESSAGE,
            user_message=user_message,
            output_contract=CORRECTION_OUTPUT_CONTRACT,
            prompt_version=CORRECTION_PROMPT_VERSION,
            schema_version=CORRECTION_SCHEMA_VERSION,
            policy_version=LITERARY_CORRECTION_POLICY_VERSION,
        )
        try:
            response = parse_correction_response(text, expected_ids=expected_ids)
        except LiteraryResponseError as exc:
            rejected = record.model_copy(
                update={"status": "output_rejected", "error_code": exc.code}
            )
            raise LiteraryProviderError(
                f"literary correction output was rejected: {exc}",
                code=exc.code,
                retryable=False,
                record=rejected,
            ) from exc
        return LiteraryCorrectionCall(response, record)

    def review_continuity(
        self,
        *,
        run_id: str,
        chapter_id: str,
        sequence: int,
        user_message: str,
        known_ids: frozenset[str],
    ) -> ContinuityCall:
        text, record = self._execute(
            prefix="lcq",
            role=LiteraryCallRole.CONTINUITY,
            run_id=run_id,
            chapter_id=chapter_id,
            sequence=sequence,
            system_message=CONTINUITY_SYSTEM_MESSAGE,
            user_message=user_message,
            output_contract=CONTINUITY_OUTPUT_CONTRACT,
            prompt_version=CONTINUITY_PROMPT_VERSION,
            schema_version=CONTINUITY_SCHEMA_VERSION,
            policy_version=LITERARY_EDIT_POLICY_VERSION,
        )
        try:
            response = parse_continuity_response(text, known_ids=known_ids)
        except LiteraryResponseError as exc:
            rejected = record.model_copy(
                update={"status": "output_rejected", "error_code": exc.code}
            )
            raise LiteraryProviderError(
                f"continuity review output was rejected: {exc}",
                code=exc.code,
                retryable=exc.code in {"empty_response", "malformed_json", "contract_violation"},
                record=rejected,
            ) from exc
        return ContinuityCall(response, record)


class ScriptedLiteraryProvider:
    """Local provider double that returns responses in order and never inspects story text."""

    provider_name = "fake"
    adapter_version = "1.0.0"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.requests: list[ProviderRequest] = []

    def execute(self, request: ProviderRequest) -> ProviderResult:
        index = len(self.requests)
        self.requests.append(request)
        if index >= len(self.outputs):
            raise RuntimeError("scripted literary provider exhausted")
        return ProviderResult(
            response_id=f"fake-{index + 1}",
            model="fake-literary-v1",
            status="completed",
            text_outputs=(self.outputs[index],),
        )
