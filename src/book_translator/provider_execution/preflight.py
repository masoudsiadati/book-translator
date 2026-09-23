"""Provider-neutral preflight performed before any queue mutation or job claim.

Providers whose availability can be established locally expose a preflight; the dispatch itself is
generic so the queue, worker, and CLI never carry provider-specific knowledge. A failing preflight
raises before a claim is taken, so no attempt is consumed and no ambiguous attempt is published.
"""

from dataclasses import dataclass

from book_translator.models import ProviderName
from book_translator.provider_execution.providers.claude_code import (
    CLAUDE_CODE_PREFLIGHT_TIMEOUT_SECONDS,
    preflight_claude_code,
)
from book_translator.provider_execution.providers.codex_cli import (
    REQUIRED_EXEC_CAPABILITIES,
    preflight_codex_cli,
)

#: 1.1.0 adds the ``codex-cli`` preflight; until then that provider reported "not implemented"
#: and passed, so ``passed`` proved nothing for it.
PROVIDER_PREFLIGHT_VERSION = "1.1.0"


@dataclass(frozen=True, slots=True)
class ProviderPreflightReport:
    """Structural, prose-free evidence about a provider's local readiness."""

    provider: str
    supported: bool
    passed: bool
    details: tuple[tuple[str, str], ...]


def _claude_code_report(executable: str, timeout_seconds: int) -> ProviderPreflightReport:
    report = preflight_claude_code(executable=executable, timeout_seconds=timeout_seconds)
    return ProviderPreflightReport(
        provider=ProviderName.CLAUDE_CODE.value,
        supported=True,
        passed=True,
        details=(
            ("executable", report.executable),
            ("cli_version", report.cli_version),
            ("non_interactive", "supported" if report.non_interactive_supported else "missing"),
            ("authenticated", "yes" if report.authenticated else "no"),
            ("auth_method", report.auth_method or "unknown"),
            ("api_provider", report.api_provider or "unknown"),
            ("subscription_type", report.subscription_type or "unknown"),
            ("api_key_used", "no"),
        ),
    )


def _codex_cli_report(executable: str, timeout_seconds: int) -> ProviderPreflightReport:
    report = preflight_codex_cli(executable=executable, timeout_seconds=timeout_seconds)
    capability_count = len(REQUIRED_EXEC_CAPABILITIES)
    return ProviderPreflightReport(
        provider=ProviderName.CODEX_CLI.value,
        supported=True,
        passed=True,
        details=(
            ("executable", report.executable),
            ("cli_version", report.cli_version),
            ("non_interactive", "supported" if report.non_interactive_supported else "missing"),
            ("exec_capabilities", f"{capability_count}/{capability_count} present"),
            ("authenticated", "yes" if report.authenticated else "no"),
            ("auth_method", report.auth_method or "unknown"),
            ("subscription_type", report.subscription_type or "unknown"),
            ("api_key_used", "no"),
        ),
    )


def preflight_provider(
    provider: ProviderName,
    *,
    executable: str | None = None,
    timeout_seconds: int = CLAUDE_CODE_PREFLIGHT_TIMEOUT_SECONDS,
) -> ProviderPreflightReport:
    """Check local provider readiness, raising ``ProviderExecutionError`` when it is unusable."""
    if provider is ProviderName.CLAUDE_CODE:
        return _claude_code_report(executable or "claude", timeout_seconds)
    if provider is ProviderName.CODEX_CLI:
        return _codex_cli_report(executable or "codex", timeout_seconds)
    return ProviderPreflightReport(
        provider=provider.value,
        supported=False,
        passed=True,
        details=(("preflight", "not implemented for this provider"),),
    )
