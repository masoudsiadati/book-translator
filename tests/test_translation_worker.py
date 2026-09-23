from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import (
    DecisionSource,
    ProviderErrorCategory,
    ProviderName,
    SnapshotResourceKind,
    TerminologyRegistry,
    TerminologyType,
    TranslationAcceptanceStatus,
    TranslationJob,
    TranslationJobStatus,
    TranslationRunStatus,
)
from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
from book_translator.provider_execution import (
    ProviderExecutionError,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from book_translator.provider_execution.errors import ErrorClassification
from book_translator.terminology import save_registry, set_decision
from book_translator.translation_queue import create_run, load_run_summary, plan_run
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import (
    TranslationWorkerError,
    WorkerOptions,
    run_translation_worker,
)
from tests.test_translation_queue import NOW, SNAPSHOT_NOW, _run, _set_job
from tests.test_translation_snapshot import synthetic_project

runner = CliRunner()


class SyntheticProvider:
    provider_name = "fake"
    adapter_version = "test-1.0.0"

    def __init__(self, text: str, calls: list[str] | None = None) -> None:
        self.text = text
        self.calls = calls

    def execute(self, request: ProviderRequest) -> ProviderResult:
        if self.calls is not None:
            self.calls.append(request.job_id)
        return ProviderResult(
            response_id="synthetic-response",
            model=request.model or "fake-v1",
            status="completed",
            text_outputs=(self.text,),
            usage=ProviderUsage(10, 10, 20),
            sanitized_response={"status": "completed"},
        )


class FailingProvider:
    provider_name = "fake"
    adapter_version = "test-1.0.0"

    def __init__(self, *, retryable: bool) -> None:
        self.retryable = retryable

    def execute(self, request: ProviderRequest) -> ProviderResult:
        raise ProviderExecutionError(
            ErrorClassification(
                ProviderErrorCategory.RATE_LIMIT,
                self.retryable,
                False,
                False,
                "synthetic provider failure",
            )
        )


def _options(**updates: object) -> WorkerOptions:
    values: dict[str, object] = {
        "provider": ProviderName.FAKE,
        "model": "fake-v1",
        "worker_id": "worker-test",
        "max_jobs": 1,
        "lease_seconds": 900,
        "timeout_seconds": 120,
        "max_output_tokens": 1000,
    }
    values.update(updates)
    return WorkerOptions(**values)  # type: ignore[arg-type]


def _prepared_run(root: Path, *, chapters: int = 1, max_attempts: int = 3) -> str:
    root.mkdir(parents=True, exist_ok=True)
    run_id, _ = _run(root, chapters=chapters, max_attempts=max_attempts)
    execute_prompt_packages(plan_prompt_packages(run_id, project_root=root, now=lambda: NOW))
    return run_id


def _run_path(root: Path, run_id: str) -> Path:
    return root / "workspace/translation-runs" / run_id


def _allow_claude_code_session_default(root: Path) -> None:
    """Configure claude-code with an empty allow-list: session default only, no explicit model."""
    config_path = root / "config/book.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "translation_provider_models:\n  fake: [fake-v1]\n  claude-code: []\n",
        encoding="utf-8",
    )


def _queue_bytes(root: Path, run_id: str) -> dict[str, bytes]:
    run_path = _run_path(root, run_id)
    return {
        str(path.relative_to(run_path)): path.read_bytes()
        for path in sorted(run_path.rglob("*"))
        if path.is_file()
    }


def _jobs_by_id(root: Path, run_id: str) -> dict[str, TranslationJob]:
    return {job.job_id: job for job in load_run_summary(run_id, project_root=root).jobs}


