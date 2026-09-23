"""Codex CLI preflight: local, model-free readiness evidence for the ``codex-cli`` provider.

Every test scripts ``subprocess.run`` so no test ever launches the real executable. A scripted
invocation the preflight is not allowed to make — anything other than ``--version``,
``exec --help``, and ``login status`` — fails the test outright, because such a call would be an
inference request, a session, or a queue-visible side effect.
"""

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import ProviderErrorCategory, ProviderName
from book_translator.provider_execution import (
    ProviderExecutionError,
    ProviderPreflightReport,
    preflight_provider,
)
from book_translator.provider_execution.providers.codex_cli import (
    CODEX_CLI_PREFLIGHT_TIMEOUT_SECONDS,
    REQUIRED_EXEC_CAPABILITIES,
    CodexCliPreflightReport,
    preflight_codex_cli,
)
from book_translator.translation_provider_recovery import record_preflight
from tests.test_claude_code_provider import FakeClaudeCli
from tests.test_provider_execution import _claimed_execution

runner = CliRunner()

VERSION_OUTPUT = b"codex-cli 0.144.6\n"
# A trimmed copy of the real ``codex exec --help`` shape: every capability the adapter relies on is
# documented here exactly the way the installed CLI documents it.
EXEC_HELP_OUTPUT = b"""Run Codex non-interactively

Usage: codex exec [OPTIONS] [PROMPT]
       codex exec [OPTIONS] <COMMAND> [ARGS]

Arguments:
  [PROMPT]
          Initial instructions for the agent. If not provided as an argument (or if `-` is used),
          instructions are read from stdin.

Options:
  -m, --model <MODEL>
          Model the agent should use

  -s, --sandbox <SANDBOX_MODE>
          Select the sandbox policy to use when executing model-generated shell commands

          [possible values: read-only, workspace-write, danger-full-access]

  -C, --cd <DIR>
          Tell the agent to use the specified directory as its working root

      --skip-git-repo-check
          Allow running Codex outside a Git repository

      --ephemeral
          Run without persisting session files to disk

      --color <COLOR>
          Specifies color settings for use in the output

          [possible values: always, never, auto]

  -o, --output-last-message <FILE>
          Specifies file where the last message from the agent should be written
"""
CHATGPT_LOGIN = b"Logged in using ChatGPT\n"
API_KEY_LOGIN = b"Logged in using an API key - sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd\n"
LOGGED_OUT = b"Not logged in\n"


class FakeCodexCli:
    """Answers only the three read-only probes; any other invocation is a test failure."""

    def __init__(
        self,
        *,
        version_output: bytes = VERSION_OUTPUT,
        version_returncode: int = 0,
        exec_help: bytes = EXEC_HELP_OUTPUT,
        exec_help_returncode: int = 0,
        login_stdout: bytes = CHATGPT_LOGIN,
        login_stderr: bytes = b"",
        login_returncode: int = 0,
        effect: BaseException | None = None,
    ) -> None:
        self.version_output = version_output
        self.version_returncode = version_returncode
        self.exec_help = exec_help
        self.exec_help_returncode = exec_help_returncode
        self.login_stdout = login_stdout
        self.login_stderr = login_stderr
        self.login_returncode = login_returncode
        self.effect = effect
        self.calls: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append({"command": list(command), **kwargs})
        if self.effect is not None:
            raise self.effect
        arguments = command[1:]
        if arguments == ["--version"]:
            return subprocess.CompletedProcess(
                command, self.version_returncode, self.version_output, b""
            )
        if arguments == ["exec", "--help"]:
            return subprocess.CompletedProcess(
                command, self.exec_help_returncode, self.exec_help, b""
            )
        if arguments == ["login", "status"]:
            return subprocess.CompletedProcess(
                command, self.login_returncode, self.login_stdout, self.login_stderr
            )
        raise AssertionError(f"preflight made a forbidden Codex invocation: {arguments}")

    @property
    def argument_lists(self) -> list[list[str]]:
        return [cast(list[str], call["command"])[1:] for call in self.calls]


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeCodexCli) -> FakeCodexCli:
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_preflight_verifies_executable_exec_capabilities_and_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("THIRD_PARTY_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://router.invalid")
    fake = _install(monkeypatch, FakeCodexCli())
    report = preflight_codex_cli()

    assert fake.argument_lists == [["--version"], ["exec", "--help"], ["login", "status"]]
    assert report == CodexCliPreflightReport(
        executable="codex",
        cli_version="0.144.6",
        non_interactive_supported=True,
        authenticated=True,
        auth_method="chatgpt",
        subscription_type=None,
    )
    for call in fake.calls:
        environment = cast(dict[str, str], call["env"])
        assert not any(name.endswith("_API_KEY") for name in environment)
        assert "OPENAI_BASE_URL" not in environment
        assert call["input"] == b""
        assert call["timeout"] == CODEX_CLI_PREFLIGHT_TIMEOUT_SECONDS


