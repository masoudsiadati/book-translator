"""What runs while ``.queue.lock`` is held, and what must not.

The queue lock serializes *queue mutations*. Everything a worker merely reads or computes —
live snapshot lineage validation, persisted attempt validation, terminology/structural/alignment
QA — must happen before the lock is taken, because on a real book a single live snapshot
validation re-derives the book's series terminology and takes tens of seconds, and a critical
section several of those long starves every other execution slot into ``QueueLockTimeout``.

Lock state is observed directly: a spy wrapped around an expensive call tries a non-blocking
``flock`` on a fresh descriptor of the run's lock file, which conflicts with a lock held by any
descriptor, in this process or another. Every run lives under ``tmp_path`` and every provider is
synthetic. Delays and lock timeouts are test-local and short, but keep the production *ratio*:
the artificial QA delay is several times the lock timeout.
"""

import fcntl
import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from book_translator import translation_worker
from book_translator.models import (
    ProviderName,
    TranslationAcceptanceStatus,
    TranslationAttemptStatus,
    TranslationJobStatus,
)
from book_translator.provider_execution import (
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
    execute_translation_job,
    plan_execution,
)
from book_translator.provider_execution import executor as provider_executor
from book_translator.terminology import validate_terminology
from book_translator.translation_queue import claim_job, load_run_summary
from book_translator.translation_queue import workflow as queue_workflow
from book_translator.translation_snapshot import validate_snapshot
from book_translator.translation_worker import (
    TranslationWorkerError,
    accept_provider_success,
    run_translation_worker,
)
from tests.test_translation_queue import NOW
from tests.test_translation_worker import SyntheticProvider, _options, _prepared_run, _run_path

ACCEPTABLE = "ترجمه نخست.\n\nترجمه دوم.\n"
#: Test-local lock timeout; production keeps 120 seconds.
TEST_LOCK_TIMEOUT_SECONDS = 0.5
#: Artificial acceptance QA delay, deliberately a multiple of the lock timeout.
TEST_QA_DELAY_SECONDS = 1.5
WAIT_LIMIT_SECONDS = 60.0


def _lock_is_free(run_path: Path) -> bool:
    with (run_path / ".queue.lock").open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return True


def _spy(
    observed: dict[str, list[bool]], name: str, run_path: Path, original: Callable[..., Any]
) -> Callable[..., Any]:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        observed.setdefault(name, []).append(_lock_is_free(run_path))
        return original(*args, **kwargs)

    return wrapper


def _short_lock_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (queue_workflow, translation_worker, provider_executor):
        if hasattr(module, "LOCK_TIMEOUT_SECONDS"):
            monkeypatch.setattr(module, "LOCK_TIMEOUT_SECONDS", TEST_LOCK_TIMEOUT_SECONDS)


def _draft(root: Path, *, chapters: int = 2, text: str = ACCEPTABLE) -> tuple[str, str]:
    """Leave one provider-successful, not yet assessed draft on the first job of a fresh run."""
    run_id = _prepared_run(root, chapters=chapters)
    claim = claim_job(
        run_id, project_root=root, worker_id="drafter", lease_seconds=900, now=lambda: NOW
    )
    plan = plan_execution(
        run_id,
        claim.job.job_id,
        project_root=root,
        worker_id="drafter",
        claim_token=claim.claim_token,
        provider=ProviderName.FAKE,
        model="fake-v1",
        timeout_seconds=120,
        max_output_tokens=1000,
        now=lambda: NOW,
    )
    execution = execute_translation_job(
        plan,
        worker_id="drafter",
        claim_token=claim.claim_token,
        provider=SyntheticProvider(text),
        now=lambda: NOW + timedelta(seconds=1),
    )
    assert execution.attempt_status is TranslationAttemptStatus.SUCCEEDED
    assert _job(root, run_id, claim.job.job_id).acceptance_status is None
    return run_id, claim.job.job_id


def _job(root: Path, run_id: str, job_id: str) -> Any:
    return next(
        job for job in load_run_summary(run_id, project_root=root).jobs if job.job_id == job_id
    )


# --- where the expensive work runs ------------------------------------------------------------


