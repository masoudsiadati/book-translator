# Architecture contract

This document freezes the boundaries of the future `book-translator` pipeline. The detailed
contracts are [the data model](docs/data-model.md), [pipeline](docs/pipeline.md),
[recovery rules](docs/recovery.md), and [naming conventions](docs/naming-conventions.md).

## Principles

- The configured PDF is an initial input, not a downstream data store. Once a validated
  extraction snapshot exists, every later stage reads workspace Markdown and metadata only.
- `workspace/` is the canonical representation. The legacy top-level `extracted/`, `chunks/`,
  `translated/`, and `review/` directories may remain during migration but receive no new
  contract.
- Persisted records use Pydantic contracts in `book_translator.models`; committed JSON Schemas
  are generated from those models. Schema version `1.0` is the initial contract.
- Artifact lineage is established by byte-exact SHA-256 hashes. A mismatch makes a derived
  artifact stale; it must be refused or explicitly regenerated.
- State changes use `status_transitions.py`. Translation, validation, review, and approval are
  distinct states. Approval is never inferred.
- Every generated write is UTF-8 where textual, atomic, resumable, and idempotent. Book content
  is never logged and is sent only to the configured translation provider.

## Component boundaries

Inspection and discovery may read the PDF. Extraction writes immutable chapter `source.md`.
Normalization derives `cleaned.md`. Generic document chunking may derive deterministic chunks from a
hash-verified canonical `cleaned.md` before snapshot support exists. Snapshot creation will freeze
validated extraction and chunk lineage before any external or translation stage. Chunking and every
later component consume only workspace Markdown and metadata and never reopen the PDF. Models and transition
policy contain no file I/O; path derivation contains no directory creation; orchestration owns
I/O, atomic replacement, hash checks, and state persistence.

The freeze specifies boundaries rather than implementation details. Chapter extraction and
deterministic normalization and generic document chunking now implement those boundaries; translation,
review, snapshots, and EPUB generation remain outside the implemented scope.

Embedded TOC discovery preserves every source entry in source order. Classification is
deterministic: numbered `Chapter` labels and `Prologue` are parsed structurally; non-structural
entries before the first of those are front matter; conventional `Appendix`/`Glossary` labels
are appendices; everything else remains unknown. Equal start pages share the boundary before
the next later distinct start page.
