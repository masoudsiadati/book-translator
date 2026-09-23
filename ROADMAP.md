# Roadmap

The roadmap communicates intended sequencing, not a commitment to scope or delivery dates. Every item
still requires design review, explicit task authorization, tests, compatibility analysis, and the
governance process in `docs/developer-charter.md`.

## Completed

- Deterministic RTL EPUB 3 Builder 1.0.0 over a verified release — canonical Markdown to XHTML,
  EPUB 3 package and navigation documents, right-to-left stylesheet and page progression,
  release-derived book identifier and modification time, fixed archive timestamps and ordering for
  byte-identical rebuilds, and read-back validation before anything is written, under `build-epub`
  (implemented under `Unreleased`)

- Deterministic Release Verification Engine 1.0.0 — a read-only publication gate over an assembled
  release covering manifest, book, chapters, translation coverage, titles, terminology, and
  provenance, with paragraph-tiling completeness proof, skipped-is-not-passed reporting, a
  reproducible content-addressed verdict, and `verify-release` (implemented under `Unreleased`)

- Governed Chapter Title Translation Engine 1.0.0 — chapter titles translated under frozen
  terminology into one immutable content-addressed artifact, with deduplicated identical titles,
  provider-free title QA that refuses before publication, digest-only provenance, and a release
  overlay that supplies Persian headings without touching any translation state
  (implemented under `Unreleased`)

- Deterministic Release Assembly Engine 1.0.0 — a completed translation run assembled into a
  canonical Markdown book with a content-addressed manifest, immutably copied frozen resources,
  counters-only translation report, and chunk-level lineage, under `export-translation-run`; no
  provider, no mutation, and reproducible content hashes (implemented under `Unreleased`)

- Provider-free Structural Repair Engine 1.0.0 and the final-QA finalization
  ladder — deterministic separator repair, provider repair, bounded provider recovery, and one
  governed exceptional repair-budget escalation — under a single `finalize-translation-run`
  (implemented under `Unreleased`)

- General Source Anomaly Registry with deterministic evidence, explicit readiness policy, Source
  Quality Audit links, frozen snapshot/prompt lineage, and scoped QA awareness

- Project and CLI foundation
- PDF inspection
- Raw page-range extraction
- Embedded TOC discovery and normalization
- Canonical immutable chapter extraction
- Deterministic modular chapter normalization
- Deterministic full-book Source Quality Audit 1.0.0 with provider-free structural detection,
  conservation validation, stable finding identities, filtering, and optional reproducible reports
- Source Quality Audit 1.1.0 resolution/readiness analysis with historical finding retention,
  downstream-lineage evidence, blocking policy, and targeted finding inspection
- Source Quality Audit 1.2.0 speaker-transition classification and independent interrupted-token
  truncation visibility
- Normalization Engine 1.5.0 conservative article-complement, dialogue-punctuation-tail, and
  narration-dash page-boundary repair
- Normalization Engine 1.6.0 lowercase-edge possessive-name page-boundary repair; Source Quality
  Audit 1.4.0 with joined-token detector 1.1.0 corpus-attested modal-join evidence
- Generic Document Chunk Engine 1.0.0 (implemented under `Unreleased`)
- Explicit mutable-artifact staleness inspection and safe staged regeneration
- Deterministic Translation Snapshot Engine 1.0.0 (implemented under `Unreleased`)
- Provider-free Book Metadata Resolution Engine 1.0.0 with evidence-ranked provenance, canonical
  `workspace/metadata.yaml`, and stable filesystem-safe book identity
- Governed book identity migration 1.0.0 with byte-minimal identity rewriting, proved semantic
  invariance, re-derived book-scoped identifiers, and immutable auditable lineage
- Translation Snapshot Engine 1.1.0 durable freezing of every mutable translation input, book
  metadata participation in snapshot identity, placeholder rejection for authoritative full-book
  snapshots, and explicitly versioned backward-compatible manifest identity
- Provider-independent Translation Queue 1.0.0 (implemented under `Unreleased`)
- Deterministic Prompt Package Renderer 1.0.0 (implemented under `Unreleased`)
- Provider-independent Translation Provider Executor 1.0.0 with OpenAI Responses, authenticated
  Codex CLI, and authenticated Claude Code CLI adapters, plus provider-neutral preflight before any
  queue mutation
  (implemented under `Unreleased`)
- Sequential provider-neutral Translation Worker 1.0.0 with durable QA acceptance, explicit
  provider/acceptance separation, retry policy, recovery reconciliation, and operator visibility
- Explicit operator-driven Execution Reservation Resolver 1.0.0 for incomplete `requesting`
  reservations, with immutable per-attempt resolution lineage, provider-attempt accounting separated
  from the attempt-slot sequence, and no generic force reset
- Explicit provider-neutral Translation Repair Workflow with immutable repair lineage and bounded
  reuse of the existing queue/attempt lifecycle