def test_acceptance_validation_and_qa_run_outside_the_queue_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, job_id = _draft(tmp_path)
    run_path = _run_path(tmp_path, run_id)
    observed: dict[str, list[bool]] = {}
    for name in (
        "validate_attempt",
        "load_registry",
        "validate_terminology",
        "validate_translation_qa",
        # The alignment judgement runs inside the shared acceptance battery.
        "assess_translation",
    ):
        monkeypatch.setattr(
            translation_worker,
            name,
            _spy(observed, name, run_path, getattr(translation_worker, name)),
        )
    monkeypatch.setattr(
        queue_workflow,
        "validate_snapshot",
        _spy(observed, "validate_snapshot", run_path, validate_snapshot),
    )

    status = accept_provider_success(
        run_id, job_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=2)
    )

    assert status is TranslationAcceptanceStatus.ACCEPTED
    assert {
        "validate_attempt",
        "validate_terminology",
        "validate_translation_qa",
        "assess_translation",
        "validate_snapshot",
    } <= set(observed)
    held = {name: calls for name, calls in observed.items() if not all(calls)}
    assert held == {}, f"ran while .queue.lock was held: {sorted(held)}"
    job = _job(tmp_path, run_id, job_id)
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.acceptance_file is not None and (tmp_path / job.acceptance_file).is_file()


def test_claim_validates_the_snapshot_once_and_outside_the_queue_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path, chapters=1)
    run_path = _run_path(tmp_path, run_id)
    observed: dict[str, list[bool]] = {}
    monkeypatch.setattr(
        queue_workflow,
        "validate_snapshot",
        _spy(observed, "validate_snapshot", run_path, validate_snapshot),
    )

    claim = claim_job(
        run_id, project_root=tmp_path, worker_id="slot", lease_seconds=900, now=lambda: NOW
    )

    assert claim.job.status is TranslationJobStatus.CLAIMED
    assert observed == {"validate_snapshot": [True]}


def test_execution_reservation_and_finalization_validate_the_snapshot_outside_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path, chapters=1)
    run_path = _run_path(tmp_path, run_id)
    claim = claim_job(
        run_id, project_root=tmp_path, worker_id="slot", lease_seconds=900, now=lambda: NOW
    )
    plan = plan_execution(
        run_id,
        claim.job.job_id,
        project_root=tmp_path,
        worker_id="slot",
        claim_token=claim.claim_token,
        provider=ProviderName.FAKE,
        model="fake-v1",
        timeout_seconds=120,
        max_output_tokens=1000,
        now=lambda: NOW,
    )
    observed: dict[str, list[bool]] = {}
    monkeypatch.setattr(
        queue_workflow,
        "validate_snapshot",
        _spy(observed, "validate_snapshot", run_path, validate_snapshot),
    )

    result = execute_translation_job(
        plan,
        worker_id="slot",
        claim_token=claim.claim_token,
        provider=SyntheticProvider(ACCEPTABLE),
        now=lambda: NOW + timedelta(seconds=1),
    )

    assert result.attempt_status is TranslationAttemptStatus.SUCCEEDED
    assert observed.get("validate_snapshot"), "reservation and finalization reload the queue"
    assert all(observed["validate_snapshot"]), observed


# --- the production failure, reproduced at test scale ------------------------------------------


def test_slow_acceptance_qa_does_not_block_another_workers_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: worker-2's claim timed out while worker-1 held the lock through acceptance."""
    run_id, job_id = _draft(tmp_path, chapters=2)
    _short_lock_timeouts(monkeypatch)
    started, finished = threading.Event(), threading.Event()
    original = validate_terminology

    def slow_terminology_qa(*args: Any, **kwargs: Any) -> Any:
        started.set()
        time.sleep(TEST_QA_DELAY_SECONDS)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(translation_worker, "validate_terminology", slow_terminology_qa)
    statuses: list[TranslationAcceptanceStatus] = []
    errors: list[BaseException] = []

    def accept() -> None:
        try:
            statuses.append(
                accept_provider_success(
                    run_id, job_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=2)
                )
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    acceptor = threading.Thread(target=accept)
    acceptor.start()
    try:
        assert started.wait(timeout=WAIT_LIMIT_SECONDS)
        began = time.monotonic()
        claim = claim_job(
            run_id,
            project_root=tmp_path,
            worker_id="worker-2",
            lease_seconds=900,
            now=lambda: NOW + timedelta(seconds=3),
        )
        waited = time.monotonic() - began
        assert not finished.is_set(), "the claim must complete while acceptance QA is running"
    finally:
        acceptor.join(timeout=WAIT_LIMIT_SECONDS)
    assert waited < TEST_LOCK_TIMEOUT_SECONDS
    assert claim.job.job_id != job_id
    assert errors == []
    assert statuses == [TranslationAcceptanceStatus.ACCEPTED]
    state = load_run_summary(run_id, project_root=tmp_path)
    assert state.validation.valid
    by_id = {job.job_id: job for job in state.jobs}
    assert by_id[job_id].acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert by_id[claim.job.job_id].status is TranslationJobStatus.CLAIMED
    assert by_id[claim.job.job_id].claimed_by == "worker-2"


