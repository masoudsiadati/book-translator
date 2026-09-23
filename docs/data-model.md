# Data model

Terminology Review Engine 2 adds content-addressed `trs-<24 hex>` shards beneath private parent
staging. Identity covers proposal lineage, ordered IDs, prompt/resources, provider semantics, and
engine version. Public aggregate contracts remain compatible with historical pilot reviews.

The authoritative Python contracts are in `src/book_translator/models/`; machine-readable
counterparts are in `schemas/`. Models reject unknown fields. Paths stored in metadata are
project-relative canonical paths, use `/` separators when serialized, and must not escape the
project root.

## Documents

- `workspace/metadata.yaml` — `BookWorkspaceMetadata`: schema version, stable book ID, PDF path
  and hash, titles, author, languages, expected output, and timestamps.
- `workspace/toc.yaml` — `TocDocument`: schema version, embedded-source provenance (source PDF,
  byte-exact hash, PDF page count, and entry count), and ordered normalized `TocEntry` values.
  Pages are inclusive and 1-based. Kinds are `front_matter`, `prologue`, `chapter`, `epilogue`,
  `appendix`, and `unknown`. Structural title prefixes determine narrative kinds independently of
  embedded hierarchy level. `source_toc_title` preserves the embedded TOC label with surrounding
  whitespace removed. Entries with the same start page share an end page determined by the next
  later distinct start page; the final distinct group ends at the PDF page count.
- Chapter `metadata.yaml` — `ChapterMetadata`: identity and titles, inclusive PDF boundaries,
  source/cleaned paths and hashes, counts, lifecycle status, warnings, algorithm versions, and
  timestamps.
- Chapter `normalization-report.yaml` — compact schema 1.0 provenance: chapter and source/cleaned
  hashes, algorithm version, ordered passes, physical-line and paragraph counts, page transitions,
  aggregate marker/join/hyphen/ambiguity counters, warnings, timestamp, and structural change records.
  It stores no book paragraphs or snippets.
- Chapter `chunking-report.yaml` — Document Chunk Engine schema 1.0 lineage, configuration, source and
  chunk counts, scene/tail decisions, oversize warnings, context totals, reconstruction hash, and
  conservation result. It contains no text snippets.
- Chunk `metadata.yaml` — `ChunkMetadata`: identity and order, artifact paths and hashes, counts,
  lifecycle status, attempt counts, provider/model/version provenance, error, and timestamps.
- `workspace/snapshot.yaml` — `ExtractionSnapshot`: snapshot/book IDs, PDF and normalized TOC
  hashes, algorithm versions, count, source and cleaned hashes for every chapter, creation time,
  validation flag, and warnings.
- `workspace/snapshots/translation/<snapshot-id>/manifest.yaml` — deterministic
  `TranslationManifest` references for the book, PDF, TOC, selected cleaned chapters, chunks,
  contexts, prompt, glossary, style guide, languages, and engine versions. The identity payload is
  stable sorted canonical JSON, not YAML bytes.
- The same directory's `snapshot.yaml` and `validation-report.yaml` — frozen publication metadata
  plus prose-free validation checks and counts. Labels and timestamps do not affect identity.
- Adjacent `config/*.metadata.yaml` records use `VersionedResourceMetadata` to bind prompt,
  glossary, and style-guide paths to semantic versions without rewriting resource bytes.
- `workspace/translation-runs/<run-id>/run.yaml` — `TranslationRun` snapshot lineage, queue version,
  configuration, lifecycle, timestamps, and derived job counters.
- The same run's `queue-report.yaml` — immutable creation-time coverage, ordering, configuration, and
  snapshot-validation evidence.
- `jobs/<chunk-id>/job.yaml` — `TranslationJob` structural chunk/context references, versions,
  priority, attempt/claim/lease/retry state, errors, and at most 20 recent prose-free queue events.
  Claim tokens are persisted for future executor authorization but excluded from list/show output.

Timestamps are timezone-aware ISO 8601 values in serialized YAML/JSON. Hashes are lowercase,
64-character SHA-256 hex strings. IDs are lowercase filesystem-safe ASCII matching
`[a-z0-9][a-z0-9._-]*`. Deterministic chapter IDs equal their directory name; chunk IDs are
`<chapter-id>-<three-digit-sequence>`. Titles, including Persian text, live only in metadata.

Chapter extraction counts source words by splitting each nonempty extracted PyMuPDF text block
on Unicode whitespace; Markdown page-marker comments are never counted. The source paragraph
count is the number of nonempty extracted text blocks, not a linguistic paragraph inference.
The `source_sha256` is calculated from the exact final persisted UTF-8 `source.md` bytes after
the atomic source write. At extraction time `cleaned_file` records its canonical future path,
while cleaned hashes/counts are null and `normalization_version` is empty.

Normalization counts nonblank text physical lines and excludes marker comments. The cleaned paragraph
count is the number of logical blocks after conservative page-boundary continuation joins. Cleaned
words are the final Markdown split on Unicode whitespace. The cleaned SHA-256 covers exact persisted
UTF-8 bytes, including one final newline. Marker removals, wrapped joins, page-continuation joins, and
hyphen repairs count individual structural operations, and reported page transitions remain 1-based.
Normalization Engine 1.5.0 and page-boundary-continuations 1.4.0 retain the existing schema while
adding deterministic canonical-page-only continuation shapes for delayed dialogue punctuation,
lowercase narrative dash clauses, and capitalized article complements; Normalization Engine 1.6.0 and
page-boundary-continuations 1.5.0 add the lowercase-edge-to-possessive-name shape. Older normalized
generations are valid historical data but stale for current-lineage planning.

Success updates cleaned path/hash/counts, algorithm version, warnings, `status: normalized`, and
`updated_at`. It preserves source metadata/hash/counts, extraction version, and `created_at`. These use
existing schema 1.0 fields, so no stored-data migration is required.

Chunking treats each canonical blank-line-separated cleaned block as one paragraph. Paragraph and
chunk word counts split on Unicode whitespace; context counts are separate. Chunk `source.md` preserves
exact paragraph text with canonical separators and a final newline. `context.md` is empty for the first
chunk and otherwise contains only preceding text; it is never translated as chunk source and does not
participate in reconstruction.

`ChunkMetadata` records source/context paths and hashes, source/context counts, optional context
paragraph provenance, cleaned-source lineage, engine version, oversize state, warnings, and future
translation paths without creating `translation.md`. Chunk IDs retain the canonical
`<chapter-id>-<three-digit-sequence>` form. Successful chapter publication sets `chunk_count`,
`chunk_engine_version`, `status: chunked`, and `updated_at` while preserving extraction and
normalization lineage and `created_at`.

### Compatible chunk metadata extension

