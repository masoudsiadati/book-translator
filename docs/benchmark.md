# Benchmark policy

Benchmarks are required when a change is likely to materially affect large-text processing,
complete-book batches, network or API usage, retained memory, concurrency, EPUB construction, or
repeated hashing and serialization.

## Rules

- Generate synthetic input by default; never commit real book prose.
- State the input shape and byte, page, paragraph, word, item, or job count relevant to the stage.
- Measure elapsed wall-clock time and throughput where it clarifies scaling.
- Measure peak Python or process memory when practical.
- Run multiple iterations and report individual or aggregate observations.
- Verify deterministic output bytes, hashes, identifiers, and counters when the stage is deterministic.
- Do not add machine-specific timing assertions to unit tests.
- Store repeatable benchmark programs under `scripts/`.
- Treat results as regression indicators and capacity evidence, not micro-optimization contests.
- Do not optimize a theoretical issue unless profiling, scaling analysis, or the benchmark shows a
  material problem for expected workloads.

For provider-backed future stages, synthetic/local provider doubles are used unless the user
explicitly authorizes a paid or external benchmark. Reports separate network latency, rate limiting,
retries, and provider cost where meaningful and never disclose book content.

## Benchmark inventory

| Area | Status | Script or intended evidence |
| --- | --- | --- |
| PDF inspection | Planned | Generated PDFs across representative page counts and text densities |
| Chapter extraction | Planned | Generated PDF and canonical TOC with varying pages and block counts |
| Deterministic normalization | Implemented | `scripts/benchmark_normalization.py` |
| Document Chunk Engine | Planned for 0.6.0 | Generated paragraphs, boundary cases, and complete-book scale |
| Translation queue | Planned | Local provider double, queue depth, retry, rate, and memory measurements |
| RTL EPUB generation | Planned | Generated approved chapters and archive/validation measurements |

The normalization benchmark generates 500 synthetic blocks over multiple canonical pages, runs at
least three iterations, traces peak Python memory, and verifies identical output and counters. Current
observations and complexity analysis are recorded in `docs/normalization-audit.md`.