# --- compare and revalidate --------------------------------------------------------------------


def test_acceptance_adopts_a_verdict_another_worker_published_during_qa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, job_id = _draft(tmp_path)
    _short_lock_timeouts(monkeypatch)
    original = validate_terminology
    racing = False

    def racing_terminology_qa(*args: Any, **kwargs: Any) -> Any:
        nonlocal racing
        if not racing:
            racing = True
            accept_provider_success(
                run_id, job_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=5)
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(translation_worker, "validate_terminology", racing_terminology_qa)

    status = accept_provider_success(
        run_id, job_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=2)
    )

    job = _job(tmp_path, run_id, job_id)
    assert status is TranslationAcceptanceStatus.ACCEPTED
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.updated_at == NOW + timedelta(seconds=5), "the published verdict is not rewritten"
    assert [event.event_type for event in job.recent_events].count("translation_accepted") == 1
    assert len(list(_run_path(tmp_path, run_id).glob("jobs/*/acceptance/*/evidence.yaml"))) == 1


def test_acceptance_publishes_nothing_when_the_translation_changed_during_qa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, job_id = _draft(tmp_path)
    run_path = _run_path(tmp_path, run_id)
    translation = run_path / "jobs" / job_id.split("__")[1] / "attempts/001/translation.md"
    original = validate_terminology

    def tampering_terminology_qa(*args: Any, **kwargs: Any) -> Any:
        translation.write_text("ترجمه دستکاری‌شده.\n\nبند دوم.\n", encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(translation_worker, "validate_terminology", tampering_terminology_qa)

    with pytest.raises(TranslationWorkerError, match="changed"):
        accept_provider_success(
            run_id, job_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=2)
        )

    job = _job(tmp_path, run_id, job_id)
    assert job.acceptance_status is None and job.acceptance_file is None
    assert list(run_path.glob("jobs/*/acceptance")) == []


def test_acceptance_publishes_nothing_when_the_job_left_provider_success_during_qa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, job_id = _draft(tmp_path)
    run_path = _run_path(tmp_path, run_id)
    job_path = run_path / "jobs" / job_id.split("__")[1] / "job.yaml"
    original = validate_terminology

    def intruding_terminology_qa(*args: Any, **kwargs: Any) -> Any:
        data = yaml.safe_load(job_path.read_text(encoding="utf-8"))
        data.update(
            status="claimed",
            claimed_by="intruder",
            claim_token="intruder-token",
            claimed_at=(NOW + timedelta(seconds=2)).isoformat(),
            lease_expires_at=(NOW + timedelta(seconds=900)).isoformat(),
        )
        job_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(translation_worker, "validate_terminology", intruding_terminology_qa)

    with pytest.raises(TranslationWorkerError, match="changed"):
        accept_provider_success(
            run_id, job_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=2)
        )

    job = _job(tmp_path, run_id, job_id)
    assert job.status is TranslationJobStatus.CLAIMED and job.claimed_by == "intruder"
    assert job.acceptance_status is None
    assert list(run_path.glob("jobs/*/acceptance")) == []


def test_an_already_assessed_job_is_answered_without_recomputing_qa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, job_id = _draft(tmp_path)
    accept_provider_success(
        run_id, job_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=2)
    )
    before = _job(tmp_path, run_id, job_id)

    def must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("QA recomputed for an assessed job")

    monkeypatch.setattr(translation_worker, "validate_terminology", must_not_run)

    status = accept_provider_success(
        run_id, job_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=9)
    )

    assert status is TranslationAcceptanceStatus.ACCEPTED
    assert _job(tmp_path, run_id, job_id) == before


# --- slow acceptance QA inside a real process pool ----------------------------------------------

#: The one job whose acceptance QA is slow in the pool scenario; every other job's QA is not.
SLOW_CHUNK = "001-001"
#: Long enough that every other slot must claim again inside it. The slots keep the production
#: lock timeout: with eight slots the synthetic queue's own contention can legitimately make one
#: waiter wait a couple of seconds, so a short timeout would test scheduling luck. What proves the
#: lock scope is deterministic instead — while one slot holds the lock through QA, no other slot
#: can complete a claim at all.
POOL_QA_DELAY_SECONDS = 3.0