Document Chunk Engine 1.0.0 adds defaulted fields to schema 1.0: chunk context hash/count/range,
chunk-engine version, cleaned-source hash, oversize flag, and warnings, plus a defaulted chapter
chunk-engine version. This is additive and existing stored chapter/chunk metadata remains valid; no
rewrite or data migration is required. The committed schemas are regenerated from the canonical
models. Removing or making these fields required later would require a breaking migration.

- `workspace/books/<book>/terminology/occurrence-exemptions.yaml` —
  `TerminologyOccurrenceExemptionRegistry` (schema `terminology-occurrence-exemptions`), with a
  `.metadata.yaml` resource version. Each `TerminologyOccurrenceExemption` names one source
  occurrence by chunk ID, chunk source SHA-256, span, spelling ordinal, paragraph number and
  SHA-256, candidate, decision ID and decision digest, and series concept. It records the sense
  (`ordinary_english`), `decided_by: user`, a rationale and a timestamp, and no source prose. A
  translation manifest freezes the file as the optional `terminology_exemptions` resource, and prompt
  packages reference it. Both fields are omitted when absent.

## Artifact mutability

`config/source-anomalies.yaml` is the strict `SourceAnomalyRegistry`: deterministic identity,
book/chapter/page/location scope, exact original and immutable-source hashes, explicit status,
severity and blocking policy, separate verification evidence, and no guessed correction. New
translation snapshots freeze exact bytes under `workspace/resources/source-anomalies/`. Optional
manifest/snapshot/prompt fields preserve legacy identity when absent and make registry hash/version
material to new lineage when present.

- Chapter `source.md` is conservative raw extraction. After a successful atomic write it is
  immutable to normalization, translation, and review. Replacement requires an explicit forced
  extraction rerun.
- `cleaned.md` derives only from the recorded `source.md` hash.
- Chunk `source.md` derives only from the recorded chapter `cleaned.md` hash; `context.md` is
  supporting input and not translated as source.
- Chunk `translation.md` is a draft until its chunk status is `approved`.
- Chapter `final.md` is assembled only when every included chunk is approved and hash-current.
- Failed or review-required records retain all previous artifacts.
- An assembled release under `workspace/releases/<release-id>/` is derived output. It is never read
  back by a translation stage, and release assembly writes nowhere else.
- The series terminology policy is a governed mutable resource; the derived book registry and the
  reference-context index are regenerated deterministically from it and the canonical chunks; a
  per-chunk reference package under `resources/reference-context/<sha256>.yaml` is immutable and
  content-addressed from birth. The external reference corpora are never copied or modified.
- A chapter-title artifact under `workspace/resources/chapter-titles/<ct-id>/` is immutable
  metadata. It is produced only by chapter-title translation, consumed read-only by release
  assembly, and never read back by a translation stage.

`cleaned.md`, `normalization-report.yaml`, `chunks/`, `chunking-report.yaml`, and the derived fields in
chapter metadata form the mutable deterministic generation. They may be replaced together only by the
explicit regeneration workflow after source integrity, existing mutable integrity, local downstream
evidence, and recovery paths pass preflight. Translation snapshots, runs, queue history, prompt
packages, provider attempts, request/response artifacts, and translations are never rewritten by
regeneration.

## Schema versioning

Version `1.0` uses `major.minor`. An additive optional field or newly accepted enum value may be
a compatible minor change, provided old readers and writers remain safe. Removing or renaming a
field, changing meaning/type, making an optional field required, or changing stored layout is a
breaking major change.

Existing data is never silently rewritten. Breaking changes require a documented, explicit,
idempotent, testable migration with backup/recovery guidance before consumers change. Future
commands must accept supported older versions deliberately and refuse unsupported newer
versions. No migration engine is implemented yet. The legacy `state/progress.json` model remains
unchanged and powers the existing `status` CLI; it is not a canonical workspace document.

The optional TOC provenance fields are a compatible additive clarification of schema 1.0.
Writers populate them; their defaults preserve validation compatibility with earlier schema 1.0
TOCs containing only `schema_version` and `entries`.

Translation snapshot schema 1.0 is additive and does not change extraction snapshot semantics.
Snapshot and artifact paths are project-relative POSIX paths. Frozen snapshot contracts, identity,
and persisted validation evidence remain internally valid immutable history after canonical mutable
artifacts change. Live-lineage validation then reports the lineage as not current; new queue creation,
claiming, prompt rendering, and provider execution refuse that historical lineage. New translation
work requires a new content-derived snapshot ID.

Translation Queue schema 1.0 is additive. Run IDs are allocated per snapshot digest and output root;
job IDs combine the run and canonical chunk IDs. Aggregate run counters are derived and validated
against job files rather than treated as authoritative. Counter/status repair is safe derived-state
repair; missing, extra, duplicate, malformed, or stale jobs are structural blockers.

Prompt-package schema 1.0 is additive and does not alter run or job records. `package.yaml` records a
24-hex content-derived ID, the complete canonical identity hash, exact system/user hashes and counts,
snapshot/job/chunk lineage, resource versions/hashes, language pair, token estimate, and output
contract `persian-translation-text/1.0.0`. Its paths are project-relative POSIX paths. The validation
report contains booleans, checks, warnings, and errors only—never prose.

Glossary rendering uses fixed category order: approved terminology, characters, places, groups, then
unresolved terms. Entries sort by Unicode source term; duplicate source terms across categories are
invalid. Unresolved terms are explicitly nonmandatory. Token estimates are `ceil(message characters
/ 4)` and are planning estimates, not provider tokenizer output or billing data.

Translation attempt schema 1.0 records provider/executor versions, package and queue lineage,
provider/model/response identifiers, artifact paths and hashes, timing, usage, retry/ambiguity
classification, output contract, and structural warnings. It never stores credentials or claim
tokens. Request JSON references existing system/user files and hashes instead of duplicating prompt
prose. Optional sanitized response JSON may contain the returned draft; `translation.md` contains the
exact LF-normalized extracted text with one final newline.

The rejected-output fields are a compatible additive clarification of attempt schema 1.0. A failed
attempt whose single complete answer was refused by structural validation records
`rejected_output_file` and `rejected_output_sha256` for the private `rejected-output.md` beside it
(the exact LF-normalized candidate bytes, never a translation), and a marker-contract refusal records
`marker_findings` (the persisted `ParagraphAlignmentFinding` shape: code, severity, source paragraph
index, expected marker, observed marker token, message), `expected_marker_count`, and
`observed_marker_count`. No field carries a snippet of the answer. Attempts written before these
fields existed load with their defaults (`null`, `[]`), which is the truthful record that their
prose was not retained; they are never rewritten. `TranslationAttemptValidationReport` gains
`rejected_output_hash_valid`, `null` on reports written before the check existed.

