# Deterministic normalization audit

## 2026-09-08 lowercase-edge possessive-name amendment

Normalization Engine 1.6.0 updates `page-boundary-continuations` to 1.5.0. A second-book audit
reported two canonical page boundaries whose left edge ended on a bare lowercase word and whose right
edge began with a capitalized possessive name word. The audit already classified both as
`nonterminal=true, possessive_continuation=true`, but the 1.4.0 pass only accepted a possessive name
word after a *capitalized* name word (the split-proper-name shape) and otherwise required a lowercase
right edge, so the two boundaries stayed unresolved and blocked translation readiness.

The new predicate requires the last whitespace-delimited token of the left edge to be entirely
alphabetic and lowercase (no trailing punctuation, quotation mark, dash, or ellipsis) and the first
word of the right edge to be a title-case alphabetic stem carrying a possessive suffix. A prose
paragraph never ends on a bare unpunctuated lowercase word, so the left edge proves the clause is
incomplete; the possessive noun phrase completes it. All existing guards stay in front of it:
different canonical pages, multi-line non-verse blocks on both sides, no structural or list block, no
quote-led right block, no unmatched quote-led left block without a prior completed sentence, and no
boundary hyphen. A plain capitalized right edge is still a paragraph boundary.

Across both workspaces the rule changes cleaned bytes for exactly two chapters of the second book,
each losing one logical paragraph while keeping its chunk count; all 57 first-book chapters produce
byte-identical output and were not regenerated, so their 1.5.0 metadata is version-stale only.

## 2026-08-13 interruption and grammatical-continuation amendment

Normalization Engine 1.5.0 updates `page-boundary-continuations` to 1.4.0. The new rules attach a
page-leading em-dash/closing-quote tail only to unmatched dialogue, join an em-dash plus lowercase
clause only to non-dialogue narration, and allow a capitalized complement only after an article that
cannot grammatically terminate the left clause. Quote-led new-speaker blocks remain hard boundaries.
A dangling dash alone is never sufficient. Real-workspace regression checks cover all nine manually
reviewed boundaries without embedding book prose in tests.

## 2026-08-13 capitalized-name and one-line-tail amendment

Normalization Engine 1.4.0 updates `page-boundary-continuations` to 1.3.0. Canonical boundary
inspection found two extraction shapes excluded by the 1.2.0 pass: a multi-line nonterminal block on
page 347 continued on page 348 with the capitalized possessive second word of a proper name, and a
multi-line quote-led block on page 626 continued on page 627 by a one-line lowercase closing tail.

The first extension requires capitalized lexical words on both edges and a possessive suffix on the
following edge. The second requires an unmatched quote-led left block with an earlier complete
sentence, a lowercase one-line right block ending in a dialogue closer, and all existing nonterminal
edge checks. Both require different canonical source pages. Headings, titles, verse, scene separators,
lists and structural one-line blocks, terminal prose, boundary hyphens, quote-led following blocks,
and ordinary separate dialogue remain protected.

A read-only six-chapter plan changes cleaned bytes only for chapters 020 and 042, reducing each by one
logical paragraph while retaining its chunk count. Chapter 020 chunk 002 remains byte-identical with
33 source paragraphs, and chapter 042 chunk 003 remains byte-identical with 18 source paragraphs;
their provider-created translation splits therefore remain visible to structural QA. The Prologue,
001, 033, and 056 cleaned and chunk bytes remain identical, though their normalization metadata is
version-stale. No mutable artifacts or immutable downstream lineage were rewritten.

## 2026-08-10 mixed-dialogue page-boundary amendment

Normalization Engine 1.2.0 updates `page-boundary-continuations` to 1.1.0. A structural audit of a
reported numbered-chapter boundary showed that extraction correctly preserved two PDF text blocks on
successive canonical pages, but the normalization pass rejected the otherwise clear continuation
solely because the left block began with dialogue. The block contained a closed quotation followed by
narration and another quotation; quote-led was therefore not a reliable paragraph-level exclusion.

The deterministic suspicious-boundary rule still requires different marked pages, multi-line
non-verse blocks, a nonterminal and nonhyphenated left edge, and a lowercase right edge. It still
rejects structural blocks, a quote-led right block, and a quote-led left block with no closing dialogue
delimiter. Balanced dialogue before narration no longer blocks a join. Synthetic regressions cover
ordinary page splits, lowercase continuations, lowercase starts after terminal paragraphs, straight
and curly unclosed dialogue, new quote-led dialogue, and the reported mixed-dialogue shape.

A read-only chapter 056 plan changes cleaned SHA-256 from
`98b7f37fb6344cf2092a959e481cb64b695843f5ceabb02496c3d101b612f438` to
`e9a99cc6ee6143577a9309470ef86238cbb12261e1a5cfca2cf7d2544a92e8bf`, reduces logical paragraphs
from 44 to 42, and keeps two chunks while changing both chunk source hashes. Immutable snapshots,
runs, prompt packages, attempts, source bytes, and translations were not modified.

