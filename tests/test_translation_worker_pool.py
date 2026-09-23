"""Multi-process translation worker: one coordinator, N execution slots, one queue.

Every provider here is a picklable module-level class holding plain data, because execution slots
are *spawned* processes that reconstruct their provider from pickled arguments; a lambda would be
refused before any claim. Rendezvous and execution evidence go through marker files so a test can
prove overlap, exactly-once execution, and crash behavior without sharing memory. No test reaches a
real provider, and every run lives under ``tmp_path``.
"""

import json
import os
import pickle
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import (
    ProviderErrorCategory,
    ProviderName,
    TranslationAcceptanceStatus,
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationJobStatus,
)
from book_translator.paragraph_markers import marker_for
from book_translator.prompt_packaging import (
    MARKED_OUTPUT_CONTRACT_VERSION,
    execute_prompt_packages,
    plan_prompt_packages,
)
from book_translator.provider_execution import (
    ExecutorError,
    ProviderExecutionError,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
    plan_execution,
)
from book_translator.provider_execution.artifacts import load_yaml
from book_translator.provider_execution.errors import ErrorClassification
from book_translator.translation_queue import (
    QueueError,
    claim_job,
    create_run,
    load_run_summary,
    load_settled_queue,
    plan_run,
    recover_run,
)
from book_translator.translation_queue.locking import queue_lock
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import (
    TranslationWorkerError,
    WorkerOptions,
    WorkerResult,
    WorkItem,
    generated_worker_id,
    run_translation_worker,
)
from book_translator.translation_worker_pool import (
    WORKER_POOL_VERSION,
    WorkerProgress,
    build_work_plan,
    slot_deadline_seconds,
    slot_worker_id,
)
from tests.test_translation_queue import NOW, SNAPSHOT_NOW
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import _options, _prepared_run, _queue_bytes, _run_path

runner = CliRunner()

ROOT = Path(__file__).parents[1]
ACCEPTABLE = "ترجمه نخست.\n\nترجمه دوم.\n"
MARKED_MISSING_SECOND = f"{marker_for(1)}\nآلفا بتا.\n"
WAIT_LIMIT_SECONDS = 60.0


def _pool_options(**updates: object) -> WorkerOptions:
    values: dict[str, object] = {"max_jobs": 50, "workers": 4}
    values.update(updates)
    return _options(**values)


