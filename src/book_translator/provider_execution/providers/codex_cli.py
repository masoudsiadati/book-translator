"""Authenticated local Codex CLI translation adapter.

The adapter drives the user's installed ``codex`` executable in its non-interactive ``exec`` mode
on the existing CLI login. Its preflight establishes, without a model call, that the executable
exists, which version is installed, that ``codex exec`` still documents every capability the
adapter's command line relies on, and that the CLI reports an active login. Capabilities are
detected from the installed CLI's own help text rather than pinned to a version, so a newer
compatible Codex release passes on what it documents, not on its number.
"""

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from book_translator.models import ProviderErrorCategory
from book_translator.provider_execution.errors import ErrorClassification, ProviderExecutionError
from book_translator.provider_execution.protocol import ProviderRequest, ProviderResult

CODEX_CLI_ADAPTER_VERSION = "1.0.0"
CODEX_CLI_PREFLIGHT_TIMEOUT_SECONDS = 60

#: Everything the adapter's ``codex exec`` command line depends on, as (capability, help marker)
#: pairs. The marker must appear in ``codex exec --help``; an installation that documents fewer
#: fails before any job is claimed instead of at first use with a generic nonzero exit. The list
#: mirrors :meth:`CodexCliTranslationProvider.execute` and must change together with it.
REQUIRED_EXEC_CAPABILITIES: tuple[tuple[str, str], ...] = (
    ("--sandbox", "--sandbox"),
    ("--sandbox read-only", "read-only"),
    ("--ephemeral", "--ephemeral"),
    ("--color", "--color"),
    ("--color never", "never"),
    ("--output-last-message", "--output-last-message"),
    ("--cd", "--cd"),
    ("--skip-git-repo-check", "--skip-git-repo-check"),
    ("--model", "--model"),
    ("prompt from stdin", "stdin"),
)

_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
# ``codex login status`` answers in prose, so the preflight classifies it by fixed markers and
# never retains the text: an API-key login prints a masked key fragment beside the marker.
_LOGGED_OUT_MARKER = "not logged in"
_LOGGED_IN_MARKER = "logged in"
_CHATGPT_MARKERS = ("chatgpt",)
_API_KEY_MARKERS = ("api key", "api-key", "apikey")


@dataclass(frozen=True, slots=True)
class CodexCliPreflightReport:
    """Local, model-free evidence that the installed Codex CLI can serve a translation job.

    ``auth_method`` is ``"chatgpt"`` when the CLI says so and ``None`` when the login is active
    but its kind is not stated; ``subscription_type`` stays ``None`` because the CLI exposes no
    plan without reading its credential store, which the preflight never does.
    """

    executable: str
    cli_version: str
    non_interactive_supported: bool
    authenticated: bool
    auth_method: str | None
    subscription_type: str | None


def _failure(
    category: ProviderErrorCategory,
    message: str,
    *,
    retryable: bool = False,
    ambiguous: bool = False,
) -> ProviderExecutionError:
    return ProviderExecutionError(
        ErrorClassification(category, retryable, ambiguous, False, message)
    )


def _environment() -> dict[str, str]:
    """Inherit the authenticated CLI session without forwarding API credentials or routers."""
    blocked_names = {
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "AZURE_OPENAI_ENDPOINT",
    }
    return {
        name: value
        for name, value in os.environ.items()
        if not name.upper().endswith("_API_KEY") and name.upper() not in blocked_names
    }


def _render_stdin(request: ProviderRequest) -> bytes:
    return (
        "<system-instructions>\n"
        + request.system_message
        + "</system-instructions>\n\n<user-message>\n"
        + request.user_message
        + "</user-message>\n"
    ).encode("utf-8")


