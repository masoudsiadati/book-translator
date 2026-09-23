"""Book-scoped provider configuration and pre-provider failure diagnostics.

Provider execution for a translation run must read the configuration of the book the run's frozen
snapshot came from — `config/books/<book>.yaml` for a book-scoped workspace, the legacy
`config/book.yaml` otherwise — and a worker that fails after claiming a job but before contacting a
provider must say why. Everything here is synthetic and no provider process is ever started:
`subprocess` is patched to fail loudly for the whole module, and every execution receives an
in-process provider stub.
"""

import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.config import BookConfig, load_book_config
from book_translator.models import ProviderName, TranslationJobStatus
from book_translator.paths import ProjectPaths
from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
from book_translator.provider_execution import (
    ExecutorError,
    ensure_provider_model_allowed,
    plan_execution,
)
from book_translator.translation_queue import (
    QueueError,
    claim_job,
    create_run,
    load_run_summary,
    plan_run,
    recover_run,
    resolve_run_book,
)
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import TranslationWorkerError, run_translation_worker
from tests.test_book_workspaces import RUN_NOW, _fully_chunk, _resource, _two_book_project
from tests.test_translation_queue import NOW
from tests.test_translation_worker import (
    SyntheticProvider,
    _jobs_by_id,
    _options,
    _prepared_run,
    _queue_bytes,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXECUTION_NOW = RUN_NOW + timedelta(seconds=10)
PERSIAN = "ترجمه نخست.\n\nترجمه دوم.\n"
runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_provider_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a real provider process must never be started by these tests")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def _set_provider_models(config_path: Path, providers: dict[str, list[str]]) -> None:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["translation_provider_models"] = providers
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _two_book_runs(root: Path) -> tuple[str, str]:
    """One prepared run per book: the legacy book in `workspace/`, beta under `workspace/books/`."""
    alpha, beta = _two_book_project(root)
    # The shared two-book fixture's prompt is too small to render a package; give it every
    # placeholder the renderer requires (still synthetic, still shared by both books).
    _resource(
        root,
        "translation-prompt",
        "prompt",
        (
            "Chapter {{ chapter_number }} part {{ part_number }}\n"
            "Glossary: {{ glossary }}\nContext: {{ previous_context }}\n"
            "Source: {{ source_text }}\n"
        ),
    )
    _resource(
        root,
        "glossary",
        "glossary",
        "terms: {}\ncharacters: {}\nplaces: {}\ngroups: {}\nunresolved_terms: []\n",
    )
    _fully_chunk(root, alpha, None)
    _fully_chunk(root, beta, "beta")
    legacy_snapshot = create_snapshot(plan_snapshot(project_root=root)).snapshot_id
    beta_snapshot = create_snapshot(plan_snapshot(project_root=root, book="beta")).snapshot_id
    legacy_run = create_run(
        plan_run(legacy_snapshot, project_root=root, now=lambda: RUN_NOW)
    ).run_id
    beta_run = create_run(
        plan_run(beta_snapshot, project_root=root, book="beta", now=lambda: RUN_NOW)
    ).run_id
    for run_id in (legacy_run, beta_run):
        execute_prompt_packages(
            plan_prompt_packages(run_id, project_root=root, now=lambda: RUN_NOW)
        )
    return legacy_run, beta_run


def _claimed(
    root: Path, run_id: str, *, now: Callable[[], datetime] | None = lambda: RUN_NOW
) -> tuple[str, str]:
    claim = claim_job(
        run_id,
        project_root=root,
        worker_id="worker",
        now=now,
        token_factory=lambda: "secret-claim-token",
    )
    return claim.job.job_id, claim.claim_token


def _plan(root: Path, run_id: str, job_id: str, token: str, model: str | None) -> Any:
    return plan_execution(
        run_id,
        job_id,
        project_root=root,
        worker_id="worker",
        claim_token=token,
        provider=ProviderName.CLAUDE_CODE,
        model=model,
        timeout_seconds=120,
        max_output_tokens=1000,
        now=lambda: EXECUTION_NOW,
    )


def _config(providers: dict[str, list[str]]) -> BookConfig:
    return BookConfig.model_validate(
        {
            "book_id": "synthetic",
            "title_en": "Synthetic",
            "author": "Author",
            "source_language": "en",
            "target_language": "fa",
            "pdf_path": "input/book.pdf",
            "chunk_target_words": 10,
            "chunk_max_words": 20,
            "previous_context_words": 2,
            "output_format": "epub3",
            "translation_provider_models": providers,
        }
    )


# --- model allow-list policy --------------------------------------------------------------------


def test_cli_session_providers_accept_only_the_session_default_with_an_empty_list() -> None:
    config = _config({"fake": ["fake-v1"], "claude-code": [], "codex-cli": []})
    ensure_provider_model_allowed(config, ProviderName.CLAUDE_CODE, None)
    ensure_provider_model_allowed(config, ProviderName.CODEX_CLI, None)
    ensure_provider_model_allowed(config, ProviderName.FAKE, "fake-v1")
    with pytest.raises(ExecutorError, match="not allowed by project configuration") as refused:
        ensure_provider_model_allowed(config, ProviderName.CLAUDE_CODE, "opus")
    assert "claude-code" in str(refused.value) and "opus" in str(refused.value)
    with pytest.raises(ExecutorError, match="not allowed by project configuration"):
        ensure_provider_model_allowed(config, ProviderName.CODEX_CLI, "gpt-5-codex")
    with pytest.raises(ExecutorError, match="not allowed by project configuration"):
        ensure_provider_model_allowed(config, ProviderName.OPENAI, "gpt-5")
    with pytest.raises(ExecutorError, match="not allowed by project configuration"):
        ensure_provider_model_allowed(config, ProviderName.OPENAI, None)


def test_an_explicit_allow_list_admits_exactly_its_entries_and_keeps_the_session_default() -> None:
    config = _config({"fake": ["fake-v1"], "claude-code": ["opus"]})
    ensure_provider_model_allowed(config, ProviderName.CLAUDE_CODE, "opus")
    ensure_provider_model_allowed(config, ProviderName.CLAUDE_CODE, None)
    with pytest.raises(ExecutorError, match="not allowed by project configuration") as refused:
        ensure_provider_model_allowed(config, ProviderName.CLAUDE_CODE, "sonnet")
    assert "sonnet" in str(refused.value) and "opus" in str(refused.value)


def test_committed_configurations_allow_claude_code_opus_only_for_book_six() -> None:
    book_six = load_book_config(PROJECT_ROOT / "config/books/lord-of-chaos.yaml")
    assert book_six.translation_provider_models["claude-code"] == ["opus"]
    ensure_provider_model_allowed(book_six, ProviderName.CLAUDE_CODE, "opus")
    ensure_provider_model_allowed(book_six, ProviderName.CLAUDE_CODE, None)
    ensure_provider_model_allowed(book_six, ProviderName.CODEX_CLI, None)

    book_five = load_book_config(PROJECT_ROOT / "config/book.yaml")
    assert book_five.translation_provider_models["claude-code"] == []
    assert book_five.translation_provider_models["codex-cli"] == []
    ensure_provider_model_allowed(book_five, ProviderName.CLAUDE_CODE, None)
    with pytest.raises(ExecutorError, match="not allowed by project configuration"):
        ensure_provider_model_allowed(book_five, ProviderName.CLAUDE_CODE, "opus")


# --- book-scoped configuration resolution -------------------------------------------------------


def test_book_scoped_run_plans_against_its_own_book_configuration(tmp_path: Path) -> None:
    legacy_run, beta_run = _two_book_runs(tmp_path)
    _set_provider_models(
        ProjectPaths(tmp_path, "beta").book_config, {"fake": ["fake-v1"], "claude-code": ["opus"]}
    )
    assert resolve_run_book(beta_run, project_root=tmp_path) == "beta"
    assert load_run_summary(beta_run, project_root=tmp_path).book == "beta"
    assert resolve_run_book(legacy_run, project_root=tmp_path) is None
    assert load_run_summary(legacy_run, project_root=tmp_path).book is None

    job_id, token = _claimed(tmp_path, beta_run)
    before = _queue_bytes(tmp_path, beta_run)
    plan = _plan(tmp_path, beta_run, job_id, token, "opus")
    assert plan.request.model == "opus"
    assert plan.request.metadata["provider"] == "claude-code"
    assert plan.request_payload["model"] == "opus"
    assert not plan.attempt_directory.exists()
    assert _queue_bytes(tmp_path, beta_run) == before
    # The alias is the requested model: there is no canonical model-resolution layer to rewrite it.
    with pytest.raises(ExecutorError, match="not allowed by project configuration"):
        _plan(tmp_path, beta_run, job_id, token, "sonnet")


def test_legacy_run_keeps_reading_the_legacy_configuration(tmp_path: Path) -> None:
    legacy_run, _ = _two_book_runs(tmp_path)
    _set_provider_models(
        ProjectPaths(tmp_path, "beta").book_config, {"fake": ["fake-v1"], "claude-code": ["opus"]}
    )
    job_id, token = _claimed(tmp_path, legacy_run)
    # Beta's allowance means nothing to the legacy book, whose configuration lacks claude-code.
    with pytest.raises(ExecutorError, match="not allowed by project configuration"):
        _plan(tmp_path, legacy_run, job_id, token, "opus")
    with pytest.raises(ExecutorError, match="not allowed by project configuration"):
        _plan(tmp_path, legacy_run, job_id, token, None)

    _set_provider_models(
        ProjectPaths(tmp_path).book_config, {"fake": ["fake-v1"], "claude-code": []}
    )
    assert _plan(tmp_path, legacy_run, job_id, token, None).request.model is None
    with pytest.raises(ExecutorError, match="not allowed by project configuration"):
        _plan(tmp_path, legacy_run, job_id, token, "opus")


def test_run_book_resolution_fails_closed_when_run_and_manifest_disagree(tmp_path: Path) -> None:
    _, beta_run = _two_book_runs(tmp_path)
    run_yaml = tmp_path / "workspace/translation-runs" / beta_run / "run.yaml"
    raw = yaml.safe_load(run_yaml.read_text(encoding="utf-8"))
    snapshot_id = raw["snapshot_id"]
    relocated = ProjectPaths(tmp_path).translation_snapshots / snapshot_id
    shutil.copytree(tmp_path / raw["snapshot_path"], relocated)
    raw["snapshot_path"] = relocated.relative_to(tmp_path).as_posix()
    run_yaml.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(QueueError, match="book workspace"):
        resolve_run_book(beta_run, project_root=tmp_path)
    with pytest.raises(QueueError, match="book workspace"):
        load_run_summary(beta_run, project_root=tmp_path)


def test_execute_translation_job_cli_resolves_the_run_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_run, beta_run = _two_book_runs(tmp_path)
    _set_provider_models(
        ProjectPaths(tmp_path, "beta").book_config, {"fake": ["fake-v1"], "claude-code": ["opus"]}
    )
    monkeypatch.chdir(tmp_path)
    # The CLI reads the wall clock, so these claims must be leased against it too.
    beta_job, beta_token = _claimed(tmp_path, beta_run, now=None)
    legacy_job, legacy_token = _claimed(tmp_path, legacy_run, now=None)

    def invoke(run_id: str, job_id: str, token: str) -> Any:
        return runner.invoke(
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
                "--model",
                "opus",
                "--dry-run",
            ],
        )

    planned = invoke(beta_run, beta_job, beta_token)
    assert planned.exit_code == 0, planned.output
    assert "planned" in planned.output and "opus" in planned.output
    assert beta_token not in planned.output
    refused = invoke(legacy_run, legacy_job, legacy_token)
    assert refused.exit_code == 1
    assert "not allowed by project configuration" in refused.output
    assert not list((tmp_path / "workspace/translation-runs").glob("*/jobs/*/attempts"))


