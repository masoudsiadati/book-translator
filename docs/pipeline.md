# Canonical pipeline

Full-corpus terminology review is one proposal-linked parent operation. Deterministic risk-aware
shards are bounded by estimated prompt tokens and candidate count. Each shard is validated
immediately; resume and targeted retry reuse valid siblings. Only complete exact aggregate coverage
may reach the separate atomic registry application operation.

The ordered pipeline is:

0. Resolve canonical book metadata from local source evidence.
1. Inspect the PDF without emitting book text.
2. Discover chapters from the embedded TOC and persist a normalized TOC.
3. Extract each chapter to immutable `source.md` using inclusive 1-based boundaries.
4. Normalize each source into `cleaned.md` without changing `source.md`.
5. Validate normalized structure, counts, lineage, and hashes.
6. Split hash-current normalized chapters into deterministic generic document chunks.
7. Freeze and validate a content-addressed translation snapshot before external processing.
8. Create a persistent provider-independent queue from a valid frozen snapshot.
9. A future provider executor claims jobs and records provider requests, results, and attempts.
10. Validate translations; failures become flagged work, never approval.
11. Review and correct flagged chunks.
12. Merge only approved, hash-current chunks into chapter `final.md`.
13. Build an RTL EPUB 3 from final workspace chapters and metadata.
14. Validate the EPUB structure and conformance.

Stage 0 is deterministic, provider-free, and repeatable. `resolve-book-metadata` reads embedded PDF
document information plus short lines from the first twelve and last three pages; anything longer
than 120 characters is prose and is discarded before the pure resolution engine sees it. Generic
bibliographic rules resolve the English title, author, series name and volume, edition, publisher,
publication year, and ISBN (checksum-validated). Precedence for bibliographic facts is explicit
operator assertion, then PDF document information, then front/back matter, then configuration; a
placeholder configuration value is treated as absent. `book_id` instead prefers an explicit
non-placeholder configured identifier and otherwise derives `slug(title_en)-<source>-<target>`. The
Persian title is never inferred: it stays blank until a human records it. Every field carries a
provenance of `pdf_metadata`, `front_matter`, `configuration`, `external_research`, or `unresolved`,
with a short evidence locator such as `pdf:docinfo.title` or `pdf:page-918` — never prose. The
command is a dry run unless `--apply` is supplied, and an unchanged resolution is a `skip` that
leaves `workspace/metadata.yaml` byte-identical so snapshot identity does not drift.

`migrate-book-identity` propagates a resolved `book_id` to the current book-scoped registries
(`config/terminology.yaml`, `config/terminology-questions.yaml`, `config/source-anomalies.yaml`). It
is a dry run unless `--apply` is supplied and a no-op `skip` once they agree. Only identity moves:
exactly the addressed `book_id` scalar lines are rewritten in place, preserving indentation, quoting,
folded blocks, and every other byte, and the parsed payload with identity fields removed must hash
identically before and after or the migration is refused. A book-scoped *derived* identifier is
migrated with it — `anomaly_identity` hashes the book ID, so leaving a stored `anomaly_id` behind
would make the registry contradict its own derivation rule. Each migration publishes an immutable
record under `workspace/migrations/book-identity/bim-<24 hex>.yaml` with both identities, per-registry
before/after raw SHA-256, the invariant semantic SHA-256, occurrence and changed-line counts, and
every re-derived identifier. Execution refuses registry drift between planning and publication.

`freeze-translation-resources` publishes the durable content-addressed copies for the current
resource generation without creating a snapshot, which is what lets an operator re-freeze after a
migration while snapshot creation stays a separate authorized step. It is a dry run unless `--apply`
is supplied, reuses byte-identical existing copies, and never overwrites an earlier generation.

Stages 1–5 may access the PDF as required. Chunking and later text processing operate only on workspace
Markdown and metadata. Snapshot preflight streams the configured PDF solely to verify canonical hash
provenance and never extracts text. Future external/translation stages must require a currently valid
frozen translation snapshot ID.

Stage 3 reads boundaries exclusively from the selected workspace's canonical `toc.yaml`
(`workspace/toc.yaml`, or `workspace/books/<book-id>/toc.yaml` under `--book`) and verifies its
PDF hash and page-count provenance before a batch begins. It extracts the prologue, the numbered
chapters, and the epilogue into `000-prologue/`, `NNN/`, and `999-epilogue/`; front matter only
with `--include-front-matter`; appendices, glossaries, and unknown entries never. It preserves ordered PDF text blocks and
page markers with only newline, non-breaking-space, trailing-whitespace, and excessive-blank-line
mechanical normalization; prose cleanup belongs to stage 4.

Stage 4 is deterministic and non-AI. Its fixed pass order is:

1. Parse canonical page markers, pages, extracted blocks, and physical lines; reject malformed
   canonical-looking markers and retain 1-based transitions for reporting.
2. Repair an ASCII hyphen only at a physical line boundary with a nonblank continuation in the same
   block. This occurs before any line flattening.
3. Join visually wrapped prose with one space, only inside one extracted source block. Conserve
   poetry-like short-line blocks.
4. Apply the suspicious page-boundary structural rule. The ordinary case joins adjacent multi-line
   prose blocks only when the left edge is nonterminal and the right edge begins lowercase. It also
   recognizes two narrowly bounded extraction shapes: a capitalized possessive name word after a
   capitalized name word, and a lowercase one-line closing tail after a multi-line quote-led block
   that contains an earlier complete sentence and ends with unmatched dialogue. One-line structural
   blocks otherwise remain protected. Scene separators, Markdown headings, verse-like blocks,
   terminal punctuation, boundary hyphens, quote-led following blocks, and quote-led left blocks with
   no earlier completed sentence prevent the join. All joins require different canonical source
   pages. A balanced quotation before later narration or dialogue does not by itself disqualify an
   otherwise clear continuation. Version 1.5.0 additionally attaches a delayed em-dash/closing-quote
   tail to its unmatched dialogue, joins an em-dash plus lowercase clause to non-dialogue narration,
   and permits a capitalized complement after an article that cannot end the preceding clause.
   A quote-led new speaker remains a hard boundary; a dangling em dash alone never authorizes a join.
   Version 1.6.0 also joins a left edge that ends on a bare lowercase word with no punctuation at
   all to a right edge that begins with a capitalized possessive name word; a terminal, punctuated,
   or quote-closed left edge, a plain capitalized right edge, and a one-line right block still hold.
5. Render logical blocks with exactly one blank line between them and one final newline.
6. Validate hashes, block correspondence, marker removal, characters, punctuation, newlines, and the
   pure `extracted → normalized` lifecycle transition.

This stage does not remove headers or footers, infer heading markup, rewrite semantics, detect
speakers, or perform sophisticated poetry classification. Page-boundary continuation detection is
structural and deterministic rather than semantic. Ambiguous short-line blocks yield warnings.

## Document Chunk Engine

The generic Document Chunk Engine 1.0.0 consumes only canonical `cleaned.md` and matching chapter
metadata. It parses exact blank-line-separated Markdown paragraphs and never classifies semantics,
splits a paragraph, changes paragraph order, or discards a separator.

Planning considers every paragraph boundary that keeps a normal chunk at or below the maximum. An
explicit standalone separator (`***`, `* * *`, `# # #`, or `— — —`) is preferred as the chunk end
when the resulting chunk is between 60% and 140% of target, bounded by the maximum. Otherwise the
closest boundary to target is chosen; ties prefer the larger chunk, then source order. A paragraph
larger than the maximum becomes one warned oversize chunk.

After initial planning, if the final normal chunk is below 60% of target, only the final two chunks are
considered for deterministic rebalance. Whole paragraphs may move when both results stay within the
maximum and the smaller result strictly improves; candidates prefer both chunks above the minimum,
then minimum total distance from target. No semantic or random optimization occurs.

Chunk sources use one blank line between original paragraphs and one final newline. Concatenating the
parsed paragraphs from chunk sources and rerendering by this rule must reproduce `cleaned.md` exactly,
including its SHA-256. Context is derived only from preceding paragraphs, newest first for selection
but restored to source order: complete paragraphs are preferred, then a whitespace-word tail from the
next older paragraph fills the remaining allowance. Context never affects source counts or
reconstruction.

Chunking validates the existing `normalized → validated → chunked` transition path during preflight
and publishes only the final `chunked` state after artifacts pass conservation and hash validation. It
does not advance to translation.

## Terminology consistency

`config/terminology.yaml` is the current deterministic registry. Candidates may exist without a
decision and carry classification, confidence, aliases, provenance, status, and structural occurrence
locations. A canonical decision links to one candidate and records target text, provenance, competing
variants, contextual notes, required status, and lock state.

A competing target variant cannot be a boundary-safe lexical component or contiguous subsequence of
its canonical target. Decision entry enforces this invariant, proposal processing discards invalid
provider-supplied components, and read-only validation defensively ignores historical violations.

Decision precedence is `user > ai > imported > inferred`. Locked user decisions cannot be replaced by
discovery, inference, or import. Context-sensitive candidates are never selected as global exact
terminology.

### Enforcement modes

`required` answers whether a decision is in scope for QA. `enforcement` answers the separate question
of what target evidence proves it was honored, because collapsing the two made every approved
decision mean "the exact canonical string must appear" — right for a name, wrong for a source form
the target language systematically transforms.

| mode | evidence | absence |
| --- | --- | --- |
| `exact` | the canonical target lexeme, allowing only contracted target inflection | `canonical_term_missing` error |
| `morphological` | that, or a derivational stem of the canonical target, or the canonical target of the decision the source form is derived from | `canonical_term_missing` error |
| `contextual` | the same relaxed evidence, but presence is not what decides correctness | `canonical_term_unverified` warning |
| `advisory` | prompt guidance only | no finding |

A decision that records no mode resolves deterministically from facts the registry already holds, so
a frozen terminology resource is re-evaluable under corrected semantics without being rewritten:
an unenforced decision is `advisory`; a locked or user-owned decision is `exact` whatever its
classification, because a human's terminology is an instruction; a context-sensitive candidate is
`contextual`; a `people` classification is `morphological`; everything else is `exact`. Naming,
place, artifact, title, and historical-event terminology therefore keeps byte-exact enforcement.

The derivational relation is computed from the **source** language and only for a single-word
`people` term: "Saldaean" is derived from "Saldaea", so the canonical target of the place is
legitimate evidence for the demonym. Persian renders "the Saldaean army" as "the army of Saldaea"
and "the Cairhienin throne" as "the throne of Cairhien"; before this the demonym was reported as
omitted. The two canonical targets need not be lexically related to each other, which is why the
relation cannot be recovered from the target side alone.

An explicitly recorded mode always wins, so a registry corrects one decision rather than the
validator accumulating exceptions. A locked decision refuses any mode other than `exact`.

### Coordinate ellipsis

Exact enforcement asks for one contiguous canonical lexeme. One source construction defeats that
without any translation defect: a coordination that shares the canonical term's modifier. "High Lord
or Lady" is a single noun phrase with two heads, and Persian writes the shared modifier once —
«لرد یا بانوی اعظم» — so the canonical target «لرد اعظم» survives as an ordered discontinuous
realization.

Canonical identity is therefore also satisfied by a *coordinate-elided* realization, under
conditions that are all derivable from the frozen registry plus the immutable source:

- the authoritative source match is multiword and directly followed by `and` or `or` plus a head;
- substituting that head into the matched term yields the source term of another **approved,
  decided** candidate, so the registry itself attests the second conjunct;
- the decision is neither locked nor user-owned, because a human's terminology is an instruction
  about the written form;
- the canonical target words appear in the translation **in order**, each gap at most three words,
  within one line, and at least one gap contains a target coordinator (`و`, `یا`).

Nothing here accepts reordered modifiers, unbounded insertions, or a coordination the registry
cannot corroborate. Those are bad registry data or a real defect, and a matcher relaxed to absorb
them would trade a visible error for a silent one.

### Occurrence-anchored competing variants

Terminology QA 1.6.0 changed what `competing_variant` claims. Until then the check asked whether a
recorded rival string occurred *anywhere in the chunk*, which is not a statement about any source
occurrence at all. A finished book is full of expressions that look like a rival rendering and are
not one — an alias, a title with a surname, a short name beside a full one, a descriptive reference,
a source-provided gloss, a plural, or simply a different source term whose canonical target is
related — and the chunk-wide check reported every one of them. On the first completed run it
produced nineteen rejected chunks that reduced to twelve terminology decisions, and inspection of
the source context found that all twelve alleged competitors rendered a *different source
expression* than the decision governed.

The finding now requires positive evidence that the variant realizes the same source occurrence the
decision governs. That needs an alignment, and no span-for-span alignment is persisted, so QA uses
the structural correspondence the pipeline already contracts on and nothing weaker: one target block
per source block, which is exactly what `paragraph_count_mismatch` exists to enforce. Two conditions
must hold together, in one aligned block:

- **locality** — the translation block corresponds to a source block that carries an authoritative,
  non-alias occurrence of *this decision's* source term. Alias occurrences are excluded on purpose:
  a decision governs how its own source term is rendered, and an alias is a different source
  expression; and
- **deficit** — that block carries fewer canonical realizations than it has governed occurrences to
  realize, so at least one governed occurrence there is not canonically rendered and the variant is
  a candidate for what replaced it. A coordinate-elided realization counts as fully realized rather
  than being counted word by word.

Without a deficit every governed occurrence in the block is already accounted for canonically, so
whatever the variant renders, it is not one of them. Co-occurrence in the chunk is never evidence on
its own, and nothing is whitelisted by string: the rule is derived from the source and the registry
alone, so it holds for terminology it has never seen.

**Fail closed on undecidable alignment.** When the two sides have different block counts there is no
correspondence to anchor to, and the finding stands exactly as it did before 1.6.0. That is reached
only for a translation whose block structure already differs from its source, which the structural
validator reports as a blocking finding of its own — so the terminology question becomes decidable
once the structural defect is repaired, and never before.

**The change only ever removes findings.** The new rule is the old condition plus a conjunctive
gate, so for every input its finding set is a subset of the pre-1.6.0 one. A translation that
carried no terminology error cannot acquire one, which is what makes the version installable over a
run whose accepted jobs must not regress. Canonical enforcement itself is untouched:
`canonical_term_missing`, `canonical_term_unverified`, enforcement-mode resolution, and locked and
user-owned decisions all behave exactly as before.

`evidenced_competing_variants` is the single source of truth. The validator raises exactly what it
returns and the final-QA analyzer reports `enforceable` from it, so an audit cannot tell a reviewer
that the installed validator would fire on something it would not.

### Attached pronominal clitics

Terminology QA 1.7.0 corrects what an *exact* Persian canonical lexeme may carry. Persian writes a
possessive or object pronoun onto the noun it belongs to: "her <canonical>" is the canonical followed
directly by `ش`, and "their <canonical>" by `شان`. Exact enforcement matched the canonical only
bare, with `ی`, or with a ZWNJ-separated inflection, so a locked canonical written with an attached
clitic — the ordinary and correct Persian form — was reported as `canonical_term_missing`. The first
Book 6 worker pool rejected a correct translation of a locked organization name that way.

The contracted inflection set now also admits the six attached pronominal clitics `م`, `ت`, `ش`,
`مان`, `تان`, `شان` directly after the canonical. It is a closed list, not a relaxation into
substring matching: the trailing word boundary still applies after the clitic, so a longer token
that merely begins with the canonical plus a clitic-shaped letter (`<canonical>شگاه`,
`<canonical>مانند`) is a different word, and the leading boundary still refuses the canonical as the
tail of a longer token. The same clitics after a ZWNJ were already accepted, because a ZWNJ is a
non-word boundary. The rule is generic and Persian-only; no term is special-cased, and other target
languages are unchanged. Because the set is shared, counting (occurrence-anchored variant evidence),
coordinate-elided matching, the final-QA analyzer, and the identity test that stops a variant spelled
as the canonical plus a clitic from counting as a rival rendering all agree on it.

**What can move, and what did.** For the canonical the change is monotone: a realization can only
be *added*, so `canonical_term_missing` can only clear and a block's canonical count can only rise.
The shared set also recognizes a recorded *rival* variant written with a clitic, so a genuine
`competing_variant` that the bare-only matcher missed can now be found — the one direction in which
a verdict may tighten, and a correct one. Checked against every locally recorded acceptance whose
inputs still hash: 396 evidence records, 9 rejections clear (one on Book 6, eight on Book 5), no
finding is added anywhere, and nothing accepted regresses. The minor bump is what makes those
rejections re-evaluable through `reevaluate-translation-job`; no historical evidence is rewritten.

### Explicitly authorized target forms

Terminology QA 1.8.0 changes what *exact* enforcement means for a decision that records
`allowed_target_forms`: at least one explicitly authorized realization — the preferred `target_term`
or a listed form — must occur, where it used to be the target literal alone. Book 6 rejected a
semantically correct chunk because a participial source term governed by a locked participle target
was rendered, grammatically, as the verbal noun of the same concept.

Each authorized form is matched with exactly the lexical boundaries and the contracted Persian
inflections (including the 1.7.0 attached clitics) a lone canonical gets, so authorizing a form widens
the canonical by that form and nothing else: a shared word, a longer token, a paraphrase, or an
unlisted derivation is still `canonical_term_missing`. Morphological and contextual enforcement apply
their usual matcher to every authorized form. One shared rule set serves presence, occurrence
counting (the realization deficit behind `competing_variant`), competing-variant enforceability (a
variant inside or carrying the identity of an authorized form cannot evidence a rival), and the
final-QA analyzer. The forms are governed data: `set-terminology --target-form` and
`set-series-terminology --target-form` record them, `import-series-terminology-policy` carries them
from user registry decisions, and `derive-book-terminology` projects them; no form is inferred.

**What can move.** For a decision without forms nothing changes — same pattern, same counts, same
bytes. Only a registry that records forms can produce a different verdict, and only toward
acceptance. No existing registry or policy carries the field, so no recorded evidence changes; a
frozen snapshot keeps the registry it froze, and a run's terminology is updated by a new snapshot
plus `reuse-accepted-translations`, never by editing the run.

### Indefinite marker after a long vowel

Terminology QA 1.9.0 corrects one more written form of an *exact* Persian canonical. The indefinite
marker ("a <canonical>") is the attached `ی` after a consonant, which was already contracted. After a
stem ending in the long vowel `ا` or `و` Persian writes a hiatus glide first, so the same marker is
`یی`. Exact enforcement allowed only one attached `ی`, found the second `ی` where it required a word
boundary, and reported `canonical_term_missing`. Book 6 rejected a correct chunk that way: a locked
occupational title ending in `ا`, written with the indefinite marker.

This is not an authorized-target-forms case — the lexical rendering is exactly the canonical, carrying
ordinary grammar — so no form is recorded and no term is special-cased. The shared inflection set
gains one alternative, `یی`, guarded by a one-letter lookbehind that admits it only when the canonical
itself ends in `ا` or `و`. The indefinite allomorphs of the other stem shapes are already covered: a
consonant-final stem takes the single attached `ی`, and a stem ending in `ه` or `ی` takes `‌ای` behind
a ZWNJ, which is a non-word boundary (as are `‌یی` and a ZWNJ plural followed by the marker, `‌هایی`).