def test_preflight_reports_a_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, FakeCodexCli(effect=FileNotFoundError()))
    with pytest.raises(ProviderExecutionError) as caught:
        preflight_codex_cli()

    assert caught.value.classification.category is ProviderErrorCategory.EXECUTABLE_NOT_FOUND
    assert not caught.value.classification.ambiguous


@pytest.mark.parametrize(
    ("effect", "category", "retryable"),
    [
        (subprocess.TimeoutExpired(["codex"], 60), ProviderErrorCategory.TIMEOUT_PRE_SEND, True),
        (OSError(), ProviderErrorCategory.INVOCATION_ERROR, False),
        (KeyboardInterrupt(), ProviderErrorCategory.INTERRUPTED, False),
    ],
)
def test_preflight_invocation_failures_are_classified_before_any_send(
    monkeypatch: pytest.MonkeyPatch,
    effect: BaseException,
    category: ProviderErrorCategory,
    retryable: bool,
) -> None:
    _install(monkeypatch, FakeCodexCli(effect=effect))
    with pytest.raises(ProviderExecutionError) as caught:
        preflight_codex_cli()

    assert caught.value.classification.category is category
    assert caught.value.classification.retryable is retryable
    assert not caught.value.classification.ambiguous


@pytest.mark.parametrize(
    ("version_output", "expected"),
    [
        (b"codex-cli 0.144.6\n", "0.144.6"),
        (b"codex-cli 0.151.2\n", "0.151.2"),
        (b"codex 1.3.0 (build abc)\n", "1.3.0"),
    ],
)
def test_preflight_detects_the_installed_version_without_pinning_one(
    monkeypatch: pytest.MonkeyPatch, version_output: bytes, expected: str
) -> None:
    _install(monkeypatch, FakeCodexCli(version_output=version_output))
    report = preflight_codex_cli()

    assert report.cli_version == expected
    assert report.non_interactive_supported and report.authenticated


@pytest.mark.parametrize(
    "updates",
    [{"version_returncode": 2}, {"version_output": b"unknown build\n"}],
)
def test_preflight_refuses_an_executable_without_a_recognizable_version(
    monkeypatch: pytest.MonkeyPatch, updates: dict[str, Any]
) -> None:
    fake = _install(monkeypatch, FakeCodexCli(**updates))
    with pytest.raises(ProviderExecutionError) as caught:
        preflight_codex_cli()

    assert caught.value.classification.category is ProviderErrorCategory.INVOCATION_ERROR
    assert fake.argument_lists == [["--version"]]


def test_required_capabilities_cover_every_flag_the_executor_sends() -> None:
    names = {name for name, _ in REQUIRED_EXEC_CAPABILITIES}
    assert {
        "--sandbox",
        "--ephemeral",
        "--color",
        "--output-last-message",
        "--cd",
        "--skip-git-repo-check",
        "--model",
    } <= names
    assert any("stdin" in name for name in names)
    assert any("read-only" in name for name in names)