## 2026-08-08 page-boundary regression amendment

Normalization Engine 1.1.0 adds `page-boundary-continuations` 1.0.0 after wrapped-line joining and
before paragraph rendering. Extraction already retained canonical page markers and ordered PDF text
blocks, but version 1.0 rendered every block adjacent to a marker as a paragraph. The new pass joins
only adjacent blocks on different marked pages when both are multi-line non-verse prose, the left edge
has no terminal punctuation or boundary hyphen, and the right edge begins lowercase. One-line
structural blocks, Markdown headings, explicit scene separators, and quote-led dialogue are excluded.

A controlled read-only Prologue dry run found eight qualifying boundaries among 23 page transitions,
including both reported regressions. Paragraph count changed from 144 to 136; non-whitespace character
order was conserved; the two known artificial breaks disappeared; and the generic chunk engine
reconstructed the resulting five-chunk document byte-for-byte. No canonical source, cleaned artifact,
snapshot, run, attempt, or prompt package was written or replaced during the check.

Version 1.0 artifacts are historical rather than silently upgraded. The normalization version and
cleaned hash make them stale; chunk hashes and immutable snapshot manifests carry that staleness
through chunk, snapshot, run, and prompt-package lineage. Regeneration therefore creates new derived
artifacts in that order after downstream safety blockers are explicitly resolved.

Audit date: 2026-08-06. The review covered the frozen architecture, project rules, canonical models
and paths, hashing and atomic writes, lifecycle policy, CLI integration, every normalization module,
tests, and pipeline/recovery documentation. No numbered real chapter was normalized.

## API and architecture result

The public CLI remains `book-translator normalize-chapters` with optional `--chapter`, `--force`,
`--dry-run`, `--show-diff-summary`, and hidden testing override `--normalization-version`. Selectors
accept `prologue`, `0`, decimal numbers with padding, and `chapter-NNN`. Dry runs perform parsing,
normalization, validation, and hashing without writes. The public Python engine API is
`normalize_text(source: str) -> NormalizationResult`; `NORMALIZATION_VERSION` is `1.0`.

The engine order is fixed:

1. `page-markers` 1.0 parses strictly increasing canonical markers into pages, immutable blocks, and
   immutable physical lines. It rejects CR input, text before the first marker, and malformed
   canonical-looking markers. Source order and original block text remain available for audit.
2. `end-of-line-hyphen-repair` 1.0 removes only eligible physical newlines and retains one ASCII
   hyphen. It records page, block, and line provenance.
3. `wrapped-line-joining` 1.0 joins only within one source block, using one space, while preserving
   blocks classified as verse-like and warning on ambiguous short-line structure.
4. `paragraph-rendering` 1.0 renders one Markdown paragraph per source block, one blank line between
   blocks, no outer blank lines, and one final newline.
5. `validation` 1.0 checks nonempty output, block correspondence, marker absence, character and
   punctuation preservation, LF form, and the final newline contract.

Pass input structures are frozen dataclasses and pass modules have stable names and versions. Parsing,
hyphen repair, and joining return structured transformed data and change records. Rendering returns
text and validation returns by success or exception rather than the common `PassResult` shape; this is
an internal interface inconsistency, not a correctness or frozen-contract violation. All passes are
deterministic, independently callable, free of file I/O, external services, AI models, and semantic
rewriting. They do not mutate the supplied source string.

The report is prose-free schema 1.0 YAML with lineage hashes, version, ordered passes, structural
counts, transitions, warnings, timestamp, and bounded structural descriptions. Existing complete
artifacts are now revalidated against deterministic output, metadata counts, report counters,
warnings, paths, identity, status, and lineage before skip. A version mismatch is stale. Partial sets
and hash/count/report inconsistencies block. Forced replacement is limited to normalized artifacts and
is refused when chunks, translations, review files, or `final.md` exist.

Publication generates all data in memory, atomically writes and verifies `cleaned.md`, atomically
writes the report, and atomically writes metadata last. A report/metadata failure leaves diagnostic
evidence without claiming normalized status. Completed earlier chapters remain valid and reruns skip
them. This follows the frozen workspace, hash-lineage, atomicity, and pure lifecycle architecture.

## Proven defects and regression coverage

Two defects were reproduced with minimal synthetic cases and fixed narrowly:

- Consecutive eligible boundaries such as three synthetic hyphenated physical lines repaired only the
  first boundary. The hyphen pass now continues across each eligible boundary. The regression checks
  two repairs, no inserted space, and no subsequent wrapped join.
- Skip validation accepted a tampered metadata word count or report join counter when lineage hashes
  still matched. Existing-artifact validation now compares persisted counts and report structure with
  the deterministic in-memory result. Parameterized regressions cover both corruptions.

No reproduction showed cross-block/page merging, source mutation, dialogue or heading corruption,
dash/apostrophe/quotation loss, hyphen deletion, malformed-marker acceptance, unsafe force behavior,
misleading partial publication, or nondeterministic cleaned output.

## CLI and immutable-artifact regression