The rule stays bounded:

- the trailing word boundary still applies after `یی`, so `<canonical>ییگاه`, `<canonical>ییی`, and
  any other letters continuing the token are a different word;
- the leading boundary still refuses the canonical inside or at the tail of a longer token;
- `یی` after a consonant-, `ه`-, or `ی`-final canonical is not accepted;
- sequences that are not one contracted form — attached plural `ها` or `یان`, `یها`, `یین` — are not
  accepted;
- the older hamza spelling `ئی`, Arabic Yeh `يي`, and a `ه`-final stem with the marker attached without
  its ZWNJ are not accepted;
- markers are not chained: one optional contracted suffix follows the canonical, as before.

The written form cannot distinguish the indefinite marker from other suffixed `ی` (ezafe, a relational
adjective, an abstract noun). That was already true of the single `ی` after a consonant; 1.9.0 gives a
vowel-final canonical the same written-form parity and nothing more. Morphological and contextual
enforcement, authorized target forms, occurrence counting, coordinate-elided matching, and the
final-QA analyzer share the set and move together. The persisted terminology schema is unchanged.

**What can move, and what did.** For the canonical the change is monotone in the same sense as 1.7.0:
a realization can only be added, so `canonical_term_missing` can only clear; a recorded rival variant
written with `یی` can now be found too. Checked against every locally recorded acceptance whose
inputs still hash: 409 evidence records, 4 rejections clear (one on Book 6, three on Book 5), no
finding is added anywhere, and nothing accepted regresses. The version bump is what makes the Book 6
rejection re-evaluable through `reevaluate-translation-job`; no historical evidence is rewritten.

### Grammatical realizations and inflectional aliases

Terminology QA 1.10.0 corrects two matcher defects, one on each side.

**Target side.** A canonical target is a lexeme, and Persian writes a lexeme with regular morphology
that the contracted inflections above never covered. `realization_pattern` in
`terminology/lexical.py` is the one builder for "this authorized form is present". Exact and
morphological enforcement, occurrence counting, and the final-QA analyzer all use it. It adds two
closed nominal rules to the contracted inflections:

- the attached plural: `ان` after a consonant or `ی`, `یان` after `ا`/`و`, optionally followed by
  the ezafe/indefinite `ی` or a pronominal clitic. A heh-final stem changes its own letter before
  the suffix (`گان`) and is deliberately not recognized;
- a multi-word phrase takes its plural on the head (`فرزند نور` → `فرزندان نور`), and may carry a
  written ezafe mark (kasra, or hamza above) after any non-final word. A plural on the final word of
  an ezafe phrase is not Persian and stays rejected.

Terminology QA 1.11.0 adds two more rules, by operator decision:

- an authorized compound verb (`<element><sep>شدن/شده/کردن/کرده`) conjugates. The same element
  followed by any form of that light verb or its transitive/intransitive partner is the compound,
  including the `می`, `ن` and `ب` prefixes and an object clitic on the element: "was gentled" is
  `خنثی شد`, "gentled him" is `خنثی‌اش کرد`. The separators allowed are exactly those the
  decision's own forms use, so ZWNJ and space are still never normalized into each other. The
  element alone, or with any other verb (`خنثی بود`, `خنثی ماند`), is not the compound, and
  neither is a derivation with its own stem change (`خنثی‌شدگان`);
- a plural canonical has a singular. It is never guessed by stripping letters, because that turns
  names into other words (`ساییدین` → `سایید`). It is admitted only where the decision itself
  records that exact spelling as the canonical minus one plural suffix: `مطرود` for `مطرودین`.
  That spelling then carries the contracted inflections (`مطرودی`, `مطرودش`) and is no longer
  enforceable as a rival. A different plural (`مطرودان`) stays a rival.

A realization always begins with the authorized spelling, so an unrelated synonym never satisfies a
decision. Competing variants
are still matched with the contracted `lexeme_pattern` only, so rival detection does not widen. A
recorded variant that is itself a realization of the canonical is refused as a rival, for the same
reason a plural of the canonical already was.

**Source side.** `governing_source_matches` in `terminology/registry.py` decides which source
occurrences bind a decision: the source term and its English inflections (`inflectional_key`: case,
one leading article, the possessive, the plural, `man`/`men`). It backs
`canonically_matched_decisions`, the validator's per-block occurrence counts, and the final-QA
analyzer. `Warders` now governs `Warder`. `Gaidin`, `Red sisters`, `her old nurse`, and
`the Breaking` do not, because each is a different source expression, and a faithful rendering of
one (`دایهٔ پیرش` for "her old nurse") is not a violation of the name it refers to. Such aliases
still reach the prompt through `applicable_decisions`, as guidance. Enforced selection is always a
subset of prompted selection.

Participles and gerunds are separate decisions, so `stilled` never governs `stilling`. Ordinary
English uses of a lowercase alias (`stilled her hands`) are still governed wherever a registry
records that alias. That is a registry decision, not a matcher defect, and it reaches a frozen run
only through a new snapshot plus `reuse-accepted-translations`.

**What moved.** Across 661 locally verifiable acceptance records under 1.11.0:
- 42 rejections clear: gentled/Stilled conjugations, the Forsaken singular, gleeman plurals,
  Whitecloak through the group plural, and Book 5 `menagerie`/`foxhead`/`Wise One`;
- 1 historical Book 5 record newly rejects: it writes the plural `ها` joined without a ZWNJ,
  which the policy pinned since 1.7.0 still refuses.

Enforcing every prompted alias instead would have added 88 rejections, almost all on Book 5.

**Re-evaluating a carried-forward draft (QA Re-evaluation 1.1.0).** `reevaluate-translation-job
RUN JOB --attempt N` now also accepts a job whose standing verdict is a carry-forward, with `N` the
carry-forward number. It judges the carried bytes through the carry-forward record: the record, the
carry-forward acceptance, and the translated bytes are each re-hashed and must name one another.
It publishes an ordinary re-evaluation that names that acceptance as what it re-judged, and it
writes nothing else. The translation, the carry-forward record, and the historical verdict keep
their bytes, and provenance still resolves to the carried source attempt.

The job contract admits `qa_reevaluation` as the effective origin of a carried-forward job once it
has a re-evaluation; the carry-forward still roots the lineage. A carried draft that the new QA
still rejects stays repairable: the repair boundary resolves the rejection through the same
carry-forward and answers the current findings.

### Exact-enforcement audit

`audit-terminology-exact-enforcement` is read-only and changes no QA behavior. It reports which
exactly enforced decisions rest on a source form that cannot support a fixed target lexeme, so a
false positive traced to bad registry data is fixed by remediating the registry rather than by
loosening the matcher:

| risk | meaning |
| --- | --- |
| `containment_inconsistency` | the canonical target is absent from the longer canonical term the source form is contained in, modulo ezafe, ZWNJ, hyphenation, and a plural `ی` — the two spellings cannot both be honored |
| `verb_phrase` | the source form is a clause headed by a finite verb, so no fixed target lexeme exists |
| `descriptive_phrase` | the source form is an open description rather than a name |
| `coordinated_source` | the source form coordinates two heads the target may write with one shared modifier |
| `source_fragment` | the source form is a fragment of a longer canonical term and may belong to it as an alias |
| `composite_of_decided_terms` | the source form composes canonical terms already decided independently |

A `containment_inconsistency` is a registry contradiction and never a translation defect: no
translation can satisfy both spellings at once.

### Provisional decisions and open questions

Ownership, settledness, and lock state are three independent facts. `decision_source` records who
owns a decision, `decision_state` records whether it is `confirmed` or `provisional`, and `locked`
records whether it is protected from replacement. `user + confirmed + locked`,
`ai + confirmed + unlocked`, and `ai + provisional + unlocked` are all normal. A provisional decision
must never be locked, and decisions written before `decision_state` existed load as `confirmed`.

A **provisional** decision is a working translation choice made so an unresolved meaning does not
block translation — not a factual claim about the source. It is enforced exactly like a confirmed
decision, because a book drifting term by term while a question is open is worse than committing to
one form and revising it later. Every provisional decision must carry an open question; a provisional
decision with nothing recorded about it fails readiness.

`config/terminology-questions.yaml` is the durable Open Question Registry. A question's identity is
`(candidate, category)`, so rewording it or revising its provisional target never creates a duplicate.
Categories are general (`kinship_side`, `gender`, `identity`, `title_meaning`, `transliteration`,
`world_term_semantics`, `proper_noun_classification`, `other`) and no term is ever special-cased.
Each question records status, whether it is blocking, evidence status, related chapters and chunks,
evidence provenance, and its eventual resolution.

Research follows current book → web → other volumes, and each channel records its own state
(`not_checked`, `checked_inconclusive`, `checked_resolved`, `unavailable`). Evidence provenance
distinguishes the current-book corpus, web, other volumes, and model prior knowledge. Web and
other-volume research are optional and capability-dependent: network access is never required for
translation readiness.

A **non-blocking** open question does not stop snapshot creation, prompt rendering, provider
execution, QA, or acceptance. Only an explicitly blocking question does. Readiness reporting keeps
confirmed and provisional decisions, and blocking and non-blocking questions, separately visible.

Prompt packages mark provisional state, name the unresolved question, and instruct the provider to use
the provisional canonical form consistently rather than alternate or invent evidence. Terminology QA
enforces the provisional target and records `provisional_decision_enforced` as a warning; an open
semantic question never fails QA on its own.

Escalation accounting derives from current registry, question, and review state. Historical review
runs remain immutable historical facts, but a resolved term is never reported as unresolved.

### Rejection and correction

Rejecting a candidate is an explicit recorded state with a required reason, not the absence of a
decision. Rejected candidates are never enforced.

Ordinary decision entry refuses to lower provenance or overwrite a lock, which is what protects a
human choice from an automated pass. When the stored provenance is itself wrong, an explicit
correction operation rewrites it and publishes a durable record preserving the superseded target,
ownership, state, and lock verbatim under `workspace/terminology-corrections/`.

Changing a decision later is scoped by read-only impact analysis, which reports the chunks that
contain the source form, the snapshots that froze the affected decision, and the translations that
may need re-evaluation. It never mutates a snapshot, job, or translation.

The analysis flow is:

```text
frozen source → discovery run → reconciled candidates → proposal run → canonical review run
→ explicit acceptance policy → canonical registry → new snapshot → translation → terminology QA
```

Discovery and proposal providers return strict structured contracts rather than prose. Discovery never
creates decisions. Proposals always have `decision_source: ai` and `locked: false`. Reconciliation
merges exact source-term duplicates, aliases, and structural occurrences; maximum confidence and a
stable confidence/type ordering select the primary classification while every alternate
classification remains recorded as conflict evidence.
Every reported primary term and observed alias must independently occur in selected frozen source.
Evidence matching permits only Unicode compatibility, typographic apostrophe/dash, and whitespace
equivalence with exact lexical boundaries. The provider must not fabricate canonicalized,
singularized, pluralized, reconstructed, or translated source evidence.

Acceptance policy is configured per book as `manual`, `high-confidence`, or `automatic`, with a
configurable inclusive confidence threshold. Manual retains proposals only in immutable run output.
High-confidence accepts at or above the threshold. Automatic accepts every structurally valid proposal
unless precedence or conflict blocks it. Registry application is always a distinct CLI operation.
Both policies additionally require a safe global scope derived from classification, context-sensitive
state, evidence confidence, rejection state, and classification conflicts. Provider proposal
confidence alone never makes an ambiguous ordinary term globally canonical.

Proposal planning combines canonical registry decisions and the legacy glossary without migrating
either. Precedence is locked user registry, user registry, other registry decisions, legacy glossary,
then AI. Exact matches—including narrowly isolated source-language variants—produce
`reuse_existing` records and are omitted from provider input. A larger candidate containing a
canonical source term at lexical boundaries must preserve the target lexical identity; natural
target-language morphology remains permitted. Persian compound checks include a bounded attached
`ان` plural on a canonical component; this does not enable fuzzy or substring matching. Conflicting proposals are retained as blocked audit
records and count as conflicts, so no acceptance policy can apply them.

Before a successful future proposal run is published, each AI-returned target passes through the
versioned target-language style adapter. For Persian, adapter `fa-canonical-target/1.0.0` removes only
optional fatha, kasra, and damma marks disallowed by the style guide. It does not change letters,
ZWNJ, spacing, word form, morphology, or meaning. The normalized value becomes the proposal's
canonical target and identity input; a changed provider value is retained separately for audit. The
style-resource hash and adapter version are material to proposal run identity. Exact canonical reuse
from the registry or legacy glossary bypasses this transformation, so stored authority is preserved.

Canonicalization safety answers whether a candidate may be frozen globally; canonical review answers
whether its target is good enough to freeze. Review supplies selected evidence, aliases, confidence
values, relevant constraints, languages, and the hash-pinned target style guide. Structured dimensions
cover fidelity, naturalness, transliteration, consistency, register, morphology, and ambiguity.
Dispositions are `accept`, `revise`, `reject`, and `defer`; reuse-existing records normally bypass AI
review. With `terminology_review_required: true`, application requires an explicitly named review run.
`accept` means the supplied canonical lexical target is suitable. `revise` requires a non-empty,
genuinely different canonical lexical target. Equality is exact after the same narrow target-style
adapter: there is no fuzzy equality, stemming, morphology folding, or semantic equivalence. A
style-only Persian short-vowel difference is therefore not a semantic revision; revised AI targets
are deterministically normalized and retain changed provider bytes as evidence. `reject` means do not
canonicalize, and `defer` means evidence is insufficient.

Later chapters may create incremental discovery runs. Existing canonical decisions are reused and
excluded from proposal requests; new candidates and accepted decisions change the current registry
hash. Earlier snapshots and translations remain immutable history, and a new snapshot is required.

New snapshots include the registry as a hashed resource and retain its exact bytes under
`workspace/resources/terminology/<sha256>.yaml`. The manifest keeps the mutable canonical path for
current-lineage comparison and the immutable content-addressed path for historical rendering and QA.
Older manifests without the optional frozen path remain readable. Rendering filters approved canonical
decisions against complete source-term boundaries in the chunk and context. Prompts permit natural
grammatical inflection while requiring lexical identity. Read-only validation reports missing
canonical forms and configured competing variants without correcting prose.

Aliases remain source-only discovery and prompt-relevance evidence. Translation QA enforces a
decision's canonical target and competing variants only when the full canonical source term has an
authoritative source occurrence; an alias-only occurrence cannot require the full target because the
registry has no target-alias mapping. Source matching remains exact and boundary-safe. All candidate
canonical terms and aliases are collected, then overlapping spans are resolved longest first, with a
canonical source term winning an equal-length tie over an alias and stable identity ordering breaking
otherwise equal ties. The retained spans are returned in source order. Thus an alias embedded in a
full phrase is not counted independently, while separate alias and full-term occurrences remain
distinct. Persian target morphology and competing-variant lexical rules are unchanged.

Persian canonical QA permits only the exact lexeme, directly attached indefinite `ی` (written `یی`
after a canonical ending in `ا` or `و`), a directly attached pronominal clitic `م`, `ت`, `ش`, `مان`,
`تان`, or `شان`, or a ZWNJ plus one enumerated suffix: `ها`, `های`, `ام`, `ات`, `اش`, `مان`, `تان`,
`شان`, `ای`, `تر`, or `ترین`. The suffix must end at a lexical boundary. Other languages retain exact boundary-safe matching. There
is no fuzzy matching, stemming, edit distance, semantic equivalence, or unrestricted substring
matching. Required demonyms remain required unless explicitly modeled as context-sensitive; a related
place-name root does not satisfy the canonical decision.

Separate Translation QA compares paragraph counts and reports conservative mechanical signals.
Generic checks cover trailing hard-break spaces, tabs, replacement characters, and doubled ZWNJ.
Target-specific checks are isolated; Persian checks report Arabic Yeh/Kaf and one strictly bounded
conjunction-spacing signal without attempting grammar correction.

Current CLI boundaries are:

```bash
book-translator list-terminology
book-translator add-terminology-candidate SOURCE --type TYPE --confidence 0.8
book-translator set-terminology SOURCE TARGET --type TYPE --decision-source user --locked
book-translator validate-translation-qa --source SOURCE.md --translation TRANSLATION.md
book-translator discover-terminology SNAPSHOT --chapter 001 --chunk 001-001 --provider fake --dry-run
book-translator discover-terminology --current-ready --provider codex-cli --dry-run
book-translator propose-terminology-decisions DISCOVERY_RUN --provider fake --dry-run
book-translator apply-terminology-proposals PROPOSAL_RUN --policy high-confidence --confidence-threshold 0.9
book-translator list-terminology-analysis-runs
book-translator show-terminology-analysis-run RUN_ID
book-translator inspect-terminology-request RUN_ID
book-translator audit-terminology-proposal-run RUN_ID
book-translator review-terminology-proposals PROPOSAL_RUN --provider fake --dry-run
book-translator audit-terminology-review-run REVIEW_RUN
book-translator apply-terminology-proposals PROPOSAL_RUN --review-run REVIEW_RUN --dry-run
book-translator discover-terminology SNAPSHOT ... --retry-of FAILED_OR_AMBIGUOUS_RUN --dry-run
.venv/bin/python scripts/reconcile_terminology_review.py REVIEW_RUN
```

The reconciliation script is read-only and emits deterministic JSON. It proves duplicate-free exact
coverage of proposal and reviewed IDs, separates exact authoritative reuse from reviewed outcomes,
and reports candidate/decision arithmetic plus any normalized-candidate bookkeeping mismatch.

`--current-ready` is the terminology-only full-corpus boundary. It requires every audited current
chapter to be translation-ready, hashes all canonical chunk lineage plus the Source Anomaly Registry,
and does not create or impersonate a translation snapshot. Registered malformed spans are removed
only from discovery provider input. Full-corpus discovery, proposal, and review use bounded provider
batches; complete validated output is required. Unsupported discovery claims are discarded with a
count, while invalid no-op review revisions are conservatively deferred for human review.

The add command records discovery without deciding a translation. The set command approves the linked
candidate and establishes the canonical decision. Validation prints only aggregate finding metadata.

## Lifecycle

Normal chapter flow is `discovered → extracted → normalized → validated → chunked → translating
→ translated → review_required → reviewed → approved`. Normal chunk flow is `pending →
translating → translated → review_required → reviewed → approved`. Translation completion does
not imply approval. Validation may move a translated chunk to `validation_failed`, then to
`review_required`.

Active processing states may move to `failed`. Explicit retries allow chapter `failed →
extracted` or `failed → normalized`, and chunk `failed → pending`. A reviewed artifact may return
to `review_required`. Idempotent same-state checks are accepted. Direct `translated → approved`,
`pending → approved`, `extracted → approved`, and all other skipped prerequisite transitions are
forbidden. Approved is terminal until a future explicit invalidation operation is defined.

