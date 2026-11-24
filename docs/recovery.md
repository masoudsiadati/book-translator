# Recovery, resumability, and idempotency

An in-progress terminology parent review lives at
`workspace/terminology-runs/.<review-run-id>.review/`. Its deterministic `plan.json`, immutable
attempt responses, and strictly valid shard results support crash-safe resume. Valid shards are never
contacted again, attempts are capped at three per shard, and no aggregate is published until coverage
is exact.

Every future stage follows the same protocol:

1. Validate supported schema versions, prerequisites, statuses, snapshot ID, and recorded input
   hashes before work.
2. If output exists, has the expected metadata/hash, and is complete, skip it by default.
3. If metadata or hashes disagree, report stale state and refuse unless an explicit permitted
   regeneration mode is selected.
4. Write output to a temporary file in the destination directory, flush as appropriate, then
   atomically replace the destination. Persist status only after the artifact succeeds.
5. Record errors and attempt counts without deleting any earlier successful artifact.

An interrupted `running`/`translating` status is not completion. On restart, validate any files,
record the interrupted attempt, and retry or flag it. Temporary files are never inputs; safely
remove known stage-owned temporary files or ignore them until cleanup. Retries increment the
relevant translation or review attempt counter.

`--force` may replace reproducible derived outputs after lineage checks. It must not silently
replace chapter `source.md` (which needs explicit forced extraction) or any approved translation,
`final.md`, or snapshot. Approved artifacts require a separate, explicit invalidation action
that records why and preserves recoverability. Failed and review-required states preserve their
previous artifacts. Future pipeline commands should offer `--dry-run` showing skips, writes,
stale inputs, and proposed transitions without mutation.

Hashing is byte-exact and streamed through `sha256_file`; no Unicode, newline, Markdown, or other
normalization occurs during hashing. Consumers refuse or prominently warn on input-hash mismatch
according to stage safety; approval, final assembly, and publishing must refuse.

Chapter extraction preflights the complete selected batch before any chapter directory is
created. A valid `source.md`/`metadata.yaml` pair is skipped; a lone artifact, identity/range
conflict, or hash mismatch blocks the batch. Forced extraction is refused whenever downstream
artifacts exist. For publication, `source.md` is written atomically, its persisted bytes are
hashed, and only then is matching metadata written atomically. If metadata publication fails,
new source output and metadata for that failed chapter are removed; earlier completed chapters
remain valid and a rerun skips them. During a forced replacement, old metadata is removed before
source publication so an interruption cannot leave metadata claiming a mismatched source; rerun
with the same explicit `--force` is then safe.

EPUB import follows the same recovery rule. Planning re-parses the container, OPF spine, optional
NCX, and HTML resources, then compares the exact rendered source and metadata lineage. An exact pair
is skipped, a partial or conflicting pair is refused, and `--force` is refused once cleaned, chunk,
translation, review, or final artifacts exist. A run interrupted between units is resumed by
replanning; completed units skip and missing units publish. The canonical TOC must match the current
archive hash, spine positions, identities, and titles before a non-forced rerun proceeds.

Chapter normalization preflights every selected source, metadata record, source hash, existing
normalized set, version, and downstream conflict before batch writes. A complete hash-current set is
skipped. A version mismatch is stale and requires `--force`; a lone cleaned file or report, a
normalized status without both files, or a hash disagreement is an explicit inconsistency. Forced
replacement is refused when chunks, translations, review files, or `final.md` exist.

For each chapter, normalization generates content and its prose-free report in memory, writes and
verifies `cleaned.md`, writes `normalization-report.yaml`, then writes metadata last; all writes are
atomic. If report or metadata publication fails, evidence remains and old metadata does not claim
success. A normal rerun identifies the partial set; after diagnosis, safe `--force` replacement can
recover it when no downstream work exists. Earlier completed chapters remain valid and are skipped.
Dry runs normalize and hash in memory without creating directories, files, or timestamps.
Normalization rule/version changes make every older materialized normalization generation stale even
when cleaned bytes are unchanged. Inspect with `inspect-artifact-staleness`; do not rewrite immutable
snapshots or translation history. Regeneration remains an explicit operator action and follows the
existing mutable-artifact safety preflight.

