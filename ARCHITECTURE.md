# Architecture contract

This document freezes the boundaries of the future `book-translator` pipeline. The detailed
contracts are [the data model](docs/data-model.md), [pipeline](docs/pipeline.md),
[recovery rules](docs/recovery.md), and [naming conventions](docs/naming-conventions.md).

## Principles

- The configured PDF is an initial input, not a downstream data store. Once a validated
  extraction snapshot exists, every later stage reads workspace Markdown and metadata only.
- `workspace/` is the canonical representation. The legacy top-level `extracted/`, `chunks/`,
  `translated/`, and `review/` directories may remain during migration but receive no new
  contract.
- One repository may hold several books. The legacy root `workspace/` with `config/book.yaml`
  is one book workspace (the first book); every further book gets an isolated workspace
  `workspace/books/<book-id>/` with `config/books/<book-id>.yaml`, carrying the identical
  layout (`toc.yaml`, `chapters/`, `metadata.yaml`, `audits/`, `resources/`, `snapshots/`,
  `translation-runs/`, …). `ProjectPaths(project_root, book)` is the single derivation point;
  the source-processing commands select a workspace with the shared `--book <book-id>` option
  and, without it, resolve the legacy root byte-for-byte as before. Two books therefore never
  share a mutable artifact path even when their logical unit IDs coincide (each may have a
  `000-prologue`), and one book's stale/conflict detection is unchanged inside its own root.
  A frozen snapshot records the workspace it came from through its project-relative paths, so
  its lineage is validated against that workspace rather than against whatever root a caller
  happened to select. Translation runs are stored project-wide and carry that lineage in their
  `snapshot_path`; every run-side reader of book configuration (provider execution, the worker,
  repair, finalization, the semantic and literary boundaries) resolves the run's book from it,
  fails closed if the run and its frozen manifest name different workspaces, and never reads a
  fixed configuration file. The narrative corpus of a book is its prologue, numbered chapters, and
  epilogue (`000-prologue`, `001`…`999`, `999-epilogue`); front matter is opt-in and
  appendices/glossaries/unknown trailing entries are never extracted.
- Persisted records use Pydantic contracts in `book_translator.models`; committed JSON Schemas
  are generated from those models. Schema version `1.0` is the initial contract.
- Artifact lineage is established by byte-exact SHA-256 hashes. A mismatch makes a derived
  artifact stale; it must be refused or explicitly regenerated.
- State changes use `status_transitions.py`. Translation, validation, review, and approval are
  distinct states. Approval is never inferred.
- Every generated write is UTF-8 where textual, atomic, resumable, and idempotent. Book content
  is never logged and is sent only to the configured translation provider.

## Component boundaries

Book Metadata Resolution is a separate provider-free boundary that establishes book identity before
any lineage depends on it. It reads embedded PDF document information plus bounded short front and
back matter lines, applies generic bibliographic rules, and never reads or retains prose. Local
source evidence outranks project configuration for bibliographic facts; the project identifier
prefers an explicit non-placeholder configured value and otherwise derives a filesystem-safe slug
from the resolved title. Every field records its provenance — PDF metadata, front matter,
configuration, or explicit external research — and an absent fact is recorded as unresolved rather
than guessed. The canonical record is `workspace/metadata.yaml`; it is idempotent, so an unchanged
resolution never rewrites its bytes and never disturbs snapshot identity.

Canonical book identity propagates to book-scoped registries only through an explicit governed
migration, never through hand editing. The migration moves the `book_id` header of each *current*
registry plus any derived identifier defined as a function of it — a source anomaly ID is such a
hash — and proves it changed nothing else: exactly the addressed scalar lines are rewritten in place,
and the parsed payload with identity fields removed must hash identically before and after. An
immutable record under `workspace/migrations/book-identity/` carries the old and new identity, each
registry's before/after raw hashes, the invariant semantic hash, and every re-derived identifier.
Frozen historical resources and snapshots retain the earlier identity as evidence and are never
migrated: current authoritative state and immutable historical state are distinct.

Inspection and discovery may read the configured PDF or EPUB. PDF extraction and provider-free EPUB
import both write the same immutable chapter `source.md` contract. EPUB import resolves the package
through `META-INF/container.xml`, treats the OPF spine as authoritative reading order, uses NCX
labels when available, merges consecutive split resources into one logical narrative unit, and
excludes non-narrative spine matter before publication. Downstream normalization and chunking do not
branch on source format.
The provider-neutral Source Quality Audit is a read-only boundary over immutable extraction,
normalization, and chunk artifacts. It verifies lineage and conservation, reports conservative
structural suspicions with stable content-derived identities, and never repairs or advances state.
Current downstream lineage resolves each historical suspicion independently: exact normalization
change provenance or chunk conservation may make a finding nonblocking without erasing it.
Normalization derives `cleaned.md`, including conservative marker-aware repair of prose paragraphs
split only by a PDF page boundary, including guarded capitalized-name and one-line dialogue-tail
extraction shapes. It also attaches delayed dialogue punctuation tails, lowercase narrative dash
clauses, and capitalized complements after an incomplete article while preserving interrupted
speaker turns. Generic document chunking derives deterministic chunks from a
hash-verified canonical `cleaned.md`. The Translation Snapshot Engine freezes validated chunk and
resource lineage before any external or translation stage. It hashes the configured PDF only to verify
canonical provenance; it never reads or copies PDF text. Chunking and later text processing consume
only workspace Markdown and metadata. Models and transition
policy contain no file I/O; path derivation contains no directory creation; orchestration owns
I/O, atomic replacement, hash checks, and state persistence.

The freeze specifies boundaries rather than implementation details. Chapter extraction and
deterministic normalization, generic document chunking, and immutable translation snapshots now
implement those boundaries, as do deterministic release assembly, governed chapter-title
translation, read-only release verification, and the RTL EPUB 3 builder; translation review and
approval remain outside scope.

The EPUB Builder is a publication format converter and the last boundary in the chain. Its only
input is a release directory that verification has just judged `READY_FOR_PUBLICATION`; it holds no
import of a translation run, job, attempt, provider artifact, QA record, or snapshot, and no
provider import at all. It makes no editorial decision — chapters, their order, their headings, and
their paragraphs arrive already settled — and the only transformation it applies to text is the XML
escaping the format requires. Determinism is structural rather than incidental: the book identifier
is derived from the release's content address, the modification timestamp is the release's own,
archive entry timestamps and attributes are fixed, and entry order is sorted, so an EPUB is a pure
function of the release it publishes. The archive is rendered in memory, read back, and validated
against the release before anything is written, so a build that fails its own checks produces no
file.

