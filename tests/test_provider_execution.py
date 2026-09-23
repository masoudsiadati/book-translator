import os
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import (
    ProviderErrorCategory,
    ProviderName,
    TranslationAttemptStatus,
    TranslationJobStatus,
    TranslationRunStatus,
)
from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
from book_translator.provider_execution import (
    ExecutionPlan,
    ExecutorError,
    ProviderExecutionError,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
    execute_translation_job,
    list_attempts,
    plan_execution,
    validate_attempt,
)
from book_translator.provider_execution.errors import ErrorClassification
from book_translator.provider_execution.providers.codex_cli import CodexCliTranslationProvider
from book_translator.provider_execution.providers.fake import FakeTranslationProvider
from book_translator.provider_execution.providers.openai_responses import OpenAIResponsesProvider
from book_translator.provider_execution.result_validation import extract_translation
from book_translator.translation_queue import claim_job, load_run_summary, recover_run, release_job
from tests.test_translation_queue import NOW, _run

EXECUTION_NOW = NOW + timedelta(seconds=10)
runner = CliRunner()


def _provider_request(*, model: str | None = None) -> ProviderRequest:
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
        100,
    )


def _claimed_execution(
    root: Path, *, max_attempts: int = 3, lease_seconds: int = 900
) -> tuple[str, Path, str, str]:
    run_id, run_path = _run(root, max_attempts=max_attempts)
    execute_prompt_packages(plan_prompt_packages(run_id, project_root=root, now=lambda: NOW))
    claim = claim_job(
        run_id,
        project_root=root,
        worker_id="worker",
        lease_seconds=lease_seconds,
        now=lambda: NOW,
        token_factory=lambda: "secret-claim-token",
    )
    return run_id, run_path, claim.job.job_id, claim.claim_token


def _plan(
    root: Path,
    run_id: str,
    job_id: str,
    token: str,
    *,
    now: Callable[[], datetime] = lambda: EXECUTION_NOW,
) -> ExecutionPlan:
    return plan_execution(
        run_id,
        job_id,
        project_root=root,
        worker_id="worker",
        claim_token=token,
        provider=ProviderName.FAKE,
        model="fake-v1",
        timeout_seconds=120,
        max_output_tokens=1000,
        now=now,
    )


def test_fake_provider_protocol_and_scenarios() -> None:
    request = ProviderRequest(
        "request",
        "key",
        "run",
        "job",
        1,
        "system",
        "user",
        "en",
        "fa",
        "contract",
        "fake-v1",
        30,
        100,
    )
    success = FakeTranslationProvider().execute(request)
    assert success.status == "completed"
    assert success.text_outputs[0].startswith("ترجمهٔ ساختگی")
    for scenario in (
        "timeout",
        "rate_limit",
        "authentication",
        "server_error",
        "interrupted_ambiguous",
    ):
        with pytest.raises(ProviderExecutionError):
            FakeTranslationProvider(scenario).execute(request)
    assert FakeTranslationProvider("empty").execute(request).text_outputs == ("",)
    assert len(FakeTranslationProvider("malformed").execute(request).text_outputs) == 2


def test_openai_adapter_preserves_roles_disables_tools_store_and_retries() -> None:
    calls: list[dict[str, object]] = []

    class Responses:
        def create(self, **kwargs: object) -> object:
            calls.append(kwargs)
            content = SimpleNamespace(type="output_text", text="ترجمهٔ ساختگی.\n")
            message = SimpleNamespace(type="message", role="assistant", content=[content])
            usage = SimpleNamespace(input_tokens=10, output_tokens=3, total_tokens=13)
            return SimpleNamespace(
                id="resp-test",
                model="gpt-test",
                status="completed",
                output=[message],
                usage=usage,
                incomplete_details=None,
            )

    client = SimpleNamespace(responses=Responses())
    request = ProviderRequest(
        "request",
        "key",
        "run",
        "job",
        1,
        "system text",
        "user text",
        "en",
        "fa",
        "contract",
        "gpt-test",
        30,
        100,
    )
    result = OpenAIResponsesProvider(client=client).execute(request)
    assert result.text_outputs == ("ترجمهٔ ساختگی.\n",)
    call = calls[0]
    inputs = cast(list[dict[str, object]], call["input"])
    assert [item["role"] for item in inputs] == ["system", "user"]
    assert call["tools"] == []
    assert call["tool_choice"] == "none"
    assert call["store"] is False
    assert call["extra_headers"] == {"Idempotency-Key": "key"}
    assert "api_key" not in str(call).lower()


