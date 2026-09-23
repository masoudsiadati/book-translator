import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import ProviderErrorCategory, ProviderName, TranslationAttemptStatus
from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
from book_translator.provider_execution import (
    ProviderExecutionError,
    ProviderRequest,
    execute_translation_job,
    list_attempts,
    plan_execution,
    preflight_provider,
)
from book_translator.provider_execution.errors import ErrorClassification
from book_translator.provider_execution.executor import _provider_for
from book_translator.provider_execution.providers.claude_code import (
    CLAUDE_CODE_ADAPTER_VERSION,
    CLAUDE_CODE_DIAGNOSTIC_LIMIT,
    REQUIRED_CLI_FLAGS,
    STREAM_JSON_HELP_MARKERS,
    SYSTEM_PROMPT_FILE_HELP_MARKERS,
    ClaudeCodePreflightReport,
    ClaudeCodeTranslationProvider,
    preflight_claude_code,
    redacted_diagnostic,
)
from book_translator.translation_queue import claim_job, load_run_summary
from tests.test_provider_execution import EXECUTION_NOW, _claimed_execution
from tests.test_translation_queue import _run

runner = CliRunner()

PERSIAN = "پاراگراف نخست با نویسهٔ فارسی.\n\nپاراگراف دوم «با گیومه».\n"
VERSION_OUTPUT = b"2.1.233 (Claude Code)\n"
HELP_OUTPUT = (
    "Usage: claude [options] [prompt]\n"
    + "\n".join(
        (*REQUIRED_CLI_FLAGS, SYSTEM_PROMPT_FILE_HELP_MARKERS[1], STREAM_JSON_HELP_MARKERS[0])
    )
).encode("utf-8")
SESSION_ID = "0c9d6f38-0000-4000-8000-000000000001"
AUTH_STATUS = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "email": "person@example.invalid",
    "orgId": "00000000-0000-0000-0000-000000000000",
    "subscriptionType": "max",
}
REPORT = ClaudeCodePreflightReport(
    executable="claude",
    cli_version="2.1.233",
    non_interactive_supported=True,
    authenticated=True,
    auth_method="claude.ai",
    api_provider="firstParty",
    subscription_type="max",
)


def _payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": PERSIAN,
        "session_id": SESSION_ID,
        "stop_reason": "end_turn",
        "num_turns": 1,
        "duration_api_ms": 1657,
        "permission_denials": [],
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 2400,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 100,
        },
        "modelUsage": {
            "claude-opus-5[1m]": {
                "canonicalModel": "claude-opus-5",
                "provider": "firstParty",
            }
        },
    }
    payload.update(updates)
    return payload


def _assistant(
    text: str,
    *,
    message_id: str = "msg_01",
    uuid: str = "a0000000-0000-4000-8000-000000000001",
    model: str = "claude-opus-5",
    stop_reason: str | None = "end_turn",
    blocks: list[dict[str, object]] | None = None,
    parent_tool_use_id: str | None = None,
    session_id: str | None = SESSION_ID,
    **extra: object,
) -> dict[str, object]:
    """One ``assistant`` stream event in the shape the CLI writes: a whole message snapshot."""
    event: dict[str, object] = {
        "type": "assistant",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}] if blocks is None else blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 5},
        },
        "parent_tool_use_id": parent_tool_use_id,
        "session_id": session_id,
        "uuid": uuid,
    }
    event.update(extra)
    return event


def _user(
    text: str,
    *,
    uuid: str = "b0000000-0000-4000-8000-000000000001",
    is_meta: bool = True,
    parent_tool_use_id: str | None = None,
    blocks: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "type": "user",
        "message": {"role": "user", "content": blocks or [{"type": "text", "text": text}]},
        "parent_tool_use_id": parent_tool_use_id,
        "is_meta": is_meta,
        "session_id": SESSION_ID,
        "uuid": uuid,
    }