Release Verification is the publication gate, and it is a judge rather than a producer. It reads one
assembled release directory and decides whether the release is complete, internally consistent,
immutable since publication, and traceable to the run and snapshot it names — from the bytes on
disk, never from the assembly's own claims. It is the narrowest boundary in the project: read-only
everywhere, no provider import and no path to one, no mutation of any release, run, snapshot,
registry, or artifact, and no clock dependency beyond the report's own timestamp, which is excluded
from the report's content address so two runs over an unchanged release are comparable byte for
byte. It fails closed in both directions: a discrepancy fails the check, and evidence that cannot be
reached is *skipped* and reported as skipped rather than counted as proof. An EPUB builder consumes
a verified release.

Chapter Title Translation is a separate metadata boundary beside the translation chain, not a step
inside it. A chapter title never passed through a chunk, so no accepted translation contains one;
the stage translates titles under the terminology the snapshot already froze and publishes its own
immutable artifact. It opens a translation run read-only, purely to learn which snapshot that run
froze, and reads, writes, or judges nothing the run owns — no job, attempt, acceptance, QA verdict,
counter, or status. What reaches a provider is one English title plus the canonical decisions that
title actually contains, never chapter prose or context. Identical titles are asked once and the
answer is reused, so one English title cannot receive two Persian ones; the artifact contract
enforces that independently of the provider. Identity addresses the inputs, which is what lets a
consumer look for an artifact before knowing the titles; the produced titles carry their own
content address. Pure provider-free title QA — coverage, emptiness, script, hash integrity, source
drift, self-consistency, and canonical terminology through the installed validator — runs before
publication and again at every consumption, and refuses rather than warns.

Release Assembly is the boundary between a finished translation run and a distributable book, and
it is strictly a consumer. It reads one completed run, the frozen snapshot that run answers, and
the accepted translations it produced, and it writes only into its own release directory. It never
mutates a run, snapshot, frozen resource, or registry, never contacts a provider, and never
re-judges a translation: acceptance is upstream state, and assembly is not another QA pass. Its
output is a canonical Markdown book plus a content-addressed manifest, an immutable copy of the
frozen book metadata and terminology, a counters-only translation report, and chunk-level lineage
carrying hashes and identities but no prompt or response material. Because every byte is a pure
function of frozen inputs, re-exporting the same run reproduces the same content address; only the
manifest timestamp differs. A later format builder — EPUB and beyond — consumes the release rather
than walking the run, which keeps format concerns out of translation state entirely.

Translation snapshots extend rather than replace `ExtractionSnapshot`. A translation snapshot is a
content-addressed manifest of project-relative references and exact hashes; it does not duplicate book
text. Its identity is the SHA-256 of stable canonical JSON for the semantic manifest, excluding labels
and timestamps. Published frozen snapshots are immutable and future translation execution must accept
only a frozen snapshot that still validates all referenced artifacts.

Snapshot Engine 1.1.0 freezes *every* mutable translation input, not only the registries: the
translation prompt, glossary, style guide, terminology registry, Source Anomaly Registry, Open
Question Registry, and the resolved book metadata record each receive a durable byte-exact
content-addressed copy under `workspace/resources/<category>/<sha256><suffix>`. A new snapshot is
refused when any referenced mutable input lacks a durable frozen artifact, and historical validation
verifies the frozen bytes rather than trusting the current config file. Resolved book metadata
participates in manifest identity, so changing placeholder identity to real identity necessarily
produces a new snapshot ID even when every chunk byte is unchanged, and an authoritative full-book
snapshot refuses placeholder book identity outright. Identity representation is versioned rather
than retrofitted: legacy and frozen-terminology manifests keep their original hashing rules and stay
byte-identical and valid, while manifests that durably freeze their resources use the newer
representation.

The Source Anomaly Registry is an explicit, versioned record of source-edition facts, separate from
extraction correction and normalization. Stable IDs bind location and exact original evidence, never
mutable operator notes. Audit may link exact findings but never creates entries. Local anomalies may
be nonblocking when conservation and structure remain safe; structural loss remains blocking. New
translation snapshots freeze registry bytes, and prompts and QA consume only scoped anomalies.

The provider-independent Translation Queue creates structural job references from a currently valid
frozen translation snapshot. It owns local claim leases, attempt accounting, retries, derived
counters, and crash recovery, but never reads book prose or changes chapter/chunk translation,
review, or approval state. Future provider execution must require a valid claim token and remains a
separate boundary.

The deterministic Prompt Package Renderer is a separate local boundary between queueing and provider
execution. It reads a valid eligible queue job and its frozen snapshot references, then publishes
validated system/user messages without claiming or executing the job. Length-framed data sections
preserve exact source, context, and style bytes; a content-addressed package manifest records all
material resource and message hashes. Prompt packages are sensitive derived inputs, ignored by Git,
and are not translation results.

The Translation Provider Executor is the only implemented boundary permitted to send prompt-package
content to a translation provider, including through an authenticated local provider executable. Its
provider-neutral protocol has no queue or filesystem dependency; orchestration separately enforces
frozen lineage, claims, leases, reservations, attempt publication, and lifecycle transitions. A
successful provider attempt is a draft result only and never implies review or approval. The Codex
CLI adapter runs in a temporary working directory with a read-only sandbox and captures only the
official final-message artifact.

The Execution Reservation Resolver is a small operator-only boundary beside the queue. Automatic
recovery can restore a `prepared` reservation because the workspace proves no send happened, but a
`requesting` reservation makes delivery unknowable and is refused forever. The resolver is where a
person supplies that missing external fact under an explicit resolution enum, and it never contacts a
provider, never edits or deletes the reservation, and never fabricates an attempt. It publishes an
immutable `resolutions/NNN/resolution.yaml` record and then applies only the accounting that
assertion justifies. Because claims — not provider calls — advance `attempt_count`, the queue
distinguishes the attempt-slot sequence from `provider_attempt_count`: an abandoned slot that
provably never reached a provider is not a spent attempt, and only the provider count is compared
against `max_attempts`.