Before authorizing a byte-changing normalization replacement, run
`book-translator inspect-normalization-changes --chapter SELECTOR`. This is read-only and shows only
the semantic changed regions from the exact regeneration candidate, including page/block provenance
and affected chunk source/context hashes. A zero-region result with different stored/current engine
versions is version-only staleness; apply may refresh normalization metadata/report but preserves
identical cleaned and chunk bytes.

Document chunking preflights every selected cleaned hash, status path, configuration, plan,
reconstruction, existing artifact set, and downstream blocker before a batch publishes. A complete
matching chunks/report/chapter-metadata set is skipped. Configuration, engine, or legitimate cleaned
lineage changes are stale and require `--force`; partial directories and integrity failures block.
Force is refused for translation content, non-pending chunk states, review evidence, or `final.md`.

For each chapter, all chunk sources, contexts, and metadata are built in memory and written to a
sibling staging directory. Staged hashes and metadata are validated before the directory is atomically
renamed to `chunks/`; the report follows, and chapter metadata is atomic and last. A first-publication
failure therefore leaves the old normalized/validated metadata and an explicit partial artifact set
that a safe forced retry may replace. Previously completed chapters remain valid.

Forced replacement first stages and validates the complete new set. Fixed stage-owned backup paths
temporarily hold old metadata, report, and chunks so old `chunked` metadata cannot claim a half-swapped
set. A publication failure restores the prior valid set and retains failed replacement evidence under
stage-owned hidden paths; a subsequent explicit safe `--force` clears that evidence and retries. Any
unresolved backup path blocks automated work for manual diagnosis. Neither mode changes `cleaned.md`
or extraction artifacts.

Cross-stage mutable regeneration is deliberately separate from normalization/chunking `--force`.
It requires repeatable explicit chapter selectors and plans without writes by default; only `--apply`
authorizes replacement. Complete cleaned/report/chunks/report/metadata candidates are built and
validated in a sibling `.regeneration-staging.*` directory. Fixed `.regeneration-backup-*` paths hold
the prior mutable generation during publication. Chapter metadata is removed first and published
last, so it never claims a half-swapped generation. A caught publication failure removes candidate
targets and restores every backup. Existing snapshots, runs, jobs, prompt packages, attempts,
responses, and translations are outside the transaction and remain byte-identical.

An abrupt process termination can leave a backup and a missing or partially replaced mutable target.
Those fixed backups are recovery evidence: inspection reports a blocker and apply refuses. Do not
delete the backup blindly. Compare it with the canonical paths and restore the complete prior
generation before rerunning the same dry run. The workflow does not provide automatic crash recovery
because choosing between two externally interrupted generations is intentionally explicit.

Translation snapshot creation validates all selected references before creating the output root. It
writes the manifest, validation report, and frozen snapshot metadata into a sibling staging directory,
validates their contracts and identity, then atomically renames the complete directory. Staging paths
and incomplete directories are never usable snapshots. A valid frozen same-ID snapshot skips without
timestamp or label mutation and cannot be forced. An incomplete same-ID directory remains evidence;
explicit `--force` may replace only that non-frozen directory through a recoverable sibling backup.
Read-only validation never repairs a snapshot. Referenced drift makes it invalid and requires a new
snapshot after the canonical upstream state is resolved.

Translation run creation validates its snapshot, builds every job in a sibling staging directory,
validates one-to-one coverage and ordering, writes the report and ready run metadata last, then uses a
root allocation lock and atomic directory rename. A publication race fails without reusing or
overwriting an existing run.

Queue mutations use a bounded POSIX advisory run lock. The kernel releases it after process failure;
version 1 guarantees local-filesystem single-host coordination only, not distributed or NFS locking.
A crash after a job write but before the run aggregate write leaves a detectable counter mismatch.
Recovery may release expired claims, promote due retries, and repair derived counters/statuses. It
never lowers attempts, recreates missing jobs, changes a snapshot, or marks work succeeded. Release
and lease expiry preserve the consumed claim attempt. Structural corruption remains for diagnosis.

Prompt-package rendering completes batch preflight before writing. Each package is built in a private
sibling staging directory, validated there, and atomically renamed only after all four files exist;
metadata is written last inside staging. An unexpected batch failure preserves earlier valid packages
and exposes no validated directory for the failed job. A partial or stale package blocks a normal
rerun. `--force` may replace only safe stage-owned package evidence when no request, response,
translation, provider, review, or approval evidence exists and the job remains eligible. Queue job
metadata, claims, attempts, leases, counters, and snapshot files are never repaired or changed by this
stage. A failed replacement restores the prior directory when possible and leaves backup evidence if
cleanup itself fails.