The exact pure policy is `src/book_translator/status_transitions.py`. Status changes never delete
artifacts. Chapter `final.md` cannot be generated if any selected chunk is unapproved.

## Mutable regeneration boundary

`inspect-artifact-staleness` is read-only. For each explicitly selected chapter it verifies canonical
TOC/source identity and hashes, validates the internally complete stored normalization and chunk
generations, computes the current deterministic normalization and chunk plans in memory, compares
content hashes and versions, counts immutable downstream snapshots/runs/packages/attempts, and reports
the next required layer. Prompt, glossary, and style-guide drift affects snapshot currency, not
normalization or chunk content.

`inspect-normalization-changes --chapter SELECTOR` is the prose-bearing, operator-facing companion
for authorizing stale normalization replacement. It consumes the same complete regeneration plan,
compares materialized `cleaned.md` with the planner's exact in-memory candidate bytes, and renders
only changed paragraph regions with semantic classification, normalization pass/rule, and canonical
page/block provenance. It also reports source/context chunk-hash changes. `--json` emits the same
deterministic inspection structure. The command never stages or publishes artifacts, and the planned
SHA is computed from the exact byte string later written by `regenerate-artifacts --apply`.

`regenerate-artifacts` performs the same plan and is a dry run unless `--apply` is supplied. Apply is
permitted only when `source.md` is current, existing mutable artifacts are internally coherent, all
chunks are pending without local translation/review/final evidence, and no recovery backup exists. A
normalization-version change replaces normalization metadata/report even when cleaned bytes happen to
remain identical. Chunks are replaced only when the planned cleaned hash, chunk engine/configuration,
or planned chunk hashes differ. A glossary/style/prompt-only change therefore performs no mutable
write and points directly to creation of a new snapshot.

Each changed chapter is fully staged and hash-validated first. Publication moves chapter metadata out
of the way before swapping mutable artifacts and publishes new metadata last. Expected exceptions
roll back every swapped target byte-for-byte; an abrupt process or host failure can leave fixed hidden
backup evidence and missing metadata, which blocks automation until recovery is diagnosed. Batch
preflight completes before the first chapter, while successfully completed earlier chapters remain a
valid generation if a later chapter fails.

## Series terminology and cross-book reference

The series terminology layer (`docs/terminology-architecture-v2.md`) sits beside the per-book
Terminology Consistency Engine and feeds it. Its inputs and outputs:

| artifact | owner | role |
| --- | --- | --- |
| `config/series/<series>/reference-corpus.yaml` | project, versioned | pins the external retriever script and every corpus database by SHA-256, lists excluded legacy databases, fixes retrieval limits; anomalous and reference-only material can never be enabled |
| `config/series/<series>/terminology-policy.yaml` | project, versioned, governed | the series decisions: `LOCKED`, `STRONG_REFERENCE`, `OBSERVED_VARIANTS`, each with provenance, observed variants, and recorded conflicts |
| `workspace/books/<book>/terminology/registry.yaml` | derived | the book's own occurrence registry projected from the policy; frozen as the snapshot's `terminology` resource |
| `workspace/books/<book>/reference-context/index.yaml` | derived | chunk → immutable reference package, with the retrieval identity it came from |
| `workspace/books/<book>/resources/reference-context/<sha256>.yaml` | immutable | one chunk's clean retrieved evidence with provenance; quotes reference prose and is gitignored |

`import-series-terminology-policy` builds the policy. Explicit sources come first: user-owned
decisions of an explicit registry and the project's mandatory glossary become `LOCKED` with
`explicit_user` / `explicit_project` provenance, a user decision outranking a glossary entry. Corpus
concepts and entity forms from every configured corpus are merged by source term (typographic
apostrophes folded, case-variant spellings kept as aliases). Promotion rule
`cross-source-agreement/1.0.0` promotes a form to `STRONG_REFERENCE` only when at least two
independent clean sources agree (a source is one book's narrative or one book's printed glossary),
at least one of them is narrative prose, and no rival clean form reaches the larger of two
observations and a quarter of the leader's support; a promoted compound must also contain the locked
rendering of every locked component it contains. Everything else is `OBSERVED_VARIANTS`. A prior
volume's non-user decisions are imported as `prior_volume` evidence, never as decisions. A re-import
preserves decisions recorded with `set-series-terminology`.

Once a snapshot has frozen a policy, the policy is edited through a governed amendment rather than
by hand. `amend-series-terminology REQUEST --book BOOK [--apply]` reads a
`SeriesPolicyAmendmentRequest` (series ID, a strictly newer `resulting_policy_version`, a rationale,
and one complete intended decision per existing term: force, target, `allowed_target_forms`,
aliases). The whole batch is refused and nothing is written if any item names a term the policy does
not hold (an amendment never creates a term), or would override an `explicit_user` decision: a
different target or force, a withdrawn authorized form or alias, or an alias another user decision
owns. Otherwise every item is applied through the explicit-user edit (variants, conflicts, and notes
preserved; provenance `explicit_user`, origin `amend-series-terminology:<spa-id>`), the policy is
revalidated as a whole, and an immutable `SeriesPolicyAmendmentRecord` is published to
`workspace/series-terminology-amendments/spa-<24hex>.yaml` *before* the policy file is atomically
replaced. The record carries the policy file SHA-256 and semantic identity before and after, both
versions, the request and its hash, and per-term set differences (aliases and forms added/removed,
force, target, provenance). Amendment identity hashes the engine version, the exact policy bytes it
applies to, and the request, so a rerun after an interruption between the two writes completes the
same amendment and an applied one is skipped. A chain of records whose hashes connect two frozen
policies is what cross-snapshot migration accepts as a governed terminology move.

Some spellings are both a story term and an ordinary word. A decision cannot be scoped by sense, and
widening or narrowing it is wrong in one sense or the other. The user instead adjudicates single
occurrences: `exempt-terminology-occurrence CHUNK --spelling S --occurrence N --rationale R --book
BOOK [--apply]` writes a `TerminologyOccurrenceExemption` to
`workspace/books/<book>/terminology/occurrence-exemptions.yaml`. The record contains no prose. It
binds:

- the chunk source SHA-256;
- the character span and the 1-based ordinal of the spelling's boundary-safe matches;
- the containing paragraph's number and SHA-256;
- the registry candidate, the decision ID and a digest of the whole decision;
- the series concept that owns the spelling.

A snapshot freezes the file as the optional `terminology_exemptions` resource and checks every
record against its chunk before publication. Every consumer resolves the frozen copy for exactly one
chunk and re-proves each fact, failing closed on any mismatch. That covers prompt rendering (the
registry tier and the series-policy tier), acceptance QA in the worker, reuse, historical
re-evaluation, `reevaluate-translation-job`, structural repair, final QA, and the semantic gate's
per-paragraph judge/reviser constraints. An exempt span selects no decision. Every other occurrence
of the spelling, including the same words inside another chunk's context window, stays governed.
Literary editing, translation comparison and the read-only semantic coverage audit do not load
exemptions yet.

A historical output whose request moved only because exemptions reach its job is admitted by
historical re-evaluation under the `occurrence_exemption_drift` tier (policy 1.2.0). The boundary
renders the new job again with the exemptions withheld, and that counterfactual user message must
equal the historical one byte for byte. Otherwise the job is refused.

`derive-book-terminology` projects the policy over the book's hash-verified chunks. The spelling of
the term that actually occurs in the book becomes the candidate's exact source term (the corpora and
the two books disagree on apostrophe glyphs); other attested spellings become aliases; a spelling
that names another concept belongs to that concept alone. `LOCKED` projects to an exact, required
decision (user-locked for `explicit_user`) whose clean narrative rivals are competing variants;
`STRONG_REFERENCE` projects to a contextual, required decision with no competing variants;
`OBSERVED_VARIANTS` projects to an undecided candidate. Terminology QA therefore reports a locked
violation as blocking `canonical_term_missing` / `competing_variant`, a strong-reference divergence as
the non-blocking `canonical_term_unverified` warning, and nothing for observed variants.

A run created under a semantic gate policy is releasable only when every job holds a semantic
PASS, and "not judged" is never read as a pass. Publishing without that coverage is a decision a
person takes on the record: `authorize-release-waiver RUN --reason R [--apply]` writes a
content-addressed `ReleaseSemanticWaiver` to `workspace/release-waivers/rw-<24hex>.yaml` naming the
run, the snapshot manifest, the gate policy version and the exact semantic and mechanical counts it
was authorized against. `export-translation-run --semantic-waiver ID` re-derives it against the
live run and fails closed on any mismatch — another run, another snapshot, another policy, or
counts that moved since. It admits only jobs with no verdict at all; a job the judge refused is
repaired, never waived. Nothing writes `semantic_status`, so the work stays unjudged, and the
release manifest and translation report say so in their own bytes
(`semantic_coverage: waived_not_judged`, the unjudged count, the reason, `decided_by: user`).

A published chapter title is corrected with `revise-chapter-titles PARENT --set CHAPTER=TITLE
--reason R [--apply]`, which contacts no provider. The revision carries every other title from its
parent byte for byte, is addressed by the parent plus its overrides, and is published beside the
parent, which is never rewritten. Each title records what it is: a carried provider answer
(`inherited_title`) or a person's replacement (`operator_override`, which keeps the superseded
title, the reason and who decided it). Title QA runs over the whole resulting set first, so a
correction that would give two identical English titles different Persian ones is refused. Release
discovery selects the head of a parent/revision chain, and still fails closed on two unrelated
artifacts for one snapshot.

A defect that is mechanically clean and semantically wrong — a paragraph that says the wrong
thing — is corrected with `revise-translation-paragraph RUN JOB --paragraph N --code C
--source-evidence E --reason R [--apply]`. The operator names one paragraph and states the defect;
they never supply Persian, so there is no arbitrary text-replacement path. The plan re-proves the
run, the snapshot, the effective acceptance, the chunk source, the paragraph's own hashes and the
frozen occurrence exemptions, and contacts nobody. Execution asks the existing reviser role to
correct that one paragraph inside a one-paragraph window, splices the answer so every other
paragraph is copied from the standing translation, and adopts it only when the bounds, the full
acceptance battery, the terminology-regression guard and an independent judge call on the revised
paragraph all agree — two provider calls, serial, inside the two-revisions-per-paragraph budget.
An adopted revision supersedes the translated bytes through the job's semantic-revision pointer;
the accepted attempt and every earlier record stay byte-identical, and `semantic_status` is left
alone, so a release still requires a full semantic gate pass.

`prepare-reference-context` verifies every pinned hash, executes the retriever from its verified
bytes without writing bytecode beside it, opens exactly the configured corpora, and retrieves one
context per chunk. The typed package keeps clean translation memory, phrases, character voice, style
samples, and term/entity evidence with book, chapter, source kind, quality, counts, and corpus
identifiers, and counts what it refused: reference-only TM, anomaly records, uncertain glossary
extractions, ambiguous common-word matches. Packages are content-addressed and immutable; the index
is republished after every chunk so an interrupted batch resumes, and an unchanged chunk under an
unchanged retrieval identity is reused rather than retrieved again.

