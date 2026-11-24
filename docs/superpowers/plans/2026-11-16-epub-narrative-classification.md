# EPUB Narrative Classification Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover Book 9's prologue and 35 chapters from generic combined and ordinal EPUB 2 NCX labels without changing Book 8 behavior.

**Architecture:** Keep container, OPF, manifest, NCX path resolution, and spine traversal unchanged. Replace identity-only label parsing with one structured parser that returns identity plus an optional title, and use a conservative chapter-stem parser only as fallback.

**Tech Stack:** Python 3.12 standard library, pytest.

**Spec:** `docs/superpowers/specs/2026-11-07-epub-ingestion-design.md`

## Global Constraints

- OPF spine order remains authoritative.
- Unknown frontmatter and backmatter remain excluded.
- EPUB 3 navigation support is out of scope.
- Use synthetic test content; never print real book prose.
- Do not import either real EPUB, execute translation, call a provider, commit, or modify Book 8 artifacts.

## Review Focus

- Exact Book 8 structural labels and paired title labels must retain their current interpretation.
- Combined labels must split identity from a nonempty title suffix.
- Ordinal recognition must require a positive chapter number and a nonempty title.
- Stem fallback must recognize only the requested `chapter` plus digits forms.
- Non-narrative spine entries around the narrative corpus must remain excluded.

---

### Task 1: Structured narrative classification

**Files:**
- Modify: `tests/test_epub_ingestion.py`
- Modify: `src/book_translator/epub_ingestion.py`

**Interfaces:**
- Consumes: NCX labels grouped by resolved manifest path and spine resources in OPF order.
- Produces: structured narrative identity/title classification used by `_discover_units`.

- [ ] Add one synthetic nested-OPF regression covering combined prologue, ordinal chapters 1–35, attached chapter stems, non-narrative neighbors, title recovery, resolved paths, and spine positions.
- [ ] Run the regression alone and confirm it fails because no narrative units are classified.
- [ ] Implement the structured label parser and conservative filename fallback.
- [ ] Run the regression and complete EPUB-ingestion test file and confirm they pass.
- [ ] Run Ruff, focused mypy, Book 8 dry-run, and Book 9 dry-run; inspect structural Book 9 identities/titles without printing prose.
- [ ] Review the final diff against the requirements; do not commit.