def _system_init() -> dict[str, object]:
    return {
        "type": "system",
        "subtype": "init",
        "cwd": "/private/tmp/book-translator-claude-code-x",
        "session_id": SESSION_ID,
        "tools": [],
        "mcp_servers": [],
        "model": "claude-opus-5",
        "permissionMode": "default",
        "apiKeySource": "none",
        "uuid": "c0000000-0000-4000-8000-000000000001",
    }


def _stream(*events: dict[str, object]) -> bytes:
    """Encode events as the newline-delimited JSON the CLI writes in stream-json mode."""
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events).encode()


def _success_stream(text: str = PERSIAN, **result_updates: object) -> bytes:
    """A complete one-turn session: init, one assistant message, and its matching result."""
    result_updates.setdefault("result", text)
    return _stream(_system_init(), _assistant(text), _payload(**result_updates))


class FakeClaudeCli:
    """Records every local CLI invocation so no test ever launches the real executable."""

    def __init__(
        self,
        *,
        stdout: bytes | None = None,
        stderr: bytes = b"",
        returncode: int = 0,
        effect: BaseException | None = None,
        auth_status: object = AUTH_STATUS,
        auth_returncode: int = 0,
        help_output: bytes = HELP_OUTPUT,
        version_output: bytes = VERSION_OUTPUT,
        version_returncode: int = 0,
    ) -> None:
        self.stdout = _success_stream() if stdout is None else stdout
        self.stderr = stderr
        self.returncode = returncode
        self.effect = effect
        self.auth_status = auth_status
        self.auth_returncode = auth_returncode
        self.help_output = help_output
        self.version_output = version_output
        self.version_returncode = version_returncode
        self.calls: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append({"command": list(command), **kwargs})
        arguments = command[1:]
        if arguments[:1] == ["--version"]:
            return subprocess.CompletedProcess(
                command, self.version_returncode, self.version_output, b""
            )
        if arguments[:1] == ["--help"]:
            return subprocess.CompletedProcess(command, 0, self.help_output, b"")
        if arguments[:3] == ["auth", "status", "--json"]:
            encoded = (
                self.auth_status
                if isinstance(self.auth_status, bytes)
                else json.dumps(self.auth_status).encode("utf-8")
            )
            return subprocess.CompletedProcess(command, self.auth_returncode, encoded, b"")
        if self.effect is not None:
            raise self.effect
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)

    @property
    def translation_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if "--print" in call["command"]]


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeClaudeCli) -> FakeClaudeCli:
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def _request(*, model: str | None = None, max_output_tokens: int = 12000) -> ProviderRequest:
    return ProviderRequest(
        "request",
        "key",
        "run",
        "job",
        1,
        "system text\n",
        "user text\n",
        "en",
        "fa",
        "contract",
        model,
        30,
        max_output_tokens,
    )


def _provider() -> ClaudeCodeTranslationProvider:
    return ClaudeCodeTranslationProvider(preflight_report=REPORT)


def _raised(provider: ClaudeCodeTranslationProvider) -> ProviderErrorCategory:
    with pytest.raises(ProviderExecutionError) as caught:
        provider.execute(_request())
    return caught.value.classification.category


def test_provider_name_and_selection_recognize_claude_code(tmp_path: Path) -> None:
    assert ProviderName("claude-code") is ProviderName.CLAUDE_CODE
    run_id, _, job_id, token = _claimed_execution(tmp_path)
    _allow_claude_code(tmp_path)
    plan = _claude_plan(tmp_path, run_id, job_id, token)
    adapter = _provider_for(plan)

    assert isinstance(adapter, ClaudeCodeTranslationProvider)
    assert adapter.provider_name == ProviderName.CLAUDE_CODE.value
    assert adapter.adapter_version == CLAUDE_CODE_ADAPTER_VERSION
    assert preflight_provider(ProviderName.FAKE).supported is False


def _allow_claude_code(root: Path) -> None:
    config_path = root / "config/book.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "translation_provider_models:\n  fake: [fake-v1]\n  claude-code: []\n",
        encoding="utf-8",
    )