Provider execution uses `attempts/.NNN.reservation.yaml` because the queue lock is intentionally not
held during a network call. A crash while the reservation remains `prepared` proves the send marker
was never published; recovery may remove that stage-owned marker and restore `running → claimed`.
Once it is `requesting`, delivery is unknowable: recovery reports a blocker and never calls the
provider again. A crash after provider response but before attempt publication likewise leaves
requesting evidence for manual resolution. A crash after complete attempt publication but before job
metadata update is detectable from the immutable attempt and must be reconciled by a future explicit
command rather than replayed.

A `requesting` reservation is resolved only by the explicit, audited
`resolve-translation-reservation` workflow, never by automatic recovery and never by a generic force
reset. The operator asserts one external fact the workspace cannot observe, and each fact has fixed
consequences:

- `provider_not_contacted` — inference definitely never started. The attempt slot is abandoned
  (`abandoned_attempt_count + 1`), so it costs no provider attempt, and the job returns to `pending`
  for an ordinary claim. This is the only resolution that permits replay.
- `provider_contacted_no_result` — the request may have been delivered and billed. The attempt is
  charged and the job becomes `failed`; nothing is replayed automatically, and only a separate
  explicit `retry-translation-job` decision (with the configured backoff) can schedule attempt N+1.
- `provider_contact_unknown` — delivery is unknowable. The attempt is charged and the job becomes
  `blocked`; the queue offers no replay path at all, and moving it requires a further deliberate
  decision outside this workflow.

The command plans by default and writes nothing; `--apply` publishes
`jobs/<chunk>/resolutions/NNN/resolution.yaml` by atomic directory rename and then updates job and
run metadata under the queue lock. The reservation itself stays byte-identical forever. Preflight
refuses on any drift: an invalid run structure, a job that is not `running`, an attempt number that
is not the job's current slot, a reservation whose bytes are non-canonical or whose lineage does not
match, a `prepared` reservation (ordinary recovery owns that case), a published attempt directory for
the same or a later slot, a later reservation marker, a prompt package whose identity no longer
matches the reserved request, or evidence that supersedes the attempt.

What supersedes an attempt depends on what the interrupted execution was, and the two kinds are
told apart structurally rather than by reading an attempt that may never have been written: a
`pending` job may not carry an acceptance status at all and an ordinary claim only ever starts from
`pending`, so an *active* job that still carries one is a repair (`is_active_repair`). For an
ordinary translation the rule is unchanged — any acceptance, re-evaluation or reuse at all means
some draft already settled the job, so the unknown attempt is moot. For a repair that rule would be
wrong, because a repair exists precisely to answer a standing QA rejection: that rejection describes
the draft being repaired, not the attempt in flight. A repair reservation is therefore superseded
only by an accepted verdict, by acceptance evidence or a standing draft belonging to *this* attempt
slot, or by a reuse; and it is exempted from the blunt rule only after its rejection lineage has
been hash-verified end to end, so an unprovable repair target fails closed. "This slot" reads
`accepted_attempt` as an attempt number only when the job holds no carry-forward, because a
carried-forward draft is numbered by carry-forward and its first repair attempt therefore shares
the number — the same collision `is_failed_repair` already has to work around. Execution Resolution
Engine 1.0.0 → 1.1.0.

A published resolution is content-addressed with the engine version that wrote it, and that
recorded version — not the installed one — is what its identity is verified against, so bumping
this engine never invalidates resolutions already on record.

Repeating the identical resolution is idempotent: the published record is adopted, no second record
is written, and the stored reason is never rewritten. A different resolution for the same attempt is
refused as conflicting. A crash between publication and the job update leaves exactly one record and
an unreconciled job; `recover-translation-run` reports that specific blocker instead of the generic
one, and rerunning the same resolution reconciles the job without republishing lineage. Inspect
either state with `inspect-translation-reservations`, which shows unresolved and resolved
reservations, the chosen resolution, replay permission, lineage paths, and reservation-drift
integrity, and never exposes claim tokens.