def _run_local(
    command: list[str], *, timeout_seconds: int, label: str
) -> subprocess.CompletedProcess[bytes]:
    """Run one read-only probe under the executor's own environment, with empty stdin."""
    try:
        return subprocess.run(
            command,
            input=b"",
            capture_output=True,
            env=_environment(),
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise _failure(
            ProviderErrorCategory.EXECUTABLE_NOT_FOUND,
            "Codex CLI executable was not found",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _failure(
            ProviderErrorCategory.TIMEOUT_PRE_SEND,
            f"Codex CLI preflight timed out ({label})",
            retryable=True,
        ) from exc
    except KeyboardInterrupt as exc:
        raise _failure(
            ProviderErrorCategory.INTERRUPTED,
            f"Codex CLI preflight was interrupted ({label})",
        ) from exc
    except OSError as exc:
        raise _failure(
            ProviderErrorCategory.INVOCATION_ERROR,
            f"Codex CLI process could not be invoked ({label})",
        ) from exc


def _probe_text(completed: subprocess.CompletedProcess[bytes]) -> str:
    """Decode a probe's combined output for classification only; it is never retained."""
    return (completed.stdout + b"\n" + completed.stderr).decode("utf-8", "replace")


def _detect_version(executable: str, timeout_seconds: int) -> str:
    completed = _run_local(
        [executable, "--version"], timeout_seconds=timeout_seconds, label="version"
    )
    if completed.returncode != 0:
        raise _failure(
            ProviderErrorCategory.INVOCATION_ERROR,
            "Codex CLI executable did not report a version",
        )
    match = _VERSION_PATTERN.search(_probe_text(completed))
    if match is None:
        raise _failure(
            ProviderErrorCategory.INVOCATION_ERROR,
            "Codex CLI version output was not recognizable",
        )
    return match.group(0)


def _verify_exec_capabilities(executable: str, timeout_seconds: int) -> None:
    completed = _run_local(
        [executable, "exec", "--help"], timeout_seconds=timeout_seconds, label="exec help"
    )
    help_text = _probe_text(completed) if completed.returncode == 0 else ""
    missing = tuple(name for name, marker in REQUIRED_EXEC_CAPABILITIES if marker not in help_text)
    if completed.returncode != 0 or missing:
        raise _failure(
            ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER,
            "Codex CLI does not support the required non-interactive capabilities: "
            + ", ".join(missing or ("exec --help",)),
        )


def _verify_login(executable: str, timeout_seconds: int) -> str | None:
    """Confirm an active ChatGPT-session login and return its stated kind, if any.

    An API-key login is refused: the adapter is documented as using the CLI's own session, the
    executor strips every ``*_API_KEY`` variable, and recovery evidence must never record an
    API-key path. The CLI's status line carries a masked key fragment in that case, which is one
    more reason the text is classified and dropped rather than reported.
    """
    completed = _run_local(
        [executable, "login", "status"], timeout_seconds=timeout_seconds, label="login status"
    )
    lowered = _probe_text(completed).lower()
    if _LOGGED_OUT_MARKER in lowered:
        raise _failure(
            ProviderErrorCategory.AUTHENTICATION,
            "Codex CLI is not logged in; run 'codex login' first",
        )
    if completed.returncode != 0:
        raise _failure(
            ProviderErrorCategory.AUTHENTICATION,
            "Codex CLI did not report an active login "
            f"(exit code {completed.returncode}); run 'codex login' first",
        )
    if _LOGGED_IN_MARKER not in lowered:
        raise _failure(
            ProviderErrorCategory.MALFORMED_RESPONSE,
            "Codex CLI login status was not recognizable",
        )
    if any(marker in lowered for marker in _API_KEY_MARKERS):
        raise _failure(
            ProviderErrorCategory.AUTHENTICATION,
            "Codex CLI is logged in with an API key; this provider requires a ChatGPT login",
        )
    return "chatgpt" if any(marker in lowered for marker in _CHATGPT_MARKERS) else None


def preflight_codex_cli(
    *,
    executable: str = "codex",
    timeout_seconds: int = CODEX_CLI_PREFLIGHT_TIMEOUT_SECONDS,
) -> CodexCliPreflightReport:
    """Verify the executable, its ``exec`` capabilities, and the local login without a model call.

    The three probes are ``--version``, ``exec --help``, and ``login status``. None of them starts
    a session, reads a credential file, claims a job, or receives any content: stdin is empty and
    no prompt argument is passed.
    """
    cli_version = _detect_version(executable, timeout_seconds)
    _verify_exec_capabilities(executable, timeout_seconds)
    auth_method = _verify_login(executable, timeout_seconds)
    return CodexCliPreflightReport(
        executable=executable,
        cli_version=cli_version,
        non_interactive_supported=True,
        authenticated=True,
        auth_method=auth_method,
        subscription_type=None,
    )


class CodexCliTranslationProvider:
    def __init__(self, *, executable: str = "codex") -> None:
        self._executable = executable

    @property
    def provider_name(self) -> str:
        return "codex-cli"

    @property
    def adapter_version(self) -> str:
        return CODEX_CLI_ADAPTER_VERSION

    def execute(self, request: ProviderRequest) -> ProviderResult:
        with tempfile.TemporaryDirectory(prefix="book-translator-codex-") as temporary:
            temporary_path = Path(temporary)
            final_output = temporary_path / "final-message.txt"
            command = [
                self._executable,
                "exec",
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--color",
                "never",
                "--output-last-message",
                str(final_output),
                "--cd",
                str(temporary_path),
                "--skip-git-repo-check",
            ]
            if request.model is not None:
                command.extend(("--model", request.model))
            command.append("-")
            try:
                completed = subprocess.run(
                    command,
                    input=_render_stdin(request),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=temporary_path,
                    env=_environment(),
                    timeout=request.timeout_seconds,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise _failure(
                    ProviderErrorCategory.EXECUTABLE_NOT_FOUND,
                    "Codex CLI executable was not found",
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise _failure(
                    ProviderErrorCategory.TIMEOUT,
                    "Codex CLI execution timed out after process start",
                    retryable=True,
                    ambiguous=True,
                ) from exc
            except KeyboardInterrupt as exc:
                raise _failure(
                    ProviderErrorCategory.INTERRUPTED,
                    "Codex CLI execution was interrupted after process start",
                    ambiguous=True,
                ) from exc
            except OSError as exc:
                raise _failure(
                    ProviderErrorCategory.INVOCATION_ERROR,
                    "Codex CLI process could not be invoked",
                ) from exc
            if completed.returncode != 0:
                raise _failure(
                    ProviderErrorCategory.NONZERO_EXIT,
                    f"Codex CLI exited unsuccessfully (exit code {completed.returncode})",
                )
            try:
                output = final_output.read_bytes()
                text = output.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise _failure(
                    ProviderErrorCategory.INVOCATION_ERROR,
                    "Codex CLI final output could not be read as UTF-8",
                ) from exc
            if not text.strip():
                raise _failure(
                    ProviderErrorCategory.EMPTY_OUTPUT,
                    "Codex CLI returned an empty final assistant answer",
                    retryable=True,
                )
            return ProviderResult(
                response_id=None,
                model=None,
                status="completed",
                text_outputs=(text,),
                sanitized_response={
                    "model": request.model,
                    "status": "completed",
                    "capture": "output-last-message",
                },
            )