def _claude_plan(root: Path, run_id: str, job_id: str, token: str) -> Any:
    return plan_execution(
        run_id,
        job_id,
        project_root=root,
        worker_id="worker",
        claim_token=token,
        provider=ProviderName.CLAUDE_CODE,
        model=None,
        timeout_seconds=120,
        max_output_tokens=1000,
        now=lambda: EXECUTION_NOW,
    )


def test_preflight_verifies_executable_non_interactive_mode_and_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://router.invalid")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    fake = _install(monkeypatch, FakeClaudeCli())
    report = preflight_claude_code()

    assert [call["command"][1:] for call in fake.calls] == [
        ["--version"],
        ["--help"],
        ["auth", "status", "--json"],
    ]
    assert report.cli_version == "2.1.233"
    assert report.non_interactive_supported and report.authenticated
    assert (report.auth_method, report.api_provider) == ("claude.ai", "firstParty")
    assert report.subscription_type == "max"
    environment = cast(dict[str, str], fake.calls[0]["env"])
    assert not any(name.endswith("_API_KEY") for name in environment)
    assert "ANTHROPIC_BASE_URL" not in environment
    assert "CLAUDE_CODE_USE_BEDROCK" not in environment
    assert not fake.translation_calls


def test_preflight_reports_a_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeClaudeCli())
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError())
    )
    with pytest.raises(ProviderExecutionError) as caught:
        preflight_claude_code()

    assert caught.value.classification.category is ProviderErrorCategory.EXECUTABLE_NOT_FOUND
    assert not caught.value.classification.ambiguous


@pytest.mark.parametrize(
    ("updates", "category"),
    [
        ({"auth_status": {"loggedIn": False}}, ProviderErrorCategory.AUTHENTICATION),
        ({"auth_status": {}}, ProviderErrorCategory.AUTHENTICATION),
        ({"auth_returncode": 1}, ProviderErrorCategory.AUTHENTICATION),
        ({"auth_status": b"not json"}, ProviderErrorCategory.MALFORMED_RESPONSE),
        (
            {"help_output": b"Usage: claude\n--print\n"},
            ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER,
        ),
        (
            {"help_output": HELP_OUTPUT.replace(b"--system-prompt[-file]", b"")},
            ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER,
        ),
        ({"version_returncode": 3}, ProviderErrorCategory.INVOCATION_ERROR),
        ({"version_output": b"unknown build\n"}, ProviderErrorCategory.INVOCATION_ERROR),
    ],
)
def test_preflight_failures_are_classified(
    monkeypatch: pytest.MonkeyPatch, updates: dict[str, Any], category: ProviderErrorCategory
) -> None:
    fake = _install(monkeypatch, FakeClaudeCli(**updates))
    with pytest.raises(ProviderExecutionError) as caught:
        preflight_provider(ProviderName.CLAUDE_CODE)

    assert caught.value.classification.category is category
    assert not fake.translation_calls


def test_successful_invocation_is_tool_free_and_preserves_persian_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "1")
    fake = _install(monkeypatch, FakeClaudeCli())
    result = _provider().execute(_request(max_output_tokens=9000))

    call = fake.translation_calls[0]
    command = cast(list[str], call["command"])
    assert command[0] == "claude"
    assert command[1:5] == ["--print", "--output-format", "stream-json", "--verbose"]
    for flag in ("--disable-slash-commands", "--strict-mcp-config", "--safe-mode"):
        assert flag in command
    assert command[command.index("--tools") + 1] == ""
    assert "--no-session-persistence" in command and "--model" not in command
    assert "--dangerously-skip-permissions" not in command
    system_prompt_file = Path(command[command.index("--system-prompt-file") + 1])
    assert "system text" not in " ".join(command)
    assert cast(bytes, call["input"]) == b"user text\n"
    assert Path(cast(str, call["cwd"])) == system_prompt_file.parent
    environment = cast(dict[str, str], call["env"])
    assert environment["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "9000"
    assert not any(name.endswith("_API_KEY") for name in environment)
    assert result.status == "completed"
    assert result.text_outputs == (PERSIAN,)
    assert result.text_outputs[0].encode("utf-8").decode("utf-8") == PERSIAN
    assert not system_prompt_file.exists()


def test_explicit_model_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, FakeClaudeCli())
    _provider().execute(_request(model="claude-opus-5"))

    command = cast(list[str], fake.translation_calls[0]["command"])
    assert command[command.index("--model") + 1] == "claude-opus-5"


