"""Benchmark provider-result validation and acceptance QA with synthetic text only."""

import hashlib
import statistics
import time
import tracemalloc

from book_translator.models import TerminologyRegistry
from book_translator.provider_execution import ProviderRequest
from book_translator.provider_execution.providers.fake import FakeTranslationProvider
from book_translator.terminology import validate_terminology, validate_translation_qa

JOBS = 400
ITERATIONS = 3


def run_once() -> tuple[float, int, str]:
    source = "Synthetic first paragraph.\n\nSynthetic second paragraph.\n"
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )
    provider = FakeTranslationProvider()
    aggregate = hashlib.sha256()
    started = time.perf_counter()
    for number in range(JOBS):
        request = ProviderRequest(
            f"request-{number}",
            f"key-{number}",
            "synthetic-run",
            f"synthetic-job-{number}",
            1,
            "Synthetic system message.",
            "Synthetic user message.",
            "en",
            "fa",
            "persian-translation-text/1.0.0",
            "fake-v1",
            30,
            100,
        )
        provider.execute(request)
        translation = "ترجمهٔ ساختگی نخست.\n\nترجمهٔ ساختگی دوم.\n"
        terminology = validate_terminology(source, translation, registry)
        structural = validate_translation_qa(source, translation, target_language="fa")
        if not terminology.valid or not structural.valid:
            raise RuntimeError("synthetic acceptance unexpectedly failed")
        aggregate.update(terminology.source_sha256.encode())
        aggregate.update(structural.translation_sha256.encode())
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    return elapsed, peak, aggregate.hexdigest()


def main() -> None:
    timings: list[float] = []
    peaks: list[int] = []
    hashes: list[str] = []
    tracemalloc.start()
    for _ in range(ITERATIONS):
        tracemalloc.reset_peak()
        elapsed, peak, digest = run_once()
        timings.append(elapsed)
        peaks.append(peak)
        hashes.append(digest)
    tracemalloc.stop()
    if len(set(hashes)) != 1:
        raise RuntimeError("worker benchmark result was not deterministic")
    median = statistics.median(timings)
    print(f"jobs={JOBS}")
    print(f"iterations={ITERATIONS}")
    print(f"median_seconds={median:.6f}")
    print(f"jobs_per_second={JOBS / median:.2f}")
    print(f"peak_traced_bytes={max(peaks)}")
    print(f"aggregate_sha256={hashes[0]}")


if __name__ == "__main__":
    main()