def test_codex_cli_captures_official_final_output_and_enforces_safe_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("THIRD_PARTY_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://router.invalid")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured.update(command=command, **kwargs)
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_bytes("ترجمهٔ نهایی دقیق.\n".encode())
        return subprocess.CompletedProcess(command, 0, b"Codex banner and progress", b"diagnostic")

    monkeypatch.setattr(subprocess, "run", run)
    result = CodexCliTranslationProvider().execute(_provider_request())

    command = cast(list[str], captured["command"])
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--output-last-message" in command and "--ephemeral" in command
    assert "--model" not in command and command[-1] == "-"
    assert result.text_outputs == ("ترجمهٔ نهایی دقیق.\n",)
    assert "Codex banner" not in result.text_outputs[0]
    stdin = cast(bytes, captured["input"]).decode()
    assert stdin == (
        "<system-instructions>\nsystem text\n</system-instructions>\n\n"
        "<user-message>\nuser text\n</user-message>\n"
    )
    environment = cast(dict[str, str], captured["env"])
    assert "OPENAI_API_KEY" not in environment
    assert "THIRD_PARTY_API_KEY" not in environment
    assert "OPENAI_BASE_URL" not in environment


def test_codex_cli_explicit_model_adds_model_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        Path(command[command.index("--output-last-message") + 1]).write_text(
            "ترجمه.\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    CodexCliTranslationProvider().execute(_provider_request(model="configured-model"))
    assert commands[0][commands[0].index("--model") + 1] == "configured-model"


@pytest.mark.parametrize(
    ("effect", "category", "ambiguous"),
    [
        (FileNotFoundError(), ProviderErrorCategory.EXECUTABLE_NOT_FOUND, False),
        (
            subprocess.TimeoutExpired(["codex"], 30),
            ProviderErrorCategory.TIMEOUT,
            True,
        ),
        (OSError(), ProviderErrorCategory.INVOCATION_ERROR, False),
        (KeyboardInterrupt(), ProviderErrorCategory.INTERRUPTED, True),
    ],
)
def test_codex_cli_invocation_failures_are_classified(
    monkeypatch: pytest.MonkeyPatch,
    effect: BaseException,
    category: ProviderErrorCategory,
    ambiguous: bool,
) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise effect

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ProviderExecutionError) as caught:
        CodexCliTranslationProvider().execute(_provider_request())
    assert caught.value.classification.category is category
    assert caught.value.classification.ambiguous is ambiguous


@pytest.mark.parametrize(
    ("returncode", "output", "category"),
    [
        (7, b"ignored", ProviderErrorCategory.NONZERO_EXIT),
        (0, b" \n", ProviderErrorCategory.EMPTY_OUTPUT),
    ],
)
def test_codex_cli_process_and_output_failures(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    output: bytes,
    category: ProviderErrorCategory,
) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        Path(command[command.index("--output-last-message") + 1]).write_bytes(output)
        return subprocess.CompletedProcess(command, returncode, b"transcript", b"diagnostic")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ProviderExecutionError) as caught:
        CodexCliTranslationProvider().execute(_provider_request())
    assert caught.value.classification.category is category