`codex-cli` is an additive provider enum value. Its requested and returned model fields may be null
when the command deliberately omits `--model`, because final-message mode does not expose a stable
machine-readable effective-model field. No authentication or session state is stored in attempt
artifacts.

`claude-code` is an additive provider enum value for the local Claude Code CLI. Its requested model
may be null when `--model` is deliberately omitted, but the returned model is normally present
because the structured result reports the model actually used; it stays null only when the CLI
reports more than one model for a single call, which is never assumed to be the translating model.
The sanitized response records execution mode, CLI version, authentication method (`claude.ai` login
rather than an API key), first-party inference provider, canonical model name, the enforced output
token limit, and that tools and session persistence were disabled. Account identity and credentials
are never stored.

Job schema additions (`provider`, `model`, `last_attempt`, and `successful_attempt`) are optional and
backward-compatible. `succeeded` means provider execution completed; it is not review or approval.
Exhausted pending or released claims derive to `blocked` without reducing attempts.

`attempt_count` is the attempt-slot sequence: it advances on every claim so each execution owns its
own immutable `attempts/NNN/` directory, and it is never lowered. The optional, backward-compatible
`abandoned_attempt_count` (default `0`, absent on historical jobs) counts slots an operator
explicitly resolved as never having reached a provider. The derived `provider_attempt_count`
(`attempt_count - abandoned_attempt_count`) is the authoritative provider budget compared against
`max_attempts` for claim eligibility, retry eligibility, repair eligibility, and permanent-failure
derivation. A job may therefore hold more attempt slots than `max_attempts` while still having spent
fewer provider attempts; `attempt_count` alone no longer implies provider contact.

A slot is charged when it is claimed, not when a provider answers. A claim that never reaches a
provider — an expired lease, an administrative release, or a worker failure between the claim and
the reservation — keeps its slot, and that slot counts against `max_attempts` until an operator
explicitly abandons it through reservation resolution; automatic recovery and release never abandon
one. This is deliberate: the budget bounds what a job may *consume*, and only an audited human
assertion may declare that a consumed slot cost nothing. Pre-provider misconfiguration is therefore
refused before the claim (worker and repair model-policy checks, provider preflight) rather than
forgiven after it.

`last_error_category` and `last_error_message` normally carry a provider error classification and
its sanitized message from the job's last attempt. A worker that fails while holding a claim but
before any reservation records `last_error_category: worker`, the failure class and message as
`last_error_message`, and a `worker_error` event carrying the worker ID and the same reason; none of
these change status, lease, or counters, and a later attempt overwrites them as usual.

`ExecutionReservationResolution` is stored at
`jobs/<chunk>/resolutions/<attempt>/resolution.yaml`. It records the content-derived resolution ID,
resolver engine version, `provenance: operator`, run/job/chunk/attempt identity, the resolved
reservation's project-relative path, exact SHA-256, status and provider, the reserved prompt package
ID and request SHA-256, the selected resolution, the operator's reason, the job status and both
attempt counters before and after, whether a provider attempt was consumed, whether replay becomes
permitted, and the timestamp. It contains no prose, no claim material, and no provider response, and
it never replaces the reservation it describes.

Worker schema additions are optional and backward-compatible. `acceptance_status`,
`accepted_attempt`, `acceptance_file`, and `acceptance_sha256` attach an assessed provider-successful
job to `TranslationAcceptanceEvidence`. The evidence embeds terminology and structural QA reports,
records exact attempt/source/translation/terminology paths and hashes, and derives only `accepted` or
`qa_rejected`. Run counters separately expose accepted, QA-rejected, and provider-succeeded-unassessed
jobs. Acceptance is a mechanical translation gate, not human review or approval.

`TranslationAcceptanceEvidence` and `TranslationStructuralRepairEvidence` additionally carry an
optional `alignment_report` (`TranslationAlignmentReport`) and `alignment_qa_version`. Absence is
not a pass: evidence published before alignment QA existed was never judged against that gate, and
every reader treats it that way rather than inferring a verdict. The report records the alignment
engine version, the policy identifier and version that judged it, whether identities came from
markers or from block position, the source and translation digests, the aligned, measured, and
flagged pair counts, and the findings. Findings carry codes, severities, paragraph indices, and
marker names only; the report contains no prose. `accepted` is the conjunction of terminology,
structural, and — when present — alignment validity.

`TranslationRun.output_contract_version` is optional and absent on every run created before the
numbered paragraph contract, where it means the original unmarked prose contract. It is written
once, when the run is created, and never rewritten, which is what keeps an existing run's prompt
packages, attempts, and reuse eligibility interpretable after the default for new runs changed.

Semantic coverage contracts are additive and touch no existing record. `SemanticJudgeResponse` is
the provider output contract and is validated before anything else happens with an answer;
`SemanticQAEvidence` is the immutable per-chunk, per-repetition record; `SemanticCoverageReport`
summarizes one evaluation. Evidence is stored at
`workspace/audits/semantic-coverage/<run>/NNN/chunks/<chunk>-rNN.yaml` with the evaluation summary
beside it, outside every run directory because a measurement of a run must not become part of one.

A finding carries a code from a closed vocabulary, a severity, a confidence, a bounded source
evidence fragment, and a one-sentence explanation. A paragraph outcome records the judge's verdict
and the policy's outcome separately and never collapses them, so a policy can be recalibrated
against what the judge actually said. Message digests are persisted instead of messages, and no
raw response or model reasoning is retained. Because a finding's evidence fragment is book-derived
text, the tree is written owner-only and ignored by Git.

The semantic gate adds optional, defaulted job fields — `semantic_status`,
`semantic_evidence_count`, `latest_semantic_file`, `latest_semantic_sha256`,
`semantic_revision_count`, `latest_semantic_revision_file`, `latest_semantic_revision_sha256` — and
the run field `semantic_gate_policy_version`. All default to absent, so every existing job and run
validates unchanged and reads as not judged and not gated. `TranslationSemanticRevisionEvidence`
records one revision attempt with its window, triggering findings, request provenance, measured
bounds, mechanical QA reports, adoption, and refusals; an adopted one supersedes the job's
effective translation and names the translation and semantic evidence it superseded.
`SemanticGateReport` summarizes one gate invocation in counts.

`ParagraphAlignmentAuditReport` is stored at
`workspace/audits/paragraph-alignment/<run>/report.yaml`. It lives outside every run directory
because it is a read-only measurement of a run rather than part of one, and it records
`provider_contacted: false` and `mutated_state: false` as contract literals.

