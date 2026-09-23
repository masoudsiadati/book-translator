"""Synthetic benchmark for the provider-independent Translation Queue."""

import hashlib
import json
import statistics
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

import yaml
from benchmark_translation_snapshot import build_manifest

from book_translator.atomic_write import atomic_write_text
from book_translator.models import TranslationJob
from book_translator.translation_queue.policy import recompute_counters
from book_translator.translation_queue.workflow import _build_jobs

ITERATIONS = 3
NOW = datetime(2026, 1, 1, tzinfo=UTC)
RUN_ID = "tr-0123456789ab-001"
SNAPSHOT_ID = "translation-fa-0123456789abcdef01234567"
MANIFEST_HASH = "0123456789abcdef" * 4


def _serialize(job: TranslationJob) -> str:
    rendered = yaml.safe_dump(
        job.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
    )
    if not isinstance(rendered, str):
        raise RuntimeError("could not serialize synthetic queue job")
    return rendered


def main() -> None:
    elapsed: list[float] = []
    peaks: list[int] = []
    hashes: list[str] = []
    publication_bytes = 0
    for _ in range(ITERATIONS):
        tracemalloc.start()
        started = time.perf_counter()
        manifest = build_manifest()
        jobs = _build_jobs(
            run_id=RUN_ID,
            snapshot_id=SNAPSHOT_ID,
            manifest_hash=MANIFEST_HASH,
            manifest=manifest,
            max_attempts=3,
            timestamp=NOW,
        )
        serialized = tuple(_serialize(job) for job in jobs)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for job, text in zip(jobs, serialized, strict=True):
                atomic_write_text(root / job.chunk_id / "job.yaml", text)
            loaded = tuple(
                TranslationJob.model_validate(
                    yaml.safe_load((root / job.chunk_id / "job.yaml").read_text(encoding="utf-8"))
                )
                for job in jobs
            )
        counters = recompute_counters(loaded)
        selected = min(
            loaded,
            key=lambda job: (
                job.priority,
                job.chapter_sequence,
                job.chunk_sequence,
                job.job_id,
            ),
        )
        structural = [
            {
                "job_id": job.job_id,
                "chapter": job.chapter_id,
                "chunk": job.chunk_id,
                "priority": job.priority,
                "status": job.status.value,
            }
            for job in loaded
        ]
        plan_hash = hashlib.sha256(
            (json.dumps(structural, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest()
        if counters.pending != 400 or selected.priority != 1:
            raise RuntimeError("synthetic queue benchmark invariant failed")
        elapsed.append(time.perf_counter() - started)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peaks.append(peak)
        hashes.append(plan_hash)
        publication_bytes = sum(len(text.encode("utf-8")) for text in serialized)
    if len(set(hashes)) != 1:
        raise RuntimeError("translation queue plan is nondeterministic")
    print("chapters: 60")
    print("jobs: 400")
    print(f"published job metadata bytes: {publication_bytes}")
    print(f"iterations: {ITERATIONS}")
    print(f"elapsed seconds: {', '.join(f'{value:.6f}' for value in elapsed)}")
    print(f"median seconds: {statistics.median(elapsed):.6f}")
    print(f"peak traced bytes: {max(peaks)}")
    print(f"structural plan SHA-256: {hashes[0]}")


if __name__ == "__main__":
    main()