Prompt Package Renderer 1.4.0 adds, for snapshots that froze a series policy, a length-framed
`terminology-policy` section built from the frozen policy and the chunk's frozen package: `LOCKED
CONSISTENCY TERMS` (mandatory; no re-transliteration, paraphrase, modernization, or
reinterpretation), `STRONG REFERENCE EVIDENCE` (strongly preferred unless immediate grammar or meaning
requires another form), `OBSERVED VARIANTS` (evidence with provenance; no choice by raw frequency; a
printed-glossary headword is not a prose form), and `REFERENCE CONTEXT` (analogy for terminology,
phrase choice, register, and voice; never text to copy; OCR noise warned). Corpus terminology the
policy already holds is not restated as reference evidence. Context contract
`series-terminology-context/1.1.0` keeps the observed tier to evidence the passage actually raises:
an observed term must occur in the chunk's own source text, not only in its untranslated context
window; a spelling a locked or strong decision in the same section already claims (as its source
term or an alias) is not reopened as an observed variant; and a compound's corpus form that does not
contain the locked rendering of a locked component it names (the policy builder's own promotion
rule, applied to evidence) is withheld with a count, the term disappearing when no form survives.
Locked and strong tiers are unchanged by these rules, and the policy is never edited. A locked or
strong term that authorizes further target forms is rendered with `allowed_target_forms` and a
`form_rule` line immediately after its `target`: the target stays the default and an allowed form is
a permitted grammatical realization, never a rival. Competing and observed variants are never
rendered as allowed forms. Both lines are emitted only for a term that has forms, so every section
rendered from a policy without forms is byte-identical to before (verified against all 227 stored
packages of Book 6 run 004); the same rule, through the shared `authorized_form_lines` /
`authorized_forms_payload` helpers, reaches the legacy registry block, the chapter-title prompt, the
repair request, the semantic judge and reviser requests, and the literary editor's terminology
payload. The literary editor also counts canonical realizations through the shared matcher over the
target and every authorized form, and final-QA analysis treats a variant another decision authorizes
as that decision's claim. The legacy
in-glossary registry block is omitted for such snapshots; snapshots without a policy render
byte-identically to 1.3.0. Package identity includes the policy hash, the per-chunk package hash,
and the context contract version.

A book-scoped workspace resolves terminology only from its own configuration: a series policy plus
reference corpus, an explicit registry, or nothing. It never probes `config/terminology.yaml`,
`config/terminology-questions.yaml`, or `config/source-anomalies.yaml` at the root, which remain
the legacy first book's. `validate-series-terminology` and snapshot planning fail closed while the
derived registry is stale against the policy or any chunk lacks a current reference package.

## Snapshot boundary

A validated snapshot freezes the PDF hash, normalized TOC and page boundaries, every chapter's
source and cleaned hashes, and extraction/normalization algorithm versions. Translation runs
must record the snapshot ID. Any PDF, TOC, boundary, algorithm, source, or cleaned-content change
requires a new snapshot ID; an old snapshot is never silently modified.

Snapshot Engine 1.1.0 freezes the exact bytes of every mutable translation input as a hash-named copy
under `workspace/resources/<category>/<sha256><suffix>`: the translation prompt (`prompts/`),
glossary (`glossaries/`), style guide (`style-guides/`), terminology registry (`terminology/`),
Source Anomaly Registry (`source-anomalies/`), Open Question Registry (`open-questions/`), and the
resolved book metadata record (`book-metadata/`). Copies are published idempotently before the
snapshot directory; a matching copy is reused and a hash-conflicting copy blocks publication. Because
a snapshot carries the question set that was open when it was taken, resolving a question later
produces new lineage rather than rewriting a frozen snapshot; historical snapshots stay valid.

Snapshot Engine 1.2.0 additionally freezes, for a book with a series terminology policy, the policy
(`series-terminology/`), the reference-corpus configuration (`reference-corpus/`) together with the
verified external identities of every corpus and of the retriever script, and the reference-context
index (`reference-context-index/`) whose entries pin every immutable per-chunk package under
`reference-context/`. These manifests use the `series-terminology-v1` identity representation and
are hashed exactly as written; a manifest without a policy omits the series fields from its identity
and keeps every earlier representation's digest. Validation re-verifies the frozen policy,
configuration, index, and every listed package; a later external corpus change or policy edit makes
the snapshot historical, not invalid.

Creation refuses a manifest in which any referenced mutable input has no durable frozen artifact, and
prompt, glossary, and style guide must always be present. Validation of a frozen-resources manifest
re-verifies, for every input, that the frozen artifact exists and still hashes to the manifested
SHA-256, so editing `config/translation-prompt.md`, `config/glossary.yaml`, or `config/style-guide.md`
afterwards makes the snapshot non-current for new work without making it unresolvable or invalid.

Resolved book metadata is part of book identity and therefore part of manifest identity: the manifest
records `book_id`, English and Persian titles, author, series name and volume, and the metadata
record's path, version, hash, and frozen copy. Replacing placeholder identity with real identity
produces a new snapshot ID even when every chunk hash is unchanged, so two semantically different
books can never share lineage merely because their chunk bytes match. A full-selection snapshot is
authoritative and refuses placeholder book identity; the resolver must run first.

Placeholder detection is a small generic scaffolding vocabulary (`example`, `placeholder`, `sample`,
`lorem`, `ipsum`, `changeme`, `fixme`, `todo`, `tbd`, `untitled`, `نمونه`) matched as whole
casefolded words. No book, author, or series is encoded. `synthetic` is deliberately excluded because
the testing policy uses synthetic fixtures as legitimate content. A real title containing one of
these words would be refused loudly rather than accepted silently, which is the correct default for
authoritative lineage.

Snapshot validation distinguishes immutable historical integrity from current-lineage readiness.
Manifest identity, frozen status, counts, and persisted validation contracts determine historical
validity. Live canonical hashes, conservation, and resource versions determine current lineage.
Historical snapshots and their runs remain valid audit artifacts, but cannot create or claim new work;
the current mutable/resource generation must be frozen into a new snapshot first.

Normalization Engine 1.5.0 extends the page-boundary structural rule for capitalized possessive-name
splits, narrowly guarded one-line dialogue tails, delayed interruption punctuation, lowercase
narrative dash clauses, and incomplete article complements. Normalization Engine 1.6.0 adds the
lowercase-edge-to-possessive-name continuation, the shape the source-quality audit already reported
as `possessive_continuation` evidence.
An unmatched quote-led block may join only when it contains a prior completed sentence, its final
clause remains nonterminal, and the next cross-page block begins lowercase. Simple unmatched dialogue,
new speakers, terminal paragraphs, structural blocks, and same-page boundaries remain protected.
Existing earlier normalized artifacts are stale by version; qualifying mixed dialogue-and-narration
chapters are also stale by cleaned hash. Safe regeneration proceeds in lineage order: normalize,
chunk, create a new immutable snapshot, create a new run, and render new prompt packages. Existing
snapshots, runs, attempts, and prompt packages remain immutable historical artifacts and are never
updated in place.

The implemented Translation Snapshot Engine is distinct from the extraction snapshot. It selects
chapters in canonical TOC order, requires complete valid pending chunks, revalidates exact
cleaned/source/context hashes and conservation, and freezes prompt, glossary, and style-guide versions
and hashes. Omitted selection means the complete TOC-derived prologue and numbered set; repeatable
selectors produce a subset. Provider and model binding are deliberately deferred.

The canonical manifest has no timestamp or label. Stable sorted compact JSON, encoded as UTF-8 with
one final newline, is hashed for identity; readable YAML persists the model. Publication follows
`draft → validated → frozen` inside staging, then atomically renames the complete directory.
Historical validation selects canonical identity serialization from the validated immutable manifest
representation. Legacy version-1 manifests hash only fields present under their original contract
(including omission of absent terminology and `frozen_file` fields); manifests containing a frozen
terminology reference use the frozen-terminology representation; manifests whose prompt reference
carries a non-null `frozen_file` use the frozen-resources representation and hash the payload exactly
as written. Loading a legacy manifest into a newer model never injects defaults into its identity
payload, so earlier snapshots keep their original IDs, hashes, and validity forever.

## Translation Queue

Translation Queue 1.0.0 creates one structural job per snapshot chunk in manifest order. It stores no
source, context, prompt, glossary, or style-guide content. Runs begin `created → ready`, become active
on the first claim, and complete only from future provider-result transitions. Queue completion never
means translated content was reviewed or approved.

Claims consume one attempt when ownership is granted, carry a UTC lease, worker ID, and secret claim
token, and are serialized by a bounded local advisory lock. Release or expired-lease recovery returns
the job to pending without decrementing the attempt. Failed jobs require explicit retry scheduling;
recovery promotes them only after the configured backoff. Every claim fully revalidates its frozen
snapshot in version 1.

## Prompt Package Renderer

Prompt Package Renderer 1.0.0 selects only structurally valid `pending` or `claimed` jobs and performs
a full run, snapshot, chunk, context, and resource preflight before any batch write. It does not claim
jobs, consume attempts, alter leases, or execute translation.

The system message holds durable provider-neutral translation constraints. The user message contains
structural JSON followed by independently length-framed prompt rules, exact style-guide bytes,
deterministically rendered glossary data, exact preceding context, and exact current source. Context
is comprehension-only; only the source frame requests output. Frame byte lengths and SHA-256 values
make delimiter-like story text unambiguous and permit byte-exact extraction tests. A future executor
must preserve system/user roles because framing alone is not a complete prompt-injection defense.

Package identity hashes canonical JSON containing renderer/output-contract versions, snapshot/run/job
lineage, source/context hashes, resource versions/hashes, languages, and rendered message hashes.
Timestamps, labels, claim data, workers, and machine paths are excluded. Publication uses a private
sibling staging directory and atomically renames a complete validated package. Matching packages
skip; stale or safely incomplete stage-owned packages require `--force`; execution evidence always
blocks replacement.

Writing a package and judging a stored one are separate questions and use separate planning purposes.
Rendering refuses a job that has left `pending`/`claimed` or that already carries downstream execution
evidence. Validation asks only whether the stored package still equals what the current inputs render,
so a published attempt never makes it stale and an unchanged package stays valid for an explicit
retry; message, source, context, snapshot, or frozen-resource drift still invalidates it. Whether a
retry is *permitted* is a queue and executor decision, never a package-validity one.

## Numbered paragraph contract

Blank lines carry no identity. Two source paragraphs written as one target block and one source
paragraph written as two produce a translation with exactly as many blocks as its source, so a
count comparison sees nothing while every paragraph after the first defect is the translation of a
different source paragraph than the reader is looking at. One merge plus one compensating split is
the concrete shape this took in the completed pilot, in three separate chunks, and structural QA
passed all three.

The numbered paragraph contract (`book_translator.paragraph_markers`,
`PARAGRAPH_MARKER_CONTRACT_VERSION` 1.0.0) replaces that inference with an explicit identity. Each
source paragraph is sent under a marker line of the exact form `[[Pnnnn]]`, one-based, zero-padded
to four digits, and the provider must return every marker exactly once, alone on its own line, in
ascending order, each carrying the translation of that one source paragraph.

A run selects its contract when it is created and freezes it on `TranslationRun`:

| `output_contract_version` | meaning |
| --- | --- |
| absent | the original unmarked prose contract, which is what every run created before this contract records |
| `persian-translation-text/1.0.0` | the same contract, selected explicitly |
| `persian-translation-marked-paragraphs/1.0.0` | the numbered paragraph contract |

Freezing it on the run rather than reading configuration at render time is what keeps an existing
run interpretable: its packages still render byte-identically, its attempts still mean what they
meant, and its accepted translations stay eligible for cross-run reuse. `create-translation-run`
takes `--paragraph-markers/--no-paragraph-markers`; the default comes from
`paragraph_marker_contract` in `config/book.yaml`, or in `config/books/<book-id>.yaml` under
`--book`, and is on.

`create-translation-run` resolves a bare snapshot ID in exactly one workspace: the legacy root, or
the book workspace selected with `--book <book-id>`. It never scans other workspaces, so an ID
cannot resolve ambiguously; a book-scoped ID without its `--book` (or with the wrong one) is
refused. An explicit snapshot path bypasses the lookup and is validated against the workspace its
own frozen manifest records, which `--book` may confirm but never overrides. Run storage is
unchanged by workspace selection: every run lives under the project-level
`workspace/translation-runs/`, its ID is keyed by the content-addressed manifest hash, and its
`snapshot_path` names the workspace it belongs to. The run-side commands (listing, claiming,
execution, QA, reuse, repair, ...) resolve bare run IDs only under that root; relocating runs under
`workspace/books/<book-id>/translation-runs/` would be a migration of that whole command family.

Under the marked contract the framed `source-text` section carries the *marked* source, and
conservation is proven by stripping the markers back to the frozen bytes rather than by comparing
the framed bytes directly. The system message gains the marker rules, and the user message carries
an `<output-contract>` block naming the contract, the marker count, and the first and last marker.

Parsing is deterministic and fails closed. `markers_absent`, `marker_missing`,
`marker_duplicated`, `marker_unknown`, `marker_out_of_order`, `marker_malformed`,
`text_before_first_marker`, and `empty_marker_block` are all errors, and each finding names the
expected marker, the observed marker, and the source paragraph index where one applies. No attempt
is made to work out which paragraph a provider meant: a wrong answer there moves prose between
identities silently, which is worse than the defect being detected.

Conformance is decided at the provider boundary, before a translation artifact can exist. A
violation is classified as a retryable malformed response and the job re-enters the ordinary retry
path with no `translation.md` written, so a broken answer never becomes a draft. A conformant
answer is stripped and stored as pure prose: marker syntax never reaches an artifact, a release, an
EPUB, or a reader.

A refused answer is not discarded. Result validation is split into two pure steps: the candidate
step establishes that a completed result carries exactly one non-empty text output and
LF-normalizes it; the structural step judges that candidate as a translation (source echo,
prompt-framing reproduction, refusal envelope, marker conformance). When the structural step refuses
a candidate, the executor publishes the exact normalized candidate bytes as
`attempts/NNN/rejected-output.md` beside the failed attempt — private, hashed, and recorded on the
attempt as `rejected_output_file` / `rejected_output_sha256` — so an expensive answer can be
inspected instead of lost. Marker refusals additionally record `marker_findings` (finding code,
expected marker, observed marker token, source paragraph index), `expected_marker_count`, and
`observed_marker_count`, which say which marker failed without anyone opening the prose. A result
that fails the candidate step (truncated, empty, several competing outputs) and a failure that
arrives as a transport, authentication, or rate-limit error leave no rejected artifact, because no
trustworthy candidate exists; an ambiguous attempt never carries one either. The rejected artifact is
diagnostic only: it is never `translation.md`, never assessed, accepted, repaired, reused, or
released, and no preamble stripping, marker synthesis, or paragraph merging is performed on it.

## Paragraph alignment QA

Alignment QA (`book_translator.paragraph_alignment`, `ALIGNMENT_QA_VERSION` 1.0.0) runs after
paragraph identity is established and before acceptance. For each aligned pair it measures source
and target word counts and their ratio, source and target non-whitespace character counts and their
ratio, source and target sentence counts, numerals folded to their decimal values, and quotation
marks. Words are counted by whitespace separation, because a Unicode word scan splits Persian
compounds at the zero-width non-joiner and inflates the target count.

Thresholds live in a named, versioned policy rather than in the engine. `en-fa/1.0.0` requires at
least 25 source words before a pair is measured at all and treats a target/source word ratio below
0.60 or above 1.60 as out of band. Those numbers were measured: over the completed pilot's 5,068
aligned pairs they selected exactly the three chunks an independent audit had confirmed to carry
boundary drift, and nothing else. A target language with no measured evidence gets the same numbers
under the identifier `unmeasured-pair`, so a report never claims English-to-Persian evidence for a
pair that has none.

| code | severity | what it claims |
| --- | --- | --- |
| `paragraph_word_ratio_out_of_band` | warning | one long paragraph is much shorter or longer than its source |
| `adjacent_boundary_drift_suspected` | error | two adjacent paragraphs carry complementary deficit and excess whose combined ratio is back inside the band |
| `numeral_not_preserved` | warning | a source numeral has no counterpart, which a spelled-out number legitimately explains |
| `dialogue_marks_absent` | warning | the source marks direct speech and the target carries no quotation marks at all |
| `alignment_not_evaluable` | warning | block counts differ, so no pair can be measured; the structural validator already blocks on this |

Only the drift finding blocks acceptance, and the asymmetry is deliberate. A lone ratio outlier is
one signal from one corpus, and literary Persian legitimately compresses and expands. Complementary
adjacent ratios are a different claim: content that leaves one paragraph and arrives in the next
conserves the total, and that conservation is the signature. It is also the one defect the marker
contract cannot see, because a provider can honour every marker and still put the wrong sentence
under one.

Alignment findings are never automatically repairable. `adjacent_boundary_drift_suspected` is
`operator_required` in the auto-repair policy and forces `operator_required` in the final-QA
policy, because every automatic remedy either rewrites prose or moves separators and both would be
acting on a guess about which paragraph the content belongs to.

### Legacy alignment audit

`audit-paragraph-alignment RUN_ID` measures a run that predates the marker contract. It resolves
each job's effective acceptance through the same resolver release assembly uses, reads the frozen
source and the accepted translation, and measures them positionally. It contacts no provider and
changes no acceptance, status, counter, or translated byte; its report is published outside every
run directory, under `workspace/audits/paragraph-alignment/RUN_ID/`.

```bash
book-translator audit-paragraph-alignment RUN_ID --dry-run --show-findings
book-translator audit-paragraph-alignment RUN_ID
```

## Translation Provider Executor

Provider Executor 1.0.0 executes exactly one claimed job. It requires a live-valid frozen snapshot
and prompt package, constant-time worker/token ownership, a nonexpired lease, an allowed
provider/model, and no prior successful or ambiguous attempt. Attempt number equals the claim's
already-consumed queue attempt count.

The configuration that governs an execution is the run's own book's, never a fixed file. Runs are
stored project-wide, so the run ID alone does not say which book a run serves; its frozen lineage
does: the run's `snapshot_path` and the manifest's workspace paths were published together and
must name the same workspace (`workspace/` → `config/book.yaml`, `workspace/books/<book-id>/` →
`config/books/<book-id>.yaml`), and a run whose two records disagree is refused as ambiguous rather
than executed under a guessed configuration. Every run-side consumer of `BookConfig` resolves this
way — the executor's model allow-list and OpenAI reasoning-model check, the worker's and
`execute-translation-job`/`repair-translation-job`/`finalize-translation-run`'s timeout and output
defaults, and the semantic and literary boundaries' provider settings — so a Book 6 run reads Book
6's allow-list while every legacy-root run reads `config/book.yaml` exactly as before.

The provider/model allow-list (`translation_provider_models`) is exact. A requested `--model` must
be listed under its provider, and an unlisted provider is refused even without a model. An empty
list is not a wildcard: it admits only the omission of `--model`, which for the authenticated CLI
session providers (`claude-code`, `codex-cli`) means the session's own selection. A model alias such
as `opus` is compared against the list and forwarded to the CLI exactly as written; there is no
canonical model-resolution layer, and the returned model is recorded from the provider's result.

Before network contact, execution extends the owned lease when necessary to cover the configured
timeout plus 60 seconds, capped at one hour, writes a fixed private reservation, and transitions
`claimed → running`. The reservation changes from `prepared` to `requesting` immediately before the
adapter call. Recovery may safely restore a `prepared` reservation to `claimed`; `requesting`
evidence is ambiguous and blocks automatic replay.

## Execution Reservation Resolution

Execution Resolution Engine 1.0.0 (`resolve-translation-reservation`) is the only sanctioned way past
a `requesting` reservation, and it is operator-driven rather than automatic. The operator selects one
external assertion — `provider_not_contacted`, `provider_contacted_no_result`, or
`provider_contact_unknown` — and supplies a reason. The command plans by default and mutates nothing
without `--apply`; applying preserves the reservation byte-for-byte, publishes an immutable
`jobs/<chunk>/resolutions/NNN/resolution.yaml` record with operator provenance, and moves the job to
`pending`, `failed`, or `blocked` respectively. Only `provider_not_contacted` abandons the attempt
slot instead of spending a provider attempt, and only it permits replay; the other two charge the
attempt and require a further explicit decision or none at all. `inspect-translation-reservations`
reports unresolved and resolved reservations without exposing claim material. See
`docs/recovery.md` for the full preflight, idempotence, and conflict rules.

The provider protocol preserves separate system and user messages. The OpenAI adapter uses the
official Python SDK Responses API with `store: false`, no streaming, no tools or retrieval, an
idempotency header, explicit timeout/output limit, and zero SDK retries. The fake adapter is local,
deterministic, and unsuitable as book translation.

The `codex-cli` adapter invokes the authenticated local `codex exec` process without a shell, API key,
router, or direct API call. It renders explicit system/user sections to standard input, runs with
`--sandbox read-only` and `--ephemeral` in a private temporary working directory, and uses
`--output-last-message` as the sole translation source. An omitted model leaves the user's Codex
configuration/login selection untouched; an explicitly allowed `--model` is forwarded. CLI stdout
and stderr are never parsed or published as translation content.

The `claude-code` adapter is the same shape for Claude Code and is deliberately not the Anthropic
API: it invokes the user's installed `claude` executable under their existing Claude Code login,
strips every `*_API_KEY` variable plus Anthropic base-URL, Bedrock, Vertex, and Foundry routing
variables from the child environment, and therefore cannot fall back to an API account or a proxy.
One job is one `claude --print --output-format stream-json --verbose` process run in a private
temporary working directory with `--tools ""`, `--disable-slash-commands`, `--strict-mcp-config`,
`--safe-mode`, and `--no-session-persistence`, so Claude has no tools, repository access, MCP
servers, skills, hooks, or session history during translation. The rendered system prompt is staged
as an owner-only `--system-prompt-file` instead of an argv value, and the rendered user message is
written to standard input. The translation is reconstructed from the transcript by the pure
`claude_stream` parser (see below); ANSI sequences are stripped, an empty reconstruction is a
retryable failure, and a non-`success` result subtype is refused rather than published.
`--timeout-seconds` becomes the process timeout, and `--max-output-tokens` is exported through
`CLAUDE_CODE_MAX_OUTPUT_TOKENS`, which the CLI applies as the per-request `max_tokens`; an overrun
the CLI gives up on is classified as retryable truncation. The CLI version, authentication method,
first-party inference provider, returned model, and canonical model name are recorded so a Claude
Code attempt is distinguishable from an Anthropic API, OpenAI, or Codex CLI attempt.

Adapter 1.1.0 classifies a failing exit from what the CLI actually reported rather than from the exit
status alone: the structured result object first (`subtype`, `api_error_status`, error text), the
trailing stderr lines second. That separates usage/option, configuration, authentication, model,
rate-limit, and server failures from a bare nonzero exit and sets the retryable flag the worker's
automatic retry reads. Whatever is retained is redacted and capped first, so a diagnostic cannot
become a place where credentials, identity, machine paths, or prose accumulate — see `docs/privacy.md`.

Adapter 1.3.0 captures the session losslessly. Verified against the installed CLI, the `json`
format's `result` field is the last text block of the *last* assistant message, and the CLI's turn
loop resets it on every `user` message it processes. When a response reaches the output token
limit, Claude Code injects a synthetic user nudge ("Output token limit hit. Resume directly …") and
continues in a further assistant turn, reporting `num_turns: 2` — so `result` carried only the
continuation and the first turn was gone. The adapter therefore runs `stream-json` (which the CLI
requires `--verbose` for in print mode) and `claude_stream.parse_claude_stream` reconstructs the
answer: every `assistant` event whose `parent_tool_use_id` is null, whose `session_id` matches the
final `result` object, and which is neither a meta nor an API-error carrier contributes its `text`
blocks, joined in stream order with no separator (a continuation resumes exactly where the cut
happened, and inserting anything would invent bytes); events sharing one `message.id` are the
per-block snapshots of one API message and are joined, an event replaying an already-seen `uuid`
with identical content is counted once, and everything else — thinking and tool blocks, `user`,
`system`, progress, and `stream_event` deltas, subagent messages — is ignored. It fails closed, as a
retryable `malformed_response`, when a line is not JSON, there is not exactly one `result` object,
an assistant event follows it, `num_turns`, `session_id`, or `result` is missing or malformed, an
assistant event lacks a `uuid` or `message.id`, message ids interleave, one `uuid` carries
conflicting content, the reported turn count exceeds the reconstructed primary messages, the
stream carries more user turns than the result reports, or the `result` text is non-empty and
differs from the final assistant message. A one-turn session reconstructs to exactly what the
`result` field used to yield. `response.json` gains `assistant_message_count` and
`user_event_count`; it still holds no prose, and the raw transcript exists only in the adapter's
subprocess capture.

### Provider preflight

`preflight_provider` is a provider-neutral dispatch performed *before* recovery, claiming, or any
other queue mutation by `run-translation-worker`, `execute-translation-job`, and
`repair-translation-job`, and exposed directly as `check-translation-provider --provider NAME`.
Providers with no local precondition report `supported: false` and pass. `claude-code` verifies the
executable, that the installed CLI still documents every non-interactive flag the adapter uses, and
that `claude auth status --json` reports an active login. `codex-cli` verifies the executable and
detects its version from `codex --version`, that `codex exec --help` documents every capability the
adapter's command line relies on (the required list is `REQUIRED_EXEC_CAPABILITIES` and changes
together with the adapter's command), and that `codex login status` reports an active login;
capabilities are detected from the installed CLI rather than pinned to a version number. The login
status text is classified by fixed markers and never retained: a logged-out or unrecognizable status
fails closed, a ChatGPT login is reported as `auth_method: chatgpt`, an active login of unstated kind
as `unknown`, the subscription plan is always `unknown` because the CLI does not expose it without
reading its credential store, and an API-key login is refused because recovery evidence never records
an API-key path. No preflight step sends book content or makes a model call, and a preflight failure
raises before a claim, so no attempt is consumed and no ambiguous attempt is published. Dry runs skip
preflight entirely and remain fully local.

Clear failures publish a sanitized failed attempt and move `running → failed`; when the failure is a
structural refusal of an answer that arrived whole, the failed attempt also carries the private
`rejected-output.md` diagnostic described under the numbered paragraph contract. Post-send ambiguity
or lease expiry publishes an ambiguous attempt and moves `running → blocked`. A structurally
successful result publishes private response/translation evidence and moves `running → succeeded`.
None of these states changes chapter/chunk review or approval metadata. Attempt validation checks
the rejected artifact's presence, hash, and permissions as its own result
(`rejected_output_hash_valid`) and refuses the combinations no honest execution produces: a
rejected artifact on a succeeded or ambiguous attempt, a rejected artifact beside `translation.md`,
or marker findings on a succeeded attempt.

## Translation Worker

`run-translation-worker` processes one run, sequentially by default or with `--workers N`
execution slots (see below). It first assesses any historical or
crash-interrupted provider-successful draft, then claims pending work, executes the selected provider,
revalidates the persisted attempt, and applies terminology, structural, and paragraph alignment QA
against the frozen snapshot lineage. Warnings such as `trailing_double_space` remain nonfatal;
terminology errors, `paragraph_count_mismatch`, and a blocking alignment finding reject acceptance.
The alignment report is recorded on the acceptance sidecar, and its absence on older evidence means
the gate never judged it rather than that it passed.

```bash
book-translator check-translation-provider --provider claude-code
book-translator check-translation-provider --provider codex-cli
book-translator run-translation-worker RUN_ID --provider codex-cli --max-jobs 1 --dry-run
book-translator run-translation-worker RUN_ID --provider claude-code --max-jobs 1 --dry-run
book-translator run-translation-worker RUN_ID --provider codex-cli --max-jobs 5
book-translator list-translation-jobs RUN_ID --acceptance-status qa_rejected
```

Defaults are one execution slot, automatic queue scheduling for clear retryable provider failures,
continued processing after per-job errors, no automatic QA repair, and no automatic
provider-failure recovery of either a translation or a repair. Use `--no-retry-provider-failures`, `--stop-on-error`, `--auto-repair`,
`--auto-recover-provider-failures`, provider timeout/output options, or an explicit `--worker-id`
when operational policy requires them. Dry-run performs no recovery, claim, provider
call, or write. Worker output contains structural identifiers and counters only, never translation
prose or claim tokens. Acceptance remains distinct from later review and approval.

The worker checks the requested provider/model against the run's own book configuration before
the provider preflight and before any claim — in a dry run too, so `--dry-run` is a faithful
rehearsal of the configuration. A disallowed model therefore stops the worker with the executor's
own message and consumes nothing; the executor repeats the check when it plans, but by then a claim
would already have spent an attempt slot.

A per-job failure that the worker records and moves past (`Errors` in the summary) is never a bare
count. Each `error` outcome carries the failure class and its structural message, printed as
`Error <job-id>: ...` after the summary, and when the failure struck a claim the worker still holds
— between the claim and the reservation, where no attempt directory and no provider classification
exist — the same message is recorded on the job as `last_error_category: worker`,
`last_error_message`, and a `worker_error` event with the worker ID. The record is diagnostic only:
the claim's status, lease, and counters are untouched, so lease expiry, recovery, and release behave
exactly as before, and the consumed slot stays consumed (see the data model's attempt accounting).

### Concurrent execution slots

`--workers N` (default 1, minimum 1) runs one pass with N execution slots. The coordinator — the
process the command started — performs the once-per-pass work exactly as the sequential worker
does and in the same order: option validation, run and book resolution, existing-acceptance
validation, selection validation, the provider/model policy check, the provider preflight, and
queue recovery. Only then does it build the pass's work plan and start N *spawned* processes.
Each slot receives explicit pickled arguments (the run path, the project root, its own options
with a derived worker identity `<worker-id>-<slot>`, and the provider factory) and no inherited
state; a provider factory or clock that cannot be pickled is refused before any claim.

The work plan is the sequential worker's selection, bounded by the global `--max-jobs`: unassessed
provider-successful drafts first, then one *claim* per currently claimable job, then QA-rejected
drafts automatic repair may act on. A claim item does not name a job. The slot that receives it
calls the queue's own `claim_job`, which selects the next eligible job in canonical priority order
and claims it under the queue lock in one critical section, so two slots asking at once receive
two different jobs or one receives nothing. The executor's existing ownership checks are unchanged
and still run immediately before the provider call — claimed status, matching `claimed_by`,
constant-time claim-token comparison, a live lease, a unique attempt slot, and no successful or
ambiguous attempt — and again before the attempt is published. Leases are unchanged too: the claim
carries `--lease-seconds`, and the reservation extends it to the provider timeout plus the safety
margin, which covers one provider call; there is no heartbeat.

Slots return structured results; the coordinator renders them. Results are reported in completion
order, each naming its slot, and the summary aggregates every slot's outcomes with the same
counters as a sequential pass plus `Workers` (effective slot count) and `Worker failures`.
Effective concurrency never exceeds the selected work, and one explicit `--job-id` runs with one
slot. `--dry-run` plans as before and starts no process. Per-job policy is unchanged and per job:
`--no-retry-provider-failures`, QA acceptance, `rejected-output.md` preservation, and automatic
repair each belong to the job that produced them.

`--stop-on-error` stops the coordinator from dispatching new work after the first `error` outcome;
slots that are executing finish and finalize normally, and the summary reports everything that
completed. A stop request (Ctrl+C or SIGTERM to the coordinator) is handled the same way. A slot
process that exits unexpectedly is reported as an `error` row for the job it held (found from the
queue by its worker identity), counted under `Worker failures`, and stops further dispatch; its
claim and any `requesting` reservation are left exactly as the queue recovery rules expect, and no
other slot may execute that job while the claim is live. `--auto-recover-provider-failures` is
refused with more than one worker; see `docs/recovery.md`.

```bash
book-translator run-translation-worker RUN_ID --provider claude-code --model opus \
  --workers 4 --max-jobs 226 --no-retry-provider-failures --stop-on-error
