"""Official OpenAI SDK Responses API adapter."""

from typing import Any, Protocol

import openai
from openai import OpenAI

from book_translator.models import ProviderErrorCategory
from book_translator.provider_execution.errors import ErrorClassification, ProviderExecutionError
from book_translator.provider_execution.protocol import (
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)

OPENAI_RESPONSES_ADAPTER_VERSION = "1.0.0"


class ResponsesResource(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class OpenAIClientLike(Protocol):
    responses: ResponsesResource


def _classification(exc: Exception) -> ErrorClassification:
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    if isinstance(exc, openai.AuthenticationError):
        category = ProviderErrorCategory.AUTHENTICATION
        retryable = False
        ambiguous = False
    elif isinstance(exc, openai.PermissionDeniedError):
        category = ProviderErrorCategory.AUTHORIZATION
        retryable = False
        ambiguous = False
    elif isinstance(exc, openai.RateLimitError):
        category = ProviderErrorCategory.RATE_LIMIT
        retryable = True
        ambiguous = False
    elif isinstance(exc, openai.APITimeoutError):
        category = ProviderErrorCategory.TIMEOUT_AMBIGUOUS
        retryable = True
        ambiguous = True
    elif isinstance(exc, openai.APIConnectionError):
        category = ProviderErrorCategory.CONNECTION_AMBIGUOUS
        retryable = True
        ambiguous = True
    elif isinstance(exc, openai.BadRequestError):
        message = str(exc).lower()
        if "context" in message and "length" in message:
            category = ProviderErrorCategory.CONTEXT_LENGTH
        elif "model" in message or "parameter" in message:
            category = ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER
        else:
            category = ProviderErrorCategory.INVALID_REQUEST
        retryable = False
        ambiguous = False
    elif isinstance(exc, openai.InternalServerError):
        category = ProviderErrorCategory.SERVER_ERROR
        retryable = True
        ambiguous = False
    else:
        category = ProviderErrorCategory.UNKNOWN
        retryable = False
        ambiguous = True
    return ErrorClassification(
        category,
        retryable,
        ambiguous,
        False,
        f"OpenAI request failed ({category.value})",
        status,
        str(code) if code else None,
    )


class OpenAIResponsesProvider:
    def __init__(
        self,
        *,
        client: OpenAIClientLike | None = None,
        max_retries: int = 0,
        timeout_seconds: int = 600,
    ) -> None:
        self._client = client or OpenAI(max_retries=max_retries, timeout=float(timeout_seconds))

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def adapter_version(self) -> str:
        return OPENAI_RESPONSES_ADAPTER_VERSION

    def execute(self, request: ProviderRequest) -> ProviderResult:
        if request.model is None:
            raise ProviderExecutionError(
                ErrorClassification(
                    ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER,
                    False,
                    False,
                    False,
                    "OpenAI provider requires an explicitly configured model",
                )
            )
        parameters: dict[str, Any] = {
            "model": request.model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": request.system_message}],
                },
                {"role": "user", "content": [{"type": "input_text", "text": request.user_message}]},
            ],
            "max_output_tokens": request.max_output_tokens,
            "tools": [],
            "tool_choice": "none",
            "store": False,
            "metadata": request.metadata,
            "timeout": float(request.timeout_seconds),
            "extra_headers": {"Idempotency-Key": request.idempotency_key},
        }
        if request.reasoning_effort is not None:
            parameters["reasoning"] = {"effort": request.reasoning_effort}
        try:
            response = self._client.responses.create(**parameters)
        except Exception as exc:
            if isinstance(exc, openai.OpenAIError):
                raise ProviderExecutionError(_classification(exc)) from exc
            raise
        output = getattr(response, "output", [])
        texts: list[str] = []
        for item in output:
            if (
                getattr(item, "type", None) != "message"
                or getattr(item, "role", None) != "assistant"
            ):
                continue
            for content in getattr(item, "content", []):
                if getattr(content, "type", None) == "output_text":
                    value = getattr(content, "text", None)
                    if isinstance(value, str):
                        texts.append(value)
        usage = getattr(response, "usage", None)
        incomplete = getattr(response, "incomplete_details", None)
        incomplete_reason = getattr(incomplete, "reason", None) if incomplete else None
        response_id = str(getattr(response, "id", ""))
        model = str(getattr(response, "model", request.model))
        status = str(getattr(response, "status", "unknown"))
        provider_usage = ProviderUsage(
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
            getattr(usage, "total_tokens", None),
        )
        sanitized = {
            "id": response_id,
            "model": model,
            "status": status,
            "output": texts,
            "usage": {
                "input_tokens": provider_usage.input_tokens,
                "output_tokens": provider_usage.output_tokens,
                "total_tokens": provider_usage.total_tokens,
            },
            "incomplete_reason": incomplete_reason,
        }
        return ProviderResult(
            response_id,
            model,
            status,
            tuple(texts),
            provider_usage,
            incomplete_reason,
            sanitized,
        )