def test_claim_token_worker_and_lease_are_enforced(tmp_path: Path) -> None:
    run_id, _, job_id, token = _claimed_execution(tmp_path)
    with pytest.raises(ExecutorError, match="token"):
        _plan(tmp_path, run_id, job_id, "wrong")
    with pytest.raises(ExecutorError, match="worker"):
        plan_execution(
            run_id,
            job_id,
            project_root=tmp_path,
            worker_id="other",
            claim_token=token,
            provider=ProviderName.FAKE,
            model="fake-v1",
            timeout_seconds=10,
            max_output_tokens=10,
            now=lambda: EXECUTION_NOW,
        )
    with pytest.raises(ExecutorError, match="expired"):
        _plan(tmp_path, run_id, job_id, token, now=lambda: NOW + timedelta(seconds=901))


def test_fake_success_publishes_private_attempt_and_updates_queue(tmp_path: Path) -> None:
    run_id, run_path, job_id, token = _claimed_execution(tmp_path)
    plan = _plan(tmp_path, run_id, job_id, token)
    result = execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        now=lambda: EXECUTION_NOW,
        monotonic=lambda: 1.0,
    )
    state = load_run_summary(run_id, project_root=tmp_path)
    job = state.jobs[0]
    assert result.attempt_status is TranslationAttemptStatus.SUCCEEDED
    assert job.status is TranslationJobStatus.SUCCEEDED
    assert job.successful_attempt == 1
    assert state.run.status is TranslationRunStatus.ACTIVE
    assert state.run.provider_succeeded_unassessed_count == 1
    attempts = list_attempts(run_id, job_id, project_root=tmp_path)
    assert len(attempts) == 1 and attempts[0].validation == "valid"
    directory = run_path / "jobs" / job.chunk_id / "attempts/001"
    assert (directory / "translation.md").is_file()
    assert "secret-claim-token" not in "".join(
        path.read_text(encoding="utf-8") for path in directory.iterdir()
    )
    request = (directory / "request.json").read_text(encoding="utf-8")
    assert "system text" not in request and "Alpha beta" not in request
    if os.name == "posix":
        assert directory.stat().st_mode & 0o077 == 0
        assert all(path.stat().st_mode & 0o077 == 0 for path in directory.iterdir())


def test_codex_cli_success_preserves_persian_without_transcript_and_updates_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, run_path, job_id, token = _claimed_execution(tmp_path)
    config_path = tmp_path / "config/book.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "translation_provider_models:\n  fake: [fake-v1]\n  codex-cli: []\n",
        encoding="utf-8",
    )
    final_text = "پاراگراف نخست با نویسهٔ فارسی.\n\nپاراگراف دوم.\n"

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        Path(command[command.index("--output-last-message") + 1]).write_bytes(
            final_text.encode("utf-8")
        )
        return subprocess.CompletedProcess(
            command,
            0,
            b"Codex CLI banner\nmodel metadata\nsession id\nreasoning\n",
            b"progress output\n",
        )

    monkeypatch.setattr(subprocess, "run", run)
    plan = plan_execution(
        run_id,
        job_id,
        project_root=tmp_path,
        worker_id="worker",
        claim_token=token,
        provider=ProviderName.CODEX_CLI,
        model=None,
        timeout_seconds=120,
        max_output_tokens=1000,
        now=lambda: EXECUTION_NOW,
    )
    result = execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        now=lambda: EXECUTION_NOW,
        monotonic=lambda: 1.0,
    )

    job = load_run_summary(run_id, project_root=tmp_path).jobs[0]
    attempt = list_attempts(run_id, job_id, project_root=tmp_path)[0].attempt
    translation = run_path / "jobs" / job.chunk_id / "attempts/001/translation.md"
    assert result.attempt_status is TranslationAttemptStatus.SUCCEEDED
    assert job.status is TranslationJobStatus.SUCCEEDED
    assert attempt.provider is ProviderName.CODEX_CLI
    assert attempt.model_requested is None and attempt.model_returned is None
    assert translation.read_bytes() == final_text.encode("utf-8")
    assert b"Codex CLI banner" not in translation.read_bytes()


