# Changelog

This changelog follows a Keep a Changelog-inspired structure. The project uses semantic versioning for
the application; earlier milestone dates were not recorded and are not invented here.

## [Unreleased]

### Added

- **Provider-free EPUB ingestion (EPUB Import 1.0.0).** `import-epub [EPUB] --book BOOK` resolves
  `META-INF/container.xml`, the OPF manifest and authoritative spine, and optional NCX navigation;
  discovers prologue, numbered chapters, and epilogue; extracts paired NCX chapter titles; merges
  consecutive split resources; and excludes title/publication pages, maps, contents, glossaries,
  and about matter. HTML/XHTML becomes canonical Markdown with paragraph order, dialogue
  punctuation, and emphasis preserved while navigation and page decoration are removed. Imported
  chapters publish the existing immutable/hash-tracked source and metadata contract, so unchanged
  normalization and chunking stages consume them. Book configuration now accepts exactly one of
  `pdf_path` or `epub_path`; existing PDF configurations and artifacts are unchanged.

- **Line-layout PDF ingestion (Line Layout 1.0.0).** `discover-toc --text-headings` discovers the
  narrative structure of a PDF that has no embedded TOC from its own heading lines (`PROLOGUE`,
  `CHAPTER [n]` with the number on the same or the next line, `EPILOGUE`, the first `GLOSSARY`),
  records sub-page `start_block`/`end_block` anchors because such PDFs begin chapters mid-page,
  and marks the TOC `layout: line_blocks` with the running-head keys it derived. A chapter number
  the OCR lost is inferred from sequence and says so in `source_toc_title`; an explicit number
  that breaks the sequence fails closed. Extraction of a line-layout TOC rebuilds paragraphs from
  one-block-per-line pages, removes counted page furniture (`Page N` footers, print page numbers,
  running heads, tolerating one OCR edit), and rejoins a paragraph across a physical seam only when
  the seam is structurally a continuation; chapters record `extraction_version:
  line-layout/1.0.0`. Embedded-TOC books are unaffected and their TOC files keep their bytes.

### Changed

- **Bounded nightly translation now covers at most three narrative units per invocation.**
  `run-nightly-translation RUN_ID --max-units 3` remains sequential, resumes from the earliest
  untranslated source-order unit, treats usable `qa_rejected` attempts as translated coverage
  without repairing them, performs no semantic QA, preserves the local 22:00–05:00 gate, and
  fails closed when another run-scoped nightly coordinator holds the lock.

- **Series terminology derivation is memoized per process.** Snapshot validation re-derives a
  series book's registry to prove the frozen one still matches; the result is now cached under a
  digest of the policy, book ID and chunk texts, so repeated validations in one worker process no
  longer repeat ~60 s of regex counting. Results are deep-copied and byte-identical.

- **Release-time semantic-gate waiver (Release Waiver 1.0.0).** `authorize-release-waiver RUN
  --reason R [--apply]` records an explicit, content-addressed authorization to publish a run whose
  jobs the semantic judge never saw, and `export-translation-run --semantic-waiver ID` presents it.
  The waiver names one run, one snapshot manifest and one gate policy version, states the semantic
  counts (`judged_pass` / `judged_not_pass` / `not_judged`) and mechanical counts it was authorized
  against, and is addressed by all of it, so changing anything changes its identity. At release
  time it is re-derived against the live run and refused when it names another run, another
  snapshot, another policy, or counts that have moved. It admits only jobs whose `semantic_status`
  is absent: a job that was judged and did not pass is repaired, never waived, and authorizing a
  waiver over one is refused outright. `semantic_status` is never written, so unjudged work stays
  unjudged. Without a waiver the release path refuses such a run exactly as before, and a run under
  no gate policy is unaffected. The release manifest and the translation report state the waiver,
  its reason, who decided it, the unjudged count and `semantic_coverage: waived_not_judged` in
  their own bytes; `ReleaseTranslationReport` refuses to describe unjudged work released without
  one. All new fields are omitted when absent, so existing releases keep their bytes.

- **Provider-free chapter-title revision (Chapter Title Revision 1.0.0).**
  `revise-chapter-titles PARENT --set CHAPTER=TITLE --reason R [--apply]` corrects named titles of
  a published chapter-title artifact without asking a provider for anything. Every other title is
  carried from the parent byte for byte, the parent artifact is never rewritten, and the result is
  published as its own immutable artifact. A translated artifact is addressed by its *inputs*, so
  a revision could not be addressed that way without colliding with its parent; a revision is
  therefore addressed by the parent plus the exact overrides it makes. Two new origins record what
  each title is — `inherited_title` for a carried provider answer, `operator_override` for a
  person's replacement, which keeps the title it superseded, the reason and `decided_by: user` —
  and a title a person decided stays theirs when a later revision carries it forward. The full
  title QA runs over the resulting set before anything is written, so a correction that would make
  two identical English titles disagree is refused. `ChapterTitleDocument`, `ChapterTitleManifest`
  and `ChapterTitleProvenanceRecord` gained the lineage fields, all omitted when absent so every
  published artifact keeps its exact bytes and address. Release discovery now orders a parent and
  its revision instead of failing closed on the pair: an artifact another artifact revises is
  superseded, and the head of the chain is selected; two unrelated artifacts still fail closed.

- **Targeted semantic revision (Targeted Semantic Revision 1.0.0).**
  `revise-translation-paragraph RUN JOB --paragraph N --code C --source-evidence E --reason R
  [--provider codex-cli] [--apply]` corrects exactly one operator-identified paragraph of an
  already-accepted translation. The operator names the paragraph and states the defect and never
  supplies replacement text, so this is not a text-replacement path. The window is one paragraph
  (`SemanticRevisionWindow(reason="single_paragraph")`), the answer is spliced by
  `splice_window`, which copies every other paragraph from the standing translation, and
  `measure_bounds` re-derives that nothing outside it moved. Adoption additionally requires the
  whole mechanical acceptance battery (terminology with the snapshot's occurrence exemptions,
  structural, alignment), no terminology regression, and an independent judge call on the revised
  paragraph returning PASS: the reviser never certifies its own answer. Planning writes nothing
  and contacts nobody; execution costs two provider calls, serial, and honours the existing
  two-revisions-per-paragraph budget. An adopted revision supersedes the translated bytes through
  `latest_semantic_revision_*` exactly as a gate revision does, leaving the accepted attempt, its
  acceptance evidence and every earlier record byte-identical; a refused revision is published as
  evidence and counted from disk, and deliberately does not move the job's pointer, because a
  pointer to a record that published no translation would make the job's own effective acceptance
  unresolvable. `semantic_status` is untouched: one judged paragraph is not a job-wide semantic
  verdict, so a release still requires the full gate.
  `TranslationSemanticRevisionEvidence` gains `defect_origin`, `operator_defect`,
  `revised_paragraph` (paragraph identity with the source and before/after Persian paragraph
  hashes), `snapshot_id`, `manifest_sha256`, `occurrence_exemption_ids` and `verification`, and
  `superseded_semantic_file`/`superseded_semantic_sha256` become optional because an operator
  revision answers a person rather than a judgement. Every field is omitted when absent, so
  existing gate records keep their bytes and verify under their own recorded versions; the gate
  engine, the reviser prompt, the judge and the revision policy are all unchanged.

- **Occurrence-specific terminology exemptions (Terminology Exemption 1.0.0).**
  `exempt-terminology-occurrence CHUNK --spelling S --occurrence N --rationale R --book BOOK
  [--apply]` records a user's adjudication that exactly one source occurrence of a governed
  spelling is not the term (sense `ordinary_english`), in
  `workspace/books/<book>/terminology/occurrence-exemptions.yaml` with versioned metadata. The
  record carries no prose: it binds the chunk source SHA-256, the character span, the 1-based
  ordinal of the spelling, the containing paragraph's number and SHA-256, the candidate, the
  decision ID and a digest of the whole decision, and the owning series concept. Every consumer
  re-proves all of it and fails closed on any mismatch, including a re-decided term. Every other
  occurrence of the spelling stays governed, and the policy and registry are untouched. A snapshot
  freezes the file as a new optional resource `terminology_exemptions`, omitted rather than null
  when absent, so earlier manifests, packages and identities keep their bytes. Prompt selection,
  both terminology tiers and the semantic gate's per-paragraph constraints honour the exempt spans,
  as do QA in the worker, reuse, historical re-evaluation, re-evaluation, structural repair and
  final QA. An exemption applies only to its own chunk's source text; the same words inside
  another chunk's context window stay governed. New schema `terminology-occurrence-exemptions`.
  Terminology QA 1.11.0 → 1.12.0 (verdicts are unchanged without exemptions). The prompt renderer
  and the terminology context contract are deliberately not bumped: rendered bytes are unchanged
  for every input set that existed before, and a bump would make every historical package fail
  verification.
- **Exemption-attributable historical carry-forward (Historical Re-evaluation Policy 1.2.0).** A
  new eligible tier, `occurrence_exemption_drift`, lets a historical output be re-assessed when its
  request moved only because exemptions withheld decisions from the new request. The proof is a
  counterfactual render: the new job is rendered with the exemptions withheld, and that render must
  reproduce the historical user message byte for byte. If anything else also moved, including a
  terminology amendment, the job is refused. The compatibility record stores the exemption IDs and
  the counterfactual digest, and the contract rejects that tier without them.

- **Concept precedence between article and case variants (Concept Precedence 1.0.0).** A new pure
  module `terminology/concept_precedence.py` decides which decision governs when two series
  concepts name one thing in the story. Two concepts are the same concept only when their source
  terms match after folding case and removing one leading English article, so `the Dark One` and
  `Dark One` are consolidated while `Dragon`/`false Dragon`, `Two Rivers`/`Two Rivers folk`,
  `angreal`/`ter'angreal` and every other merely nested pair keep their own decisions. Precedence
  runs explicit-user `LOCKED` > explicit-project `LOCKED` > other `LOCKED` > `STRONG_REFERENCE` >
  weaker evidence; ties break on the longer source term then the concept id, so the result never
  depends on input order. `policy_concept_conflicts(policy)` reports the class without editing
  anything.
- **Governed retirement of a displaced decision.** A series policy amendment may now carry
  `retired_terms`: a decided term is withdrawn to `OBSERVED_VARIANTS` keeping its concept, its
  spelling, its conflicts and every observed variant, with the withdrawn decision written into its
  notes and the amendment id in its provenance origin. Retirement is refused, and the whole
  amendment with it, for a term the policy does not hold, a user-owned decision, a term that holds
  no decision, a term with no evidence to keep, or a term the same amendment also amends. The field
  is omitted from serialization when empty, so every amendment recorded before retirement existed
  keeps its bytes and its identity. Series Policy Amendment 1.0.0 → 1.1.0.

- **Governed series policy amendments (Series Policy Amendment 1.0.0).**
  `amend-series-terminology REQUEST --book BOOK [--apply]` applies a batch of complete term decisions
  (force, target, authorized forms, aliases) to a frozen-in-use series policy all-or-nothing. It
  refuses the whole batch if any item names a term the policy does not hold or would override an
  explicit user decision (target, force, withdrawn form or alias, alias owned by another user
  decision), publishes an immutable `SeriesPolicyAmendmentRecord` under
  `workspace/series-terminology-amendments/` before replacing the policy, bumps the policy and its
  metadata version, and completes an interrupted application on rerun. New schemas
  `series-policy-amendment-request` and `series-policy-amendment-record`.
- **Cross-snapshot run migration (Translation Migration 1.0.0).** `migrate-translation-run NEW_RUN
  --from-run OLD_RUN [--apply]` classifies every successor job from immutable evidence and carries
  usable provider work through the existing governed boundaries only: identical-request accepted
  outputs by reuse, moved-request accepted and rejected outputs by historical re-evaluation (still
  rejected ones become explicit-repair candidates), and nothing for jobs without provider output.
  Every publication is re-planned under the successor's lock and must match the dry run; no provider
  is contacted and the historical run is only read.
- **Repair of a carried-forward draft.** `repair-translation-job` resolves a rejection standing on a
  carry-forward (acceptance sidecar, record, carried translation, and the named historical attempt,
  all hash-verified) and records `repaired_from_carry_forward` on the repair attempt (additive,
  omitted when absent). A delivered repair drops the job's carry-forward pointer; auto-repair,
  structural repair, and automatic provider recovery refuse carried drafts.

### Changed

- **Authorized target forms reach every provider-facing rendering.** The series `LOCKED` and
  `STRONG REFERENCE` tiers, the legacy registry block, the chapter-title prompt, repair requests, the
  semantic judge and reviser requests, and the literary editor's payload state
  `allowed_target_forms` plus one shared `form_rule` beside the target; competing and observed
  variants are never rendered as forms. Nothing is emitted for a decision without forms, so no
  existing package, request digest, or recovery proof changes (all 227 stored packages of Book 6 run
  004 re-render byte-identically); no renderer or context contract version moved. The literary
  editor counts canonical realizations over target and authorized forms through the shared matcher,
  and final-QA analysis treats a variant another decision authorizes as that decision's claim.
- **Historical re-evaluation 1.1.0 (policy and engine).** Admits an effectively accepted output whose
  rendered request moved (an identical request is still routed to reuse); governs series-policy books
  through amendment lineage and a term-level drift classification; requires the reference-context
  package and context contract to be identical; judges with the full acceptance battery including
  alignment QA; and publishes its acceptance sidecar inside the carry-forward record instead of
  `acceptance/001`. The compatibility record adds both manifest and series-policy SHA-256s.
- **Accepted-translation reuse 1.2.0.** Judges with the full acceptance battery (alignment QA
  included) under the source attempt's output contract and publishes its acceptance sidecar inside
  the reuse record. Both engines accept already-loaded run states so batch planning validates each
  snapshot once.
- **One acceptance battery.** The worker, reuse, and historical re-evaluation share
  `translation_acceptance_qa.assess_translation`; worker behaviour is unchanged.

### Added

- **`restore-translation-repair-target` settles a failed repair whose draft was carried forward.**
  `restore_repair_target` could only restore a draft *this* run produced: `successful_attempt` was
  a required `int` and was written back verbatim. A carried-forward draft has no successful
  provider attempt behind it and is numbered by carry-forward, so the only number available was the
  carry-forward number — writing it would have asserted a provider success that the failed
  execution never produced, and the job had no governed way out of `failed` at all (Book 6
  `017-002`). `successful_attempt` is now `int | None`. `None` is admitted only after the carry-
  forward settlement is *proven*: `carry_forward_count > 0`, a historical re-evaluation origin, the
  repair boundary's own `load_effective_rejection` verifying the acceptance bytes, the carry-forward
  record, the two numbers agreeing and the historical attempt, the translated bytes on disk
  re-hashing to the acceptance, and no successful provider attempt existing for the job. Supplying
  a number for a carry-forward job, or omitting one for a provider-backed job, is refused, so the
  two settlements can never be confused. Ordinary failed translations are outside the path
  entirely (`is_failed_repair`). Attempt accounting, `last_attempt`, and every attempt, acceptance,
  reservation and resolution record are untouched. The new command plans by default, prints the
  full before/after, requires `--apply`, and makes no provider call.

  No engine version is bumped: the only candidate, `TRANSLATION_QUEUE_VERSION`, is an equality gate
  in `_load_queue`, so raising it would fail every stored run with "translation queue engine version
  is stale" — a breaking stored-data change needing a migration. No persisted contract, field or
  serialized byte changes here; only which callers may take an already-governed transition.

### Changed

- **A restored repair target's `completed_at` is read, not supplied.** It was a caller argument,
  and provider recovery passed the failed draft's *attempt* completion while a released repair
  claim wrote the *acceptance* timestamp — two values for the same "return to the settled
  rejection" outcome. Both now derive it from the standing acceptance evidence via
  `_settled_completion`. For a carry-forward or re-evaluation settlement the value is unchanged;
  for a provider-backed restoration it moves from the attempt's completion to the moment the
  verdict that settles the job was published.

### Fixed

- **A provider repair delivered after a QA re-evaluation no longer writes an invalid job.** The
  executor retired the carry-forward and structural-repair pointers of the draft a repair
  replaces, but not its re-evaluation pointers. The settled job then named a re-evaluation while
  its acceptance origin was `initial_qa`, which the contract rejects, and the whole run stopped
  loading. A delivered repair now retires the re-evaluation pointers too. The count stays, because
  it numbers the job's immutable re-evaluation history, and the contract admits a counted history
  with no pointer when no re-evaluation defines the verdict. Re-evaluating the repaired draft
  numbers after that history. Book 6 job `053-003` was settled by the defective path once and was
  corrected under the queue lock to exactly what the fixed settlement writes, with a
  `repair_settlement_corrected` event recording it.
- **A carried-forward draft can be re-evaluated provider-free (QA Re-evaluation 1.0.0 → 1.1.0).**
  `reevaluate-translation-job` required a provider attempt of the job, and the job contract forbade
  a re-evaluation origin on a carried-forward job. A carried draft that newer QA would accept could
  therefore never receive that verdict. The route now judges a carried draft through its
  carry-forward record (every link re-hashed) and publishes an ordinary re-evaluation naming the
  carry-forward acceptance it re-judged. The translation, carry-forward record, and historical
  verdict are never rewritten. `TranslationJob` admits `qa_reevaluation` as a carried job's
  effective origin once a re-evaluation exists. The repair boundary resolves a still-rejected
  re-evaluated carried draft through the same carry-forward.