- Bounded automatic QA repair orchestration 1.0.0 over that mechanism, with an independently
  versioned pure classification policy, explicit opt-in worker options, durable per-attempt
  authorization records, and unchanged pending-queue ordering
- Bounded automatic provider-failure recovery 1.0.0, with an independently versioned pure failure
  family and verdict policy, mandatory fresh preflight for health-dependent failures, immutable
  historical attempt classification, a separate recovery budget, durable per-attempt authorization
  records, and unchanged pending-queue ordering
- Bounded automatic recovery of a repair a provider failure ended, within that same recovery
  boundary: the recovered attempt stays `execution_kind: repair` with its original repair root,
  the derived repair request is re-derived and proven byte-identical before it may be re-sent, the
  displaced QA-rejected draft is restored through a governed opt-in transition rather than
  re-queued as a translation, and the replay spends no second QA repair decision
- Explicit provider-free QA re-evaluation with immutable reconciliation history, frozen lineage,
  effective acceptance resolution, and derived counter repair
- Explicit cross-run accepted translation reuse with strict material-input compatibility, immutable
  carry-forward provenance, and current-run QA
- Explicit governed cross-run re-evaluation of historically QA-rejected provider outputs, with a
  separate tiered compatibility policy, governed terminology-lineage proof, retained historical QA
  findings, and no provider call
- Generic Terminology Consistency Engine storage, decision, snapshot, prompt, CLI, and read-only
  validation foundation
- Provider-neutral structured AI Terminology Discovery and Canonical Decision Proposal pipeline with
  immutable runs, deterministic reconciliation, policy-controlled acceptance, fake and Codex CLI
  adapters, and incremental discovery support
- Unified registry/legacy-glossary canonical proposal view with exact reuse, subterm constraints,
  conflict-safe application, and risk-aware canonicalization eligibility
- Provider-neutral immutable AI canonical terminology review with structured quality dimensions,
  style guidance, review-gated application, and independent provider/model lineage
- Resumable full-book terminology review with deterministic risk-aware shards, immutable attempts,
  targeted retry, strict validation, and complete aggregate gating
- Numbered paragraph output contract with fail-closed marker parsing at the provider boundary,
  per-run contract freezing, and marker-preserving repair derivation
- Deterministic provider-free paragraph alignment QA with a versioned language-pair threshold
  policy, adjacent boundary-drift detection, repair-safety gating, and a read-only legacy audit
- Provider-backed Semantic Coverage Judge with an independent judge role, strict fail-closed
  structured output, a versioned materiality policy, immutable non-mutating evidence, and a
  measured pilot evaluation
- Production semantic gate with full paragraph coverage, a separate acceptance dimension,
  one-shot wider-context second looks, bounded window-scoped semantic revision with measured diff
  bounds and independent re-judging, a deterministic displacement classifier, and whitespace-only
  re-segmentation with a uniqueness proof
- Chapter-Level Persian Literary Editor 1.0.0 with whole-chapter context, deterministic overlapping
  section fallback, versioned generic style and book profile, structured edit/diff governance,
  independent changed-paragraph semantic re-judging, bounded literary correction with byte-exact
  faithful fallback, read-only continuity findings, role-separated cost provenance, and blind A/B
  comparison artifacts

## Planned

- **0.6.0 — Document Chunk Engine release:** representative real-chapter pilots, release review, and
  manual version/tag preparation for the implemented generic engine.
- **0.7.0 — Provider execution release:** authorized one-job provider pilot, representative execution
  validation, and manual release review for the implemented executor.
- **0.8.0 — Translation engine:** authorized terminology discovery pilots, prompt versioning, glossary
  compatibility, context packaging, and translation lineage.
- **0.9.0 — Validation and review engine:** mechanical validation, review-required states, correction
  workflow, and explicit approval boundaries.
- **1.0.0 — RTL EPUB 3 release:** the builder itself is implemented and consumes only a verified
  release. What remains for the release is external conformance validation (EPUBCheck), reading-system
  trials on real devices, and manual version/tag preparation.

### Translation quality, after the structural contract

Structural validity is the floor, not the goal. The numbered paragraph contract and alignment QA
establish that each translated paragraph answers to the right source paragraph; nothing yet judges
whether it says the right thing or reads well. The next step is a **semantic coverage judge**: a
bounded, evidence-producing check that the meaning of each source paragraph is actually present in
its own target paragraph, which is the one question alignment measurement deliberately refuses to
answer from counts. The judge and bounded revision loop now establish the fidelity floor. The
Chapter-Level Persian Literary Editor now exists as a separate derivative layer over that floor,
including lightweight chapter-local voice guidance and read-only continuity review. Human blind
comparison is the next quality decision; permanent cross-chapter story memory and character memory
remain later and separate.

## Future possibilities

These ideas are noncommitted possibilities and are not part of the planned release contract:

- Additional providers and models
- Multiple source and target languages
- Additional terminology discovery providers and book-scale candidate ranking
- Character and location indexes
- Style-consistency analysis
- Audiobook preparation
- A graphical or web user interface
- An MCP interface
- Additional output formats