class MarkingProvider:
    """A provider that leaves one marker file per execution and can rendezvous, fail, or crash.

    ``wait_for`` makes every execution wait until that many executions have *started*, which is
    the deterministic proof that provider calls overlapped across processes. ``recovery_probe``
    makes it record what a foreign dry-run recovery would say while the calls are live.
    """

    provider_name = "fake"
    adapter_version = "test-1.0.0"

    def __init__(
        self,
        marker_dir: Path,
        *,
        text: str = ACCEPTABLE,
        delay_seconds: float = 0.0,
        wait_for: int = 0,
        fail_jobs: tuple[str, ...] = (),
        retryable: bool = False,
        crash_jobs: tuple[str, ...] = (),
        recovery_probe: Path | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.marker_dir = marker_dir
        self.text = text
        self.delay_seconds = delay_seconds
        self.wait_for = wait_for
        self.fail_jobs = fail_jobs
        self.retryable = retryable
        self.crash_jobs = crash_jobs
        self.recovery_probe = recovery_probe
        self.project_root = project_root

    def __call__(self, _job: object) -> "MarkingProvider":
        return self

    def execute(self, request: ProviderRequest) -> ProviderResult:
        self.marker_dir.mkdir(parents=True, exist_ok=True)
        marker = self.marker_dir / f"{request.job_id}--{os.getpid()}--{uuid.uuid4().hex}"
        marker.write_text(str(request.attempt_number), encoding="utf-8")
        if request.job_id in self.crash_jobs:
            os._exit(3)
        if self.wait_for:
            _wait_until(lambda: len(_executions(self.marker_dir)) >= self.wait_for)
        if self.recovery_probe is not None and self.project_root is not None:
            probe = recover_run(request.run_id, project_root=self.project_root, dry_run=True)
            _append_json_line(
                self.recovery_probe,
                {"job_id": request.job_id, "blockers": list(probe.blockers)},
            )
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if request.job_id in self.fail_jobs:
            raise ProviderExecutionError(
                ErrorClassification(
                    ProviderErrorCategory.RATE_LIMIT,
                    self.retryable,
                    False,
                    False,
                    "synthetic provider failure",
                )
            )
        return ProviderResult(
            response_id="synthetic-response",
            model=request.model or "fake-v1",
            status="completed",
            text_outputs=(self.text,),
            usage=ProviderUsage(10, 10, 20),
            sanitized_response={"status": "completed"},
        )


def _wait_until(condition: Callable[[], bool], *, limit: float = WAIT_LIMIT_SECONDS) -> None:
    deadline = time.monotonic() + limit
    while not condition():
        if time.monotonic() >= deadline:
            raise TimeoutError("synthetic rendezvous timed out")
        time.sleep(0.02)


def _append_json_line(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")


def _executions(marker_dir: Path) -> list[str]:
    """Return the job ID of every provider execution, one entry per execution."""
    if not marker_dir.is_dir():
        return []
    return sorted(path.name.split("--")[0] for path in marker_dir.iterdir())


def _executing_pids(marker_dir: Path) -> set[int]:
    return {int(path.name.split("--")[1]) for path in marker_dir.iterdir()}


def _job_ids(root: Path, run_id: str) -> list[str]:
    return [job.job_id for job in load_run_summary(run_id, project_root=root).jobs]


def _statuses(root: Path, run_id: str) -> dict[str, TranslationJobStatus]:
    return {job.job_id: job.status for job in load_run_summary(run_id, project_root=root).jobs}


def _translation_files(root: Path, run_id: str) -> list[Path]:
    return sorted(_run_path(root, run_id).glob("jobs/*/attempts/*/translation.md"))


def _reservations(root: Path, run_id: str) -> list[Path]:
    return sorted(_run_path(root, run_id).glob("jobs/*/attempts/.*.reservation.yaml"))


def _claim_owners(root: Path, run_id: str) -> dict[str, set[str]]:
    """Return every worker ID that ever claimed each job, from the job's own event log."""
    owners: dict[str, set[str]] = {}
    for job in load_run_summary(run_id, project_root=root).jobs:
        owners[job.job_id] = {
            event.worker_id
            for event in job.recent_events
            if event.event_type == "job_claimed" and event.worker_id is not None
        }
    return owners


def _assert_clean_queue(root: Path, run_id: str) -> None:
    state = load_run_summary(run_id, project_root=root)
    assert state.validation.valid
    assert not [job for job in state.jobs if job.status is TranslationJobStatus.CLAIMED]
    assert not [job for job in state.jobs if job.status is TranslationJobStatus.RUNNING]
    assert _reservations(root, run_id) == []
    recovery = recover_run(run_id, project_root=root, dry_run=True)
    assert recovery.blockers == ()
    assert recovery.expired_claims == ()


def _marked_pool_run(root: Path, *, chapters: int) -> str:
    root.mkdir(parents=True, exist_ok=True)
    synthetic_project(root, chapters=chapters)
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    created = create_run(
        plan_run(
            snapshot.snapshot_id,
            project_root=root,
            output_contract_version=MARKED_OUTPUT_CONTRACT_VERSION,
            now=lambda: NOW,
        )
    )
    execute_prompt_packages(
        plan_prompt_packages(created.run_id, project_root=root, now=lambda: NOW)
    )
    return created.run_id


# --- option parsing and mode selection -------------------------------------------------------


@pytest.mark.parametrize("workers", [0, -1])
def test_workers_option_rejects_non_positive_values_before_any_claim(
    tmp_path: Path, workers: int
) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    before = _queue_bytes(tmp_path, run_id)
    with pytest.raises(TranslationWorkerError, match="workers"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_pool_options(workers=workers),
            provider_factory=MarkingProvider(tmp_path / "markers"),
        )
    assert _queue_bytes(tmp_path, run_id) == before
    assert _executions(tmp_path / "markers") == []


def test_workers_option_defaults_to_one() -> None:
    assert _options().workers == 1


@pytest.mark.parametrize("value", ["0", "-1"])
def test_cli_rejects_non_positive_workers_before_queue_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    monkeypatch.chdir(tmp_path)
    before = _queue_bytes(tmp_path, run_id)
    result = runner.invoke(
        app,
        ["run-translation-worker", run_id, "--provider", "fake", "--workers", value],
    )
    assert result.exit_code == 1
    assert _queue_bytes(tmp_path, run_id) == before


def test_cli_documents_workers_option() -> None:
    result = runner.invoke(
        app,
        ["run-translation-worker", "--help"],
        env={"COLUMNS": "250", "TERM": "dumb"},
    )
    assert result.exit_code == 0
    assert "--workers" in result.stdout


def test_workers_one_never_starts_the_pool_and_matches_sequential_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path, chapters=3)

    def refuse(*args: object, **kwargs: object) -> WorkerResult:
        raise AssertionError("the process pool must not start for --workers 1")

    monkeypatch.setattr("book_translator.translation_worker_pool.run_worker_pool", refuse)
    markers = tmp_path / "markers"
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=1, max_jobs=3),
        provider_factory=MarkingProvider(markers),
    )
    assert result.workers == 1
    assert result.worker_failures == 0
    assert result.processed == 3 and result.accepted == 3
    assert _executions(markers) == sorted(_job_ids(tmp_path, run_id))
    assert _executing_pids(markers) == {os.getpid()}
    assert [item.job_id for item in result.jobs] == _job_ids(tmp_path, run_id)
    _assert_clean_queue(tmp_path, run_id)