# --- worker: policy before claim, diagnostics after claim ---------------------------------------


def test_worker_refuses_a_disallowed_model_before_preflight_or_claim(tmp_path: Path) -> None:
    _, beta_run = _two_book_runs(tmp_path)
    _set_provider_models(
        ProjectPaths(tmp_path, "beta").book_config, {"fake": ["fake-v1"], "claude-code": ["opus"]}
    )
    before = _queue_bytes(tmp_path, beta_run)
    preflights: list[ProviderName] = []
    calls: list[str] = []
    for dry_run in (True, False):
        with pytest.raises(TranslationWorkerError, match="not allowed by project configuration"):
            run_translation_worker(
                beta_run,
                project_root=tmp_path,
                options=_options(
                    provider=ProviderName.CLAUDE_CODE, model="sonnet", dry_run=dry_run
                ),
                provider_factory=lambda _job: SyntheticProvider(PERSIAN, calls),
                preflight=preflights.append,
                now=lambda: EXECUTION_NOW,
            )
    assert not preflights and not calls
    assert _queue_bytes(tmp_path, beta_run) == before
    assert all(job.attempt_count == 0 for job in _jobs_by_id(tmp_path, beta_run).values())

    rehearsal = run_translation_worker(
        beta_run,
        project_root=tmp_path,
        options=_options(provider=ProviderName.CLAUDE_CODE, model="opus", dry_run=True),
        provider_factory=lambda _job: SyntheticProvider(PERSIAN, calls),
        preflight=preflights.append,
        now=lambda: EXECUTION_NOW,
    )
    assert rehearsal.dry_run and rehearsal.processed == 1
    assert rehearsal.jobs[0].outcome == "would_process"
    assert not preflights and not calls and _queue_bytes(tmp_path, beta_run) == before

    result = run_translation_worker(
        beta_run,
        project_root=tmp_path,
        options=_options(provider=ProviderName.CLAUDE_CODE, model="opus"),
        provider_factory=lambda _job: SyntheticProvider(PERSIAN, calls),
        preflight=preflights.append,
        now=lambda: EXECUTION_NOW,
    )
    assert result.processed == 1 and result.errors == 0
    assert preflights == [ProviderName.CLAUDE_CODE] and len(calls) == 1
    job = _jobs_by_id(tmp_path, beta_run)[calls[0]]
    assert job.status is TranslationJobStatus.SUCCEEDED
    request = yaml.safe_load(
        (
            tmp_path
            / "workspace/translation-runs"
            / beta_run
            / "jobs"
            / job.chunk_id
            / "attempts/001/request.json"
        ).read_text(encoding="utf-8")
    )
    assert request["provider"] == "claude-code" and request["model"] == "opus"


