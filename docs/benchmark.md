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
| Full-book Source Quality Audit | Implemented | `scripts/benchmark_source_quality_audit.py` |

The source-quality benchmark includes current normalization-change provenance resolution and chunk
readiness analysis; it performs no writes or provider calls.
On 2026-08-13, Audit Engine 1.2.0 scanned 57 chapters and 913 pages with 159 findings in 0.895832
seconds (63.63 chapters/second, 1,019.16 pages/second) with 45,344 KiB peak process memory. The
read-only benchmark includes current lineage validation and its timing is observational.
| Document Chunk Engine | Implemented | `scripts/benchmark_chunking.py` |
| Mutable artifact regeneration | Implemented | `scripts/benchmark_artifact_regeneration.py` |
| Translation Snapshot Engine | Implemented | `scripts/benchmark_translation_snapshot.py` |
| Translation Queue | Implemented | `scripts/benchmark_translation_queue.py` |
| Prompt Package Renderer | Implemented | `scripts/benchmark_prompt_packages.py` |
| Terminology Consistency Engine | Implemented | `scripts/benchmark_terminology.py` |
| Terminology Discovery/Proposal | Implemented | `scripts/benchmark_terminology_analysis.py` |
| Terminology Registry Remediation | Implemented | `scripts/benchmark_registry_remediation.py` |

The registry remediation benchmark builds a synthetic 300-decision registry carrying every audited
defect shape and proves that three runs produce an identical plan identity and simulated registry
hash. On 2026-08-15 it observed a 6.705314-second median plan and a 10.204287-second median dry run
with 10,416,011 bytes peak traced memory. Cost is dominated by the pre-existing read-only audit
rather than by planning: on the real 882-decision registry the audit takes about 24.7 seconds while
plan construction takes about 2.8 seconds, and a dry run re-audits the registry twice by design.
Timings are observational and carry no pass/fail threshold.
| Terminology Canonical Review | Implemented | `scripts/benchmark_terminology_review.py` |
| Series terminology derivation and context rendering | Implemented | `scripts/benchmark_series_terminology.py` |

The series-terminology benchmark builds a synthetic 1,200-term policy (a third locked, a third
strong reference, a third observed) and 227 synthetic chunks of 30 paragraphs, derives the book
registry, and assembles and renders the effective context of every chunk with a synthetic
15-term / 8-segment reference package, three times, verifying one stable aggregate SHA-256. On
2026-09-14 derivation completed in 13.044442 seconds median and context rendering in 8.170441
seconds median (27.95 chunks/second best) with 44,009,434 peak traced bytes and stable digest
`050371fb4815e0b28edca6c48ec305b8c5fee849a18a0a4fa78a38b583d9c6b5`. Derivation runs once per
policy change; rendering cost is paid once per prompt package. External retrieval is measured by
the operator on the real corpora because it is dominated by the storage they live on: on the same
day the read-only retriever needed about 20 seconds for its first context (loading both books'
translation memory) and 9–12 seconds per further chunk from a Windows-mounted drive, so the full
227-chunk batch is a one-time, resumable operation of roughly 40 minutes. Timings are observational.
| Translation Provider Executor | Implemented | `scripts/benchmark_provider_executor.py` |
| Translation Worker acceptance path | Implemented | `scripts/benchmark_translation_worker.py` |
| Translation Worker process pool | Implemented | `scripts/benchmark_worker_pool.py` |
| Chapter-level literary governance | Implemented | `scripts/benchmark_literary_editing.py` |
| RTL EPUB generation | Planned | Generated approved chapters and archive/validation measurements |

The literary-governance benchmark generates five 80-paragraph synthetic chapters, changes half the
paragraphs, and runs whole-chapter section planning, strict editor-response validation, deterministic
merge, diff classification, marker validation, structural QA, and alignment QA three times. It never
contacts a provider and therefore measures local safety overhead rather than model latency. On
2026-09-06, 400 paragraphs / 140,710 source-plus-target characters completed in 0.141249 seconds best
(2,831.89 paragraphs/second) with 763,195 peak traced bytes and stable governance SHA-256
`b8a17b8349198bf2f102a1ad9141e7dbf11f5ccc0e7bef71d21101ab32261193`. Timings are observational.

