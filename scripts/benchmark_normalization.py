"""Benchmark deterministic normalization using generated synthetic text only."""

import hashlib
import statistics
import time
import tracemalloc
from dataclasses import dataclass

from book_translator.normalization import normalize_text

PAGE_COUNT = 25
PARAGRAPHS_PER_PAGE = 20
RUN_COUNT = 3


@dataclass(frozen=True, slots=True)
class BenchmarkObservation:
    elapsed_seconds: float
    peak_bytes: int
    output: bytes
    counters: tuple[int, int, int, int, int]


def _synthetic_block(index: int) -> str:
    variant = index % 10
    if variant == 0:
        return f"Synthetic Heading {index:03d}"
    if variant == 1:
        return "A short standalone line"
    if variant == 2:
        return (
            '"Synthetic dialogue begins here," the test speaker said.\n'
            "The generated response continues on a wrapped physical line."
        )
    if variant == 3:
        return "A generated multi-\nline-\ncompound remains explicitly hyphenated."
    if variant == 4:
        return (
            "A generated internal-hyphen stays within its physical line, while an em dash—stays.\n"
            "An en dash–also stays, and ordinary punctuation remains deterministic."
        )
    if variant == 5:
        return "First synthetic verse line\nSecond synthetic verse line\nThird synthetic verse line"
    return (
        f"Synthetic paragraph {index:03d} contains generated words for deterministic measurement.\n"
        "Its second physical line is visually wrapped but belongs to the same extracted block.\n"
        "Its final physical line provides stable punctuation and repeatable counters."
    )


def generate_synthetic_source() -> str:
    sections: list[str] = []
    block_index = 0
    for page in range(1, PAGE_COUNT + 1):
        blocks = []
        for _ in range(PARAGRAPHS_PER_PAGE):
            blocks.append(_synthetic_block(block_index))
            block_index += 1
        sections.append(f"<!-- source-page: {page} -->\n\n" + "\n\n".join(blocks))
    return "\n\n".join(sections) + "\n"


def _run_once(source: str) -> BenchmarkObservation:
    tracemalloc.start()
    started = time.perf_counter()
    result = normalize_text(source)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    output = result.cleaned_text.encode("utf-8")
    counters = (
        result.document.source_physical_line_count,
        len(result.document.blocks),
        result.page_markers_removed,
        result.wrapped_line_joins,
        result.hyphen_repairs,
    )
    return BenchmarkObservation(elapsed, peak, output, counters)


def main() -> None:
    source = generate_synthetic_source()
    observations = [_run_once(source) for _ in range(RUN_COUNT)]
    baseline = observations[0]
    if any(item.output != baseline.output for item in observations[1:]):
        raise RuntimeError("normalization output bytes differ across benchmark runs")
    if any(item.counters != baseline.counters for item in observations[1:]):
        raise RuntimeError("normalization counters differ across benchmark runs")
    physical_lines, source_blocks, markers, joins, repairs = baseline.counters
    digest = hashlib.sha256(baseline.output).hexdigest()
    elapsed = [item.elapsed_seconds for item in observations]
    print("Synthetic normalization benchmark")
    print(f"runs: {RUN_COUNT}")
    print(f"input bytes: {len(source.encode('utf-8'))}")
    print(f"physical lines: {physical_lines}")
    print(f"source blocks: {source_blocks}")
    print(f"cleaned paragraphs: {source_blocks}")
    print(f"page markers removed: {markers}")
    print(f"wrapped-line joins: {joins}")
    print(f"hyphen repairs: {repairs}")
    print(f"elapsed seconds per run: {', '.join(f'{value:.6f}' for value in elapsed)}")
    print(f"median elapsed seconds: {statistics.median(elapsed):.6f}")
    print(f"peak Python memory bytes: {max(item.peak_bytes for item in observations)}")
    print(f"output SHA-256: {digest}")
    print("deterministic output and counters: yes")


if __name__ == "__main__":
    main()