def test_legacy_worker_ignores_another_books_allowance(tmp_path: Path) -> None:
    legacy_run, _ = _two_book_runs(tmp_path)
    _set_provider_models(
        ProjectPaths(tmp_path, "beta").book_config, {"fake": ["fake-v1"], "claude-code": ["opus"]}
    )
    with pytest.raises(TranslationWorkerError, match="not allowed by project configuration"):
        run_translation_worker(
            legacy_run,
            project_root=tmp_path,
            options=_options(provider=ProviderName.CLAUDE_CODE, model="opus", dry_run=True),
            now=lambda: EXECUTION_NOW,
        )
    _set_provider_models(
        ProjectPaths(tmp_path).book_config, {"fake": ["fake-v1"], "claude-code": ["opus"]}
    )
    planned = run_translation_worker(
        legacy_run,
        project_root=tmp_path,
        options=_options(provider=ProviderName.CLAUDE_CODE, model="opus", dry_run=True),
        now=lambda: EXECUTION_NOW,
    )
    assert planned.processed == 1 and planned.jobs[0].outcome == "would_process"


def test_pre_provider_failure_after_claim_is_diagnosable_and_keeps_claim_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path)
    message = "provider/model is not allowed by project configuration: claude-code"
    calls: list[str] = []

    def refuse_planning(*args: object, **kwargs: object) -> None:
        raise ExecutorError(message)

    monkeypatch.setattr("book_translator.translation_worker.plan_execution", refuse_planning)
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider(PERSIAN, calls),
        now=lambda: NOW + timedelta(seconds=10),
    )

    assert result.errors == 1 and result.processed == 1 and not calls
    (item,) = result.jobs
    assert item.outcome == "error" and item.attempt_number == 1
    assert item.error == f"ExecutorError: {message}"
    job = _jobs_by_id(tmp_path, run_id)[item.job_id]
    # The claim itself is untouched: the slot stays consumed and the lease still runs, exactly as
    # before, so recovery semantics are unchanged. What is new is that the job now says why.
    assert job.status is TranslationJobStatus.CLAIMED and job.claimed_by == "worker-test"
    assert job.attempt_count == 1 and job.provider_attempt_count == 1
    assert job.last_error_category == "worker"
    assert job.last_error_message == f"ExecutorError: {message}"
    assert job.recent_events[-1].event_type == "worker_error"
    assert job.recent_events[-1].reason == f"ExecutorError: {message}"
    assert job.recent_events[-1].worker_id == "worker-test"
    assert not list((tmp_path / "workspace/translation-runs" / run_id).glob("jobs/*/attempts"))

    recovered = recover_run(
        run_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=1000)
    )
    assert recovered.expired_claims == (item.job_id,)
    released = _jobs_by_id(tmp_path, run_id)[item.job_id]
    assert released.status is TranslationJobStatus.PENDING and released.attempt_count == 1
    assert released.last_error_message == f"ExecutorError: {message}"


def test_worker_cli_prints_the_pre_provider_failure_instead_of_a_bare_error_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    message = "provider/model is not allowed by project configuration: claude-code"

    def refuse_planning(*args: object, **kwargs: object) -> None:
        raise ExecutorError(message)

    monkeypatch.setattr("book_translator.translation_worker.plan_execution", refuse_planning)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["run-translation-worker", run_id, "--provider", "fake", "--model", "fake-v1"],
    )
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert message in flat and "ExecutorError" in flat
    assert "worker-secret" not in result.output and "Alpha" not in result.output

    stopping = runner.invoke(
        app,
        [
            "run-translation-worker",
            run_id,
            "--provider",
            "fake",
            "--model",
            "fake-v1",
            "--job-id",
            f"{run_id}__002-001",
            "--stop-on-error",
        ],
    )
    assert stopping.exit_code == 1
    assert message in " ".join(stopping.output.split())