The normalization benchmark generates 500 synthetic blocks over multiple canonical pages, runs at
least three iterations, traces peak Python memory, and verifies identical output and counters. Current
observations and complexity analysis are recorded in `docs/normalization-audit.md`.

The chunk benchmark generates 5,000 variable-size paragraphs with explicit separators and context,
runs three iterations, validates exact reconstruction, and hashes the complete structural plan. On
2026-08-06, a 2,039,410-byte / 397,004-word input produced 220 chunks and 54,750 context words with a
0.303957-second median, 6.399 MiB/second throughput, 17,841,411 peak traced bytes, and stable plan hash
`18a6ae310abdd45df8186f8f1249fef739ca3da538b3615a2603d6a7bbe9ef02`. Timings are observational.

The mutable-regeneration benchmark builds 12 synthetic canonical chapters with 960 extracted blocks,
old-version normalized metadata, and current chunks, then performs the complete read-only inspection
and candidate-planning path three times. On 2026-08-08, 106,968 source bytes completed in 1.708170
seconds median with 3,147,689 peak traced bytes and stable plan SHA-256
`84b4bd38e45c1eef1eb38d29628b1e127b149cc43660d66b1fec9e827653c5bb`. The workflow intentionally
rehashes canonical artifacts and rebuilds deterministic plans during every safety preflight; timings
are observational.

The translation snapshot benchmark builds a synthetic 60-chapter, 400-chunk manifest with source,
context, and resource references. It measures model construction, canonical JSON identity encoding,
SHA-256, readable YAML generation, and validation round trip over three iterations, including peak
traced memory and deterministic hash verification. Local observations are recorded after validation.
On 2026-08-06, the 201,330-byte canonical payload and 210,518-byte YAML representation completed in
0.578338 seconds median with 5,246,737 peak traced bytes and stable manifest SHA-256
`499853dfed7190396fee591f7f5ff8991503198d1dc2b6626e3796e2eb582607`. Timings are observational.

The queue benchmark constructs a synthetic 60-chapter/400-job plan, generates YAML, atomically
publishes and reloads every job in a temporary directory, recomputes counters, selects the first
claim candidate, and verifies a stable structural hash over three runs. On 2026-08-06, 475,958 bytes
of job metadata completed in 8.108996 seconds median with 5,778,883 peak traced bytes and stable hash
`aafd8afed17525f05f6da3550eaabf66d389d398aaf446e3d62d955513deae03`.
The per-file `fsync` publication cost is intentional durability evidence; timings are observational.

The prompt-package benchmark renders, hashes, validates length-framed source/context, generates
canonical identities, and serializes structural YAML for 400 synthetic jobs across 60 chapters. It
uses a 500-term glossary, varied sources and contexts, three iterations, traced peak memory, and a
stable aggregate plan hash. Local observations are recorded after validation.
On 2026-08-06, 400 packages from 528,610 source/context bytes rendered at a best 140.38
packages/second across three runs, with 1,182,598 peak traced bytes and stable plan SHA-256
`4a5ccc980940e4baf7896645b767c246c159478a69da3836a440a0426e127c25`. Timings are observational.

The provider-executor benchmark constructs 400 synthetic role-separated requests, uses only the
deterministic fake provider, performs structural result validation, serializes private-attempt-shaped
metadata, and verifies a stable nonsecret aggregate hash over three iterations. It deliberately does
not measure network latency or provider quality. Local observations are recorded after validation.
On 2026-08-06, 400 jobs serialized 266,000 artifact bytes at a best 1,132.52 jobs/second,
with 636,962 peak traced bytes and stable structural SHA-256
`5e871d73f2b1c9e0ae2d4a4c4fc9231032c3968ed10513dc09a8dd0fdc16827b`.