```

Start with 2–4 workers: provider-side concurrency or rate limits usually bind before CPU does,
and throughput with a real provider is not linear in the slot count.

### Targeted worker selection

`--job-id` and `--chapter` narrow which queue jobs the worker may select. They reuse the queue's
own claim eligibility rule, priority ordering, lock, and lease, so a targeted worker run is an
ordinary claim rather than a second selection path, and the worker still manages the claim token
internally.

```bash
book-translator run-translation-worker RUN_ID --provider codex-cli --job-id JOB_ID \
  --max-jobs 1 --dry-run
book-translator run-translation-worker RUN_ID --provider codex-cli --chapter 007 --max-jobs 1
```

`--job-id` processes exactly that job and never falls back to another one. If the job is absent
from the run, or is not an unassessed provider-successful draft and not claimable — already
claimed, running, succeeded, assessed, cancelled, blocked, or out of attempt budget — the worker
processes zero jobs and reports the structural reason as an error. `--chapter` accepts the usual
chapter selector (`7`, `007`, `chapter-7`, `prologue`) and restricts selection to that chapter
while keeping canonical queue priority order inside it; an unknown chapter is refused. The two
options are mutually exclusive because a job ID already identifies exactly one job, and both work
with `--dry-run`, which selects without claiming, writing, or contacting a provider.

### Automatic QA repair

`--auto-repair` lets the worker answer a mechanical QA rejection itself instead of stopping at
`qa_rejected` and waiting for an operator to run `repair-translation-job`. It changes nothing by
default: without the flag the worker behaves exactly as before.

```bash
book-translator run-translation-worker RUN_ID --provider claude-code --auto-repair \
  --include-qa-rejected --job-id JOB_ID --max-jobs 1 --dry-run
book-translator run-translation-worker RUN_ID --provider claude-code --auto-repair \
  --include-qa-rejected --max-jobs 200
```

With `--auto-repair` alone, a job the worker just processed and QA rejected is assessed against
the auto-repair policy and, if the policy and budget permit, repaired through the ordinary repair
boundary and re-assessed, until it is accepted, the policy refuses, or the budget runs out.
`--include-qa-rejected` additionally selects jobs that were *already* QA-rejected before this
invocation, and requires `--auto-repair`. Those jobs form their own selection class placed after
all pending translation work: pending queue ordering is a contract, so remediation of
already-drafted work never displaces, reorders, or starves it. A targeted `--job-id` naming a
QA-rejected job needs the same two flags, and the error explains that when they are absent.

The worker's full selection order is four classes, each exhausted in canonical queue order before
the next is offered:

1. provider-successful drafts awaiting QA assessment, and pending translations, in canonical
   queue order;
2. QA-rejected jobs eligible for repair (`--auto-repair --include-qa-rejected`);
3. failed translations eligible for provider recovery
   (`--auto-recover-provider-failures --include-failed`);
4. failed repairs eligible for repair recovery (those two flags plus `--auto-repair`).

Classes 2–4 are remediation and never reorder class 1. Within the remediation classes the order is
cheapest-remedy-first: a repair reuses a draft that already exists, a translation recovery buys a
draft that does not, and a repair recovery is remediation of remediation — the job already has a
draft and already has a repair decision, and only the provider call was lost.

The policy (`AUTO_REPAIR_POLICY_VERSION`, currently `1.2.0`) classifies every finding of the
effective QA evidence:

| class | meaning | findings |
| --- | --- | --- |
| `auto_repairable` | a repair carrying the finding can mechanically resolve it | `paragraph_count_mismatch`, `canonical_term_missing` under exact or morphological enforcement, and the mechanical warning codes |
| `reevaluation_candidate` | the QA judgement may simply have moved; re-assess before paying a provider | `canonical_term_unverified`, `provisional_decision_enforced` |
| `non_repairable` | the cause is not in the translation | `known_source_anomaly`, and the advisory alignment codes |
| `operator_required` | a person must decide | `competing_variant`, `adjacent_boundary_drift_suspected`, and every unrecognized code |

Alignment findings entered the classification in 1.2.0 as their own origin. None is
`auto_repairable`: suspected boundary drift is a claim that content sits under the wrong paragraph
identity, and handing it to a provider as "fix this" would ask the provider to guess which sentence
belongs where — the guess the numbered paragraph contract exists to eliminate.

A repair is authorized only when *every* blocking finding is `auto_repairable`, so a rejection
mixing a repairable finding with an unsafe one is refused whole rather than half-repaired. If the
effective evidence records QA engine versions that are no longer the installed ones, the verdict
is `reevaluation_required` and the correct next step is the provider-free
`reevaluate-translation-job` boundary rather than a paid repair.

`--max-repair-attempts` (default 2) bounds repairs per job — one initial translation attempt plus
at most that many repairs — and is counted from the immutable attempts themselves, so a repair an
operator performed by hand is charged against the same budget. It is not provider transport
retry: `--retry-provider-failures` reschedules a request that never produced a usable result,
while a repair answers a delivered result that QA rejected. The effective budget is the smaller
of this allowance and the job's remaining `max_attempts`.

Each authorized repair publishes an immutable `jobs/<chunk>/auto-repair/NNN/decision.yaml` before
the provider call, naming the policy version, the classified findings, the triggering codes, the
budget, the repaired-from attempt, and `initiator: automatic`. Re-entering the same attempt slot
after an interruption reuses that record instead of rewriting it, and a record contradicting the
current lineage stops the repair. Outcome evidence is not copied into it: `attempts/NNN` and
`acceptance/NNN` already hold the provider status, the resulting QA findings, and the final
acceptance status. A repaired job is counted once — `accepted` with two provider attempts, never
two jobs — and a repair that fails or goes ambiguous stops the cycle with the job still
`qa_rejected` and its earlier acceptance evidence untouched. `--dry-run` reports the selection and
the policy verdict where it is knowable, contacts no provider, and writes nothing.

### Automatic provider-failure recovery

A provider failure ends one attempt and leaves the job `failed`. The classification the executor
recorded on that attempt — `error_category`, `retryable`, `ambiguous` — is history: it describes
what was true at that moment, it is never rewritten, and an attempt marked `retryable: false` was
genuinely not retryable when it was made. What it does *not* describe is the provider now. An
expired login that has since been renewed is a changed fact about the provider, not a changed fact
about the attempt.

`--auto-recover-provider-failures` is where that later question is answered. It is off by default
and changes nothing without it.

```bash
book-translator run-translation-worker RUN_ID --provider claude-code \
  --auto-recover-provider-failures --include-failed --job-id JOB_ID --max-jobs 1 --dry-run
book-translator run-translation-worker RUN_ID --provider claude-code \
  --auto-recover-provider-failures --include-failed --max-jobs 200
