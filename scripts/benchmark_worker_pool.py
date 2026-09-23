"""Benchmark multi-process worker scaling with a latency-bound fake provider and synthetic text.

The pool is built to overlap provider latency, so the provider double here does nothing but
sleep for a fixed time and answer with a fixed synthetic translation. The script builds one
synthetic run per worker count, runs a whole pass, and reports wall-clock time, throughput, and
the speedup relative to one worker. It performs no network access, contacts no real provider,
and prints only counts and timings.

Two latencies are measured. At 100 ms the pass is dominated by the queue's own per-job work
(several full parses of the run per job, some under the queue lock), so the speedup shows the
lock-bound floor; at 1 s it is provider-dominated, which is the shape of a real run.
"""

import sys
import tempfile
import time
from pathlib import Path

from book_translator.models import ProviderName
from book_translator.provider_execution import ProviderRequest, ProviderResult, ProviderUsage
from book_translator.translation_queue import load_run_summary
from book_translator.translation_worker import WorkerOptions, run_translation_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

JOBS = 24
WORKER_COUNTS = (1, 2, 4, 8)
LATENCIES_SECONDS = (0.1, 1.0)
TRANSLATION = "ترجمهٔ ساختگی نخست.\n\nترجمهٔ ساختگی دوم.\n"


class SleepingProvider:
    """Answer every request with the same synthetic translation after a fixed delay."""

    provider_name = "fake"
    adapter_version = "benchmark-1.0.0"

    def __init__(self, latency_seconds: float) -> None:
        self.latency_seconds = latency_seconds

    def __call__(self, _job: object) -> "SleepingProvider":
        return self

    def execute(self, request: ProviderRequest) -> ProviderResult:
        time.sleep(self.latency_seconds)
        return ProviderResult(
            response_id="benchmark-response",
            model=request.model or "fake-v1",
            status="completed",
            text_outputs=(TRANSLATION,),
            usage=ProviderUsage(10, 10, 20),
            sanitized_response={"status": "completed"},
        )


def _build_run(root: Path) -> str:
    from tests.test_translation_worker import _prepared_run

    return _prepared_run(root, chapters=JOBS)


def _measure(root: Path, run_id: str, workers: int, latency: float) -> tuple[float, int]:
    options = WorkerOptions(
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="benchmark-worker",
        max_jobs=JOBS,
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        workers=workers,
    )
    started = time.perf_counter()
    result = run_translation_worker(
        run_id,
        project_root=root,
        options=options,
        provider_factory=SleepingProvider(latency),
    )
    elapsed = time.perf_counter() - started
    if result.processed != JOBS or result.accepted != JOBS or result.errors:
        raise RuntimeError("benchmark pass did not accept every synthetic job")
    state = load_run_summary(run_id, project_root=root)
    if state.run.accepted_count != JOBS or any(job.attempt_count != 1 for job in state.jobs):
        raise RuntimeError("benchmark pass left an unexpected queue state")
    return elapsed, result.workers


def main() -> None:
    print(f"jobs={JOBS}")
    for latency in LATENCIES_SECONDS:
        baseline: float | None = None
        for workers in WORKER_COUNTS:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run_id = _build_run(root)
                elapsed, effective = _measure(root, run_id, workers, latency)
            baseline = elapsed if baseline is None else baseline
            print(
                f"latency_seconds={latency:.1f} workers={workers} effective_workers={effective}"
                f" elapsed_seconds={elapsed:.3f} jobs_per_second={JOBS / elapsed:.2f}"
                f" speedup={baseline / elapsed:.2f}"
            )


if __name__ == "__main__":
    main()
