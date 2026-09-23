"""The judge's provider boundary: a separate role over the same audited transport.

The Semantic Coverage Judge is a different question asked of a provider, not a different way of
reaching one. It therefore reuses the existing translation transport — the same authenticated
local Claude Code or Codex CLI subprocess, with the same stripped credentials, the same ephemeral
read-only working directory, and the same sanitized result handling — and supplies its own system
and user messages. Nothing of the translator's role travels with it: the judge never receives the
translation prompt, the style guide, the translator's instructions, or any earlier judgement.

What this layer adds is a strict answer type. A transport returns text; a judge must return a
validated verdict for exactly the paragraphs it was asked about, so the parse happens here and a
response that fails it is a provider failure rather than a low-confidence result.
"""

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from book_translator.models import SCHEMA_VERSION
from book_translator.models.semantic_qa import (
    SemanticJudgeRequestRecord,
    SemanticJudgeResponse,
)
from book_translator.provider_execution.protocol import (
    ProviderRequest,
    ProviderResult,
    TranslationProvider,
)
from book_translator.semantic_qa.parsing import SemanticResponseError, parse_judge_response
from book_translator.semantic_qa.rendering import (
    JUDGE_SCHEMA_VERSION,
    SYSTEM_MESSAGE,
)

#: The judge's own output contract identifier, kept distinct from the translation contract so an
#: artifact can never be read as though it were a translation request.
JUDGE_OUTPUT_CONTRACT = "semantic-coverage-verdicts/1.0.0"

__all__ = [
    "JUDGE_OUTPUT_CONTRACT",
    "FakeSemanticJudgeProvider",
    "JudgeCall",
    "SemanticJudgeError",
    "TransportSemanticJudge",
    "judge_request_id",
]


class SemanticJudgeError(RuntimeError):
    """Raised when a judge call did not produce a trustworthy structured answer."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def judge_request_id(
    *,
    run_id: str,
    chunk_id: str,
    paragraph_ids: tuple[str, ...],
    repetition: int,
    system_message: str,
    user_message: str,
) -> str:
    """Derive a stable identifier for one judge call from exactly what it asks.

    The repetition index participates, which is what lets the same paragraphs be judged twice
    without the two calls colliding on identity — the independence experiment needs both answers
    to exist side by side and to be told apart.
    """
    material = "\n".join(
        (
            JUDGE_OUTPUT_CONTRACT,
            run_id,
            chunk_id,
            ",".join(paragraph_ids),
            str(repetition),
            _digest(system_message),
            _digest(user_message),
        )
    )
    return f"sjq-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


@dataclass(frozen=True, slots=True)
class JudgeCall:
    """One completed judge call: its validated answer and everything provable about the call."""

    response: SemanticJudgeResponse
    record: SemanticJudgeRequestRecord


class TransportSemanticJudge:
    """Ask a translation-transport provider the judge's question and validate its answer."""

    def __init__(
        self,
        provider: TranslationProvider,
        *,
        model: str | None = None,
        timeout_seconds: int = 600,
        max_output_tokens: int = 8000,
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

    def judge(
        self,
        *,
        run_id: str,
        job_id: str,
        chunk_id: str,
        user_message: str,
        paragraph_ids: tuple[str, ...],
        source_language: str,
        target_language: str,
        repetition: int,
        batch_size: int,
    ) -> JudgeCall:
        request_id = judge_request_id(
            run_id=run_id,
            chunk_id=chunk_id,
            paragraph_ids=paragraph_ids,
            repetition=repetition,
            system_message=SYSTEM_MESSAGE,
            user_message=user_message,
        )
        request = ProviderRequest(
            request_id=request_id,
            idempotency_key=request_id,
            run_id=run_id,
            job_id=job_id,
            # The judge spends no attempt slot and owns no queue accounting; the transport needs
            # an attempt number and this one is deliberately constant and meaningless.
            attempt_number=1,
            system_message=SYSTEM_MESSAGE,
            user_message=user_message,
            source_language=source_language,
            target_language=target_language,
            output_contract=JUDGE_OUTPUT_CONTRACT,
            model=self._model,
            timeout_seconds=self._timeout_seconds,
            max_output_tokens=self._max_output_tokens,
            metadata={
                "run_id": run_id,
                "job_id": job_id,
                "provider": self._provider.provider_name,
                "execution_kind": "semantic_judge",
            },
        )
        started = self._monotonic()
        try:
            result: ProviderResult = self._provider.execute(request)
        except Exception as exc:
            # The transport raises its own classified failure types, and this boundary owns none
            # of them; wrapping preserves the cause while keeping one error type for callers.
            raise SemanticJudgeError(
                f"semantic judge provider call failed ({type(exc).__name__})",
                code="provider_failure",
                retryable=True,
            ) from exc
        latency = max(0, round((self._monotonic() - started) * 1000))
        if result.status != "completed" or result.incomplete_reason:
            raise SemanticJudgeError(
                "semantic judge response was incomplete", code="incomplete", retryable=True
            )
        outputs = tuple(text for text in result.text_outputs if text.strip())
        if len(outputs) != 1:
            raise SemanticJudgeError(
                "semantic judge returned no single text output",
                code="malformed_response",
                retryable=True,
            )
        try:
            response = parse_judge_response(outputs[0], expected_ids=paragraph_ids)
        except SemanticResponseError as exc:
            raise SemanticJudgeError(
                f"semantic judge output was rejected: {exc}",
                code=exc.code,
                # A contract-shaped failure is worth one more ask; an identity failure is the
                # judge answering a different question and is not made better by repetition.
                retryable=exc.code in {"malformed_json", "empty_response", "contract_violation"},
            ) from exc
        record = SemanticJudgeRequestRecord(
            request_id=request_id,
            batch_size=batch_size,
            repetition=repetition,
            provider=self._provider.provider_name,
            adapter_version=self._provider.adapter_version,
            model_requested=self._model,
            # Never invented: a transport that does not report the model it used records nothing
            # rather than an assumption that the requested model is the one that answered.
            model_returned=result.model,
            system_message_sha256=_digest(SYSTEM_MESSAGE),
            user_message_sha256=_digest(user_message),
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            total_tokens=result.usage.total_tokens,
            latency_milliseconds=latency,
        )
        return JudgeCall(response, record)


class FakeSemanticJudgeProvider:
    """A local scripted transport for tests.

    It never inspects prose and never leaves the process.
    """

    provider_name = "fake"
    adapter_version = "1.0.0"

    def __init__(self, texts: tuple[str, ...], calls: list[str] | None = None) -> None:
        self.texts = texts
        self.calls = calls if calls is not None else []

    def execute(self, request: ProviderRequest) -> ProviderResult:
        index = min(len(self.calls), len(self.texts) - 1)
        self.calls.append(request.request_id)
        text = self.texts[index]
        if text == "__raise__":
            raise RuntimeError("synthetic judge transport failure")
        return ProviderResult(
            response_id="synthetic-judge-response",
            model=request.model or "fake-judge-v1",
            status="completed",
            text_outputs=(text,),
        )


def passing_response(paragraph_ids: tuple[str, ...]) -> str:
    """Render a conformant all-PASS judge answer, used by tests and the fake provider."""
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "judge_schema_version": JUDGE_SCHEMA_VERSION,
            "verdicts": [
                {
                    "paragraph_id": identifier,
                    "verdict": "PASS",
                    "confidence": 0.9,
                    "findings": [],
                }
                for identifier in paragraph_ids
            ],
        },
        ensure_ascii=False,
    )
