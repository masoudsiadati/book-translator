"""Authenticated local Claude Code CLI translation adapter.

The adapter drives the user's installed ``claude`` executable in its non-interactive print mode.
It never uses an Anthropic API key, an alternate base URL, or a third-party inference router: the
existing Claude Code login is the only authentication path. Tools, MCP servers, skills, plugins,
hooks, project settings, and session persistence are disabled for every call, so translation is a
pure text-in / text-out exchange that cannot read or modify this repository.

Output is read from the ``stream-json`` transcript rather than the ``json`` result field: the
latter holds only the last assistant message, so a session that Claude Code continued past the
output token limit lost every earlier turn.  See ``claude_stream`` for the reconstruction rules.
"""

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from book_translator.models import ProviderErrorCategory
from book_translator.provider_execution.errors import ErrorClassification, ProviderExecutionError
from book_translator.provider_execution.protocol import (
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from book_translator.provider_execution.providers.claude_stream import (
    ClaudeStreamError,
    final_result_object,
    parse_claude_stream,
)

#: 1.2.0 identifies the answering model when a call bills more than one, which is the ordinary
#: shape of a Claude Code result and used to leave ``model_returned`` empty on every attempt.
#: 1.3.0 captures the whole session from ``--output-format stream-json`` instead of the JSON
#: ``result`` field, which holds only the last assistant message and silently dropped every
#: earlier turn when Claude Code continued past the output token limit.
CLAUDE_CODE_ADAPTER_VERSION = "1.3.0"
CLAUDE_CODE_PROVIDER_NAME = "claude-code"
CLAUDE_CODE_PREFLIGHT_TIMEOUT_SECONDS = 60
# A failing call must stay diagnosable without becoming a place where secrets or prose accumulate,
# so a retained diagnostic is redacted first and then hard-capped at this many characters.
CLAUDE_CODE_DIAGNOSTIC_LIMIT = 240

# Flags the adapter relies on; preflight refuses an installation that does not document all of them.
REQUIRED_CLI_FLAGS = (
    "--print",
    "--output-format",
    "--verbose",
    "--system-prompt",
    "--tools",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--safe-mode",
)
# The transcript format the adapter reconstructs from; the CLI documents it as an
# ``--output-format`` choice and refuses it in print mode without ``--verbose``.
STREAM_JSON_HELP_MARKERS = ("stream-json",)
# The file form of the system prompt keeps prompt text off the process command line. Installed CLIs
# document it either as its own entry or inside the combined `--system-prompt[-file]` reference, so
# either spelling counts as support; an installation that accepts neither fails at first use with an
# explicit unsupported-parameter classification rather than a generic nonzero exit.
SYSTEM_PROMPT_FILE_HELP_MARKERS = ("--system-prompt-file", "--system-prompt[-file]")
_UNKNOWN_OPTION_MARKER = "unknown option"

# Credentials and inference-routing overrides never reach the child process. Removing them keeps
# the call on the user's own Claude Code login instead of an API key, Bedrock, Vertex, or a proxy.
_BLOCKED_ENVIRONMENT_NAMES = frozenset(
    {
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "AZURE_OPENAI_ENDPOINT",
    }
)

_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")
_SUBTYPE_PATTERN = re.compile(r"^[a-z0-9_]{1,40}$")
_AUTHENTICATION_MARKERS = (
    "authentication_error",
    "invalid api key",
    "invalid bearer token",
    "not logged in",
    "oauth token has expired",
    "please run /login",
    "unauthorized",
)
_OUTPUT_LIMIT_MARKER = "output token maximum"
_RATE_LIMIT_MARKERS = (
    "rate_limit_error",
    "rate limit",
    "too many requests",
    "usage limit reached",
    "quota exceeded",
)
_SERVER_ERROR_MARKERS = (
    "overloaded_error",
    "api_error",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "upstream connect error",
)
_MODEL_MARKERS = (
    "not_found_error",
    "model not found",
    "invalid model",
    "unknown model",
    "unsupported model",
)
_USAGE_MARKERS = (
    _UNKNOWN_OPTION_MARKER,
    "unknown argument",
    "unknown command",
    "invalid option",
    "missing required argument",
    "required option",
)
_CONFIGURATION_MARKERS = (
    "invalid settings",
    "invalid config",
    "settings.json",
    "mcp config",
    "failed to parse",
)

# Redaction runs before any process text is retained. Each pattern removes a class of value that a
# diagnostic must never carry: account identity, endpoints, local filesystem layout, session
# identifiers, and anything token-shaped.
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_WINDOWS_PATH_PATTERN = re.compile(r"\b[A-Za-z]:\\[^\s\"']*")
_POSIX_PATH_PATTERN = re.compile(r"(?<![\w~])(?:/[\w.+-]+){2,}/?")
_UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
_LABELLED_SECRET_PATTERN = re.compile(
    r"\b(?:bearer|token|key|secret|password)\b[\s:=]+\S+", re.IGNORECASE
)
_PREFIXED_SECRET_PATTERN = re.compile(r"\b(?:sk|pk|ghp|gho|xox[a-z])[-_][A-Za-z0-9._-]{6,}")
_LONG_TOKEN_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
# The adapter's own output contract is Persian prose. Arabic-script characters therefore identify
# model output rather than a Claude Code diagnostic, and such a candidate is dropped entirely.
_PROSE_SCRIPT_PATTERN = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_WHITESPACE_PATTERN = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ClaudeCodePreflightReport:
    """Local, model-free evidence that the installed CLI can serve a translation job."""

    executable: str
    cli_version: str
    non_interactive_supported: bool
    authenticated: bool
    auth_method: str | None
    api_provider: str | None
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


def environment() -> dict[str, str]:
    """Inherit the authenticated CLI session without forwarding API keys or inference routers."""
    return {
        name: value
        for name, value in os.environ.items()
        if not name.upper().endswith("_API_KEY") and name.upper() not in _BLOCKED_ENVIRONMENT_NAMES
    }


def _sanitized_text(value: str) -> str:
    return _ANSI_PATTERN.sub("", value)


def redacted_diagnostic(value: str) -> str | None:
    """Reduce process text to a short, privacy-safe diagnostic, or ``None`` when nothing survives.

    Redaction is unconditional and ordered from the most specific value class to the least, so a
    secret can never survive by also matching a broader pattern. The result is capped in length
    because a diagnostic is evidence about a failure, never a transcript of one.
    """
    text = _CONTROL_PATTERN.sub(" ", _sanitized_text(value))
    text = _EMAIL_PATTERN.sub("<email>", text)
    text = _URL_PATTERN.sub("<url>", text)
    text = _WINDOWS_PATH_PATTERN.sub("<path>", text)
    text = _POSIX_PATH_PATTERN.sub("<path>", text)
    text = _LABELLED_SECRET_PATTERN.sub("<redacted>", text)
    text = _PREFIXED_SECRET_PATTERN.sub("<redacted>", text)
    text = _UUID_PATTERN.sub("<id>", text)
    text = _LONG_TOKEN_PATTERN.sub("<token>", text)
    text = _WHITESPACE_PATTERN.sub(" ", text).strip()
    if not text or _PROSE_SCRIPT_PATTERN.search(text):
        return None
    if len(text) > CLAUDE_CODE_DIAGNOSTIC_LIMIT:
        text = text[:CLAUDE_CODE_DIAGNOSTIC_LIMIT].rstrip() + "…"
    return text


def _result_payload(stdout: bytes) -> dict[str, Any] | None:
    """Return the CLI's final result object when it emitted one, including on a failing exit."""
    try:
        return final_result_object(_sanitized_text(stdout.decode("utf-8")))
    except UnicodeError:
        return None


def _payload_subtype(payload: dict[str, Any]) -> str:
    subtype = payload.get("subtype")
    return subtype if isinstance(subtype, str) and _SUBTYPE_PATTERN.match(subtype) else "unknown"


def _payload_error_text(payload: dict[str, Any]) -> str:
    """Collect the payload's error prose only; a successful ``result`` field is translated prose."""
    if payload.get("is_error") is not True and _payload_subtype(payload) == "success":
        return ""
    error = payload.get("error")
    errors = payload.get("errors")
    candidates = [
        payload.get("result"),
        error.get("message") if isinstance(error, dict) else error,
        *(errors if isinstance(errors, list) else ()),
    ]
    return " ".join(value for value in candidates if isinstance(value, str) and value)


def _payload_status(payload: dict[str, Any]) -> int | None:
    status = payload.get("api_error_status")
    if isinstance(status, bool):
        return None
    if isinstance(status, int):
        return status
    return int(status) if isinstance(status, str) and status.isdigit() else None


def _payload_detail(payload: dict[str, Any]) -> str:
    """Describe a failing payload structurally first, then with its redacted error prose."""
    parts = [f"subtype={_payload_subtype(payload)}"]
    status = _payload_status(payload)
    if status is not None:
        parts.append(f"api_error_status={status}")
    message = redacted_diagnostic(_payload_error_text(payload))
    if message:
        parts.append(message)
    return "; ".join(parts)


def _stderr_detail(stderr: bytes) -> str:
    """Keep only the trailing diagnostic lines; earlier output is banner and progress noise."""
    lines = [line for line in stderr.decode("utf-8", "replace").splitlines() if line.strip()]
    return redacted_diagnostic(" ".join(lines[-3:])) or ""


def _run_local(
    command: list[str], *, timeout_seconds: int, label: str
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command,
            input=b"",
            capture_output=True,
            env=environment(),
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise _failure(
            ProviderErrorCategory.EXECUTABLE_NOT_FOUND,
            "Claude Code executable was not found",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _failure(
            ProviderErrorCategory.TIMEOUT_PRE_SEND,
            f"Claude Code preflight timed out ({label})",
            retryable=True,
        ) from exc
    except KeyboardInterrupt as exc:
        raise _failure(
            ProviderErrorCategory.INTERRUPTED,
            f"Claude Code preflight was interrupted ({label})",
        ) from exc
    except OSError as exc:
        raise _failure(
            ProviderErrorCategory.INVOCATION_ERROR,
            f"Claude Code process could not be invoked ({label})",
        ) from exc


def _decoded_stdout(completed: subprocess.CompletedProcess[bytes], label: str) -> str:
    try:
        return _sanitized_text(completed.stdout.decode("utf-8"))
    except UnicodeError as exc:
        raise _failure(
            ProviderErrorCategory.MALFORMED_RESPONSE,
            f"Claude Code output was not valid UTF-8 ({label})",
        ) from exc


def preflight_claude_code(
    *,
    executable: str = "claude",
    timeout_seconds: int = CLAUDE_CODE_PREFLIGHT_TIMEOUT_SECONDS,
) -> ClaudeCodePreflightReport:
    """Verify the executable, its non-interactive mode, and the local login without a model call."""
    version_process = _run_local(
        [executable, "--version"], timeout_seconds=timeout_seconds, label="version"
    )
    if version_process.returncode != 0:
        raise _failure(
            ProviderErrorCategory.INVOCATION_ERROR,
            "Claude Code executable did not report a version",
        )
    version_match = _VERSION_PATTERN.search(_decoded_stdout(version_process, "version"))
    if version_match is None:
        raise _failure(
            ProviderErrorCategory.INVOCATION_ERROR,
            "Claude Code version output was not recognizable",
        )

    help_process = _run_local([executable, "--help"], timeout_seconds=timeout_seconds, label="help")
    help_text = _decoded_stdout(help_process, "help")
    missing = tuple(flag for flag in REQUIRED_CLI_FLAGS if flag not in help_text)
    if not any(marker in help_text for marker in SYSTEM_PROMPT_FILE_HELP_MARKERS):
        missing = (*missing, SYSTEM_PROMPT_FILE_HELP_MARKERS[0])
    if not any(marker in help_text for marker in STREAM_JSON_HELP_MARKERS):
        missing = (*missing, STREAM_JSON_HELP_MARKERS[0])
    if help_process.returncode != 0 or missing:
        raise _failure(
            ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER,
            "Claude Code does not support the required non-interactive flags: "
            + ", ".join(missing or ("--help",)),
        )

    auth_process = _run_local(
        [executable, "auth", "status", "--json"], timeout_seconds=timeout_seconds, label="auth"
    )
    if auth_process.returncode != 0:
        raise _failure(
            ProviderErrorCategory.AUTHENTICATION,
            "Claude Code could not report authentication status",
        )
    try:
        status = json.loads(_decoded_stdout(auth_process, "auth"))
    except ValueError as exc:
        raise _failure(
            ProviderErrorCategory.MALFORMED_RESPONSE,
            "Claude Code authentication status was not valid JSON",
        ) from exc
    if not isinstance(status, dict) or status.get("loggedIn") is not True:
        raise _failure(
            ProviderErrorCategory.AUTHENTICATION,
            "Claude Code is not logged in; run 'claude auth login' first",
        )
    return ClaudeCodePreflightReport(
        executable=executable,
        cli_version=version_match.group(0),
        non_interactive_supported=True,
        authenticated=True,
        auth_method=_optional_string(status.get("authMethod")),
        api_provider=_optional_string(status.get("apiProvider")),
        subscription_type=_optional_string(status.get("subscriptionType")),
    )


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _denial_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


#: The per-model counters that must agree with the call's own usage for an entry to be the model
#: that produced the answer, paired with their spelling in the top-level usage object.
_MODEL_USAGE_COUNTERS = (
    ("inputTokens", "input_tokens"),
    ("outputTokens", "output_tokens"),
    ("cacheCreationInputTokens", "cache_creation_input_tokens"),
    ("cacheReadInputTokens", "cache_read_input_tokens"),
)


def _answering_model(payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Return the ``modelUsage`` entry that produced the result, or ``None`` when it is unclear.

    A Claude Code call can legitimately bill more than one model: the answer comes from the
    selected model while a small auxiliary model may run alongside it. Treating "more than one
    entry" as unknowable, which is what this adapter used to do, threw away the provenance on
    every real call and left ``model_returned`` empty in the record.

    The call's own top-level ``usage`` object reports the answering model's token counters, so the
    entry whose four counters all agree with it is that model. The match must be unique: zero or
    several agreeing entries mean the evidence does not identify one, and the adapter records
    nothing rather than choosing.
    """
    entries = payload.get("modelUsage")
    if not isinstance(entries, dict) or not entries:
        return None
    typed = {name: detail for name, detail in entries.items() if isinstance(detail, dict)}
    if len(typed) == 1:
        return next(iter(typed.items()))
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    matches = [
        (name, detail)
        for name, detail in typed.items()
        if all(
            _optional_integer(detail.get(model_key)) == _optional_integer(usage.get(usage_key))
            for model_key, usage_key in _MODEL_USAGE_COUNTERS
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _model_provenance(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return the returned model, canonical name, and inference provider when unambiguous."""
    answering = _answering_model(payload)
    if answering is None:
        return None, None, None
    name, detail = answering
    return (
        _optional_string(name),
        _optional_string(detail.get("canonicalModel")),
        _optional_string(detail.get("provider")),
    )


def _usage(payload: dict[str, Any]) -> ProviderUsage:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return ProviderUsage()
    input_tokens = _optional_integer(usage.get("input_tokens"))
    output_tokens = _optional_integer(usage.get("output_tokens"))
    billed = [
        value
        for value in (
            input_tokens,
            output_tokens,
            _optional_integer(usage.get("cache_creation_input_tokens")),
            _optional_integer(usage.get("cache_read_input_tokens")),
        )
        if value is not None
    ]
    return ProviderUsage(input_tokens, output_tokens, sum(billed) if billed else None)


def _marker_category(text: str) -> tuple[ProviderErrorCategory, bool] | None:
    """Map known fixed markers to a category and whether the failure is worth retrying.

    The order runs from the most specific failure to the least, so a message that mentions several
    markers is classified by the one that actually determines the operator's next step.
    """
    lowered = text.lower()
    for markers, category, retryable in (
        ((_OUTPUT_LIMIT_MARKER,), ProviderErrorCategory.OUTPUT_TRUNCATED, True),
        (_AUTHENTICATION_MARKERS, ProviderErrorCategory.AUTHENTICATION, False),
        (_USAGE_MARKERS, ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER, False),
        (_MODEL_MARKERS, ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER, False),
        (_RATE_LIMIT_MARKERS, ProviderErrorCategory.RATE_LIMIT, True),
        (_SERVER_ERROR_MARKERS, ProviderErrorCategory.SERVER_ERROR, True),
        (_CONFIGURATION_MARKERS, ProviderErrorCategory.INVALID_REQUEST, False),
    ):
        if any(marker in lowered for marker in markers):
            return category, retryable
    return None


def _status_category(status: int | None) -> tuple[ProviderErrorCategory, bool] | None:
    """Classify a reported HTTP status, which is more precise than any prose marker."""
    if status is None:
        return None
    if status == 401:
        return ProviderErrorCategory.AUTHENTICATION, False
    if status == 403:
        return ProviderErrorCategory.AUTHORIZATION, False
    if status == 404:
        return ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER, False
    if status == 429:
        return ProviderErrorCategory.RATE_LIMIT, True
    if 400 <= status < 500:
        return ProviderErrorCategory.INVALID_REQUEST, False
    if status >= 500:
        return ProviderErrorCategory.SERVER_ERROR, True
    return None


def _reports_failure(payload: dict[str, Any]) -> bool:
    return payload.get("is_error") is True or _payload_subtype(payload) != "success"


def _refuse_error_payload(payload: dict[str, Any], *, exit_code: int | None = None) -> None:
    """Refuse a final result object that the CLI itself reported as unsuccessful."""
    detail = _payload_detail(payload)
    if exit_code is not None:
        detail = f"exit code {exit_code}; {detail}"
    matched = _status_category(_payload_status(payload)) or _marker_category(
        _payload_error_text(payload)
    )
    category, retryable = matched or (ProviderErrorCategory.SERVER_ERROR, True)
    raise _failure(
        category,
        f"Claude Code reported an unsuccessful result ({detail})",
        retryable=retryable,
    )


def _refuse_failing_exit(completed: subprocess.CompletedProcess[bytes]) -> None:
    """Classify a failing exit from whatever safe evidence the CLI left behind.

    A failing Claude Code run still reports itself structurally whenever it can, so the final result
    object is preferred and the trailing stderr lines are the fallback. Both are redacted and capped
    before they are retained, and neither is ever allowed to reach the translation output.
    """
    exit_code = completed.returncode
    payload = _result_payload(completed.stdout)
    if payload is not None and _reports_failure(payload):
        _refuse_error_payload(payload, exit_code=exit_code)
    detail = _stderr_detail(completed.stderr)
    # Markers are matched against the raw process text because redaction may legitimately remove
    # the very substring a marker names; only the retained detail is the redacted copy.
    matched = _marker_category(
        (completed.stdout + b"\n" + completed.stderr).decode("utf-8", "replace")
    )
    if matched is not None:
        category, retryable = matched
        raise _failure(
            category,
            f"Claude Code exited unsuccessfully (exit code {exit_code}; {detail})",
            retryable=retryable,
        )
    suffix = f"; {detail}" if detail else "; no safe diagnostic output"
    raise _failure(
        ProviderErrorCategory.NONZERO_EXIT,
        f"Claude Code exited unsuccessfully (exit code {exit_code}{suffix})",
    )


class ClaudeCodeTranslationProvider:
    """Text-in / text-out translation through the authenticated local Claude Code CLI."""

    def __init__(
        self,
        *,
        executable: str = "claude",
        preflight_report: ClaudeCodePreflightReport | None = None,
        preflight_timeout_seconds: int = CLAUDE_CODE_PREFLIGHT_TIMEOUT_SECONDS,
    ) -> None:
        self._executable = executable
        self._report = preflight_report
        self._preflight_timeout_seconds = preflight_timeout_seconds

    @property
    def provider_name(self) -> str:
        return CLAUDE_CODE_PROVIDER_NAME

    @property
    def adapter_version(self) -> str:
        return CLAUDE_CODE_ADAPTER_VERSION

    def preflight(self) -> ClaudeCodePreflightReport:
        """Return cached preflight evidence, performing the local checks at most once."""
        if self._report is None:
            self._report = preflight_claude_code(
                executable=self._executable, timeout_seconds=self._preflight_timeout_seconds
            )
        return self._report

    def _command(self, system_prompt_file: Path, request: ProviderRequest) -> list[str]:
        # stream-json writes every message of the session as one JSON line, which is the only
        # print format whose output survives Claude Code's own output-limit continuation; the
        # CLI requires --verbose beside it in print mode.
        command = [
            self._executable,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--system-prompt-file",
            str(system_prompt_file),
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--safe-mode",
            "--tools",
            "",
        ]
        if request.model is not None:
            command.extend(("--model", request.model))
        return command

    def execute(self, request: ProviderRequest) -> ProviderResult:
        report = self.preflight()
        with tempfile.TemporaryDirectory(prefix="book-translator-claude-code-") as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            system_prompt_file = root / "system-prompt.md"
            try:
                system_prompt_file.write_text(request.system_message, encoding="utf-8")
                os.chmod(system_prompt_file, 0o600)
            except OSError as exc:
                raise _failure(
                    ProviderErrorCategory.LOCAL_IO,
                    "Claude Code system prompt could not be staged privately",
                ) from exc
            command = self._command(system_prompt_file, request)
            child_environment = environment()
            child_environment["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(request.max_output_tokens)
            try:
                completed = subprocess.run(
                    command,
                    input=request.user_message.encode("utf-8"),
                    capture_output=True,
                    cwd=root,
                    env=child_environment,
                    timeout=request.timeout_seconds,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise _failure(
                    ProviderErrorCategory.EXECUTABLE_NOT_FOUND,
                    "Claude Code executable was not found",
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise _failure(
                    ProviderErrorCategory.TIMEOUT,
                    "Claude Code execution timed out after process start",
                    retryable=True,
                    ambiguous=True,
                ) from exc
            except KeyboardInterrupt as exc:
                raise _failure(
                    ProviderErrorCategory.INTERRUPTED,
                    "Claude Code execution was interrupted after process start",
                    ambiguous=True,
                ) from exc
            except OSError as exc:
                raise _failure(
                    ProviderErrorCategory.INVOCATION_ERROR,
                    "Claude Code process could not be invoked",
                ) from exc
            if completed.returncode != 0:
                _refuse_failing_exit(completed)
            return self._result(completed, request, report)

    def _result(
        self,
        completed: subprocess.CompletedProcess[bytes],
        request: ProviderRequest,
        report: ClaudeCodePreflightReport,
    ) -> ProviderResult:
        stdout = _decoded_stdout(completed, "execution")
        # The CLI's own verdict comes first: an error result is classified as what it reports,
        # and only a successful session is held to the reconstruction rules below.
        payload = final_result_object(stdout)
        if payload is None:
            raise _failure(
                ProviderErrorCategory.MALFORMED_RESPONSE,
                "Claude Code structured output carried no final result object",
            )
        if payload.get("is_error") is True or payload.get("subtype") != "success":
            _refuse_error_payload(payload)
        try:
            stream = parse_claude_stream(stdout)
        except ClaudeStreamError as exc:
            # Retryable: the answer may well have arrived, but nothing here may guess at it, and
            # a fresh call is the only remedy that cannot publish a partial translation.
            raise _failure(
                ProviderErrorCategory.MALFORMED_RESPONSE,
                f"Claude Code session could not be reconstructed losslessly ({exc})",
                retryable=True,
            ) from exc
        translation = _sanitized_text(stream.text)
        if not translation.strip():
            raise _failure(
                ProviderErrorCategory.EMPTY_OUTPUT,
                "Claude Code returned an empty final assistant message",
                retryable=True,
            )
        model_returned, canonical_model, model_provider = _model_provenance(payload)
        return ProviderResult(
            response_id=stream.session_id,
            model=model_returned,
            status="completed",
            text_outputs=(translation,),
            usage=_usage(payload),
            sanitized_response={
                "provider": CLAUDE_CODE_PROVIDER_NAME,
                "adapter_version": CLAUDE_CODE_ADAPTER_VERSION,
                "execution_mode": "local-cli-print-stream-json",
                "capture": "primary-assistant-messages",
                "cli_executable": report.executable,
                "cli_version": report.cli_version,
                "authentication": "claude-code-login",
                "auth_method": report.auth_method,
                "api_provider": report.api_provider,
                "model_requested": request.model,
                "model_returned": model_returned,
                "canonical_model": canonical_model,
                "model_inference_provider": model_provider,
                "max_output_tokens_enforced": True,
                "max_output_tokens": request.max_output_tokens,
                "tools_enabled": False,
                "session_persistence": False,
                "status": "completed",
                "subtype": "success",
                "stop_reason": _optional_string(payload.get("stop_reason")),
                "num_turns": _optional_integer(payload.get("num_turns")),
                "assistant_message_count": stream.assistant_message_count,
                "user_event_count": stream.user_event_count,
                "duration_api_ms": _optional_integer(payload.get("duration_api_ms")),
                "permission_denial_count": _denial_count(payload.get("permission_denials")),
            },
        )