def _event(events: Path, kind: str) -> None:
    events.mkdir(parents=True, exist_ok=True)
    name = f"{kind}--{os.getpid()}--{time.monotonic_ns()}--{uuid.uuid4().hex}"
    (events / name).write_text("", encoding="utf-8")


def _events(events: Path, kind: str) -> list[tuple[int, int]]:
    found = []
    for path in events.iterdir():
        name, pid, stamp, _ = path.name.split("--")
        if name == kind:
            found.append((int(pid), int(stamp)))
    return sorted(found, key=lambda item: item[1])


def _install_slow_acceptance(events: Path, qa_delay: float) -> None:
    """Make this execution slot's acceptance QA slow for one job.

    Runs only inside a spawned slot, once, when the slot unpickles its provider factory — before
    its first claim. It records when each claim completes and when the slow QA starts and ends.
    """
    if getattr(translation_worker, "_slow_acceptance_installed", False):
        return
    # Never undone: the patch lives exactly as long as the slot process it was installed in.
    patcher = pytest.MonkeyPatch()
    patcher.setattr(translation_worker, "_slow_acceptance_installed", True, raising=False)
    current: dict[str, str] = {}
    original_accept = accept_provider_success
    original_qa = validate_terminology
    original_claim = claim_job

    def accept(run: Any, job_id: str, **kwargs: Any) -> Any:
        current["job_id"] = job_id
        return original_accept(run, job_id, **kwargs)

    def terminology_qa(*args: Any, **kwargs: Any) -> Any:
        if current.get("job_id", "").endswith(f"__{SLOW_CHUNK}"):
            _event(events, "qa-start")
            time.sleep(qa_delay)
            _event(events, "qa-end")
        return original_qa(*args, **kwargs)

    def claim(*args: Any, **kwargs: Any) -> Any:
        result = original_claim(*args, **kwargs)
        _event(events, "claim")
        return result

    patcher.setattr(translation_worker, "accept_provider_success", accept)
    patcher.setattr(translation_worker, "validate_terminology", terminology_qa)
    patcher.setattr(translation_worker, "claim_job", claim)


class SlowAcceptanceProvider:
    """A synthetic provider whose unpickling, inside a spawned slot, installs slow acceptance QA."""

    provider_name = "fake"
    adapter_version = "test-1.0.0"

    def __init__(self, events: Path, *, qa_delay: float) -> None:
        self.events = events
        self.qa_delay = qa_delay
        self.origin_pid = os.getpid()

    def __setstate__(self, state: dict[str, Any]) -> None:
        # A spawned slot unpickles its arguments before multiprocessing marks it a child, so the
        # only reliable test for "this is a slot, not the test process" is the process ID.
        self.__dict__.update(state)
        if os.getpid() != self.origin_pid:
            _install_slow_acceptance(self.events, self.qa_delay)

    def __call__(self, _job: object) -> "SlowAcceptanceProvider":
        return self

    def execute(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(
            response_id="synthetic-response",
            model=request.model or "fake-v1",
            status="completed",
            text_outputs=(ACCEPTABLE,),
            usage=ProviderUsage(10, 10, 20),
            sanitized_response={"status": "completed"},
        )


@pytest.mark.parametrize("workers", [2, 4, 8])
def test_pool_claims_continue_while_one_slot_runs_slow_acceptance_qa(
    tmp_path: Path, workers: int
) -> None:
    jobs = 3 * workers
    run_id = _prepared_run(tmp_path, chapters=jobs)
    events = tmp_path / "events"

    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(max_jobs=jobs, workers=workers),
        provider_factory=SlowAcceptanceProvider(events, qa_delay=POOL_QA_DELAY_SECONDS),
    )

    assert [item.error for item in result.jobs if item.error] == []
    assert result.worker_failures == 0
    assert result.processed == jobs and result.accepted == jobs
    starts, ends = _events(events, "qa-start"), _events(events, "qa-end")
    assert len(starts) == len(ends) == 1, "exactly one job's acceptance QA was slow"
    (slow_pid, qa_started), (_, qa_ended) = starts[0], ends[0]
    during = [
        pid
        for pid, stamp in _events(events, "claim")
        if pid != slow_pid and qa_started < stamp < qa_ended
    ]
    assert during, "no other slot completed a claim while acceptance QA was running"
    state = load_run_summary(run_id, project_root=tmp_path)
    assert state.validation.valid
    assert all(job.attempt_count == 1 for job in state.jobs)
    assert all(job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED for job in state.jobs)
