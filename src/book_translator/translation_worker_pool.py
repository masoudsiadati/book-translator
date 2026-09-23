"""Coordinated multi-process execution of one translation worker pass.

One coordinator — the process that ran validation, preflight, and queue recovery — starts N
*execution slots*, each a spawned process with its own worker identity, and feeds each slot one
:class:`WorkItem` at a time. A slot claims through the queue's own lock, executes exactly that
job, finalizes the attempt, applies acceptance QA, and returns a compact result; it never renders
output, never touches another slot's claim, and never runs queue recovery.

Why processes and why spawn: a provider call and the queue work around it are CPU-bound in this
interpreter for a meaningful share of their time, so threads would serialize on the GIL; and a
spawned child starts from a clean interpreter with only the explicit, pickled arguments it was
given, so nothing reaches a slot by accident of fork-time global state.

What the coordinator owns: the global ``max_jobs`` budget (the plan is at most that long and
every item is dispatched at most once), the stop decision (``--stop-on-error``, a stop request,
a slot failure), and the shutdown (sentinels, a bounded join, a kill only as a last resort).
"""

import multiprocessing
import os
import signal
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from multiprocessing.process import BaseProcess
from pathlib import Path
from queue import Empty
from typing import Any

from book_translator.models import TranslationJob, TranslationJobStatus
from book_translator.provider_execution import TranslationProvider
from book_translator.provider_execution.executor import LEASE_SAFETY_MARGIN_SECONDS
from book_translator.translation_queue import QueueError, load_run_summary
from book_translator.translation_queue.workflow import QueueState
from book_translator.translation_worker import (
    TranslationWorkerError,
    WorkerJobResult,
    WorkerOptions,
    WorkerProgress,
    WorkerResult,
    WorkItem,
    _diagnostic,
    _RecoverySession,
    aggregate_results,
    build_work_plan,
    process_work_item,
)

WORKER_POOL_VERSION = "1.0.0"
#: How often the coordinator looks up from the result queue to check slots and stop requests.
POLL_SECONDS = 0.2
#: How long an idle slot may take to leave after its sentinel before it is killed.
SHUTDOWN_GRACE_SECONDS = 30.0
#: Allowance per provider call beyond its timeout and lease margin for everything around it:
#: planning, reservation, queue-lock waits behind other slots, validation, QA, and publication.
#: It is deliberately generous, because the deadline exists only to stop a coordinator waiting
#: forever on a slot that is truly hung, and killing a healthy slot mid-call is the worse error.
FINALIZATION_SLACK_SECONDS = 600.0
#: How often an idle slot checks that its coordinator is still alive.
SLOT_IDLE_POLL_SECONDS = 1.0
#: The job column of a slot failure that struck before any job was claimed.
UNCLAIMED = "(unclaimed)"

__all__ = [
    "WORKER_POOL_VERSION",
    "WorkerProgress",
    "build_work_plan",
    "run_worker_pool",
    "slot_deadline_seconds",
    "slot_worker_id",
]


def slot_worker_id(coordinator_worker_id: str, slot: int) -> str:
    """Derive one slot's distinct worker identity from the coordinator's."""
    return f"{coordinator_worker_id}-{slot}"


def slot_deadline_seconds(options: WorkerOptions) -> float:
    """Return how long one dispatched item may run before its slot is presumed hung.

    One item can spend more than one provider call: automatic repair re-enters the provider up to
    ``max_repair_attempts`` times for the same job. Each call is bounded by the provider timeout
    and the lease margin, and each gets the same generous slack for the work around it.
    """
    calls = 1 + (options.max_repair_attempts if options.auto_repair else 0)
    per_call = options.timeout_seconds + LEASE_SAFETY_MARGIN_SECONDS + FINALIZATION_SLACK_SECONDS
    return float(calls * per_call)