def test_workers_above_one_refuses_provider_recovery_selection(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    before = _queue_bytes(tmp_path, run_id)
    with pytest.raises(TranslationWorkerError, match="recover"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_pool_options(workers=2, auto_recover_provider_failures=True),
            provider_factory=MarkingProvider(tmp_path / "markers"),
        )
    assert _queue_bytes(tmp_path, run_id) == before


def test_unpicklable_injections_are_refused_before_any_claim(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    before = _queue_bytes(tmp_path, run_id)
    with pytest.raises(TranslationWorkerError, match="pickl"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_pool_options(workers=2),
            provider_factory=lambda _job: MarkingProvider(tmp_path / "markers"),
        )
    assert _queue_bytes(tmp_path, run_id) == before
    assert pickle.dumps(MarkingProvider(tmp_path / "markers"))


def test_dry_run_never_starts_a_pool_or_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path, chapters=3)

    def refuse(*args: object, **kwargs: object) -> WorkerResult:
        raise AssertionError("a dry run must not start the process pool")

    monkeypatch.setattr("book_translator.translation_worker_pool.run_worker_pool", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    before = _queue_bytes(tmp_path, run_id)
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=4, dry_run=True),
        provider_factory=MarkingProvider(tmp_path / "markers"),
    )
    assert result.dry_run and result.processed == 3
    assert result.workers == 3
    assert _queue_bytes(tmp_path, run_id) == before
    assert _executions(tmp_path / "markers") == []


# --- pure planning and identity ---------------------------------------------------------------


def test_work_plan_orders_assessment_then_claims_then_repairs(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=3)
    jobs = _job_ids(tmp_path, run_id)
    # Job 1 becomes an unassessed provider-successful draft (its acceptance is stripped, as a
    # crash between evidence publication and the job update would leave it); job 2 becomes a
    # QA-rejected draft; job 3 stays pending.
    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(max_jobs=1),
        provider_factory=lambda _job: MarkingProvider(tmp_path / "m1"),
    )
    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(max_jobs=1),
        provider_factory=lambda _job: MarkingProvider(tmp_path / "m2", text="فقط یک بند.\n"),
    )
    state = load_run_summary(run_id, project_root=tmp_path)
    first = state.jobs[0]
    job_path = state.path / "jobs" / first.chunk_id / "job.yaml"
    data = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    data.update(
        acceptance_status=None,
        accepted_attempt=None,
        acceptance_file=None,
        acceptance_sha256=None,
        acceptance_origin=None,
    )
    job_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")
    state = load_run_summary(run_id, project_root=tmp_path)
    assert state.jobs[1].acceptance_status is TranslationAcceptanceStatus.QA_REJECTED

    plain = build_work_plan(state, _pool_options())
    assert plain == (WorkItem("assess", jobs[0]), WorkItem("claim", None))
    repairing = build_work_plan(state, _pool_options(auto_repair=True, include_qa_rejected=True))
    assert repairing == (
        WorkItem("assess", jobs[0]),
        WorkItem("claim", None),
        WorkItem("remediate", jobs[1]),
    )
    assert build_work_plan(state, _pool_options(max_jobs=1)) == (WorkItem("assess", jobs[0]),)


def test_work_plan_from_a_fresh_queue_is_one_claim_per_pending_job(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=4)
    state = load_run_summary(run_id, project_root=tmp_path)
    plan = build_work_plan(state, _pool_options(max_jobs=50))
    assert plan == tuple(WorkItem("claim", None) for _ in range(4))
    truncated = build_work_plan(state, _pool_options(max_jobs=2))
    assert len(truncated) == 2
    chapter = build_work_plan(state, _pool_options(chapter_id="002"))
    assert chapter == (WorkItem("claim", None),)


def test_slot_deadline_covers_every_provider_call_an_item_may_spend() -> None:
    plain = slot_deadline_seconds(_options(timeout_seconds=600))
    repairing = slot_deadline_seconds(
        _options(timeout_seconds=600, auto_repair=True, max_repair_attempts=2)
    )
    assert plain > 600
    assert repairing == 3 * plain


