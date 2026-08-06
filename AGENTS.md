# Project rules for Codex

For every nontrivial task, read `ARCHITECTURE.md`, `docs/developer-charter.md`, `ROADMAP.md`, and the
documentation for the affected stage. Read `CHANGELOG.md` and `docs/release-process.md` whenever
release behavior or a user-facing capability changes. Follow `docs/coding-standards.md`,
`docs/testing.md`, `docs/review-process.md`, and `docs/benchmark.md` as applicable.

Future prompts may define the feature goal; the developer charter permanently supplies the development
process. Do not skip architecture inspection, design critique, regression testing, or final review.

Durable safety rules:

- Preserve UTF-8, Persian text, immutable extraction sources, and approved artifacts exactly.
- Never print book prose or send it to an external service except through an explicitly approved and
  configured translation provider.
- Use only the canonical `workspace/` layout, models, paths, schemas, hashes, and transition policies.
- Keep stages deterministic where contracted, resumable, idempotent, hash-tracked, and atomically
  published. Metadata never claims missing or mismatched artifacts.
- `--force` never bypasses downstream safety; breaking stored-data changes require an explicit
  documented migration.
- Processing, translation, review, and approval remain distinct lifecycle states; shortcuts to
  approval are prohibited.
- Use synthetic test content unless a controlled real-data pilot is explicitly requested.
- Run the complete tests, Ruff lint and formatting, and strict mypy after code changes. Benchmark
  materially performance-sensitive work.
- Do not create commits, tags, releases, destructive invalidation, or unrelated production features
  without explicit user instruction.
