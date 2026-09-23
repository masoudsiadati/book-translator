# Terminology Architecture v2: series policy, book occurrences, cross-book reference

This note records the design decisions behind the series-aware terminology layer added for the
second book of a series translated in one project. It complements `ARCHITECTURE.md` and
`docs/pipeline.md`, which carry the binding contract; this document explains why the boundaries
sit where they do.

## The problem

The first book was translated under one per-book terminology registry (`config/terminology.yaml`)
that conflates three different kinds of data: the decisions the translation must honour, the
occurrence evidence that says where a term appears in *that* book, and the discovery candidates the
AI pipeline produced. That conflation was tolerable for one book. It fails for the next one:

- A second book has its own occurrences (its own chapters and chunks) but must honour the same
  series decisions. Copying the first book's registry would present first-book occurrence
  identities as second-book evidence; not copying it would lose every settled decision.
- The professional Persian translations of the first two series volumes exist as frozen,
  read-only bilingual corpora with their own retrieval tool. They are *observations* about what a
  translator did, including OCR noise and inconsistency, and must never be rewritten into
  decisions or edited to match a project preference.
- The snapshot planner silently fell back to the root `config/terminology.yaml`,
  `config/terminology-questions.yaml`, and `config/source-anomalies.yaml` when a book config left
  them unset. A book-scoped workspace could therefore freeze another book's registries as its own.

## Three layers that must not be confused

| layer | owner | mutability | content |
| --- | --- | --- | --- |
| historical corpus evidence | external (`reference-db/book1`, `reference-db/book2`, V2.1 freezes) | immutable, read-only by policy and by `mode=ro` | what the published translations actually used, clean and anomalous alike |
| retrieval | `series_terminology/retrieval.py` over the external `cross_book_retriever.py` | read-only, deterministic for pinned corpus bytes | evidence selected for one passage, with provenance |
| translation policy | `config/series/<series>/terminology-policy.yaml` | mutable, governed | what the current book must or should use, and why |

Historical evidence never changes because the policy chooses a different form. When they disagree,
the policy records the conflict beside the explicit decision and keeps the decision authoritative.

## Concept identity, and the one thing it cannot see

A concept is keyed on its exact source term. That is deliberate: every matcher downstream is
byte-exact, so folding case or inflection into the key would hide real occurrences. `_key` in
`bootstrap.py` folds case when *bucketing* imported sources, which is why `The One Power` and
`the One Power` arrive as one concept.

What the key cannot see is that a leading English article is grammar, not identity. An explicit
project decision on `the Dark One` and a corpus promotion of `Dark One` therefore became two
concepts with two targets for one thing in the story, and the same happened to `The One Power` and
`One Power`. Both survived into the derived registry as live decisions — one exact, one contextual,
both required. Every prompt path rendered both; every enforcement path honoured only the longest
matching span. A repair told to write `اهریمن` for `Dark One` applied it to the occurrences nested
inside `the Dark One` and was rejected with `canonical_term_missing` for exactly that.

`terminology/concept_precedence.py` resolves this and nothing else. Two concepts are the same
concept only when their source terms are equal after folding case and removing one leading article.
The rule is intentionally too narrow to reach `Dragon`/`false Dragon`, `Two Rivers`/`Two Rivers
folk`, `angreal`/`ter'angreal`, `Moiraine`/`Moiraine Sedai`, `Rand`/`Rand al'Thor` or any other
nested pair that names something different; containment alone never collapses two concepts, because
collapsing them would silently retarget real decisions. Within a group the order is explicit-user
`LOCKED` > explicit-project `LOCKED` > other `LOCKED` > `STRONG_REFERENCE` > weaker evidence, and
variants that already agree on a target are not a conflict at all.

Three points enforce it. **Import** refuses to promote a corpus form that is an article or case
variant of a locked term — the mirror of the pre-existing guard that refuses a promoted compound
spelling a locked component another way, which only ever looked in the one direction.
**Derivation** collapses a group onto the governing decision, turning the displaced spelling into
an alias that stays matched and is enforced to the one target, and moving the displaced evidence
onto the governing concept, where a clean narrative rival of a locked target becomes an enforceable
competing variant — never an authorized target form, because these are competing historical
renderings and not grammatical realizations. **Selection** is shared: `applicable_decisions` uses
the same `source_term_matches` as the QA validator.

An existing conflict is retired, not deleted: a governed amendment withdraws the displaced decision
to `OBSERVED_VARIANTS` with its concept, spelling, conflicts and every observed variant intact, and
`policy_concept_conflicts` reports the class so validation finds it rather than a rejected repair.

## Chosen boundaries

### A. Series policy (`SeriesTerminologyPolicy`)

A versioned resource with a `.metadata.yaml` sidecar like every other frozen input. Each term has
exactly one status:

- `LOCKED` — mandatory; only trivial grammatical adaptation is allowed. Provenance is
  `explicit_user` (a user decision) or `explicit_project` (the project's mandatory glossary).
- `STRONG_REFERENCE` — strongly preferred; provenance `promoted_reference` carries the evidence
  summary that justified promotion under a named, versioned promotion rule.
- `OBSERVED_VARIANTS` — unresolved alternatives; evidence only, never canonicalized.

Every term keeps its observed variants with per-variant support (`book1`/`book2`, narrative or
printed glossary, clean or OCR-suspect, counts), so a status is always explainable from the record.

Promotion is not frequency. A form is promoted only when at least two independent clean evidence
sources agree (a source is one book's narrative or one book's printed glossary) and no rival clean
form reaches a material share of its support. Single-source evidence, glossary-only evidence, and
disagreeing books stay `OBSERVED_VARIANTS` with their tier visible in the rendered evidence.

Explicit decisions are imported from the sources the project already governs: user-owned registry
decisions and the mandatory glossary. Prior-volume AI decisions are imported as `prior_volume`
evidence variants, never as decisions: they inform, they do not bind.

### B. Book occurrences (derived registry)

The book's terminology resource is a *derived* `TerminologyRegistry` under
`workspace/books/<book>/terminology/registry.yaml`, projected from the frozen policy over the
book's own canonical chunks. Its candidates carry only this book's chapter/chunk occurrences; its
decisions are the policy projected onto the existing enforcement vocabulary:

| policy status | projected decision | QA effect |
| --- | --- | --- |
| `LOCKED` | exact enforcement, user-locked when `explicit_user` | `canonical_term_missing` / `competing_variant` are blocking |
| `STRONG_REFERENCE` | contextual enforcement, no competing variants | `canonical_term_unverified` is a non-blocking warning |
| `OBSERVED_VARIANTS` | candidate only, no decision | no finding |

A decided term's `allowed_target_forms` are projected onto its decision unchanged; corpus evidence
for an authorized form is agreement, so it is neither a competing variant nor a recorded conflict.

This projection is the explicit compatibility path: the worker, repair, semantic gate, title
translation, release assembly, and final-QA boundaries read a `TerminologyRegistry` exactly as
before, so the first book's frozen artifacts and the QA engine are unchanged. Series provenance
travels in candidate evidence notes and decision contextual notes rather than in new registry
fields, because adding a field would change the semantic identity of every existing registry.

### C. Cross-book reference retrieval

The external retriever is loaded from its configured path, executed from bytes whose SHA-256 is
pinned in `config/series/<series>/reference-corpus.yaml`, and never modified. Corpus files are
likewise pinned by hash and verified before any retrieval. Only the configured corpora are opened;
the configuration lists the legacy databases it excludes.

Retrieval runs once per chunk as a governed batch (`prepare-reference-context`) and publishes a
content-addressed `ReferenceContextPackage` per chunk under
`workspace/books/<book>/resources/reference-context/<sha256>.yaml`, indexed by a mutable
`reference-context/index.yaml`. Packages keep only clean evidence: reference-only TM, anomaly
records, uncertain glossary extractions, and ambiguous common-word matches are counted in an
exclusion summary and never carried. Every retained item records book, kind (narrative or printed
glossary), quality, counts, and corpus identifiers.

Prompt rendering and validation read the frozen package, not the live corpus. That keeps rendering
deterministic and offline, and confines the slow external lookup (the corpora live on a slow
mount) to one resumable batch.

### D. Effective translation constraints

`series_terminology/context.py` builds one deterministic `EffectiveTerminologyContext` for a chunk
from the frozen policy, the derived registry, and the chunk's reference package, and renders it as
a separate length-framed `terminology-policy` section with four labelled parts:

1. `LOCKED CONSISTENCY TERMS` — mandatory; no re-transliteration, paraphrase, modernization, or
   reinterpretation.
2. `STRONG REFERENCE EVIDENCE` — strongly preferred unless immediate grammar or meaning requires
   another form.
3. `OBSERVED VARIANTS` — contextual evidence only; no silent choice by raw frequency.
4. `REFERENCE CONTEXT` — analogy for terminology, phrase choice, register, and voice; not text to
   copy.

The legacy in-glossary "relevant canonical terminology" block is omitted when this section is
present so the same decision is never stated twice with two different force levels.

## Lineage

A snapshot that uses a series policy records, in its manifest identity: the frozen policy
(kind `series_terminology`), the frozen reference-corpus configuration (kind `reference_corpus`)
plus the verified external identities (each corpus path, SHA-256, corpus version, freeze manifest
hash, and the retriever's SHA-256 and schema version), the frozen reference-context index
(kind `reference_context`, which pins every per-chunk package hash), the derived book registry
(kind `terminology`, as before), and the terminology-context contract version. Manifests without a
series policy hash exactly as before, so no existing snapshot changes identity.

Validation proves the frozen bytes of every one of those inputs and every listed package.
External corpus drift after freezing is reported as non-current lineage, not as corruption: the
frozen packages are what the snapshot used.

Prompt-package identity includes the policy hash, the per-chunk package hash, and the context
contract version, so a later policy edit produces a different package identity rather than
changing a rendered one.

A governed policy edit after freezing is an amendment (`amend-series-terminology`): all-or-nothing,
never creating a term, never overriding an explicit user decision, and published as an immutable
record before the policy file moves. A chain of amendment records connecting two frozen policy files
is the evidence cross-snapshot historical re-evaluation requires before it waives a changed
`terminology-policy` projection, and it then classifies every changed term the job reaches rather
than trusting the chain alone.

## Compatibility

- Legacy root workspaces resolve terminology exactly as before, fallbacks included.
- Book-scoped workspaces never probe root config files. They must name a series policy and
  reference-corpus configuration (or an explicit book registry); ambiguous or mixed lineage fails
  closed at snapshot planning.
- The first book's registry, glossary, snapshots, runs, literary runs, releases, and EPUB are not
  read for writing by any new command and are not migrated.

## Implementation plan

1. Models: policy, reference-corpus configuration, reference-context package and index; manifest,
   snapshot, prompt-package, and book-config extensions; schema regeneration.
2. `series_terminology` package: identity, policy persistence, promotion rule, corpus reader,
   policy import, registry derivation, retriever adapter, context builder and renderer, workflow.
3. Snapshot planner: book-scoped lineage resolution, freezing, validation, identity format.
4. Prompt renderer 1.4.0: framed `terminology-policy` section, identity fields.
5. CLI commands, documentation, benchmark, real-data validation on book 6 chunks.