def test_settled_queue_read_waits_out_a_publication_in_progress(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    run_path = _run_path(tmp_path, run_id)
    claim_job(run_id, project_root=tmp_path, worker_id="writer", lease_seconds=900)
    job_path = next(run_path.glob("jobs/*/job.yaml"))
    run_yaml = run_path / "run.yaml"
    published_job = job_path.read_bytes()
    published_run = run_yaml.read_bytes()
    # Rewind to the state *before* the claim, then replay the two writes of one publication with
    # the lock held and a reader arriving between them.
    claimed = yaml.safe_load(published_job)
    rewound = dict(claimed, status="pending", attempt_count=0, claimed_by=None, claim_token=None)
    rewound.update(claimed_at=None, lease_expires_at=None)
    run_data = yaml.safe_load(published_run)
    run_data.update(pending_count=run_data["pending_count"] + 1, claimed_count=0)
    held = threading.Event()
    reader_started = threading.Event()
    observed: list[bool] = []

    def publisher() -> None:
        with queue_lock(run_path / ".queue.lock", timeout_seconds=10):
            job_path.write_bytes(published_job)
            held.set()
            reader_started.wait(timeout=WAIT_LIMIT_SECONDS)
            time.sleep(0.5)
            run_yaml.write_bytes(published_run)

    job_path.write_text(yaml.safe_dump(rewound, sort_keys=False), encoding="utf-8")
    run_yaml.write_text(yaml.safe_dump(run_data, sort_keys=False), encoding="utf-8")
    thread = threading.Thread(target=publisher)
    thread.start()
    held.wait(timeout=WAIT_LIMIT_SECONDS)
    assert not load_run_summary(run_id, project_root=tmp_path).validation.valid
    reader_started.set()
    observed.append(load_settled_queue(run_id, project_root=tmp_path).validation.valid)
    thread.join(timeout=WAIT_LIMIT_SECONDS)
    assert observed == [True]


def test_settled_queue_read_still_reports_a_persistent_mismatch(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    # Claim first: a claim publishes fresh aggregates, which would repair the corruption below.
    claim = claim_job(run_id, project_root=tmp_path, worker_id="w", lease_seconds=900)
    run_yaml = _run_path(tmp_path, run_id) / "run.yaml"
    data = yaml.safe_load(run_yaml.read_text(encoding="utf-8"))
    data["pending_count"] += 1
    run_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    state = load_settled_queue(run_id, project_root=tmp_path)
    assert not state.validation.valid and state.validation.counter_mismatch
    with pytest.raises(ExecutorError, match="invalid"):
        plan_execution(
            run_id,
            claim.job.job_id,
            project_root=tmp_path,
            worker_id="w",
            claim_token=claim.claim_token,
            provider=ProviderName.FAKE,
            model="fake-v1",
            timeout_seconds=60,
            max_output_tokens=100,
        )


def test_slot_worker_ids_are_distinct_and_derived_from_the_coordinator() -> None:
    base = generated_worker_id()
    ids = [slot_worker_id(base, slot) for slot in range(1, 9)]
    assert len(set(ids)) == 8
    assert all(item.startswith(base + "-") for item in ids)
    assert ids[0].endswith("-1") and ids[7].endswith("-8")
    assert WORKER_POOL_VERSION.count(".") == 2


# --- execution invariants ----------------------------------------------------------------------


def test_two_workers_execute_both_jobs_once_with_overlapping_provider_calls(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    markers = tmp_path / "markers"
    progress: list[WorkerProgress] = []
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=2),
        provider_factory=MarkingProvider(markers, wait_for=2),
        on_progress=progress.append,
    )
    jobs = _job_ids(tmp_path, run_id)
    assert result.workers == 2
    assert result.processed == 2 and result.accepted == 2 and result.errors == 0
    # Progress comes from returned results, never from re-reading the queue, and ends complete.
    assert [item.completed for item in progress] == [1, 2]
    assert progress[-1].planned == 2 and progress[-1].accepted == 2
    assert progress[-1].active == 0 and progress[-1].workers == 2 and not progress[-1].stopping
    assert progress[-1].latest is not None and progress[-1].latest.worker_slot in {1, 2}
    assert _executions(markers) == sorted(jobs)
    assert len(_executing_pids(markers)) == 2
    assert os.getpid() not in _executing_pids(markers)
    assert sorted(item.job_id for item in result.jobs) == sorted(jobs)
    assert {item.worker_slot for item in result.jobs} == {1, 2}
    owners = _claim_owners(tmp_path, run_id)
    assert all(len(owned) == 1 for owned in owners.values())
    assert len(set().union(*owners.values())) == 2
    _assert_clean_queue(tmp_path, run_id)


def test_four_workers_execute_many_jobs_exactly_once_each(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=12)
    markers = tmp_path / "markers"
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=4),
        provider_factory=MarkingProvider(markers, delay_seconds=0.05),
    )
    jobs = _job_ids(tmp_path, run_id)
    assert result.workers == 4
    assert result.processed == 12 and result.accepted == 12
    assert _executions(markers) == sorted(jobs), "every job executed exactly once"
    assert sorted(item.job_id for item in result.jobs) == sorted(jobs)
    # One canonical successful artifact per accepted job, and never a second attempt.
    assert len(_translation_files(tmp_path, run_id)) == result.accepted == len(set(jobs))
    state = load_run_summary(run_id, project_root=tmp_path)
    assert all(job.attempt_count == 1 and job.successful_attempt == 1 for job in state.jobs)
    assert all(job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED for job in state.jobs)
    owners = _claim_owners(tmp_path, run_id)
    assert all(len(owned) == 1 for owned in owners.values())
    distinct = set().union(*owners.values())
    assert len(distinct) == 4 and all(
        owner.endswith(("-1", "-2", "-3", "-4")) for owner in distinct
    )
    assert len(_executing_pids(markers)) == 4
    _assert_clean_queue(tmp_path, run_id)


