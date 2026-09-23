"""Deterministic offline provider for tests and workflow validation."""

import hashlib

from book_translator.models import ProviderErrorCategory
from book_translator.provider_execution.errors import ErrorClassification, ProviderExecutionError
from book_translator.provider_execution.protocol import (
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)

FAKE_PROVIDER_VERSION = "1.0.0"


class FakeTranslationProvider:
    def __init__(self, scenario: str = "success") -> None:
        self.scenario = scenario

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def adapter_version(self) -> str:
        return FAKE_PROVIDER_VERSION

    def execute(self, request: ProviderRequest) -> ProviderResult:
        failures = {
            "timeout": ErrorClassification(
                ProviderErrorCategory.TIMEOUT_AMBIGUOUS,
                True,
                True,
                False,
                "synthetic provider timeout after possible send",
            ),
            "rate_limit": ErrorClassification(
                ProviderErrorCategory.RATE_LIMIT,
                True,
                False,
                False,
                "synthetic provider rate limit",
                429,
            ),
            "authentication": ErrorClassification(
                ProviderErrorCategory.AUTHENTICATION,
                False,
                False,
                False,
                "synthetic provider authentication failure",
                401,
            ),
            "server_error": ErrorClassification(
                ProviderErrorCategory.SERVER_ERROR,
                True,
                False,
                False,
                "synthetic provider server failure",
                500,
            ),
            "interrupted_ambiguous": ErrorClassification(
                ProviderErrorCategory.CONNECTION_AMBIGUOUS,
                True,
                True,
                False,
                "synthetic ambiguous interrupted call",
            ),
        }
        if self.scenario in failures:
            raise ProviderExecutionError(failures[self.scenario])
        digest = hashlib.sha256(request.request_id.encode()).hexdigest()[:12]
        text = f"ترجمهٔ ساختگی برای آزمون محلی {digest}.\n"
        outputs: tuple[str, ...] = (text,)
        status = "completed"
        incomplete = None
        if self.scenario == "empty":
            outputs = ("",)
        elif self.scenario == "malformed":
            outputs = (text, "خروجی دوم ساختگی.\n")
        elif self.scenario == "truncated":
            status = "incomplete"
            incomplete = "max_output_tokens"
        elif self.scenario != "success":
            raise ValueError(f"unknown fake-provider scenario: {self.scenario}")
        return ProviderResult(
            response_id=f"fake-{digest}",
            model=request.model or "fake-v1",
            status=status,
            text_outputs=outputs,
            usage=ProviderUsage(100, 20, 120),
            incomplete_reason=incomplete,
            sanitized_response={
                "id": f"fake-{digest}",
                "model": request.model,
                "status": status,
                "output": list(outputs),
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                "incomplete_reason": incomplete,
            },
        )