Attempt schema additions are backward-compatible. `execution_kind` defaults to `translation` for
historical records; repair attempts set it to `repair` and set `repaired_from_attempt`, or — for the
repair of a draft a governed carry-forward brought into the job — `repaired_from_carry_forward` (the
carry-forward number) instead; exactly one of the two is set, and the field is omitted when absent so
earlier attempts keep their bytes. Repair attempt
N remains under `jobs/<chunk>/attempts/NNN/`, with new acceptance at
`jobs/<chunk>/acceptance/NNN/evidence.yaml`. The job points to the latest assessment while all earlier
attempt and acceptance directories remain immutable.

`TranslationAutoRepairDecision` is stored at `jobs/<chunk>/auto-repair/NNN/decision.yaml`, where NNN
is the attempt slot the decision authorized, and exists only for a repair a policy decided rather
than a person. It records the auto-repair engine and policy versions, `initiator: automatic`, the
`repairable` verdict, the classified findings and the blocking codes that triggered it, the repair
and provider attempt budgets it was drawn against, the repaired-from attempt, and hash-verified
references to the rejection evidence, the rejected attempt, the frozen source, and the rejected
translation. It adds no job fields and duplicates no outcome: the provider status, the resulting QA
findings, and the final acceptance status stay in `attempts/NNN` and `acceptance/NNN` for the same
attempt number. It contains no translation prose and conveys no human review or approval.

`TranslationProviderRecoveryDecision` is stored at
`jobs/<chunk>/provider-recovery/NNN/decision.yaml`, where NNN is the attempt slot the decision
authorized, and exists only for a provider failure a policy decided to answer with a new attempt.
It records the provider-recovery engine and policy versions, `initiator: automatic`, the
`recoverable_now` verdict, the provider-neutral failure family, the failed attempt's number, ID,
project-relative path, and SHA-256, that attempt's recorded `error_category`, `retryable`, and
`ambiguous` flags exactly as they stand, the replay safety established from artifacts, the recovery
and provider attempt budgets it was drawn against, whether a preflight was required, the preflight
evidence, the authorized attempt number, and the retry eligibility it scheduled. The historical
attempt is referenced and hashed, never modified: `retryable: false` stays `false` forever, and the
decision is a new record citing it rather than a reinterpretation of it. Preflight evidence records
provider identity, executable, CLI version, authentication status, and auth method as short
structural values; credential-shaped values and any API-key path are refused by the contract
itself. Like a repair decision it adds no job fields, duplicates no outcome, contains no
translation prose, and conveys no human review or approval.