@pytest.mark.parametrize(("name", "marker"), REQUIRED_EXEC_CAPABILITIES)
def test_preflight_refuses_an_installation_missing_a_required_capability(
    monkeypatch: pytest.MonkeyPatch, name: str, marker: str
) -> None:
    assert marker.encode("utf-8") in EXEC_HELP_OUTPUT
    fake = _install(
        monkeypatch, FakeCodexCli(exec_help=EXEC_HELP_OUTPUT.replace(marker.encode("utf-8"), b""))
    )
    with pytest.raises(ProviderExecutionError) as caught:
        preflight_codex_cli()

    classification = caught.value.classification
    assert classification.category is ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER
    assert name in classification.message
    assert fake.argument_lists == [["--version"], ["exec", "--help"]]


def test_preflight_refuses_when_exec_help_itself_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        FakeCodexCli(exec_help=b"error: unrecognized subcommand", exec_help_returncode=2),
    )
    with pytest.raises(ProviderExecutionError) as caught:
        preflight_codex_cli()

    category = caught.value.classification.category
    assert category is ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER


@pytest.mark.parametrize(
    "updates",
    [
        {"login_stdout": b"", "login_stderr": LOGGED_OUT, "login_returncode": 1},
        {"login_stdout": LOGGED_OUT, "login_returncode": 0},
        {
            "login_stdout": b"",
            "login_stderr": b"error: auth store unreadable",
            "login_returncode": 1,
        },
    ],
)
def test_preflight_refuses_a_logged_out_session(
    monkeypatch: pytest.MonkeyPatch, updates: dict[str, Any]
) -> None:
    fake = _install(monkeypatch, FakeCodexCli(**updates))
    with pytest.raises(ProviderExecutionError) as caught:
        preflight_codex_cli()

    classification = caught.value.classification
    assert classification.category is ProviderErrorCategory.AUTHENTICATION
    assert "codex login" in classification.message
    assert fake.argument_lists == [["--version"], ["exec", "--help"], ["login", "status"]]


def test_preflight_fails_closed_on_an_unrecognizable_login_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeCodexCli(login_stdout=b"status: green\n"))
    with pytest.raises(ProviderExecutionError) as caught:
        preflight_codex_cli()

    assert caught.value.classification.category is ProviderErrorCategory.MALFORMED_RESPONSE


def test_preflight_distinguishes_chatgpt_login_from_api_key_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeCodexCli(login_stdout=CHATGPT_LOGIN))
    assert preflight_codex_cli().auth_method == "chatgpt"

    _install(monkeypatch, FakeCodexCli(login_stdout=API_KEY_LOGIN))
    with pytest.raises(ProviderExecutionError) as caught:
        preflight_codex_cli()

    classification = caught.value.classification
    assert classification.category is ProviderErrorCategory.AUTHENTICATION
    assert "API key" in classification.message and "ChatGPT" in classification.message
    assert "sk-" not in classification.message
    assert "ABCDEFGHIJ" not in classification.message


def test_preflight_reports_unknown_optional_auth_metadata_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeCodexCli(login_stdout=b"Logged in\n"))
    report = preflight_codex_cli()
    assert report.authenticated
    assert report.auth_method is None
    assert report.subscription_type is None

    _install(monkeypatch, FakeCodexCli(login_stdout=CHATGPT_LOGIN))
    provider_report = preflight_provider(ProviderName.CODEX_CLI)
    details = dict(provider_report.details)
    assert details["auth_method"] == "chatgpt"
    assert details["subscription_type"] == "unknown"
    assert "plus" not in details["subscription_type"].lower()


def test_preflight_provider_reports_codex_as_implemented_and_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeCodexCli())
    report = preflight_provider(ProviderName.CODEX_CLI)

    assert report == ProviderPreflightReport(
        provider="codex-cli",
        supported=True,
        passed=True,
        details=(
            ("executable", "codex"),
            ("cli_version", "0.144.6"),
            ("non_interactive", "supported"),
            (
                "exec_capabilities",
                f"{len(REQUIRED_EXEC_CAPABILITIES)}/{len(REQUIRED_EXEC_CAPABILITIES)} present",
            ),
            ("authenticated", "yes"),
            ("auth_method", "chatgpt"),
            ("subscription_type", "unknown"),
            ("api_key_used", "no"),
        ),
    )
    record = record_preflight(
        ProviderName.CODEX_CLI, performed_at=datetime(2026, 9, 14, tzinfo=UTC)
    )
    assert record.supported and record.passed and record.authenticated
    assert record.executable_version == "0.144.6"
    assert record.auth_method == "chatgpt"
    assert record.api_key_used is False
    assert preflight_provider(ProviderName.FAKE).supported is False


