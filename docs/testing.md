# Testing policy

Tests use synthetic content by default, remain deterministic, and do not depend on network access,
machine-specific absolute paths, or the real book PDF. Every feature change runs the complete existing
suite as regression coverage.

## Unit tests

Unit tests cover parsers, selectors, pure transformations, lifecycle rules, counting, hashing, path
generation, validation, deterministic identifiers, and serialization helpers. They should identify
one contract or edge case clearly and avoid unnecessary filesystem setup.

## Integration tests

Integration tests cover CLI orchestration, metadata and artifact publication, atomic writes, skip and
force behavior, recovery, safe publication ordering, and interaction between pipeline stages. They
use temporary workspaces and generated PDFs or text.

## Regression tests

Every proven bug fix includes the smallest test that fails before the fix and passes afterward. Never
delete or weaken a regression merely because an implementation changes; update it only when the
underlying contract intentionally changes and that impact is documented.

## Contract tests

Contract tests verify committed JSON Schemas against Pydantic models, model round trips, field and enum
compatibility, canonical paths, CLI command compatibility, application and engine versions, strict
unknown-field behavior, deterministic output, and governance files that are meaningful to check
mechanically. Documentation tests assert stable links or headings rather than entire prose documents.

## Failure-injection tests

Where practical, inject write failure, metadata publication failure, provider failure, interrupted
processing, invalid hashes, stale artifacts, and partial workspace state. Verify both the immediate
error and the next-run recovery behavior. Provider failure tests remain synthetic and local.

## Why equal paragraph counts prove nothing

`paragraph_count_mismatch` compares two integers, and integers do not carry identity. A translation
that fuses source paragraphs 4 and 5 into one block and breaks source paragraph 9 into two has the
same block count as its source, so the count check passes while paragraphs 5 through 9 are each the
translation of a different source paragraph than the reader is looking at.

This is not a hypothetical. Three chunks of the completed pilot — `011-003`, `032-003`, `040-003` —
carry exactly this shape: one merge and one compensating split, total count preserved, structural
QA green, and the drift reached the published EPUB.

A test that only asserts equal counts therefore proves nothing about alignment, and neither does a
QA engine that only checks them. Two things replace that inference and both need their own tests:

- **Marker identity.** Each source paragraph is sent under an explicit `[[Pnnnn]]` marker and every
  marker must return exactly once, in order. A merge loses a marker, a split duplicates one, a
  reorder breaks the sequence, and each fails deterministically at the provider boundary before any
  translation artifact exists. Tests must cover every one of those shapes individually, and must
  assert that the marker syntax is gone from the accepted text and that the paragraph text itself
  survived byte for byte.
- **Alignment measurement.** Markers cannot see content that moved *between* two correctly marked
  paragraphs, so tests must also cover the ratio band, the adjacent complementary-drift signature,
  and the deliberate quietness of the policy on short paragraphs.

A repair test carries the same burden. Restoring count equality is not evidence that a repair split
the right paragraph, so a repair test asserts what the repaired text says about identity, not that
the counts now match.

## Testing a provider-backed judge

A stage whose answer comes from a model needs its determinism tested where determinism actually
lives, which is everywhere except the model. Task construction, prompt rendering, response
validation, and the acceptance policy are pure and are tested exhaustively against fixtures; the
provider is scripted.

Three classes of test carry most of the weight:

- **Fail-closed parsing.** Every way an answer can be wrong about *identity* gets its own test: an
  unknown paragraph, a missing verdict, a duplicated verdict, an out-of-vocabulary code or
  severity, a contradiction between a verdict and its findings, and malformed output. Each must be
  a provider failure, never a verdict, because a verdict attached to the wrong paragraph is worse
  than no verdict.
- **False-positive control.** The tolerances are contract, not taste, so they are asserted: a
  natural sentence merge, a sentence split, an implicit pronoun, a reordering, and a shorter
  Persian paragraph must all pass. A judge prompt that stops stating a tolerance should fail a
  test.
- **Isolation.** The judge must not reach the translator's role, an earlier verdict, or any
  publication stage. Import-level assertions prove release assembly, release verification, and the
  EPUB builder hold no dependency on it, and byte-level assertions prove an evaluation leaves a run
  unchanged.

Real-model behavior is measured rather than asserted. A bounded, stratified sample is judged
against known cases, repeated judgements measure stability, and batch granularities are compared on
cost and attribution. Those results are evidence for a design decision and are reported, not turned
into a test that would fail on a model's ordinary variation.

## Testing a bounded revision loop

A loop that spends provider calls needs its bounds tested more carefully than its happy path.
The gate suite scripts one transport that answers judge and reviser calls from separate scripts,
so a test can state exactly how many calls a scenario is allowed to cost and assert the count.
The properties that carry the weight: a reviser that always answers well while the judge always
rejects ends in `unresolved` after exactly the budget; a revision that changes a paragraph outside
its window is refused before adoption; a lost, reordered, or out-of-window marker is refused; a
mechanical regression is refused; `INCONCLUSIVE` gets exactly one wider look and never a pass; and
the gate leaves every mechanical evidence file byte-identical.

## Real-data pilot progression

A new major stage does not run across the complete real book immediately. Use this progression:

1. synthetic unit and integration tests;
2. one small controlled real artifact;
3. selected representative chapters with explicit approval;
4. a read-only full-book dry run;
5. a full batch with explicit approval.

Real book prose is never committed as a fixture or printed in terminal output, logs, test failures, or
reports. Hashes, counts, identifiers, and structural warnings are appropriate audit output.

## Testing the literary fidelity firewall

Literary tests keep source and Persian fixtures synthetic and script editor, judge, correction, and
continuity responses independently. Contract tests reject missing, duplicate, reordered, extra, and
unknown paragraph identities; deterministically normalize contradictory changed flags from bytes;
reject free-form reasons, self-certified fidelity, continuity rewrites, and correction attempts outside
the failed identity set. Section tests prove the
whole chapter is preferred, oversized cores tile every identity exactly once, and overlaps are context
only.

Orchestration tests assert the safety invariant from both directions: a retained changed paragraph
has fresh semantic PASS evidence for its exact final hash, while an unchanged paragraph reuses old
evidence only when its bytes match the faithful input. Scripted failures cover marker/terminology
refusal, `REVISE`, `INCONCLUSIVE`, bounded correction, and retry exhaustion; the terminal assertion is
byte equality with the pre-editor paragraph plus `literary_edit_fallback`. Broad edits must not be
rejected by size alone, while suspicious edits must take individual wider-context judgement.

Blind-comparison tests prove the reader document contains only Version A/Version B and the requested
Persian rubric, with the mapping confined to a separately hashed key. Real prose is never printed by
tests or command summaries. Recovery tests carry a completed chapter into a new content-addressed run
and assert that neither the literary provider nor semantic judge is called again.

## Required checks

After production changes, run:

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src tests
```

Also run affected CLI commands, schema synchronization checks, and benchmarks required by
`docs/benchmark.md`.