def _slot_main(
    slot: int,
    coordinator_pid: int,
    run_path: Path,
    project_root: Path,
    options: WorkerOptions,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
    now: Callable[[], datetime] | None,
    tasks: Any,
    results: Any,
) -> None:
    """Process work items until the sentinel, reporting each outcome to the coordinator.

    The coordinator alone decides when a slot stops, and it does so by sentinel once the slot's
    current job has been finalized, never by interrupting a provider call or a queue mutation
    half-way. Two things keep a terminal interrupt from deciding otherwise. The slot leaves the
    terminal's process group by starting its own session, so Ctrl+C is delivered to the
    coordinator only — and a provider CLI this slot launches stays in the slot's session, so its
    answer is not cut short either. And the slot ignores SIGINT and SIGTERM, for a signal sent to
    it explicitly.

    An idle slot also checks that its coordinator still exists. A coordinator that was killed
    outright cannot send the sentinel, and a slot must not wait for one forever.
    """
    os.setsid()
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    session = _RecoverySession(options.provider, None)
    while True:
        try:
            item = tasks.get(timeout=SLOT_IDLE_POLL_SECONDS)
        except Empty:
            if os.getppid() != coordinator_pid:
                return
            continue
        if item is None:
            return
        try:
            result = process_work_item(
                run_path,
                item,
                options,
                session,
                project_root=project_root,
                provider_factory=provider_factory,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - reported to the coordinator, which fails closed
            results.put(("failure", slot, item, _diagnostic(exc)))
            return
        results.put(("result", slot, item, result))


@dataclass(slots=True)
class _Slot:
    number: int
    worker_id: str
    process: BaseProcess
    tasks: Any
    item: WorkItem | None = None
    dispatched_at: float | None = None
    live: bool = True


class _StopSignals:
    """Turn the first SIGINT/SIGTERM into a stop request; leave a second SIGINT to interrupt."""

    def __init__(self) -> None:
        self.requested = False
        self._previous: dict[int, Any] = {}

    def install(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous[signum] = signal.signal(signum, self._handle)

    def _handle(self, signum: int, _frame: object) -> None:
        self.requested = True
        if signum == signal.SIGINT:
            signal.signal(signal.SIGINT, signal.default_int_handler)

    def restore(self) -> None:
        for signum, previous in self._previous.items():
            signal.signal(signum, previous)
        self._previous.clear()


def _kill_slot(slot: _Slot) -> None:
    """Kill a slot and everything it launched.

    A slot leads its own session and process group, so the provider CLI it started is killed with
    it rather than orphaned. This is the last resort only — a deadline passed or a hard interrupt
    — and a job killed mid-call keeps its ``requesting`` reservation for explicit resolution.
    """
    pid = slot.process.pid
    if pid is not None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            slot.process.kill()
    slot.process.join(SHUTDOWN_GRACE_SECONDS)


def _owned_job_id(state_path: Path, project_root: Path, worker_id: str) -> str:
    """Name the job a dead slot still owns, from the queue, or ``(unclaimed)``."""
    state = load_run_summary(state_path, project_root=project_root)
    for job in state.jobs:
        if job.claimed_by == worker_id and job.status in {
            TranslationJobStatus.CLAIMED,
            TranslationJobStatus.RUNNING,
        }:
            return job.job_id
    return UNCLAIMED


class _Coordinator:
    def __init__(
        self,
        state: QueueState,
        plan: tuple[WorkItem, ...],
        options: WorkerOptions,
        *,
        workers: int,
        project_root: Path,
        provider_factory: Callable[[TranslationJob], TranslationProvider] | None,
        now: Callable[[], datetime] | None,
        stop_requested: Callable[[], bool] | None,
        on_progress: Callable[[WorkerProgress], None] | None,
    ) -> None:
        self.state = state
        self.options = options
        self.workers = workers
        self.project_root = project_root
        self.stop_requested = stop_requested
        self.on_progress = on_progress
        self.pending: deque[WorkItem] = deque(plan)
        self.planned = len(plan)
        self.results: list[WorkerJobResult] = []
        self.worker_failures = 0
        self.stopping = False
        self.claims_drained = False
        self.deadline_seconds = slot_deadline_seconds(options)
        context = multiprocessing.get_context("spawn")
        self.result_queue: Any = context.Queue()
        self.slots: list[_Slot] = []
        for number in range(1, workers + 1):
            worker_id = slot_worker_id(options.worker_id, number)
            tasks: Any = context.Queue()
            process = context.Process(
                target=_slot_main,
                name=f"book-translator-slot-{number}",
                args=(
                    number,
                    os.getpid(),
                    state.path,
                    project_root,
                    replace(options, worker_id=worker_id),
                    provider_factory,
                    now,
                    tasks,
                    self.result_queue,
                ),
            )
            self.slots.append(_Slot(number, worker_id, process, tasks))

    # -- dispatch --------------------------------------------------------------------------

    def _next_item(self) -> WorkItem | None:
        while self.pending:
            item = self.pending.popleft()
            if item.kind == "claim" and self.claims_drained:
                continue
            return item
        return None

    def _dispatch(self) -> None:
        if self.stopping:
            return
        for slot in self.slots:
            if not slot.live or slot.item is not None:
                continue
            item = self._next_item()
            if item is None:
                return
            slot.item = item
            slot.dispatched_at = time.monotonic()
            slot.tasks.put(item)

    def _in_flight(self) -> list[_Slot]:
        return [slot for slot in self.slots if slot.item is not None]

    # -- results ---------------------------------------------------------------------------

    def _record(self, result: WorkerJobResult) -> None:
        self.results.append(result)
        if result.outcome == "error" and self.options.stop_on_error:
            self.stopping = True

    def _handle(self, message: tuple[str, int, WorkItem, object]) -> None:
        kind, number, item, payload = message
        slot = self.slots[number - 1]
        slot.item = None
        slot.dispatched_at = None
        if kind == "failure":
            slot.live = False
            self.worker_failures += 1
            self.stopping = True
            self._record(
                WorkerJobResult(
                    item.job_id or self._owned_job_id(slot),
                    "error",
                    error=f"execution slot {number} failed: {payload}",
                    worker_slot=number,
                )
            )
        elif payload is None:
            if item.kind == "claim":
                self.claims_drained = True
        elif isinstance(payload, WorkerJobResult):
            self._record(replace(payload, worker_slot=number))
        else:
            raise TranslationWorkerError("execution slot returned an unrecognized message")
        self._report()

    def _report(self) -> None:
        if self.on_progress is None:
            return
        results = self.results
        self.on_progress(
            WorkerProgress(
                self.workers,
                len(self._in_flight()),
                len(results),
                self.planned,
                sum(item.outcome == "accepted" for item in results),
                sum(item.outcome == "qa_rejected" for item in results),
                sum(item.outcome == "provider_failed" for item in results),
                sum(item.outcome == "retry_pending" for item in results),
                sum(item.outcome == "permanent_failure" for item in results),
                sum(item.outcome == "error" for item in results),
                self.worker_failures,
                self.stopping,
                results[-1] if results else None,
            )
        )

    # -- slot health -----------------------------------------------------------------------

    def _drain(self) -> None:
        while True:
            try:
                self._handle(self.result_queue.get_nowait())
            except Empty:
                return

    def _slot_failed(self, slot: _Slot, reason: str) -> None:
        item = slot.item
        slot.item = None
        slot.dispatched_at = None
        slot.live = False
        self.worker_failures += 1
        self.stopping = True
        job_id = UNCLAIMED
        if item is not None:
            job_id = item.job_id or self._owned_job_id(slot)
        self._record(
            WorkerJobResult(
                job_id,
                "error",
                error=(
                    f"execution slot {slot.number} {reason}; its claim, if any, is left"
                    " untouched for queue recovery"
                ),
                worker_slot=slot.number,
            )
        )
        self._report()

    def _owned_job_id(self, slot: _Slot) -> str:
        """Name the job a failed slot still owns; an unreadable queue must not hide the failure."""
        try:
            return _owned_job_id(self.state.path, self.project_root, slot.worker_id)
        except (QueueError, OSError) as exc:
            return f"{UNCLAIMED} (owned job unknown: {_diagnostic(exc)})"

    def _check_slots(self) -> None:
        for slot in self.slots:
            if not slot.live or slot.item is None:
                continue
            if not slot.process.is_alive():
                # A result may have been sent right before the slot died; take it first.
                self._drain()
                if slot.item is None:
                    slot.live = False
                    continue
                self._slot_failed(
                    slot, f"exited unexpectedly with exit code {slot.process.exitcode}"
                )
            elif (
                slot.dispatched_at is not None
                and time.monotonic() - slot.dispatched_at > self.deadline_seconds
            ):
                _kill_slot(slot)
                self._slot_failed(slot, "exceeded its execution deadline and was killed")

    # -- lifecycle -------------------------------------------------------------------------

    def _stop_wanted(self) -> bool:
        return self.stop_requested is not None and self.stop_requested()

    def run(self) -> WorkerResult:
        signals = _StopSignals()
        signals.install()
        try:
            for slot in self.slots:
                slot.process.start()
            while True:
                if not self.stopping and (signals.requested or self._stop_wanted()):
                    self.stopping = True
                    self._report()
                self._dispatch()
                if not self._in_flight():
                    # Nothing was dispatched to an idle slot, so either the pass is stopping,
                    # the plan is exhausted, or no slot is left alive: all three end the pass.
                    break
                try:
                    message = self.result_queue.get(timeout=POLL_SECONDS)
                except Empty:
                    self._check_slots()
                    continue
                self._handle(message)
        except KeyboardInterrupt:
            # A second Ctrl+C is a hard interrupt. Waiting out the grace period for slots that
            # are mid-call would turn it into a slow one, so they are killed now; a job killed
            # mid-call keeps its `requesting` reservation for explicit resolution.
            for slot in self.slots:
                if slot.process.is_alive():
                    _kill_slot(slot)
            raise
        finally:
            signals.restore()
            self._shutdown()
        self._refuse_duplicates()
        return aggregate_results(
            self.state.run.run_id,
            self.options.worker_id,
            tuple(self.results),
            workers=self.workers,
            worker_failures=self.worker_failures,
        )

    def _shutdown(self) -> None:
        for slot in self.slots:
            if slot.process.is_alive():
                slot.tasks.put(None)
        for slot in self.slots:
            if slot.process.is_alive():
                slot.process.join(SHUTDOWN_GRACE_SECONDS)
            if slot.process.is_alive():
                _kill_slot(slot)
            slot.tasks.close()
        self.result_queue.close()

    def _refuse_duplicates(self) -> None:
        seen: set[str] = set()
        for result in self.results:
            if result.job_id.startswith(UNCLAIMED):
                continue
            if result.job_id in seen:
                raise TranslationWorkerError(
                    f"execution slots reported the same job twice: {result.job_id}"
                )
            seen.add(result.job_id)


def run_worker_pool(
    state: QueueState,
    plan: tuple[WorkItem, ...],
    options: WorkerOptions,
    *,
    workers: int,
    project_root: Path,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None = None,
    now: Callable[[], datetime] | None = None,
    stop_requested: Callable[[], bool] | None = None,
    on_progress: Callable[[WorkerProgress], None] | None = None,
) -> WorkerResult:
    """Execute one already-recovered pass with ``workers`` slots and return the aggregate.

    The caller has validated the options, run the provider preflight, and run queue recovery;
    ``state`` is the queue as recovered and ``plan`` its bounded work plan. Results are reported
    in completion order, each naming the slot that produced it.
    """
    if workers < 2:
        raise TranslationWorkerError("a worker pool needs at least two execution slots")
    if not plan:
        return aggregate_results(state.run.run_id, options.worker_id, (), workers=workers)
    return _Coordinator(
        state,
        plan,
        options,
        workers=workers,
        project_root=project_root,
        provider_factory=provider_factory,
        now=now,
        stop_requested=stop_requested,
        on_progress=on_progress,
    ).run()