```

With `--auto-recover-provider-failures` alone, a job the worker just processed and the provider
just failed is assessed against the recovery policy. `--include-failed` additionally selects jobs
that were *already* `failed` before this invocation, and requires
`--auto-recover-provider-failures`. Those jobs form a third selection class, after pending
translation work and after QA repair: a recovered job re-enters the canonical queue, so selecting
it earlier would let remediation decide what gets translated next. A targeted `--job-id` naming a
failed job needs the same two flags, and the error says so when they are absent.

The policy (`PROVIDER_RECOVERY_POLICY_VERSION`, currently `1.0.0`) is pure and lives in
`provider_recovery_policy`. It first places the historical failure in a provider-neutral family,
because the question "may a new attempt be spent, and what has to be re-proved first" has the same
answer for every executor category inside a family:

| family | executor categories | recovery |
| --- | --- | --- |
| `transient` | `timeout_pre_send`, `connection_pre_send`, `server_error` | recoverable, no re-evidence needed |
| `rate_limit` | `rate_limit` | recoverable after a longer cooldown |
| `authentication` | `authentication`, `authorization` | recoverable only against a fresh passing, authenticated preflight |
| `provider_unavailable` | `executable_not_found`, `invocation_error` | recoverable only against a fresh passing preflight |
| `malformed_response` | `malformed_response`, `empty_response`, `empty_output`, `output_truncated` | `operator_required` |
| `contract_violation` | `invalid_request`, `unsupported_model_or_parameter`, `context_length`, `package_invalid`, `claim_invalid` | `operator_required` |
| `content_policy` | `content_policy` | `operator_required` |
| `local_environment` | `local_io` | `operator_required` |
| `ambiguous_execution` | `timeout_ambiguous`, `connection_ambiguous`, `timeout`, `interrupted`, `lease_invalid`, or any attempt flagged ambiguous | `unsafe_to_replay` |
| `unknown` | `nonzero_exit`, `unknown`, an absent category, and any category a future adapter adds | `operator_required` |

Recovery acts only on a failed job that has no acceptance status yet. A job whose *repair*
attempt failed at the provider is `failed` while still carrying a QA rejection, and re-entering it
as an ordinary translation would spend an attempt whose result the rejection on record would not
describe; that case stays with the repair path and an operator, and recovery does not quietly take
it over.

A numbered-paragraph-contract violation is classified `malformed_response`, which makes it
*retryable* at the queue — a shape failure is answered by asking the same request again — while
still being `operator_required` at this boundary, which is only reached once the ordinary attempt
budget is spent. A provider that cannot follow the contract in three attempts is not a scheduling
problem.

A malformed response, a contract violation, or a content refusal is not recovered however healthy
the provider now looks: the provider answered, and re-sending an identical request it already
answered badly is repetition rather than recovery. An undiagnosed nonzero exit is an unclassified
failure wearing a specific-looking name, so it fails closed. An ambiguous attempt is never
replayed at all — not knowing whether a request reached the provider is exactly what
`resolve-translation-reservation` exists for, and a job left ambiguous is `blocked` rather than
`failed`, which puts it outside the recovery selection class by construction.

The verdicts are `recoverable_now`, `wait_for_backoff`, `preflight_required`, `operator_required`,
`unsafe_to_replay`, and `exhausted`. For a health-dependent family the policy performs no I/O and
therefore cannot conclude anything on its own: it returns `preflight_required` until the caller
supplies preflight evidence taken *after* the failure. The worker then runs the existing provider
preflight at most once per pass and re-assesses. Evidence is dropped again the moment a recovered
attempt fails, so a provider that lapsed mid-pass cannot authorize a second recovery on evidence
that predates the lapse. A failing preflight stops recovery cleanly with `operator_required` and
writes nothing.

`--max-provider-recovery-attempts` (default 2) bounds recoveries per job and is counted from the
published decision records themselves, so it cannot drift. It is deliberately not
`--max-repair-attempts`, not provider transport retry, and not the queue's `max_attempts`: the
effective budget is the smaller of this allowance and the job's remaining provider attempts, so
the policy can only ever be more conservative than the queue's own accounting.

A `transient` failure is additionally not replayed before the run's `retry_backoff_seconds` has
elapsed since it happened, and a rate-limited provider is rested five times as long; until then
the verdict is `wait_for_backoff` and the next worker invocation picks the job up. The
preflight-gated families have no cooldown, because there the readiness proof is the preflight
rather than the passage of time — a login a preflight has just verified is not more verified sixty
seconds later — and a provider that passes preflight while still failing every call exhausts the
recovery budget in two attempts rather than being paced by a timer.

Recovery contains no provider execution of its own. An authorization publishes an immutable
`jobs/<chunk>/provider-recovery/NNN/decision.yaml` — named by the attempt slot it authorizes —
*before* the queue is touched, then re-enters the job through the ordinary
`failed -> retry_pending -> pending` lifecycle and claims it explicitly by ID. The recovered
attempt is executed by the same governed path as every other attempt, continues into ordinary QA
acceptance and, when `--auto-repair` is also on, into ordinary automatic repair. The decision
record names the policy and engine versions, the failure family, the failed attempt's number, ID,
path, and SHA-256, its recorded `error_category`, `retryable`, and `ambiguous` flags, the replay
safety established from artifacts, the budget drawn against, whether a preflight was required, the
preflight evidence, the authorized attempt number, and `initiator: automatic`. Preflight evidence
records provider identity, executable, CLI version, authentication status, and auth method, and
structurally refuses credential-shaped values and any API-key path. Outcome evidence is not copied
into the decision: `attempts/NNN` and `acceptance/NNN` already hold it.

`--dry-run` reports the selection and the recovery verdict and writes nothing. It is the one
dry-run answer that may run the provider preflight, because a health-dependent verdict is not
knowable without current health evidence; the preflight makes no model call and sends no content.

### Recovering a repair the provider failed to deliver

A provider failure can end a *repair* rather than a translation, and the resulting job is a
different animal: `failed`, but still carrying the QA rejection the repair was answering, with
`accepted_attempt` still naming the rejected draft rather than the attempt that just failed. That
shape is what `is_failed_repair` recognizes, and nothing else produces it.

Such a job is recovered **as a repair**. It is deliberately outside the ordinary translation
recovery class — re-entering it as a translation would spend a provider attempt whose result the
QA rejection on record does not describe — and outside the auto-repair class, which needs a live
provider success this job no longer has. Selecting one needs
`--auto-recover-provider-failures --include-failed` *and* `--auto-repair`, because it is both a
recovery and a repair execution; `--include-qa-rejected` is not required, since that flag admits
new QA repair decisions and a recovery makes none.

The recovered attempt keeps `execution_kind: repair` and keeps `repaired_from_attempt` pointing at
the QA-rejected **translation** the repair answers — the repair root — never at the failed repair,
which produced no translation and so is nothing to repair from. A repair request is derived rather
than frozen, so recovery must prove it derives the same one: the request is rebuilt from the
standing rejection, the rejected draft, and the frozen resources, and its two provider-facing
message digests are matched against what the failed attempt recorded sending. Tampered QA
evidence, a tampered request payload, an edited `repair-system.md` or `repair-user.md`, a frozen
resource that no longer matches the manifest, or a rejection that no longer carries a blocking
finding all stop the recovery with `operator_required` and a named reason, because sending a
*different* repair under the authorization for the original is precisely what must not happen.

An authorized repair recovery does not pass through `pending`. A pending job is claimable as an
ordinary translation, and this one has a successful attempt that ordinary execution refuses
outright. Instead the authorization restores the QA-rejected draft the failed repair displaced —
`status: succeeded` with `successful_attempt` and `completed_at` read back from the immutable
accepted attempt — through a keyword-only `failed -> succeeded` transition that only an authorized
repair recovery may request. The failed attempt stays on disk, immutable, still named by
`last_attempt`, and still counted against the provider budget; only a status the failed attempt
had no business changing is put back. The job then goes through the same repair mechanism an
operator would drive by hand.

Budgets stay separate. The failed repair call consumed one provider attempt and one repair
*execution*; the recovery consumes another of each, plus one provider-recovery attempt. It
consumes no second QA repair *decision*, because QA chose that repair action once and the provider
simply failed to carry it out. The repair-decision count is derived as repair-kind attempts minus
the attempt slots a repair recovery authorized, so an outage cannot exhaust the repair allowance.
Translation recovery and repair recovery share one `--max-provider-recovery-attempts` allowance
per job: the question it bounds is how many times an unattended process may re-enter one job after
a provider failure, and that does not become two questions because the failed request was a
repair. Sharing it is also strictly the more conservative choice.

A recovered repair that succeeds and QA accepts ends the job. One that succeeds and QA *still*
rejects is a new situation — QA has judged new output — so it continues into the ordinary
auto-repair boundary, which authorizes another repair only if the repair budget permits. One that
fails again re-enters recovery until the recovery allowance or the job's attempt budget runs out,
after which the job is reported `qa_rejected` with the verdict that stopped it, because a standing
rejection is the true fact about it and the lost repair call did not change that.

The decision record adds, beside every field a translation recovery records,
`recovered_execution_kind: repair` and a `repair` lineage block: the repair root attempt, the
failed repair attempt, the auto-repair decision that spent the QA decision being replayed (absent
when an operator drove the failed repair), the effective rejection's origin, file, and SHA-256,
the blocking finding codes that caused the repair, the rejected attempt's file, SHA-256, and
translation hash, the source, context, prompt, style-guide, and terminology hashes, the repair
prompt version, the output contract, both message digests, a digest binding the whole derivation,
and the repair decisions charged so far against the repair allowance. Every field is a hash, a
path, an integer, or a code; no derived repair prose is ever persisted there.

`--dry-run` reports `would_recover_failed_repair` (or `would_refuse_repair_recovery`) with the
recovered execution kind and the repair root it would preserve. Planning one re-derives and hashes
the repair request, which reads the workspace; it writes nothing and makes no model call.

## Translation Repair

`repair-translation-job RUN_ID JOB_ID --provider PROVIDER` explicitly continues one
provider-successful `qa_rejected` job. It verifies the rejection sidecar and referenced bytes, claims
the next monotonic attempt, and sends the immutable source, rejected translation, machine-readable
QA findings, relevant frozen terminology decisions, and frozen prompt/style constraints through the
ordinary `fake`, `openai`, `codex-cli`, or `claude-code` provider boundary. The request requires minimal edits,
exact paragraph conservation, exact canonical terminology, and prose-only output.

The derivation is versioned by the contract the rejected draft was produced under, and the two
versions are separate on purpose. `REPAIR_PROMPT_VERSION` 1.0.0 is frozen forever, because bounded
provider recovery re-derives a failed repair and proves it byte-identical to the one that failed;
changing that derivation would make every historical repair irreproducible and therefore
unrecoverable. A draft produced under the numbered paragraph contract uses
`MARKED_REPAIR_PROMPT_VERSION` 2.0.0 instead, which re-attaches the same markers to both the source
and the rejected draft, proves the re-attachment conserves both inputs, restates the marker rules
in the system message, and carries the frozen previous context that the original derivation omitted
— the omission that let a whole-chunk repair rewrite an opening paragraph without seeing what
preceded it. A marked draft that does not carry one block per source paragraph is refused rather
than re-marked, because inventing the identities would be exactly the guess the contract exists to
eliminate.

Repair reuses reservation, attempt validation, QA, and acceptance publication. It never changes the
original attempt or rejection evidence and is bounded by `max_attempts`. It records
`initiator: operator`, which is what distinguishes it afterwards from the same mechanism driven by
the worker's auto-repair policy; only an automatic decision writes a policy record. `--dry-run` validates eligibility and reports structural lineage without
claiming, writing, contacting a provider, or exposing prose or claim tokens.

A rejected draft carried forward from a historical run (acceptance origin
`historical_reevaluation`) can be repaired the same way. The effective rejection is resolved through
the carry-forward record — its acceptance sidecar, the record's hash, the carried translation, and
the historical attempt the record names, each hash-verified — and the historical attempt supplies
the output contract. The repair attempt is the job's attempt 001 with
`repaired_from_carry_forward: 1` instead of `repaired_from_attempt`; its acceptance is published in
`acceptance/001` like any attempt's, and a delivered repair drops the job's carry-forward pointer the
way it drops a structural-repair pointer, leaving the record on disk as lineage. Only this explicit
operator command may repair a carried draft: the worker's auto-repair, structural repair, and
automatic provider recovery all refuse it, so a provider failure during such a repair leaves the
rejection standing for an operator decision rather than being replayed.

## Translation QA re-evaluation

`reevaluate-translation-job RUN_ID JOB_ID --attempt N` is the explicit provider-free path for
reassessing an immutable provider-successful attempt after a QA engine correction. It requires and
hash-validates the original acceptance evidence, persisted attempt metadata and translation, frozen
snapshot identity, source, frozen terminology when present, and snapshot-pinned prompt and style
resources. Mutable current terminology is never substituted for the snapshot's frozen copy.

The workflow executes current versioned terminology and structural QA locally. It publishes
`jobs/<chunk>/reevaluations/NNN/evidence.yaml` atomically, then updates the job's effective
`acceptance_status`, `accepted_attempt`, acceptance origin, latest re-evaluation pointer, and derived
run counters under the queue lock. Provider status and attempt counters do not change. A valid result
is mechanical `accepted`; an error result is `qa_rejected`; warnings remain nonfatal. Neither result
is review or approval.

Re-evaluation identity hashes the run/job/attempt identity, exact attempt/source/translation,
historical acceptance, frozen terminology/prompt/style hashes, and all QA engine versions. Repeating
identical inputs safely reuses the matching evidence and performs no write when the effective pointer
already agrees. A future QA version produces a new identity and next sequence. Use `--dry-run` for a
complete read-only plan and `show-translation-reevaluations` to inspect historical versus effective
status and versioned history without prose.

```bash
book-translator reevaluate-translation-job RUN_ID JOB_ID --attempt 1 --dry-run
book-translator reevaluate-translation-job RUN_ID JOB_ID --attempt 1
book-translator show-translation-reevaluations RUN_ID JOB_ID
```
## Accepted translation reuse

`book-translator reuse-accepted-translations NEW_RUN --from-run OLD_RUN --dry-run` performs a
read-only, prose-free comparison. Removing `--dry-run` explicitly publishes compatible jobs; `--job`
limits inspection to one job or chunk ID. Reuse is never automatic.

Compatibility is decided on *effective job inputs*, not on whole-resource equality. Every frozen
resource reaches a provider through exactly one channel — the deterministically rendered system and
user messages — so request identity, not resource identity, is the safety property. The tier
`exact_rendered_request` requires exact equality of source SHA-256, context SHA-256, source and
target languages, output-contract version, and both rendered message SHA-256 digests, and it
additionally re-verifies that the rendered message bytes behind those digests are still present and
unchanged on both sides. Context is treated as material; a context mismatch always blocks reuse.
Snapshot/job and attempt/acceptance/reevaluation lineage and every referenced immutable hash must
also validate, the snapshot workflow must match, and the new run's frozen terminology resource must
be intact.

Whole-resource differences — terminology, prompt, glossary, style-guide, and source-anomaly
hash/version (including equal absence), prompt-package renderer version, and chapter chunk count —
are reported as *observed resource drift*. They never block on their own, because a global resource
edit that never reaches a job's projection cannot have changed that job's request; when the tier is
`exact_rendered_request` they are recorded as waived, with the digests that prove the waiver. This
also closes a gap in the old rule: the open-question registry is rendered into the prompt but is not
a package resource reference, so whole-resource equality never proved request equality anyway.

When a rendered message digest does differ, reuse is blocked and the difference is attributed to the
framed user-message sections whose content digests changed (`effective_glossary_data_changed`,
`effective_source_text_changed`, and so on). Attribution reads only the section headers' digests, so
no framed content is inspected or reported. No second tier permits reuse across a changed rendered
request: a different request produced a different translation, and only current QA — not a
compatibility rule — can speak to whether the old output still holds.

Only the effective historical outcome is considered. Initial or repair acceptance and acceptance
established by the latest QA re-evaluation are eligible. Rejected, unassessed, failed, ambiguous,
invalid, corrupt, stale, or incomplete evidence is blocked. Publication copies no history: it creates
new-run reuse evidence and a byte-identical private translation, runs current terminology and
structural QA against the new run's frozen snapshot resources (the content-addressed frozen registry,
not the mutable config path the manifest names), then records accepted or QA-rejected normally. A
QA-rejected reuse is retained as a reused rejection; no provider is ever contacted to replace it.

Reuse engine 1.2.0 judges the reused output with the same acceptance battery a fresh provider
attempt faces — terminology, structural QA with the frozen source-anomaly registry, and paragraph
alignment QA under the output contract the historical attempt was produced against — and records
the alignment QA version and report. Its acceptance evidence is published inside the reuse record as
`jobs/<chunk>/reuse/001/acceptance.yaml`, staged in the same atomic directory as the evidence and
translation, and never in `acceptance/001`: that slot belongs to provider attempt 001, and the
worker refuses to adopt a sidecar there that names any other draft. Reuse published by engine 1.1.0
keeps its `acceptance/001` evidence and stays valid.
## Historical rejected-output re-evaluation

`book-translator reevaluate-historical-output NEW_RUN --from-run OLD_RUN --job JOB --attempt N`
carries one historical provider-successful output — QA-rejected, or accepted but with a request
that moved — into a newer run and assesses it
under that run's frozen QA, without contacting a provider. It is deliberately a separate command
from same-run `reevaluate-translation-job` (which needs an attempt inside the run being assessed)
and from `reuse-accepted-translations` (which consumes only effectively accepted outputs). One job
per invocation; `--dry-run` reports the complete decision and writes nothing.

Accepted-translation reuse can demand a byte-identical rendered request. These candidates are the
opposite case: they are worth revisiting *because* terminology QA moved after the output was
produced, so an identical-request rule would refuse every one of them. The separate policy
(`historical_reevaluation_policy`, version 1.0.0) therefore classifies into four tiers, of which
only the first two are eligible:

- `exact_rendered_request` — request contract and both rendered message digests identical.
- `governed_resource_drift` — the rendered user message differs only in the framed `glossary-data`
  projection; every other package resource is byte identical; the terminology resource is the one
  that moved; that move is a contiguous chain of *published governed* registry edits (decision
  corrections and applied remediation plans, resolved with `explain_registry_drift`); and no locked
  or user-owned canonical decision reaching this job changed inside it.
- `contract_changed` — source, context, languages, system message, or output contract differs, or a
  contract-bearing framed section (`translation-prompt-rules`, `style-guide`, `previous-context`,
  `source-text`) changed. Always blocked.
- `unsafe_drift` — a changed projection that cannot be attributed to a governed terminology move,
  ungoverned non-terminology resource drift, locked or user-owned terminology drift, or an
  unrecognized framed section. Always blocked.

Eligibility for the governed tier rests on lineage and current QA, never on a term-specific
exception. The historical attempt's own `output_contract` is compared against the new package's, so
a re-evaluation cannot silently cross an output-contract change.

The historical side must be provider-successful, unambiguous, and hash-valid on request, response,
and translation. Policy and engine 1.1.0 admit an *effectively accepted* output only when its
rendered request is not identical: an identical request is refused with
`historical_output_reusable_exactly` and belongs to accepted-translation reuse, while a moved request
is exactly what reuse must refuse and what current QA must judge again.

Policy 1.1.0 also governs a series-policy book. There the resource projection is the framed
`terminology-policy` section (the `glossary-data` section must then be unchanged), the resources
allowed to move are the frozen series policy and the registry derived from it, and every other
resource — prompt, glossary, style guide, source anomalies, the chunk's reference-context package,
and the context rendering contract — must be identical. "Governed" means a contiguous chain of
published series-policy amendments from the historical policy file to the new one. The locked-drift
rule becomes a term-level classification (`evaluate_series_term_drift`) of every changed term the
job's source or context reaches, under either policy version: a change is waived only when the
target and force are unchanged, authorized forms only widened, no added alias occurs in the job,
and every withdrawn alias that occurs in the job is now owned by another decided term that still
authorizes the target the historical request named for it. Anything else blocks
(`series_decision_changed`, `series_authorized_form_withdrawn`, `series_alias_added_reaches_job`,
`series_withdrawn_instruction_reaches_job`, `series_classification_changed`,
`series_evidence_changed`, `series_term_added_or_removed`), and so does any registry decision
reaching the job whose difference no changed term's spellings account for
(`ungoverned_registry_drift`). A withdrawn instruction that nothing still authorizes blocks because
the historical provider was told to render that spelling a way the project now calls wrong, and no
mechanical check can see whether it obeyed. The compatibility record adds both manifest SHA-256s and
both series-policy SHA-256s; the drift record carries `lineage_kind: series_policy`, the amendment
IDs, and the waived concept IDs.

Engine 1.1.0 judges with the full acceptance battery (terminology, structural with scoped
anomalies, and alignment under the historical attempt's output contract) and records
`alignment_qa_version` and `alignment_report`; `accepted` requires all three. The destination job must still be pending with no attempt, no acceptance, and no
reuse. Both snapshots' frozen terminology resources must be present and digest-verified; the new
run's frozen registry, not the mutable config path the manifest names, is what current QA runs
against.

Publication writes `jobs/<chunk>/carry-forward/001/` (a private byte-identical `translation.md`,
`evidence.yaml`, and — from engine 1.1.0, in the same atomic staging directory — `acceptance.yaml`;
engine 1.0.0 wrote `acceptance/001/evidence.yaml`, which that slot's provider attempt would collide
with), then sets the job to `succeeded`
with `acceptance_origin: historical_reevaluation`, `carry_forward_count: 1`, and `attempt_count`
unchanged at 0. No provider attempt is created and no provider is contacted. Historical QA findings
are copied into the new evidence rather than erased, so a rejection that persists — a real
paragraph-count defect, say — stays visible as `qa_rejected` under the new snapshot too. Repeating an
identical carry-forward is recognized and writes nothing.

```bash
book-translator reevaluate-historical-output NEW_RUN --from-run OLD_RUN --job 001-004 --attempt 1 \
  --dry-run
book-translator reevaluate-historical-output NEW_RUN --from-run OLD_RUN --job 001-004 --attempt 1
```

## Cross-snapshot run migration

`book-translator migrate-translation-run NEW_RUN --from-run OLD_RUN [--apply]` carries every usable
provider output of a historical run into a successor run in one provider-free pass, using only the
two governed boundaries above. Render the successor's prompt packages first: both boundaries compare
the historical rendered request with the successor's. Each successor job is classified from immutable
evidence (Translation Migration 1.0.0):

| class | historical evidence | carried by |
| --- | --- | --- |
| `reuse_exact` | effectively accepted, identical rendered request | accepted-translation reuse |
| `reassess_accepted_output` | effectively accepted, request moved under governed drift | historical re-evaluation |
| `reassess_rejected_output` | effectively QA-rejected, current frozen QA accepts it | historical re-evaluation |
| `carry_forward_for_repair` | QA-rejected (or accepted) output current frozen QA rejects | historical re-evaluation, then an explicit operator repair |
| `fresh_translation` | never attempted, or attempts produced no provider output | nothing; stays pending |
| `blocked_pending` | a usable output a governed boundary refused (reasons named) | nothing; stays pending |

`already_migrated` and `destination_not_pending` report successor jobs that are already complete or
in progress. A historical job that failed without output — including an attempt an operator resolved
as `provider_contacted_no_result` — is `fresh_translation`: its consumed attempts stay in the
historical run and the successor job starts at `attempt_count: 0` with its own full budget.

Without `--apply` nothing is written. With it, each carrying decision is re-planned under the
successor's queue lock against a fresh read of the successor, must reproduce the dry-run decision
exactly (reuse: same source attempt and translation hash; re-evaluation: identical evidence apart
from its timestamp), and is then published by the owning boundary; a changed decision raises and
writes nothing for that job. The historical run is only read, snapshot validations are computed once
per run and reused, and no provider code path exists. A rerun recognizes published jobs as
`already_migrated` and publishes nothing new.

## Semantic Coverage Judge

Structural QA can prove that the right paragraph is in the right place. It cannot prove that a
sentence inside that paragraph survived. `audit-semantic-coverage RUN_ID` is the provider-backed
stage that asks the narrower fidelity question, and it is the first stage in the chain that reads
meaning rather than shape.

The stage separates its concerns the way every other one does. `engine.py` builds tasks,
`rendering.py` builds the prompt, `parsing.py` validates the answer, `policy.py` decides what an
answer may mean, `providers.py` is the judge's role over the audited transport, and `workflow.py`
is the only layer with I/O. Four versions move independently: `SEMANTIC_JUDGE_ENGINE_VERSION`
(orchestration), `JUDGE_PROMPT_VERSION` (the question asked), `JUDGE_SCHEMA_VERSION` (the answer
contract), and `SEMANTIC_COVERAGE_POLICY_VERSION` (what an answer is allowed to mean).

### What the judge is asked

One request carries, per paragraph: the judge-facing identity `Pnnnn`, the English source
paragraph, the Persian candidate, at most one neighbouring paragraph on each side of both, and only
the canonical decisions that paragraph's own source text triggers. Neighbours exist to resolve
pronouns, speakers, and continuations, and the prompt forbids reporting a defect against a
neighbour's content. Nothing of the translator's role travels with the request: no translation
prompt, no style guide, no earlier verdict, no repair rationale, no other chapter, and never the
whole registry.

The question is deliberately narrow. "Is this a good translation" would mix fidelity with style and
return every sentence the model would have written differently. The prompt asks only whether all
materially relevant source meaning is represented faithfully without unsupported additions, and it
lists the tolerances before the defect vocabulary: sentences merged or split inside the same
paragraph, clauses reordered for Persian word order, pronouns left implicit, compression that keeps
the meaning, idiomatic rendering, canonical terms inflected, and a shorter Persian paragraph are
all correct translation and are never findings.

### The answer contract

Strict JSON, validated against the exact identities the request named. Each paragraph gets a
verdict of `PASS`, `REVISE`, or `INCONCLUSIVE`, a confidence, and findings drawn from a closed
vocabulary: `meaning_omitted`, `meaning_added`, `factual_change`, `action_changed`,
`negation_changed`, `modality_changed`, `number_changed`, `entity_relation_changed`,
`dialogue_content_changed`, `speaker_attribution_risk`, `temporal_relation_changed`, and
`causal_relation_changed`. A finding carries a severity, a short source-local evidence fragment, a
one-sentence explanation, and its own confidence.

The parser fails closed. An unknown paragraph identity, a missing verdict, a duplicate verdict, a
code or severity outside the vocabulary, a `REVISE` with no finding, a `PASS` with one, an
over-long evidence fragment, a preamble around the JSON, or a mismatched schema version is a
provider failure and never a verdict. A wrapping code fence is the one tolerated deviation, because
it is a formatting habit rather than a contract failure.

### What an answer may mean

| severity | material at confidence | meaning |
| --- | --- | --- |
| `HIGH` | 0.60 | plot fact, action, negation, number, identity, causal relation, or critical dialogue changed |
| `MEDIUM` | 0.70 | descriptive, relational, dialogue, or motivation content changed or lost |
| `LOW` | never | nuance lost with little reader consequence |

A judge `REVISE` whose findings are all immaterial becomes `PASS` with advisories recorded, which
is the single lever that stops a merely fussy judge from costing anything. A judge `PASS` is never
promoted into a `REVISE`, because the policy holds no evidence the judge lacked. `INCONCLUSIVE`
stays `INCONCLUSIVE`: it is not a pass, not a failure, and not an automatic escalation, and a later
orchestration stage decides it with the evidence in front of it.

### Evidence

Immutable, published outside every run directory under
`workspace/audits/semantic-coverage/<run>/NNN/`, with `evaluation.yaml` and one
`chunks/<chunk>-rNN.yaml` per judged chunk and repetition. Each record hash-links the frozen source
and the accepted translation it read, names the identity mode, and records the provider, adapter,
requested and returned model, token counters, and latency per call. Message digests are kept rather
than messages, and the model's reasoning is never retained. The one book-derived text retained is
the short evidence fragment per finding, which is why the tree is owner-only and Git-ignored.

The stage mutates nothing: no acceptance, no status, no counter, no run record, no translated byte,
no release. A run created before the judge existed carries no semantic evidence, and that absence
means *not judged* rather than *passed*.

```bash
book-translator audit-semantic-coverage RUN_ID --chunk 011-003 --batch-size 4 --dry-run
book-translator audit-semantic-coverage RUN_ID --chunk 011-003 --provider claude-code
book-translator audit-semantic-coverage RUN_ID --chapter-id 011 --repetitions 2 --provider claude-code
```

`--dry-run` plans the whole evaluation, including the exact number of calls it would spend, and
contacts nothing. `--repetitions` judges the same paragraphs more than once under distinct request
identities, which is how judge stability is measured rather than assumed. `--batch-size` sets how
many paragraphs one call answers.

## Production semantic gate

`run-semantic-gate RUN_ID --provider PROVIDER` is the production flow the Phase 2 judge feeds
into. It governs marked-contract runs created under a gate policy, judges every accepted
paragraph, routes each verdict, corrects only what a judge named, and stops at a bounded terminal
state. `SEMANTIC_GATE_POLICY_VERSION` (1.0.0) fixes coverage at 100% of paragraphs, batch size 4,
one wider-context second look per paragraph, and two provider revisions per window. It is frozen on
`TranslationRun.semantic_gate_policy_version` when a marked run is created; a run without it
predates the gate and is refused by this command.

```
marked translation -> marker validation -> alignment QA -> mechanical acceptance
        -> Semantic Coverage Judge
             PASS         -> semantic acceptance
             REVISE       -> bounded semantic revision -> independent re-judge
             INCONCLUSIVE -> one wider-context second look
        -> pass | unresolved