@pytest.mark.parametrize(
    ("scenario", "job_status", "attempt_status"),
    [
        ("rate_limit", TranslationJobStatus.FAILED, TranslationAttemptStatus.FAILED),
        ("timeout", TranslationJobStatus.BLOCKED, TranslationAttemptStatus.AMBIGUOUS),
        ("empty", TranslationJobStatus.FAILED, TranslationAttemptStatus.FAILED),
        ("malformed", TranslationJobStatus.FAILED, TranslationAttemptStatus.FAILED),
        ("truncated", TranslationJobStatus.FAILED, TranslationAttemptStatus.FAILED),
    ],
)
def test_failure_and_ambiguity_publication(
    tmp_path: Path,
    scenario: str,
    job_status: TranslationJobStatus,
    attempt_status: TranslationAttemptStatus,
) -> None:
    run_id, _, job_id, token = _claimed_execution(tmp_path)
    plan = _plan(tmp_path, run_id, job_id, token)
    result = execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        fake_scenario=scenario,
        now=lambda: EXECUTION_NOW,
        monotonic=lambda: 1.0,
    )
    state = load_run_summary(run_id, project_root=tmp_path)
    assert result.attempt_status is attempt_status
    assert state.jobs[0].status is job_status
    assert not state.jobs[0].claim_token


def test_duplicate_success_and_existing_attempt_are_refused(tmp_path: Path) -> None:
    run_id, _, job_id, token = _claimed_execution(tmp_path)
    plan = _plan(tmp_path, run_id, job_id, token)
    execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        now=lambda: EXECUTION_NOW,
        monotonic=lambda: 1.0,
    )
    with pytest.raises(ExecutorError):
        _plan(tmp_path, run_id, job_id, token)


def test_dry_run_writes_nothing_and_preserves_claim(tmp_path: Path) -> None:
    run_id, run_path, job_id, token = _claimed_execution(tmp_path)
    before_run = (run_path / "run.yaml").read_bytes()
    job_path = next((run_path / "jobs").glob("*/job.yaml"))
    before_job = job_path.read_bytes()
    plan = _plan(tmp_path, run_id, job_id, token)
    result = execute_translation_job(plan, worker_id="worker", claim_token=token, dry_run=True)
    assert not result.provider_contacted
    assert (run_path / "run.yaml").read_bytes() == before_run
    assert job_path.read_bytes() == before_job
    assert not plan.attempt_directory.parent.exists()


def test_no_raw_response_still_persists_translation_and_usage(tmp_path: Path) -> None:
    run_id, _, job_id, token = _claimed_execution(tmp_path)
    plan = plan_execution(
        run_id,
        job_id,
        project_root=tmp_path,
        worker_id="worker",
        claim_token=token,
        provider=ProviderName.FAKE,
        model="fake-v1",
        timeout_seconds=120,
        max_output_tokens=1000,
        store_raw_response=False,
        now=lambda: EXECUTION_NOW,
    )
    execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        now=lambda: EXECUTION_NOW,
        monotonic=lambda: 1.0,
    )
    item = list_attempts(run_id, job_id, project_root=tmp_path)[0].attempt
    assert not item.raw_response_stored and item.response_file is None
    assert item.translation_file is not None and item.total_tokens == 120


def test_lease_expiring_during_call_becomes_ambiguous_and_blocked(tmp_path: Path) -> None:
    run_id, _, job_id, token = _claimed_execution(tmp_path)
    plan = _plan(tmp_path, run_id, job_id, token)
    times = iter((EXECUTION_NOW, EXECUTION_NOW, EXECUTION_NOW + timedelta(seconds=901)))
    result = execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        now=lambda: next(times),
        monotonic=lambda: 1.0,
    )
    assert result.attempt_status is TranslationAttemptStatus.AMBIGUOUS
    assert load_run_summary(run_id, project_root=tmp_path).jobs[0].status is (
        TranslationJobStatus.BLOCKED
    )


