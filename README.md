# book-translator

`book-translator` is a local Python project intended to automate faithful English-to-Persian translation of fantasy novels from text-based PDFs and eventually produce valid RTL EPUB 3 books.

## Current scope

This version provides typed configuration and progress models, PDF inspection, embedded TOC discovery,
page extraction, immutable chapter extraction, deterministic chapter normalization, and frozen
contracts. **Chunking, AI translation, review, snapshots, and EPUB generation are not implemented
yet.** No OCR is performed.

## Architecture

The future pipeline uses `workspace/` as its canonical, hash-tracked representation. Start with
[the architecture contract](ARCHITECTURE.md), then consult the [data model](docs/data-model.md),
[pipeline and lifecycle](docs/pipeline.md), [recovery rules](docs/recovery.md), and
[naming conventions](docs/naming-conventions.md). Pydantic models and synchronized JSON Schemas
form the persisted data contract; these definitions do not imply that future stages exist yet.

## Setup and installation

Python 3.12 or newer is required.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

## Commands

Show the current project status from `config/book.yaml` and `state/progress.json`:

```bash
book-translator status
```

Inspect the configured PDF, or an explicitly supplied PDF, without writing its text:

```bash
book-translator inspect-pdf
book-translator inspect-pdf input/another-book.pdf
```

Discover and normalize the configured PDF's embedded TOC to canonical `workspace/toc.yaml`:

```bash
book-translator discover-toc
book-translator discover-toc input/another-book.pdf --output workspace/toc.yaml
book-translator discover-toc --dry-run
book-translator discover-toc --force
```

Existing output is protected unless `--force` is supplied. A dry run never writes, including
when combined with `--force`. This command uses only the embedded PDF TOC; page-text fallback
discovery is not implemented.

Extract an inclusive, 1-based page range. Existing output is protected unless `--force` is supplied:

```bash
book-translator extract-pages input/book.pdf \
  --from-page 10 --to-page 25 --output extracted/pages-010-025.txt

book-translator extract-pages --from-page 10 --to-page 25 \
  --output extracted/pages-010-025.txt --force
```

Extract immutable chapter sources from the canonical TOC. By default this selects the prologue
and numbered chapters; front matter requires an explicit option:

```bash
book-translator extract-chapters --dry-run
book-translator extract-chapters input/book.pdf
book-translator extract-chapters --chapter prologue
book-translator extract-chapters --chapter chapter-042
book-translator extract-chapters --include-front-matter
```

Each selected entry creates only canonical `metadata.yaml` and `source.md` files. A valid existing
pair is skipped. `--force` can replace source extraction only when no cleaned, chunk, translation,
review, or final artifact exists. It never invalidates downstream work.

Normalize extracted chapters locally and deterministically:

```bash
book-translator normalize-chapters --dry-run
book-translator normalize-chapters --chapter prologue
book-translator normalize-chapters --chapter 042 --show-diff-summary
book-translator normalize-chapters --chapter chapter-042 --force
```

Normalization never calls an AI model or external service. It removes canonical page markers while
reporting transitions, repairs boundary hyphens, joins wrapped prose only inside one extracted block,
and renders canonical paragraph spacing. It does not remove headers or footers, infer headings,
dehyphenate words, or rewrite semantics. `source.md` stays immutable and `cleaned.md` is derived.

Run checks:

```bash
pytest
ruff check .
ruff format --check .
mypy src tests
```

Run the deterministic normalization benchmark, which generates synthetic text only:

```bash
python scripts/benchmark_normalization.py
```

The benchmark performs three identical runs over 500 generated source blocks and reports structural
counters, elapsed time, peak traced Python memory, and the stable output SHA-256. It has no
machine-specific pass/fail timing threshold.

## Governance and development

Project development is governed by the [architecture contract](ARCHITECTURE.md),
[developer charter](docs/developer-charter.md), [coding standards](docs/coding-standards.md),
[testing policy](docs/testing.md), [review process](docs/review-process.md),
[benchmark policy](docs/benchmark.md), and [release process](docs/release-process.md). See the
[roadmap](ROADMAP.md) for nonbinding planned direction and the [changelog](CHANGELOG.md) for release
history. `AGENTS.md` is the concise mandatory entry point for Codex sessions.

## Directory overview

- `input/`: source PDF files (ignored by Git)
- `config/`: book metadata, glossary, style guide, and translation prompt
- `extracted/`: extracted chapters and source text artifacts
- `chunks/`: translation-sized source chunks
- `translated/`: draft and assembled translated chapters
- `review/`: review artifacts and approvals
- `output/`: generated books
- `state/`: resumable pipeline state
- `logs/`: runtime logs
- `assets/`: output styling such as RTL EPUB CSS
- `src/book_translator/`: Python package and CLI
- `tests/`: automated tests
- `workspace/`: canonical chapter/artifact representation (created by pipeline operations)
- `schemas/`: committed JSON Schemas generated from the canonical Pydantic models