`status`, `inspect-pdf`, one-page `extract-pages` to `/tmp`, `discover-toc --dry-run`,
`extract-chapters --dry-run`, and prologue `normalize-chapters --dry-run` all completed successfully.
Signatures remain compatible. Discovery reported 59 TOC entries; extraction skipped all 57 existing
prologue/numbered workspaces; normalization skipped the valid prologue. Status remained 57 extracted,
1 normalized, 0 translated, 0 approved, and 0 failed chunks. Legacy progress was not rewritten.

Before/after SHA-256 values were identical:

| Artifact | SHA-256 |
| --- | --- |
| `workspace/toc.yaml` | `696f23bea9dcad2789d604e33edbef561b2792afee36ea382f0eeecb356e9761` |
| prologue `source.md` | `abdc2f0bd453dd7760ed912410373d13578d2cfab3a2e3bde6c75618788b1264` |
| chapter 001 `source.md` | `8479cc1f39ffb6464715f32137c9b724c7048f59aa4df217e40d7be2128e3dfa` |
| chapter 042 `source.md` | `b4e443f10092f0f6f30d8d1b7fa63c7843c57b679d5b5c000294e83cea340dac` |
| chapter 056 `source.md` | `d94ddc912552c7896eecf3fef003a8de946dae754e8a4aef72b13081da55975c` |
| `state/progress.json` | `40af5a783b33a5b88011cd2d424b79ec9aa87ff95ba21c30b54cf7e63587b7b7` |

The aggregate hash over all 57 chapter metadata paths and bytes remained
`2446a6e92eb7b8dad463eb220572083d1909d6cee8ab4af04917635f872c6257`. Exactly one
`cleaned.md` exists: the prologue pilot.

Final validation completed with 119 tests passing, Ruff lint clean, Ruff formatting clean across 109
files, and strict mypy clean across 39 source/test files. Editable installation also succeeded.

## Synthetic benchmark and complexity

`python scripts/benchmark_normalization.py` generates 25 pages and 500 blocks containing wrapped
prose, dialogue, headings, short lines, verse-like lines, boundary and internal hyphens, em/en dashes,
and canonical markers. Three runs over a 73,240-byte input produced identical output bytes, SHA-256
`7cf6a326108521ec7094f614485930a0b385842bb21581331f79f170f999c0ba`, and counters: 1,200 physical
lines, 500 blocks/paragraphs, 25 markers, 500 wrapped joins, and 100 hyphen repairs. On 2026-08-13,
observed elapsed times were 0.022963, 0.018868, and 0.019267 seconds (0.019267-second median); peak traced Python memory
was 1,750,014 bytes. Timings are observational and have no test threshold.

For normal extracted blocks, time and retained memory scale linearly with source characters, lines,
blocks, and recorded changes. A chapter is represented by the source string, parsed line/block
objects, intermediate block tuples, cleaned text, and change records. Batch preflight retains each
chapter result and its report until execution, so peak memory scales with the whole selected batch,
not only one chapter. The benchmark's roughly 1.75 MB peak for 73 KB input shows Python object and
audit-record overhead but no material chapter-scale problem. Joining uses repeated concatenation
inside a block, giving a theoretical quadratic worst case for one pathological enormous block; real
extraction blocks are bounded and the benchmark does not justify premature optimization.

## Real-prologue and warning audit

The prologue was read-only during this audit. Source and cleaned hashes match metadata and report;
status is `normalized`, version is 1.0, cleaned counts are 9,155 words and 144 paragraphs, and the
report records 841 physical lines, 24 page transitions/removed markers, 692 wrapped joins, 3 hyphen
repairs, and 3 ambiguous blocks. It has exactly one final newline, no CR or canonical marker, and all
tracked straight/curly quotation, straight/curly apostrophe, em-dash, and en-dash counts match the
marker-stripped source.

The warning records contain no snippets:

| Source page | Block | Category | Structural reason | Conservative result |
| ---: | ---: | --- | --- | --- |
| 8 | 10 | `ambiguous_block` | At least two short physical lines, below the verse threshold | Block boundary and characters preserved; lines joined within the block |
| 17 | 60 | `ambiguous_block` | At least two short physical lines, below the verse threshold | Block boundary and characters preserved; lines joined within the block |
| 23 | 97 | `ambiguous_block` | At least two short physical lines, below the verse threshold | Block boundary and characters preserved; lines joined within the block |

## Limitations and recommendation

Normalization intentionally does not remove headers/footers, infer semantic headings or speakers,
dehyphenate words, or classify poetry/epigraphs deeply. Ambiguous blocks preserve characters and block
boundaries but may flatten their internal physical lines; warnings require structural review. Reports
and batch plans retain one record per structural change, and whole-batch preflight has linear memory
cost in the full selection.

Recommendation: **safe for batch normalization with specified limitations**. Preflight, lineage,
atomic publication, deterministic output, and recovery behavior are sound after the two regression
fixes. Review ambiguity warnings after a batch, and do not interpret normalization as semantic cleanup,
translation, validation, review, or approval.
