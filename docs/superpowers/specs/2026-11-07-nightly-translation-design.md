# Bounded Nightly Translation Design

## Scope

Prepare one immutable translation snapshot and one canonical queue run for an EPUB-backed book,
render every prompt provider-free, and provide a resumable coordinator without contacting a
provider during setup.

## Decisions

- Snapshot book lineage supports either legacy PDF fields or generic source-document fields.
  Existing PDF manifests remain byte-compatible; EPUB manifests use `source_document`,
  `source_document_sha256`, and `source_format: epub`.
- The nightly coordinator owns no translation contract. It locks one existing run, derives
  coverage from validated attempt artifacts, and invokes the existing worker for one pending job
  at a time with concurrency one, automatic repair disabled, semantic judging absent, and
  provider-failure retries disabled.
- Units and chunks follow snapshot order. A unit consumes the nightly budget when its first
  provider job begins in that invocation. No more than three distinct units can be touched.
- A usable attempt counts as translated regardless of `accepted` versus `qa_rejected`. Failed
  gaps do not block later pending chunks; they are revisited only after the first coverage pass.
- The allowed window uses the machine-local aware clock. A request is never started outside
  22:00 inclusive through 05:00 exclusive. The process exits after one invocation; it is safe for
  an external daily scheduler and never sleeps across days.
- A prose-free, atomically written checkpoint records IDs, aggregate counts, ordered progress,
  sanitized provider state, and the next safe action. A run-scoped lock prevents two nightly
  coordinators.
- At complete usable coverage, the coordinator assembles a readable unreviewed draft from all
  usable attempts and stops. The verified-release EPUB builder is intentionally not used because
  it requires accepted jobs and would falsely confer release status on rejected translations.

## Failure policy

Rate/quota/reset and transient failures are classified without persisting provider text. An
explicit retry time before window close may be waited for once; otherwise bounded backoff is
clipped to the window. The failed chunk remains recorded and the coordinator continues with later
claimable chunks after the wait. No same-chunk automatic retry or repair occurs during coverage.

## Safety

The setup path renders prompts and queue state locally only. Provider construction/execution is
reachable exclusively from the explicit nightly command and only after the lock, checkpoint, and
window checks succeed.