Successful, failed, and ambiguous attempts are never overwritten. Clear provider failures become
queue-level retry candidates only through the existing explicit retry command. Ambiguous attempts and
exhausted claims are blocked. SDK transport retry is distinct from a new queue claim; version 1 sets
SDK retries to zero.

The translation worker runs ordinary queue recovery before processing — once, in the coordinator
process, before any execution slot exists when `--workers` is above one. Execution slots never run
recovery: a slot holds at most one live claim at a time, the coordinator knows which slots it
started, and recovery cannot tell a live concurrent `requesting` reservation from a crashed one.
That is deliberate and fail-closed in both directions. A *foreign* process — a second
`run-translation-worker`, `recover-translation-run`, or a provider-recovery pass — still sees a
running job with a `requesting` reservation as a blocker while a pool is executing it, so
launching independent workers against one run is not a supported concurrency mechanism and is
refused at startup; and when a pool starts, any running job left by an earlier process is still a
blocker exactly as before. `--auto-recover-provider-failures` is refused with more than one worker
because re-entering a recovered job runs global recovery, which would both refuse on the other
slots' live executions and, worse, could remove another slot's `prepared` reservation between its
publication and its send.

A stop request to the coordinator (Ctrl+C, SIGTERM, or `--stop-on-error` after an `error`
outcome) stops dispatch; in-flight slots finish their job through the ordinary finalization path,
so no reservation is left behind by a stop. For exactly that reason each slot starts its own
session: a terminal Ctrl+C is delivered to the coordinator only, and the provider CLI a slot
launches stays in the slot's session, so an in-flight provider request is not cut short. Slots
also ignore SIGINT and SIGTERM sent to them directly. The coordinator waits for in-flight work,
bounded per dispatched item by the provider timeout plus the lease margin plus a ten-minute
allowance, multiplied by the provider calls automatic repair may add, and kills a slot's whole
process group only past that bound. A second Ctrl+C interrupts hard: slots and their provider
processes are killed, and a job that was mid-call keeps its `requesting` reservation for
`resolve-translation-reservation`. A slot that dies on its own leaves the same evidence, is
reported by the coordinator as an `error` row naming the job it held, and stops further dispatch;
its claim is never transferred to another slot. If the coordinator itself is killed outright,
each slot finishes and finalizes its current job, notices the coordinator is gone, and exits
without taking more work.

Three queue-level rules make concurrent slots safe without changing what the queue records.

