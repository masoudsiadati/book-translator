# EPUB Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import a generic EPUB into canonical book chapter artifacts and ingest Book 8 through deterministic chunking.

**Architecture:** A dedicated EPUB parser/planner reads container, OPF, spine, and optional NCX, then adapts logical narrative units into the existing chapter source/metadata contract. Existing normalization and chunking run unchanged over the new workspace.

**Tech Stack:** Python 3.12 standard library (`zipfile`, `xml.etree.ElementTree`, `html.parser`), Pydantic, PyYAML, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-11-07-epub-ingestion-design.md`

## Global Constraints

- Use synthetic EPUB content in tests; never print or fixture real book prose.
- Spine order is authoritative; ZIP order and filename sorting never determine reading order.
- No provider, terminology, translation, semantic-QA, title-translation, commit, tag, or push action.
- Preserve existing Books 5, 6, and 7 byte-for-byte.
- Run only focused tests plus checks directly required by shared contract edits.

## Review Focus

- Archive href traversal or missing manifest targets must fail closed.
- NCX labels with fragments and URL encoding must attach titles to the correct spine resource.
- Nonconsecutive resources with similar stems must not be merged across another unit.
- Inline emphasis spanning nested tags must remain balanced Markdown.
- Reruns must skip exact artifacts and refuse conflicting/downstream artifacts.

---

### Task 1: EPUB parsing and narrative discovery

**Files:**
- Create: `src/book_translator/epub_ingestion.py`
- Create: `tests/test_epub_ingestion.py`

**Interfaces:**
- Produces: `inspect_epub(path: Path) -> EpubDocument` and narrative unit records in spine order.
- Consumes: none beyond standard library and existing `TocKind`/path identity helpers.

- [ ] Write synthetic EPUB tests for spine order, NCX paired title extraction, split-unit merging,
  non-narrative exclusion, path safety, and malformed/missing package records.
- [ ] Run `pytest tests/test_epub_ingestion.py -q` and verify failures are due to missing behavior.
- [ ] Implement container/OPF/manifest/spine/NCX parsing and narrative grouping.
- [ ] Run the focused file and verify discovery tests pass.

### Task 2: HTML extraction and canonical publication

**Files:**
- Modify: `src/book_translator/epub_ingestion.py`
- Modify: `tests/test_epub_ingestion.py`
- Modify: `src/book_translator/models/book.py`
- Modify: `schemas/toc.schema.json`

**Interfaces:**
- Consumes: narrative units from Task 1.
- Produces: `plan_epub_import(...) -> EpubImportPlan` and `execute_epub_import(...) -> EpubImportResult` writing canonical TOC, `source.md`, and `metadata.yaml`.

- [ ] Add failing tests for paragraph preservation, emphasis, heading/noise removal, source hashes,
  split-resource markers, idempotent rerun, and downstream force refusal.
- [ ] Run the new tests and verify the intended failures.
- [ ] Implement deterministic HTML rendering, planning, validation, serialization, and atomic writes.
- [ ] Regenerate the TOC schema and run focused ingestion/extraction/normalization tests.

### Task 3: Configuration and CLI integration

**Files:**
- Modify: `src/book_translator/config.py`
- Modify: `src/book_translator/cli.py`
- Modify: PDF-only config consumers as required for explicit EPUB refusal.
- Modify: `tests/test_epub_ingestion.py`
- Modify: relevant config/CLI contract tests.

**Interfaces:**
- Consumes: import plan/executor from Task 2.
- Produces: `import-epub EPUB --book BOOK [--force] [--dry-run]` and mutually exclusive PDF/EPUB config paths.

- [ ] Add failing tests for EPUB config validation, legacy PDF compatibility, and CLI dry-run/import.
- [ ] Run those tests and verify failures.
- [ ] Implement config helpers and CLI command without changing existing PDF output behavior.
- [ ] Run focused EPUB, config, PDF extraction, workspace, normalization, and chunking tests.

### Task 4: Documentation, Book 8 ingestion, and evidence

**Files:**
- Create: `config/books/the-path-of-daggers.yaml`
- Create: `workspace/books/the-path-of-daggers/toc.yaml`
- Create: `workspace/books/the-path-of-daggers/chapters/**`
- Modify: `ARCHITECTURE.md`, `docs/pipeline.md`, `docs/data-model.md`, `docs/recovery.md`, `README.md`, `CHANGELOG.md` as needed.

**Interfaces:**
- Consumes: `import-epub`, `normalize-chapters`, and `chunk-chapters`.
- Produces: one Book 8 workspace stopped at `chunked` state and a reproducible final report.

- [ ] Add the Book 8 config and verify the supplied EPUB SHA-256 before import.
- [ ] Run EPUB import, normalization, and chunking with `--book the-path-of-daggers`.
- [ ] Inspect only metadata/counters/hashes to verify 32 units, no glossary, title coverage, word
  conservation, chunk conservation, and zero translation/provider artifacts.
- [ ] Update affected docs and changelog.
- [ ] Run focused pytest, Ruff lint/format, and strict mypy for changed modules; do not run the full
  suite unless a shared-contract failure demonstrates it is required.
- [ ] Review `git diff`, list all created/modified files, and prove Books 5/6/7 have no diffs.

