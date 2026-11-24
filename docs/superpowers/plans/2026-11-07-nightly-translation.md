# Bounded Nightly Translation Implementation Plan

Spec: `docs/superpowers/specs/2026-11-07-nightly-translation-design.md`

1. Add failing contract and snapshot tests for EPUB source-document lineage, then extend the
   snapshot models/workflow while preserving legacy PDF manifests.
2. Add failing coordinator tests for local windows, three-unit bounds, ordered resume, usable
   rejected output, lock/checkpoint behavior, failure gaps, and post-coverage draft assembly; then
   implement the generic nightly module and CLI command.
3. Update focused documentation and Book 8's provider allow-list.
4. Derive Book 8 terminology and reference context, validate lineage, freeze the snapshot, create
   one run, and render all prompt packages without provider execution.
5. Run focused tests and static checks, inspect immutable IDs/counts/checkpoint, verify zero
   attempts/provider calls, and audit that Books 5/6/7 have no new changes.

Ruling: execute in the existing user-owned dirty workspace because the EPUB ingestion work being
extended is uncommitted and must not be split or copied; do not commit despite the execution skill's
normal commit checkpoints, because the user explicitly prohibited commits.