def _terminology_run(root: Path) -> str:
    synthetic_project(root)
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )
    registry = set_decision(
        registry,
        source_term="Alpha",
        target_term="آلفا",
        classification=TerminologyType.STORY_TERM,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    save_registry(root / "config/terminology.yaml", registry)
    (root / "config/terminology.metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "resource_kind": SnapshotResourceKind.TERMINOLOGY.value,
                "resource_file": "config/terminology.yaml",
                "version": "1.0.0",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    created = create_run(plan_run(snapshot.snapshot_id, project_root=root, now=lambda: NOW))
    execute_prompt_packages(
        plan_prompt_packages(created.run_id, project_root=root, now=lambda: NOW)
    )
    return created.run_id


def test_provider_success_and_qa_acceptance_is_persisted(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("ترجمه نخست.\n\nترجمه دوم.\n"),
        now=lambda: NOW + timedelta(seconds=10),
    )
    state = load_run_summary(run_id, project_root=tmp_path)
    job = state.jobs[0]
    assert result.accepted == 1 and result.qa_rejected == 0
    assert job.status is TranslationJobStatus.SUCCEEDED
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.acceptance_file is not None and (tmp_path / job.acceptance_file).is_file()
    assert state.run.accepted_count == 1
    assert state.run.status is TranslationRunStatus.COMPLETED


def test_terminology_error_rejects_provider_success(tmp_path: Path) -> None:
    run_id = _terminology_run(tmp_path)
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("ترجمه نخست.\n\nترجمه دوم.\n"),
        now=lambda: NOW + timedelta(seconds=10),
    )
    state = load_run_summary(run_id, project_root=tmp_path)
    assert result.qa_rejected == 1
    assert state.jobs[0].acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert state.run.qa_rejected_count == 1
    assert state.run.status is TranslationRunStatus.COMPLETED_WITH_FAILURES


def test_structural_error_rejects_but_warning_only_accepts(tmp_path: Path) -> None:
    rejected_run = _prepared_run(tmp_path / "rejected")
    rejected = run_translation_worker(
        rejected_run,
        project_root=tmp_path / "rejected",
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("فقط یک بند.\n"),
        now=lambda: NOW + timedelta(seconds=10),
    )
    assert rejected.qa_rejected == 1

    accepted_run = _prepared_run(tmp_path / "warning")
    accepted = run_translation_worker(
        accepted_run,
        project_root=tmp_path / "warning",
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("عنوان  \n\nبند دوم.\n"),
        now=lambda: NOW + timedelta(seconds=10),
    )
    assert accepted.accepted == 1


@pytest.mark.parametrize("retryable", [False, True])
def test_provider_failure_and_retry_backoff(tmp_path: Path, retryable: bool) -> None:
    run_id = _prepared_run(tmp_path)
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: FailingProvider(retryable=retryable),
        now=lambda: NOW + timedelta(seconds=10),
    )
    job = load_run_summary(run_id, project_root=tmp_path).jobs[0]
    if retryable:
        assert result.retry_pending == 1
        assert job.status is TranslationJobStatus.RETRY_PENDING
        assert job.next_retry_at == NOW + timedelta(seconds=70)
    else:
        assert result.provider_failed == 1
        assert job.status is TranslationJobStatus.FAILED


def test_max_jobs_dry_run_and_already_accepted_idempotency(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=3)
    calls: list[str] = []
    run_path = tmp_path / f"workspace/translation-runs/{run_id}"
    before = tuple(path.read_bytes() for path in sorted(run_path.glob("jobs/*/job.yaml")))
    dry = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(max_jobs=2, dry_run=True),
        provider_factory=lambda _job: SyntheticProvider("unused", calls),
    )
    assert dry.processed == 2 and not calls
    assert before == tuple(path.read_bytes() for path in sorted(run_path.glob("jobs/*/job.yaml")))

    actual = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(max_jobs=2),
        provider_factory=lambda _job: SyntheticProvider("ترجمه نخست.\n\nترجمه دوم.\n", calls),
        now=lambda: NOW + timedelta(seconds=10),
    )
    assert actual.processed == 2 and len(calls) == 2
    rerun = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(max_jobs=2),
        provider_factory=lambda _job: SyntheticProvider("ترجمه نخست.\n\nترجمه دوم.\n", calls),
        now=lambda: NOW + timedelta(seconds=20),
    )
    assert rerun.processed == 1 and len(calls) == 3
    final = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(max_jobs=2),
        provider_factory=lambda _job: SyntheticProvider("unused", calls),
        now=lambda: NOW + timedelta(seconds=30),
    )
    assert final.processed == 0 and len(calls) == 3


def test_worker_cli_does_not_leak_claim_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _size: "worker-secret-token")
    result = runner.invoke(
        app,
        ["run-translation-worker", run_id, "--provider", "fake", "--max-jobs", "1"],
    )
    assert result.exit_code == 0
    assert "worker-secret-token" not in result.stdout
    assert "Claim token" not in result.stdout