def test_pool_result_aggregates_every_slot_result(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=6)
    jobs = _job_ids(tmp_path, run_id)
    markers = tmp_path / "markers"
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=3, retry_provider_failures=False),
        provider_factory=MarkingProvider(markers, fail_jobs=(jobs[1], jobs[4])),
    )
    assert result.processed == 6
    assert result.accepted == 4 and result.provider_failed == 2
    assert result.qa_rejected == result.retry_pending == result.permanent_failure == 0
    assert result.errors == 0 and result.worker_failures == 0
    assert result.repair_attempts == result.provider_recovery_attempts == 0
    assert len(result.jobs) == 6 and all(item.worker_slot in {1, 2, 3} for item in result.jobs)
    assert {item.job_id for item in result.jobs if item.outcome == "provider_failed"} == {
        jobs[1],
        jobs[4],
    }
    _assert_clean_queue(tmp_path, run_id)


def test_global_max_jobs_bounds_the_whole_pool_not_each_worker(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=12)
    markers = tmp_path / "markers"
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=4, max_jobs=5),
        provider_factory=MarkingProvider(markers, delay_seconds=0.05),
    )
    assert result.processed == 5 and result.accepted == 5
    assert len(_executions(markers)) == 5
    assert len(set(_executions(markers))) == 5
    statuses = _statuses(tmp_path, run_id)
    assert sum(status is TranslationJobStatus.PENDING for status in statuses.values()) == 7
    # Canonical order is preserved: the five lowest-priority jobs were the ones taken.
    jobs = _job_ids(tmp_path, run_id)
    assert _executions(markers) == sorted(jobs[:5])
    _assert_clean_queue(tmp_path, run_id)


def test_explicit_job_id_runs_exactly_once_with_effective_concurrency_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path, chapters=4)
    jobs = _job_ids(tmp_path, run_id)

    def refuse(*args: object, **kwargs: object) -> WorkerResult:
        raise AssertionError("an explicit job needs no process pool")

    monkeypatch.setattr("book_translator.translation_worker_pool.run_worker_pool", refuse)
    markers = tmp_path / "markers"
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=4, job_id=jobs[2]),
        provider_factory=MarkingProvider(markers),
    )
    assert result.workers == 1
    assert result.processed == 1 and result.accepted == 1
    assert _executions(markers) == [jobs[2]]
    assert _statuses(tmp_path, run_id)[jobs[0]] is TranslationJobStatus.PENDING
    _assert_clean_queue(tmp_path, run_id)


def test_effective_workers_never_exceed_the_selected_work(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    markers = tmp_path / "markers"
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=8),
        provider_factory=MarkingProvider(markers, wait_for=2),
    )
    assert result.workers == 2
    assert result.processed == 2 and len(_executing_pids(markers)) == 2


# --- atomic claims and ownership ---------------------------------------------------------------


def _claim_in_process(root: Path, run_id: str, worker_id: str, barrier: Any, out: Path) -> None:
    barrier.wait(timeout=WAIT_LIMIT_SECONDS)
    try:
        claim = claim_job(run_id, project_root=root, worker_id=worker_id, lease_seconds=60)
    except QueueError as exc:
        out.write_text(f"error:{exc}", encoding="utf-8")
        return
    out.write_text(f"claimed:{claim.job.job_id}", encoding="utf-8")