def test_execution_preflights_once_and_caches_the_local_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, FakeClaudeCli())
    provider = ClaudeCodeTranslationProvider()
    provider.execute(_request())
    provider.execute(_request())

    assert [call["command"][1] for call in fake.calls] == [
        "--version",
        "--help",
        "auth",
        "--print",
        "--print",
    ]


@pytest.mark.parametrize(
    ("effect", "category", "ambiguous"),
    [
        (FileNotFoundError(), ProviderErrorCategory.EXECUTABLE_NOT_FOUND, False),
        (subprocess.TimeoutExpired(["claude"], 30), ProviderErrorCategory.TIMEOUT, True),
        (KeyboardInterrupt(), ProviderErrorCategory.INTERRUPTED, True),
        (OSError(), ProviderErrorCategory.INVOCATION_ERROR, False),
    ],
)
def test_process_failures_are_classified(
    monkeypatch: pytest.MonkeyPatch,
    effect: BaseException,
    category: ProviderErrorCategory,
    ambiguous: bool,
) -> None:
    _install(monkeypatch, FakeClaudeCli(effect=effect))
    with pytest.raises(ProviderExecutionError) as caught:
        _provider().execute(_request())

    assert caught.value.classification.category is category
    assert caught.value.classification.ambiguous is ambiguous


def test_nonzero_exit_and_authentication_failure_are_distinguished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeClaudeCli(returncode=7, stdout=b"", stderr=b"diagnostic"))
    assert _raised(_provider()) is ProviderErrorCategory.NONZERO_EXIT

    _install(
        monkeypatch,
        FakeClaudeCli(returncode=1, stdout=b"", stderr=b"authentication_error: invalid session"),
    )
    assert _raised(_provider()) is ProviderErrorCategory.AUTHENTICATION

    _install(
        monkeypatch,
        FakeClaudeCli(
            returncode=1, stdout=b"", stderr=b"error: unknown option '--system-prompt-file'"
        ),
    )
    assert _raised(_provider()) is ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER


def _failing_exit(
    monkeypatch: pytest.MonkeyPatch, *, stdout: bytes = b"", stderr: bytes = b"", code: int = 1
) -> ErrorClassification:
    _install(monkeypatch, FakeClaudeCli(returncode=code, stdout=stdout, stderr=stderr))
    with pytest.raises(ProviderExecutionError) as caught:
        _provider().execute(_request())
    return caught.value.classification


@pytest.mark.parametrize(
    ("stderr", "category", "retryable"),
    [
        (b"API Error: rate_limit_error", ProviderErrorCategory.RATE_LIMIT, True),
        (b"Claude AI usage limit reached", ProviderErrorCategory.RATE_LIMIT, True),
        (b"API Error: overloaded_error", ProviderErrorCategory.SERVER_ERROR, True),
        (b"Internal server error", ProviderErrorCategory.SERVER_ERROR, True),
        (
            b"error: unknown command 'translate'",
            ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER,
            False,
        ),
        (
            b"not_found_error: model not found",
            ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER,
            False,
        ),
        (b"Invalid settings in the project scope", ProviderErrorCategory.INVALID_REQUEST, False),
        (b"authentication_error", ProviderErrorCategory.AUTHENTICATION, False),
    ],
)
def test_failing_exit_distinguishes_cli_config_auth_model_and_server_failures(
    monkeypatch: pytest.MonkeyPatch,
    stderr: bytes,
    category: ProviderErrorCategory,
    retryable: bool,
) -> None:
    classification = _failing_exit(monkeypatch, stderr=stderr)

    assert classification.category is category
    assert classification.retryable is retryable
    assert not classification.ambiguous
    assert stderr.decode("utf-8").split(":")[0].lower() in classification.message.lower()