```

### Acceptance dimensions

Mechanical acceptance (`acceptance_status`) and semantic acceptance (`semantic_status`) are
separate job fields and never collapse into one. `semantic_status` is `pass`, `revise`,
`inconclusive`, or `unresolved`; absent means not judged. Release assembly refuses a gated run
unless every job is `pass`, reading the field only and importing nothing from the judge.

### INCONCLUSIVE routing

An `INCONCLUSIVE` paragraph is re-judged once, alone, with two neighbouring paragraphs on each
side instead of one (`SECOND_LOOK_CONTEXT_POLICY_VERSION` 1.0.0). The judged identity stays the
single paragraph and the prompt still forbids reporting a neighbour's content against it. A second
`INCONCLUSIVE` is terminal: the paragraph is `unresolved`, never accepted, never looped.

### Bounded semantic revision

The reviser (`semantic_revision/`, `REVISION_PROMPT_VERSION` 1.0.0) is a second provider role over
the same transport. It receives an explicit window of marked paragraphs, the material findings
that rejected them, the canonical terms they trigger, and one neighbour on each side, and returns
the window under its own markers. Its instruction is restraint: correct the named defects,
preserve every already-faithful byte, do not polish, do not touch an unnamed paragraph. It never
sees the judge's confidence, a verdict word, or an earlier reviser's explanation.

The default unit is one paragraph. A classified displacement uses an explicit window such as
`P0015..P0020`, never the whole chunk. The answer is spliced into the standing translation so
paragraphs outside the window come from the standing bytes, then measured
(`SemanticRevisionBounds`): changed indexes, character and word deltas, outside-window digests,
marker identity, terminology blocking codes before and after. Adoption requires all of: markers
valid for the window, nothing changed outside it, structural QA valid, alignment QA valid, no new
blocking terminology code. Every attempt, adopted or refused, is recorded at
`jobs/<chunk>/semantic-revision/NNN/revision.yaml`, and an adopted one carries `translation.md`
and supersedes the effective translation.

After adoption the window is re-judged by the ordinary judge with the ordinary prompt and no
knowledge of the revision. `REVISION_POLICY_VERSION` 1.0.0 allows two provider revisions per
window; a paragraph still `REVISE` afterwards is `unresolved`.

### Displacement

`DISPLACEMENT_CLASSIFIER_VERSION` 1.0.0 reads findings only. A maximal contiguous run of non-PASS
paragraphs is `semantic_boundary_displacement` when at least two of them carry both
`meaning_omitted` and `meaning_added` and those are at least half the run. The gate then tries a
whitespace-only re-segmentation first: the window's Persian is cut at sentence units and every
monotone assignment to the window's paragraphs is scored against the window's own expansion; the
best is adopted only when it beats every alternative by 0.25 in log-ratio cost, every block lands
inside the alignment band, and the concatenated non-whitespace content is unchanged. Otherwise the
window goes to bounded revision. A re-segmentation is a revision like any other and is re-judged.

### Evidence

Judge and second-look evidence: `jobs/<chunk>/semantic-qa/NNN/evidence.yaml` (the Phase 2
contract, with `look`, `context_radius`, and `context_policy_version`). Revisions:
`jobs/<chunk>/semantic-revision/NNN/`. The gate's own report:
`<run>/semantic-gate/NNN/report.yaml`, counts only. Both in-run evidence trees are owner-only and
Git-ignored because they carry evidence fragments and, for adopted revisions, translated text.

```bash
book-translator run-semantic-gate RUN_ID --dry-run
book-translator run-semantic-gate RUN_ID --provider claude-code
book-translator run-semantic-gate RUN_ID --job JOB_ID --provider claude-code
```

## Chapter-level Persian literary editing

`run-literary-edit RUN_ID --chapter-id CHAPTER ...` consumes only marked-contract jobs whose
mechanical and semantic acceptance are complete. Chapter selection is explicit and required; the
command has no implicit whole-book mode. A dry run resolves and hashes the accepted text, style,
profile, terminology, prior semantic evidence, and historical comparison release without contacting
a provider or creating a literary directory.

```
semantically accepted Persian chapter
  -> chapter-level literary edit
  -> exact marker/order validation
  -> structural, alignment, and terminology QA
  -> changed paragraphs only: independent Semantic Coverage Judge
       PASS         -> retain literary text
       REVISE       -> bounded literary fidelity correction -> re-judge
       INCONCLUSIVE -> one individual wider-context look
  -> unresolved after two correction rounds: restore faithful bytes
  -> assemble chapter
  -> read-only continuity review
  -> immutable literary evidence and blind A/B artifacts
```

The editor receives the semantically accepted Persian chapter, chapter-global paragraph identities,
the English chapter as consultation context, only relevant frozen terminology, the generic Persian
style contract, and the optional book profile. It does not receive translator reasoning, semantic
judge reasoning, repair rationale, or an earlier literary score. Its strict response covers every
editable identity exactly once in order with `edited_text`, a `changed` flag, and optional edit
reasons from a closed vocabulary. The engine derives the actual flag from byte equality and records
any normalization of contradictory provider metadata. Unknown fields are refused, including any
self-certified fidelity verdict or chain-of-thought field.

The complete chapter is one request whenever it fits the versioned limit. Otherwise the planner
creates deterministic overlapping sections: every paragraph is editable in exactly one core and may
appear as context in neighbouring sections. Only core answers merge. A request to move content across
paragraphs is explicit, and the named paragraphs stay byte-identical until a future governed workflow
exists.

Diff Governance 1.1.0 records changed identities, character and word deltas, edit categories,
terminology count differences, unchanged hashes, and one of `normal_edit`, `broad_edit`, or
`suspicious_rewrite`. Breadth is evidence, not an automatic rejection. Suspicious rewrites are judged
one paragraph per request with two neighbours each side; ordinary and broad edits use batches of four
with one neighbour. Marker, structure, alignment, and new terminology regressions fail locally before
model judgement.

Every retained changed paragraph must finish with a fresh semantic PASS whose evidence hashes its
source and exact candidate. Existing PASS evidence is reusable only for a final paragraph whose bytes
equal the faithful input. `REVISE` returns to the literary editor under a separate correction prompt
that carries only the material semantic findings and asks it to preserve as much prose improvement as
possible. Two rounds are allowed. `INCONCLUSIVE` receives one individual wider look. Anything still
unproven becomes `literary_edit_fallback` and the pre-literary bytes are restored. The editor never
judges itself and the ordinary semantic reviser is not asked to make prose literary.

The final continuity role reads only the assembled Persian chapter, terminology, and style/profile
context. It cannot rewrite and does not assess English-source fidelity. Findings use a closed
vocabulary for naturalness, narrator or character register, dialogue, pronoun/reference clarity,
translationese, transitions, terminology, and local discontinuity.

The immutable result is `workspace/literary-runs/<lr-id>/`. Per-chapter records contain the three
comparison layer references and hashes, paragraph governance records, mechanical reports, provider
provenance, semantic evidence, correction responses, continuity findings, and private final Markdown.
The top-level run separates editor, re-judge, correction, and continuity call counts, tokens, latency,
returned models, changed percentage, regression rate, and fallback rate. `blind/comparison.md` exposes
only Version A/Version B passages and the Persian rubric; its mapping lives in the separately hashed
`blind/answer-key.yaml`.

```bash
book-translator run-literary-edit tr-example-001 \
  --chapter-id 011 --chapter-id 012 --provider claude-code --dry-run
book-translator run-literary-edit tr-example-001 \
  --chapter-id 011 --chapter-id 012 --provider claude-code
book-translator run-literary-edit tr-example-001 \
  --chapter-id 011 --chapter-id 012 --provider claude-code \
  --reuse-literary-run lr-0123456789abcdef01234567
book-translator show-literary-run lr-0123456789abcdef01234567
```

This stage does not publish, approve, assemble a release, or build an EPUB. Reader scores are evidence
for comparison only and are never written into terminology automatically.

## Translation comparison

`compare-translation-runs FIRST_RUN SECOND_RUN --chapter-id CH [--first-release DIR]` puts two
runs' effective translations of one chapter side by side in hashes and counts, so a later stage or
a human reader can compare generations with their evidence attached. It resolves both sides
through the canonical acceptance resolver, re-runs only the provider-free validators in memory so
both are measured by the same installed engines, reads semantic and revision evidence where a run
has any, and records per chunk: effective translation file and hash, acceptance origin, provider
attempts, structural repairs, provider and model, structural and terminology blocking codes,
alignment codes, semantic status and call counts, revised paragraph identities, unresolved
identities, and which paragraph identities differ between the two sides. It contacts no provider,
judges no prose, and carries `literary_judgement: false` as a contract literal. The artifact is
written to `workspace/audits/translation-comparison/<second run>/<chapter>.yaml`.

## Final QA resolution

`book-translator audit-final-qa RUN_ID` and `book-translator finalize-translation-run RUN_ID` are
the last provider-adjacent stage: what to do about the jobs a finished run still rejects. The audit
half mutates nothing and is expected to run first.

The stage separates five concerns — analysis, policy, audit workflow, orchestration, CLI — and owns
one remedy of its own, the provider-free structural repair below, which lives in its own package.
`final_qa/analysis.py` and `final_qa/policy.py` are pure; `final_qa/workflow.py` is the only layer
with I/O. `FINAL_QA_RESOLUTION_POLICY_VERSION` (1.2.0) is versioned independently of the engines it
calls. From 1.2.0 a blocking paragraph alignment finding forces `operator_required` ahead of every
other verdict, including the free structural repair: a suspected boundary drift is a claim that
content sits under the wrong paragraph identity, and moving separators would answer it with a
guess.

### What the audit records

For every `qa_rejected` job: the effective acceptance attempt and its origin, the evidence and
attempt files with their hashes, the source and translation digests, the QA engine versions the
evidence recorded against the installed ones, the historical and the *current* blocking codes,
whether the finding still reproduces, the current terminology and structural verdicts, the budget
and any escalation already granted, the verdict, and the reasons behind it. For each competing
variant it additionally records the decision and its state, ownership, enforcement and lock, the
candidate's provenance, whether the canonical and the variant appear in the translation, source and
target occurrence counts, whether the effective registry still records the rivalry, and any rival
decision claiming the same target.

The report is written to `workspace/audits/final-qa/<run>/audit.yaml`; a finalization writes
`workspace/audits/final-qa/<run>/finalizations/NNN/report.yaml`. No book prose appears in either.

### Verdicts

`accepted_no_change`, `reevaluate`, `repair_structure`, `repair_translation`,
`resolve_terminology_then_reevaluate`, `resolve_terminology_then_repair`, `exhausted`,
`operator_required`. A job carrying several blockers takes the most conservative verdict any one of
them implies, because clearing the easy half of a rejection publishes an attempt that still cannot
be accepted — with one exception, and it is deliberate. `repair_structure` outranks everything,
because it costs nothing, cannot introduce a finding, and is re-diagnosed from scratch afterwards;
reaching for a paid remedy while a free proven one is on the table is never right.

Only the first four are actionable: `reevaluate` goes through `reevaluate_translation_job`,
`repair_structure` through `repair_translation_structure`, `repair_translation` through
`auto_repair_job` with automatic provenance. The rest are reported. A repair the provider never
delivered is not a verdict but a job state, and the orchestrator hands it to
`plan_provider_recovery` / `authorize_provider_recovery` — with a provider preflight taken lazily
and at most once per finalization, and dropped again the moment a recovered attempt fails — and
then repeats the repair. `--no-provider-recovery` reports it instead.

The two `resolve_terminology_*` verdicts are reported deliberately. The run's terminology resource
is frozen into its identity, so a registry answer cannot be projected back onto it; the governed
route is `freeze-translation-resources` -> `create-translation-snapshot` ->
`create-translation-run` -> `reuse-accepted-translations`.

### Competing-variant classes

| Class | Decided by | Verdict |
| --- | --- | --- |
| `canonical_identity` | the variant contains or derives from the canonical | not enforced |
| `retired_competitor` | the effective registry no longer records it | `resolve_terminology_then_reevaluate` |
| `historical_output_only` | only an older output attested it | `resolve_terminology_then_reevaluate` |
| `canonical_absent` | rival present, canonical nowhere | `repair_translation` |
| `locked_canonical_conflict` | a locked or user-owned decision claims the target | `resolve_terminology_then_repair` |
| `rival_ai_decisions` | one settled decision against a provisional one | `resolve_terminology_then_reevaluate` |
| `genuine_canonical_conflict` | two settled decisions disagree | `operator_required` |
| `insufficient_evidence` | nothing governed explains the occurrence | `operator_required` |

`canonical_identity` is handled by enforcement itself (terminology QA 1.5.0) rather than by this
policy, so such a finding never reaches a verdict. Since terminology QA 1.6.0 the same is true of a
variant the validator cannot tie to a governed source occurrence: the analyzer still records it —
the audit must be able to explain a finding's *absence* as well as its presence — but it is marked
unenforceable and produces no verdict. A locked or user-owned canonical is never mutated; the
translation is what conforms.

Where the analyzer has settled a finding's meaning it says so to the repair policy through the
per-finding `terminology_overrides` channel rather than by weakening that policy for every caller.
The only override currently issued is `competing_variant` -> `auto_repairable` for
`canonical_absent`.

### Provider-free structural repair

`structural_repair/` follows the ordinary stage layering: `engine.py` decides and renders and is
pure, `models.py` types the plan, `reporting.py` builds the persisted record, `workflow.py` is the
only layer with I/O. `STRUCTURAL_REPAIR_ENGINE_VERSION` is 1.1.0.

1.1.0 changes when a repair may be *adopted*, not how one is derived. A job whose rejected draft
was produced under the numbered paragraph contract is refused outright with
`marked_contract_forbids_deterministic_separator_repair`: such a draft always carries one block per
source paragraph, proven at the provider boundary, so a block-count difference means a stored
artifact is corrupt rather than that a separator is misplaced, and re-deriving separators would
satisfy the count by splitting a paragraph nobody proved is the right one. Independently, the
rendered repair is measured by alignment QA before adoption, and a repair that would introduce a
blocking alignment finding is refused as `repair_introduces:adjacent_boundary_drift_suspected`
instead of being published. Count equality is no longer sufficient evidence that a repair split the
right paragraph.

The engine segments both texts into non-blank lines, remembering for each source line which block
it belongs to. Equal line counts mean the correspondence is the identity — nothing is missing,
nothing is extra, and no search can improve on it — so the defect is `blank_line_geometry` and the
repair is the source's own blank-line placement re-applied. Unequal line counts are searched with a
monotone dynamic program over groups where one side is always a single line, scored by log-ratios
of word and character counts against the chunk's own observed expansion plus a paired-quotation-mark
term. The result is accepted only when the line delta is at most 4, there are at most 3 non-identity
groups, no group costs more than 1.25, and the best alignment beats every materially different
alternative — computed by re-running the search once per chosen group with that group forbidden — by
at least 0.50. Anything else is `line_count_divergence` or `content_divergence` and is refused.

`translation_line_merge` — several source lines written as one translation line — is never
deterministic even when the alignment is certain, because choosing where to break translated prose
is a judgement about meaning.

A repair re-emits the translation's own lines in their own order with separators taken from the
source, and the renderer asserts on its own output that the whitespace-stripped content is
unchanged. Publication writes `jobs/<chunk>/structural-repair/NNN/{translation.md,repair.yaml}`
atomically through a staging directory. The evidence names the superseded verdict and its digest,
the provider attempt the bytes still descend from, both translation digests, the full diagnosis, the
superseded blocking codes, and both QA reports re-run over the repaired text. Adoption sets
`acceptance_status`, `acceptance_origin = structural_repair`, and the
`latest_structural_repair_*` pointer, and leaves `acceptance_file` and `accepted_attempt` exactly
where they were — the same shape a provider-free re-evaluation leaves. `load_effective_rejection`
resolves the pointer and verifies the supersession chain, so every downstream reader sees the
repaired draft as the standing translation. A delivered provider repair clears the pointer, because
a new draft supersedes every derived verdict about the old one, and a structurally repaired job is
refused by `reevaluate-translation-job`, which would otherwise judge the draft it no longer stands
on. One repair per job: there is nothing left to repair after block correspondence is restored.

The contract refuses to publish a record whose repaired text does not restore block correspondence,
whose blocking codes are not a subset of the ones the standing rejection already carried, or whose
diagnosis is not deterministic.

### Exceptional repair-budget escalation

`--allow-repair-escalation` authorizes at most one grant per job for an `exhausted` job whose
blocking findings are all mechanically repairable. `jobs/<chunk>/repair-escalation/NNN/
escalation.yaml` is published before the ceiling moves and names the original ceiling, the attempts
granted, the attempt slot authorized, the budget already spent, the triggering findings, the
structural diagnosis proving no free remedy remained, and that the grant itself contacted no
provider. It is refused while a deterministic structural repair is available. Historical counters
are untouched.
The queue verifies the amendment: a job's attempt ceiling must equal its run's unless persisted
escalations account for exactly the excess.

### Dry run

`finalize-translation-run` defaults to `--dry-run`, which guarantees zero provider calls, zero
registry mutations, zero queue mutations, and zero acceptance mutations, and still reports the
planned verdict for every rejected job together with the step an applying run would take:
`reevaluate`, `repair_structure`, `repair_translation`, `recover_provider_failure`,
`escalate_and_repair`, or `report`. `--apply` is the explicit opposite. Without `--provider` it still performs every provider-free
resolution and reports each repairable job with a `provider_required` reason, so the free half of a
cleanup never depends on naming a provider.

## Chapter title translation

A chapter title is book *metadata*, not prose. It lives in `workspace/toc.yaml`, in chapter
metadata, and in every snapshot manifest, and it is the one part of a chapter the translation run
never translated: chunks carry paragraphs, headings do not appear in them. Until this stage a
finished book still had English headings over Persian text.

`translate-chapter-titles` closes that gap without reopening anything. Chapter Title Engine 1.0.0
(`book_translator.chapter_titles`) reads one run's frozen snapshot and the terminology that
snapshot froze, translates each chapter title, and publishes one immutable artifact. It never
reads a job, attempt, acceptance, or QA verdict for mutation and never writes one; the run is
opened read-only purely to learn which snapshot it froze.

### What reaches a provider

One call carries one English chapter title plus the canonical terminology decisions that title
actually contains — nothing else. No chapter prose, no chunk context, no other chapter's title, no
translation attempt. The output contract is `persian-chapter-title-text/1.0.0`: one line of plain
text.

Distinct titles are asked exactly once and the answer is reused for every chapter sharing it, so
one English title cannot receive two Persian titles. That is a property of the artifact rather
than a hope about the provider, and the document contract enforces it independently.

### Identity

The artifact ID `ct-<24 hex>` is the SHA-256 of canonical JSON over the *inputs*: engine and prompt
versions, book identity, languages, snapshot ID and manifest hash, frozen terminology hash, and the
ordered `(chapter_id, kind, number, title_en)` list. Addressing the inputs rather than the answer
is what lets a release ask "is there a title artifact for this snapshot" before knowing the titles,
and what makes a second run over the same inputs collide with the published answer instead of
quietly creating a rival. What came back is addressed separately by `content_sha256` over the
produced `(chapter_id, title_en, title_fa)` triples.

### Output

```
workspace/resources/chapter-titles/<ct-id>/
    chapter-titles.yaml
    manifest.yaml
    provenance.json