def test_atomic_claim_race_grants_exactly_one_owner_per_job(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=1)
    context = get_context("spawn")
    barrier = context.Barrier(2)
    outputs = [tmp_path / f"claim-{index}.txt" for index in range(2)]
    processes = [
        context.Process(
            target=_claim_in_process,
            args=(tmp_path, run_id, f"racer-{index}", barrier, outputs[index]),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=WAIT_LIMIT_SECONDS)
        assert process.exitcode == 0
    answers = sorted(path.read_text(encoding="utf-8") for path in outputs)
    assert sum(answer.startswith("claimed:") for answer in answers) == 1
    assert sum(answer.startswith("error:") for answer in answers) == 1
    assert "no eligible translation job" in answers[1]
    state = load_run_summary(run_id, project_root=tmp_path)
    assert state.validation.valid
    assert state.jobs[0].status is TranslationJobStatus.CLAIMED
    assert state.jobs[0].attempt_count == 1
    assert state.jobs[0].claimed_by in {"racer-0", "racer-1"}


def test_lease_ownership_refuses_a_foreign_slot_and_a_wrong_token(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=1)
    base = generated_worker_id()
    claim = claim_job(
        run_id, project_root=tmp_path, worker_id=slot_worker_id(base, 1), lease_seconds=900
    )

    def plan(worker: str, token: str) -> None:
        plan_execution(
            run_id,
            claim.job.job_id,
            project_root=tmp_path,
            worker_id=worker,
            claim_token=token,
            provider=ProviderName.FAKE,
            model="fake-v1",
            timeout_seconds=60,
            max_output_tokens=100,
        )

    with pytest.raises(ExecutorError, match="own"):
        plan(slot_worker_id(base, 2), claim.claim_token)
    with pytest.raises(ExecutorError, match="token"):
        plan(slot_worker_id(base, 1), "not-the-token")
    plan(slot_worker_id(base, 1), claim.claim_token)


# --- failures, stop, and isolation -------------------------------------------------------------


@pytest.mark.parametrize("retry", [False, True])
def test_provider_failure_is_isolated_to_its_own_job(tmp_path: Path, retry: bool) -> None:
    run_id = _prepared_run(tmp_path, chapters=4)
    jobs = _job_ids(tmp_path, run_id)
    markers = tmp_path / "markers"
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=2, retry_provider_failures=retry),
        provider_factory=MarkingProvider(markers, fail_jobs=(jobs[1],), retryable=True),
    )
    assert result.processed == 4 and result.accepted == 3
    assert _executions(markers) == sorted(jobs)
    state = {job.job_id: job for job in load_run_summary(run_id, project_root=tmp_path).jobs}
    failed = state[jobs[1]]
    if retry:
        assert result.retry_pending == 1 and result.provider_failed == 0
        assert failed.status is TranslationJobStatus.RETRY_PENDING
        assert failed.next_retry_at is not None
    else:
        assert result.provider_failed == 1 and result.retry_pending == 0
        assert failed.status is TranslationJobStatus.FAILED
    assert failed.attempt_count == 1
    for job_id in (jobs[0], jobs[2], jobs[3]):
        assert state[job_id].status is TranslationJobStatus.SUCCEEDED
        assert state[job_id].attempt_count == 1
    _assert_clean_queue(tmp_path, run_id)


def test_stop_on_error_stops_new_dispatch_and_lets_in_flight_work_finish(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=6)
    jobs = _job_ids(tmp_path, run_id)
    # The second job in canonical order has no prompt package, so its slot fails between the
    # claim and the reservation: an `error` outcome, exactly what --stop-on-error acts on.
    broken = load_run_summary(run_id, project_root=tmp_path).jobs[1]
    package = _run_path(tmp_path, run_id) / "jobs" / broken.chunk_id / "prompt-package"
    for path in sorted(package.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    markers = tmp_path / "markers"
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=2, stop_on_error=True),
        provider_factory=MarkingProvider(markers, delay_seconds=1.0),
    )
    assert result.errors == 1
    error = next(item for item in result.jobs if item.outcome == "error")
    assert error.job_id == jobs[1] and error.error is not None
    assert "prompt package" in error.error
    # The in-flight job finished and was accepted; nothing further was dispatched.
    assert result.processed == 2 and result.accepted == 1
    statuses = _statuses(tmp_path, run_id)
    assert sum(status is TranslationJobStatus.PENDING for status in statuses.values()) == 4
    assert statuses[jobs[1]] is TranslationJobStatus.CLAIMED
    assert _reservations(tmp_path, run_id) == []
    assert len(_executions(markers)) == 1
    recovery = recover_run(run_id, project_root=tmp_path, dry_run=True)
    assert recovery.blockers == ()


def test_stop_request_stops_new_dispatch_and_finishes_in_flight_jobs(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=6)
    markers = tmp_path / "markers"
    calls = {"count": 0}

    def stop_requested() -> bool:
        calls["count"] += 1
        return len(_executions(markers)) >= 2

    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=2),
        provider_factory=MarkingProvider(markers, delay_seconds=1.0),
        stop_requested=stop_requested,
    )
    assert calls["count"] > 0
    assert result.processed == 2 and result.accepted == 2
    assert len(_executions(markers)) == 2
    statuses = _statuses(tmp_path, run_id)
    assert sum(status is TranslationJobStatus.PENDING for status in statuses.values()) == 4
    _assert_clean_queue(tmp_path, run_id)


def _sigterm_main(root: str, run_id: str, markers: str, out: str) -> None:
    result = run_translation_worker(
        run_id,
        project_root=Path(root),
        options=_pool_options(workers=2),
        provider_factory=MarkingProvider(Path(markers), delay_seconds=2.0),
    )
    Path(out).write_text(
        json.dumps({"processed": result.processed, "accepted": result.accepted}),
        encoding="utf-8",
    )