def test_failing_exit_retains_the_structured_result_object_over_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload(
        is_error=True,
        subtype="error_during_execution",
        api_error_status=529,
        result="Upstream reported overloaded_error",
    )
    classification = _failing_exit(
        monkeypatch, stdout=json.dumps(payload).encode("utf-8"), stderr=b"unrelated banner noise"
    )

    assert classification.category is ProviderErrorCategory.SERVER_ERROR
    assert classification.retryable
    assert "exit code 1" in classification.message
    assert "subtype=error_during_execution" in classification.message
    assert "api_error_status=529" in classification.message
    assert "overloaded_error" in classification.message
    assert "banner noise" not in classification.message


def test_failing_exit_falls_back_to_stderr_when_the_payload_reports_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful payload beside a failing exit is not an error report, nor prose evidence."""
    classification = _failing_exit(
        monkeypatch,
        stdout=_success_stream(),
        stderr=b"claude: process terminated after writing output",
    )

    assert classification.category is ProviderErrorCategory.NONZERO_EXIT
    assert "process terminated after writing output" in classification.message
    assert PERSIAN.strip() not in classification.message


def test_failing_exit_reports_when_no_safe_diagnostic_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classification = _failing_exit(monkeypatch, code=1, stderr=b"   \n\n")

    assert classification.category is ProviderErrorCategory.NONZERO_EXIT
    assert classification.message == (
        "Claude Code exited unsuccessfully (exit code 1; no safe diagnostic output)"
    )


def test_retained_diagnostics_drop_secrets_identity_paths_and_session_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = (
        b"Request failed for person@example.invalid at https://api.example.invalid/v1/messages\n"
        b"Authorization: Bearer sk-ant-oat01-abcdefghijklmnopqrstuvwxyz0123456789\n"
        b"session 0c9d6f38-0000-4000-8000-000000000001 wrote /home/operator/.claude/state.json\n"
        b"\x1b[31mAPI Error: rate_limit_error\x1b[0m\n"
    )
    message = _failing_exit(monkeypatch, stderr=stderr).message

    for secret in (
        "person@example.invalid",
        "api.example.invalid",
        "sk-ant-oat01",
        "abcdefghijklmnopqrstuvwxyz0123456789",
        "0c9d6f38-0000-4000-8000-000000000001",
        "/home/operator",
        ".claude/state.json",
        "\x1b",
    ):
        assert secret not in message
    assert "rate_limit_error" in message


def test_retained_diagnostics_are_capped_and_never_carry_translated_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_message = _failing_exit(monkeypatch, stderr=b"failure " * 200).message
    prose = _failing_exit(monkeypatch, stderr=PERSIAN.encode("utf-8")).message

    assert len(long_message) < 400
    assert long_message.endswith("…)")
    truncated = redacted_diagnostic("failure " * 200)
    assert truncated is not None and len(truncated) <= CLAUDE_CODE_DIAGNOSTIC_LIMIT + 1
    assert redacted_diagnostic(PERSIAN) is None
    assert PERSIAN.strip() not in prose
    assert prose.endswith("no safe diagnostic output)")


def test_published_attempt_keeps_the_sanitized_diagnostic_and_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, run_path, job_id, token = _claimed_execution(tmp_path)
    _allow_claude_code(tmp_path)
    _install(
        monkeypatch,
        FakeClaudeCli(
            returncode=1,
            stdout=b"",
            stderr=b"API Error: rate_limit_error for person@example.invalid",
        ),
    )
    plan = _claude_plan(tmp_path, run_id, job_id, token)
    execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        now=lambda: EXECUTION_NOW,
        monotonic=lambda: 1.0,
    )

    attempt = list_attempts(run_id, job_id, project_root=tmp_path)[0].attempt
    message = attempt.sanitized_error_message or ""
    assert attempt.status is TranslationAttemptStatus.FAILED
    assert attempt.error_category is ProviderErrorCategory.RATE_LIMIT
    assert attempt.retryable and not attempt.ambiguous
    assert "rate_limit_error" in message and "exit code 1" in message
    assert "person@example.invalid" not in message
    published = "".join(
        path.read_text(encoding="utf-8")
        for path in (run_path / "jobs" / plan.job.chunk_id / "attempts/001").iterdir()
    )
    assert "person@example.invalid" not in published


@pytest.mark.parametrize(
    ("stdout", "category"),
    [
        (b"Claude Code banner, not JSON\n", ProviderErrorCategory.MALFORMED_RESPONSE),
        (b"[]\n", ProviderErrorCategory.MALFORMED_RESPONSE),
        (b'{"type":"system","subtype":"success"}\n', ProviderErrorCategory.MALFORMED_RESPONSE),
        (b"\xff\xfe not utf-8", ProviderErrorCategory.MALFORMED_RESPONSE),
        (b"", ProviderErrorCategory.MALFORMED_RESPONSE),
        # A bare result object with no assistant message is the lossy capture this adapter left.
        (json.dumps(_payload()).encode("utf-8") + b"\n", ProviderErrorCategory.MALFORMED_RESPONSE),
    ],
)
def test_malformed_structured_output_is_refused(
    monkeypatch: pytest.MonkeyPatch, stdout: bytes, category: ProviderErrorCategory
) -> None:
    _install(monkeypatch, FakeClaudeCli(stdout=stdout))
    assert _raised(_provider()) is category


def test_missing_or_empty_result_text_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeClaudeCli(stdout=_success_stream(result=42)))
    assert _raised(_provider()) is ProviderErrorCategory.MALFORMED_RESPONSE

    _install(monkeypatch, FakeClaudeCli(stdout=_success_stream("  \n\x1b[2K ")))
    with pytest.raises(ProviderExecutionError) as caught:
        _provider().execute(_request())
    assert caught.value.classification.category is ProviderErrorCategory.EMPTY_OUTPUT
    assert caught.value.classification.retryable


def test_reported_error_results_are_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    limit = "API Error: Claude's response exceeded the 32 output token maximum."
    _install(monkeypatch, FakeClaudeCli(stdout=_success_stream(limit, is_error=True)))
    assert _raised(_provider()) is ProviderErrorCategory.OUTPUT_TRUNCATED

    unauthenticated = "API Error: authentication_error"
    _install(monkeypatch, FakeClaudeCli(stdout=_success_stream(unauthenticated, is_error=True)))
    assert _raised(_provider()) is ProviderErrorCategory.AUTHENTICATION

    exhausted = _stream(
        _system_init(),
        _assistant(PERSIAN),
        _payload(is_error=True, subtype="error_max_turns", result="", errors=["max turns"]),
    )
    _install(monkeypatch, FakeClaudeCli(stdout=exhausted))
    with pytest.raises(ProviderExecutionError) as caught:
        _provider().execute(_request())
    assert caught.value.classification.category is ProviderErrorCategory.SERVER_ERROR
    assert "error_max_turns" in caught.value.classification.message


def test_ansi_control_sequences_never_reach_the_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decorated = "\x1b[1;32m" + PERSIAN + "\x1b[0m"
    _install(monkeypatch, FakeClaudeCli(stdout=_success_stream(decorated)))
    result = _provider().execute(_request())

    assert result.text_outputs == (PERSIAN,)
    assert "\x1b" not in result.text_outputs[0]


def test_recorded_provenance_distinguishes_the_local_cli_from_the_anthropic_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeClaudeCli())
    result = _provider().execute(_request(model="claude-opus-5"))

    assert result.sanitized_response == {
        "provider": "claude-code",
        "adapter_version": CLAUDE_CODE_ADAPTER_VERSION,
        "execution_mode": "local-cli-print-stream-json",
        "capture": "primary-assistant-messages",
        "cli_executable": "claude",
        "cli_version": "2.1.233",
        "authentication": "claude-code-login",
        "auth_method": "claude.ai",
        "api_provider": "firstParty",
        "model_requested": "claude-opus-5",
        "model_returned": "claude-opus-5[1m]",
        "canonical_model": "claude-opus-5",
        "model_inference_provider": "firstParty",
        "max_output_tokens_enforced": True,
        "max_output_tokens": 12000,
        "tools_enabled": False,
        "session_persistence": False,
        "status": "completed",
        "subtype": "success",
        "stop_reason": "end_turn",
        "num_turns": 1,
        "assistant_message_count": 1,
        "user_event_count": 0,
        "duration_api_ms": 1657,
        "permission_denial_count": 0,
    }
    assert result.response_id == SESSION_ID
    assert result.model == "claude-opus-5[1m]"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (1200, 2400)
    assert result.usage.total_tokens == 3700
    assert "email" not in str(result.sanitized_response)


def test_ambiguous_model_usage_records_no_returned_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeClaudeCli(stdout=_success_stream(modelUsage={"a": {}, "b": {}})))
    result = _provider().execute(_request())

    assert result.model is None
    assert result.sanitized_response["canonical_model"] is None


def test_executor_publishes_a_claude_code_attempt_without_leaking_the_claim_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, run_path, job_id, token = _claimed_execution(tmp_path)
    _allow_claude_code(tmp_path)
    fake = _install(monkeypatch, FakeClaudeCli())
    plan = _claude_plan(tmp_path, run_id, job_id, token)
    result = execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        now=lambda: EXECUTION_NOW,
        monotonic=lambda: 1.0,
    )

    job = load_run_summary(run_id, project_root=tmp_path).jobs[0]
    attempt = list_attempts(run_id, job_id, project_root=tmp_path)[0].attempt
    directory = run_path / "jobs" / job.chunk_id / "attempts/001"
    assert result.attempt_status is TranslationAttemptStatus.SUCCEEDED
    assert attempt.provider is ProviderName.CLAUDE_CODE
    assert attempt.provider_adapter_version == CLAUDE_CODE_ADAPTER_VERSION
    assert attempt.model_requested is None and attempt.model_returned == "claude-opus-5[1m]"
    assert (directory / "translation.md").read_bytes() == PERSIAN.encode("utf-8")
    for call in fake.calls:
        assert token not in " ".join(cast(list[str], call["command"]))
        assert token not in cast(bytes, call.get("input") or b"").decode("utf-8")
        assert all(token not in value for value in cast(dict[str, str], call["env"]).values())
    published = "".join(path.read_text(encoding="utf-8") for path in directory.iterdir())
    assert token not in published and "system text" not in published
    if os.name == "posix":
        assert all(path.stat().st_mode & 0o077 == 0 for path in directory.iterdir())


def test_cli_preflight_command_reports_and_refuses_without_touching_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeClaudeCli())
    passed = runner.invoke(app, ["check-translation-provider", "--provider", "claude-code"])
    assert passed.exit_code == 0
    assert "claude-code" in passed.stdout and "2.1.233" in passed.stdout
    assert "person@example.invalid" not in passed.stdout

    _install(monkeypatch, FakeClaudeCli(auth_status={"loggedIn": False}))
    refused = runner.invoke(app, ["check-translation-provider", "--provider", "claude-code"])
    assert refused.exit_code == 1
    assert "not logged in" in refused.stderr


def test_cli_exposes_claude_code_and_dry_run_contacts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _ = _run(tmp_path)
    execute_prompt_packages(plan_prompt_packages(run_id, project_root=tmp_path))
    claim = claim_job(run_id, project_root=tmp_path, worker_id="worker", lease_seconds=900)
    job_id, token = claim.job.job_id, claim.claim_token
    _allow_claude_code(tmp_path)
    fake = _install(monkeypatch, FakeClaudeCli())
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "execute-translation-job",
            run_id,
            job_id,
            "--worker-id",
            "worker",
            "--claim-token",
            token,
            "--provider",
            "claude-code",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "claude-code" in result.stdout and token not in result.stdout
    assert not fake.calls
    for command in ("run-translation-worker", "check-translation-provider"):
        assert command in runner.invoke(app, ["--help"]).stdout
    worker_help = runner.invoke(
        app, ["run-translation-worker", "--help"], env={"COLUMNS": "250", "TERM": "dumb"}
    )
    assert "<fake|openai|codex-cli|claude-code>" in worker_help.stdout