def test_worker_restart_reconciles_published_acceptance_without_provider_replay(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path)
    calls: list[str] = []
    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("ترجمه نخست.\n\nترجمه دوم.\n", calls),
        now=lambda: NOW + timedelta(seconds=10),
    )
    state = load_run_summary(run_id, project_root=tmp_path)
    job_path = state.path / "jobs" / state.jobs[0].chunk_id / "job.yaml"
    job_data = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    job_data.update(
        acceptance_status=None,
        accepted_attempt=None,
        acceptance_file=None,
        acceptance_sha256=None,
    )
    job_path.write_text(
        yaml.safe_dump(job_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    run_path = state.path / "run.yaml"
    run_data = yaml.safe_load(run_path.read_text(encoding="utf-8"))
    run_data.update(
        accepted_count=0,
        qa_rejected_count=0,
        provider_succeeded_unassessed_count=1,
        status="active",
        completed_at=None,
    )
    run_path.write_text(
        yaml.safe_dump(run_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    restarted = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("must not execute", calls),
        now=lambda: NOW + timedelta(seconds=20),
    )
    assert restarted.accepted == 1
    assert len(calls) == 1
    assert (
        load_run_summary(run_id, project_root=tmp_path).jobs[0].acceptance_status
        is TranslationAcceptanceStatus.ACCEPTED
    )


def test_targeted_job_processes_only_that_job_and_ignores_higher_priority_work(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path, chapters=3)
    target = f"{run_id}__002-001"
    calls: list[str] = []
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(max_jobs=3, job_id=target),
        provider_factory=lambda _job: SyntheticProvider("ترجمه نخست.\n\nترجمه دوم.\n", calls),
        now=lambda: NOW + timedelta(seconds=10),
    )
    jobs = _jobs_by_id(tmp_path, run_id)

    assert calls == [target]
    assert result.processed == 1 and result.accepted == 1
    assert tuple(item.job_id for item in result.jobs) == (target,)
    assert jobs[target].status is TranslationJobStatus.SUCCEEDED
    assert jobs[target].acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    for untouched in (f"{run_id}__001-001", f"{run_id}__003-001"):
        assert jobs[untouched].status is TranslationJobStatus.PENDING
        assert jobs[untouched].attempt_count == 0
        assert jobs[untouched].acceptance_status is None


def test_targeted_job_reassesses_an_unassessed_draft_without_a_provider_call(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    target = f"{run_id}__001-001"
    calls: list[str] = []
    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(job_id=target),
        provider_factory=lambda _job: SyntheticProvider("ترجمه نخست.\n\nترجمه دوم.\n", calls),
        now=lambda: NOW + timedelta(seconds=10),
    )
    job_path = _run_path(tmp_path, run_id) / "jobs/001-001/job.yaml"
    job_data = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    job_data.update(
        acceptance_status=None,
        accepted_attempt=None,
        acceptance_file=None,
        acceptance_sha256=None,
    )
    job_path.write_text(
        yaml.safe_dump(job_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    restarted = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(max_jobs=3, job_id=target),
        provider_factory=lambda _job: SyntheticProvider("must not execute", calls),
        now=lambda: NOW + timedelta(seconds=20),
    )
    jobs = _jobs_by_id(tmp_path, run_id)

    assert restarted.processed == 1 and restarted.accepted == 1
    assert len(calls) == 1
    assert jobs[target].acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert jobs[f"{run_id}__002-001"].status is TranslationJobStatus.PENDING


def test_targeted_job_that_is_already_complete_never_falls_back(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    target = f"{run_id}__001-001"
    calls: list[str] = []
    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(job_id=target),
        provider_factory=lambda _job: SyntheticProvider("ترجمه نخست.\n\nترجمه دوم.\n", calls),
        now=lambda: NOW + timedelta(seconds=10),
    )
    before = _queue_bytes(tmp_path, run_id)
    with pytest.raises(TranslationWorkerError, match="not processable"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_options(max_jobs=2, job_id=target),
            provider_factory=lambda _job: SyntheticProvider("must not execute", calls),
            now=lambda: NOW + timedelta(seconds=20),
        )

    assert calls == [target]
    assert _queue_bytes(tmp_path, run_id) == before


def test_targeted_blocked_job_reports_its_reason_without_claiming_another(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    run_path = _run_path(tmp_path, run_id)
    state = load_run_summary(run_id, project_root=tmp_path)
    target = state.jobs[0]
    _set_job(run_path, target, status=TranslationJobStatus.BLOCKED, attempt_count=3)
    calls: list[str] = []
    with pytest.raises(TranslationWorkerError, match="job status is blocked"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_options(max_jobs=2, job_id=target.job_id),
            provider_factory=lambda _job: SyntheticProvider("must not execute", calls),
            now=lambda: NOW + timedelta(seconds=10),
        )
    jobs = _jobs_by_id(tmp_path, run_id)

    assert not calls
    assert jobs[target.job_id].status is TranslationJobStatus.BLOCKED
    assert jobs[state.jobs[1].job_id].status is TranslationJobStatus.PENDING
    assert jobs[state.jobs[1].job_id].attempt_count == 0


def test_targeted_job_outside_the_run_is_refused_without_mutation(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    before = _queue_bytes(tmp_path, run_id)
    calls: list[str] = []
    for dry_run in (False, True):
        with pytest.raises(TranslationWorkerError, match="not part of this run"):
            run_translation_worker(
                run_id,
                project_root=tmp_path,
                options=_options(job_id=f"{run_id}__404-001", dry_run=dry_run),
                provider_factory=lambda _job: SyntheticProvider("must not execute", calls),
                now=lambda: NOW + timedelta(seconds=10),
            )

    assert not calls
    assert _queue_bytes(tmp_path, run_id) == before


def test_targeted_dry_run_selects_the_job_without_touching_queue_state(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=3)
    target = f"{run_id}__003-001"
    before = _queue_bytes(tmp_path, run_id)
    calls: list[str] = []
    planned = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(max_jobs=3, job_id=target, dry_run=True),
        provider_factory=lambda _job: SyntheticProvider("must not execute", calls),
    )

    assert planned.dry_run and planned.processed == 1
    assert tuple(item.job_id for item in planned.jobs) == (target,)
    assert tuple(item.outcome for item in planned.jobs) == ("would_process",)
    assert not calls
    assert _queue_bytes(tmp_path, run_id) == before
    assert not list(_run_path(tmp_path, run_id).glob("jobs/*/attempts/*"))


def test_targeted_dry_run_refuses_an_ineligible_job(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    run_path = _run_path(tmp_path, run_id)
    state = load_run_summary(run_id, project_root=tmp_path)
    _set_job(run_path, state.jobs[0], status=TranslationJobStatus.PENDING, attempt_count=3)
    before = _queue_bytes(tmp_path, run_id)
    with pytest.raises(TranslationWorkerError, match="attempt budget is exhausted"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_options(max_jobs=2, job_id=state.jobs[0].job_id, dry_run=True),
        )

    assert _queue_bytes(tmp_path, run_id) == before


def test_chapter_filter_restricts_selection_to_that_chapter(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=3)
    calls: list[str] = []
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(max_jobs=3, chapter_id="002"),
        provider_factory=lambda _job: SyntheticProvider("ترجمه نخست.\n\nترجمه دوم.\n", calls),
        now=lambda: NOW + timedelta(seconds=10),
    )
    jobs = _jobs_by_id(tmp_path, run_id)

    assert calls == [f"{run_id}__002-001"]
    assert result.processed == 1 and result.accepted == 1
    assert jobs[f"{run_id}__001-001"].status is TranslationJobStatus.PENDING
    assert jobs[f"{run_id}__003-001"].status is TranslationJobStatus.PENDING


def test_chapter_filter_dry_run_keeps_queue_priority_order(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=3)
    before = _queue_bytes(tmp_path, run_id)
    planned = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(max_jobs=3, chapter_id="003", dry_run=True),
    )
    unfiltered = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(max_jobs=3, dry_run=True),
    )

    assert tuple(item.job_id for item in planned.jobs) == (f"{run_id}__003-001",)
    assert tuple(item.job_id for item in unfiltered.jobs) == (
        f"{run_id}__001-001",
        f"{run_id}__002-001",
        f"{run_id}__003-001",
    )
    assert _queue_bytes(tmp_path, run_id) == before


def test_unknown_chapter_is_refused(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    with pytest.raises(TranslationWorkerError, match="targeted chapter has no jobs"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_options(chapter_id="004"),
        )


def test_job_and_chapter_filters_are_mutually_exclusive(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    with pytest.raises(TranslationWorkerError, match="do not combine it with a chapter"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_options(job_id=f"{run_id}__001-001", chapter_id="001"),
        )


def test_targeted_cli_dry_run_reports_one_job_without_leaking_a_claim_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _size: "worker-secret-token")
    before = _queue_bytes(tmp_path, run_id)
    result = runner.invoke(
        app,
        [
            "run-translation-worker",
            run_id,
            "--provider",
            "fake",
            "--job-id",
            f"{run_id}__002-001",
            "--max-jobs",
            "1",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "Processed" in result.stdout
    assert "would_process" in result.stdout
    assert f"{run_id}__002-001" in result.stdout.replace("\n", "")
    assert "worker-secret-token" not in result.stdout
    assert "Claim token" not in result.stdout
    assert _queue_bytes(tmp_path, run_id) == before


def test_targeted_cli_reports_an_unclaimable_job_and_rejects_filter_combinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path)
    monkeypatch.chdir(tmp_path)
    missing = runner.invoke(
        app,
        ["run-translation-worker", run_id, "--provider", "fake", "--job-id", "no-such-job"],
    )
    combined = runner.invoke(
        app,
        [
            "run-translation-worker",
            run_id,
            "--provider",
            "fake",
            "--job-id",
            f"{run_id}__001-001",
            "--chapter",
            "001",
        ],
    )

    assert missing.exit_code == 1
    assert "not part of this run" in missing.output
    assert combined.exit_code == 1
    assert "already identifies exactly one job" in combined.output


def test_chapter_filter_cli_normalizes_the_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "run-translation-worker",
            run_id,
            "--provider",
            "fake",
            "--chapter",
            "chapter-2",
            "--max-jobs",
            "5",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "Processed" in result.stdout
    assert "1" in result.stdout


def test_qa_rejected_jobs_are_listable_without_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path)
    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("فقط یک بند.\n"),
        now=lambda: NOW + timedelta(seconds=10),
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "list-translation-jobs",
            run_id,
            "--acceptance-status",
            "qa_rejected",
        ],
    )
    assert result.exit_code == 0
    assert "Translation Jobs" in result.stdout
    assert "qa_re" in result.stdout
    assert "فقط یک بند" not in result.stdout


def test_provider_preflight_runs_before_any_claim_or_queue_mutation(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    observed: list[tuple[str, tuple[TranslationJobStatus, ...]]] = []

    def record(provider: ProviderName) -> None:
        state = load_run_summary(run_id, project_root=tmp_path)
        observed.append((provider.value, tuple(job.status for job in state.jobs)))

    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("ترجمه نخست.\n\nترجمه دوم.\n"),
        preflight=record,
        now=lambda: NOW + timedelta(seconds=10),
    )

    assert result.processed == 1
    assert observed == [("fake", (TranslationJobStatus.PENDING, TranslationJobStatus.PENDING))]


def test_provider_preflight_failure_consumes_no_attempt_and_mutates_no_queue_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    _allow_claude_code_session_default(tmp_path)
    before = _queue_bytes(tmp_path, run_id)
    calls: list[str] = []

    def missing_executable(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("claude")

    monkeypatch.setattr("subprocess.run", missing_executable)
    with pytest.raises(TranslationWorkerError, match="provider preflight failed for claude-code"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_options(provider=ProviderName.CLAUDE_CODE, model=None),
            provider_factory=lambda _job: SyntheticProvider("must not execute", calls),
        )

    jobs = _jobs_by_id(tmp_path, run_id)
    assert _queue_bytes(tmp_path, run_id) == before
    assert not calls
    assert all(job.attempt_count == 0 for job in jobs.values())
    assert all(job.status is TranslationJobStatus.PENDING for job in jobs.values())
    assert not list(_run_path(tmp_path, run_id).glob("jobs/*/attempts/*"))


def test_targeted_dry_run_never_preflights_or_contacts_the_provider(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    _allow_claude_code_session_default(tmp_path)
    before = _queue_bytes(tmp_path, run_id)
    preflights: list[ProviderName] = []
    calls: list[str] = []
    planned = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(
            provider=ProviderName.CLAUDE_CODE,
            model=None,
            job_id=f"{run_id}__002-001",
            dry_run=True,
        ),
        provider_factory=lambda _job: SyntheticProvider("must not execute", calls),
        preflight=preflights.append,
    )

    assert planned.dry_run and planned.processed == 1
    assert not preflights and not calls
    assert _queue_bytes(tmp_path, run_id) == before