def test_sigterm_to_the_coordinator_finishes_in_flight_jobs_then_stops(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=6)
    markers = tmp_path / "markers"
    out = tmp_path / "result.json"
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        " from tests.test_translation_worker_pool import _sigterm_main;"
        " _sigterm_main(*sys.argv[2:])"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(ROOT), str(tmp_path), run_id, str(markers), str(out)],
        cwd=tmp_path,
    )
    try:
        _wait_until(lambda: len(_executions(markers)) >= 2)
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=WAIT_LIMIT_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 0
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert summary == {"processed": 2, "accepted": 2}
    assert len(_executions(markers)) == 2
    statuses = _statuses(tmp_path, run_id)
    assert sum(status is TranslationJobStatus.PENDING for status in statuses.values()) == 4
    _assert_clean_queue(tmp_path, run_id)


def _process_gone(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    return any(line.startswith("State:") and "Z" in line for line in status.splitlines())


def test_slots_finish_their_job_and_exit_when_the_coordinator_is_killed(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=6)
    markers = tmp_path / "markers"
    out = tmp_path / "result.json"
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        " from tests.test_translation_worker_pool import _sigterm_main;"
        " _sigterm_main(*sys.argv[2:])"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(ROOT), str(tmp_path), run_id, str(markers), str(out)],
        cwd=tmp_path,
    )
    try:
        _wait_until(lambda: len(_executions(markers)) >= 2)
        process.kill()
        process.wait(timeout=WAIT_LIMIT_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
    slot_pids = _executing_pids(markers)
    assert len(slot_pids) == 2
    _wait_until(lambda: all(_process_gone(pid) for pid in slot_pids))
    assert not out.exists()
    # Both in-flight jobs were finalized by their own slots; nothing else was started.
    assert len(_executions(markers)) == 2
    statuses = _statuses(tmp_path, run_id)
    assert sum(status is TranslationJobStatus.SUCCEEDED for status in statuses.values()) == 2
    assert sum(status is TranslationJobStatus.PENDING for status in statuses.values()) == 4
    _assert_clean_queue(tmp_path, run_id)


def test_child_crash_is_reported_and_its_live_claim_is_never_reexecuted(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=4)
    jobs = _job_ids(tmp_path, run_id)
    markers = tmp_path / "markers"
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=2),
        provider_factory=MarkingProvider(markers, delay_seconds=1.0, crash_jobs=(jobs[0],)),
    )
    assert result.worker_failures == 1
    assert result.errors == 1
    crashed = next(item for item in result.jobs if item.outcome == "error")
    assert crashed.job_id == jobs[0]
    assert crashed.error is not None and "exit" in crashed.error
    # The crashed slot's job keeps its live claim and requesting reservation untouched; the
    # other in-flight job finished, and nothing new was dispatched after the crash.
    executions = _executions(markers)
    assert executions.count(jobs[0]) == 1
    assert len(executions) == 2 and len(set(executions)) == 2
    state = {job.job_id: job for job in load_run_summary(run_id, project_root=tmp_path).jobs}
    assert state[jobs[0]].status is TranslationJobStatus.RUNNING
    owner = state[jobs[0]].claimed_by
    assert owner is not None and owner.endswith(("-1", "-2"))
    assert len(_reservations(tmp_path, run_id)) == 1
    assert sum(job.status is TranslationJobStatus.PENDING for job in state.values()) == 2
    assert result.processed == 2 and result.accepted == 1
    recovery = recover_run(run_id, project_root=tmp_path, dry_run=True)
    assert len(recovery.blockers) == 1 and jobs[0] in recovery.blockers[0]


def test_crashed_slot_evidence_blocks_the_next_pool_start_fail_closed(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=3)
    jobs = _job_ids(tmp_path, run_id)
    markers = tmp_path / "markers"
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=2, max_jobs=2),
        provider_factory=MarkingProvider(markers, crash_jobs=(jobs[0],), delay_seconds=0.5),
    )
    assert result.worker_failures == 1
    before = _queue_bytes(tmp_path, run_id)
    with pytest.raises(TranslationWorkerError, match="recovery has blockers"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_pool_options(workers=2),
            provider_factory=MarkingProvider(tmp_path / "second"),
        )
    assert _queue_bytes(tmp_path, run_id) == before
    assert _executions(tmp_path / "second") == []


def test_live_concurrent_execution_is_a_blocker_for_foreign_recovery_only(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    markers = tmp_path / "markers"
    probe = tmp_path / "probe.jsonl"
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=2),
        provider_factory=MarkingProvider(
            markers, wait_for=2, recovery_probe=probe, project_root=tmp_path
        ),
    )
    jobs = _job_ids(tmp_path, run_id)
    probes = [json.loads(line) for line in probe.read_text(encoding="utf-8").splitlines()]
    assert {entry["job_id"] for entry in probes} == set(jobs)
    # While both calls were live, a *foreign* recovery pass could not tell them from a crash and
    # reported both as blockers — the fail-closed answer for any process outside the pool.
    for entry in probes:
        assert len(entry["blockers"]) == 2
        assert all("requires resolution" in blocker for blocker in entry["blockers"])
    # The pool itself, which ran recovery once before it started its slots, finished both.
    assert result.processed == 2 and result.accepted == 2
    _assert_clean_queue(tmp_path, run_id)