def test_preflight_provider_refuses_codex_failures_instead_of_passing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, FakeCodexCli(login_stderr=LOGGED_OUT, login_returncode=1))
    with pytest.raises(ProviderExecutionError):
        preflight_provider(ProviderName.CODEX_CLI)
    record = record_preflight(
        ProviderName.CODEX_CLI, performed_at=datetime(2026, 9, 14, tzinfo=UTC)
    )
    assert record.supported and not record.passed and not record.authenticated
    assert record.failure_category is ProviderErrorCategory.AUTHENTICATION


def test_preflight_makes_no_inference_request_claims_nothing_and_sends_no_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claimed_execution(tmp_path)
    workspace = tmp_path / "workspace"
    before = _tree_digest(workspace)
    fake = _install(monkeypatch, FakeCodexCli())

    preflight_provider(ProviderName.CODEX_CLI)

    assert _tree_digest(workspace) == before
    for call in fake.calls:
        command = cast(list[str], call["command"])
        assert "-" not in command and "--output-last-message" not in command
        assert command[1:3] != ["exec", "-"] and "--model" not in command
        assert call["input"] == b""
        assert call.get("cwd") is None
    assert len(fake.calls) == 3


def test_cli_preflight_command_reports_and_refuses_without_touching_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakeCodexCli())
    passed = runner.invoke(app, ["check-translation-provider", "--provider", "codex-cli"])
    assert passed.exit_code == 0
    assert "codex-cli" in passed.stdout and "0.144.6" in passed.stdout
    assert "Preflight implemented" in passed.stdout and "yes" in passed.stdout
    assert "chatgpt" in passed.stdout and "unknown" in passed.stdout

    _install(monkeypatch, FakeCodexCli(login_stderr=LOGGED_OUT, login_returncode=1))
    refused = runner.invoke(app, ["check-translation-provider", "--provider", "codex-cli"])
    assert refused.exit_code == 1
    assert "not logged in" in refused.stderr

    _install(monkeypatch, FakeCodexCli(login_stdout=API_KEY_LOGIN))
    api_key = runner.invoke(app, ["check-translation-provider", "--provider", "codex-cli"])
    assert api_key.exit_code == 1
    assert "sk-" not in api_key.stdout + api_key.stderr


def test_execution_is_refused_before_an_attempt_when_codex_preflight_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, run_path, job_id, token = _claimed_execution(tmp_path)
    config_path = tmp_path / "config/book.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "translation_provider_models:\n  fake: [fake-v1]\n  codex-cli: []\n",
        encoding="utf-8",
    )
    before = _tree_digest(run_path)
    fake = _install(monkeypatch, FakeCodexCli(login_stderr=LOGGED_OUT, login_returncode=1))
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
            "codex-cli",
        ],
    )

    assert result.exit_code == 1
    assert "preflight failed for codex-cli" in result.stderr
    assert token not in result.stdout + result.stderr
    assert _tree_digest(run_path) == before
    assert fake.argument_lists == [["--version"], ["exec", "--help"], ["login", "status"]]


def test_claude_code_preflight_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClaudeCli()
    monkeypatch.setattr(subprocess, "run", fake)
    report = preflight_provider(ProviderName.CLAUDE_CODE)

    assert [cast(list[str], call["command"]) for call in fake.calls] == [
        ["claude", "--version"],
        ["claude", "--help"],
        ["claude", "auth", "status", "--json"],
    ]
    assert report == ProviderPreflightReport(
        provider="claude-code",
        supported=True,
        passed=True,
        details=(
            ("executable", "claude"),
            ("cli_version", "2.1.233"),
            ("non_interactive", "supported"),
            ("authenticated", "yes"),
            ("auth_method", "claude.ai"),
            ("api_provider", "firstParty"),
            ("subscription_type", "max"),
            ("api_key_used", "no"),
        ),
    )
