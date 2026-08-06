# Recovery, resumability, and idempotency

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