- **An authorized compound verb conjugates, and a plural canonical's recorded singular realizes it
  (terminology QA 1.10.0 → 1.11.0).** Both rules follow operator decisions.
  - A form authorized as `<element> شدن/شده/کردن/کرده` is realized by the same element with any
    conjugation of that light verb or its transitive/intransitive partner. The separators allowed
    are the ones the decision's own forms use; `خنثی ماند`, `خنثی‌سازی`, and `خنثی‌شدگان` are
    still not the compound.
  - A plural canonical accepts its singular only where the decision records that exact spelling as
    the canonical minus one plural suffix (`مطرود` for `مطرودین`). That spelling is no longer
    enforceable as a rival; a different plural (`مطرودان`) still is. Nothing is derived by
    stripping letters.

  The two 1.8.0 pins that treated these conjugations as unauthorized now accept them. Across 661
  verifiable acceptance records, 42 rejections clear and the one historical Book 5 record noted
  under 1.10.0 is still the only regression.
- **Exact Persian terminology accepts regular nominal morphology, and inflectional aliases are
  enforced (terminology QA 1.10.0).** Book 6 rejected correct translations whose only fault was
  ordinary Persian grammar. Exact matching now recognizes a canonical target through closed nominal
  rules, all applied to the authorized spelling itself and never to a stem guessed from the
  surface:
  - the attached plural (`ان` after a consonant or `ی`, `یان` after `ا`/`و`);
  - the plural on the head of a multi-word phrase, and a written ezafe mark inside it.

  Verbal morphology is deliberately excluded. A conjugated compound verb must still be authorized
  explicitly by its decision, as pinned since 1.8.0.

  Source-side, the validator enforced an alias only when it equalled the source term after case and
  article folding, so `Warders` was prompted as the `Warder` decision but never checked. An alias
  that is an English inflection of the source term (possessive, plural, `man`/`men`) now governs,
  through one shared rule, `governing_source_matches`, which the validator, the final-QA analyzer,
  and chapter-title QA all use.

  Two things are deliberately unchanged. First, an alias that is a *different* source expression
  (`Gaidin`, `her old nurse`, `Mistress Tharne`) is still prompted but never enforced: enforcing
  it would have added 88 false rejections across Book 5. Second, the canonical-target morphology
  does not apply to competing variants, which keep the contracted matcher, so rival detection
  cannot widen. A recorded variant that is itself a realization of the canonical is no longer
  enforceable as a rival.

  Across 658 locally verifiable acceptance records, 14 rejections clear and 1 historical Book 5
  record newly rejects: it writes `ها` joined without a ZWNJ, which stays outside policy.
  Final-QA Analysis 1.2.0 → 1.3.0; chapter-title prompt and QA 1.0.0 → 1.1.0. The persisted schema
  is unchanged.
- **A stranded *repair* reservation can now be resolved (Execution Resolution 1.0.0 → 1.1.0).**
  `resolve-translation-reservation` refused every repair execution outright:
  `_refuse_superseding_evidence` treated any acceptance status as evidence superseding the attempt,
  which is right for a translation but backwards for a repair, whose whole premise is a standing QA
  rejection describing the draft being repaired rather than the attempt in flight. An interrupted
  repair therefore had no governed way out of `running` at all. The guard now classifies the
  execution first, structurally: a `pending` job may not carry an acceptance status and an ordinary
  claim only starts from `pending`, so an active job that carries one is a repair — the new
  `is_active_repair` in `translation_repair_policy.py`. Translation reservations keep the existing
  blunt rule verbatim. A repair reservation is superseded only by an accepted verdict, by an
  accepted/successful attempt or `acceptance/NNN/` belonging to *this* slot, or by a reuse, and it
  is exempted only after `load_effective_rejection` (via a new read-only `allow_active_repair`
  tolerance, matching the existing `allow_failed_repair`) hash-verifies the whole rejection lineage,
  so an unprovable repair target fails closed. The "this slot" test reads `accepted_attempt` as an
  attempt number only when the job holds no carry-forward: a carried-forward draft is numbered by
  carry-forward, so its first repair attempt shares the number and comparing the two spaces would
  refuse exactly the stranded repair this allows (Book 6 job `017-002`). Published evidence for the
  slot still covers that case.

- **Bumping the execution-resolution engine no longer invalidates resolutions already published.**
  `_plan_from_record` re-derived a stored record's content address with whichever engine version was
  installed, so the first bump of `EXECUTION_RESOLUTION_ENGINE_VERSION` would have made every
  historical resolution fail with "the recorded resolution identity does not verify", breaking the
  idempotent rerun and reconcile paths that lineage exists to support. The identity is now verified
  against the record's own `resolver_engine_version`.

- **Returning a repair claim no longer writes an invalid job.** Claiming a repair clears
  `completed_at`, because the claim displaces the settled draft it is about to replace. When no
  repair followed, both governed ways back — `release-translation-job` and the expired-claim branch
  of `recover-translation-run` — restored `status: succeeded` and left `completed_at` null, which
  the `TranslationJob` contract rejects ("succeeded jobs require completed_at"): the record was
  written and every later read of the run failed on it. Both paths now also restore the timestamp,
  read back from the job's own immutable acceptance evidence (hash-verified, lineage-checked) and
  never from the clock. For a carry-forward or re-evaluation settlement that value is the same
  instant the original `completed_at` held. A settlement that cannot be proven from lineage fails
  closed: release raises, and recovery reports a blocker and leaves the claim exactly as it was
  rather than writing it back invalid. Ordinary pending translation claims, attempt accounting and
  the exhausted-budget path to `blocked` are unchanged.

- **One fictional concept can no longer expose two conflicting canonical renderings.** An explicit
  project decision keyed on `the Dark One` and a corpus promotion keyed on `Dark One` were two
  concepts with two targets. Every prompt path rendered both instructions and every enforcement
  path honoured only the longest span, so a repair could be told to write the weaker rendering and
  then be rejected for having written it (Book 6 successor job `003-003`). Four layers were
  involved and all four are addressed:
  - *Import.* The promotion guard in `series_terminology/bootstrap.py` only refused a promoted term
    that **contained** a locked source. It now also refuses one **contained within** a locked
    source: a shorter article or case variant of a locked term is never promoted to a rival
    decision, and keeps its corpus evidence. Series Terminology Engine 1.1.0 → 1.2.0.
  - *Derivation.* `derive_book_registry` collapses each group of article/case variants onto the
    governing decision. The displaced spelling becomes an alias of that decision so every
    occurrence stays matched and is enforced to the one canonical target, and the displaced corpus
    evidence moves onto the governing concept, where a clean narrative rival of a locked target
    becomes an enforceable competing variant — never an authorized target form. A decided term's
    alias now also outranks an evidence-only term's own spelling, so a retired concept cannot hold
    a spelling hostage and leave a governed occurrence unenforced.
  - *Selection.* `applicable_decisions` is the one selector every prompt path uses, sharing
    `source_term_matches` with the QA validator so a provider is never told to honour a term the
    validator will not look for. Prompt packaging, the semantic judge and reviser, and literary
    editing moved onto it from the overlap-blind `relevant_decisions`, which remains for the
    historical re-evaluation path that must reproduce recorded bytes. Prompt Package Renderer
    1.4.0 → 1.5.0.
  - *Detection.* `policy_concept_conflicts` reports the class automatically and is covered by both
    the two real conflicts and the eleven nested-but-distinct pairs that must never be collapsed.

  The repair derivation itself is deliberately unchanged: `REPAIR_PROMPT_VERSION` stays `1.0.0`
  because bounded provider recovery re-derives a failed repair and proves it byte-identical, and a
  change there would make every historical repair irreproducible. The repair path is corrected by
  the registry stating one decision per concept, not by changing how a repair reads it.


- **A provider attempt's acceptance slot is never adopted from another draft.** The worker adopted any
  pre-existing `acceptance/NNN/evidence.yaml` for attempt NNN after checking only run, job, number, and
  source; a reuse or carry-forward sidecar in that slot would have been recorded as the verdict on a
  repair attempt. The sidecar must now name that attempt's own `attempt.yaml`.

- **Exact Persian terminology accepts the indefinite marker after a long vowel (terminology QA
  1.9.0).** A canonical ending in `ا` or `و` takes the indefinite marker as `یی` (hiatus glide plus
  `ی`); exact enforcement allowed only one attached `ی` and reported `canonical_term_missing`. Book 6
  run 004 job `004-003` was rejected that way although structural and alignment QA passed. The shared
  Persian inflection set gains `یی`, admitted only after a canonical whose final letter is `ا` or `و`
  and still followed by a word boundary; the other indefinite allomorphs (attached `ی` after a
  consonant, `‌ای` behind a ZWNJ) were already recognized. No term is special-cased, no target form
  is recorded, and the persisted schema is unchanged. Across 409 locally verifiable acceptance
  records, 4 rejections clear (Book 6 `004-003`; Book 5 `024-002`, `043-004`, `049-006`), no finding
  is added, and nothing regresses. Run 004 was not modified; `004-003` re-evaluates to `accepted` in
  a dry run.
- **A decision can authorize more than one canonical target realization (terminology QA 1.8.0).**
  Book 6 job `002-001` was rejected with `canonical_term_missing` although structural and alignment
  QA passed: the participial source term is locked to a participle target, and the translation
  correctly used the verbal noun of the same concept. The decision model had no way to say so —
  candidate aliases are source-side, competing variants mean the opposite, observed series variants
  are evidence only, and morphological stemming is automatic. `TerminologyDecision` and `SeriesTerm`
  gain an optional `allowed_target_forms` list (additive to schema 1.0, omitted when empty so no
  existing registry, policy, or identity changes). Exact enforcement now means at least one
  explicitly authorized realization occurs, each matched with the existing lexical boundaries and
  Persian inflections; presence, deficit counting, competing-variant enforceability, derivation,
  policy import, and the final-QA analyzer share the rule. `set-terminology` and
  `set-series-terminology` accept `--target-form`. Nothing is inferred, no term is special-cased, and
  a decision without forms is enforced exactly as before. No registry, policy, snapshot, or run was
  edited; run 004's frozen registry still rejects `002-001` until a new snapshot authorizes the forms.