The Translation Worker composes, without collapsing, the queue, provider executor, persisted-attempt
validator, terminology validator, and structural QA validator. Provider `succeeded` remains a draft
execution outcome. A separate private, hash-linked acceptance sidecar records `accepted` or
`qa_rejected`; neither outcome implies review or approval. The worker is sequential by default;
`--workers N` runs the same per-job path in N spawned execution slots under one coordinator that
does the once-per-pass validation, preflight, and recovery, owns the global `max_jobs` budget and
the stop decision, and renders every slot's structured result. Each slot claims through the queue's
own atomic claim, so no two slots can own one job. The worker keeps claim tokens inside
orchestration and reuses the queue's lock, lease, reservation, retry, and recovery mechanisms in
both modes. Optional job or chapter targeting narrows which jobs the worker may select
through the queue's own eligibility rule and priority ordering rather than a second selection path;
an explicitly targeted job is never substituted by another one.

The Numbered Paragraph Contract and Paragraph Alignment QA are two boundaries answering one defect,
and they are separate because one enforces and the other detects. Blank lines carry no identity: a
translation that fuses two source paragraphs and splits a third has exactly as many blocks as its
source, so a count comparison passes while every paragraph after the first defect answers to a
different source paragraph than the reader is looking at. The contract removes the inference. Each
source paragraph is sent under an explicit fixed-width marker, every marker must return exactly
once and in order carrying that one paragraph, and a violation is refused whole at the provider
boundary before any translation artifact can exist — never repaired by guessing which paragraph the
provider meant, because a wrong guess moves prose between identities silently. The accepted
translation stays pure prose: markers are stripped after conformance is proven, and the framed
source's conservation is proven by stripping them back to the frozen bytes. Which contract governs
is frozen on the run when the run is created, never read from live configuration, so an existing
run's packages still render byte-identically, its attempts still mean what they meant, and its
accepted translations stay eligible for cross-run reuse.

Alignment QA is the detector for what identity alone cannot see, because a provider can honour
every marker and still put the wrong sentence under one. It runs after identity is established and
before acceptance, measures only counts — words, characters, sentences, numerals, quotation marks —
and never reads a glossary or judges wording; terminology defects and boundary defects have
different remedies and a report that merged them would leave the repair policy unable to tell them
apart. Thresholds live in a named, versioned, language-pair-scoped policy rather than in the
engine, because the band this project ships was measured over one completed pilot rather than
derived. Severity follows the strength of the claim: a lone out-of-band ratio is advisory, while
two adjacent paragraphs whose deficit and excess cancel back into the band is content conservation
across a boundary, which is the signature of migrated prose, and that finding blocks. No alignment
finding is ever automatically repairable, because every automatic remedy either rewrites prose or
moves separators and both would act on a guess about which paragraph the content belongs to. A
separate read-only audit measures a marker-less historical run positionally under the same policy,
which is the only way to know whether the policy reproduces what a human audit found; it contacts
no provider and changes no acceptance, status, counter, or translated byte.

The Translation Repair Workflow explicitly reclaims a provider-succeeded, QA-rejected job within
the same queue and monotonic attempt sequence. A repair attempt is immutable, records
`execution_kind: repair` and `repaired_from_attempt`, and receives the exact frozen source, rejected
translation, versioned acceptance findings, relevant frozen canonical terminology, and frozen
prompt/style resources. The prior attempt and acceptance evidence remain immutable. Repair remains
bounded by the job's original `max_attempts`, and the explicit operator command stays the way a
person drives it by hand.

The Automatic QA Repair Orchestrator sits above that mechanism so a book can finish without a
person answering every mechanical rejection. It separates three concerns that must not absorb one
another: the repair mechanism, an independently versioned pure policy, and worker orchestration.
The policy classifies each QA finding as `auto_repairable`, `reevaluation_candidate`,
`non_repairable`, or `operator_required` and authorizes a repair only when *every* blocking
finding is auto-repairable; an unrecognized finding code is `operator_required`, so a QA engine
that grows a new blocking code inherits no permission to spend provider attempts on it. Budget is
the smaller of an explicit repair allowance and the job's remaining provider attempts, so the
policy can only ever be more conservative than the queue's own accounting, and each cycle claims
a new attempt slot before contacting a provider, which makes the loop strictly decreasing.
Provider transport retry and QA repair stay distinct: the first answers a failed delivery of the
same request, the second answers a delivered result that QA rejected. Before each authorized
repair the orchestrator publishes an immutable `auto-repair/NNN/decision.yaml` record naming the
policy version, the classified findings, and the budget the attempt was drawn against; outcome
evidence is deliberately not duplicated there, because the attempt and acceptance sidecar for the
same attempt number already hold it. The only route to `accepted` remains the ordinary acceptance
sidecar published by QA over a real repair attempt, so an attempted repair never launders a
rejection into an acceptance, and a repaired job is still one accepted job that took two provider
attempts rather than two jobs.