`recovered_execution_kind` names what the authorized attempt will carry, and is also what the
failed attempt carried: a recovery never changes the kind of request it recovers. It defaults to
`translation`, so existing decision records stay valid unchanged. When it is `repair`, the
decision carries a `repair` block and is invalid without one — and invalid *with* one when the
kind is `translation`. The block records the repair root attempt (the QA-rejected translation the
repair answers, and therefore the resulting attempt's `repaired_from_attempt`), the failed repair
attempt, the auto-repair decision that spent the QA repair decision being replayed or `null` when
an operator drove it, the effective rejection's origin, file, and SHA-256, the blocking finding
codes that caused the repair, the rejected attempt's file, SHA-256, and translation hash, the
frozen source, context, prompt, style-guide, and terminology hashes, the repair prompt version,
the output contract, both provider-facing message digests, a digest binding the whole derivation
to its lineage, `request_reproduced_exactly`, and the repair decisions charged so far with the
allowance they were charged against. The contract refuses a lineage whose failed repair does not
follow its repair root, whose request was not reproduced exactly, or whose failed attempt number
disagrees with the decision's own. Every field is a hash, a path, an integer, or a finding code:
the repair request itself is derived at send time and never persisted here.

QA re-evaluation adds optional, backward-compatible job fields: `acceptance_origin`,
`reevaluation_count`, `latest_reevaluation_file`, and `latest_reevaluation_sha256`. The original
`acceptance_file` and `acceptance_sha256` continue to identify the historical worker assessment and
are never repointed. `acceptance_status` and `accepted_attempt` are the effective current mechanical
assessment; when `acceptance_origin` is `qa_reevaluation`, the latest re-evaluation sidecar is its
authority. Missing `acceptance_origin` on historical assessed jobs means `initial_qa`.

`TranslationQAReevaluationEvidence` is stored at
`jobs/<chunk>/reevaluations/<sequence>/evidence.yaml`. It records a content-derived re-evaluation ID,
monotonic sequence, current workflow/terminology/structural QA versions, original acceptance
path/hash, attempt/source/translation paths and hashes, snapshot-pinned terminology/prompt/style
paths and hashes, both QA reports, previous and resulting mechanical status, origin, and timestamp.
It contains no translation prose and conveys no human review or approval.

Accepted translation reuse adds optional job fields `reuse_count`, `latest_reuse_file`, and
`latest_reuse_sha256`. A reused job is queue `succeeded` (execution-complete), but retains
`attempt_count: 0`, no `successful_attempt`, and no provider/model attribution from reuse. Thus the
existing succeeded count represents provider-complete-equivalent queue progress, while provider
attempt metrics remain derived only from provider attempt artifacts. Acceptance counters include
current-QA acceptance of reuse; provider-succeeded-unassessed excludes reuse.

`TranslationReuseEvidence` is stored at `jobs/<chunk>/reuse/001/evidence.yaml` beside a private,
hash-identical `translation.md`. It records the historical run/job/attempt, source attempt and
translation hashes, historical acceptance and optional effective re-evaluation provenance, all
material compatibility values, and the new translation hash. It also records
`rendered_request_compatibility`, a `TranslationReuseCompatibility` block holding the compatibility
policy version, the tier, `provider_contacted: false`, both snapshot IDs, both prompt-package IDs,
both system- and user-message digests, the identity checks that were verified, the resource drift
that was waived, and the rationale. New-run acceptance evidence points to the reuse evidence as its
execution artifact, uses `acceptance_origin: reuse_qa`, and records the `terminology_qa_version` and
`structural_qa_version` that produced its findings. The block is optional so reuse evidence written
before the policy change still loads unchanged.
Historical rejected-output re-evaluation adds optional job fields `carry_forward_count`,
`latest_carry_forward_file`, and `latest_carry_forward_sha256`, and the acceptance origin
`historical_reevaluation`. A carried-forward job is queue `succeeded` with `attempt_count: 0`, no
`successful_attempt`, and no provider attribution, exactly like reuse; a job is completed by reuse or
by carry-forward, never by both. Historical jobs load unchanged because every field defaults to
absent.

`TranslationHistoricalReevaluationEvidence` is stored at
`jobs/<chunk>/carry-forward/001/evidence.yaml` beside a private, hash-identical `translation.md`.
Unlike `TranslationReuseEvidence` it records the historical *rejection*: `historical_status`,
`historical_terminology_report`, and `historical_structural_report` are copied from the effective
historical assessment (the latest QA re-evaluation when there was one, otherwise the original
acceptance), with paths and hashes for both. It also records the source run/job/attempt identity,
attempt and translation hashes, the historical provider, adapter version, response ID and returned
model, the new run's source, context, frozen terminology, prompt and style paths and hashes, the
current terminology and structural QA versions and reports, `resulting_status`, and the destination
translation hash, which must equal the historical one. `execution_kind` is fixed to
`historical_reevaluation` and `provider_contacted` to `false`.

From carry-forward engine 1.1.0: `historical_status` may be `accepted` (never with an
`exact_rendered_request` compatibility tier, which belongs to reuse); optional `alignment_qa_version`
and `alignment_report` are recorded together and `accepted` requires the alignment report to be valid
as well; the new-run acceptance sidecar is `jobs/<chunk>/carry-forward/001/acceptance.yaml`, published
in the same atomic directory. `HistoricalRequestCompatibility` gains optional
`source_manifest_sha256`, `target_manifest_sha256`, `source_series_policy_sha256`, and
`target_series_policy_sha256`; `GovernedTerminologyDrift` gains `lineage_kind` (`registry` default, or
`series_policy`, in which case the from/to identities are frozen policy file SHA-256s and each
transition is a `series_policy_amendment`) and `waived_term_changes` (series concept IDs). All
additions are omitted when absent, so engine 1.0.0 evidence loads and serializes unchanged. From
reuse engine 1.2.0 the reuse acceptance sidecar is `jobs/<chunk>/reuse/001/acceptance.yaml` and carries
alignment QA; engine 1.1.0 reuse keeps `acceptance/001/evidence.yaml`.

`SeriesPolicyAmendmentRecord` (`workspace/series-terminology-amendments/spa-<24hex>.yaml`, project
wide) is immutable evidence of one governed policy edit: `amendment_id`, `series_id`, `policy_file`,
the policy file SHA-256, semantic identity, and version before and after (the file hash must move),
the embedded `SeriesPolicyAmendmentRequest` and its canonical SHA-256, and one `SeriesTermChange` per
requested term in request order (status, target, and provenance before/after; aliases and authorized
forms added and removed). The ID is `spa-` plus the first 24 hex digits of the SHA-256 of the engine
version, the pre-amendment policy file hash, and the request hash.

Provider-free structural repair adds optional job fields `structural_repair_count`,
`latest_structural_repair_file`, and `latest_structural_repair_sha256`, and the acceptance origin
`structural_repair`. It behaves exactly like QA re-evaluation with respect to acceptance: the
original `acceptance_file`, `acceptance_sha256`, and `accepted_attempt` continue to identify the
historical worker assessment and are never repointed, while `acceptance_status` plus the latest
structural-repair record are the effective mechanical verdict. Because a delivered provider repair
replaces the draft the record was about, a successful repair attempt clears `acceptance_origin` and
the structural-repair pointer along with the acceptance fields it already cleared. Historical jobs
load unchanged because every field defaults to absent.

`TranslationStructuralRepairEvidence` is stored at
`jobs/<chunk>/structural-repair/NNN/repair.yaml` beside a private `translation.md` holding the
repaired bytes. It records the superseded evidence path, hash and origin; the provider attempt ID,
number, path and hash the bytes still descend from; the source path and hash; the original and the
repaired translation paths and hashes, which must differ; the frozen terminology path and hash; a
`StructuralRepairDiagnosis`; the blocking codes the superseded verdict carried; both QA reports
re-run over the repaired text; previous and resulting mechanical status; and `provider_contacted:
false` with `content_preserved: true`. The contract refuses a record whose diagnosis is not
deterministic, whose repaired structural report still shows a block-count mismatch, or whose blocking
codes are not a subset of the superseded ones.

`StructuralRepairDiagnosis` holds the engine version, the defect class
(`none`, `blank_line_geometry`, `translation_line_split`, `translation_line_merge`,
`line_count_divergence`, `content_divergence`), source and translation block counts and their delta,
source and translation line counts and their delta, whether a blank-line artefact is present,
whether a deterministic repair exists, the alignment cost and margin as integer thousandths so the
record round-trips byte-for-byte, the non-identity alignment groups, and the refusals. Identity
groups are deliberately not recorded: they would be as long as the chunk and would bury the
disagreement that matters.

`TranslationRepairEscalation` additionally records `authorized_attempt_number`, a `reason`, the
`structural_diagnosis` that showed no free remedy remained, `deterministic_repair_available` (which
must be false), and `provider_contacted: false`. `authorized_attempt_number` and `reason` are
required, which makes `repair_escalation_version` 1.1.0 a stored-data change rather than an additive
one; it needs no migration only because no escalation has ever been granted in any workspace. A
1.0.0 record would be refused rather than silently defaulted, which is the correct outcome for an
authorization that no longer states everything an authorization has to state.

Its `compatibility` block is a `HistoricalRequestCompatibility`: policy version, tier, both snapshot
IDs, both prompt-package IDs, both system- and user-message digests, both output contracts, both
terminology hashes, the verified identity checks, the changed framed sections, the observed resource
drift, the rationale, and — for the governed-drift tier — a `GovernedTerminologyDrift` holding the
two semantic registry identities and the contiguous chain of published governed transitions that
connects them, plus the (necessarily empty) list of locked or user-owned decisions that moved. Model
validators refuse to persist an ineligible tier, a changed system contract, a changed output
contract, an unexplained governed-drift lineage, any locked-decision drift, or a historical status
that is not `qa_rejected`.

# Terminology registry

The canonical current registry is `config/terminology.yaml`, validated by `TerminologyRegistry` and
content-addressed through stable sorted JSON encoded as UTF-8. It is generic across books and
languages.

- `TerminologyCandidate` stores discovery evidence: stable ID, source term, classification,
  confidence, lifecycle status, provenance, aliases, context-sensitivity, and prose-free occurrence
  locations.
- `TerminologyDecision` stores one linked canonical target choice, provenance, lock state, required
  status, competing historical variants, and contextual notes.
- `TerminologyValidationReport` and `TranslationQAReport` contain hashes, counts, and finding codes;
  they never contain or rewrite prose.

Candidate and decision IDs are deterministic hashes of language-qualified semantic identity. Registry
serialization sorts records by stable ID. A candidate and a canonical translation decision remain
distinct lifecycle records.

Candidate `aliases` are source-language discovery forms only. They carry no target-language meaning
and do not imply a translation of the alias or a shortened form of the decision's canonical target.
Historical registries retain this interpretation unchanged. A future bilingual alias contract would
require an explicit target-alias or source-to-target alias mapping rather than deriving target text
by slicing the canonical target.

A competing target variant must be a genuine alternative: it must not occur as a boundary-safe
lexical component or contiguous subsequence inside the canonical target. Explicit decision writes
reject such values, AI proposal validation removes them before application, and read-only QA ignores
them when loading historical registries so correct canonical use cannot report a competing variant.
Full alternative phrases absent from the canonical target remain enforceable.

`TerminologyDecision.allowed_target_forms` (and the matching `SeriesTerm.allowed_target_forms` in a
series policy, projected unchanged onto the derived decision) lists further target realizations that
a person or the project *explicitly authorized* for one decision — a participle beside a verbal noun
of the same governed concept, or a space-separated spelling the target normalization does not equate
with a ZWNJ spelling. `target_term` stays the preferred rendering; the forms are permitted grammatical
shapes of it, never competing translations, and nothing derives them from morphology. Each form is
non-empty, unpadded, unique, differs from the target, and is never also a competing variant; only a
decided (`LOCKED` or `STRONG_REFERENCE`) series term may carry one. Explicit decision writes also
refuse a competing variant occurring lexically inside an authorized form, as they already did for the
target. This is a compatible additive field of schema 1.0: an empty list is omitted from
serialization and from identity bytes, so every registry and policy written before it keeps its exact
bytes, hash, and identity, and a decision without forms behaves exactly as before. A reader older than
the field refuses a registry that carries one (`extra="forbid"`) rather than silently ignoring it.

`TranslationManifest.terminology` and corresponding snapshot/package fields are optional for backward
compatibility. Absence is omitted from historical version-1 identity bytes. Presence makes registry
hash and version material to new snapshot and prompt identities.
Every resource reference sets optional `frozen_file` to a content-addressed exact-byte copy. The
mutable `file` continues to identify current canonical lineage, while historical consumers prefer
`frozen_file` and verify the recorded SHA-256. Legacy references without it remain valid.
Snapshot identity validation dispatches from the validated field-present immutable manifest payload:
legacy version-1 payloads retain their original absent-field semantics, payloads containing
`terminology.frozen_file` use the frozen-terminology identity representation, and payloads whose
`prompt.frozen_file` is non-null use the frozen-resources representation and hash their payload as
written. Model defaults added by later application versions are not identity input for older
manifests.

`BookWorkspaceMetadata` is the canonical resolved book metadata contract persisted at
`workspace/metadata.yaml`. It carries `metadata_version` (the Book Metadata Resolution engine
version), a stable filesystem-safe `book_id`, English and Persian titles, author, source and target
languages, optional series name and volume, edition, publisher, publication year, and
checksum-validated ISBN, plus source PDF path, filename, hash, and page count. `title_fa` may be
blank, which means the human translation decision has not been made; every other identity field must
be resolved. `provenance` records exactly one entry per resolvable field, ordered by field name, with
a `BookMetadataProvenance` of `pdf_metadata`, `front_matter`, `configuration`, `external_research`, or
`unresolved` and a short evidence locator that never contains prose. A contract validator rejects a
record whose provenance disagrees with whether its value is actually resolved.

`BookIdentityMigration` is the immutable lineage of one governed identity migration, persisted at
`workspace/migrations/book-identity/bim-<24 hex>.yaml`. It records the engine version, the
`canonical_book_metadata` provenance with the metadata record's path and hash, both book identities,
and one `MigratedRegistry` per file: project-relative path, registry kind, before and after raw
SHA-256, the `semantic_sha256` that is invariant across the migration, the `book_id` occurrence count,
the changed-line count, and every `DerivedIdentityChange`. Contract validators refuse a record whose
identity did not move, whose registries are unordered or duplicated, or that claims semantic content
changed. `semantic_sha256` is computed over the canonical registry payload with book identity fields
removed — `book_id` for terminology and open questions, and `book_id` plus `anomaly_id` for source
anomalies, because an anomaly ID is a hash that includes the book ID.

`SnapshotBookReference` additionally carries `author`, `series_name`, `series_index`, and the
`metadata_file`, `metadata_sha256`, `metadata_version`, and `frozen_metadata_file` reference to that
record. All are optional so manifests published before resolved book metadata keep loading and keep
their original identity bytes; when present they are material to new snapshot identity.

`TerminologyDiscoveryResponse` and `TerminologyProposalResponse` are provider-facing strict structured
contracts. Reconciled discovery output uses `TerminologyCandidateOutput`, preserving deterministic IDs,
all occurrence evidence, aliases, context sensitivity, and classification conflicts. Proposals have
deterministic IDs derived from candidate, target term, and target language.
Validated proposal records add an action (`ai_proposal` or `reuse_existing`), canonical provenance,
separate canonicalization confidence, blocked state, and deterministic conflict codes. Defaults keep
historical version-1 proposal artifacts readable. The unified canonical view is derived and read-only;
it is not a new persisted registry or a legacy-glossary migration.
Future AI proposals store the style-normalized canonical value in `proposed_target` and, only when it
differs, the exact returned value in optional `provider_proposed_target`. Revised reviews follow the
same rule with `reviewed_target` and optional `provider_reviewed_target`. The analysis run records the
target-style adapter version; proposal and review request identities include it, and proposal identity
is calculated from the published canonical target. These optional additions preserve historical run
readability and do not rewrite any immutable artifact.

`TerminologyReviewResponse` contains one structured review per AI proposal: deterministic identity,
accept/revise/reject/defer disposition, optional reviewed target, overall and seven dimension scores,
reason codes, notes, and deterministic canonical-conflict state. Review run metadata adds proposal
lineage, canonical-view hash, and style-resource hash; optional fields preserve historical readers.

Canonical storage has three deliberately distinct provenance classes. AI-generated proposal/revision
targets receive deterministic target-style normalization before successful immutable publication.
Imported and legacy glossary targets remain authoritative exactly as stored. Explicit user decisions,
particularly `decision_source: user` with `locked: true`, are never style-normalized without explicit
user action.

Each immutable `TerminologyAnalysisRun` records stage, engine version, frozen snapshot and manifest
hash, selected source/context references and hashes, provider/model, request hash, output and report
paths/hashes, optional discovery-run lineage, registry hash for proposals, and timestamp. Associated
validation reports contain only counts, conflicts, warnings, and errors. Runs do not contain
translation attempts and never claim translation lifecycle state.
Provider-contacted validation failures are also immutable runs. They record `status: failed`,
`contacted: true`, a failure code/message, the exact safe structured output and its hash, and optional
`retry_of` lineage. This distinguishes a rejected response from a request that was never sent.

`TerminologyApplicationResult` records policy, threshold, counts, and precedence/conflict findings.
It is computed before the only mutable boundary: atomic replacement of the current terminology
registry. Proposals remain auditable even when manual policy accepts none.
# Source quality audit contracts

`SourceAuditFinding` records a stable `sqf-<24 hex>` identity, canonical chapter/chunk and page
coordinates, category, independent severity and confidence, likely source layer, concise structural
evidence, and suggested action. IDs hash the audit engine version and semantic coordinates/evidence,
not timestamps or absolute paths.

Every finding records `resolution_status` (`unresolved`, `resolved_by_normalization`,
`resolved_by_chunking`, `not_applicable_downstream`, or `cannot_evaluate`) and policy-derived
`blocking`. Resolution is deliberately excluded from finding identity so the same historical
suspicion remains addressable as downstream lineage changes. The report persists blocking,
resolved/unresolved-high-confidence, missing-layer, and translation-ready chapter counters.

`SourceAuditReport` records the scanned counters, ordered findings, and a `SourceAuditIdentity` over
the extraction, normalization/report, and chunk/report lineages plus their engine versions. The
content-derived directory name is `source-quality-<24 hex>`. Changing audit engine behavior changes
historical identity.
# Chapter title contracts

`ChapterTitleDocument` is the answer: engine version, artifact ID, book and language identity, the
snapshot and its manifest hash, the frozen terminology reference, and one `ChapterTitleRecord` per
chapter, plus a `content_sha256` over the produced `(chapter_id, title_en, title_fa)` triples. It
carries no timestamp, so the same titles always serialize to the same bytes. Its validator requires
contiguous ordered chapter sequences, unique chapter identities, and — independently of any
provider — that one English title maps to exactly one Persian title.

`ChapterTitleRecord` holds the chapter's identity, kind and number, both titles, both title hashes,
and a `ChapterTitleOrigin` of `provider_translation`, `deduplicated`, or `recorded_title`. Its
validator refuses a multi-line or untrimmed title and refuses a target title identical to its
source, so an untranslated heading cannot be persisted at all.

`ChapterTitleManifest` is the content-addressed index: the three engine versions, the artifact and
book identity, the source run and snapshot, the terminology hash and version, the per-origin title
counts, the provider call count with key-ordered provider and model tallies, the QA verdict, the
`content_sha256`, the file index with per-file hash and byte count, and the artifact's single
timestamp. Its validator requires every chapter to have exactly one origin and one provider call to
be spent per distinct translated title.

`ChapterTitleProvenanceRecord` is history, one per chapter: both titles and hashes, the origin, the
provider, adapter version and model, the provider response ID, the request digest, the snapshot and
terminology it was bound by, the ordered canonical decision IDs enforced, the per-chapter QA verdict
and finding codes, when it was translated, and the chapter a deduplicated title reused. No prompt
text and no raw provider response is stored. Its validator ties each field set to its origin: a
provider translation must name a provider and a time, a deduplicated title must name the chapter it
reused, and a recorded workspace title must name no provider at all. `ChapterTitleProvenance` wraps
those with the artifact identity, the full QA report, and the creation timestamp, and requires one
record per chapter in chapter order.

`ChapterTitleQAReport` is the provider-free verdict — `provider_contacted` is structurally `False` —
with the QA version, the checked chapter and terminology-decision counts, and ordered
`ChapterTitleQAFinding` entries. A finding names a chapter, a code, and optionally a terminology
candidate; it never carries a title's text. Validity is derived: valid is true exactly when no error
finding is present.

A chapter-title artifact under `workspace/resources/chapter-titles/<ct-id>/` is immutable. Unlike
the file-shaped frozen resources beside it, it is a directory addressed by the identity of its
*inputs* rather than by its own bytes, because a consumer must be able to look for it before
knowing the titles it holds. It is replaced only by an explicit `--force` execution whose manifest
already names the same artifact ID, and it is never an input to any translation stage.

# Release contracts

`ReleaseManifest` is the content-addressed index of one release directory. It records the release
and book identities, the run and snapshot it descends from with the snapshot's manifest hash, the
frozen book metadata and terminology references, the chapter-title artifact ID and content hash when
one supplied headings, the chapter/chunk/paragraph/word totals, one
`ReleaseChapterRecord` per chapter, one `ReleaseArtifactReference` per published file, a
`content_sha256` over the sorted `(path, sha256)` pairs, and the only timestamp in the release. Its
validator makes the index self-proving: chapter sequences are contiguous, the chunk and paragraph
totals equal the chapters' sums, the accepted count equals the chunk count, artifact paths are
unique and ordered, every chapter file appears in the artifact index with a matching hash and the
chapter role, exactly one book artifact and exactly one artifact per chapter exist, a metadata
or terminology reference exists if and only if its artifact does, and a chapter whose heading came
from a title artifact requires that artifact to be referenced.

`ReleaseChapterRecord` carries the chapter's frozen identity and titles, the heading actually used
with which title it came from, the released file and its hash, the chunk/paragraph/word counts, and
the `cleaned.md` file and hash the chapter descends from.

`ReleaseChunkLineage` is what a third party needs to re-verify one released chunk without the
session: chapter and chunk identity and order, the paragraph offset where the chunk starts in its
chapter file, the job, the frozen source file/hash/paragraph count and context hash, the released
translation file/hash/paragraph count, the acceptance status and origin with the effective evidence
file and hash, the attempt identity/file/hash, the originating provider and model with a
`ReleaseProviderResolution` saying whether it came from this run's attempt, a carry-forward source,
a reuse source, or could not be established, the provider attempts spent, the recorded QA versions,
and the cross-run lineage identities when there are any. It holds no prose and no prompt or response
material: translated text is named and hashed, never quoted. Its validator refuses anything but an
accepted status, requires an unresolved provider to be marked unresolved, and requires a lineage
source run and job together.

`ReleaseLineageDocument` wraps those records with the release, book, run, and snapshot identities and
requires one record per chunk, unique, in chapter and chunk order. It is serialized as deterministic
UTF-8 JSON in declared field order.

`ReleaseTranslationReport` is counters only: the run's identity, engine version and status, the
chapter and chunk totals, provider attempts and abandoned slots, provider repairs, provider
recoveries, repair escalations, structural repairs, QA re-evaluations, cross-run reuses, historical
carry-forwards, key-ordered acceptance-origin/provider/model tallies, the sorted distinct terminology
and structural QA versions, the frozen resource versions, and a `final_status` of `released`. Its
validator requires full acceptance, requires the acceptance origins to account for every chunk, and
requires every counter map and version inventory to be deterministically ordered.

Releases are derived, not canonical. A release directory is written only by release assembly, is
replaced only by a `--force` export whose manifest already names the same release, and is never an
input to any translation stage. Because every byte except the manifest timestamp is a pure function
of the frozen snapshot and the accepted translations, re-exporting the same run reproduces the same
`content_sha256`.

# Release verification contracts

`ReleaseVerificationReport` is the complete, re-derivable verdict on one release directory: the
verification engine version, the release identity and project-relative path, the assembly engine
version, run, and snapshot the release names, the manifest hash and the release's own content
address, the book, terminology, and title-artifact digests, per-dimension verified/expected counts
for files, chapters, chunks, and titles, whether the canonical workspace was reachable, one
`ReleaseVerificationCheck` per `ReleaseCheck` in canonical order, every
`ReleaseVerificationFinding`, the failed and skipped check lists, the status, a `content_sha256`
over everything except the timestamp, and the timestamp.

Its validator makes the verdict follow the evidence rather than sit beside it: every check appears
exactly once in `RELEASE_CHECK_ORDER`, the failed and skipped lists are derived from the checks
themselves, each check lists exactly its own findings' codes, and the release is
`READY_FOR_PUBLICATION` exactly when no check failed and no error finding stands.

`ReleaseVerificationCheck` records one check's status, what it examined against what it expected,
a one-line statement of what it proves, and its ordered distinct finding codes. A `PASS` is refused
unless the check examined everything it expected, so a check can never report success over evidence
it never looked at. `ReleaseCheckStatus.SKIPPED` means the evidence was unreachable — it is never a
quiet pass, and it appears as skipped everywhere it is reported.

`ReleaseVerificationFinding` names a check, a snake_case code, a severity, an optional subject
(a file, chapter, or chunk), and a message. It never carries book prose.

A verification report is derived and disposable. It is never an input to any stage, and it may not
be written inside the release it judges: a release is content-addressed by its own manifest, so a
report dropped into it would fail the next verification as an unindexed file.

# Final QA resolution contracts

`FinalQAAuditReport` is the read-only diagnosis of one completed run: the resolution policy and QA
engine versions it was produced under, the frozen terminology resource and the effective registry
with their digests and whether the two still agree, the run's job/accepted/rejected counts, verdict
and competing-variant-class tallies derived from the diagnoses, and one `FinalQAJobDiagnosis` per
`qa_rejected` job. The counts are validated against the diagnoses rather than stored beside them.

`FinalQAJobDiagnosis` carries the effective attempt and acceptance origin, the evidence, attempt,
source, and translation digests, the recorded QA engine versions against the installed ones, the
historical and current blocking codes with whether the rejection still reproduces, the classified
findings, the competing-variant records, the structural diagnosis, the budget, and the verdict with
its reasons. Its validator ties the verdict to its own evidence: `accepts_without_provider` must
follow from the verdict, a job with no current blocking finding cannot require intervention, and a
job with one is not provider-free. The one exemption is `provider_recovery_required`, which marks a
job whose repair the provider never delivered — recovery is a different boundary's decision.

`FinalQACompetingVariantRecord` is the evidence behind one classification: the decision and its
state, ownership, enforcement and lock, the candidate's provenance, whether canonical and variant
appear in the translation, source and target occurrence counts, whether the effective registry still
records the rivalry, any rival decision claiming the same target, and whether current QA still
reproduces the finding. Terminology targets and variants are registry data, not book prose; source
and translated text appear only as counts and digests.

`FinalQAFinalizationReport` records one execution: dry-run flag, provider, cycle bound, escalation
authorization, the audit it acted on, a `FinalQAJobOutcome` per job, the resulting counters and run
status, and the total provider calls. Its validator makes a dry run structurally incapable of
claiming an action — no provider calls, no mutating action, no spent attempts.

`TranslationRepairEscalation` is the bounded exceptional grant of extra provider attempts to one
job. It records the original ceiling, the attempts granted, the resulting ceiling, the budget
already spent, the blocking findings it is spent on, and the rejection evidence it answers. Its
validator requires the resulting ceiling to be exactly the original plus the grant, the budget to
have actually been exhausted, and the grant to name what it is for. Counters are never rewritten;
only the ceiling moves, and the queue refuses a ceiling that no persisted escalation accounts for.

# Persian literary editing contracts

`PersianLiteraryStyleContract` is the generic versioned Persian prose contract. Its separate rule
groups cover literary naturalness, dialogue register, punctuation and Persian quotation, orthography
and ZWNJ, English syntax avoidance, narrator consistency, names, terminology flexibility, inner
monologue, emphasis, and prohibitions. `BookLiteraryProfile` is a separately frozen book-scoped
overlay for prose character and chapter guidance; the generic contract contains no novel-specific
character or plot fact.

`LiteraryEditorResponse` is strict provider output with exactly one `LiteraryEditedParagraph` per
requested identity in requested order. Each paragraph carries text, a changed flag, an optional
closed edit-reason list, and an engine-authored flag when contradictory change metadata was
normalized from byte equality. A `GovernedMultiParagraphEditRequest` names restructuring the editor refused
to do silently. `LiteraryCorrectionResponse` uses the same paragraph payload but can cover only the
semantic-failed identity set. Neither response has a fidelity verdict or free-form rationale.

`ContinuityReviewResponse` contains bounded `ContinuityFinding` values only. A finding has a closed
code, warning/advisory severity, up to four paragraph identities, and a short observation. The schema
contains no replacement-text field and therefore cannot act as an editor.

`LiterarySemanticEvidence` records one independent post-edit Semantic Coverage Judge call, its
prose-free provider provenance, candidate-chapter hash, correction round and look type, and one
`LiterarySemanticParagraphEvidence` per judged identity with exact source/candidate hashes and policy
outcome. `LiteraryEvidenceReference` links either immutable pre-literary PASS evidence or this new
evidence by path and digest.

`LiteraryParagraphRecord` binds chapter-global and original chunk-local identities to source,
faithful, and final hashes; change and attempted-change state; reasons and diff class; character and
word deltas; correction count; fallback disposition; and semantic evidence. Its validator requires
every retained change to be a fresh PASS, every non-change to be byte-identical, and every fallback to
restore faithful bytes and name the fallback.

`LiteraryChapterArtifact` is the prose-free governance record beside private `chapter.md`. It carries
changed identities and percentage, edit breadth, unchanged hashes, terminology differences,
mechanical reports, semantic regression/correction/fallback counts, continuity findings, provider
calls, and `v1_published`, `v2_fidelity_only`, and `v2_literary_edited` layer references where V1 is
available. A carried chapter records the source literary run and source artifact hash, and each
carried provider call records the same provenance. `LiteraryRunArtifact` aggregates the selected
chapters and separates calls, tokens,
latency, returned model identities, change rate, semantic-regression rate, and fallback rate by role.

`BlindComparisonArtifact` is a prose-free manifest for the human-facing Version A/Version B document.
It identifies passages and version hashes without revealing origin; `BlindComparisonAnswerKey` is a
separate hashed mapping. Human scores are not a canonical pipeline state.