```

`chapter-titles.yaml` is the *answer* and carries no timestamp, so the same titles always
serialize to the same bytes and the same content address — that is what a release consumes and
what keeps a release reproducible. `manifest.yaml` is the content-addressed index and holds the
single timestamp. `provenance.json` is *history*: per title the source and translation hashes, the
origin, the provider, adapter version and model, the request digest, the snapshot and terminology
it was bound by, the canonical decisions enforced, the per-chapter QA verdict, and when it was
translated. History is deliberately not reproducible. No prompt text and no raw provider response
is stored anywhere.

### Title QA

`CHAPTER_TITLE_QA_VERSION` 1.0.0 is pure, provider-free, and runs over the assembled set *before*
a byte is written; an artifact with any error finding is refused outright. It checks that every
snapshot chapter has a title and no extra chapter appears, that no target title is empty, echoes
its source, lacks Persian script, or exceeds 200 characters, that both recorded hashes address
their own titles, that the recorded source title has not drifted from the snapshot's, that one
English title maps to one Persian title, and — through the installed `validate_terminology` — that
every canonical decision the source title contains survives into the translation. A finding names
a chapter and a code, never a title's text.

### Immutability

A published artifact is never replaced without `--force`, and `--force` replaces a destination only
after confirming its own manifest names the same artifact ID. Publication stages the artifact
beside its destination and swaps it in with the manifest written last. Loading verifies every
indexed file still hashes as published, so a file changed after the fact is detected rather than
consumed.

### Release overlay

Release assembly discovers the title artifact for its snapshot automatically, re-runs the full
title QA against the terminology *the release itself* froze, and refuses an artifact that no longer
holds rather than silently ignoring it. Two artifacts for one snapshot is an operator question and
fails closed. Heading precedence is workspace title, then artifact title, then source title:
a `title_fa` a person recorded in the workspace outranks a translated one, because it is a decision
rather than a derivation. Each chapter records which it used as `heading_source`
(`title_target`, `title_target_artifact`, `title_source`), and the release manifest carries
`chapter_title_artifact_id` and `chapter_title_artifact_sha256`. With no artifact present the
release behaves exactly as before. `--no-chapter-titles` reproduces that older behaviour, and
`--chapter-titles <id|path>` names one explicitly.

The release stage imports only `book_translator.chapter_titles.artifact`, which has no provider,
no writer, and no clock — a consumer of titles cannot acquire the ability to produce them, and an
export still makes zero provider calls.

## Release assembly

`export-translation-run` turns one completed translation run into a canonical Markdown book. It is
the boundary between translation and distribution: everything upstream of it decides *what the
translation is*, and everything downstream of it — EPUB, print, anything else — reads the release
rather than walking the run again. It is provider-free, mutates nothing, and is a pure function of
one frozen snapshot plus the accepted translations answering it.

Release Assembly Engine 1.0.0 consumes only immutable accepted evidence. It never touches a
translation run, a snapshot, a frozen resource, or the terminology registry, and it does not
re-judge a translation: acceptance already happened, and assembly is not another QA pass.

### Preconditions

Every one of these is checked before a byte is rendered, and any failure refuses the export:

- the run status is `completed`;
- the run's derived counters and status agree with its jobs;
- the run's frozen translation snapshot still validates against the workspace, and its identity
  and manifest hash still match the run;
- every job is `succeeded` with acceptance status `accepted`, and none is `qa_rejected`,
  outstanding, or provider-succeeded-but-unassessed;
- chunk coverage is exactly one job per frozen chunk, in frozen order, with no duplicate, no hole,
  and no chunk the snapshot never froze;
- every accepted translation file exists and still hashes to what its acceptance evidence recorded,
  answering the same source hash the snapshot froze;
- every accepted translation is canonical Markdown.

Effective acceptance is resolved the same way effective rejection is: the acceptance sidecar first,
then a provider-free QA re-evaluation, then a provider-free structural repair, each of which must
name the record it supersedes. A release therefore publishes the bytes the run actually accepted,
including repaired, re-evaluated, reused, and carried-forward ones.

### Assembly rule

A chapter is its heading paragraph followed by the paragraphs of its accepted chunks in frozen
order. The join is the canonical Markdown paragraph form the Document Chunk Engine already
conserves in the other direction — `parse_document` then `render_paragraph_texts` — so paragraph
order, scene separators, and every byte inside a paragraph survive untouched. Nothing is
normalized, re-wrapped, or rewritten after acceptance. A translation that is not canonical
Markdown is refused rather than tidied.

The heading is the only text the assembly adds, and it is never invented. Precedence is the
workspace's own recorded target-language title, then a governed chapter-title artifact, then the
source-language title verbatim; the manifest records which of the three each chapter used and, when
an artifact supplied any of them, that artifact's ID and content hash. `book.md` is the chapters
concatenated with exactly one blank line between them, so it is byte-identical to joining the
chapter files with a newline.

### Output

```
workspace/releases/<release-id>/
    manifest.yaml
    book.md
    chapters/000-prologue.md, 001-chapter.md, ...
    metadata.yaml
    terminology.yaml
    translation-report.yaml
    provenance/translation-lineage.json
```

A numbered chapter is named `<number>-<kind>.md` so a release directory sorts into reading order;
an unnumbered one keeps its canonical chapter ID. Two chapters that would collide refuse the
export. `metadata.yaml` and `terminology.yaml` are byte-exact copies of the resources the snapshot
froze, verified against the snapshot's own hashes before they are copied and never re-serialized.

`manifest.yaml` is content-addressed: it indexes every published file with its SHA-256 and byte
count, records the chapter inventory, and carries a `content_sha256` over the sorted
`(path, sha256)` pairs. Only the manifest holds a timestamp, so re-exporting the same run
reproduces every other byte and the same content address. `provenance/translation-lineage.json`
records, per chunk, the source and translation hashes, the acceptance origin and evidence, the
attempt, the originating provider and how it was established, the QA versions, and the paragraph
offset where the chunk starts in its chapter — and no prompt or response material of any kind.

### Safety

`--dry-run` renders the entire release and writes nothing. Without `--force` an existing
destination refuses the export; with `--force` the destination is replaced only after its own
manifest is confirmed to name the same release, so `--force` re-exports a release and is never a
licence to delete a directory an operator merely pointed at. A release may never be written into
canonical workspace state. Publication stages the whole release beside its destination and swaps
it into place with the manifest written last, so an interruption leaves a staging directory rather
than a release that indexes files it does not contain.

## Release verification

`verify-release` is the gate between assembly and publication. It answers one question — may this
release directory be published — and it answers it from the bytes on disk rather than from any
claim the assembly made about itself. Release Verification Engine 1.0.0
(`book_translator.release_verification`) is the narrowest stage in the project: it opens every file
read-only, has no provider import and no path to one, mutates nothing anywhere, and writes only the
report an operator explicitly asks for.

### The seven checks

Each check counts what it examined against what the manifest said to expect, so "passed" can never
mean "found nothing to look at". Findings name a check, a code, and a subject — a file, a chapter,
a chunk — never a line of the book.

- **manifest** — the manifest parses as a `ReleaseManifest`, every indexed file exists and matches
  its hash and byte count, no published file is missing from the index, and `content_sha256` still
  addresses the index. A manifest that breaks its own contract is reported as
  `invalid_manifest_schema`, not raised: an invalid inventory is a verdict, and everything measured
  against it is then *skipped* rather than passed.
- **book_integrity** — `book.md` exists, decodes as UTF-8, is canonical Markdown, is byte-identical
  to its chapter files joined with a newline, and its paragraph and word counts match the manifest.
- **chapters** — every chapter file exists, no chapter identity or filename is published twice,
  sequences are contiguous, the count matches, each chapter is canonical Markdown with the recorded
  paragraph and word counts, and each opens with exactly the heading the manifest recorded.
- **translations** — the lineage covers exactly the released chunks, every chunk is accepted, none
  is duplicated, each chapter's chunk sequences are contiguous, and — the strongest of these — the
  chunks *tile* their chapter's paragraphs from paragraph 2 with no gap and no overlap, ending
  exactly at the chapter's recorded paragraph count. That is what makes "nothing is missing"
  checkable from the release alone rather than trusted.
- **titles** — when the manifest references a chapter-title artifact, that artifact is present and
  loadable, its content address is the one the release recorded, it still passes the installed title
  QA against the terminology the release published, and every heading marked
  `title_target_artifact` is exactly the title the artifact holds. A heading claiming an artifact
  the manifest does not reference fails.
- **terminology** — the release's copies of the frozen terminology registry and resolved book
  metadata hash to the resources the manifest names, and — when the canonical workspace is
  reachable — the workspace's own frozen copies still hash to the same thing.
- **provenance** — the lineage describes this release's run and snapshot, carries a record for every
  released chunk, and each record names both hashes, its acceptance evidence, its attempt, and a
  provider resolution consistent with the provider it recorded. A carried-forward or reused chunk
  must name its source run and job.

### Skipped is not passed

A check whose evidence is unreachable is `SKIPPED`, never `PASS`. In practice that is the
cross-workspace evidence — the chapter-title artifact — when a release is verified after being
copied away from the project. Skipped checks are listed in the report and printed in the table, and
a release with skipped checks has been verified only as far as the bytes in front of the verifier
allow. A release is `READY_FOR_PUBLICATION` exactly when no check failed and no error finding
stands; anything else is `NOT_READY_FOR_PUBLICATION` and the command exits non-zero.

### Reproducibility

Everything in the report except `created_at` is a pure function of the bytes on disk, and
`content_sha256` covers exactly that part. Two runs over an unchanged release produce the same
content address; a differing one is evidence that something moved. That holds for failing
verdicts too.

### Report

`--json` prints the report; `--output PATH` writes it, as JSON when the path ends in `.json` and
YAML otherwise. The report may never be written inside the release it judges — a release is
content-addressed by its own manifest, so dropping a report into it would fail the next
verification on an unindexed file. `--verbose` additionally prints what each check proved and every
recorded digest.

## EPUB builder

`build-epub` turns one verified release into an RTL EPUB 3. EPUB Builder 1.0.0
(`book_translator.epub`) is a *publication format converter* and nothing else. Its only input is a
release directory, and it makes no editorial decision: chapters, their order, their headings, and
their paragraphs all arrive already settled by the release. It never reads a translation run, job,
attempt, provider artifact, QA record, or snapshot — the package holds no import of any of them and
no provider import at all.

### The release must verify first

Release verification runs inside the plan, over the release the builder was pointed at. Anything
short of `READY_FOR_PUBLICATION` refuses the build outright and names the failed checks, so an
invalid manifest, a missing chapter, missing provenance, a hash that no longer matches, or a title
artifact that no longer holds all stop the book before a byte is rendered. The builder then adds
its own two demands: the release must carry book metadata it can describe the publication with, and
each chapter must open with exactly the heading the release recorded.

### Archive

```
mimetype                       (first entry, stored uncompressed)
META-INF/container.xml
OEBPS/content.opf
OEBPS/nav.xhtml
OEBPS/styles/rtl.css
OEBPS/chapters/000-prologue.xhtml, 001.xhtml, ...
```

Chapters are named by their canonical chapter ID, so an archive entry maps one-to-one onto a
release chapter. The package document is EPUB 3.0 with `page-progression-direction="rtl"`; every
document carries `dir="rtl"` and the book's language; `rtl.css` sets `direction: rtl` on `html` and
`body` with right-aligned text. The navigation document is a real EPUB 3 `nav` listing every
chapter, in order, under its target-language title.

### Conversion

A chapter is canonical Markdown, which makes the conversion total rather than heuristic. The first
paragraph is the heading and becomes the `<h1>`; every other paragraph becomes a `<p>` in published
order. A multi-line paragraph keeps its line structure with `<br/>` rather than being flattened —
verse and inscriptions are meaning, not accident. A scene separator keeps its literal text in a
`p.scene-separator`. The only transformation applied to text is the XML escaping the format
requires, so unescaping any document returns the release's bytes exactly.

### Determinism

Building one release twice produces byte-identical archives. Nothing is invented and nothing is
sampled from the environment:

- the book's `dc:identifier` is a UUIDv5 derived from the release's own content address;
- `dcterms:modified` is the release manifest's `created_at`, not the clock;
- every archive entry is stamped with a fixed timestamp and fixed attributes;
- `mimetype` is first and stored, and every other entry follows in sorted path order, so
  filesystem enumeration order never reaches the archive.

The build is addressed twice, for two different questions: `content_sha256` (and `epub-<sha256>`)
addresses the book *inside* the container, while the archive's own SHA-256 addresses the container
bytes.

### Validation before publication

The archive is rendered in memory, read back, and checked before it is written — a build that fails
its own validation writes nothing. The checks cover the container structure and entry order, the
mimetype, that every entry is valid UTF-8 and well-formed XML, that the package is EPUB 3.0 with
the release's title, language, author, a stable identifier and exactly one `dcterms:modified`, that
the manifest and spine carry the release's chapters in order, that the navigation lists every
chapter title in order, that each chapter carries its release title once in a right-to-left body,
that no chapter's bytes changed during archiving, and that the archive's content address is the one
the plan derived. `verify_epub_file` performs the structural half again on a finished file, without
the release.

### Safety

`--dry-run` renders the whole book and writes nothing. `--force` is required to replace an existing
EPUB. An EPUB may never be written inside the release it was built from — a release is
content-addressed by its own manifest, so a book dropped into it would fail the release's next
verification as an unindexed file. The default output is `output/<release-id>.epub`, which the
repository already ignores.

# Source quality audit

`audit-source-quality` discovers the canonical chapter inventory and reads source page markers,
cleaned Markdown, normalization reports, chunk source/metadata, and chunk reports. Pure detectors
classify page/paragraph/dialogue boundaries, joined or split tokens, heading structure, duplication,
chunk boundaries, and conservation. Findings separate severity from confidence and identify the
likely owning layer. The command is provider-free, never translates, and writes nothing unless an
operator supplies `--output`.

Run it before freezing a full-book translation snapshot. A high-confidence conservation error blocks
snapshot readiness; findings never authorize automatic source or normalization mutation.

Resolution is evaluated against current hash-valid lineage. An exact recorded page-continuation join
resolves the associated upstream boundary and split-token evidence by normalization while preserving
the finding. Missing or invalid normalization yields `cannot_evaluate`; a separate conservation
finding blocks readiness. Normalization/chunk findings remain unresolved until their owning layer no
longer emits them. Medium-confidence structural findings are review evidence and do not block.

A chapter is translation-ready only when source metadata/hash, normalization artifact/report/hash,
chunks/report/hashes and exact chunk reconstruction are valid and it has no unresolved
high-confidence blocker. Missing normalization or chunks always blocks through conservation.
# Source anomaly policy

The operator-authored `config/source-anomalies.yaml` is distinct from extraction and normalization.
Audit can suggest or link an exact entry but cannot register one. Local defects are nonblocking only
when conservation and structure remain recoverable; missing pages or paragraphs, major unreadable
regions, and structural corruption block translation. Source bytes remain unchanged. Frozen relevant
metadata tells translation to preserve uncertainty without guessing, while QA retains all general
structural checks.