def test_rejected_output_is_preserved_privately_in_parallel(tmp_path: Path) -> None:
    run_id = _marked_pool_run(tmp_path, chapters=3)
    markers = tmp_path / "markers"
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_pool_options(workers=3, retry_provider_failures=False),
        provider_factory=MarkingProvider(markers, text=MARKED_MISSING_SECOND, wait_for=3),
    )
    assert result.processed == 3 and result.provider_failed == 3 and result.accepted == 0
    run_path = _run_path(tmp_path, run_id)
    rejected = sorted(run_path.glob("jobs/*/attempts/001/rejected-output.md"))
    assert len(rejected) == 3
    assert _translation_files(tmp_path, run_id) == []
    for path in rejected:
        assert path.read_bytes() == MARKED_MISSING_SECOND.encode("utf-8")
        assert path.stat().st_mode & 0o077 == 0
        attempt = load_yaml(path.parent / "attempt.yaml", TranslationAttempt)
        assert attempt.status is TranslationAttemptStatus.FAILED
        assert attempt.rejected_output_file is not None
        assert [finding.code for finding in attempt.marker_findings] == ["marker_missing"]
    state = load_run_summary(run_id, project_root=tmp_path)
    assert all(job.status is TranslationJobStatus.FAILED for job in state.jobs)
    assert all(job.acceptance_status is None for job in state.jobs)
    _assert_clean_queue(tmp_path, run_id)


# --- CLI rendering -----------------------------------------------------------------------------


def test_cli_pool_output_names_worker_slots_and_never_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path, chapters=3)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _size: "worker-secret-token")
    result = runner.invoke(
        app,
        [
            "run-translation-worker",
            run_id,
            "--provider",
            "fake",
            "--workers",
            "2",
            "--max-jobs",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "worker-secret-token" not in result.output
    assert "Claim token" not in result.output
    assert "[worker-1]" in result.output or "[worker-2]" in result.output
    assert "Workers" in result.output
    assert "Completed: 3 / 3" in result.output
    state = load_run_summary(run_id, project_root=tmp_path)
    assert all(job.status is TranslationJobStatus.SUCCEEDED for job in state.jobs)


# --- stress and isolation ----------------------------------------------------------------------


def _stress(root: Path, *, jobs: int, workers: int) -> None:
    run_id = _prepared_run(root, chapters=jobs)
    markers = root / "markers"
    result = run_translation_worker(
        run_id,
        project_root=root,
        options=_pool_options(workers=workers, max_jobs=jobs),
        provider_factory=MarkingProvider(markers, delay_seconds=0.01),
    )
    expected = _job_ids(root, run_id)
    executions = _executions(markers)
    assert result.processed == jobs and result.accepted == jobs
    assert result.errors == 0 and result.worker_failures == 0
    assert len(executions) == jobs, "one execution per job"
    assert executions == sorted(expected), "every job exactly once, no duplicates"
    assert len(_translation_files(root, run_id)) == jobs
    owners = _claim_owners(root, run_id)
    assert all(len(owned) == 1 for owned in owners.values()), "one owner per job"
    assert len(set().union(*owners.values())) == min(workers, jobs)
    state = load_run_summary(run_id, project_root=root)
    assert all(job.attempt_count == 1 for job in state.jobs)
    assert state.run.accepted_count == jobs
    _assert_clean_queue(root, run_id)


def test_stress_twenty_four_jobs_eight_workers(tmp_path: Path) -> None:
    _stress(tmp_path, jobs=24, workers=8)


@pytest.mark.skipif(
    os.environ.get("BOOK_TRANSLATOR_STRESS") != "1",
    reason="set BOOK_TRANSLATOR_STRESS=1 to run the 100-job stress test",
)
@pytest.mark.parametrize("round_number", [1, 2, 3])
def test_stress_one_hundred_jobs_eight_workers(tmp_path: Path, round_number: int) -> None:
    _stress(tmp_path / f"round-{round_number}", jobs=100, workers=8)


@pytest.mark.skipif(
    os.environ.get("BOOK_TRANSLATOR_STRESS") != "1",
    reason="set BOOK_TRANSLATOR_STRESS=1 to run the 100-job stress test",
)
@pytest.mark.parametrize("workers", [2, 4, 8])
def test_stress_one_hundred_jobs_across_pool_sizes(tmp_path: Path, workers: int) -> None:
    _stress(tmp_path, jobs=100, workers=workers)


def test_pool_tests_never_touch_the_production_workspace(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=1)
    run_path = _run_path(tmp_path, run_id).resolve()
    assert run_path.is_relative_to(tmp_path.resolve())
    assert not run_path.is_relative_to(ROOT)