The Provider Recovery Orchestrator answers the other half of unattended operation: a provider
failure that ended an attempt and left a job stranded. Its governing principle is that historical
attempt classification is immutable. An attempt marked `retryable: false` was genuinely not
retryable when it was made, and that record is never rewritten; what a later event can change is
whether a *new* attempt is justified, because provider health is a property of the provider rather
than of the historical attempt. Five concerns stay separate: failure classification (the
executor's, on the attempt), provider health (the existing preflight), a pure separately versioned
recovery policy, orchestration, and worker selection. The policy maps every executor error
category to a provider-neutral family and answers `recoverable_now`, `wait_for_backoff`,
`preflight_required`, `operator_required`, `unsafe_to_replay`, or `exhausted`. Only families that
assert something about provider *availability* are recoverable; a malformed response, a contract
violation, a content refusal, a local fault, and any unclassified failure fail closed however
healthy the provider now looks, and an ambiguous execution is never replayed because that is
exactly what the reservation-resolution boundary exists for. Authentication and
provider-unavailability are recoverable only against a fresh preflight taken after the failure,
which the pure policy cannot obtain and therefore refuses to assume. Budget is the smaller of an
explicit recovery allowance and the job's remaining provider attempts, counted from the published
decisions themselves. Recovery performs no provider execution of its own: it publishes an
immutable `provider-recovery/NNN/decision.yaml` before touching the queue, then re-enters the job
through the ordinary `failed -> retry_pending -> pending` lifecycle so the recovered attempt runs
down the same governed path, and into the same QA acceptance and automatic repair, as every other
attempt. Provider transport retry, queue retry, QA repair, provider recovery, and operator retry
therefore remain five distinct mechanisms answering five distinct questions.

One recovery boundary serves both kinds of request a job can fail on, because the question is the
same: a provider failed to carry out a request, and re-sending it is a bounded scheduling
decision. What the job itself carries decides the kind, never an argument — a failure with no
assessed draft is a translation, a failure that left a standing QA rejection intact is a repair.
Recovering a repair keeps `execution_kind: repair` and keeps `repaired_from_attempt` pointing at
the QA-rejected *translation* the repair answers, never at the failed repair, which produced no
translation and is therefore nothing to repair from. Because a repair request is derived rather
than frozen — built from the rejected draft, the findings that rejected it, and the frozen
resources — recovery carries one extra conjunct the translation case does not need: the request is
re-derived and proven byte-identical to the one that failed, through a hash chain covering the
rejection evidence, the rejected attempt, the frozen source, context, prompt, style, and
terminology resources, and both recorded provider-facing messages. A request that cannot be
reproduced is `operator_required`, because sending a *different* repair under the authorization
for the original is the failure mode this boundary exists to prevent. A recovered repair does not
re-enter through `pending`: a pending job is claimable as an ordinary translation, and this one
carries a successful attempt that ordinary execution refuses outright, so the authorization
instead restores the QA-rejected draft that the failed repair displaced — a governed, keyword-only
`failed -> succeeded` transition that puts back a status the failed attempt had no business
changing, leaving the failed attempt immutable on disk and still counted against the provider
budget. The three budgets stay separate accordingly: a failed repair call consumed one provider
attempt and one repair *execution*, and its recovery consumes another of each plus one recovery
attempt, but no second QA repair *decision* — QA chose that repair action once and the provider
merely failed to deliver it. The repair-decision count is therefore derived as repair-kind
attempts minus the slots a repair recovery authorized. A recovered repair that succeeds but is
still rejected is a different matter, because QA has now judged new output, so it continues into
the ordinary auto-repair boundary, which decides for itself whether the repair budget permits
another decision.

The QA Re-evaluation Workflow is a separate provider-free reconciliation boundary. An explicit
operator command revalidates one immutable provider-successful attempt and its historical acceptance
against the current versioned local QA engines while retaining the run's exact frozen source,
terminology, prompt, and style lineage. It publishes new immutable evidence under a monotonic
re-evaluation sequence, never replaces acceptance or repair history, and updates only the job's
effective acceptance pointer plus derived run counters. Mechanical acceptance remains distinct from
human review and approval.

The Accepted Translation Reuse Workflow is an explicit provider-free cross-run boundary. Compatibility
is decided by effective-request identity rather than whole-resource equality: exact source and context
hashes, language pair, output contract, and both rendered provider-facing message digests, whose bytes
are re-verified on both sides. Whole-resource drift in terminology, prompt, glossary, style, or
renderer version is recorded and waived under that proof, because those resources reach a provider
only through the rendered messages; a changed rendered message is always blocking and is attributed
to the framed section that changed. It consumes only effectively accepted, fully validated historical
attempts, publishes a new-run immutable reuse artifact with byte-identical translation and complete
source lineage, and runs current QA against the new run's frozen resources. Reuse never increments
provider attempts or impersonates a provider call.

The Historical Rejected-Output Re-evaluation Workflow is a third provider-free boundary, distinct
from both of the above. It carries one historically QA-rejected but provider-successful output — or,
since 1.1.0, an accepted one whose rendered request moved, which reuse must refuse — into a newer run
and assesses it under that run's frozen QA with the same acceptance battery a fresh attempt faces. Its compatibility policy is separate on
purpose: accepted-translation reuse may demand an identical rendered request, but a rejected output
is worth revisiting precisely because terminology QA moved after it was produced. The request
contract — source, context, languages, system message, and output contract, including the historical
attempt's own recorded contract — is never waivable, and neither is a change to a contract-bearing
framed section. A changed resource projection is eligible only when it is attributable and governed:
every non-terminology resource byte identical, the terminology resource the one that moved, that move
a contiguous chain of published governed registry edits, and no locked or user-owned canonical
decision reaching the job changed inside it. The destination job must still be pending and
unattempted; the historical run, its attempts and acceptance evidence, and every frozen resource stay
byte identical. Publication writes carry-forward evidence that *retains* the historical rejection and
its findings alongside the current QA outcome, gives the job acceptance origin
`historical_reevaluation`, and never creates a provider attempt or increments provider counters. Its
acceptance sidecar lives inside the carry-forward record, never in a provider attempt's acceptance
slot. For a series-policy book the governed move is a contiguous chain of published series-policy
amendments, and the locked-drift rule becomes a term-level judgement: a change reaching the job is
waived only if it cannot have altered what the historical request asked for — same target and force,
authorized forms only widened, no added alias in the job, and every withdrawn alias the job contains
still owned by a decision that authorizes the target the historical request named for it. A withdrawn
instruction nothing still authorizes blocks, because whether the provider obeyed it is invisible to
every mechanical check.

Series policy amendments are the governed writer of a frozen series policy: an all-or-nothing batch of
complete term decisions that never creates a term and never overrides an explicit user decision,
published as an immutable content-addressed record before the policy file is replaced. Cross-Snapshot
Run Migration composes the two carry boundaries rather than adding a third way to accept: it
classifies every successor job from immutable evidence (identical-request reuse, governed
re-assessment, carried-forward rejection awaiting an explicit repair, or pending for fresh
translation), re-plans each publication under the successor's lock, and contacts no provider. A
carried rejected draft is repairable only by the explicit operator repair, whose attempt names the
carry-forward rather than an attempt; automatic repair, structural repair, and automatic provider
recovery refuse it.

The Semantic Coverage Judge is a provider-backed fidelity boundary and the first stage that judges
*meaning* rather than structure. It exists because everything upstream of it can prove only that
the right paragraph is in the right place: the numbered paragraph contract establishes identity and
alignment QA measures proportion, but neither can tell whether a sentence inside a correctly
identified paragraph reached the reader. The judge answers exactly one question — is all materially
relevant source meaning represented in this Persian paragraph, without unsupported additions or
distortions — and is forbidden the second question a translator would ask, because "is this a good
translation" mixes fidelity with style and produces findings a repair stage cannot act on.

Judging is separated from translating on purpose, and the separation is enforced by what the judge
receives rather than by convention. One request carries one paragraph identity, its source, its
candidate, a bounded neighbour on each side for pronoun and speaker resolution, and only the
canonical decisions that paragraph's own source triggers. It never receives the translator's prompt
or style guide, the whole registry, another chapter, an earlier verdict, or any repair rationale,
so a judgement is an independent reading rather than a review of the pipeline's own reasoning. The
prompt spends as much space on what is *not* a defect as on what is: Persian merges and splits
sentences, reorders clauses, drops pronouns its morphology carries, and prefers an idiom to a
literal rendering, and a judge that has not been calibrated against those flags every one of them.

The provider contract is strict structured output validated against the exact paragraph identities
the request named. An answer naming an identity nobody asked about, omitting one, or answering one
twice is a provider failure rather than a low-confidence verdict, for the same reason a broken
marker is: attaching a verdict to the wrong paragraph is the defect this chain exists to prevent.
What the judge reports and what the pipeline may act on are then separated once more by an
independently versioned policy, because a judge that notices a lost shade of emphasis is not wrong
and a pipeline that treated every such report as a defect would spend a person's attention on prose
that is fine. `INCONCLUSIVE` is never converted: it is not a pass, not a failure, and not an
automatic escalation, and it is persisted as itself so a later stage decides with evidence rather
than inheriting a decision made here.

The judge changes nothing. It publishes immutable evidence outside every run directory, mutates no
acceptance, status, counter, run record, translated byte, or release, and never rewrites prose. A
run that predates it carries no semantic evidence, and that absence means *not judged* rather than
*passed*. Evidence is hash-linked to the exact source and translated bytes it read, records the
provider, adapter, and returned model where the transport reports one, and retains message digests
rather than messages.

The Production Semantic Gate composes the judge into the translation lifecycle as a fourth
acceptance dimension beside structural, terminology, and alignment acceptance, and it is recorded
as its own state rather than folded into mechanical acceptance. A job can be mechanically accepted
and semantically unjudged, and every reader treats the absence of a semantic state as *not
judged*, never as *passed*. A run created under the gate records the gate policy at creation and
is releasable only when every job's semantic state is `pass`; a run created before the gate
records no policy and is held to exactly the requirement it always was. Coverage is total by
policy: deterministic signals prioritize, route, and choose repair strategy, but no paragraph is
excused from judgement because it looks clean, since a paragraph with a normal length, a normal
count, and no structural warning is precisely where a semantic defect hides.

The gate is a bounded state machine over paragraphs. Each is judged once; an `INCONCLUSIVE`
paragraph is granted exactly one wider-context second look under a versioned context policy, and
if still inconclusive it is recorded as *unresolved*; a `REVISE` paragraph is corrected inside an
explicit window at most twice and re-judged independently after every correction, and when that
budget is spent it too is *unresolved*. Unresolved is a terminal state that is never accepted and
never quietly handed to a person. Every provider call — judge, second look, reviser — records
provider, adapter, requested and returned model, token counters, latency, and the prompt, schema,
and policy versions it ran under, and the returned model is never invented.

Bounded Semantic Revision is a second provider role over the same audited transport, narrower than
the translator's by construction. It receives one explicit window of marked paragraphs, the
findings that rejected them, the canonical terms they trigger, and bounded neighbours, and it is
told to correct the named defects and preserve everything else byte for byte. What comes back is
spliced into the standing translation so that paragraphs outside the window are copied from the
standing bytes rather than from the answer, then measured: changed paragraph indexes, character and
word deltas, outside-window digests, marker identity, and terminology blocking codes before and
after. A change outside the window, a lost or reordered marker, a terminology regression, or a
structural or alignment failure refuses the candidate before it is adopted, and the refusal is
recorded with the same measurements. An adopted revision supersedes the effective translation the
way a structural repair does, naming what it superseded, and the reviser's own claim of success is
never evidence: only the independent re-judge is.

Displacement is classified deterministically from judge findings before any text is touched. A
contiguous run of paragraphs each carrying both an omission and an addition is content that moved
across boundaries, not content that was lost, and the gate first tries to put the separators back
without a provider: the window's Persian is re-split at sentence units to mirror the source's
proportions, accepted only when that split is uniquely best by a margin, lands every block inside
the alignment band, and preserves the concatenated non-whitespace content exactly. When the split
is not determined the gate refuses to guess and a bounded revision answers instead; when it is, the
result is still re-judged, because a unique proportional split is evidence of where the boundaries
went and not proof of what the paragraphs mean.

The generic Terminology Consistency Engine separates discovered candidates from canonical target
decisions. The current registry is a versioned, deterministic resource; decisions carry explicit
provenance and only user decisions may be locked. Context-sensitive ordinary language is retained as
candidate evidence but excluded from global prompt enforcement. New translation snapshots freeze the
registry hash and exact bytes in immutable content-addressed workspace storage when present, while
older manifests retain their original identity and historical validity. Prompt rendering selects only relevant decisions and marks required and user-locked
terminology. Post-translation terminology and mechanical QA validators are pure, read-only reporters.

AI terminology analysis is a separate provider-neutral boundary with two immutable stages. Discovery
reads hash-verified chunk sources from a frozen translation snapshot and emits structured candidate
evidence; proposal reads reconciled candidates plus a hash-pinned current registry and emits
structured, unlocked AI proposals. Content-addressed analysis runs persist source lineage, sanitized
request identity, provider/model, typed output hashes, and validation reports under
`workspace/terminology-runs/`. Neither stage changes canonical terminology. A third explicit policy
operation may merge candidates and accept proposals into the current registry, which makes existing
translation snapshots historical and requires a new snapshot for future translation work.
Discovery evidence uses deterministic lexical boundaries plus only Unicode compatibility,
typographic apostrophe/dash, and whitespace normalization. It never uses fuzzy matching, case
folding, or inflection. A response that fails this contract is published as an immutable failed,
provider-contacted run with its structured output hash; its requesting marker remains until explicit
operator recovery, and any retry receives a new content-addressed identity linked by `retry_of`.
Proposal planning uses a unified read-only canonical view over locked/user/other registry decisions
and legacy glossary compatibility entries. Exact candidates are recorded as deterministic reuse and
are not sent to AI. Boundary-safe canonical subterms constrain, but do not construct, larger target
phrases. Provider confidence is separate from deterministic global-canonicalization eligibility.
Canonicalization safety and target quality are separate gates. An immutable provider-neutral review
run consumes one proposal run, selected occurrence evidence, relevant canonical constraints, and a
hashed target-style resource. It records accept/revise/reject/defer decisions without changing the
registry. Application may require explicit review lineage and revalidates canonical constraints.
Book-scale review uses one deterministic parent plan with token/count-bounded, risk-classed shards.
Raw attempts and strictly validated shard results are immutable; completed shards are reused after a
crash or explicit retry. Only an exact, duplicate-free aggregate is eligible for application.
Historical unsharded pilot runs remain valid.
AI proposal and revised-review targets pass through a versioned target-language style adapter before
immutable successful publication. The exact provider target remains attached as audit evidence when
normalization changes it. Canonical lexical comparison uses that same narrow adapter; it performs no
fuzzy, stemming, morphological, or semantic equivalence. Existing registry decisions, legacy glossary
entries, and especially locked user decisions remain authoritative bytes and never pass through this
AI-output normalization boundary.

The Terminology Registry Remediation Engine is a separate provider-free boundary that consumes the
read-only remediation audit and never re-derives its own findings. That audit reports two families
over one registry read. The canonical-validator family asks whether two canonical records
contradict each other; the exact-enforcement family, produced by the read-only exactness audit,
asks whether a source form can bear a fixed target lexeme at all — a question no validator conflict
can raise, which is why a bare common noun or a finite clause promoted to terminology was
previously invisible to remediation. Risk-to-remedy classification lives in exactly one pure module
so neither the audit nor the planner classifies risk itself, and it resolves each flagged decision
as definitely invalid, definitely valid, requiring context review, or unresolved. Where the two
families would act on the same decision, the exact-enforcement family stands down and the report
records that it did. Every audited finding receives exactly one primary action, and an action is
planned as deterministic only when its outcome is provable from stored governance evidence.

Withdrawing a canonical decision is the most destructive change the registry allows, so there is no
command that performs it. A retirement exists only as a record inside an immutable plan: it carries
the SHA-256 of the exact decision and candidate records the plan was built against, refuses to
apply when either drifted, rejects the candidate in the same atomic publication when the source
form is not terminology at all, and is forbidden by the contract itself from naming a locked or
user-owned decision — the planner re-proves that ownership rule rather than trusting its input.
Relaxing enforcement never edits target text, and a defect that would need a *composed* target
becomes a durable question request rather than a mutation, which keeps application bounded to the
registry file. The one target rewrite the engine may perform is the opposite of composition: when a
compound already renders a decided component in the right place and order but spells it a way the
registry does not recognize, and exactly one token of that target is unaccounted for by any other
component, the component's own canonical target is substituted for that single token and every
other token is copied verbatim. An elliptical shorthand is never such a component, for the same
reason the canonical validator excludes it from its constraints. A remediation operation carries
the consequences of its own fix: correcting a spelling can be what first makes a compound honor its
component and therefore first makes its exact claim over-broad, so the correction carries that
relaxation rather than leaving a defect the plan itself created. Because withdrawing a decision can
likewise uncover a contradiction the withdrawn component was masking, a dry run proves safety by
requiring that the same plan already referred every surviving blocking finding to a human, not by
requiring none to survive.

Immutable lineage records — an identity migration, a frozen resource, a snapshot — pin the registry
hash that was true when they were written, and later governed edits legitimately move the live
registry away from it. Auditing that drift by comparing the two hashes proves only that time
passed. What must hold instead is that every step away from the recorded state is itself recorded,
so the live registry identity is the head of the governed transition chain that continues from the
record. That chain has more than one writer: a decision correction restates one canonical decision
and an applied remediation plan retires, retargets, or relaxes many, and both publish the semantic
identity they moved the registry from and to, so both are the same kind of evidence and are merged
into one chronologically ordered chain. A published plan directory is application evidence rather
than a proposal, because the executor writes it only after the applied registry hashed to the
identity the plan projected. A gap inside the chain is reported rather than treated as a failure,
because another governed writer — the identity migration itself — may legitimately sit between two
events. Precedence remains locked user decision, then existing
authoritative canonical decision, then legacy glossary, then new AI review; a newer AI target never
silently displaces an authoritative older target. Because the audit selects the authoritative side of
an equal-priority pair by stable ordering rather than by governance authority, equal-priority
conflicts are recorded as unresolved rather than guessed. Composing a corrected target is a
translation act and is always deferred to review. A plan is content-addressed over the registry
semantic identity, the raw registry bytes, and the audit identity, so it changes whenever registry
state or audited findings change. A plan carrying any unresolved operation is never applicable. Dry
run applies the plan to an isolated registry value, revalidates the contract, re-runs the audit and
the structural invariants, compares canonical lookups before and after, and proves the real registry
file is byte-identical afterwards. Application is a separate explicit operator action that reverifies
the source hashes, refuses on drift, publishes immutable evidence, and replaces the registry
atomically.

The Targeted Semantic Terminology Reconciliation workflow resolves what deterministic remediation
must refuse. It first builds a conflict graph over the unresolved operations, joining records by exact
canonical conflict, alias relationship, canonical subterm, competing variant, and normalization
identity, so mutually related decisions are always resolved as one family rather than as independent
records. Bounded corpus evidence per family carries occurrence and chapter counts, capitalization,
short single-line contexts, and co-occurring entities; full prose passages are never assembled.
Review is sharded by family, never splitting one, with immutable attempts, strict per-shard
validation, retry of only failed shards, and resume from validated shards. Alias identity must come
from source-language evidence: matching target translations never establish it, and impossible
cross-character aliases, geographic conflation, reciprocal cycles, and titles or disguises treated as
name aliases are rejected. Every resolution is machine-readable and expands into typed registry edits
that stay inside their component and never touch a locked user decision. The resulting operations
merge with the deterministic operations into one prospective plan that is simulated in isolation.
Protected terminology is judged on authoritative correctness rather than mere stability: a protected
lookup may move, but only towards its governed target, and the simulation reports both whether a
lookup changed and whether it is correct.

Reviewer provenance separates local orchestration from the service that actually performed model
inference. Rendering packets and reading response files locally is orchestration and never by itself
makes a review local; a reviewer whose reasoning runs on a hosted model records remote inference, its
inference provider and account type, and an external service contact, together with which categories
of source-derived material entered model context and which were withheld. Because a run is immutable,
factually wrong provenance is corrected by an explicit superseding record that carries a hash of the
semantic decisions and asserts they are unchanged; the original run artifact is never rewritten and
review is never re-executed merely to fix metadata.

The Final QA Resolution boundary is what a *finished* run needs, and it is a diagnosis boundary
before it is a remedy one. A run that ends `completed_with_failures` holds two unrelated things
under one status: translations a provider got wrong, and QA verdicts that have since stopped being
true. Deciding between them is the whole stage, so it reads the current validators against the
frozen resources in memory and never treats the historical evidence as the answer. Five concerns
stay separate: analysis (pure classification of the evidence), a pure separately versioned
resolution policy, the audit workflow, the bounded orchestration loop, and the CLI. Re-evaluation,
repair, and provider recovery stay the existing boundaries, reached through their existing entry
points and provenance; the stage may act on only four of its eight verdicts, and everything else is
reported.

The one remedy the boundary adds is the one no provider should ever be asked for. A
`paragraph_count_mismatch` is two unrelated defects under one code: a translation whose prose is
right and whose blank lines are in the wrong places, and a translation whose content genuinely
diverges. The first is decidable from the frozen source alone, so paying a provider attempt for it
is paying for an answer the workspace already holds. Structural Repair 1.0.0 decides between them at
the level of lines rather than blocks, which is what makes the question answerable: equal line
counts mean nothing is missing and nothing is extra, so the source's own blank-line placement can
simply be re-applied, and unequal line counts are aligned only when the alignment is bounded, cheap,
and uniquely best by a margin. Where the translation fused two source lines into one, the boundary
refuses on principle — deciding where to break translated prose is a judgement about meaning — and
the provider answers instead.

The repair is whitespace-only by construction and verifies that on its own output, publishes derived
bytes beside the attempt they descend from, never touches the attempt or its acceptance sidecar, and
re-runs both validators over the result. It is adopted only if block correspondence is restored and
no blocking code appears that the standing rejection did not already carry, so it can only ever
remove findings. That last property is what makes it safe to run first: terminology QA is
occurrence-anchored and fails closed without a block correspondence, so a terminology finding that
existed only because the alignment was broken clears once the blocks line up — and one that does not
clear stays exactly as blocking as it was.

`competing_variant` is why the boundary exists. One finding code covers at least eight different
situations, and the auto-repair policy is right to refuse the code on sight, because the code alone
cannot tell a contested canonical from a plural of the canonical. Terminology QA 1.6.0 removed the
largest source of those situations at the root: the finding now requires positive evidence that the
variant renders the *same source occurrence* the decision governs, anchored to the block
correspondence the pipeline already contracts on, instead of asking whether the string occurs
anywhere in the chunk. Co-occurrence is not evidence, and an alias, title, gloss, or unrelated
source expression is no longer mistaken for a rival rendering. The analyzer settles the question
from governed evidence only: whether the variant carries the canonical's own lexeme, whether the
effective registry still records the rivalry, whether another active decision claims the variant as
its target and how that decision is owned, and whether the canonical appears in the translation at
all. A locked or user-owned canonical never gives way — the decision stands and the translation is
what may be repaired. Where nothing governed explains the occurrence, the stage fails closed and
names the terminology decision a person has to answer, grouped by decision rather than by chunk,
because a handful of decisions is what nineteen rejected chunks usually are.

The two `resolve_terminology_*` verdicts are deliberately reported rather than executed. A run's
terminology resource is frozen into its identity, so settling a registry question for that run
means re-freezing terminology, taking a new snapshot, and carrying accepted work forward — not
editing a frozen run. Where a caller has proven a specific finding's meaning, it says so through
the repair policy's per-finding override channel rather than by weakening the policy for everyone,
and the override lands in the persisted repair authorization where it can be read back.

An exceptional repair-budget escalation is the one place the stage widens something. It is
explicit (an operator flag), bounded (one grant per job), versioned, and persisted before the
ceiling moves; it resets no historical counter, and the queue enforces it structurally — a job
whose attempt ceiling exceeds its run's without a matching persisted escalation is a stale queue,
not a job with extra budget.

The Mutable Artifact Regeneration workflow owns the explicit exception to ordinary stage-local
downstream guards. It may replace only chapter `cleaned.md`, normalization reports, chunk directories,
chunking reports, and the matching chapter metadata after a complete read-only preflight. It never
changes `source.md` or any snapshot, run, job history, prompt package, attempt, response, translation,
review, or final artifact. A frozen snapshot has two independent properties: immutable internal
integrity and current canonical lineage. Later canonical generations may make it historical for new
work without making its frozen manifest corrupt.

Embedded TOC discovery preserves every source entry in source order. Classification is
deterministic: numbered `Chapter` labels, number-prefixed titles, `Prologue`, and colon-prefixed
`Epilogue` labels are parsed structurally; non-structural entries before the first of those are front
matter; conventional `Appendix`/`Glossary` labels are appendices; everything else remains unknown.
Embedded hierarchy levels are preserved but do not override structural title classification. Equal
start pages share the boundary before the next later distinct start page.

The Series Terminology layer keeps three kinds of data apart that the first book's registry had
conflated: historical corpus evidence, retrieval, and translation policy. The professional Persian
translations of the first two series volumes exist as external, frozen, read-only bilingual corpora
with their own retrieval tool; they are observations about what a translator did, OCR noise and
inconsistency included, and are never copied, merged, or edited to match a project preference. The
governed decision layer is a versioned `SeriesTerminologyPolicy` in which every concept is `LOCKED`
(mandatory; explicit user or project provenance), `STRONG_REFERENCE` (strongly preferred; promoted
by a named cross-source-agreement rule that requires independent clean sources including narrative
prose and no material rival, never raw frequency), or `OBSERVED_VARIANTS` (evidence only, never
silently canonicalized). Every term keeps its observed variants with per-book, per-source, per-quality
support, so a status is explainable from the record, and an explicit decision that disagrees with the
corpus records the conflict beside itself while staying authoritative. A book's own occurrence layer
is a *derived* `TerminologyRegistry` projected from the frozen policy over that book's canonical
chunks: candidates carry only that book's chapter and chunk identities, locked terms project to exact
enforcement, strong references to contextual enforcement, and observed variants to undecided
candidates, so the worker, repair, semantic, title, release, and final-QA boundaries read the
contract they always read. Retrieval runs once per chunk as a governed, resumable batch against
corpora whose bytes and retriever script are pinned by hash, publishes one immutable content-addressed
reference package per chunk carrying clean evidence with provenance only, and is what prompt
rendering reads; the prompt states the four tiers in a separate length-framed section whose headings
say which force applies, and the observed tier is limited to terms the passage itself contains,
never restates a spelling a decision already claims, and withholds compound forms that spell a locked
component another way. A book-scoped workspace never probes another book's root registries: it
names a series policy and reference-corpus configuration or has no terminology, and a snapshot fails
closed until the derived registry and every reference package are proven current. Snapshot identity
then pins the policy, the corpus configuration with every external corpus identity, the reference
context index with every package hash, the derived registry, and the context rendering contract;
manifests without a series policy hash exactly as before.

### One concept, one canonical rendering

A concept is keyed on its exact source term, because every matcher downstream is byte-exact and a
dropped spelling is a dropped occurrence. That keying cannot see one thing on its own: an explicit
project decision on `the Dark One` and a corpus promotion of `Dark One` are two concepts with two
targets for one thing in the story. Nothing reconciled them, so every prompt path rendered both
instructions while every enforcement path honoured only the longest matching span — a provider told
to write the weaker rendering was then rejected for having written it.

`terminology/concept_precedence.py` is the one place that decides which of two such concepts
governs, and it is pure. Two concepts are the same concept only when their source terms are equal
after folding case and removing one leading English article, so the rule reaches `the Dark One` and
`The One Power` and deliberately leaves every merely nested pair alone: `Dragon` and `false
Dragon`, `Two Rivers` and `Two Rivers folk`, `angreal` and `ter'angreal` are different concepts that
share a substring, and containment is never sufficient on its own. Among variants of one concept the
order is explicit-user `LOCKED`, explicit-project `LOCKED`, other `LOCKED`, `STRONG_REFERENCE`, then
weaker evidence; ties break on the longer source term and then the concept id, so a resolution never
depends on input order. Two spellings that already agree on a target are not a conflict.

The rule is enforced at three points rather than patched at one. Import refuses to promote a corpus
form that is an article or case variant of a locked term, the mirror of the existing guard that
refuses a promoted compound spelling a locked component another way; the refused form keeps its
evidence. Derivation collapses each group onto the governing decision, so the displaced spelling
becomes an alias that is still matched and now enforced to the one canonical target, and the
displaced corpus evidence moves onto the governing concept, where a clean narrative rival of a
locked target becomes an enforceable competing variant and never an authorized target form.
Selection is shared: `applicable_decisions` uses the same `source_term_matches` as the QA validator,
so a provider is never asked to honour a term the validator will not look for.

A conflict that already exists in a policy is retired rather than deleted. A governed amendment
withdraws the displaced decision to `OBSERVED_VARIANTS`, keeping its concept, its spelling, its
conflicts and every observed variant, and writing the decision it used to state into its notes;
`policy_concept_conflicts` reports the class so it is found by validation rather than by a rejected
repair. The repair derivation is deliberately untouched throughout: bounded provider recovery
re-derives a failed repair and proves it byte-identical, so the repair path is corrected by the
registry stating one decision per concept, never by changing how a repair reads it.


## Persian literary editing boundary

The chapter-level literary editor is a derivative stage after semantic acceptance, not another
translation or acceptance state. It reads a marked, mechanically accepted, semantically `pass`
translation and publishes an immutable `workspace/literary-runs/lr-<24-hex>/` layer. It never edits
the translation run, release, or EPUB it descends from. Fidelity and literary quality stay separate:
the editor has no fidelity-verdict field, the continuity reviewer has no replacement-text field, and
release assembly does not implicitly consume literary output.

The default request unit is the complete chapter under chapter-global `[[Pnnnn]]` identities. An
oversized chapter is deterministically tiled into uniquely owned core sections with context-only
overlap; overlaps cannot race during merge. Moving prose between identities is never inferred: the
editor must request a governed multi-paragraph edit, and Phase 4 retains the faithful bytes instead.
The generic Persian style contract and optional book profile are separate, versioned, and frozen into
run identity. This keeps book-specific voice and negative controls out of the engine.

The provider's `changed` field is redundant metadata, not authority: the engine derives change from
byte equality with the faithful paragraph and records when it normalizes a contradictory flag. This
cannot bypass the fidelity firewall because every actual byte change is still selected for fresh
semantic judgement.

Every attempted result goes through marker, structural, alignment, and terminology QA. Diff policy
classifies normal, broad, and suspicious edits without rejecting a broad edit merely for breadth;
suspicious paragraphs receive individual semantic judgement with wider context. Every final changed
paragraph requires new Semantic Coverage Judge evidence over its exact candidate hash. An unchanged
paragraph may reuse earlier PASS evidence only because its SHA-256 is byte-identical. The literary
editor can correct a named fidelity regression at most twice, after which the paragraph is restored
from the faithful pre-editor bytes and recorded as `literary_edit_fallback`. Thus the optimization
layer can improve prose but has no path that can lower the proven fidelity floor.

After assembly, the continuity reviewer reports only bounded structured findings about Persian prose,
narrator register, character voice, dialogue, references, translationese, transitions, terminology,
and stylistic discontinuity. Human quality measurement remains external. The stage prepares a blind
Version A/Version B document and a separately hashed answer key; it never turns a reader preference
into canonical terminology or approval.

Recovery may explicitly name a compatible earlier literary run. Only already-atomic chapters whose
style/profile bytes, source and faithful hashes, engine/policies, model/provider, final output, and
semantic evidence all verify are carried into a new content-addressed run. Carried calls and the
chapter record name the source run; unfinished chapters alone contact the provider.