- **Worker-pool slots no longer starve each other on the run lock.** On
  `tr-014220572d3c-004` with `--workers 2`, one slot's claim failed with `QueueLockTimeout` (120 s)
  while another slot assessed a draft. Acceptance QA itself takes well under a second; the time went
  to *live snapshot validation*, which re-derives Book 6's series terminology (~55 s per load) inside
  every queue load. Acceptance held the lock across two of those (its own load and the one inside
  attempt validation), a claim across two (its load and the mutation's snapshot re-check), and
  reservation and finalization across one each. The lock never protected the snapshot or the
  workspace, so validation now runs before the lock is requested (`_locked_queue`), and a critical
  section is only the parse of the run's records, the checks, and the writes. Acceptance is
  two-phase: attempt validation, frozen-input hashing, and all QA run with no lock held; under the
  lock the run is reloaded and the verdict published only if the job is still the same unassessed
  draft of the same successful attempt and every hashed input is unchanged (a verdict another process
  recorded meanwhile is returned as is; anything else publishes nothing). The 120 s timeout is
  unchanged and still fails closed. No persisted contract, schema, or engine version changes.
- **Exact Persian terminology accepts an attached pronominal clitic (terminology QA 1.7.0).** A
  locked canonical followed directly by `م`, `ت`, `ش`, `مان`, `تان`, or `شان` — the normal Persian
  way to write "my/your/her/our/your/their <term>" — was reported as `canonical_term_missing`. The
  closed clitic list joins the shared contracted-inflection set; the trailing word boundary still
  applies after the clitic, so a longer token that merely begins with the canonical is still a
  different word. Across the 396 locally recorded acceptances whose inputs still hash, 9 rejections
  clear and no finding is added. Existing evidence is not rewritten; the minor bump makes those
  rejections re-evaluable.

- **Claude Code output capture is lossless across assistant turns (Claude Code adapter 1.3.0).**
  The adapter read the JSON `result` field as the translation, but that field is only the last text
  block of the last assistant message and Claude Code resets it on every internal user turn. When
  a response exhausted `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, the CLI injected its own "Output token
  limit hit. Resume directly" nudge and continued in a second assistant turn (`num_turns: 2`), so
  every Book 6 attempt delivered only the continuation — an unmarked fragment followed by
  `[[P0014]]`–`[[P0029]]` — and was correctly rejected for `marker_missing` and
  `text_before_first_marker`. The adapter now runs `--output-format stream-json --verbose` and a
  pure parser (`providers/claude_stream.py`) reconstructs the answer from every primary-session
  assistant message in order: subagent, meta, and API-error-carrier messages, thinking and tool
  blocks, user, system, progress, and delta events are excluded, per-block snapshots of one API
  message are joined, byte-identical replays are counted once, and anything that cannot be
  reconstructed deterministically (a missing turn, interleaved or conflicting messages, a `result`
  text that contradicts the final message, missing turn or session metadata, malformed lines) fails
  closed as a retryable `malformed_response` instead of publishing a partial translation. A
  one-turn session yields exactly the bytes the `result` field used to. Provenance, sanitized
  `response.json` (now also carrying `assistant_message_count` and `user_event_count`), rejected-
  output preservation, and marker validation are unchanged; preflight additionally requires the
  CLI to document `--verbose` and `stream-json`. Existing attempts are not rewritten.
- **Book 6 output budget.** `config/books/lord-of-chaos.yaml` raises `provider_max_output_tokens`
  from 12000 to 32000. Claude Code applies the value as the per-request `max_tokens` (thinking
  tokens included), both real Opus attempts on the 1,791-word prologue chunk produced about 16,200
  output tokens, and 32000 is the CLI's own default ceiling. The budget is not part of request or
  prompt-package identity and the frozen snapshot does not reference the file, so nothing existing
  becomes stale. Book 5's `config/book.yaml` is unchanged.
- **Alignment QA findings never quote a malformed marker line.** A carried `marker_malformed`
  finding now keeps only the bare marker-like token as `observed_marker`, matching the provider
  validation path, instead of the parser's quoted line that could carry up to a few words of prose.

### Added

- **Multi-process translation worker (`--workers N`, Worker Pool 1.0.0).**
  `run-translation-worker --workers N` runs one pass with N execution slots as spawned processes
  under one coordinator. The coordinator performs the once-per-pass work exactly as before — option
  validation, run and book resolution, acceptance and selection validation, provider/model policy,
  provider preflight, queue recovery — then builds the sequential worker's own selection as a
  bounded work plan and dispatches it: unassessed drafts, then one claim per claimable job, then
  QA-rejected drafts automatic repair may act on. A claim item is resolved by the queue's own
  `claim_job` under the run lock, so two slots can never own the same job; the executor's ownership
  checks (claimed status, `claimed_by`, constant-time token comparison, live lease, unique attempt
  slot, no successful or ambiguous attempt) run unchanged before every provider call and before
  publication. Each slot has a distinct derived worker identity (`<worker-id>-<slot>`), processes
  one job at a time, and returns a structured `WorkerJobResult` that the coordinator renders as
  `[worker-N] <job-id> <outcome>` plus a running total; the summary gains `Workers` and `Worker
  failures`. `--max-jobs` is a global limit across all slots, effective concurrency never exceeds
  the selected work, an explicit `--job-id` always runs alone, `--workers 1` is the sequential
  worker byte for byte, and `--dry-run` starts no process. `--stop-on-error`, Ctrl+C, and SIGTERM
  stop new dispatch and let in-flight jobs finish and finalize; slots run in their own session so
  a terminal interrupt never reaches an in-flight provider call, a second Ctrl+C interrupts hard,
  and slots of a coordinator that was killed outright finish their current job and exit. A slot that dies is reported as an `error` row naming the job it
  held, stops further dispatch, and leaves its claim for queue recovery.
  `--auto-recover-provider-failures` is refused with more than one worker because it re-runs
  global queue recovery per job. Per-job semantics — retry policy, QA acceptance, rejected-output
  preservation, automatic repair — are unchanged and per job. Documented in `README.md`,
  `docs/pipeline.md`, and `docs/recovery.md`; benchmarked by `scripts/benchmark_worker_pool.py`.
- **Rejected provider output is preserved for diagnosis.** A provider answer that arrived whole
  but was refused by structural validation — most importantly a numbered-paragraph contract
  violation such as `marker_missing` or `text_before_first_marker` — is no longer discarded. The
  executor publishes the exact LF-normalized candidate as the private `rejected-output.md` beside
  the failed attempt, hashed and recorded on `attempt.yaml` as `rejected_output_file` /
  `rejected_output_sha256`, and marker refusals record prose-free `marker_findings` with
  `expected_marker_count` / `observed_marker_count`. `validate-translation-attempt` verifies the
  artifact (`rejected_output_hash_valid`) and refuses impossible combinations (rejected output on a
  succeeded or ambiguous attempt, or beside `translation.md`); `list-translation-attempts` and
  `show-translation-attempt` show whether an attempt carries one and which finding codes. Nothing
  is retained for truncated, empty, multi-output, transport, authentication, rate-limit, or
  ambiguous failures. Acceptance semantics are unchanged: every marker violation remains blocking,
  the artifact is never a translation, no preamble stripping or marker synthesis is attempted, and
  existing attempts are not retrofitted. Attempt schema 1.0 is extended additively.

- **Codex CLI provider preflight (Provider Preflight 1.1.0).** `check-translation-provider
  --provider codex-cli` now performs a real preflight instead of reporting "not implemented" and
  passing: it verifies the executable and detects its version, that `codex exec --help` documents
  every capability the adapter's command line relies on (detected from the installed CLI rather
  than pinned to a version), and that `codex login status` reports an active login. A logged-out,
  unrecognizable, or API-key login fails closed; the login kind is reported as `chatgpt` or
  `unknown` and the subscription plan always as `unknown`. The same preflight now gates
  `execute-translation-job`, `repair-translation-job`, `run-translation-worker`, and provider
  recovery for `codex-cli` before any claim, so an unusable Codex installation consumes no attempt.
  No probe sends content, makes a model call, or reads a credential file.
- **Terminology Architecture v2 (Series Terminology 1.0.0, Snapshot Engine 1.2.0, Prompt Package
  Renderer 1.4.0).** A series-aware terminology layer beside the per-book registry: a governed
  `SeriesTerminologyPolicy` (`LOCKED` / `STRONG_REFERENCE` / `OBSERVED_VARIANTS` with
  `explicit_user`, `explicit_project`, `promoted_reference`, `prior_volume`, and `unresolved`
  provenance and recorded conflicts), a derived per-book occurrence registry that projects the policy
  onto the existing enforcement vocabulary, read-only retrieval over hash-pinned external reference
  corpora through their own retriever producing immutable per-chunk reference packages with
  provenance and explicit exclusions, and a length-framed `terminology-policy` prompt section that
  separates the four tiers. New commands: `import-series-terminology-policy`,
  `set-series-terminology`, `derive-book-terminology`, `prepare-reference-context`,
  `show-terminology-context`, `validate-series-terminology`. Book configuration gains
  `series_terminology_policy` and `reference_corpus`. Snapshots freeze the policy, the corpus
  configuration with every external corpus and retriever identity, and the reference-context index,
  under the `series-terminology-v1` identity representation; manifests without a policy keep their
  identity. Book-scoped workspaces no longer fall back to the root `config/terminology.yaml`,
  `config/terminology-questions.yaml`, or `config/source-anomalies.yaml`; the legacy root is
  unchanged. See `docs/terminology-architecture-v2.md`.

### Changed

- **Queue reads and lock timeout sized for concurrent execution slots.** The run lock timeout is
  120 seconds instead of 5: a full parse of a 226-job run takes about a second, an acceptance holds
  the lock across two, and a waiter behind several slots legitimately waits longer than five; an
  unreleased lock still fails closed. The executor's plan and prompt-package planning now use a
  *settled* queue read: a read outside the lock that catches another process between its job write
  and its run-aggregate write is re-read once under the lock before a counter or status mismatch is
  reported, so a transient half-publication no longer refuses a job while a genuine crash signature
  still does. Acceptance no longer reloads the queue twice while holding the lock. Nothing the
  queue records changes; single-worker behavior is unchanged.
- **Series terminology context contract 1.1.0 (Series Terminology 1.1.0).** The rendered
  `OBSERVED VARIANTS` tier is now limited to evidence the passage actually raises. An observed term
  must occur in the chunk's own source text rather than only in its untranslated context window; a
  spelling that a locked or strong decision in the same section already claims as its source term
  or an alias is no longer reopened as an observed variant beneath that decision; and a compound's
  corpus form that does not contain the locked rendering of a locked component it names (the
  policy builder's own promotion rule, applied to evidence) is withheld with a count, the term
  disappearing when no form survives. Locked and strong tiers, reference packages, package
  identities, and the policy are unchanged. No snapshot had frozen the 1.0.0 contract.

### Fixed

- **Provider execution reads the run's own book configuration.** Every run-side provider boundary
  loaded `config/book.yaml` unconditionally, so a Book 6 worker (`--provider claude-code --model
  opus` against a `workspace/books/lord-of-chaos/` run) was judged against Book 5's allow-list
  and, because `claude-code: []` admits no explicit model, refused after the claim had already
  spent an attempt slot — with the worker reporting only `Errors 1`. `QueueState` now carries the
  book workspace the run's frozen lineage names, derived from the run's `snapshot_path` and
  cross-checked against the manifest's workspace paths (a disagreement is refused as ambiguous,
  never guessed at), and `resolve_run_book` exposes the same derivation to the CLI. The executor's
  model allow-list and reasoning-model checks, the worker's, `execute-translation-job`'s,
  `repair-translation-job`'s, and `finalize-translation-run`'s timeout/output defaults, and the
  `audit-semantic-coverage`, `run-semantic-gate`, and `run-literary-edit` provider settings all
  resolve through it: a legacy-root run reads `config/book.yaml` byte-for-byte as before and a
  book-scoped run reads `config/books/<book-id>.yaml`. `config/books/lord-of-chaos.yaml` now lists
  `claude-code: ["opus"]` explicitly; Book 5's configuration and the empty-list contract (session
  default only, never a wildcard) are unchanged, and the alias is forwarded as requested.
- **Model policy is checked before a claim, and pre-provider worker failures are diagnosable.**
  The worker and the repair boundary now ask `ensure_provider_model_allowed` before the provider
  preflight and before any claim — in a dry run too — so a disallowed model consumes nothing and
  `--dry-run` rehearses the configuration faithfully. A per-job failure the worker records and
  moves past now carries the failure class and message on `WorkerJobResult.error`, is printed as
  `Error <job-id>: ...` by `run-translation-worker`, and, when it struck a claim the worker still
  holds before any reservation, is recorded on the job as `last_error_category: worker`,
  `last_error_message`, and a `worker_error` event. The record is diagnostic only: status, lease,
  counters, recovery, and release are unchanged, and a claimed slot still counts against
  `max_attempts` until an operator abandons it through reservation resolution, as documented.
- **`create-translation-run` resolves book-scoped snapshots by ID.** The queue planner validated a
  bare snapshot ID without a workspace selection, so a snapshot frozen under
  `workspace/books/<book-id>/` was looked up in the legacy root and refused as "not current,
  valid, and frozen", and only the full snapshot path worked. The command now accepts the shared
  `--book <book-id>` option, and `plan_run(..., book=...)` passes it to snapshot validation. A
  bare ID is looked up in exactly one workspace and never searched for elsewhere, so an ID cannot
  resolve ambiguously and the wrong (or missing) `--book` fails closed; an explicit path keeps
  working and is still validated against the workspace its frozen manifest records. `--book` also
  selects the book configuration that supplies the default paragraph-marker contract. Without
  `--book` the legacy root resolves exactly as before, and run storage is unchanged: every run
  stays under the project-level `workspace/translation-runs/` with its `snapshot_path` naming the
  workspace it belongs to.
- **Book configuration accepts a persisted series decision.** `BookConfig` rejected `series_name`
  and `series_index` although `BookMetadata` and `resolve-book-metadata --set` already supported
  them, so a series decision could only be re-asserted on every resolve. Both keys are now optional
  in `config/book.yaml` and `config/books/<id>.yaml`; when set they resolve with `configuration`
  provenance and sit below an explicit `--set` assertion and above front-matter inference. Configs
  without them keep the previous source-evidence resolution unchanged.
- **Joined-token detector 1.1.0 (Source Quality Audit 1.4.0).** The modal-join heuristic matched
  any token that merely began with `could`, `would`, or `should` and continued with five or more
  lowercase letters, so ordinary agent nouns and their inflections became high-confidence blocking
  extraction findings, and every such hit carried the case-boundary message although no uppercase
  letter was involved. A modal prefix is now evidence only when the remainder is attested as a
  standalone word elsewhere in the same chapter; case-boundary, article, and modal joins carry
  distinct messages and a `join_kind` evidence field, and the detector has its own version so its
  finding identities no longer share `DETECTION_ENGINE_VERSION`. No allowlist was added.

### Changed

- **Normalization Engine 1.6.0 / page-boundary-continuations 1.5.0.** A canonical page boundary
  whose left edge ends on a bare, unpunctuated lowercase word and whose right edge begins with a
  capitalized possessive name word is now joined. This is the `possessive_continuation` shape the
  source-quality audit already reported as high-confidence evidence; the 1.4.0 pass only accepted it
  after a capitalized name word. Terminal, punctuated, and quote-closed left edges, plain capitalized
  right edges, one-line and list blocks, and same-page boundaries remain boundaries. Existing
  normalized artifacts are version-stale; the first book's output is byte-identical under the new
  rule and was not regenerated.

### Added

- **Book-scoped workspaces.** One repository can now process several books without their
  mutable artifacts ever sharing a path. `ProjectPaths(project_root, book)` roots the unchanged
  canonical layout under `workspace/books/<book-id>/` with `config/books/<book-id>.yaml`; the
  legacy `workspace/` root and `config/book.yaml` remain the first book's workspace and are
  resolved byte-for-byte as before whenever no book is selected. The source-processing commands
  (`inspect-pdf`, `extract-pages`, `discover-toc`, `extract-chapters`, `normalize-chapters`,
  `chunk-chapters`, `audit-source-quality`, `show-source-quality-finding`,
  `inspect-artifact-staleness`, `inspect-normalization-changes`, `regenerate-artifacts`,
  `resolve-book-metadata`, `freeze-translation-resources`, `create-translation-snapshot`,
  `validate-translation-snapshot`, `list-translation-snapshots`, `status`) share one `--book`
  option. Two books may both have a `000-prologue`; stale/conflict detection is unchanged
  inside each book. Snapshot validation rebuilds lineage from the workspace the frozen manifest
  itself records and refuses an explicit selection that names a different one, so a later run
  cannot be planned against the wrong book's canonical artifacts.
- **Epilogue extraction.** `extract-chapters` treats the epilogue as a narrative unit next to the
  prologue and numbered chapters, publishing it as `999-epilogue/` (chapter ID `999-epilogue`,
  chunk IDs `999-epilogue-NNN`, selector `epilogue`). Normalization, chunking, source-quality
  audit, staleness inspection, regeneration, status, and snapshot planning recognise it.
  Appendices (glossary), unknown trailing entries (author note), and front matter stay outside
  the default corpus. No persisted contract changed: `ChapterMetadata` already carried
  `kind: epilogue` with `number: null`; only the source-anomaly `chapter_id` pattern was
  widened to admit `999-epilogue` (schema regenerated). `EXTRACTION_VERSION` stays `1.0`
  because no existing artifact's bytes or selection changed and staleness inspection compares
  that version byte-for-byte on the first book's sources.

- **Chapter-Level Persian Literary Editor 1.0.0 (v2 Phase 4).** `run-literary-edit` operates only
  on explicitly selected, marked, mechanically accepted, semantically PASS chapters and publishes a
  new immutable `workspace/literary-runs/lr-<24-hex>/` derivative. The normal editing unit is the
  complete chapter; an oversized chapter is split into deterministic overlapping sections whose
  editable cores tile paragraph identity exactly once. The editor gets the faithful Persian draft,
  chapter-global paragraph identities, source consultation context, relevant frozen terminology, a
  generic versioned Persian style contract, and a separately frozen book profile. It receives no
  translator reasoning, semantic-judge reasoning, repair rationale, or previous score.

  Editor, literary correction, and continuity output are strict structured contracts. Every
  paragraph is returned once in order with text, a byte-derived changed flag, and closed edit categories;
  a cross-paragraph move can only be requested explicitly and is not silently applied. The editor
  cannot return a fidelity verdict. The read-only continuity reviewer cannot return replacement text
  and reports only bounded Persian-prose findings.

  Diff Governance 1.1.0 records changed paragraph count and IDs, character/word deltas,
  terminology differences, unchanged hashes, marker identity, categories, and normal/broad/suspicious
  classes. Broad literary work is allowed; suspicious rewrites are routed to individual wider-context
  verification. Marker, structural, alignment, and terminology QA precede the mandatory Semantic
  Coverage Judge pass over every changed paragraph. Unchanged byte-identical paragraphs reuse their
  existing PASS evidence. A changed failure returns only to the literary editor for at most two
  fidelity-correction rounds and is independently re-judged; anything still unproven restores the
  exact pre-literary bytes as `literary_edit_fallback`. A literary model cannot self-certify fidelity
  or lower the established fidelity floor.

  Run evidence separates editor, semantic re-judge, correction, and continuity calls, tokens,
  latency, and requested/returned model provenance, plus change, semantic-regression, and fallback
  rates. Each pilot chapter names and hashes V1 published, V2 fidelity-only, and V2 literary layers.
  A blind human-comparison document uses only Version A/Version B and the requested Persian rubric;
  its origin mapping is held in a separately hashed answer key. The layer does not approve text,
  publish a release, regenerate an EPUB, or feed reader preference into terminology.

  Literary Editor Prompt 1.0.1 and Literary Correction Prompt 1.0.1 enumerate the exact closed edit
  reason vocabulary in the request instructions. This replaces Prompt 1.0.0 after the controlled
  pilot correctly rejected otherwise well-formed provider output that invented reason labels; the
  rejected outputs were not persisted or published, and the prompt version creates a new immutable
  literary-run identity.

  Continuity Prompt 1.0.1 likewise enumerates its exact finding-code vocabulary and four-identity
  limit. Literary Edit Policy 1.0.1 allows one bounded retry for a structurally invalid read-only
  continuity response; rejected completed calls remain in role cost/provenance accounting with
  `status: output_rejected` and a prose-free error code. The change follows a pilot response that the
  strict continuity contract correctly refused before chapter publication.

  Continuity Schema 1.0.1 widens its bounded observation field from 300 to 1,000 Unicode characters,
  and Continuity Prompt 1.0.2 states that limit explicitly. Five otherwise structured pilot findings
  exceeded the original undocumented field bound; both contract attempts were refused and no chapter
  was published. The field still cannot carry replacement text or unrestricted reasoning.

  Literary Edit Policy 1.1.0 lowers the safe complete-request bound to 28,000 combined source/draft
  characters after two full 60-paragraph Opus responses failed exact identity coverage. Chapters
  above that measured limit now take the already-governed deterministic section path: chapter-global
  identities, two context-only overlap paragraphs, uniquely owned editable cores, and the frozen
  chapter profile in every request. The policy change creates another immutable run identity; it is
  not a silent fallback to translation chunks.

  Literary Editor Schema 1.0.1 and Prompt 1.0.2 make `changed` deterministic metadata: the engine
  derives it from byte equality with the faithful paragraph and records
  `changed_flag_normalized: true` if a provider reports the opposite. Reasons remain an optional
  closed vocabulary. Provider prose is never discarded or accepted merely because this redundant
  boolean is wrong, while actual text changes still require fresh semantic evidence.

  `--reuse-literary-run` supports an explicit recovery bridge when a prior run identity changed
  after one or more chapters had already published atomically. The planner verifies style/profile,
  source and faithful hashes, engine/policy compatibility, provider/model provenance, final text,
  and every semantic-evidence hash. A compatible chapter is copied into the new immutable layer,
  its evidence paths are re-bound and re-hashed, and every carried call records its source literary
  run. No provider call is repeated for the carried chapter.

- **The production semantic gate, and bounded semantic revision.** Phase 2 built a judge and
  measured it; this makes the judge's answer count. `run-semantic-gate` governs marked-contract
  runs created under a gate policy: it judges every accepted paragraph, routes each verdict, and
  stops at a bounded terminal state. Semantic acceptance is a fourth dimension recorded as its own
  job state beside structural, terminology, and alignment acceptance, and its absence means *not
  judged*, never *passed*. A gated run cannot be released until every job is semantically `pass`;
  release assembly reads the state and imports nothing from the judge. A run created before the
  gate records no policy and is held to exactly the requirement it always was.

  Coverage is total by policy (`SEMANTIC_GATE_POLICY_VERSION` 1.0.0: 100% of paragraphs, batch
  size 4, one second look, two revisions per window). Deterministic signals prioritize and route;
  they never excuse a paragraph from judgement, because a paragraph with a normal length and no
  structural warning is precisely where a semantic defect hides. The policy is versioned so
  measured recall evidence can change it later; nothing here optimizes it away.

  `INCONCLUSIVE` is re-judged once, alone, with two neighbours on each side instead of one
  (`SECOND_LOOK_CONTEXT_POLICY_VERSION` 1.0.0), with the judged identity unchanged. A second
  `INCONCLUSIVE` is terminal `unresolved`. `REVISE` goes to a new provider role, the semantic
  reviser (`REVISION_PROMPT_VERSION` 1.0.0), which receives an explicit window of marked
  paragraphs, the material findings, the triggering canonical terms, and one neighbour each side,
  and is told to correct the named defects and preserve everything else byte for byte. It never
  sees a verdict, a confidence, or an earlier reviser's explanation. Its answer is spliced into
  the standing translation so paragraphs outside the window come from the standing bytes, then
  measured: changed indexes, character and word deltas, outside-window digests, marker identity,
  terminology codes before and after. Anything outside the window changing, a lost or reordered
  marker, a terminology regression, or a structural or alignment failure refuses the candidate.
  An adopted revision supersedes the effective translation the way a structural repair does, and
  the window is then re-judged by the ordinary judge with no knowledge of the revision. Two
  revisions per window (`SEMANTIC_REVISION_POLICY_VERSION` 1.0.0), then `unresolved`.

  A deterministic classifier (`DISPLACEMENT_CLASSIFIER_VERSION` 1.0.0) reads findings only and
  names a contiguous run of paired omission-plus-addition paragraphs as
  `semantic_boundary_displacement`. The gate first tries a whitespace-only re-segmentation of
  such a window — sentence units re-assigned to mirror the source's proportions — adopted only
  when the split is uniquely best by a margin, every block lands inside the alignment band, and
  the concatenated non-whitespace content is unchanged; otherwise it refuses and a bounded
  revision answers. Against the pilot, the one classified window could not be uniquely
  re-segmented (margin 0.05 against a 0.25 requirement), which is the classifier working: a
  proportional split that is not determined is not a repair.

  Evidence lives in the run: `jobs/<chunk>/semantic-qa/NNN/` (the Phase 2 contract, with
  `look`, `context_radius`, and `context_policy_version`), `jobs/<chunk>/semantic-revision/NNN/`,
  and a counts-only `semantic-gate/NNN/report.yaml`. Every judge, second-look, and reviser call
  records provider, adapter, requested and returned model, tokens, latency, and versions.

- **`compare-translation-runs`**, a provider-free comparison of two runs' effective translations
  of one chapter in hashes and counts, with semantic and revision evidence attached and no
  literary judgement. It exists so the literary editor and a human reader can compare v1 and v2
  from evidence rather than from memory.

- **`audit-semantic-coverage --context-radius`** exercises the second-look context policy
  read-only against any run, which is how the unstable pilot paragraph was re-measured.

- **The Semantic Coverage Judge: the first stage that reads meaning.** Everything upstream of it
  proves that the right paragraph is in the right place. The numbered paragraph contract
  establishes identity and alignment QA measures proportion, and neither can tell whether a
  sentence inside a correctly identified paragraph reached the reader. `audit-semantic-coverage`
  asks a provider one narrow question about each aligned pair: is all materially relevant source
  meaning represented faithfully in this Persian paragraph, without unsupported additions or
  distortions?

  The question it is forbidden to ask matters as much. "Is this a good translation" mixes fidelity
  with style and returns every sentence a model would have written differently, which is noise no
  repair stage can act on. Half the prompt is therefore a list of things that are *not* defects:
  sentences merged or split inside the same paragraph, clauses reordered for Persian word order,
  pronouns left implicit where the morphology carries them, compression that keeps the meaning,
  idiomatic rendering, an inflected canonical term, and a shorter Persian paragraph.

  Judging is separated from translating by what the judge receives. One request carries one
  paragraph identity, its source, its candidate, one neighbouring paragraph on each side for
  pronoun and speaker resolution, and only the canonical decisions that paragraph's own source
  triggers. It never receives the translation prompt, the style guide, the whole registry, another
  chapter, an earlier verdict, or any repair rationale.

  The answer is strict JSON validated against the exact identities the request named. An identity
  nobody asked about, a missing verdict, a duplicate verdict, an out-of-vocabulary code or
  severity, a `REVISE` with no finding, a `PASS` with one, or a preamble around the JSON is a
  provider failure and never a verdict, for the same reason a broken paragraph marker is: a verdict
  attached to the wrong paragraph is the defect this chain exists to prevent.

  What the judge reports and what the pipeline may act on are separated once more by an
  independently versioned policy. `HIGH` findings count from confidence 0.60, `MEDIUM` from 0.70,
  and `LOW` never counts on its own, so a judge that notices a lost shade of emphasis costs
  nothing. A judge `REVISE` whose findings are all immaterial becomes `PASS` with advisories
  recorded; a judge `PASS` is never promoted. `INCONCLUSIVE` is persisted as itself and is not a
  pass, not a failure, and not an automatic escalation.

  The stage mutates nothing. Evidence is immutable, published outside every run directory under
  `workspace/audits/semantic-coverage/`, hash-linked to the exact bytes it read, and records
  provider, adapter, requested and returned model, token counters, and latency per call. Message
  digests are kept rather than messages and no model reasoning is retained. A run created before
  the judge existed carries no semantic evidence, and that absence means *not judged* rather than
  *passed*.

### Fixed

- Embedded TOC classification now recognizes number-prefixed chapter titles and colon-form prologue
  and epilogue labels independently of unreliable calibre hierarchy levels, while preserving the
  existing numbered `Chapter` label behavior.

- **The Claude Code adapter records the model that actually answered.** A Claude Code call
  legitimately bills more than one model: the answer comes from the selected model while a small
  auxiliary model runs alongside it. The adapter treated "more than one entry" as unknowable and
  recorded `model_returned: null` on every real attempt as a result. It now joins the per-model
  counters against the call's own top-level usage and takes the unique match, so provenance is
  recorded whenever the evidence identifies one model and nothing is recorded when it does not.
  `CLAUDE_CODE_ADAPTER_VERSION` is 1.2.0; historical attempts keep the record they were written
  with.

- **The numbered paragraph contract, and deterministic paragraph alignment QA.** Blank lines never
  carried identity, and the pipeline had been treating them as though they did. A translation that
  fuses two source paragraphs and splits a third has exactly as many blocks as its source, so
  `paragraph_count_mismatch` compares two equal integers and passes while every paragraph after the
  first defect is the translation of a different source paragraph than the reader is looking at.
  Three chunks of the completed pilot carry precisely that shape, and the drift reached the
  published EPUB.

  `book_translator.paragraph_markers` (`PARAGRAPH_MARKER_CONTRACT_VERSION` 1.0.0) replaces the
  inference with an explicit identity. Each source paragraph is sent under a `[[Pnnnn]]` marker line
  and every marker must come back exactly once, alone on its line, in ascending order, carrying that
  one paragraph. Parsing fails closed on an absent, missing, duplicated, unknown, reordered,
  malformed, or empty marker and on text before the first one, and each finding names the expected
  marker, the observed marker, and the source paragraph index. Nothing tries to work out which
  paragraph a provider meant: a wrong answer there moves prose between identities silently, which is
  worse than the defect being detected. Conformance is decided at the provider boundary, so a broken
  answer never becomes a draft — it is a retryable malformed response with no `translation.md`
  written — and a conformant answer is stripped to pure prose, so marker syntax never reaches an
  artifact, a release, an EPUB, or a reader.

  `book_translator.paragraph_alignment` (`ALIGNMENT_QA_VERSION` 1.0.0) is the detector for what
  identity cannot see, because a provider can honour every marker and still put the wrong sentence
  under one. It runs after identity and before acceptance and measures counts only: source and
  target words and their ratio, non-whitespace characters and their ratio, sentences, numerals
  folded to their decimal values so Persian digits compare as numbers, and quotation marks. Words
  are counted by whitespace separation, because a Unicode word scan splits Persian compounds at the
  zero-width non-joiner and inflates the target count — the difference is not academic, it moves a
  well-translated pilot paragraph across the band.

  Thresholds live in a named versioned policy, not in the engine. `en-fa/1.0.0` measures pairs of at
  least 25 source words and treats a word ratio below 0.60 or above 1.60 as out of band; over the
  pilot's 5,068 aligned pairs that band selects exactly the three chunks an independent audit had
  confirmed, and nothing else. A lone outlier is a warning. Two adjacent paragraphs whose deficit
  and excess cancel back inside the band raise `adjacent_boundary_drift_suspected`, which blocks:
  content that leaves one paragraph and arrives in the next conserves the total, and that
  conservation is the signature. No alignment finding is ever automatically repairable.

- **`audit-paragraph-alignment RUN_ID`**, a read-only audit that measures a marker-less historical
  run positionally under the same policy. It resolves each job's effective acceptance through the
  resolver release assembly already uses, contacts no provider, and changes no acceptance, status,
  counter, or translated byte; its report is published outside every run directory, under
  `workspace/audits/paragraph-alignment/`.

### Changed

- **A run freezes its output contract when it is created.** `TranslationRun.output_contract_version`
  is absent on every existing run and means the original unmarked prose contract; new runs default
  to `persian-translation-marked-paragraphs/1.0.0`, and `create-translation-run` takes
  `--paragraph-markers/--no-paragraph-markers` with the default coming from
  `paragraph_marker_contract` in `config/book.yaml`. Freezing it on the run rather than reading
  configuration at render time is what keeps an existing run interpretable: its packages still
  render byte-identically, its attempts still mean what they meant, and its accepted translations
  stay eligible for cross-run reuse. Prompt Package Renderer 1.3.0 renders the marked variant only
  when a run selects it and is otherwise byte-identical to 1.2.0.

- **Structural repair 1.1.0 no longer treats count equality as proof.** A job whose rejected draft
  was produced under the marked contract is refused outright, because such a draft always carries
  one block per source paragraph and a difference therefore means a corrupt artifact rather than a
  misplaced separator — re-deriving separators would satisfy the count by splitting a paragraph
  nobody proved is the right one. Independently, the rendered repair is measured by alignment QA
  before adoption and refused if it would introduce a blocking finding. How a repair is derived is
  unchanged.

- **Repair derivation is versioned by the contract it answers.** `REPAIR_PROMPT_VERSION` stays 1.0.0
  forever, because bounded provider recovery re-derives a failed repair and proves it byte-identical
  to the one that failed. A draft produced under the marked contract uses
  `MARKED_REPAIR_PROMPT_VERSION` 2.0.0, which re-attaches the same markers to both the source and
  the rejected draft, proves the re-attachment conserves both, restates the marker rules, and
  carries the frozen previous context the original derivation omitted — the omission that let a
  whole-chunk repair rewrite an opening paragraph without seeing what preceded it.

- Auto-repair policy 1.2.0 classifies alignment findings, with suspected drift as
  `operator_required`. Final-QA resolution policy 1.2.0 forces `operator_required` on a blocking
  alignment finding ahead of every other verdict, including the free structural repair.

- `TranslationAcceptanceEvidence` and `TranslationStructuralRepairEvidence` carry an optional
  `alignment_report` and `alignment_qa_version`. Absence means the gate never judged that evidence,
  which is deliberately different from passing it, so no historical acceptance is retroactively
  reinterpreted.

### Added

- **EPUB Builder 1.0.0: the pipeline produces a book.** The last stage is deliberately the least
  clever one in the project. Everything that could be decided has been decided upstream — which
  chapters exist, what order they read in, what each is called, which paragraphs belong to which —
  and a release that verification has passed states all of it. The builder's job is to change the
  container, not the contents.

  `build-epub` (`book_translator.epub`, engine / identity / models / validation / workflow,
  `EPUB_BUILDER_VERSION` 1.0.0) takes one release directory and produces an RTL EPUB 3. Its only
  input is that directory: the package holds no import of a translation run, job, attempt, provider
  artifact, QA record, or snapshot, and no provider import at all. Release verification runs inside
  the plan, and anything short of `READY_FOR_PUBLICATION` refuses the build and names the failed
  checks, so an invalid manifest, a missing chapter, missing provenance, a hash that no longer
  matches, or a stale title artifact all stop the book before a byte is rendered.

  Conversion is total rather than heuristic, because a release chapter is canonical Markdown. The
  first paragraph is the heading and becomes the `<h1>` — checked against the heading the release
  recorded rather than trusted; every other paragraph becomes a `<p>` in published order. A
  multi-line paragraph keeps its lines with `<br/>` instead of being flattened, because verse and
  inscriptions are meaning rather than accident, and a scene separator keeps its literal text. The
  only transformation applied to any text is the XML escaping the format requires, so unescaping a
  document returns the release's bytes exactly.

  The archive is `mimetype` (first, stored), `META-INF/container.xml`, `OEBPS/content.opf`,
  `OEBPS/nav.xhtml`, `OEBPS/styles/rtl.css`, and one XHTML file per chapter named by its canonical
  chapter ID. The package is EPUB 3.0 with `page-progression-direction="rtl"`, every document
  carries `dir="rtl"` and the book's language, and the navigation document is a real EPUB 3 `nav`
  listing every chapter in order under its target-language title.

  **Two builds of one release are byte-identical**, and that is structural rather than incidental.
  The `dc:identifier` is a UUIDv5 derived from the release's own content address, `dcterms:modified`
  is the release manifest's `created_at` rather than the clock, every archive entry carries a fixed
  timestamp and fixed attributes, and entries after `mimetype` are sorted by path so filesystem
  enumeration order never reaches the archive. The build is addressed twice for two different
  questions: `content_sha256` addresses the book inside the container, the archive's own SHA-256
  addresses the container bytes.

  The builder does not trust its own output. The archive is rendered in memory, read back the way a
  reading system would, and checked — container structure and entry order, mimetype, UTF-8, XML
  well-formedness, EPUB 3.0 package with the release's title, language, author, a stable identifier
  and exactly one `dcterms:modified`, manifest and spine carrying the release's chapters in order,
  navigation listing every title in order, each chapter carrying its release title once in a
  right-to-left body, no chapter's bytes changed during archiving, and the archive's content address
  matching the one the plan derived. A build that fails any of that writes nothing.

  `--dry-run` renders the whole book and writes nothing; `--force` is required to replace an
  existing file; and an EPUB may never be written inside the release it was built from, because a
  release is content-addressed by its own manifest and a book dropped into it would fail the
  release's next verification as an unindexed file.

- **Read-only release verification: the gate between assembly and publication.** Assembly proves a
  release was built correctly at the moment it was built. It cannot prove the release is still what
  it was, and it cannot prove it to anyone who was not there. `verify-release` closes that gap by
  re-deriving the answer from the bytes on disk rather than from anything the assembly said about
  itself.

  Release Verification Engine 1.0.0 (`book_translator.release_verification`, engine / workflow,
  `RELEASE_VERIFICATION_ENGINE_VERSION` 1.0.0) is the narrowest stage in the project. It opens every
  file read-only, has no provider import and no path to one, mutates no release, run, snapshot,
  registry, or artifact, and writes only the report an operator explicitly asks for — never inside
  the release it just judged, because a release is content-addressed by its own manifest and a
  report dropped into it would fail the next verification as an unindexed file.

  Seven checks run, and each counts what it examined against what the manifest said to expect, so
  "passed" can never mean "found nothing to look at". The manifest check proves every indexed file
  is present, hashed, and sized, that no published file is missing from the index, and that
  `content_sha256` still addresses the index. Book integrity proves `book.md` is present, UTF-8,
  canonical Markdown, and byte-identical to its chapter files joined. The chapters check proves each
  is present once, in order, with the recorded counts and heading. Titles proves the referenced
  chapter-title artifact is present, still addresses as recorded, still passes the installed title
  QA against the terminology the release published, and that every heading marked
  `title_target_artifact` really is the title the artifact holds. Terminology proves the release's
  copies of the frozen registry and book metadata are those exact frozen bytes, and — when the
  workspace is reachable — that the workspace's own frozen copies have not drifted. Provenance
  proves the lineage describes this run and snapshot, covers every released chunk, and that each
  record names both hashes, its acceptance evidence, its attempt, and a provider resolution
  consistent with the provider it recorded.

  The translations check is the one worth naming separately. Beyond "every chunk is present and
  accepted", it proves the chunks *tile* their chapter's paragraphs — starting at paragraph 2,
  because paragraph 1 is the heading, with no gap and no overlap, ending exactly at the chapter's
  recorded paragraph count. That turns "nothing is missing from this book" from something the
  assembly asserted into something a third party can check from the release alone.

  Two behaviours are deliberate. A manifest that breaks its own contract is reported as
  `invalid_manifest_schema` with every downstream check *skipped*, not raised as an exception: an
  operator asking whether a release may be published deserves that answer in the same shape as every
  other answer. And a check whose evidence is unreachable — the title artifact, when a release has
  been copied away from its workspace — is `SKIPPED` and reported as skipped everywhere, never
  counted as proof.

  Everything in the report except `created_at` is a pure function of the bytes on disk, and its
  `content_sha256` covers exactly that, so two runs over an unchanged release produce the same
  content address and a differing one is evidence that something moved. That holds for failing
  verdicts too. `--json` prints the report, `--output` writes it (JSON by suffix, YAML otherwise),
  `--verbose` adds what each check proved and every recorded digest, and a release that is not
  `READY_FOR_PUBLICATION` exits non-zero.

- **Governed chapter-title translation, and the release overlay it feeds.** A finished book still
  had English headings over Persian text, and no amount of re-reading the translation run could fix
  it: a chapter title never passed through a chunk, so no accepted translation contains one. The
  title is metadata — it lives in the TOC, in chapter metadata, and in every snapshot manifest —
  and it needed a boundary of its own rather than a hole cut in the translation chain.

  Chapter Title Engine 1.0.0 (`book_translator.chapter_titles`, engine / identity / rendering / qa /
  models / artifact / workflow, `CHAPTER_TITLE_ENGINE_VERSION` 1.0.0, `CHAPTER_TITLE_PROMPT_VERSION`
  1.0.0, `CHAPTER_TITLE_QA_VERSION` 1.0.0) translates the titles of one run's frozen snapshot under
  the terminology that snapshot froze, and publishes one immutable artifact at
  `workspace/resources/chapter-titles/<ct-id>/`. It opens the translation run read-only, purely to
  learn which snapshot that run froze, and reads, writes, or judges nothing the run owns — no job,
  attempt, accepted translation, QA verdict, counter, or status. `translate-chapter-titles` is the
  command; `--dry-run` renders every request, contacts nobody, and writes nothing.

  What reaches a provider is one English chapter title plus the canonical terminology decisions that
  title actually contains — no chapter prose, no chunk context, no other chapter's title, nothing
  from an attempt. The output contract `persian-chapter-title-text/1.0.0` is one line of plain text,
  and an answer that is a paragraph, a refusal, a code fence, or the prompt's own framing is refused
  rather than silently trimmed to its first line. Distinct titles are asked exactly once and the
  answer reused, so one English title cannot receive two Persian ones — a property of the artifact,
  enforced by the document contract, rather than a hope about the provider.

  The artifact ID addresses the *inputs* — engine and prompt versions, book and language identity,
  snapshot and manifest hash, frozen terminology hash, and the ordered chapter/title list — not the
  answer. That is what lets a release ask "is there a title artifact for this snapshot" before
  knowing the titles, and what makes a second run over the same inputs collide with the published
  answer instead of quietly creating a rival. The answer is addressed separately by a
  `content_sha256` over the produced titles. `chapter-titles.yaml` carries no timestamp at all, so
  the same titles always serialize to the same bytes; `manifest.yaml` holds the single timestamp and
  the per-file hash index; `provenance.json` holds history — both hashes, origin, provider, adapter
  version, model, request digest, the snapshot and terminology it was bound by, the canonical
  decisions enforced, the QA verdict, and when it was translated. No prompt text and no raw provider
  response is stored anywhere.

  Title QA is pure, provider-free, and runs over the assembled set *before* a byte is written; an
  artifact with any error finding is refused outright. It checks that every snapshot chapter has a
  title and no extra one appears, that no target title is empty, echoes its source, lacks Persian
  script, or is over-long, that both recorded hashes address their own titles, that the recorded
  source title has not drifted from the snapshot's, that one English title maps to one Persian
  title, and — through the installed `validate_terminology` — that every canonical name the source
  title contains survives into the translation. A finding names a chapter and a code, never a
  title's text.

  Release assembly grew an overlay, not a dependency. It discovers the artifact for its snapshot,
  re-runs the full title QA against the terminology *the release itself* froze, and refuses an
  artifact that no longer holds rather than ignoring it; two artifacts for one snapshot fail closed.
  Heading precedence is workspace title, then artifact title, then source title — a `title_fa` a
  person recorded outranks a translated one, because it is a decision rather than a derivation — and
  each chapter records which it used. The release manifest gained `chapter_title_artifact_id` and
  `chapter_title_artifact_sha256`, both optional, so releases published before this change keep
  loading unchanged. With no artifact present the release behaves exactly as it did.
  `--no-chapter-titles` reproduces that behaviour and `--chapter-titles <id|path>` names one
  explicitly.

  The release stage imports only `book_translator.chapter_titles.artifact`, which has no provider,
  no writer, and no clock: a consumer of titles cannot acquire the ability to produce them, and an
  export still makes zero provider calls.

- **Deterministic release assembly: a completed run becomes a book.** Until now the pipeline could
  prove a translation was accepted and could not hand anybody the result. `export-translation-run`
  closes that gap without opening a new one — it is the first stage that produces a distributable
  artifact, and it is also the most strictly read-only stage in the project.

  Release Assembly Engine 1.0.0 (`book_translator.release_assembly`, engine / identity / models /
  reporting / workflow, `RELEASE_ASSEMBLY_ENGINE_VERSION` 1.0.0) consumes exactly two things: one
  completed translation run and the frozen snapshot that run answers. It never touches a run, a
  snapshot, a frozen resource, or the terminology registry, it imports no provider code and has no
  network path through it, and it does not re-judge a translation. Acceptance already happened;
  assembly is not another QA pass.

  Everything is checked before a byte is rendered, and any failure refuses the export: the run is
  `completed`, its counters and status agree with its jobs, its snapshot still validates against the
  workspace and still matches by identity and manifest hash, every job is `succeeded` and `accepted`
  with none QA-rejected or outstanding, chunk coverage is exactly one job per frozen chunk in frozen
  order with no hole and no duplicate, and every accepted translation still exists, still hashes to
  what its acceptance evidence recorded, answers the same frozen source, and is canonical Markdown.
  Effective acceptance is resolved through the same supersession chain the repair path already uses
  — sidecar, then provider-free re-evaluation, then provider-free structural repair, each naming the
  record it supersedes — so re-evaluated, repaired, reused, and carried-forward translations are
  released as the run accepted them rather than as the provider first returned them.

  The join rule is not new. A chapter is its heading paragraph followed by the paragraphs of its
  chunks in frozen order, parsed and re-rendered in the canonical Markdown paragraph form the
  Document Chunk Engine already conserves in the other direction, so a chapter is the reconstruction
  of its chunks and nothing else: paragraph order, scene separators, and every byte inside a
  paragraph survive untouched, and a translation that is not canonical is refused rather than
  tidied. The heading is the only text assembly adds and is never invented — the target-language
  title when the workspace records one, the source-language title verbatim when it does not, with
  the choice recorded per chapter. `book.md` is byte-identical to the chapter files joined with a
  newline.

  A release directory holds `manifest.yaml`, `book.md`, `chapters/<number>-<kind>.md`,
  byte-exact copies of the frozen `metadata.yaml` and `terminology.yaml`, a counters-only
  `translation-report.yaml`, and `provenance/translation-lineage.json`. The manifest indexes every
  published file with its SHA-256 and byte count and carries a `content_sha256` over the sorted
  `(path, sha256)` pairs; it is the only file holding a timestamp, which is what makes "re-exporting
  the same run reproduces the same book" a checkable statement rather than a claim about the clock.
  The lineage records per chunk the source and translation hashes, the acceptance origin and
  evidence, the attempt, the originating provider and how it was established, the QA versions, and
  the paragraph offset where the chunk begins — and no prompt or response material of any kind.

  `--dry-run` renders the whole release and writes nothing. `--force` replaces a destination only
  after confirming its own manifest names the same release, so it re-exports a release and is never
  a licence to delete a directory an operator merely pointed at, and a release may never be written
  into canonical workspace state at all. Publication stages the release beside its destination and
  swaps it in with the manifest written last, so an interruption leaves a staging directory rather
  than a release indexing files it does not contain. Releases contain the complete translated book
  and are gitignored.

  EPUB generation is deliberately not part of this. A format builder now has a stable, hashed,
  provenance-carrying input to read instead of walking a translation run, which is what keeps format
  concerns out of translation state.

- **Provider-free structural repair, and a finalization that reaches for it first.** The last thing
  standing between a finished run and a finished book was `paragraph_count_mismatch`, and it turned
  out not to be one defect. Sometimes a translation contains exactly the right prose in exactly the
  right order and only the blank lines are in the wrong places — one source line written as two
  paragraphs, a paragraph break dropped between two source blocks, a heading's line breaks promoted
  to paragraph breaks. That is decidable from the frozen source alone, and sending it to a provider
  is paying for an answer the workspace already holds.

  Structural Repair 1.0.0 (`book_translator.structural_repair`, engine / models / reporting /
  workflow, `STRUCTURAL_REPAIR_ENGINE_VERSION` 1.0.0) draws the distinction at the level of *lines*
  rather than blocks, which is what makes it answerable. Equal line counts mean nothing is missing
  and nothing is extra, so the correspondence is the identity and the source's own blank-line
  placement is simply re-applied — no search, no heuristic, no ambiguity. Unequal line counts are
  aligned by a monotone dynamic program scored on log-ratios of word and character counts against
  the chunk's own observed expansion, plus a paired-quotation-mark term, and the result is accepted
  only when the line delta is at most 4, there are at most 3 non-identity groups, no group costs
  more than 1.25, and the best alignment beats every materially different alternative by at least
  0.50. Everything else fails closed. A translation that fused two source lines into one is
  *always* a provider's question, however certain the alignment: choosing where to break translated
  prose is a judgement about meaning.

  A repair re-emits the translation's own lines in their own order with separators taken from the
  source, so it changes whitespace and nothing else — the renderer asserts that on its own output
  before returning. The repaired bytes are published to
  `jobs/<chunk>/structural-repair/NNN/translation.md` beside a
  `TranslationStructuralRepairEvidence` record carrying the superseded verdict it answers, the
  provider attempt the bytes still descend from, both translation digests, the full diagnosis, and
  both validators re-run over the repaired text. The provider attempt, its translation, and its
  acceptance sidecar are never touched; what moves is the job's effective verdict, under the new
  acceptance origin `structural_repair`, in exactly the shape a provider-free re-evaluation already
  used. Adoption is refused unless block correspondence is genuinely restored and no blocking code
  appears that the standing rejection did not already carry, so a structural repair can only remove
  findings — which is what makes it safe to run before anything else.

  That last property matters more than it looks. Terminology QA has been occurrence-anchored since
  1.6.0, and an occurrence-anchored validator has no correspondence to anchor to when the block
  counts disagree, so it fails closed. A `competing_variant` that existed only because the alignment
  was broken therefore clears on its own once the blocks line up — and one that does not clear stays
  exactly as blocking as it was, and is answered by a provider repair in the next cycle. Nothing
  about terminology enforcement was weakened to reach acceptance.

- **Governed final QA cleanup for a completed translation run.** A run that ends
  `completed_with_failures` was, until now, the end of the automated road: every remaining job
  carried a QA rejection and nothing decided which of them were translation defects and which were
  QA verdicts that had stopped being true. `audit-final-qa` and `finalize-translation-run` close
  that gap, and the audit half is the point — a read-only diagnosis of every standing rejection,
  published to `workspace/audits/final-qa/<run>/audit.yaml`, is what an operator reads before
  authorizing anything.

  A new pure, separately versioned policy (`FINAL_QA_RESOLUTION_POLICY_VERSION` 1.0.0) answers one
  of seven verdicts per job: `accepted_no_change`, `reevaluate`, `repair_translation`,
  `resolve_terminology_then_reevaluate`, `resolve_terminology_then_repair`, `exhausted`, or
  `operator_required`. It reads the *current* QA reports, never the historical ones, so a rejection
  current QA no longer reproduces is settled by provider-free re-evaluation rather than by a repair
  invented to justify a finding that has stopped existing. Only the first three verdicts may be
  acted on automatically; the rest are reported. The stage adds no provider path, no acceptance
  path, and no remedy of its own: re-evaluation, repair, and provider recovery are reached through
  the existing boundaries with their existing provenance.

  **`competing_variant` is diagnosed rather than obeyed.** One finding code was covering at least
  eight unrelated situations, which is why the auto-repair policy refused it on sight. A new
  deterministic analyzer classifies each occurrence from governed evidence only —
  `canonical_identity`, `retired_competitor`, `historical_output_only`, `canonical_absent`,
  `locked_canonical_conflict`, `rival_ai_decisions`, `genuine_canonical_conflict`, or
  `insufficient_evidence` — recording, per finding, the decision and its state, ownership,
  enforcement and lock, whether the canonical and the variant appear in the translation, source and
  target occurrence counts, whether the effective registry still records the rivalry, and whether
  current QA still reproduces it. A locked or user-owned canonical is never the side that gives
  way: the decision stands unchanged and the translation is what may be repaired. Where nothing
  governed explains the occurrence the stage fails closed, and the CLI groups the unresolved cases
  by terminology decision rather than by chunk, because a handful of decisions is what a page of
  rejected chunks usually is.

  The two `resolve_terminology_*` verdicts are reported, not executed. A run's terminology resource
  is frozen into its identity, so settling a registry question for that run means re-freezing
  terminology, taking a new snapshot, and carrying accepted work forward with
  `reuse-accepted-translations` — not editing a frozen run.

  `--dry-run` is the default for `finalize-translation-run` and guarantees zero provider calls,
  zero registry writes, zero queue writes, and zero acceptance changes while still reporting the
  planned verdict for every rejected job.

- **Exceptional repair-budget escalation.** A job whose provider budget is spent behind a
  mechanically repairable defect is `exhausted`, and by default that is where it stops.
  `finalize-translation-run --allow-repair-escalation` authorizes at most one grant per job. A
  `TranslationRepairEscalation` record (`REPAIR_ESCALATION_VERSION` 1.0.0) is published under
  `jobs/<chunk>/repair-escalation/NNN/` before the ceiling moves, naming the ceiling it raises, the
  attempts it grants, the budget already spent, and the blocking findings it is spent on. No
  historical counter is reset. The queue now enforces the rule structurally: a job's attempt
  ceiling must equal its run's unless persisted escalations account for exactly the excess, so a
  hand-edited ceiling or a lost record is a stale queue rather than extra budget. The queue engine
  version is unchanged, because every existing job satisfies the stricter reading.

### Changed

- **`finalize-translation-run` is one command for the whole ladder.** The orchestrator now takes
  one step per cycle and re-diagnoses the workspace from scratch afterwards rather than reasoning
  forward from what it just did, which is what lets the rungs compose without knowing about each
  other: re-evaluate what current QA already accepts, repair block structure for free where the
  source proves how, spend a provider repair on what is left, hand a repair the provider never
  delivered to the existing provider-recovery boundary and re-send it once, and — only under
  `--allow-repair-escalation` — grant one bounded exceptional attempt to a job whose budget is
  spent. `--no-provider-recovery` switches the recovery rung off; the preflight it needs is taken
  lazily, at most once per finalization, and dropped again the moment a recovered attempt fails.
  `DEFAULT_MAX_CYCLES` is 4, the length of the longest legitimate chain.

  `FINAL_QA_RESOLUTION_POLICY_VERSION` is 1.1.0 for the new `repair_structure` verdict. It is the
  one verdict that outranks the others instead of yielding to the most conservative one, and
  deliberately so: it costs nothing, cannot introduce a finding, and is re-diagnosed from scratch
  afterwards, so reaching for a paid remedy while a free proven one is on the table is never right.
  The audit engine and orchestrator are 1.1.0; `FINAL_QA_ANALYSIS_VERSION` is 1.2.0 and now
  delegates the structural half to the repair engine outright, so "final QA thinks the paragraphs
  diverged" and "a deterministic repair exists" cannot drift apart. Every diagnosed job carries a
  structural record — block and line counts, defect class, alignment groups, cost and margin in
  integer thousandths, refusals — and a dry run names the step an applying run would take rather
  than only reporting that nothing has happened yet.

- **An exceptional repair-budget escalation now has to show that nothing free was left.**
  `REPAIR_ESCALATION_VERSION` is 1.1.0: the record gains the attempt slot it authorizes, the reason
  the grant was needed, the structural diagnosis behind it, an explicit
  `deterministic_repair_available` that must be false, and `provider_contacted: false`. Granting is
  refused outright while a deterministic structural repair is still available, so a provider budget
  is never widened to buy something the workspace could have given away. The bound, the
  one-grant-per-job rule, and the refusal to reset any historical counter are unchanged.

- A repair the provider *delivers* now clears `acceptance_origin` and the structural-repair pointer
  along with the acceptance fields it already cleared, because a new draft supersedes every derived
  verdict about the old one. `reevaluate-translation-job` refuses a structurally repaired job for
  the same reason: re-evaluation judges the provider attempt's own output, and running it there
  would assess a draft the job no longer stands on.

- **Terminology QA 1.4.0 -> 1.5.0: a competing variant carrying the canonical's own identity is no
  longer enforced.** `valid_competing_variants` already refused a variant that occurs *inside* the
  canonical target. The mirror case was not refused, and it is the same defect: when the canonical
  occurs as a lexeme inside the variant (`تالانور` inside `مارتین تالانور`), or the variant is a
  target-language derivation whose stem is the canonical (a Persian plural), the two are one
  lexeme. Such a variant cannot evidence a *rival* rendering — any text it matches necessarily
  matches the canonical too — so the finding it produced was unfalsifiable. Enforcement now
  disregards it through `enforceable_competing_variants`; registry *storage* rules are unchanged,
  so no decision is rewritten and no recorded variant is lost. The change is monotone: it can only
  remove findings, never add one, so no previously accepted translation can regress.

- **Structural QA 1.0.0 -> 1.1.0: a run of blank lines is typography, not a paragraph.** Paragraph
  counts were taken by splitting on the literal `\n\n`, which counted a stray blank line as an
  extra paragraph on whichever side carried it and made a purely typographic difference look like
  omitted or invented content. Both sides are now segmented on one-or-more blank lines with empty
  blocks dropped, so the comparison stays a comparison. This too can only remove findings.

  Both are **minor** bumps: derived QA bytes may intentionally change, which makes existing
  acceptance evidence re-evaluable through the ordinary provider-free re-evaluation boundary. No
  historical artifact is rewritten.

- **Auto-repair policy 1.0.0 -> 1.1.0: a per-finding classification override channel.** A finding's
  code says what QA saw, not always what it means. A caller holding strictly stronger, separately
  versioned evidence about one specific finding may now reclassify it through
  `terminology_overrides`, keyed by `code:candidate_id`. The default is empty and the
  classification is unchanged without it; the override is recorded in the persisted repair
  authorization alongside every other classification. The only current caller is final QA, and only
  for a `competing_variant` it has proven to be `canonical_absent` — a translation that used the
  rival rendering and no canonical at all.

- Bounded automatic recovery of a **repair** that a provider failure ended, closing the last
  routine case that still required an operator: a translation QA rejected, a repair authorized to
  answer that rejection, and a provider that failed to carry the repair out. Until now such a job
  stopped there, because it still carried its QA-rejection state and could not safely re-enter as
  an ordinary translation recovery. Provider-recovery engine and policy versions are unchanged at
  1.0.0; the recovery contract gained fields with defaults, so existing decision records stay
  valid unchanged.

  One recovery boundary now serves both kinds of request, rather than a second recovery system
  growing beside the first. The kind is read from the job and never chosen by an argument: a
  provider failure with no assessed draft is a translation, a provider failure that left a
  standing QA rejection intact is a repair. `translation_provider_recovery` gained
  `is_repair_recovery_candidate` and `recovery_execution_kind` beside the existing
  `is_provider_recovery_candidate`, and the pure policy gained an `execution_kind` and a
  `RepairReplayEvidence` conjunct rather than a parallel policy.

  **A recovery of a repair stays a repair.** The recovered attempt keeps
  `execution_kind: repair` and keeps `repaired_from_attempt` pointing at the QA-rejected
  *translation* the repair answers — the repair root — never at the failed repair, which produced
  no translation and is therefore nothing to repair from.

  **The exact request must be provably the same one.** A translation is defined by a frozen chunk
  and a rendered prompt package; a repair request is *derived* at send time from the rejected
  draft, the QA findings, and the frozen resources. So recovery re-derives it and matches both
  provider-facing message digests against what the failed attempt recorded sending, over a hash
  chain covering the effective rejection evidence, the rejected attempt, the rejected translation,
  and the frozen source, context, prompt, style-guide, and terminology resources. Tampered QA
  evidence, a tampered or unreadable request payload, an edited `repair-system.md` or
  `repair-user.md`, a frozen resource that no longer matches the manifest, a lineage that points
  at a different rejected attempt, or a rejection that no longer carries a blocking finding each
  stop the recovery with `operator_required` and a named reason. `translation_repair` grew a
  `RepairRequest` value carrying the derivation's input hashes and an identity digest binding them
  to the lineage and the repair prompt version; it holds prose only to send, never to store.

  **The job is restored rather than re-queued.** A recovered repair deliberately does not pass
  through `pending`, because a pending job is claimable as an ordinary translation and this one
  carries a successful attempt that ordinary execution refuses outright. Instead the authorization
  restores the QA-rejected draft the failed repair displaced — `status: succeeded` with
  `successful_attempt` and `completed_at` read back from the immutable accepted attempt — through
  a new `restore_repair_target` queue operation guarded by a keyword-only `repair_recovery` opt-in
  on `can_transition_translation_job`, the only thing that opens a `failed -> succeeded` edge. The
  failed attempt stays on disk, immutable, still named by `last_attempt`, and still counted
  against the provider budget; the repair then runs through the same mechanism an operator drives
  by hand.

  **Budgets stay four, not three.** A failed repair call consumed one provider attempt and one
  repair *execution*; its recovery consumes another of each plus one provider-recovery attempt,
  and **no second QA repair decision** — QA chose that repair action once and the provider merely
  failed to deliver it. The repair-decision count is now derived as repair-kind attempts minus the
  attempt slots a repair recovery authorized, so a provider outage can no longer exhaust the
  repair allowance without QA having reconsidered anything. Translation recovery and repair
  recovery share one `--max-provider-recovery-attempts` allowance per job, which is strictly the
  more conservative reading of "how many times may an unattended process re-enter this job".
  A recovered repair that succeeds and is *still* rejected continues into the ordinary auto-repair
  boundary, which authorizes another repair only if the repair budget permits.

  Selection is four classes, each exhausted in canonical queue order before the next is offered:
  unassessed drafts and pending translations, then QA repairs, then failed translations, then
  failed repairs. Remediation never reorders the canonical pending queue. Selecting a failed
  repair needs `--auto-recover-provider-failures --include-failed` *and* `--auto-repair`, because
  it is both a recovery and a repair execution; `--include-qa-rejected` is deliberately not
  required, since that flag admits new QA repair decisions and a recovery makes none. A worker can
  therefore resume a repair an earlier pass stranded with no operator setup at all. No new CLI
  command and no new flag were added.

  The decision record adds `recovered_execution_kind` (defaulting to `translation`) and, for a
  repair, a `TranslationRepairRecoveryLineage` block: repair root attempt, failed repair attempt,
  the auto-repair decision that spent the QA decision being replayed (`null` when an operator
  drove it), the effective rejection's origin, file, and SHA-256, the blocking finding codes, the
  rejected attempt and translation hashes, the frozen resource hashes, the repair prompt version,
  the output contract, both message digests, the derivation identity digest, and the repair
  decisions charged so far. The contract refuses a lineage whose failed repair does not follow its
  repair root, whose request was not reproduced exactly, or that disagrees with the decision it
  sits in. Every field is a hash, a path, an integer, or a finding code; no derived repair prose
  is ever persisted. `--dry-run` reports `would_recover_failed_repair` with the lineage it would
  preserve, and still makes no model call and no write. Worker results gained `repair_decisions`,
  `repair_recovered`, and `repair_recovery_attempts` so a repair *execution* and a repair
  *decision* are never reported as the same number.

- Bounded automatic provider-failure recovery in the translation worker (provider-recovery engine
  1.0.0, provider-recovery policy 1.0.0), so a transient provider failure no longer strands a job
  permanently merely because the failure classifier marked that individual attempt non-retryable.
  A book that must translate end-to-end without human intervention cannot have one expired login
  cost it a chunk forever.

  Historical attempt classification stays immutable. An attempt recorded as
  `retryable: false` was genuinely not retryable when it was made, and nothing rewrites it; the
  new decision says instead that the historical failure was non-retryable *at attempt time*, that
  provider health has since been re-established, and that recovery policy authorizes a new
  execution attempt. The recovery record cites the failed attempt by number, ID, path, and
  SHA-256 rather than touching it.

  Five concerns are kept apart rather than collapsed. Failure classification remains the
  executor's, on the attempt. Provider health remains the existing preflight. The policy is pure
  and separately versioned in `provider_recovery_policy`: it maps every executor error category
  to a provider-neutral family (`transient`, `rate_limit`, `authentication`,
  `provider_unavailable`, `malformed_response`, `contract_violation`, `content_policy`,
  `local_environment`, `ambiguous_execution`, `unknown`) and answers `recoverable_now`,
  `wait_for_backoff`, `preflight_required`, `operator_required`, `unsafe_to_replay`, or
  `exhausted`. Only families that assert something about provider availability are recoverable;
  a malformed response, a contract violation, a content refusal, a local fault, and any
  unclassified failure — including a bare nonzero exit — fail closed however healthy the provider
  now looks. Authentication and provider-unavailability require a fresh preflight taken after the
  failure, which the pure policy cannot obtain and so refuses to assume. An ambiguous execution is
  never replayed and stays with `resolve-translation-reservation`. Orchestration lives in
  `translation_provider_recovery` and performs no provider execution of its own: it publishes an
  authorization, re-enters the job through the ordinary `failed -> retry_pending -> pending`
  lifecycle, and lets the ordinary translation path execute it, so a recovered attempt reaches
  the same QA acceptance and the same automatic repair as any other.

  `--auto-recover-provider-failures` is off by default and changes nothing without it;
  `--include-failed` requires it and places already-failed jobs in their own selection class after
  pending translation and after QA repair, so canonical pending ordering never changes.
  `--max-provider-recovery-attempts` (default 2) is a separate budget from `--max-repair-attempts`,
  from provider transport retry, and from the queue's `max_attempts`; the effective bound is the
  smaller of the recovery allowance and the job's remaining provider attempts. A transient failure
  is additionally not replayed before the run's backoff has elapsed since it happened, and a rate
  limit is rested five times as long; the preflight-gated families are bounded by that proof and
  the budget rather than by a timer, because a login a preflight has just verified is not more
  verified a minute later.

  Each authorized recovery publishes an immutable
  `jobs/<chunk>/provider-recovery/NNN/decision.yaml` *before* the queue is touched — policy and
  engine versions, failure family, the referenced failed attempt with its untouched `retryable`
  and `ambiguous` flags, replay safety established from artifacts, both budgets, the preflight
  evidence, the authorized attempt number, and `initiator: automatic`. Preflight evidence records
  provider identity, executable, CLI version, authentication status, and auth method as short
  structural values, and the contract itself refuses credential-shaped values and any API-key
  path. `retry_job` gained an optional explicit eligibility time, used only by a governed recovery
  whose policy computed the spacing from when the failure actually occurred; it can never bring a
  retry forward of the present moment, and the default backoff behaviour is unchanged.

- Bounded automatic QA repair in the translation worker (auto-repair engine 1.0.0, auto-repair
  policy 1.0.0), so a mechanical QA rejection no longer requires an operator to run
  `repair-translation-job` by hand before a book can continue. The repair *mechanism* was already
  there and is unchanged; what was missing was anything able to decide whether spending a provider
  attempt on a given rejection is safe, which is exactly the decision an unattended run has to make
  on its own.

  Three concerns are kept apart rather than collapsed into worker control flow. The mechanism stays
  in `translation_repair`, gaining only an `initiator` so an automatic decision is distinguishable
  from an operator one afterwards. The policy is pure and separately versioned in
  `translation_repair_policy`: it classifies every finding of the *effective* QA evidence as
  `auto_repairable`, `reevaluation_candidate`, `non_repairable`, or `operator_required`, and
  authorizes a repair only when every blocking finding is auto-repairable.
  `paragraph_count_mismatch` and `canonical_term_missing` under exact or morphological enforcement
  qualify, because the frozen source and the governed registry already state exactly what must
  change; `competing_variant` does not, because a known rival rendering is evidence that the
  canonical decision itself is contested. An unrecognized code is `operator_required`, so a QA engine that later grows a new blocking code
  inherits no permission to spend attempts on it, and a rejection mixing a repairable finding with
  an unsafe one is refused whole rather than half-repaired. When the effective evidence records QA
  engine versions that are no longer installed the verdict is `reevaluation_required`, pointing at
  the existing provider-free re-evaluation boundary instead of a paid repair.

  Orchestration lives in `translation_auto_repair` and is bounded by construction. `--auto-repair`
  and `--include-qa-rejected` are both off by default, so no existing invocation changes behavior;
  `--include-qa-rejected` requires `--auto-repair` and places already-rejected jobs in their own
  selection class *after* all pending translation work, because pending queue ordering is a contract
  that remediation must not reorder or starve. `--max-repair-attempts` defaults to 2 and is counted
  from the immutable attempts, so a hand-driven repair is charged against the same budget; the
  effective bound is the smaller of it and the job's remaining `max_attempts`, and each cycle claims
  an attempt slot before contacting a provider, which makes the loop strictly decreasing. This is
  deliberately not provider transport retry: that answers a request that produced no usable result,
  while a repair answers a delivered result that QA rejected.

  Provenance is additive and never rewritten. Each authorized repair publishes an immutable
  `jobs/<chunk>/auto-repair/NNN/decision.yaml` *before* the provider call — policy version,
  classified findings, triggering codes, budget, repaired-from attempt, hash-verified rejection
  lineage, `initiator: automatic` — so an interrupted repair can still explain the slot it consumed;
  re-entering that slot reuses the record and a contradicting one stops the repair. Outcome evidence
  is not duplicated into it, because `attempts/NNN` and `acceptance/NNN` already hold the provider
  status, resulting QA findings, and final acceptance. The only route to `accepted` remains the
  ordinary acceptance sidecar QA publishes over a real repair attempt, so an attempted repair never
  launders a rejection, and a repaired job is counted once — one accepted job, two provider attempts.
  Against the real `tr-9df726a15a52-001` run, a read-only dry run classifies the
  `000-prologue-002` control's `paragraph_count_mismatch` as `repairable` and plans attempt 2, with
  no provider call and byte-identical run state.

- Governed cross-run carry-forward and re-evaluation of historically QA-rejected outputs
  (`reevaluate-historical-output`, carry-forward engine 1.0.0, compatibility policy 1.0.0). Neither
  existing command reached this case: same-run `reevaluate-translation-job` needs an attempt inside
  the run being assessed, and `reuse-accepted-translations` consumes only effectively accepted
  outputs, so a provider-successful output that its own run rejected had no path into a newer run
  except a second provider call for a translation that already exists. The new workflow proves
  compatibility, carries the exact historical bytes forward as evidence, and runs the new run's
  frozen QA against them, with `provider_contacted: false` throughout.

  The compatibility policy is deliberately not reuse's. Reuse may demand a byte-identical rendered
  request because an accepted translation has nothing to gain from a changed one; these candidates
  are valuable *precisely because* terminology QA moved after the output was produced, so that rule
  would refuse every one of them. Instead the request contract — source, context, languages, system
  message, and output contract, including the historical attempt's own recorded contract — is never
  waivable, and neither is a change to a contract-bearing framed section
  (`translation-prompt-rules`, `style-guide`, `previous-context`, `source-text`). A changed rendered
  user message is eligible only as `governed_resource_drift`: the difference confined to the framed
  `glossary-data` projection, every non-terminology resource byte identical, the terminology
  resource the one that moved, that move a contiguous chain of published governed registry edits
  resolved through `explain_registry_drift` over decision corrections and applied remediation plans,
  and no locked or user-owned canonical decision reaching the job changed inside it. Everything else
  is `contract_changed` or `unsafe_drift` and blocked. Eligibility rests on lineage and current QA,
  never on a term-specific exception.

  Historical QA is retained rather than erased. `TranslationHistoricalReevaluationEvidence` copies
  the effective historical rejection and its findings into the new record beside the current QA
  reports, so a carried-forward job says both what it was rejected for and what the newer snapshot
  makes of it. The destination job gains acceptance origin `historical_reevaluation` and
  `carry_forward_count`, distinct from reuse's origin and counters; `attempt_count` stays 0 and no
  provider attempt is ever created. On the real pair of runs, dry runs classified 001-004, 001-006
  and 002-001 as `governed_resource_drift` whose historical `canonical_term_missing` errors are all
  resolved by the governed registry move (`accepted` under current QA), while the 000-prologue-002
  control is equally eligible and stays `qa_rejected` because its `paragraph_count_mismatch`
  persists — carrying an output forward never promotes it.

- Effective-request reuse compatibility, reuse engine 1.1.0 and compatibility policy 2.0.0. The old
  rule was a conjunction of whole-resource hash equalities, so editing the single global terminology
  resource invalidated every historically accepted translation at once: against the real pair of runs
  all 206 jobs reported `terminology_hash_mismatch` while only 69 rendered user messages had actually
  changed. Resource equality was never the property that mattered, and it never even proved request
  equality — the open-question registry is rendered into the prompt but is not a package resource
  reference. Compatibility is now decided on effective job inputs: because every frozen resource
  reaches a provider through exactly one channel, the deterministically rendered system and user
  messages, equal source, context, languages, output contract, and both rendered message digests mean
  the historical provider-facing request *is* the new one. The `exact_rendered_request` tier
  re-verifies that the bytes behind both recorded digests are still present and unchanged before it
  waives anything, records the waived drift and the digests that justify it in durable
  `rendered_request_compatibility` provenance with `provider_contacted: false`, and still runs current
  terminology and structural QA before the job may become accepted. A changed rendered message is
  never waived; it is blocked and attributed to the framed user-message section whose content digest
  moved, read from the section headers alone so no framed content is inspected. On the real runs this
  turns 12 effectively accepted jobs into 4 reusable and 8 blocked with a named cause, instead of 12
  blocked by a global hash.

- Governed component-spelling correction (`correct_target`) and lineage-aware drift auditing.
  Retiring the bare `ring` decision uncovered a contradiction it had been masking: `ring
  ter'angreal` was rendered «تر‌آن‌گرئال حلقه‌ای», a transliteration of `ter'angreal` that appears
  in no other canonical target, in no translated attempt, and that three other decisions already
  record as a *rejected* competing variant — against «تر‌انگریال» on the 50-occurrence canonical
  decision plus `doorframe ter'angreal` and `doorway ter'angreal`. Composing a target is a
  translation act and stays forbidden, but this is its opposite: the compound already renders its
  component in the right place and the right order and only misspells it. `correct_target`
  therefore fires only when exactly one decided component is missing from a target and exactly one
  token of that target is unaccounted for by any other component, substitutes the component's own
  canonical target for that single token, and copies every other token verbatim. An elliptical
  shorthand is never such a component — the same exclusion the canonical validator already applies,
  which is what stops «آجای سرخ» being pushed into `Red Ajahs`. Because respelling is what first
  makes a compound honor its component, and therefore first makes its exact claim over-broad, the
  correction also carries the enforcement relaxation its own fix uncovers, so the plan converges in
  one round: against the current registry the dry run now leaves zero blocking findings.

### Fixed

- **`competing_variant` proved nothing about the occurrence it blocked** (terminology QA
  `1.5.0` -> `1.6.0`, final-QA analysis `1.0.0` -> `1.1.0`). The check asked whether a recorded rival
  string occurred *anywhere in the chunk*, which is not a claim about any source occurrence, so a
  finished book tripped it constantly: an alias, a title with a surname, a short name beside a full
  one, a descriptive reference, a source-provided gloss, a plural, or a different source term whose
  canonical target happens to be related. On the first completed run this produced nineteen
  `operator_required` chunks reducing to twelve terminology decisions, and reading the source context
  around every one of them found that all twelve alleged competitors rendered a *different source
  expression* than the decision governed. Nothing was contested; the validator had reported
  co-occurrence and called it a rival rendering.

  The finding now requires positive evidence that the variant realizes the same source occurrence
  the decision governs. No span-for-span alignment is persisted, so QA anchors to the structural
  correspondence the pipeline already contracts on — one target block per source block, which is
  what `paragraph_count_mismatch` exists to enforce — and to nothing weaker. A variant blocks only
  when, in one aligned block, that block translates a source block carrying an authoritative,
  non-alias occurrence of this decision's source term (*locality*) **and** carries fewer canonical
  realizations than it has governed occurrences to realize (*deficit*), so at least one governed
  occurrence there is genuinely unrendered. Without a deficit every governed occurrence is already
  accounted for canonically and the variant is rendering something else. A new pure module,
  `terminology/alignment.py`, holds the block correspondence and offers no fuzzy matching, no length
  heuristic, and no partial alignment: a wrong alignment would silently move evidence between
  occurrences, which is the failure it exists to prevent.

  **Undecidable alignment fails closed.** Different block counts mean no correspondence, and the
  finding stands exactly as it did before — reached only for a translation whose block structure
  already differs from its source, which the structural validator blocks on separately. The
  terminology question becomes decidable when that structural defect is repaired, and not before.

  **The change only ever removes findings.** The rule is the old condition plus a conjunctive gate,
  so its finding set is a subset of the pre-1.6.0 one for every input; a translation that carried no
  terminology error cannot acquire one. That is what makes the bump installable over a run holding
  179 accepted jobs. Canonical enforcement is untouched: `canonical_term_missing`,
  `canonical_term_unverified`, enforcement-mode resolution, coordinate ellipsis, and locked and
  user-owned decisions all behave exactly as before, and nothing is whitelisted by string — the rule
  is derived from the source and the registry alone.

  `evidenced_competing_variants` is now the single source of truth: the validator raises exactly
  what it returns, and the final-QA analyzer reports `enforceable` from it rather than recomputing a
  second opinion, so an audit can no longer tell a reviewer that the installed validator would fire
  on something it would not. Existing artifacts recorded under `1.5.0` are stale for this finding
  and are re-evaluated, never rewritten.

- Governed registry lineage modelled only one writer. `explain_registry_lineage` explained the live
  terminology registry from decision corrections alone, so once a governed remediation plan was
  applied — retiring six decisions and moving the registry from 894 to 888 — the live registry looked
  ungoverned even though every step was recorded. A remediation application publishes the semantic
  identity it moved the registry from and to, exactly as a correction does, so it is the same kind of
  evidence. Lineage now lives in its own pure module, `terminology/lineage.py`, over a
  writer-agnostic `RegistryTransition`; `registry_transitions` merges corrections and applied
  remediations into one chronologically ordered chain, and `load_applied_remediations` reads a plan
  as evidence only when its published dry run sits beside it, which is what the executor writes and
  a review-only plan never gets. Against the pilot corpus the chain now reads
  `trp-90ae32ee…` → `tdc-18f94c…` → `trp-be33e0bf…`, whose head is the live registry, with the
  correction handing over to the remediation on the same hash.

- Reuse ran current QA against `config/terminology.yaml` — the mutable path a snapshot manifest
  names — instead of the content-addressed frozen copy the snapshot actually pins, so a registry
  edited after the new snapshot was created would silently decide acceptance. QA now resolves the
  frozen resource, verifies it against its recorded digest, and refuses on drift; planning reports
  `frozen_terminology_unavailable` so `--dry-run` surfaces it. The chunk source read for QA is
  hash-verified for the same reason.

- The book-identity migration guard asserted that `config/terminology.yaml` still hashes to the
  value recorded at migration time, which two governed decision corrections had legitimately moved.
  Comparing a live registry against an immutable record proves only that time passed. Lineage is
  now modelled as a chain: `explain_registry_lineage` reports whether the live registry identity is
  the head of the governed corrections published after a given record, keeping gaps visible without
  treating them as failures, since the migration itself may sit between two corrections. The
  historical record is untouched and its hashes are pinned, so a record rewritten to match current
  state fails the guard instead of passing silently.

- Exact-enforcement remediation, remediation engine 1.1.0. `audit-terminology-exact-enforcement`
  could describe 134 suspicious decisions but nothing could act on them: the remediation planner
  derived its findings solely from canonical-validator conflicts, which ask whether two canonical
  records contradict each other, and a bare common noun or a finite clause promoted to terminology
  contradicts nothing — so it produced no finding, no operation, and no way forward. The CLI could
  not help either, because `reject-terminology-candidate` correctly refuses a candidate that still
  holds a canonical decision, and there is deliberately no command that deletes one.
  `audit-terminology-registry-remediation` now reports both finding families over one registry
  read, so a single plan, dry run, and apply covers both. A new pure module owns the risk-to-remedy
  mapping and classifies every flagged decision as `definitely_invalid`, `definitely_valid`,
  `context_review_required`, or `unresolved` from evidence the frozen registry already holds — a
  finite verb, an elliptical shorthand the canonical view already recognizes, a target that
  provably survives inside a longer canonical target. Four operations follow:
  `retire_and_reject_candidate` withdraws a decision and rejects its candidate in one atomic
  publication, `retire_decision` withdraws the decision alone when the form remains another
  candidate's alias, `change_enforcement` relaxes an over-broad exact claim without touching target
  text, and `open_terminology_question` records the durable question a defect needs without
  mutating anything. A retirement exists only inside an immutable plan, carries the SHA-256 of the
  decision and candidate records it was built against, refuses to apply if either drifted, and can
  never name a locked or user-owned decision — the contract pins `locked` to false and the planner
  re-proves the ownership rule rather than trusting its input. Against the current registry the
  plan retires six decisions (`ring`, `Red`, `took the Stone`, `Aldeshar fell`, `redstone
  doorframe`, `twisted redstone doorframe`), relaxes seven descriptive compounds that already
  honor the canonical name inside them, upholds 44, and refers 77 to a durable question. Because
  withdrawing a decision can uncover a contradiction the withdrawn component was masking, a dry run
  now proves safety by requiring that every surviving blocking finding was already referred to a
  human by that same plan, instead of requiring zero.

- Coordinate-elided canonical identity and terminology QA 1.4.0. The second real translation batch
  rejected five of sixteen chunks, and one rejection was a false positive of a kind enforcement
  modes could not reach: the source reads "High Lord or Lady", which is one noun phrase with two
  heads, and Persian writes the shared modifier once — «لرد یا بانوی اعظم». The canonical target
  «لرد اعظم» is fully present, in order, but not contiguous, so exact enforcement reported an
  omission the translator did not commit. Canonical identity is now additionally satisfied by an
  ordered discontinuous realization, gated on evidence the frozen registry already holds: the
  source match must lead an `and`/`or` coordination whose second conjunct, formed by substituting
  the coordinated head into the matched term, is itself an approved decided candidate; each gap is
  at most three words, may not cross a line, and one gap must contain a target coordinator. Order
  is never relaxed, locked and user-owned decisions stay contiguous, and an uncoordinated source
  occurrence still demands the contiguous target. The relation is derivable from frozen data, so
  `reevaluate-translation-job` applies it without a new snapshot. Recomputed over the sixteen
  provider-successful translations it accepts one previously rejected chunk and changes nothing
  else.

- `audit-terminology-exact-enforcement`, a read-only census of which canonical decisions should
  never have been enforced as exact target strings. Four QA rejections in a row traced to four
  different causes, only one of which was a translation defect, so the pipeline needed a way to
  find the rest before they are discovered one translated chunk at a time. The audit reports six
  risks — `containment_inconsistency`, `verb_phrase`, `descriptive_phrase`, `coordinated_source`,
  `source_fragment`, and `composite_of_decided_terms` — and changes no QA behavior, because a false
  positive caused by bad registry data is fixed by remediating the registry and freezing a new
  resource, not by loosening the matcher. Containment is compared modulo ezafe, ZWNJ, hyphenation,
  and a plural `ی`, so target grammar is not mistaken for a contradiction. Against the current
  frozen resource: 840 exact decisions, 465 of them multiword, 134 suspicious, including 8
  containment contradictions and 2 finite verb phrases.

- Terminology enforcement modes (`exact`, `morphological`, `contextual`, `advisory`) and terminology
  QA 1.3.0. The first real translation batch rejected two of five chunks, and two of its three
  terminology findings were false positives with the same cause: `required: true` was read as "the
  exact canonical Persian string must appear", so a demonym had no legitimate way to be rendered
  through its own place name. Persian says "the army of Saldaea" for "the Saldaean army" and "the
  throne of Cairhien" for "the Cairhienin throne"; both were reported as omitted terminology.
  `required` now answers only whether a decision is in scope, and `enforcement` answers what counts
  as proof. `morphological` also accepts a derivational stem of the canonical target and the
  canonical target of the decision the *source* form is derived from — a relation computed from the
  source language, because the two Persian targets need not resemble each other. `contextual`
  downgrades absence to a new `canonical_term_unverified` warning; `advisory` reports nothing.
  Findings now name the mode that produced them.

  A decision that records no mode resolves from facts the registry already holds, so a frozen
  terminology resource is re-evaluable under corrected semantics without being rewritten and
  `reevaluate-translation-job` needs no new snapshot. Ownership outranks classification: a locked or
  user-owned decision stays `exact` whatever it is classified as, and a locked decision refuses any
  other mode. Of the 894 current decisions, 840 remain `exact` and 50 (`people`) become
  `morphological`; the 4 `contextual` ones sit on context-sensitive candidates QA never selected.
  The change only ever relaxes, so no accepted translation can become rejected by it.

- Execution Reservation Resolver 1.0.0, `resolve-translation-reservation`, and
  `inspect-translation-reservations`. Provider execution writes `attempts/.NNN.reservation.yaml`
  before the network call; once it says `requesting`, delivery is unknowable from the workspace, so
  automatic recovery refuses forever and the job stayed `running` with no sanctioned way forward
  short of hand-editing run YAML. The resolver is the governed way out: the operator asserts one
  external fact (`provider_not_contacted`, `provider_contacted_no_result`, or
  `provider_contact_unknown`) with a reason, and each fact has fixed consequences — return the job to
  `pending` without spending a provider attempt, fail it with the attempt spent so only an explicit
  `retry-translation-job` can continue, or block it with no queue replay path at all. There is
  deliberately no generic force reset. The command plans by default and needs `--apply`; the
  reservation stays byte-identical, and an immutable `jobs/<chunk>/resolutions/NNN/resolution.yaml`
  record (`xrr-<24-hex>`, operator provenance) captures the reservation hash, reserved request
  identity, both attempt counters before and after, and whether replay becomes permitted. Repeating
  the identical resolution is idempotent, a conflicting one is refused, and a crash between
  publication and the job update turns the recovery blocker into a specific "rerun the resolution"
  instruction rather than a silent replay.

- First-class authenticated `claude-code` translation provider (adapter 1.0.0). The pipeline could
  already reach a locally authenticated CLI through `codex-cli`, but reaching Claude required an
  Anthropic API key and therefore a second billing and credential path; this adapter instead drives
  the user's installed `claude` executable under their existing Claude Code login. It is an ordinary
  provider adapter: the queue, prompt renderer, QA engine, and worker gained no Claude-specific logic.
  One job is one non-interactive `claude --print --output-format json` process in a private temporary
  directory with `--tools ""`, `--disable-slash-commands`, `--strict-mcp-config`, `--safe-mode`, and
  `--no-session-persistence`, so translation is pure text in and text out and Claude cannot read or
  modify the repository, run shell tools, or browse. The rendered system prompt is staged as an
  owner-only `--system-prompt-file` rather than an argv value and the user message is passed on
  standard input; only the JSON `result` field becomes `translation.md`, with ANSI stripping, empty
  output rejection, and separate timeout, nonzero exit, malformed structured output, output-limit,
  and authentication classifications. Every `*_API_KEY` variable plus Anthropic base-URL, Bedrock,
  Vertex, and Foundry routing variables are stripped from the child environment, so the call cannot
  silently become an Anthropic API call. `--timeout-seconds` is the process timeout and
  `--max-output-tokens` is genuinely enforced through `CLAUDE_CODE_MAX_OUTPUT_TOKENS`; there is no
  reasoning-effort support. Attempts record CLI version, authentication method, first-party inference
  provider, and returned plus canonical model, so the attempt is distinguishable from an Anthropic
  API, OpenAI, or Codex CLI attempt.

- Provider-neutral preflight (`preflight_provider` 1.0.0) and `check-translation-provider`. A CLI
  session provider can be uninstalled or logged out, and discovering that after a claim would burn an
  attempt and leave an ambiguous attempt behind. The worker, `execute-translation-job`, and
  `repair-translation-job` now preflight the selected provider before recovery, claiming, or any
  other queue mutation; dry runs skip it and stay fully local. Providers without a local precondition
  report `supported: false` and pass, so the dispatch stays generic. For `claude-code` it verifies the
  executable, that the installed CLI still documents every non-interactive flag the adapter uses, and
  that `claude auth status --json` reports an active login — no book content and no model call.

- Book Metadata Resolution Engine 1.0.0 and its canonical `workspace/metadata.yaml` record. Book
  identity was previously only the scaffolding in `config/book.yaml`, so an authoritative snapshot
  could freeze a real corpus under a placeholder book. The new provider-free stage reads embedded PDF
  document information plus short front and back matter lines — never prose — and applies generic
  bibliographic rules to resolve the English title, author, series name and volume, edition,
  publisher, publication year, and a checksum-validated ISBN. Local source evidence outranks project
  configuration for bibliographic facts, and a placeholder configured value is treated as absent;
  `book_id` instead prefers an explicit non-placeholder configured identifier and otherwise derives a
  stable filesystem-safe `slug(title_en)-<source>-<target>`. Every field records provenance
  (`pdf_metadata`, `front_matter`, `configuration`, `external_research`, `unresolved`) with a short
  evidence locator, and an unavailable fact is left unresolved rather than guessed — including the
  Persian title, which stays a human decision. Adds `resolve-book-metadata`, a dry run unless
  `--apply` is supplied; an unchanged resolution is a skip that leaves the record byte-identical so
  snapshot identity never drifts on a re-run.

- Governed book identity migration 1.0.0. Resolving real book metadata left the book-scoped
  registries still declaring the scaffolding `book_id`, and hand-editing them would have moved
  identity with no lineage. `migrate-book-identity` (dry run unless `--apply`, `skip` once they
  agree) rewrites only the addressed `book_id` scalar lines in `config/terminology.yaml`,
  `config/terminology-questions.yaml`, and `config/source-anomalies.yaml`, preserving indentation,
  quoting, folded blocks, and every other byte, and refuses unless the parsed payload with identity
  fields removed hashes identically before and after. A book-scoped *derived* identifier moves with
  it: `anomaly_identity` hashes the book ID, so a stored `anomaly_id` left behind would contradict
  its own derivation rule. Each migration publishes immutable lineage at
  `workspace/migrations/book-identity/bim-<24 hex>.yaml` with both identities, per-registry
  before/after raw SHA-256, the invariant semantic SHA-256, occurrence and changed-line counts, and
  every re-derived identifier. Execution refuses registry drift between planning and publication, and
  frozen historical resources and snapshots keep the earlier identity as evidence.

- `freeze-translation-resources` publishes the durable content-addressed copies for the current
  resource generation without creating a snapshot, so an operator can re-freeze after a registry
  change while snapshot creation stays a separate authorized step. It is a dry run unless `--apply`,
  reuses byte-identical existing copies, and never overwrites an earlier generation.

- Non-blocking Provisional Terminology Decisions and a durable Open Question Registry. Decision
  ownership (`decision_source`), settledness (`decision_state`: `confirmed` or `provisional`), and
  `locked` are now independent; a provisional decision is a working translation choice rather than a
  factual claim, is never lockable, and is enforced exactly like a confirmed one so a book cannot
  drift term by term while its meaning stays open. Decisions persisted before this change load as
  `confirmed`. Adds `config/terminology-questions.yaml`, a deterministically identified question set
  keyed by `(candidate, category)` so rewording a question or revising its working target never mints
  a duplicate, carrying category, status, blocking flag, evidence status, related chapters and chunks,
  a three-channel research ladder (current book, web, other volumes), evidence provenance, and the
  eventual resolution. Snapshots freeze the question registry's exact bytes alongside terminology and
  source anomalies, so resolving a question later produces new lineage instead of rewriting a frozen
  snapshot. Prompt packages mark provisional state, name the unresolved question, and instruct the
  provider to use the working form consistently rather than alternate or invent evidence; terminology
  QA records that enforcement came from a provisional decision without failing on the open question
  alone. Adds read-only impact analysis reporting the chunks, snapshots, and translations a future
  resolution would touch, and a readiness report separating confirmed from provisional decisions and
  blocking from non-blocking questions. Network access is never required for readiness.

- Explicit terminology rejection. `CandidateStatus.REJECTED` previously had no writer, so "not
  terminology" was indistinguishable from an untouched backlog entry. Rejection is now a recorded
  state with a required reason, which is what lets escalation accounting derive closure from the
  registry.

- A durable terminology decision correction record preserving the superseded target, ownership,
  state, and lock verbatim when a stored decision's provenance is itself wrong. Ordinary edits still
  refuse to lower provenance or overwrite a lock; correction is a separately named operation.

- Targeted selection for `run-translation-worker` through `--job-id` and `--chapter`. Piloting one
  deliberately chosen job previously meant `claim-translation-job` plus `execute-translation-job`,
  which puts a claim token in the operator's shell history; the worker could only take whatever the
  queue offered next. Both filters reuse the queue's own claim eligibility rule, deterministic
  priority ordering, lock, and lease rather than a second selection path, so the worker keeps
  managing the claim token internally. `--job-id` processes exactly that job and never falls back:
  a target that is missing from the run, or neither an unassessed provider-successful draft nor
  claimable, processes zero jobs and reports the structural reason. `--chapter` restricts selection
  to one chapter in canonical queue order. The options are mutually exclusive because a job ID
  already identifies exactly one job, and both honour `--dry-run`, which selects without claiming,
  writing, or contacting a provider.

### Changed

- The queue now separates the attempt-slot sequence from the provider-attempt budget. `attempt_count`
  advances on every claim — that is what gives each execution its own immutable attempt directory —
  so it counted a reservation that never reached a provider as a spent attempt. Jobs gained the
  optional, backward-compatible `abandoned_attempt_count` (default `0`, absent on historical jobs),
  and claim eligibility, retry, repair, permanent-failure derivation, and the executor's claim check
  now compare `max_attempts` against the derived `provider_attempt_count`. Only an explicit
  `provider_not_contacted` resolution ever abandons a slot. `list-translation-jobs` shows provider
  attempts rather than issued slots.

- Prompt-package planning no longer treats a job's `attempts/` directory as downstream execution
  evidence when it holds only pre-send reservation markers, and never treats `resolutions/` as
  evidence, so a job whose only attempt slot was explicitly abandoned can still validate the package
  it needs to execute. A published attempt directory blocks package work exactly as before.

- The canonical job lifecycle admits `running -> pending`, reachable only through an explicit
  `provider_not_contacted` resolution. Automatic recovery still restores `running -> claimed` for a
  `prepared` reservation and never moves a `requesting` one.

- Translation Snapshot Engine 1.1.0 durably freezes every mutable translation input, not only the
  registries. Prompt, glossary, and style-guide references previously carried `frozen_file: null`, so
  a "frozen" historical snapshot could be silently reinterpreted by editing a config file. Each input
  — prompt, glossary, style guide, terminology, source anomalies, open questions, and the resolved
  book metadata record — now receives a byte-exact content-addressed copy under
  `workspace/resources/<category>/<sha256><suffix>`, published idempotently before the snapshot
  directory. Creation is refused when any referenced input lacks a durable frozen artifact, and
  validation verifies the frozen bytes and hash rather than the current config file, so a later config
  edit makes a snapshot non-current for new work without making it unresolvable. Resolved book
  metadata now participates in manifest identity through `author`, `series_name`, `series_index`, and
  the metadata record's path, version, hash, and frozen copy, so two semantically different books can
  never share lineage merely because their chunk bytes match. A full-selection snapshot is
  authoritative and refuses placeholder book identity outright. Identity is versioned rather than
  retrofitted: a new `frozen-resources-v1` manifest representation applies only to manifests whose
  prompt reference is durably frozen, while legacy and frozen-terminology manifests keep their
  original hashing rules, IDs, bytes, and historical validity. `BookConfig.title_fa` now accepts an
  empty value, which means the Persian title has not been decided.

- Escalation accounting derives from current registry, question, and review lineage instead of a
  hardcoded per-term advisory table. `_escalation_disposition` previously asserted fixed dispositions
  for six named source terms and never consulted the registry, so resolved items were reported as
  unresolved forever and one entry (`Sar`) contradicted the evidence. Term kind and competing
  interpretations now derive from the recorded classification and classification conflicts. Immutable
  historical review artifacts are unchanged; the audit adds `unresolved_escalation_count` and
  `blocking_escalation_count` as the current-state readiness signal.

- Corrected `Aunt Resara -> خاله رسارا` from `user`/`confirmed`/`locked` to `ai`/`provisional`/
  unlocked through the correction lineage above. The user chose between two renderings; that is a
  translation choice, not an assertion that Resara is maternal kin, and the corpus does not establish
  the kinship side. `Peace`, `platform`, and `wards` are now explicitly rejected rather than merely
  undecided. Terminology registry data version 1.0.2 -> 1.1.0 because every persisted decision gained
  a field, and `config/terminology.metadata.yaml` was refreshed from a stale 1.0.1.

- Prompt package renderer 1.1.0 -> 1.2.0 and terminology QA 1.1.0 -> 1.2.0; both emit intentionally
  different bytes. Terminology QA validity is now error-based so a provisional warning cannot fail a
  translation.

- Reviewer provenance contract separating local orchestration from remote model inference, recording
  inference locality, provider, account type, external-service contact, disclosed and withheld
  source-material categories, and whether prior model knowledge was used. Adds an explicit superseding
  provenance-correction record so an immutable reconciliation run with factually wrong provenance is
  corrected without rewriting its artifact or re-running review, plus a committed schema and an
  operator command. Corrects the reconciliation run reviewed under Anthropic-hosted inference that had
  been recorded as a local reasoning agent.

- Targeted Semantic Terminology Reconciliation 1.0.0 that resolves the conflicts deterministic
  remediation must refuse. Adds a conflict graph with deterministic component identities over exact
  canonical, alias, canonical subterm, competing variant, and normalization-identity relations;
  bounded corpus evidence that never assembles prose passages; family-sharded review with immutable
  attempts, strict validation, failed-shard retry, and resume; a machine-readable resolution contract
  expanding into typed registry edits bounded to their component and forbidden on locked decisions;
  alias-cycle and cross-character alias rejection driven by source evidence rather than matching
  targets; a combined prospective plan merging deterministic and semantic operations; and
  correctness-based protected-lookup verification that reports both whether a lookup changed and
  whether it is authoritative. Also corrects the lookup-regression classifier to derive intent from
  the operations' actual edits.

- Terminology Registry Remediation Engine 1.0.0 that consumes the existing remediation audit and
  assigns exactly one primary action per affected decision. Deterministic actions are limited to
  outcomes provable from stored governance evidence: validator-false-positive no-ops, removal of a
  canonical target wrongly stored as another decision's competing variant, and the normalized
  candidate-identity migration. Equal-priority conflicts and any fix requiring a newly composed
  Persian target are recorded as unresolved with blocking evidence rather than guessed, and a plan
  containing an unresolved operation is never applicable. Adds content-addressed plans pinned to the
  registry semantic and raw hashes plus the audit identity, a dry run that applies the plan to an
  isolated registry, revalidates it, re-audits it, checks structural invariants, reports before/after
  canonical lookups, and proves the real registry byte-identical, an atomic drift-checked application
  path with immutable evidence, four operator commands, committed plan and simulation schemas, and a
  synthetic planning benchmark.

- Read-only terminology registry remediation audit with deterministic finding IDs, machine-readable
  remediation plans, filtered CLI inspection, and finding-level evidence lookup without registry
  mutation.

- Terminology Review Engine 2.0 with deterministic token/count-bounded risk-aware shards, immutable
  attempts and validated results, crash-safe resume, targeted retry/inspection CLI, and progress.

- Safe full-book terminology discovery over the audited current-ready corpus without creating a
  translation snapshot, with anomaly-span exclusion, bounded provider batches, aggregate corpus
  metrics, complete proposal/review coverage checks, and conservative review deferral.

- General Source Anomaly Registry for explicit source-edition defects with deterministic identity,
  evidence and source-hash validation, operator CLI, chapter 008 confirmed nonblocking regression,
  audit links, scoped prompt/QA context, and additive frozen snapshot lineage.

- Deterministic read-only `inspect-normalization-changes` operator preview using the exact mutable-
  regeneration candidate bytes, with semantic paragraph regions, responsible normalization rule,
  page/block provenance, affected chunk hashes, JSON output, and zero-mutation regression coverage.

- Provider-free full-book Source Quality Audit 1.0.0 with discovered inventory, conservative
  extraction/normalization/dialogue/heading/chunk detectors, exact conservation checks, stable
  finding and report identities, read-only CLI filtering/fail policy, optional persisted reports,
  synchronized schema, synthetic controls, and local benchmark.
- Source Quality Audit 1.1.0 downstream resolution and translation-readiness semantics, preserving
  historical finding IDs while distinguishing resolved, unresolved, and unevaluable structure;
  policy-derived blockers, readiness counters, blocking-only filtering, and concise finding evidence.

- Explicit `reuse-accepted-translations` dry-run/publish workflow for hash- and version-compatible,
  effectively accepted historical translations, with immutable new-run provenance, current QA,
  recovery-safe idempotency, and zero provider-attempt accounting.

- Explicit provider-free translation QA re-evaluation with complete frozen-lineage preflight,
  deterministic immutable evidence history, dry-run and prose-free inspection commands, effective
  acceptance resolution, crash-safe idempotent adoption, and derived run-counter reconciliation.

- Explicit provider-neutral translation repair for QA-rejected successful attempts, with frozen
  source/translation/findings/terminology/style inputs, monotonic immutable attempt lineage,
  versioned acceptance evidence, bounded retry semantics, dry-run, recovery safety, and structural
  operator inspection.

- Sequential provider-neutral translation worker with internal claims, persisted-attempt validation,
  frozen-lineage terminology and structural QA, durable hashed acceptance evidence, explicit accepted
  and QA-rejected counters, bounded provider retry/backoff policy, dry-run, recovery reconciliation,
  QA-rejected job inspection, fake-provider integration coverage, and synthetic benchmark.

- Immutable provider-neutral AI Canonical Terminology Review with accept/revise/reject/defer
  dispositions, explicit quality dimensions, style-resource lineage, canonical revalidation,
  deterministic fake-provider coverage, review-gated application, audit CLI, and benchmark.

- Unified read-only canonical terminology view for AI proposals, deterministic exact-term reuse,
  conservative canonical-subterm conflict detection, historical-run audit CLI, and classification-
  aware canonicalization confidence that cannot be bypassed by provider confidence.

- Durable provider-contacted terminology validation failures, conservative source-evidence
  normalization, independently verified aliases, read-only request inspection, and explicit
  new-identity retry lineage without automatic expensive-request replay.

- Generic two-stage AI terminology analysis with strict discovery/proposal response contracts,
  provider-neutral and deterministic fake/Codex CLI adapters, content-addressed immutable analysis
  runs, deterministic duplicate/alias/occurrence reconciliation, explicit classification conflicts,
  manual/high-confidence/automatic acceptance policies, incremental discovery, CLI orchestration,
  schemas, and synthetic benchmark coverage.

- Generic Terminology Consistency Engine foundation with deterministic candidates and canonical
  decisions, provenance precedence, locked user overrides, aliases and structural occurrences,
  legacy-glossary compatibility, snapshot lineage, relevance-filtered prompt injection, scriptable
  CLI operations, and read-only terminology and translation QA validators.

- Read-only selected-chapter artifact staleness inspection and explicit dry-run-by-default mutable
  regeneration with current-engine hash comparison, immutable downstream lineage reporting, complete
  staging, recoverable per-chapter replacement, and source/translation safety guards.
- Historical-versus-current snapshot validation: frozen manifests remain internally valid history
  after canonical or resource drift, while new runs, claims, prompt rendering, and provider execution
  continue to require current lineage.
- Generic deterministic Document Chunk Engine 1.0.0.
- Paragraph-conserving chunk plans with explicit scene-boundary preference and tail rebalancing.
- Deterministic preceding context, chunk lineage metadata, conservation reports, dry-run planning,
  force safety, transactional publication, recovery, status counts, and synthetic benchmark coverage.
- Content-addressed immutable Translation Snapshot Engine 1.0.0 with full/subset selection, resource
  versioning, frozen manifests, live-reference validation, atomic publication, CLI/status integration,
  synchronized schemas, and synthetic benchmark coverage.
- Provider-independent Translation Queue 1.0.0 with deterministic run/job identities, atomic
  publication, local claim locks and leases, retries, recovery, bounded event history, derived
  counters, CLI/status integration, synchronized schemas, and synthetic concurrency coverage.
- Deterministic Prompt Package Renderer 1.0.0 with inert template validation, provider-neutral
  system/user separation, byte-length framing, content-addressed identity, source/context
  conservation, private atomic publication, live validation, CLI/status integration, and benchmark
  coverage.
- Provider-independent Translation Provider Executor 1.0.0 with claim/lease enforcement, durable
  execution reservations, private immutable attempt artifacts, structural result validation,
  ambiguity blocking, deterministic fake provider, official OpenAI Responses API adapter, and
  synthetic benchmark coverage.
- First-class authenticated `codex-cli` provider using local shell-free execution, read-only sandboxing,
  official final-message capture, optional model override, credential-sanitized subprocesses, and
  deterministic subprocess failure classification.

### Fixed

- A published provider attempt no longer makes its own prompt package look stale, so an explicit
  retry is possible at all. Prompt package planning is used for two different questions — *may I
  write a package here?* and *is the stored package still the right input?* — and only the first is
  about downstream evidence. Because both went through one guard, the `attempts/` directory a job
  earns on its first provider call answered the second question with "stale", and the executor then
  refused every subsequent attempt with `prompt package is absent, stale, or invalid`. Planning now
  carries an explicit `PromptPackagePurpose`: rendering keeps the downstream-evidence refusal and the
  pending-or-claimed eligibility rule, while validation recomputes the package from live snapshot
  lineage, frozen resources, source, and context and compares it byte-for-byte with what is stored.
  Nothing about lineage validation is relaxed — genuine message, source, context, or frozen-resource
  drift still invalidates the package and still refuses the retry, a successful or ambiguous attempt
  still blocks ordinary re-translation, and an accepted translation is still unreachable from the
  queue. `list-prompt-packages` gains the same real comparison for jobs past `claimed`, which
  previously compared the stored package only against itself.

- Claude Code adapter 1.1.0 keeps safe evidence about a failing exit instead of discarding it. A real
  `claude-code` attempt failed with nothing recorded but `exited unsuccessfully (exit code 1)`, which
  was not enough to distinguish a transient provider condition from a broken invocation; the adapter
  had read the exit status and thrown away both the CLI's structured result object and its stderr.
  A failing exit is now classified from whatever the CLI actually reported — the final result object
  first (`subtype`, `api_error_status`, error text), trailing stderr lines second — so usage/option,
  configuration, authentication, model, rate-limit, and server failures are distinguished from a bare
  nonzero exit and carry the retryable flag the worker's automatic retry already reads. The retained
  diagnostic is capped at 240 characters and redacted first: emails, URLs, POSIX and Windows paths,
  bearer/labelled/prefixed secrets, session UUIDs, long token-shaped runs, and ANSI sequences are
  removed, and a candidate containing Arabic-script characters is dropped entirely because the
  adapter's own output contract is Persian prose. A successful `result` field is never treated as
  diagnostic text.

- Canonical-subterm validation now accepts bounded Persian `ان` plural morphology inside a
  larger compound, and application revalidates reviewed targets against the current rule instead of
  perpetuating stale review conflict flags. A read-only reconciliation ledger exposes exact
  proposal/review disposition coverage and normalized-candidate bookkeeping.

- Book-scale review preserves valid shard work after later failure and no longer rewrites invalid
  no-op revisions into deferrals; the provider contract now requires exact proposal IDs.

- Normalization Engine 1.5.0 now conservatively rejoins capitalized complements after incomplete
  articles, delayed dialogue punctuation tails, and lowercase narrative em-dash continuations across
  canonical pages while preserving interrupted-speaker boundaries. Source Quality Audit 1.2.0 with
  interrupted-token detector 1.1.0 no
  longer promotes those independent speaker turns to high-confidence continuation defects and keeps
  uncommon short interrupted tokens visible as extraction-owned evidence.

- Normalization Engine 1.4.0 now rejoins canonical-page continuations split within a capitalized
  possessive name and narrowly guarded one-line closing tails of already established unfinished
  dialogue, without weakening paragraph-count QA or merging the pilot's valid dialogue structures.

- Terminology QA now treats persisted aliases explicitly as source-only discovery forms: alias-only
  occurrences no longer require the full canonical target, while full canonical source terms remain
  enforced through deterministic longest-first, boundary-safe overlap resolution. Persian target
  morphology and competing-variant validation remain unchanged.

- Translation snapshot identity validation now hashes legacy manifests from their original
  field-present immutable representation, preserving historical IDs and manifest hashes after the
  additive frozen-terminology fields while retaining strict corruption checks for legacy and current
  formats.

- Persian terminology QA now recognizes only a documented set of exact grammatical suffix forms,
  including directly attached indefinite `ی` and enumerated ZWNJ suffixes, while retaining strict
  rejection of different spellings and semantic roots.
- Normalization Engine 1.3.0 rejoins quote-led cross-page continuations that contain a completed
  sentence before an incomplete final clause, while preserving conservative unmatched-dialogue
  protections.
- New translation snapshots durably freeze exact terminology bytes in immutable content-addressed
  workspace storage; historical QA and prompt rendering no longer depend on the mutable current
  registry. QA-rejected job inspection now shows structural finding codes and terminology
  code/source-term pairs without translation prose.

- Normalization Engine 1.2.0 now recognizes balanced quote-led dialogue-and-narration blocks as
  eligible page-boundary continuations when the left edge is syntactically incomplete and the next
  block begins lowercase, while preserving unclosed dialogue and new-speaker boundaries.

- Terminology competing variants can no longer be lexical components of their canonical target;
  explicit decisions reject them, AI proposals discard them, canonical review and read-only QA
  ignore historical violations, and the current registry removes three invalid name components.

- AI-generated terminology proposal and revised-review targets now pass through a versioned,
  language-specific target-style adapter before successful immutable publication; Persian optional
  short-vowel cleanup is deterministic, raw provider targets remain auditable, review revisions must
  differ in canonical lexical identity, and stored user/legacy canonical targets remain untouched.

- Normalization Engine 1.1.0 now conservatively rejoins multi-line prose blocks interrupted only by a
  canonical PDF page boundary while preserving structural, dialogue, scene, and genuine paragraph
  boundaries; prior normalized and downstream lineage becomes detectably stale and is regenerated
  forward into new artifacts.
- Exhausted released or recovered queue claims now derive to `blocked` instead of remaining
  permanently ineligible `pending` state.

## [0.5.0] - 2026-08-06

### Added

- Modular deterministic, local, non-AI chapter normalization.
- Canonical cleaned chapter artifacts, normalization reports, structural warnings, counters, and
  lineage hashes.
- Dry-run, selector, stale, skip, force-safety, and interruption-recovery behavior.
- Synthetic normalization benchmark and post-implementation audit.
- Permanent developer charter, coding, testing, review, benchmark, and release governance.
- Machine-checkable governance and application-version synchronization contracts.

### Fixed

- Consecutive eligible line-boundary hyphen repair.
- Integrity validation for persisted normalization metadata and report counters.

## [0.4.0] - Historical milestone; date not recorded

### Added

- Canonical TOC-driven immutable chapter extraction.
- Chapter metadata, source hashes, batch preflight, selector support, and resumable publication.

## [0.3.0] - Historical milestone; date not recorded

### Added

- Embedded PDF TOC discovery, deterministic classification, normalized chapter boundaries, and
  canonical TOC serialization.

## [0.2.0] - Historical milestone; date not recorded

### Added

- Text-based PDF inspection and inclusive 1-based raw page-range extraction.

## [0.1.0] - Historical milestone; date not recorded

### Added

- Initial Python project, CLI, configuration, progress models, and test/tooling foundation.
