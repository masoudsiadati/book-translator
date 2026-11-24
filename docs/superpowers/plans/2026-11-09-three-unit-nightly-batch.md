# Three-Unit Nightly Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Increase the bounded nightly translation invocation from two narrative units to three while preserving all existing safety behavior.

**Architecture:** Keep the existing nightly coordinator and worker boundary unchanged. Adjust only the coordinator/CLI defaults, the Book 8 scheduler argument, user-facing nightly documentation, and focused orchestration tests.

**Tech Stack:** Python, Typer, pytest, Bash

**Spec:** `docs/superpowers/specs/2026-11-07-nightly-translation-design.md`

## Global Constraints

- Do not execute `run-nightly-translation` or contact `codex-cli`.
- Do not modify `workspace/` or the active run `tr-473bab4ea0f7-001`.
- Concurrency remains one; semantic QA, automatic repair, and provider retry remain disabled.
- `qa_rejected` remains usable translated coverage and is not repaired.
- Preserve the inclusive 22:00 and exclusive 05:00 local-time gate and zero-timeout coordinator lock.
- Run only focused nightly tests. Do not commit.

## Review Focus

- A fourth distinct narrative unit must not start in one invocation.
- Multiple chunks in one selected unit must not consume additional unit budget.
- A usable `qa_rejected` attempt must remain covered and unselected.
- Worker options must keep `workers=1`, repair disabled, and rejected jobs excluded.
- Lock contention must fail before a worker/provider call.

---

### Task 1: Pin the three-unit orchestration contract

**Files:**
- Modify: `tests/test_nightly_translation.py`
- Modify: `src/book_translator/nightly_translation.py`
- Modify: `src/book_translator/cli.py`

**Interfaces:**
- Consumes: `run_nightly_translation(..., max_units=3)` and the existing queue/worker contracts.
- Produces: a CLI and coordinator whose omitted `--max-units` value is three.

- [ ] Add focused tests for the three-unit boundary and safety options without invoking a real provider.
- [ ] Run the selected tests and confirm the new default assertion fails for the expected two-unit behavior.
- [ ] Change the coordinator and CLI defaults and wording to three.
- [ ] Run the focused nightly tests and confirm they pass.

### Task 2: Keep the scheduler and documentation explicit

**Files:**
- Modify: `scripts/run-book8-nightly.sh`
- Modify: `docs/superpowers/specs/2026-11-07-nightly-translation-design.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `run-nightly-translation RUN_ID --max-units 3`.
- Produces: an explicit Book 8 scheduler command and accurate user-facing contract text.

- [ ] Add `--max-units 3` to the scheduler without executing it.
- [ ] Update nightly documentation/changelog from two units to three.
- [ ] Run only the focused nightly tests again and inspect the scoped diff.

No commit is permitted by the task.
