"""Benchmark the Document Chunk Engine with generated synthetic content only."""

import hashlib
import statistics
import time
import tracemalloc
from dataclasses import dataclass

from book_translator.document_chunking import ChunkingConfig, plan_document
from book_translator.document_chunking.models import DocumentChunkPlan

PARAGRAPH_COUNT = 5_000
RUN_COUNT = 3


@dataclass(frozen=True, slots=True)
class Observation:
    elapsed_seconds: float
    peak_bytes: int
    plan_hash: str
    chunk_count: int
    context_words: int


def generate_synthetic_document() -> str:
    paragraphs: list[str] = []
    for index in range(PARAGRAPH_COUNT):
        if index and index % 125 == 0:
            paragraphs.append(("***", "* * *", "# # #", "— — —")[(index // 125) % 4])
            continue
        word_count = 35 + (index * 17) % 91
        words = [f"synthetic-{index:04d}"]
        words.extend(f"w{word_index:03d}" for word_index in range(1, word_count))
        paragraphs.append(" ".join(words))
    return "\n\n".join(paragraphs) + "\n"


def _hash_plan(plan: DocumentChunkPlan) -> str:
    digest = hashlib.sha256()
    for chunk in plan.chunks:
        digest.update(str(chunk.sequence).encode())
        digest.update(str(chunk.paragraph_start).encode())
        digest.update(str(chunk.paragraph_end).encode())
        digest.update(hashlib.sha256(chunk.source_text.encode()).digest())
        digest.update(hashlib.sha256(chunk.context.text.encode()).digest())
        digest.update(str(chunk.word_count).encode())
        digest.update(str(chunk.context.word_count).encode())
    digest.update(plan.reconstructed_text.encode())
    return digest.hexdigest()


def _run(source: str, config: ChunkingConfig) -> Observation:
    tracemalloc.start()
    started = time.perf_counter()
    plan = plan_document(source, config)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if plan.reconstructed_text != source:
        raise RuntimeError("benchmark reconstruction failed")
    return Observation(
        elapsed_seconds=elapsed,
        peak_bytes=peak,
        plan_hash=_hash_plan(plan),
        chunk_count=len(plan.chunks),
        context_words=plan.total_context_words,
    )


def main() -> None:
    source = generate_synthetic_document()
    config = ChunkingConfig(target_words=1_800, max_words=2_400, context_words=250)
    observations = [_run(source, config) for _ in range(RUN_COUNT)]
    baseline = observations[0]
    if any(
        (item.plan_hash, item.chunk_count, item.context_words)
        != (baseline.plan_hash, baseline.chunk_count, baseline.context_words)
        for item in observations[1:]
    ):
        raise RuntimeError("chunk plans or counters differ across benchmark runs")
    elapsed = [item.elapsed_seconds for item in observations]
    median = statistics.median(elapsed)
    input_bytes = len(source.encode("utf-8"))
    input_words = len(source.split())
    print("Synthetic Document Chunk Engine benchmark")
    print(f"runs: {RUN_COUNT}")
    print(f"input bytes: {input_bytes}")
    print(f"input words: {input_words}")
    print(f"input paragraphs: {PARAGRAPH_COUNT}")
    print(f"chunk count: {baseline.chunk_count}")
    print(f"context words generated: {baseline.context_words}")
    print(f"elapsed seconds per run: {', '.join(f'{value:.6f}' for value in elapsed)}")
    print(f"median elapsed seconds: {median:.6f}")
    print(f"throughput MiB/second: {input_bytes / (1024 * 1024) / median:.3f}")
    print(f"peak Python memory bytes: {max(item.peak_bytes for item in observations)}")
    print(f"stable plan SHA-256: {baseline.plan_hash}")
    print("deterministic plans, counters, and reconstruction: yes")


if __name__ == "__main__":
    main()