The translation-worker benchmark runs 400 deterministic fake-provider results through terminology
and structural acceptance QA over three iterations without network access or real prose. On
2026-08-10, it completed in 0.019648 seconds median (20,358.58 jobs/second) with 136,605 peak traced
bytes and stable aggregate SHA-256
`46fe9e69608434b54bba22426be7d654425a644f966e437eb97d57f81a91705c`. This isolates local
acceptance overhead; real worker throughput is provider-latency dominated.

The worker-pool benchmark builds a synthetic 24-job run per measurement and runs a whole pass with
1, 2, 4, and 8 execution slots behind a fake provider that only sleeps and returns fixed synthetic
text, then verifies that every job was accepted on its first attempt. It proves overlap and
measures the queue-bound floor rather than asserting a speedup. On 2026-09-16:

| Provider latency | Workers | Elapsed seconds | Jobs/second | Speedup |
| --- | --- | --- | --- | --- |
| 0.1 s | 1 | 45.108 | 0.53 | 1.00 |
| 0.1 s | 2 | 31.471 | 0.76 | 1.43 |
| 0.1 s | 4 | 30.138 | 0.80 | 1.50 |
| 0.1 s | 8 | 29.574 | 0.81 | 1.53 |
| 1.0 s | 1 | 61.382 | 0.39 | 1.00 |
| 1.0 s | 2 | 38.067 | 0.63 | 1.61 |
| 1.0 s | 4 | 29.534 | 0.81 | 2.08 |
| 1.0 s | 8 | 32.066 | 0.75 | 1.91 |

At these latencies each job spends far longer in the queue's own work than at the provider: every
job performs several full parses of the run, and a claim, reservation, finalization, and
acceptance each hold the run lock across at least one of them, so slots serialize on the lock and
scaling flattens near two. Each parse costs about 0.5 s at 100 jobs and 1.1 s at 226 jobs, which
is also the one CPU core a single worker was observed to use. A real Claude call takes minutes, so
lock time is a small share of each job and several slots overlap usefully; the provider's own
concurrency and rate limits then bind first. Reducing the per-read parse cost is the lever for
higher slot counts and is deliberately out of scope here. Timings are observational.

The terminology benchmark constructs 2,000 synthetic canonical decisions and validates 100 relevant
source/target occurrences over three runs while also verifying stable registry serialization. On
2026-08-08, it completed in 0.282516 seconds median with 4,488,725 peak traced bytes and stable
registry SHA-256 `fd614c5a61bac7111c155d5879089d671e32facd8aefd1ed4582575b196e0e6a`.
Timings are observational.

The terminology-analysis benchmark reconciles 10,000 synthetic evidence records into 2,000
candidates, performs exact reuse lookup against 1,000 canonical compatibility terms, and applies
2,000 high-confidence proposals over three runs. On 2026-08-08, it completed in 6.865123 seconds
median with 23,864,482 peak traced bytes and stable registry SHA-256
`c226436624717d79be4b6a89f989f9f13cb4f4a54f53f693a4f0714e9d6cde71`. Timings are
observational; the current quadratic registry-update path is acceptable at this scale but remains an
optimization target before substantially larger candidate sets.

The terminology-review benchmark validates and identifies 2,000 synthetic accepted reviews over
three runs. On 2026-08-08, it completed in 0.081317 seconds median with 5,971,706 peak traced bytes and
stable review SHA-256 `01222dea90969ffeb08f3606083816c81088d56bf9a6176393e2ed8abdaeebd8`.
After adding versioned target-style canonical comparison, a 2026-08-09 run completed in 0.104707
seconds median with 6,177,138 peak traced bytes and stable review SHA-256
`540eb29ad136d2ec53cf87bc73218440bccd4270878fbcdbe2548f78be61aa61`. The linear exact
transformation remains immaterial beside provider latency; timings are observational.

Review Engine 2 additionally exercises deterministic token/count shard planning. Real provider call
count, shard distribution, retries, and elapsed time are recorded in the operator-run report rather
than mixed into the provider-free validation microbenchmark.
