"""Provider-neutral in-memory request and result protocol."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    request_id: str
    idempotency_key: str
    run_id: str
    job_id: str
    attempt_number: int
    system_message: str
    user_message: str
    source_language: str
    target_language: str
    output_contract: str
    model: str | None
    timeout_seconds: int
    max_output_tokens: int
    reasoning_effort: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    sdk_retry_count: int = 0


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderResult:
    response_id: str | None
    model: str | None
    status: str
    text_outputs: tuple[str, ...]
    usage: ProviderUsage = ProviderUsage()
    incomplete_reason: str | None = None
    sanitized_response: dict[str, object] = field(default_factory=dict)


class TranslationProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    def execute(self, request: ProviderRequest) -> ProviderResult: ...