**The worker's per-job path runs nothing expensive under the run lock.** The lock serializes queue
*mutations*; it never
protected the frozen snapshot or the canonical workspace its lineage is rebuilt from, which change
only through stages that do not take it. So live snapshot validation — which on a real book
re-derives the book's series terminology and took about 55 seconds per load on Book 6 — runs before
the lock is requested, and a critical section is only the parse of the run's own records, the
caller's checks, and its atomic writes. Claims, execution reservation and finalization, retry
scheduling, claim-error records, and settled reads all follow this rule. Operator and remediation
mutations — repair claims, re-evaluation, structural repair, carry-forward, reuse, the semantic gate,
reservation resolution, escalation, release, and queue recovery — still validate while holding the
lock; they are not meant to run beside a live pool and are candidates for the same treatment. Acceptance is two-phase:
attempt validation, frozen-resource hashing, and terminology, structural, and alignment QA run with
no lock held, and the lock is then taken only to reload the run and *compare and revalidate* — the
job must still be the same unassessed provider-successful draft, naming the same successful attempt
under the same snapshot, and every input the evidence hashes (attempt record, source, translation,
terminology) must still hash as it did. A job another process assessed meanwhile is answered with
its recorded verdict; anything else that changed publishes nothing and fails the job's acceptance
with a diagnostic. Before this, an acceptance held the lock across two live snapshot validations
(its own load and the one inside attempt validation) and a claim across two (its load and the
mutation's snapshot re-check): on run `tr-014220572d3c-004` one slot's acceptance kept another
slot's claim waiting past the 120-second timeout.

**The run lock timeout is 120 seconds**, sized for a waiter queued behind several slots' critical
sections. It fails closed with `QueueLockTimeout` and is not a knob for fitting long critical
sections: work that does not mutate the queue belongs outside the lock.

**Execution-gating reads are settled.** The two reads that gate execution on queue validity — the
executor's plan and prompt-package planning — are *settled* reads: a read outside the lock that
catches another slot between its job write and its run-aggregate write is re-read once under the
lock, and only a mismatch that survives that consistent read is reported. A crash signature that genuinely persists is therefore still
reported, and the sequential worker's behavior is unchanged. The worker may automatically schedule
a clear retryable provider failure through the same failed-to-retry-pending transition and configured
backoff; it never replays an ambiguous attempt. A job is processed at most once per worker invocation,
even with zero backoff, and `max_attempts` remains authoritative. QA rejection is not automatically
retried because the same frozen prompt is unlikely to repair itself. Job or chapter targeting does
not change recovery: ordinary queue recovery still runs first, and a targeted job that recovery
leaves ineligible ends the invocation with a reported reason instead of a substituted job.

Acceptance evidence is computed outside the queue lock, then — under the lock, after revalidation —
staged and atomically renamed beside the immutable attempt, and job and run metadata are updated.
A crash between those operations leaves valid evidence and
an unassessed provider-succeeded job. The next worker validates and reconciles that exact evidence
without changing the attempt or contacting the provider. Conflicting or hash-invalid evidence blocks
safe acceptance. Claim tokens appear only in in-memory calls between queue and executor.

Explicit repair uses the same prepared/requesting reservation protocol. A crash before send can be
recovered without provider contact; an expired repair claim restores the preceding QA-rejected
provider-success state unless attempts are exhausted, and an administrative
`release-translation-job` of a repair claim restores the same state. Claiming a repair clears
`completed_at`, so both paths read it back from the job's own immutable acceptance evidence, whose
`created_at` is the instant the standing verdict was published; the reference and its hash are
re-checked first, and a settlement that cannot be proven from lineage blocks — as an error on
release, as a reported blocker that leaves the claim untouched on recovery — rather than
substituting the clock. A requesting reservation is ambiguous and is never replayed. A published repair attempt without acceptance is assessed by the ordinary worker,
and published acceptance evidence without job metadata is adopted only after hash validation.

A repair whose provider call *failed* leaves a distinct state: the job is `failed`, but the QA
rejection it was answering is untouched and `accepted_attempt` still names the rejected draft
rather than the attempt that just failed. Nothing else produces that shape, and it is what the
bounded repair-recovery boundary acts on. Recovery re-derives the repair request, proves it
byte-identical to the one that failed, publishes an authorization, and then *restores* the
QA-rejected draft — putting back `status: succeeded` and `successful_attempt` — so the ordinary
repair path can answer the same rejection again.

What `successful_attempt` becomes depends on which draft is being restored, and the two cases are
proven apart rather than chosen by the caller. A draft this run produced is a provider success and
its attempt number goes back. A **carried-forward** draft was inherited from a historical run:
there is no successful provider attempt behind it, `successful_attempt` stays null, and supplying a
number is refused — a carry-forward is numbered by carry-forward, so the number would be a
carry-forward number and writing it as an attempt would assert a provider success that the failed
execution never produced. The carry-forward case is admitted only after the repair boundary's own
prover verifies the acceptance bytes, the carry-forward record behind them, the two numbers
agreeing and the historical attempt; after the translated bytes on disk re-hash to what the
acceptance says; and after a scan proves no successful provider attempt exists for the job at all,
which is what keeps the two settlements from being confused. `completed_at` is never supplied
either: it is read from the standing acceptance evidence, the same value a released repair claim is
settled back to, so every route back to a settled rejection writes one timestamp.

Operators reach this through `restore-translation-repair-target RUN JOB --reason … [--apply]`,
which plans by default, prints the whole before/after including attempt accounting, contacts no
provider, and leaves every attempt, acceptance, reservation and resolution record byte-identical.
A restoration is not a provider recovery and publishes no authorization of its own; it is how a job
whose repair was ended by a failure — or by an operator's `provider_contacted_no_result` assertion —
returns to being repair-eligible within its remaining attempt budget. It never routes the job through `pending`, where an ordinary translation claim could reach
a job that already has a successful attempt. The failed repair attempt is never rewritten and
still counts against the provider budget. An interruption between the published authorization and
the repair claim is safe: the restored state is simply an ordinary QA-rejected job, and the
authorization already on record keeps the replayed execution from being charged as a second QA
repair decision. Recovery of a repair is refused outright when the derived request does not
reproduce, when a reservation is outstanding, or when the failure was ambiguous.

For `codex-cli`, the provider preflight runs before recovery and before any claim, exactly as for
`claude-code` below, so a missing executable, an installation whose `exec` mode lacks a required
capability, or a logged-out session ends the invocation with a reported reason while the queue,
attempt counters, and lease state stay exactly as they were. After process start, a missing
executable, invocation failure, nonzero exit, or empty final message is a clear failed attempt. A timeout or interruption after process start is ambiguous and blocks automatic
replay. Temporary working directories and final-message files are cleaned by the adapter; a process
crash after the executor's requesting marker retains the same manual-recovery boundary as every other
provider.

For `claude-code`, the provider preflight runs before recovery and before any claim, so a missing
executable, an installation without the required non-interactive flags, or a logged-out session ends
the invocation with a reported reason while the queue, attempt counters, and lease state stay exactly
as they were. After process start the classification mirrors the other CLI provider: a nonzero exit,
empty final result, malformed structured output, or reported error result is a clear failed attempt,
an output-token overrun is a retryable truncation, and a timeout or interruption is ambiguous and
blocks automatic replay. The private temporary working directory and staged system prompt file are
removed when the adapter returns.

Terminology mutations atomically replace only current `config/terminology.yaml`. They never update
snapshots, runs, packages, attempts, or translations. A locked user decision blocks ordinary
replacement. After an intentional current decision change, create a new snapshot for future work;
historical snapshots retain their frozen terminology lineage. New snapshot manifests reference an
immutable `workspace/resources/<category>/<sha256><suffix>` copy of every mutable translation input —
prompt, glossary, style guide, terminology, source anomalies, open questions, and resolved book
metadata — published idempotently before the snapshot directory. A matching copy is reused; a missing
or hash-conflicting copy blocks publication, and a manifest whose input has no durable frozen artifact
is refused before anything is written. Legacy snapshots without those optional references are not
rewritten and retain their prior semantics.

Re-running `resolve-book-metadata --apply` after a crash is safe: an unchanged resolution is a skip
that leaves `workspace/metadata.yaml` byte-identical, and publication is a single atomic replace that
is re-read and compared against the planned record before it is accepted.

`migrate-book-identity --apply` re-verifies every registry's pre-migration hash before writing and
refuses on drift, so a stale plan can never be applied. Registries are replaced atomically one at a
time and each is hash-verified immediately; the immutable lineage record is written last, so an
interrupted migration leaves some registries migrated and no record. Recovery is to rerun the command:
already-migrated registries are simply absent from the new plan, and a fully migrated project reports
`skip` and writes nothing. Re-publishing an existing migration record with different content is
refused rather than overwritten. `freeze-translation-resources --apply` is likewise idempotent:
byte-identical copies are reused and a hash-conflicting copy blocks publication.
Read-only terminology and Translation QA commands create no report artifact and require no recovery
action.

Fixing a QA algorithm does not authorize rewriting immutable acceptance evidence or its assessed
attempt. Historical evidence continues to describe the engine result at its original assessment
time. Reconciliation after a QA fix should be a separate explicit operation that validates the frozen
attempt, source, terminology, and prior-evidence hashes; publishes new immutable, versioned
re-evaluation evidence linked to the original acceptance; and only then atomically updates the job's
current acceptance pointer and derived run counters. It must never replace
`acceptance/NNN/evidence.yaml`, reuse its hash, or imply human review or approval.

QA re-evaluation implements that reconciliation contract. Evidence is published first by atomic
directory rename under `reevaluations/NNN/`; the job pointer and run counters are written afterward
under the queue lock. A crash before rename leaves only private staging, which is never history and
must be diagnosed before retry. A crash after rename but before pointer update leaves one contiguous
orphan record. Repeating the same command recomputes its deterministic identity, adopts that exact
record, and repairs the pointer/counters without rewriting it. A different identity beside an
unreconciled orphan blocks publication. Hash conflicts, numbering gaps, stale pointers, missing
historical acceptance, or corrupted frozen inputs require operator diagnosis and are never forced.

Terminology discovery and proposal runs publish all files through a private sibling staging directory
and one atomic directory rename. A complete same-ID run is immutable and repeated execution skips it.
Staging directories are never inputs. A returned malformed or evidence-invalid structured response
publishes an immutable failed run containing the safe structured response, output hash, validation
error, and `contacted: true`; it never mutates the registry. Retryable, permanent, and ambiguous
provider errors remain distinguishable to callers.
Each analysis stage makes one provider request and writes a `.ta-*.requesting.json` marker immediately
before the call. Clear pre-response provider failures remove it; ambiguous delivery or a returned
invalid result retains it and blocks automatic replay. Inspect with
`book-translator inspect-terminology-request RUN_ID`. After diagnosis, plan an explicit retry with
`--retry-of RUN_ID`; this preserves the marker/failed artifact and creates a new deterministic run ID.
Proposal execution refuses if the current registry hash changed after planning. Applying proposals
changes only the current registry atomically; rerun discovery/proposal or re-plan application after a
registry race rather than editing immutable run artifacts.
Proposal plans also hash the legacy glossary used by their unified canonical view and refuse execution
after glossary drift. Historical proposal output is never rewritten; use
`audit-terminology-proposal-run` to evaluate it against current canonical knowledge.
Review uses the same requesting-marker and provider-contact durability protocol. Returned malformed,
missing, duplicate, or contract-invalid review output publishes a failed contacted run and retains its
marker. Explicit `--retry-of` creates a new identity. Review and audit never mutate the registry.
Target-style adapter and review-prompt versions participate in new request identity. Consequently, an
explicit retry after either contract changes produces a new deterministic run ID while preserving the
failed run, provider output, validation report, and requesting marker byte-for-byte.
## Accepted translation reuse recovery

Dry-run does not acquire a mutation lock and writes nothing. Publication holds the new run's queue
lock and never locks or mutates the historical run. The reuse directory is atomically renamed before
acceptance and job metadata publication. If interruption occurs after that rename, rerunning validates
and adopts the identical reuse evidence, publishes or validates current-QA evidence, and repairs the
job/run pointers. Conflicting or corrupt partial evidence is refused. Once the job pointer is current,
an identical rerun skips it without rewriting artifacts. Historical attempts, acceptance,
re-evaluations, job metadata, and run metadata remain byte-identical.
## Historical rejected-output re-evaluation recovery

Dry-run acquires no mutation lock and writes nothing. Publication holds the new run's queue lock only
and never locks or mutates the historical run; the plan is re-derived under that lock and refused if
anything moved since planning. The carry-forward directory is atomically renamed into place before
acceptance and job metadata are published. Interruption after that rename is recoverable: rerunning
revalidates the identical evidence, publishes or validates the current-QA acceptance evidence, and
repairs the job and run pointers. Conflicting or corrupt partial evidence is refused rather than
overwritten. Once the job pointer is current, an identical rerun is recognized as already applied and
writes nothing. Historical attempts, acceptance, re-evaluations, job and run metadata, and every
frozen resource remain byte-identical.

## Cross-snapshot run migration recovery

`migrate-translation-run` without `--apply` writes nothing. With it, every job is published through
its owning boundary under the successor's queue lock, one job per lock acquisition, after an
under-lock re-plan that must reproduce the planned decision; each publication is atomic and
idempotent, so an interruption leaves every earlier job complete and a rerun reports it as
`already_migrated`. A decision that changed is refused for that job and nothing is written for it.
The historical run is never locked or written. Carried rejected drafts are repaired only by
`repair-translation-job`; a provider failure during that repair is not automatically recovered
(`plan_provider_recovery` refuses it for lacking attempt lineage) and the carried rejection stays
standing for an operator decision.

## Series policy amendment recovery

`amend-series-terminology` without `--apply` writes nothing. Application publishes the immutable
amendment record first and then atomically replaces the policy file, re-checking that the live policy
still hashes to the amendment's "before". If interrupted between the two writes, the next plan finds
the record whose "before" matches the live policy and completes it (`complete`); once applied it is
`skip`. A policy edited after planning refuses publication. Rolling back a policy is itself a new
amendment; records are never edited or deleted.

# Source quality audit recovery

The default audit is read-only and needs no recovery. With `--output`, publication writes a complete
report only to a new audit directory; an identical existing report is reused and conflicting content
is refused. Audit findings do not mutate or invalidate extraction, normalization, chunks, snapshots,
or translation history. Resolve a finding through the explicit workflow for its owning layer, then
run a new audit whose changed lineage produces a new identity.
Resolution analysis is read-only and has no separate recovery action. A downstream regeneration can
change resolution and report identity but cannot delete or rewrite a prior persisted audit.
# Source anomaly recovery

A changed immutable-source hash prevents a registry entry from matching current source lineage.
Verify the edition and register a new source fact; never repair `source.md` in place. Status, span, or
registry-content changes produce new frozen snapshot lineage. Historical frozen resources remain
valid. Operator-note changes keep the stable anomaly ID but still change auditable registry bytes.
# Final QA resolution recovery

`audit-final-qa` is read-only; its artifact is derived, so re-running it simply replaces
`audit.yaml` and never touches the finalization history beside it. `finalize-translation-run`
defaults to `--dry-run` and, in that mode, writes nothing at all — an interrupted dry run leaves no
state to recover.

An interrupted `--apply` leaves the boundary it was inside, not a half-finished finalization. A
re-evaluation is idempotent by identity; a repair that reached the provider is recovered through
the ordinary provider-recovery boundary, which `finalize-translation-run` now drives itself unless
`--no-provider-recovery` is passed, and the audit still reports such a job as `operator_required`
with `provider_recovery_required` rather than failing to load. Re-running the finalization is safe:
it re-diagnoses from current state and takes the next finalization number instead of replacing the
previous report.

A structural repair publishes its directory atomically through a staging rename and only then moves
the job's pointer, so an interruption between the two leaves published evidence that nothing points
at. Re-running is safe and does not duplicate it: an existing record whose superseded evidence and
repaired-translation digests both match the re-derived repair is reused under its own number rather
than republished under a new one. A repair whose repaired bytes would differ from an existing record
at the same number is refused rather than overwritten. Recovering *from* an adopted repair is not a
recovery action at all — the provider attempt, its translation, and its acceptance sidecar were
never modified, so the pre-repair state is still on disk in full.

An exceptional repair escalation is published *before* the attempt ceiling moves, so an
interruption between the two leaves an unspent grant rather than a widened budget nobody
authorized. The queue reconciles the two on the next load: a ceiling above the run's must be
accounted for exactly by persisted escalations, and one that is not is a stale queue. A second
grant for the same job is refused; there is no way to widen a budget twice, and no counter is ever
reset to simulate one.

A job left `operator_required` needs no recovery — it is the correct terminal state for the run.
Settling the terminology question behind it means freezing terminology again, taking a new
snapshot, creating a new run, and carrying the accepted work forward with
`reuse-accepted-translations`. The frozen run is never edited to match a later registry.

# Persian literary editing recovery

Planning and `--dry-run` write nothing and contact no provider. Applying creates a private hidden
`.lr-<id>.staging` directory and freezes the exact style/profile bytes there. Each chapter is written
completely into its own private temporary directory and atomically renamed only after its final text,
mechanical evidence, post-edit semantic evidence, correction records, continuity response, and
chapter artifact agree. An interrupted chapter has no canonical directory; rerun processes it again.

A completed chapter inside the parent staging directory is resumable. Rerun validates its artifact
and final text hash and skips the provider calls for that chapter. A conflict is refused rather than
overwritten. After every selected chapter and the blind document/key/manifest exist, the run artifact
is written and the whole staging directory is atomically renamed to
`workspace/literary-runs/<lr-id>/`. A valid completed run is immutable and an identical invocation is
a no-write skip.

If a versioned prompt/schema change creates a new run identity after a chapter completed, pass the
old identity with `--reuse-literary-run`. Recovery accepts only compatible atomic chapters after
checking frozen style/profile bytes, source and faithful paragraph hashes, engine/policies,
provider/model identity, output hash, and every semantic-evidence hash. It copies and re-hashes the
self-contained evidence into the new run and marks both the chapter and its calls with the source
literary run. Missing or incompatible chapters execute normally; completed calls are not repeated.

The input translation run and historical release are never locked or modified. If a literary edit or
correction fails marker, structural, alignment, terminology, or semantic verification, recovery is
paragraph-local: restore the already accepted faithful bytes and record `literary_edit_fallback`.
There is no destructive invalidation and no force path around downstream safety. Corrupt completed
evidence is a hard error; create a new content-addressed run after changing a versioned input or
policy rather than repairing it in place.
