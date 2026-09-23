# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Governance is binding

This repository governs itself through committed contract documents, and contract tests enforce
parts of them mechanically. `AGENTS.md` is the concise mandatory entry point and applies to Claude
Code sessions too. For any nontrivial task read `ARCHITECTURE.md`, `docs/developer-charter.md`,
`ROADMAP.md`, and the docs for the affected stage before writing code. `docs/coding-standards.md`,
`docs/testing.md`, `docs/review-process.md`, and `docs/benchmark.md` define implementation, test,
review, and performance expectations; `docs/release-process.md` and `CHANGELOG.md` apply when
release behavior or a user-facing capability changes.

Do not create commits, tags, releases, or destructive invalidation without explicit user
instruction.

## Environment and checks

Python 3.12+. The project is installed editable into `.venv`; prefer the venv binaries directly so
the checks match the testing policy:

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src tests          # strict mode, must stay clean
```

Run a single test or a single case:

```bash
.venv/bin/pytest tests/test_normalization.py
.venv/bin/pytest tests/test_normalization.py::test_marker_removal_and_unrelated_comment_preservation
.venv/bin/pytest -k "chunking and force"
```

Fresh setup:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

After changing any Pydantic model under `src/book_translator/models/`, regenerate the committed
schemas or `tests/test_contracts.py::test_committed_json_schemas_match_models` fails:

```bash
.venv/bin/python scripts/generate_schemas.py
```

Benchmarks live in `scripts/benchmark_*.py`, generate synthetic text only, and are required for
materially performance-sensitive changes (`docs/benchmark.md` holds the inventory and the recorded
results). Run them with `.venv/bin/python scripts/benchmark_<stage>.py`.

The CLI is `book-translator` (`src/book_translator/cli.py`, ~80 commands); `README.md` documents
every command with its flags.

## Architecture

### `workspace/` is the canonical store

`workspace/` is the single canonical, hash-tracked representation. The top-level `extracted/`,
`chunks/`, `translated/`, `review/` directories are legacy migration remnants and receive no new
contract — do not write new artifacts there. Every path is derived from `ProjectPaths` in
`paths.py`, which is pure: it validates and formats names (`000-prologue`, `042`, chunk `001`,
`tr-<12 hex>-<seq>`) and never touches the filesystem.

The PDF is an input, not a data store. Once extraction exists, later stages read only workspace
Markdown and metadata.

### Pipeline stages

`inspect-pdf` / `discover-toc` → `extract-chapters` (immutable `source.md`) → `normalize-chapters`
(derived `cleaned.md`) → `chunk-chapters` (deterministic chunks + `context.md`) →
`create-translation-snapshot` (frozen manifest of hashes/references, never copies text) →
`create-translation-run` (provider-independent queue with claim leases and attempt accounting) →
`render-prompt-packages` → `execute-translation-job` (the only boundary that sends content to a
provider) → translation worker QA / repair / re-evaluation / cross-run reuse → `audit-final-qa` /
`finalize-translation-run` (diagnose and resolve what a completed run still rejects: re-evaluation,
provider-free structural repair, provider repair, bounded provider recovery, one governed
exceptional repair-budget escalation).

Review, approval, merging, and EPUB generation are **not implemented**. Source Quality Audit,
Source Anomaly Registry, and the Terminology Consistency Engine are read-only or
registry-only boundaries alongside this chain.

### Module layering convention

Each stage package follows the same separation (`docs/coding-standards.md` "Engine interface
convention"), and this is a convention rather than a base class — do not add an engine framework:

- `engine.py` / pure pass modules (`markers.py`, `paragraphs.py`, `hyphenation.py`, …): pure
  transformation, no I/O
- `models.py`: typed plans and results, usually frozen dataclasses
- `planning.py` / `identity.py`: pure planning, deterministic ID derivation
- `reporting.py`: deterministic report construction
- `workflow.py`: the only layer with file I/O — exposes `plan_<stage>(...)` returning a plan and
  `execute_<stage>(plan)` performing ordered atomic publication
- `cli.py`: argument parsing, orchestration, Rich rendering (Rich appears nowhere else)

`--dry-run` on a CLI command means the planning half runs and `execute_*` never does.

Cross-cutting primitives that must be reused rather than reimplemented: `paths.py` (path
derivation), `hashing.py` (`sha256_file`, streamed), `atomic_write.py` (all publication),
`status_transitions.py` (pure lifecycle policy, no I/O), `models/` (persisted contracts).

### Data contract

Persisted records are Pydantic models in `book_translator.models` with `extra="forbid"`; the JSON
Schemas in `schemas/` are generated from them and committed. `SCHEMA_VERSION = "1.0"` in
`models/__init__.py` is the contract version. Timestamps must be timezone-aware; YAML/JSON
serialization is deterministic with documented key order and a single trailing newline; persisted
paths are project-relative POSIX.

### Lineage and staleness

Artifact lineage is byte-exact SHA-256. A hash mismatch makes a derived artifact stale, and stale
artifacts are refused rather than silently rebuilt. Use `inspect-artifact-staleness` and
`regenerate-artifacts --apply`, which is the *only* sanctioned exception to stage-local downstream
guards: it may replace `cleaned.md`, normalization reports, chunk directories, chunking reports, and
matching chapter metadata — never `source.md` or any snapshot, run, attempt, translation, review, or
final artifact.

Note the asymmetry: a frozen snapshot has two independent properties. Its internal integrity stays
valid forever; a newer canonical generation only makes it *historical* for new work.

### Engine versions

Every engine carries its own semantic version independent of the application version
(`NORMALIZATION_VERSION`, `CHUNK_ENGINE_VERSION`, `SNAPSHOT_ENGINE_VERSION`,
`PROMPT_PACKAGE_RENDERER_VERSION`, `AUDIT_ENGINE_VERSION`, `FINAL_QA_RESOLUTION_POLICY_VERSION`,
`STRUCTURAL_REPAIR_ENGINE_VERSION`, `REPAIR_ESCALATION_VERSION`, `TERMINOLOGY_QA_VERSION`,
`STRUCTURAL_QA_VERSION`, per-pass `PASS_VERSION`, …). A **minor** bump means derived bytes may
intentionally change, which makes existing artifacts stale and requires
explicit regeneration — that consequence is the reason to think before bumping. Historical
two-component versions (`"1.0"`) on existing artifacts are legacy and must not be rewritten for
formatting alone. `tests/test_governance.py` asserts every implemented engine version is explicit
and well-formed.

## Invariants that break things when violated

- **`source.md` is immutable.** So are approved translations and final artifacts.
- **Never print book prose** to stdout, logs, tests, or reports, and never send it anywhere except
  through an explicitly configured translation provider (`docs/privacy.md`). Hashes, counts, IDs,
  and structural warnings are the appropriate audit output. Prompt packages and provider attempts
  contain source/translated text and are gitignored.
- **`--force` is stage-local.** It regenerates only its own stage's artifacts and never bypasses
  downstream safety; breaking stored-data changes need an explicit documented migration.
- **Determinism.** Deterministic stages must not depend on absolute paths, wall-clock time,
  randomness, filesystem ordering, locale, or network. Inject time and providers where tests benefit.
- **A finished run is diagnosed before it is repaired, and the free remedy is spent first.**
  `competing_variant` is not one defect; `final_qa/analysis.py` classifies it and only four of
  the eight final-QA verdicts may be acted on automatically. `paragraph_count_mismatch` is not one
  defect either: `structural_repair/engine.py` decides whether it is a misplaced separator (repair
  it from the frozen source, whitespace-only, no provider) or a content divergence (a provider
  answers it), and fails closed between the two. A provider attempt is never spent — and a repair
  budget never widened — while a deterministic structural repair is still available. A locked or
  user-owned canonical is never mutated to clear a finding — the translation conforms to it. A
  frozen run's terminology question is settled by a new snapshot plus
  `reuse-accepted-translations`, never by editing the frozen run.
- **Approval is never inferred.** Provider success is a draft; mechanical QA acceptance
  (`accepted` / `qa_rejected`) is not human review, and review is not approval. Route all state
  changes through `status_transitions.py`.
- **Tests use synthetic content.** Real book prose is never a committed fixture; real-data work
  follows the staged pilot progression in `docs/testing.md` and needs explicit authorization.
- No `print()` in library code, no broad exception swallowing (boundary orchestration may catch
  broadly only to preserve recovery, and must wrap the cause in a domain error), no new dependency
  without a stated rationale.

## Contract tests that fail on careless edits

`tests/test_governance.py` pins `VERSION` == `pyproject.toml` version == `__version__` (all three
must be bumped together), requires the governance docs to exist, requires `AGENTS.md` to keep
referencing each governance entry point, and asserts a set of CLI command names stays registered —
renaming a command is a breaking change. `tests/test_contracts.py` pins schema/model equivalence,
path determinism, allowed and forbidden status transitions, enum values, Persian round-tripping, and
rejection of naive timestamps.