def test_reasoning_and_stray_translation_are_refused_before_contact(tmp_path: Path) -> None:
    run_id, run_path, job_id, token = _claimed_execution(tmp_path)
    with pytest.raises(ExecutorError, match="reasoning"):
        plan_execution(
            run_id,
            job_id,
            project_root=tmp_path,
            worker_id="worker",
            claim_token=token,
            provider=ProviderName.FAKE,
            model="fake-v1",
            timeout_seconds=10,
            max_output_tokens=10,
            reasoning_effort="high",
            now=lambda: EXECUTION_NOW,
        )
    job_directory = next((run_path / "jobs").iterdir())
    (job_directory / "translation.md").write_text("synthetic stray output\n", encoding="utf-8")
    with pytest.raises(ExecutorError, match="outside"):
        _plan(tmp_path, run_id, job_id, token)


def test_reservation_prevents_concurrent_calls(tmp_path: Path) -> None:
    run_id, _, job_id, token = _claimed_execution(tmp_path)
    plan = _plan(tmp_path, run_id, job_id, token)
    entered = threading.Event()
    release = threading.Event()

    class SlowProvider(FakeTranslationProvider):
        def execute(self, request: ProviderRequest) -> ProviderResult:
            entered.set()
            release.wait(timeout=5)
            return super().execute(request)

    def first() -> object:
        return execute_translation_job(
            plan,
            worker_id="worker",
            claim_token=token,
            provider=SlowProvider(),
            now=lambda: EXECUTION_NOW,
            monotonic=lambda: 1.0,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(first)
        assert entered.wait(timeout=5)
        with pytest.raises(ExecutorError):
            execute_translation_job(
                plan,
                worker_id="worker",
                claim_token=token,
                provider=FakeTranslationProvider(),
                now=lambda: EXECUTION_NOW,
            )
        release.set()
        future.result()


def test_clear_pre_send_failure_is_failed_not_ambiguous(tmp_path: Path) -> None:
    run_id, _, job_id, token = _claimed_execution(tmp_path)
    plan = _plan(tmp_path, run_id, job_id, token)

    class PreSend(FakeTranslationProvider):
        def execute(self, request: ProviderRequest) -> ProviderResult:
            raise ProviderExecutionError(
                ErrorClassification(
                    ProviderErrorCategory.CONNECTION_PRE_SEND,
                    True,
                    False,
                    True,
                    "synthetic connection failure before send",
                )
            )

    execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        provider=PreSend(),
        now=lambda: EXECUTION_NOW,
        monotonic=lambda: 1.0,
    )
    assert (
        load_run_summary(run_id, project_root=tmp_path).jobs[0].status
        is TranslationJobStatus.FAILED
    )


def test_crash_before_send_recovery_restores_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _, job_id, token = _claimed_execution(tmp_path)
    plan = _plan(tmp_path, run_id, job_id, token)
    import book_translator.provider_execution.executor as executor

    def fail_before_send(plan: ExecutionPlan, now: datetime) -> None:
        raise OSError("injected pre-send crash")

    monkeypatch.setattr(executor, "_mark_requesting", fail_before_send)
    with pytest.raises(OSError):
        execute_translation_job(
            plan,
            worker_id="worker",
            claim_token=token,
            now=lambda: EXECUTION_NOW,
        )
    recovered = recover_run(run_id, project_root=tmp_path, now=lambda: EXECUTION_NOW)
    assert recovered.prepared_executions == (job_id,)
    assert load_run_summary(run_id, project_root=tmp_path).jobs[0].status is (
        TranslationJobStatus.CLAIMED
    )
    assert not plan.reservation_path.exists()


def test_crash_after_send_marker_is_not_automatically_recovered(tmp_path: Path) -> None:
    run_id, _, job_id, token = _claimed_execution(tmp_path)
    plan = _plan(tmp_path, run_id, job_id, token)

    class ProcessCrash(FakeTranslationProvider):
        def execute(self, request: ProviderRequest) -> ProviderResult:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        execute_translation_job(
            plan,
            worker_id="worker",
            claim_token=token,
            provider=ProcessCrash(),
            now=lambda: EXECUTION_NOW,
        )
    recovery = recover_run(run_id, project_root=tmp_path, now=lambda: EXECUTION_NOW)
    assert recovery.blockers
    assert load_run_summary(run_id, project_root=tmp_path).jobs[0].status is (
        TranslationJobStatus.RUNNING
    )


