# Roadmap

The roadmap communicates intended sequencing, not a commitment to scope or delivery dates. Every item
still requires design review, explicit task authorization, tests, compatibility analysis, and the
governance process in `docs/developer-charter.md`.

## Completed

- Project and CLI foundation
- PDF inspection
- Raw page-range extraction
- Embedded TOC discovery and normalization
- Canonical immutable chapter extraction
- Deterministic modular chapter normalization

## Planned

- **0.6.0 — Generic Document Chunk Engine:** deterministic document-level chunk planning and
  publication without translation behavior.
- **0.7.0 — Translation job queue:** resumable provider execution, attempts, failures, and queue
  orchestration.
- **0.8.0 — Translation engine:** prompt versioning, glossary integration, context packaging, and
  translation lineage.
- **0.9.0 — Validation and review engine:** mechanical validation, review-required states, correction
  workflow, and explicit approval boundaries.
- **1.0.0 — RTL EPUB 3:** approved-content assembly, EPUB construction, and structural/conformance
  validation.

## Future possibilities

These ideas are noncommitted possibilities and are not part of the planned release contract:

- Multiple providers and models
- Multiple source and target languages
- Terminology extraction
- Character and location indexes
- Style-consistency analysis
- Audiobook preparation
- A graphical or web user interface
- An MCP interface
- Additional output formats
