"""The reviser's provider role over the same audited transport the judge uses."""

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from book_translator.models.semantic_qa import SemanticJudgeRequestRecord
from book_translator.provider_execution.protocol import (
    ProviderRequest,
    ProviderResult,
    TranslationProvider,
)
from book_translator.semantic_revision.rendering import REVISER_SYSTEM_MESSAGE

REVISER_OUTPUT_CONTRACT = "semantic-revision-marked-window/1.0.0"

__all__ = [
    "REVISER_OUTPUT_CONTRACT",
    "ReviserCall",
    "SemanticReviserError",
    "TransportSemanticReviser",
]


class SemanticReviserError(RuntimeError):
    """Raised when a reviser call did not return a usable answer."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviserCall:
    """One completed reviser call: the raw marked window it returned and the call's record."""

    text: str
    record: SemanticJudgeRequestRecord


class TransportSemanticReviser:
    """Ask a translation-transport provider to correct a bounded window."""

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

    def revise(
        self,
        *,
        run_id: str,
        job_id: str,
        chunk_id: str,
        user_message: str,
        window_indexes: tuple[int, ...],
        source_language: str,
        target_language: str,
        attempt: int,
    ) -> ReviserCall:
        material = "\n".join(
            (
                REVISER_OUTPUT_CONTRACT,
                run_id,
                chunk_id,
                ",".join(str(index) for index in window_indexes),
                str(attempt),
                _digest(REVISER_SYSTEM_MESSAGE),
                _digest(user_message),
            )
        )
        request_id = f"sjq-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"
        request = ProviderRequest(
            request_id=request_id,
            idempotency_key=request_id,
            run_id=run_id,
            job_id=job_id,
            attempt_number=1,
            system_message=REVISER_SYSTEM_MESSAGE,
            user_message=user_message,
            source_language=source_language,
            target_language=target_language,
            output_contract=REVISER_OUTPUT_CONTRACT,
            model=self._model,
            timeout_seconds=self._timeout_seconds,
            max_output_tokens=self._max_output_tokens,
            metadata={
                "run_id": run_id,
                "job_id": job_id,
                "provider": self._provider.provider_name,
                "execution_kind": "semantic_reviser",
            },
        )
        started = self._monotonic()
        try:
            result: ProviderResult = self._provider.execute(request)
        except Exception as exc:
            raise SemanticReviserError(
                f"semantic reviser provider call failed ({type(exc).__name__})",
                code="provider_failure",
                retryable=True,
            ) from exc
        latency = max(0, round((self._monotonic() - started) * 1000))
        if result.status != "completed" or result.incomplete_reason:
            raise SemanticReviserError(
                "semantic reviser response was incomplete", code="incomplete", retryable=True
            )
        outputs = tuple(text for text in result.text_outputs if text.strip())
        if len(outputs) != 1:
            raise SemanticReviserError(
                "semantic reviser returned no single text output",
                code="malformed_response",
                retryable=True,
            )
        record = SemanticJudgeRequestRecord(
            request_id=request_id,
            batch_size=len(window_indexes),
            repetition=attempt,
            provider=self._provider.provider_name,
            adapter_version=self._provider.adapter_version,
            model_requested=self._model,
            model_returned=result.model,
            system_message_sha256=_digest(REVISER_SYSTEM_MESSAGE),
            user_message_sha256=_digest(user_message),
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            total_tokens=result.usage.total_tokens,
            latency_milliseconds=latency,
        )
        return ReviserCall(outputs[0].replace("\r\n", "\n"), record)