def test_translation_extraction_rejects_empty_multiple_truncated_and_echo() -> None:
    base = ProviderResult("id", "model", "completed", ("ترجمه.\n",), ProviderUsage())
    assert extract_translation(base, b"source\n").content.endswith(b"\n")
    for result in (
        ProviderResult("id", "model", "completed", ("",)),
        ProviderResult("id", "model", "completed", ("one", "two")),
        ProviderResult("id", "model", "incomplete", ("text",), incomplete_reason="limit"),
        ProviderResult("id", "model", "completed", ("source",)),
    ):
        with pytest.raises(ProviderExecutionError):
            extract_translation(result, b"source\n")


def test_attempt_validation_detects_mutation(tmp_path: Path) -> None:
    run_id, _, job_id, token = _claimed_execution(tmp_path)
    plan = _plan(tmp_path, run_id, job_id, token)
    execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        now=lambda: EXECUTION_NOW,
        monotonic=lambda: 1.0,
    )
    (plan.attempt_directory / "translation.md").write_text("mutated\n", encoding="utf-8")
    report = validate_attempt(run_id, job_id, 1, project_root=tmp_path)
    assert not report.valid and not report.translation_hash_valid


def test_exhausted_release_and_recovery_block_pending_jobs(tmp_path: Path) -> None:
    run_id, _, job_id, _ = _claimed_execution(tmp_path, max_attempts=1)
    released = release_job(
        run_id,
        job_id,
        project_root=tmp_path,
        worker_id="worker",
        now=lambda: EXECUTION_NOW,
    )
    assert released.status is TranslationJobStatus.BLOCKED
    assert released.attempt_count == 1
    assert load_run_summary(run_id, project_root=tmp_path).run.status is (
        TranslationRunStatus.COMPLETED_WITH_FAILURES
    )


def test_cli_dry_run_list_show_are_private_and_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _ = _run(tmp_path)
    execute_prompt_packages(plan_prompt_packages(run_id, project_root=tmp_path))
    claim = claim_job(
        run_id,
        project_root=tmp_path,
        worker_id="worker",
        lease_seconds=900,
        token_factory=lambda: "secret-claim-token",
    )
    job_id, token = claim.job.job_id, claim.claim_token
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
            "fake",
            "--model",
            "fake-v1",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert token not in result.stdout and "Alpha beta" not in result.stdout
    config_path = tmp_path / "config/book.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "translation_provider_models:\n  fake: [fake-v1]\n  codex-cli: []\n",
        encoding="utf-8",
    )
    codex_result = runner.invoke(
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
            "--dry-run",
        ],
    )
    assert codex_result.exit_code == 0
    assert "codex-cli" in codex_result.stdout and token not in codex_result.stdout
    help_result = runner.invoke(app, ["--help"])
    for command in (
        "execute-translation-job",
        "validate-translation-attempt",
        "list-translation-attempts",
        "show-translation-attempt",
    ):
        assert command in help_result.stdout


def test_status_and_attempt_cli_remain_structural(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _, job_id, token = _claimed_execution(tmp_path)
    plan = _plan(tmp_path, run_id, job_id, token)
    execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        now=lambda: EXECUTION_NOW,
        monotonic=lambda: 1.0,
    )
    monkeypatch.chdir(tmp_path)
    listed = runner.invoke(app, ["list-translation-attempts", run_id, job_id])
    shown = runner.invoke(app, ["show-translation-attempt", run_id, job_id, "1"])
    status = runner.invoke(app, ["status"])
    assert listed.exit_code == shown.exit_code == status.exit_code == 0
    combined = listed.stdout + shown.stdout + status.stdout
    assert "Alpha beta" not in combined and "ترجمهٔ ساختگی برای" not in combined
    assert "Provider attempts" in status.stdout
