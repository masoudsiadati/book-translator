# book-translator

`book-translator` is a local Python project intended to automate faithful English-to-Persian translation of fantasy novels from text-based PDFs and eventually produce valid RTL EPUB 3 books.

## Current scope

This version provides typed configuration and progress models, PDF inspection, embedded TOC discovery,
page extraction, immutable chapter extraction, deterministic chapter normalization, generic document
chunking, immutable translation snapshots, a provider-independent queue and prompt renderer, and
single-job provider execution, mechanical and semantic translation gates, immutable release assembly,
deterministic RTL EPUB generation, and a chapter-level Persian literary editing pilot layer. Literary
output is not implicitly approved or published. No OCR is performed.

## Architecture

The future pipeline uses `workspace/` as its canonical, hash-tracked representation. Start with
[the architecture contract](ARCHITECTURE.md), then consult the [data model](docs/data-model.md),
[pipeline and lifecycle](docs/pipeline.md), [recovery rules](docs/recovery.md), and
[naming conventions](docs/naming-conventions.md). Pydantic models and synchronized JSON Schemas
form the persisted data contract; these definitions do not imply that future stages exist yet.

## Setup and installation

Python 3.12 or newer is required.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

## Commands

Show the current project status from `config/book.yaml` and `state/progress.json`:

```bash
book-translator status
```

Resolve canonical book identity from local source evidence into `workspace/metadata.yaml`:

```bash
book-translator resolve-book-metadata
book-translator resolve-book-metadata --apply
book-translator resolve-book-metadata --apply --set isbn=9780312854270 --provenance external-research
```

The command is a dry run unless `--apply` is supplied, and an unchanged resolution is a `skip` that
leaves the record byte-identical. It reads embedded PDF document information plus short front and
back matter lines to resolve the English title, author, series name and volume, edition, publisher,
publication year, and a checksum-validated ISBN; prose is never read, printed, or persisted. Local
source evidence outranks `config/book.yaml`, and a placeholder configuration value is treated as
absent. Every field records its provenance (`pdf_metadata`, `front_matter`, `configuration`,
`external_research`, or `unresolved`) with a short evidence locator. An unavailable fact stays
unresolved rather than being guessed, including the Persian title, which remains a human decision.
Repeatable `--set FIELD=VALUE` records an explicit operator assertion under `--provenance`.

Propagate that identity to the current book-scoped registries through a governed migration:

```bash
book-translator migrate-book-identity
book-translator migrate-book-identity --apply
```

The command is a dry run unless `--apply` is supplied and reports `skip` once every current registry
already agrees. It rewrites only the addressed `book_id` scalar lines — indentation, quoting, folded
blocks, and every other byte are preserved — and refuses unless the parsed payload with identity
fields removed hashes identically before and after. A book-scoped derived identifier is migrated with
it, because `anomaly_identity` hashes the book ID and a stale `anomaly_id` would contradict its own
derivation rule. Each migration publishes immutable lineage under
`workspace/migrations/book-identity/`. Frozen historical resources and snapshots keep the earlier
identity as evidence and are never migrated.

Publish durable copies of the current mutable translation inputs without creating a snapshot:

```bash
book-translator freeze-translation-resources
book-translator freeze-translation-resources --apply
```

Inspect the configured PDF, or an explicitly supplied PDF, without writing its text:

```bash
book-translator inspect-pdf
book-translator inspect-pdf input/another-book.pdf
```

Every command below that reads or writes the canonical workspace accepts `--book <book-id>`
(`inspect-pdf`, `extract-pages`, `discover-toc`, `extract-chapters`, `normalize-chapters`,
`chunk-chapters`, `audit-source-quality`, `show-source-quality-finding`, `inspect-artifact-staleness`,
`inspect-normalization-changes`, `regenerate-artifacts`, `resolve-book-metadata`,
`freeze-translation-resources`, `create-translation-snapshot`, `validate-translation-snapshot`,
`list-translation-snapshots`, `create-translation-run`, `status`). Without it a command uses the legacy first-book workspace
`workspace/` and `config/book.yaml` exactly as before; with it the same layout is resolved under
`workspace/books/<book-id>/` with `config/books/<book-id>.yaml`, so a second book never touches the
first book's artifacts:

```bash
book-translator discover-toc input/book6.pdf --book lord-of-chaos
book-translator extract-chapters input/book6.pdf --book lord-of-chaos --dry-run
book-translator normalize-chapters --book lord-of-chaos --dry-run
```

A second book of a series names its terminology lineage explicitly in `config/books/<id>.yaml`
(`series_terminology_policy` and `reference_corpus`, configured together, or `terminology` for a
standalone registry); a book-scoped workspace never falls back to the root
`config/terminology.yaml`. Build the series policy from explicit decisions and the pinned reference
corpora, derive the book's own registry, retrieve one immutable reference package per chunk, inspect
a chunk's effective context, and prove the lineage before freezing (all read-only without `--apply`):

```bash
book-translator import-series-terminology-policy --book lord-of-chaos \
  --explicit-registry config/terminology.yaml --glossary config/glossary.yaml \
  --prior-volume-registry config/terminology.yaml --apply
book-translator set-series-terminology "Ajah" "آجای" --type organization \
  --rationale "user decision" --book lord-of-chaos
book-translator derive-book-terminology --book lord-of-chaos --apply
book-translator prepare-reference-context --book lord-of-chaos --apply
book-translator prepare-reference-context --book lord-of-chaos --chapter 13 --force --apply
book-translator show-terminology-context 013-002 --book lord-of-chaos --output /tmp/013-002.txt
book-translator validate-series-terminology --book lord-of-chaos
```

`show-terminology-context` prints counts and term-level decisions only; the rendered section,
which quotes reference prose, is written to `--output`.

Discover and normalize the configured PDF's embedded TOC to canonical `workspace/toc.yaml`:

```bash
book-translator discover-toc
book-translator discover-toc input/another-book.pdf --output workspace/toc.yaml
book-translator discover-toc --dry-run
book-translator discover-toc --force
```

Existing output is protected unless `--force` is supplied. A dry run never writes, including
when combined with `--force`. This command uses only the embedded PDF TOC; page-text fallback
discovery is not implemented.

Extract an inclusive, 1-based page range. Existing output is protected unless `--force` is supplied:

```bash
book-translator extract-pages input/book.pdf \
  --from-page 10 --to-page 25 --output extracted/pages-010-025.txt

book-translator extract-pages --from-page 10 --to-page 25 \
  --output extracted/pages-010-025.txt --force
```

Extract immutable chapter sources from the canonical TOC. By default this selects the narrative
corpus: the prologue, the numbered chapters, and the epilogue (`000-prologue/`, `NNN/`,
`999-epilogue/`). Front matter requires an explicit option; appendices such as a glossary and
unknown trailing entries such as an author note are never extracted:

```bash
book-translator extract-chapters --dry-run
book-translator extract-chapters input/book.pdf
book-translator extract-chapters --chapter prologue
book-translator extract-chapters --chapter chapter-042
book-translator extract-chapters --chapter epilogue
book-translator extract-chapters --include-front-matter
```

Each selected entry creates only canonical `metadata.yaml` and `source.md` files. A valid existing
pair is skipped. `--force` can replace source extraction only when no cleaned, chunk, translation,
review, or final artifact exists. It never invalidates downstream work.

Normalize extracted chapters locally and deterministically:

```bash
book-translator normalize-chapters --dry-run
book-translator normalize-chapters --chapter prologue
book-translator normalize-chapters --chapter 042 --show-diff-summary
book-translator normalize-chapters --chapter chapter-042 --force
```

Normalization never calls an AI model or external service. It removes canonical page markers while
reporting transitions, repairs boundary hyphens, joins wrapped prose only inside one extracted block,
conservatively repairs structurally continuous page-boundary blocks, and renders canonical paragraph
spacing. Interrupted dialogue followed by a new speaker remains separate; delayed interruption
punctuation and clearly continuous narrative dash clauses may join. It does not remove headers or footers, infer headings,
dehyphenate words, or rewrite semantics. `source.md` stays immutable and `cleaned.md` is derived.

Plan or publish generic chunks from normalized chapters. Defaults come from `config/book.yaml`:

```bash
book-translator chunk-chapters --chapter prologue --dry-run --show-plan
book-translator chunk-chapters --chapter 042
book-translator chunk-chapters --target-words 1800 --max-words 2400 --context-words 250
book-translator chunk-chapters --chapter chapter-042 --force
```

The engine preserves complete Markdown paragraphs, prefers explicit scene separators near the target,
warns instead of splitting an oversize paragraph, and proves byte-exact reconstruction before writes.
Each chunk contains only `source.md`, deterministic preceding `context.md`, and metadata. No
translation file is created. Forced regeneration refuses translation, review, and final artifacts.

Inspect and explicitly regenerate stale deterministic mutable artifacts without copying the project:

```bash
book-translator inspect-artifact-staleness --chapter prologue --chapter 1
book-translator regenerate-artifacts --chapter prologue --chapter 1
book-translator regenerate-artifacts --chapter prologue --chapter 1 --apply
```

The regeneration command is read-only unless `--apply` is present. It may replace only normalized and
chunk artifacts plus their mutable metadata/reports. Source extraction and every existing snapshot,
run, prompt package, attempt, response, and translation remain untouched. Resource-only changes skip
mutable regeneration and require a new snapshot.

Freeze future translation inputs without sending or copying book content:

```bash
book-translator create-translation-snapshot --chapter prologue --chapter 1 --dry-run
book-translator create-translation-snapshot --chapter prologue --chapter 1 --label pilot
book-translator validate-translation-snapshot translation-fa-0123456789abcdef01234567
book-translator validate-translation-snapshot translation-fa-0123456789abcdef01234567 \
  --require-current
book-translator list-translation-snapshots
```

Omitting `--chapter` requires every TOC-derived prologue and numbered chapter to be validly chunked.
Repeatable selectors create a canonical-order subset. Frozen manifests reference hashes and relative
paths instead of copying chunk text; provider and model selection is intentionally deferred.

Every mutable translation input is copied byte-for-byte to
`workspace/resources/<category>/<sha256><suffix>`: the translation prompt, glossary, style guide,
terminology registry, Source Anomaly Registry, Open Question Registry, and the resolved book metadata
record. Creation is refused when a referenced input has no durable frozen artifact, and validation
verifies the frozen bytes rather than the current config file, so editing a config file afterwards
makes a snapshot non-current for new work without making it unresolvable. Resolved book metadata
participates in snapshot identity, and a full-book snapshot refuses placeholder book identity — run
`resolve-book-metadata --apply` first.

Create and inspect a provider-independent translation queue:

```bash
book-translator create-translation-run translation-fa-0123456789abcdef01234567 --dry-run --show-plan
book-translator list-translation-runs
book-translator show-translation-run tr-0123456789ab-001
book-translator list-translation-jobs tr-0123456789ab-001 --status pending
book-translator claim-translation-job tr-0123456789ab-001 --worker-id local-worker
book-translator release-translation-job tr-0123456789ab-001 JOB_ID --worker-id local-worker
book-translator recover-translation-run tr-0123456789ab-001 --dry-run
book-translator create-translation-run SNAPSHOT_ID --no-paragraph-markers
book-translator create-translation-run SNAPSHOT_ID --book lord-of-chaos --dry-run --show-plan
```

A bare snapshot ID is looked up in exactly one workspace: the legacy root, or the book workspace
named by `--book`. No other workspace is searched, so an ID never resolves ambiguously and a
book-scoped snapshot without its `--book` is refused rather than found elsewhere. An explicit
snapshot path bypasses the lookup and is validated against the workspace its frozen manifest
records; `--book` may confirm that workspace but never overrides it. `--book` also selects the
`config/books/<book-id>.yaml` whose `paragraph_marker_contract` supplies the default contract. Runs
themselves are stored under the project-level `workspace/translation-runs/` for every book; the
run's `snapshot_path` names the workspace it belongs to.

A run freezes its output contract when it is created. New runs default to the numbered paragraph
contract, in which every source paragraph is sent under a `[[Pnnnn]]` marker and the provider must
return every marker exactly once and in order; `--no-paragraph-markers` selects the original
unmarked prose contract instead, and the default comes from `paragraph_marker_contract` in
`config/book.yaml`. Because the choice lives on the run, a run created before the contract existed
keeps rendering, executing, and reusing exactly as it always did.

Resolve an incomplete execution reservation that automatic recovery refuses, and inspect reservation
lineage (both are read-only until `--apply`):

```bash
book-translator inspect-translation-reservations tr-0123456789ab-001
book-translator resolve-translation-reservation tr-0123456789ab-001 JOB_ID \
  --attempt-number 1 --resolution provider_not_contacted --reason "why you know this"
book-translator resolve-translation-reservation tr-0123456789ab-001 JOB_ID \
  --attempt-number 1 --resolution provider_not_contacted --reason "why you know this" --apply
```

`--resolution` is one of `provider_not_contacted` (no inference started: the attempt slot is
abandoned rather than spent and the job returns to `pending`), `provider_contacted_no_result` (the
request may have run: the attempt is spent, the job fails, and only an explicit
`retry-translation-job` can schedule another), or `provider_contact_unknown` (delivery unknowable:
the attempt is spent and the job is blocked with no queue replay path). The reservation is preserved
byte-for-byte, the resolution is written once as immutable lineage, repeating it is idempotent, and a
conflicting second resolution is refused. `--reason` is audit metadata and must not contain book
prose.

The queue stores references and hashes only. Claims consume an attempt slot and use a local advisory
lock and expiring lease. `attempt_count` counts issued slots; the provider budget compared against
`max_attempts` is `attempt_count` minus explicitly abandoned slots. It performs no provider call or translation, and queue progress never implies
review or approval. Future provider execution owns request/response artifacts and result transitions.

Render and validate provider-ready prompt inputs without claiming or executing jobs:

```bash
book-translator render-prompt-packages RUN_ID --dry-run --show-plan
book-translator render-prompt-packages RUN_ID --job-id JOB_ID
book-translator validate-prompt-package RUN_ID JOB_ID
book-translator list-prompt-packages RUN_ID
```

Packages keep system and user messages separate, conserve source/context bytes, and freeze resource
hashes. They contain sensitive source material, are ignored by Git, and are not translations.

Execute one explicitly claimed job through an allowed provider:

```bash
export OPENAI_API_KEY="..."  # never commit this value
book-translator execute-translation-job RUN_ID JOB_ID \
  --worker-id WORKER --claim-token TOKEN --provider openai --model gpt-5 --dry-run
book-translator execute-translation-job RUN_ID JOB_ID \
  --worker-id WORKER --claim-token TOKEN --provider codex-cli --dry-run
book-translator execute-translation-job RUN_ID JOB_ID \
  --worker-id WORKER --claim-token TOKEN --provider claude-code --dry-run
book-translator list-translation-attempts RUN_ID JOB_ID
book-translator validate-translation-attempt RUN_ID JOB_ID 1
```

The OpenAI adapter uses the official Responses API with separate system/user roles, no tools or
retrieval, `store: false`, and zero SDK retries. The Codex CLI adapter uses the existing authenticated
CLI session, enforces `--sandbox read-only`, captures `--output-last-message`, and sends no API-key
environment variables. Its model is optional: omit `--model` to retain the Codex configuration's
selection, or configure an allowed model under `translation_provider_models.codex-cli` and pass it
explicitly. Remove `--dry-run` only for a deliberately authorized one-job pilot. Provider success
creates an unreviewed draft, never approval. See the [privacy guidance](docs/privacy.md).

The Codex CLI has its own preflight. Log in once, then check readiness:

```bash
codex login                # once, interactively (ChatGPT login)
codex login status         # must report an active login
book-translator check-translation-provider --provider codex-cli
```

`check-translation-provider --provider codex-cli` confirms the executable exists and reports its
version, that `codex exec --help` still documents every capability the adapter's command line uses
(`--sandbox read-only`, `--ephemeral`, `--color never`, `--output-last-message`, `--cd`,
`--skip-git-repo-check`, `--model`, and the stdin prompt), and that `codex login status` reports an
active login. Capabilities are detected from the installed CLI rather than pinned to a version, so
a newer compatible Codex passes on what it documents. The login kind is reported as `chatgpt` when
the CLI states it and `unknown` otherwise; the subscription plan is always `unknown` because the CLI
does not expose it without reading its credential store, which the preflight never does. An API-key
login is refused: the adapter uses the CLI's own ChatGPT session and forwards no `*_API_KEY`
variable. The preflight sends no book content, makes no model call, and claims nothing.

### Claude Code provider

`claude-code` is a local-CLI provider, not the Anthropic API. It never reads `ANTHROPIC_API_KEY`;
it drives the `claude` executable you already installed and logged into, so the work is billed and
rate-limited through that login rather than through an API account. Install and authenticate first:

```bash
claude auth login          # once, interactively
claude auth status --json  # must report "loggedIn": true
book-translator check-translation-provider --provider claude-code
```

`check-translation-provider` is the preflight on its own: it confirms the executable exists, that the
installed CLI still documents every non-interactive flag the adapter uses, and that the login is
usable. It sends no book content and makes no model call. The worker and
`execute-translation-job`/`repair-translation-job` run the same preflight before claiming a job, so an
uninstalled or logged-out CLI fails without consuming an attempt or writing an ambiguous attempt.

Its model is optional and governed by the book's `translation_provider_models.claude-code` list.
Omit `--model` to keep the model the authenticated session already selects, or list an alias
explicitly and pass it, for example `claude-code: ["opus"]` and `--model opus`; the alias is
forwarded to `claude --model` exactly as written and the model actually used is recorded from the
result. An empty list allows only the omitted model, never any explicit one.

```bash
book-translator run-translation-worker RUN_ID --provider claude-code --max-jobs 1 --dry-run
book-translator run-translation-worker RUN_ID --provider claude-code --job-id JOB_ID --max-jobs 1
```

Each job runs one non-interactive `claude --print --output-format stream-json --verbose` process
in a private temporary directory with `--tools ""`, `--disable-slash-commands`,
`--strict-mcp-config`, `--safe-mode`, and `--no-session-persistence`. Claude receives only the
rendered system prompt (as a private `--system-prompt-file`) and the rendered user message (on
standard input); it has no tools, no repository access, no MCP servers, and no session history, so
translation is pure text in and text out. The adapter reads the whole session transcript and joins
the text blocks of every primary assistant message, in order, into the one candidate that becomes
`translation.md` (adapter 1.3.0). It does not read the JSON `result` field as the answer: that field
holds only the last assistant message, and Claude Code continues a response that hit its output
token limit in a second assistant turn, which used to lose the first turn entirely. Thinking blocks,
tool blocks, user and system events, subagent output, and streaming deltas are never part of the
candidate; a transcript whose reported turn count cannot be reconstructed from the primary
assistant messages, or whose `result` text contradicts the final message, fails closed as a
retryable malformed response rather than publishing a partial translation. When the reconstructed
text arrives whole but is refused by structural validation (for example a numbered-paragraph
violation such as `marker_missing` or `text_before_first_marker`), the attempt fails and the exact
refused text is kept privately as `attempts/NNN/rejected-output.md`, recorded on `attempt.yaml` as
`rejected_output_file` / `rejected_output_sha256` together with prose-free `marker_findings`; it is
a diagnostic, never a translation, and `list-translation-attempts` shows which attempts carry one.

Option behavior: `--timeout-seconds` is enforced by the adapter as a process timeout;
`--max-output-tokens` is exported as `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, which Claude Code applies as
the per-request `max_tokens` (thinking tokens included, capped at the model's ceiling). A response
that exhausts it is continued by Claude Code in a further assistant turn, which the adapter
reconstructs losslessly and reports as `num_turns` beside `assistant_message_count` in
`response.json`; an overrun the CLI itself gives up on is recorded as a retryable truncation rather
than a silent partial translation. `--model` is optional —
omit it to keep the login's own model selection, or pass an alias or full model name. There is no
`--reasoning-effort` support for this provider. The returned model, its canonical name, the CLI
version, and the authentication method are recorded on every attempt.

When the CLI exits unsuccessfully, the attempt records what the CLI reported — its result `subtype`,
any API error status, and a short error message — rather than only the exit code, which is what
distinguishes a transient rate-limit or server failure from a broken invocation and decides whether
the worker retries automatically. The retained diagnostic is redacted (emails, URLs, paths, secrets,
session identifiers, tokens) and capped, and translated prose is never retained; see
`docs/privacy.md`.

Process claims, provider execution, and mechanical QA acceptance sequentially without handling claim
tokens yourself:

```bash
book-translator run-translation-worker RUN_ID --provider codex-cli --max-jobs 1 --dry-run
book-translator run-translation-worker RUN_ID --provider codex-cli --max-jobs 5
book-translator run-translation-worker RUN_ID --provider codex-cli --job-id JOB_ID --max-jobs 1
book-translator run-translation-worker RUN_ID --provider codex-cli --chapter 007 --max-jobs 1
```

Run several execution slots at once with `--workers N` (default 1, which is the sequential worker
exactly as before):

```bash
book-translator run-translation-worker RUN_ID --provider claude-code --model opus \
  --workers 4 --max-jobs 226 --no-retry-provider-failures --stop-on-error
```

One coordinator process does the once-per-pass work — run and book resolution, acceptance and
selection validation, the provider/model policy check, the provider preflight, and queue recovery
— and then starts N separate processes, each with its own worker identity (`<worker-id>-1`,
`<worker-id>-2`, …). Each slot claims the next eligible job through the queue's own lock, so two
slots can never own the same job; executes exactly that job under the ordinary claim, token, and
lease checks; finalizes the attempt; applies the same terminology, structural, and alignment QA;
and returns a compact result. The coordinator prints one line per finished job
(`[worker-2] <job-id> accepted`) and a running total (`Workers`, `Active`, `Completed n / planned`,
`Accepted`, `QA rejected`, `Failed`, `Errors`), which is the cheap way to watch a pass; polling
`show-translation-run` re-parses the whole run each time and is not needed.

`--max-jobs` stays a **global** limit across all slots: `--workers 4 --max-jobs 226` processes at
most 226 jobs in total, never 226 per slot. Slots never exceed the selected work (`--workers 8` on
two remaining jobs uses two), an explicit `--job-id` always runs alone, and `--dry-run` starts no
process. `--stop-on-error` stops dispatching new jobs after the first `error` outcome and lets jobs
already executing finish and finalize; nothing is killed mid-call. Ctrl+C or SIGTERM to the
coordinator does the same: no new dispatch, in-flight jobs finish, then the pass ends and reports
what completed. Slots run in their own session, so Ctrl+C never reaches an in-flight provider
request; a second Ctrl+C interrupts hard, kills the slots with their provider processes, and may
leave a `requesting` reservation that needs `resolve-translation-reservation`. A slot that dies is reported as an `error` row naming the job it
held; its claim is left untouched for `recover-translation-run`, no other slot re-executes it, and
no further work is dispatched. `--auto-recover-provider-failures` is refused with more than one
worker because it re-runs global queue recovery per job, which cannot tell a live concurrent
execution from a crashed one.

Start with 2–4 workers. Provider- and account-side concurrency or rate limits usually bind before
CPU does, throughput does not scale linearly with real Claude, and each slot spends about a second
of CPU per queue read on a 226-job run.

Selecting a provider whose readiness can be checked locally (`claude-code` and `codex-cli`) runs
its preflight before recovery, claiming, or any other queue mutation, so an unusable provider stops
the worker without consuming an attempt. The requested provider/model is checked against the run's
own book configuration even earlier — before the preflight, and in `--dry-run` too — so a
disallowed `--model` stops the worker with the reason and consumes nothing. Runs are stored
project-wide, so the worker, `execute-translation-job`, `repair-translation-job`, and
`finalize-translation-run` resolve that configuration from the run's frozen lineage: a legacy-root
run reads `config/book.yaml` and a book-scoped run reads `config/books/<book-id>.yaml`.

Per-job errors are never a bare count: each is printed as `Error <job-id>: <class>: <message>`
after the summary, and a failure between the claim and the provider reservation is also recorded on
the job (`last_error_category: worker`, `last_error_message`, a `worker_error` event) without
changing its status, lease, or counters.

The worker first assesses any unassessed provider-successful draft, then claims pending work in
canonical queue order, executes the provider, and applies terminology and structural QA. `--job-id`
processes exactly one explicitly selected job and never falls back to another job: a missing,
already claimed, running, completed, assessed, blocked, or attempt-exhausted target processes zero
jobs and reports the structural reason. `--chapter` restricts selection to one chapter, keeping
queue priority order inside it, and takes the usual chapter selector (`7`, `007`, `chapter-7`,
`prologue`). The two are mutually exclusive because a job ID already identifies exactly one job.
Both work with `--dry-run`, which selects but never claims, writes, or contacts a provider. Worker
output carries identifiers and counters only, never prose or claim tokens.

Let the worker answer a mechanical QA rejection by itself, instead of stopping at `qa_rejected`
and waiting for `repair-translation-job`:

```bash
book-translator run-translation-worker RUN_ID --provider claude-code --auto-repair \
  --include-qa-rejected --job-id JOB_ID --max-jobs 1 --dry-run
book-translator run-translation-worker RUN_ID --provider claude-code --auto-repair \
  --include-qa-rejected --max-jobs 200
```

`--auto-repair` is off by default and changes nothing without it. With it, a job the worker just
rejected is assessed against the versioned auto-repair policy and, if every blocking finding is
auto-repairable and budget remains, repaired through the ordinary repair boundary and re-assessed
until it is accepted, refused, or out of budget. `--include-qa-rejected` additionally selects jobs
that were already QA-rejected, in their own class *after* all pending translation work so canonical
queue ordering never changes, and requires `--auto-repair`. `--max-repair-attempts` (default 2)
bounds repairs per job and is distinct from provider transport retry: `--retry-provider-failures`
reschedules a request that produced no usable result, while a repair answers a delivered result that
QA rejected. `paragraph_count_mismatch` and exactly enforced `canonical_term_missing` are
auto-repairable; `competing_variant`, unknown codes, and any mixture containing one of them are
refused for an operator. Each authorized repair writes an immutable
`jobs/<chunk>/auto-repair/NNN/decision.yaml` recording the policy version, classified findings, and
budget; `docs/pipeline.md` documents the classes and the provenance model.

Let the worker answer a provider failure by itself, once the provider is demonstrably healthy
again, instead of leaving the job stranded at `failed`:

```bash
book-translator run-translation-worker RUN_ID --provider claude-code \
  --auto-recover-provider-failures --include-failed --job-id JOB_ID --max-jobs 1 --dry-run
book-translator run-translation-worker RUN_ID --provider claude-code \
  --auto-recover-provider-failures --include-failed --max-jobs 200
```

`--auto-recover-provider-failures` is off by default. It never rewrites the historical attempt: an
attempt classified `retryable: false` keeps that classification forever, and recovery is a new
governed decision saying the provider has since been re-established. The versioned recovery policy
places the failure in a provider-neutral family and answers `recoverable_now`, `wait_for_backoff`,
`preflight_required`, `operator_required`, `unsafe_to_replay`, or `exhausted`. Transient and
rate-limit failures are recoverable; authentication and provider-unavailability only against a
fresh passing preflight; malformed responses, contract violations, content refusals, local faults,
and anything unclassified fail closed; an ambiguous attempt is never replayed and stays with
`resolve-translation-reservation`. `--include-failed` additionally selects jobs that already
failed, in their own class after all pending translation and repair work, and requires
`--auto-recover-provider-failures`. `--max-provider-recovery-attempts` (default 2) is a separate
budget from `--max-repair-attempts`, from provider transport retry, and from the queue's
`max_attempts`. A transient failure additionally waits out the run's retry backoff and a rate
limit five times that, while the preflight-gated families are bounded by the preflight and the
budget instead of by a timer. Each authorized recovery writes an immutable
`jobs/<chunk>/provider-recovery/NNN/decision.yaml` recording the policy version, the failure
family, the referenced failed attempt and its untouched flags, the preflight evidence, and the
budget; `docs/pipeline.md` documents the families, the verdicts, and the provenance model.

Let the worker also recover a *repair* the provider failed to deliver, so a QA rejection followed
by a lost repair call does not need an operator either:

```bash
book-translator run-translation-worker RUN_ID --provider claude-code --auto-repair \
  --include-qa-rejected --auto-recover-provider-failures --include-failed \
  --max-jobs 1 --dry-run
book-translator run-translation-worker RUN_ID --provider claude-code --auto-repair \
  --include-qa-rejected --auto-recover-provider-failures --include-failed --max-jobs 200
```

A recovered repair stays a repair. It keeps `execution_kind: repair` and keeps
`repaired_from_attempt` pointing at the QA-rejected *translation* it answers — never at the failed
repair, which produced no translation to repair from. Because a repair request is derived rather
than frozen, recovery re-derives it and proves it byte-identical to the request that failed,
matching both provider-facing message digests against what the failed attempt recorded; tampered
QA evidence, a tampered request payload, or a missing frozen resource stops it with
`operator_required`. Instead of returning the job to `pending`, where it would be claimable as an
ordinary translation, the authorization restores the QA-rejected draft the failed repair displaced
and hands the job back to the same repair mechanism. Selecting one needs
`--auto-recover-provider-failures --include-failed` *and* `--auto-repair`; these jobs form the
last selection class, after pending translations, QA repairs, and failed translations. The replay
spends a provider attempt and a recovery attempt but **no** second QA repair decision, since QA
chose that repair once and the provider merely failed to carry it out; if the recovered repair
succeeds and QA still rejects it, a new repair decision is made only when the repair budget
allows. The decision record adds `recovered_execution_kind: repair` and a full repair-lineage
block of hashes.

Explicitly re-evaluate historical mechanical acceptance after a QA engine correction:

```bash
book-translator reevaluate-translation-job RUN_ID JOB_ID --attempt 1 --dry-run
book-translator reevaluate-translation-job RUN_ID JOB_ID --attempt 1
book-translator show-translation-reevaluations RUN_ID JOB_ID
```

Re-evaluation is local and provider-free. It preserves attempts and original acceptance evidence,
publishes separate immutable versioned evidence, and updates only effective mechanical acceptance
and derived run counters. It never implies human review or approval.

Re-evaluate a historically QA-rejected provider output inside a newer run, without a provider call:

```bash
book-translator reevaluate-historical-output NEW_RUN --from-run OLD_RUN --job 001-004 --attempt 1 \
  --dry-run
book-translator reevaluate-historical-output NEW_RUN --from-run OLD_RUN --job 001-004 --attempt 1
```

This is separate from same-run `reevaluate-translation-job`, which needs an attempt inside the run
being assessed, and from `reuse-accepted-translations`, which consumes only accepted outputs. It
handles the case the other two cannot: a provider-successful output its own run rejected, whose
destination job in a newer run has no attempt at all. `--job` takes a job or chunk ID and one job is
processed per invocation; `--dry-run` reports the whole decision and writes nothing.

Eligibility is decided by a dedicated compatibility policy. The request contract — source, context,
languages, system message, and output contract, including the historical attempt's own recorded
contract — must be identical, and so must every contract-bearing framed prompt section. A changed
rendered request is eligible only when the difference is confined to the framed terminology
projection, every other resource is byte identical, and the terminology move is a contiguous chain of
published governed registry edits that changed no locked or user-owned decision reaching the job.
The destination job must still be pending and unattempted, both frozen terminology resources must be
digest-verified, and a historically *accepted* output with an identical request is refused and routed
to reuse instead (one whose request moved is re-assessed here).

Publication copies the historical bytes into `jobs/<chunk>/carry-forward/001/`, records the
historical rejection and its findings alongside the current QA outcome, and sets the job's acceptance
origin to `historical_reevaluation`. Provider attempts stay at zero and no provider is contacted. A
defect that current QA still finds — a paragraph-count mismatch, for instance — keeps the job
`qa_rejected`; nothing is promoted for having been carried forward.

Migrate a historical run's usable provider work into a successor run over a newer snapshot, without
a provider call (render the successor's prompt packages first):

```bash
book-translator migrate-translation-run NEW_RUN --from-run OLD_RUN
book-translator migrate-translation-run NEW_RUN --from-run OLD_RUN --apply
```

Identical-request accepted outputs are reused; accepted outputs whose request moved and rejected
outputs are re-assessed under the successor's frozen QA through the historical re-evaluation
boundary (which, from 1.1.0, also governs series-policy books through amendment lineage and admits
accepted outputs whose request moved); outputs still rejected are carried forward for an explicit
`repair-translation-job`, which can repair a carried draft; jobs without a usable output stay pending
for fresh translation. Each publication is re-planned under the successor's lock and must match the
dry run.

Run the production semantic gate over a marked run:

```bash
book-translator run-semantic-gate RUN_ID --dry-run
book-translator run-semantic-gate RUN_ID --provider claude-code
```

`run-semantic-gate` judges every accepted paragraph of a marked-contract run, corrects only what
the judge named, and stops at a bounded state. `PASS` reaches semantic acceptance; `REVISE` goes
to a bounded semantic revision inside an explicit paragraph window and is re-judged independently,
at most twice; `INCONCLUSIVE` gets one wider-context second look. Whatever is still unproven is
recorded as `unresolved`, never accepted. A gated run cannot be released until every job is
semantically `pass`, and mechanical acceptance alone never counts. A run created before the gate
existed records no gate policy and is refused by this command; measure it with
`audit-semantic-coverage` instead.

Compare two runs' translations of one chapter without judging prose:

```bash
book-translator compare-translation-runs OLD_RUN NEW_RUN --chapter-id 048 \
  --first-release workspace/releases/book5-fa-v1
```

`compare-translation-runs` records both sides in hashes and counts only: acceptance origin,
attempts, structural and terminology codes, alignment findings, semantic status, revised and
unresolved paragraph identities, and which paragraphs differ. It never asks anything which
version reads better; that is the literary editor's phase.

Edit explicitly selected semantically accepted chapters for Persian literary quality:

```bash
book-translator run-literary-edit tr-9df726a15a52-002 \
  --chapter-id 011 --chapter-id 012 --chapter-id 032 --chapter-id 040 --chapter-id 048 \
  --historical-run tr-9df726a15a52-001 \
  --historical-release workspace/releases/book5-fa-v1 \
  --provider claude-code --dry-run
book-translator run-literary-edit tr-9df726a15a52-002 \
  --chapter-id 011 --chapter-id 012 --chapter-id 032 --chapter-id 040 --chapter-id 048 \
  --historical-run tr-9df726a15a52-001 \
  --historical-release workspace/releases/book5-fa-v1 \
  --provider claude-code
book-translator run-literary-edit tr-9df726a15a52-002 \
  --chapter-id 011 --chapter-id 012 --provider claude-code \
  --reuse-literary-run lr-0123456789abcdef01234567
book-translator show-literary-run lr-0123456789abcdef01234567
```

The whole chapter is the normal editor request. Every paragraph keeps one chapter-global marker, and
every retained change is independently re-judged for semantic coverage. A failed literary change gets
at most two editor correction rounds; if fidelity is still not proven, the exact pre-editor paragraph
is restored and recorded as `literary_edit_fallback`. Broad edits are measured rather than rejected;
suspicious rewrites receive stronger individual verification. Continuity review reports structured
Persian-prose findings without rewriting or grading source fidelity.

The result is a new immutable `workspace/literary-runs/<lr-id>/` derivative with separate call/token/
latency accounting for editor, semantic re-judge, correction, and continuity roles. It includes
hash-linked V1, V2 fidelity-only, and V2 literary layers plus a blind Version A/Version B reader
document and a separate answer key. This command never edits the input run, publishes a release,
regenerates the EPUB, or silently selects the whole book.

Judge whether accepted translations preserve their source meaning:

```bash
book-translator audit-semantic-coverage RUN_ID --chunk 011-003 --batch-size 4 --dry-run
book-translator audit-semantic-coverage RUN_ID --chunk 011-003 --provider claude-code
book-translator audit-semantic-coverage RUN_ID --chapter-id 011 --repetitions 2 --provider claude-code
```

`audit-semantic-coverage` is the provider-backed fidelity stage. Structural QA proves the right
paragraph is in the right place; this asks whether the meaning inside it survived. Each judge
request carries one paragraph identity, its English source, its Persian candidate, one neighbouring
paragraph on each side, and only the canonical terminology that paragraph triggers. It never
carries the translation prompt, the style guide, another chapter, the whole registry, or an earlier
verdict, so the judgement is independent of the pipeline that produced the translation.

Each paragraph comes back `PASS`, `REVISE`, or `INCONCLUSIVE` with findings from a closed
vocabulary, and a versioned policy decides which findings are material enough to count.
`INCONCLUSIVE` is persisted as itself and never converted into a pass. The stage changes no
acceptance, status, counter, or translated byte; a run created before it existed has no semantic
evidence, and that absence means *not judged* rather than *passed*. `--dry-run` reports exactly how
many provider calls an evaluation would spend and contacts nothing.

Audit paragraph alignment across a run, read-only:

```bash
book-translator audit-paragraph-alignment RUN_ID --dry-run --show-findings
book-translator audit-paragraph-alignment RUN_ID
```

`audit-paragraph-alignment` answers a question equal paragraph counts cannot: is each translated
paragraph plausibly the translation of *that* source paragraph? It resolves every job's effective
acceptance, measures each aligned pair — words, characters, sentences, numerals, quotation marks —
and reports findings under the versioned `en-fa/1.0.0` policy. It contacts no provider and changes
no acceptance, status, counter, or translated byte. `--dry-run` skips only the artifact, which is
otherwise written to `workspace/audits/paragraph-alignment/<run>/report.yaml`.

A run created before the numbered paragraph contract is measured positionally, which is the only
evidence available for it. A run created under the contract has its paragraph identities proven at
the provider boundary instead, and these measurements are the residual check on top of that.

Diagnose and resolve what a finished run still rejects:

```bash
book-translator audit-final-qa RUN_ID
book-translator finalize-translation-run RUN_ID --dry-run
book-translator finalize-translation-run RUN_ID --apply --provider claude-code
book-translator finalize-translation-run RUN_ID --apply --provider claude-code \
  --allow-repair-escalation
book-translator finalize-translation-run RUN_ID --apply --provider claude-code \
  --no-provider-recovery
```

`audit-final-qa` is read-only and answers the question a completed run leaves open: of the jobs
still `qa_rejected`, which are translation defects and which are QA verdicts that have stopped
being true. It re-runs the *current* terminology and structural validators against the frozen
resources in memory, classifies every `competing_variant` finding against the registry, publishes
`workspace/audits/final-qa/<run>/audit.yaml`, and prints the plan grouped by verdict, by
competing-variant root cause, and by the terminology decision an operator would have to answer.
`--dry-run` skips only the artifact.

`finalize-translation-run` drives the resolution, cheapest rung of the ladder first. It defaults
to `--dry-run`, and a dry run guarantees zero provider calls, zero registry writes, zero queue
writes, and zero acceptance changes while still naming, per job, the step an applying run would
take: `reevaluate`, `repair_structure`, `repair_translation`, `recover_provider_failure`,
`escalate_and_repair`, or `report`. `--apply` acts on the four verdicts an unattended process may
act on — `accepted_no_change` and `reevaluate` go through the existing provider-free re-evaluation
boundary, `repair_structure` through the provider-free structural repair below,
`repair_translation` through the existing bounded auto-repair path with automatic provenance — and
a repair the provider never delivers is handed to the existing provider-recovery boundary and
re-sent once (`--no-provider-recovery` switches that off). Every other verdict is reported, not
acted on. There is no new provider path and no new acceptance path. `--apply` without `--provider`
still runs the whole provider-free half and reports the repairs with a `provider_required` reason,
so getting the free resolutions never requires naming a provider.

**Structure is repaired before anything is sent anywhere.** `paragraph_count_mismatch` is one code
covering two unrelated defects. Sometimes the translation contains exactly the right prose in
exactly the right order and only the blank lines are in the wrong places — a source line written as
two paragraphs, or a paragraph break dropped between two source blocks. That is decidable from the
frozen source alone, and spending a provider attempt on it would be paying for an answer the
workspace already holds. The structural repair engine segments both sides into non-blank lines; if
the two line sequences have the same length the correspondence is the identity and the source's own
blank-line placement is simply re-applied, and if they differ it searches for a monotone alignment
and accepts it only when it is bounded, cheap, and uniquely best by a margin. The classes it can
name are `blank_line_geometry`, `translation_line_split`, `translation_line_merge`,
`line_count_divergence`, and `content_divergence`; only the first two are repaired, and a
translation that fused two source lines into one sentence flow is always a provider's question
because where the break belongs is a judgement about meaning.

An accepted repair re-emits the translation's own lines, so it changes whitespace and nothing else
— the engine verifies that on its own output before the repair leaves it. The repaired bytes are
published to `jobs/<chunk>/structural-repair/001/translation.md` beside a
`TranslationStructuralRepairEvidence` record holding the full diagnosis, the superseded verdict it
answers, both translation digests, and the terminology and structural reports re-run over the
repaired text. The provider attempt, its translation, and its acceptance sidecar are never touched;
what moves is the job's effective verdict, exactly as a provider-free re-evaluation moves it, under
acceptance origin `structural_repair`. Adoption is refused unless block correspondence is actually
restored and no blocking code appears that the standing rejection did not already carry, so a
structural repair can only ever remove findings. Occurrence-anchored terminology QA needs a block
correspondence to decide anything, so a terminology finding that only existed because the alignment
was broken clears on its own once the blocks line up — and one that does not clear stays exactly as
blocking as it was, and is answered by a provider repair in the next cycle.

`competing_variant` is the blocker that dominates the end of a book, and it is not one defect. The
analyzer classifies each occurrence deterministically:

| Class | Meaning | Verdict |
| --- | --- | --- |
| `canonical_identity` | the variant contains or derives from the canonical, so it is the same lexeme | not enforced at all |
| `retired_competitor` | the effective registry no longer records the rivalry | `resolve_terminology_then_reevaluate` |
| `historical_output_only` | only an older provider output ever attested it | `resolve_terminology_then_reevaluate` |
| `canonical_absent` | the rival is present and the canonical is nowhere | `repair_translation` |
| `locked_canonical_conflict` | a locked or user-owned decision claims the target | `resolve_terminology_then_repair` |
| `rival_ai_decisions` | one settled decision against a provisional one — a deterministic winner | `resolve_terminology_then_reevaluate` |
| `genuine_canonical_conflict` | two settled decisions of equal standing disagree | `operator_required` |
| `insufficient_evidence` | canonical and rival both present, nothing governed explains why | `operator_required` |

A locked or user-owned canonical is never the side that gives way: the decision stands and the
translation is what may be repaired. The two `resolve_terminology_*` verdicts are reported rather
than executed, because the run's terminology resource is frozen into its identity — settling them
means re-freezing terminology, taking a new snapshot, and carrying accepted work forward with
`reuse-accepted-translations`, not editing a frozen run.

A job whose provider budget is spent behind a mechanically repairable defect is `exhausted`, and
by default that is where it stops. `--allow-repair-escalation` authorizes at most one exceptional
grant per job: a `TranslationRepairEscalation` record is published under
`jobs/<chunk>/repair-escalation/001/`, naming the ceiling it raises, the attempts it grants, the
exact attempt slot it authorizes, the blocking findings it is spent on, the structural diagnosis
that shows no free remedy was left, and that it contacted no provider of its own — and only then
does the job's ceiling move. It is refused outright while a deterministic structural repair is
still available, so a provider budget is never widened to buy something the workspace could have
given away. No historical counter is reset, a second grant is refused, and the queue itself
enforces the rule — a job whose attempt ceiling exceeds the run's without a matching persisted
escalation is a stale queue.

Plan and simulate terminology registry remediation without changing the registry:

```bash
book-translator plan-terminology-remediation --review-run ta-EXAMPLE --output workspace/plan.json
book-translator show-terminology-remediation-plan PLAN_ID --plan-file workspace/plan.json
book-translator show-terminology-remediation-operation PLAN_ID OP_ID --plan-file workspace/plan.json
book-translator apply-terminology-remediation PLAN_ID --plan-file workspace/plan.json --dry-run
```

The engine consumes the audited findings instead of editing YAML directly and gives every audited
finding exactly one primary action. It plans an action as deterministic only when the outcome is
provable from stored governance evidence; equal-priority conflicts and any fix that would require
composing a new Persian target are recorded as unresolved with their blocking evidence. A plan with
unresolved operations is never applicable. The dry run applies the plan to an isolated copy,
revalidates it, re-runs the audit and structural invariants, reports before/after canonical lookups,
and leaves the real registry byte-identical. Omitting `--dry-run` is a separate explicit operator
action that refuses unless that plan's own dry run proved it safe.

The audit reports two finding families, and the plan carries both. `canonical_validator` findings
are contradictions between two canonical records; `exact_enforcement` findings come from
`audit-terminology-exact-enforcement` and say a source form cannot bear a fixed target lexeme at
all. Filter either family, or one exactness disposition — `definitely_invalid`, `definitely_valid`,
`context_review_required`, `unresolved`:

```bash
book-translator audit-terminology-registry-remediation --family exact_enforcement
book-translator audit-terminology-registry-remediation --disposition definitely_invalid
book-translator show-terminology-remediation-finding FINDING_ID
```

Exact-enforcement findings expand into five operations. `retire_and_reject_candidate` withdraws a
canonical decision and marks its candidate not terminology in the same atomic publication;
`retire_decision` withdraws the decision alone when the source form remains another candidate's
alias; `change_enforcement` relaxes an over-broad exact claim without touching any target text;
`correct_target` rewrites exactly one token of a target so a compound spells its canonical
component the way that component spells it; and `open_terminology_question` records the durable
question a defect needs without mutating anything, because composing a corrected Persian target is
a translation act. `correct_target` is the narrow opposite case rather than an exception to that
rule: it fires only when exactly one decided component is missing from a target and exactly one
token of that target is unaccounted for by any other component, so the substitution invents no
lexeme and moves no word. Because respelling a compound is what can make its exact claim over-broad
for the first time, the same operation also carries the relaxation its own fix uncovers. There is
deliberately no
`delete-terminology-decision` command: a retirement exists only inside an immutable plan, carries
the SHA-256 of the exact decision and candidate records it was built against, refuses to apply if
either drifted, and can never name a locked or user-owned decision — those stay with
`correct-terminology-decision`. Withdrawing a decision can uncover a contradiction that the
withdrawn component was masking; the dry run refuses to call a plan safe unless that plan itself
already referred each surviving blocking finding to a human.

Group unresolved remediation conflicts into families and inspect a semantic reconciliation run:

```bash
book-translator build-terminology-conflict-graph PLAN_ID --plan-file PLAN --output GRAPH
book-translator show-terminology-conflict-component COMPONENT_ID --graph-file GRAPH
book-translator show-terminology-reconciliation-run --run-file RUN --component COMPONENT_ID
```

The conflict graph joins records by exact canonical conflict, alias, canonical subterm, competing
variant, and normalization identity so a whole terminology family is resolved together. Semantic
review is sharded by family with immutable attempts, strict validation, and retry of only failed
shards. Alias identity must be proven from source evidence; matching Persian targets never establish
it. Resolutions expand into typed registry edits that are merged with the deterministic remediation
operations and simulated in isolation, and protected terminology is checked for authoritative
correctness rather than mere stability.

A decision also records *what target evidence proves it was honored*. `--enforcement` accepts
`exact`, `morphological`, `contextual`, or `advisory`; omitting it keeps the derived default, which
is `exact` for every decision except an AI-owned `people` classification (`morphological`) and a
context-sensitive one (`contextual`). A locked decision is always `exact` and refuses any other mode.

```bash
book-translator set-terminology "Ajah" "آجای" --type organization --locked
book-translator set-terminology "took the Stone" "فتح سنگ" --type historical_event \
  --decision-source ai --unlocked --enforcement contextual
```

`--target-form` (repeatable, on `set-terminology` and `set-series-terminology`) explicitly authorizes
a further target realization of the same decision. The target stays the preferred rendering; exact
enforcement is satisfied by any authorized form, each matched with the usual lexical boundaries and
Persian inflections. Forms are never inferred, and a decision without them is enforced exactly as
before. A locked decision is not silently re-saved with different forms.

```bash
book-translator set-series-terminology "gentled" "خنثی‌شده" --type story_term \
  --target-form "خنثی شده" --target-form "خنثی شدن" \
  --rationale "participle and verbal noun of one governed concept" --book lord-of-chaos
```

Every provider-facing rendering of a decision — the translation prompt's series tiers and registry
block, repair requests, the semantic judge and reviser, chapter titles, and the literary editor —
states its authorized forms beside the target with one shared rule; decisions without forms render
exactly as before.

Once a snapshot has frozen a series policy, change it with a governed, all-or-nothing amendment that
leaves an immutable lineage record (`workspace/series-terminology-amendments/`). Each item restates an
existing term's complete decision; the batch is refused if it would create a term or override an
explicit user decision.

```bash
book-translator amend-series-terminology config/series/wheel-of-time/amendments/neutralization-family.yaml \
  --book lord-of-chaos
book-translator amend-series-terminology config/series/wheel-of-time/amendments/neutralization-family.yaml \
  --book lord-of-chaos --apply
book-translator derive-book-terminology --book lord-of-chaos --apply
```

When a governed spelling is also an ordinary English word, don't widen or narrow the decision.
Exempt the single occurrence you have adjudicated. The exemption binds the chunk bytes, span,
ordinal, paragraph and decision by hash, and a consumer that finds any of them changed refuses the
exemption. Only a new snapshot carries an exemption to translations. The command is a dry run
unless `--apply` is given.

```bash
book-translator exempt-terminology-occurrence 012-001 --book lord-of-chaos \
  --spelling stilled --occurrence 1 \
  --rationale "ordinary English 'stilled' (made still), not the One Power sense"
book-translator exempt-terminology-occurrence 012-001 --book lord-of-chaos \
  --spelling stilled --occurrence 1 --rationale "..." --apply
```

A paragraph that is mechanically clean but says the wrong thing is corrected one paragraph at a
time. You name the paragraph and the defect; you never supply the replacement Persian. The
reviser may change that paragraph and nothing else, and the result is adopted only if the bounds,
the full acceptance battery and an independent judgement of the revised paragraph all agree.

```bash
book-translator revise-translation-paragraph tr-759c2a0ee15c-001 041-002 --paragraph 23 \
  --code factual_change --source-evidence "that stilled his alarm" \
  --reason "ordinary English: his alarm subsided; not the One Power sense"
book-translator revise-translation-paragraph tr-759c2a0ee15c-001 041-002 --paragraph 23 \
  --code factual_change --source-evidence "..." --reason "..." \
  --provider codex-cli --apply
```

An exact decision is additionally satisfied by a *coordinate-elided* realization: when the source
occurrence leads an `and`/`or` coordination whose second conjunct the registry independently
attests as its own decision, the canonical target words may appear in order across one short gap
containing a target coordinator. "High Lord or Lady" is honored by «لرد یا بانوی اعظم». Locked and
user-owned decisions stay contiguous.

`audit-terminology-exact-enforcement` is a read-only census of which decisions should never have
been exact. It changes no QA behavior; its findings reach the registry only through the remediation
plan/apply workflow above.

```bash
book-translator audit-terminology-exact-enforcement
book-translator audit-terminology-exact-enforcement --risk containment_inconsistency
book-translator audit-terminology-exact-enforcement \
  --registry workspace/resources/terminology/<sha256>.yaml --risk verb_phrase --limit 50
```

Work with provisional decisions and open terminology questions:

```bash
book-translator set-terminology "Aunt X" "خاله ایکس" --type person \
  --decision-source ai --state provisional --unlocked
book-translator open-terminology-question "Aunt X" --category kinship_side \
  --question "Is X a maternal or paternal aunt?" --non-blocking \
  --current-book checked_inconclusive
book-translator list-terminology-questions --status open
book-translator report-terminology-readiness
book-translator analyze-terminology-impact "Aunt X" --proposed-target "عمه ایکس"
book-translator resolve-terminology-question tq-EXAMPLE --target "عمه ایکس" \
  --provenance other_volumes --rationale "A later volume names the paternal line."
book-translator reject-terminology-candidate "platform" --reason "ordinary common noun"
book-translator correct-terminology-decision "Aunt X" --target "خاله ایکس" \
  --decision-source ai --state provisional --unlocked --reason "..." --dry-run
```

A provisional decision is a working translation choice, never a factual claim, and can never be
locked. It is enforced exactly like a confirmed decision so the book stays internally consistent
while the meaning stays open, and every provisional decision must carry an open question. A
non-blocking question keeps snapshots, prompts, provider execution, QA, and acceptance flowing; only
an explicitly blocking question stops them. Web and other-volume research are optional, so network
access is never required for readiness. `correct-terminology-decision` is the only way to lower a
decision's provenance or unlock it, and it publishes a durable record of the superseded fields.

Run checks:

```bash
pytest
ruff check .
ruff format --check .
mypy src tests
```

Run the deterministic normalization benchmark, which generates synthetic text only:

```bash
python scripts/benchmark_normalization.py
python scripts/benchmark_chunking.py
python scripts/benchmark_translation_snapshot.py
python scripts/benchmark_translation_queue.py
python scripts/benchmark_prompt_packages.py
python scripts/benchmark_provider_executor.py
```

The benchmark performs three identical runs over 500 generated source blocks and reports structural
counters, elapsed time, peak traced Python memory, and the stable output SHA-256. It has no
machine-specific pass/fail timing threshold.

### Translate chapter titles

```bash
book-translator translate-chapter-titles tr-9df726a15a52-001 \
  --provider claude-code --dry-run --show-plan
book-translator translate-chapter-titles tr-9df726a15a52-001 \
  --provider claude-code
book-translator translate-chapter-titles tr-9df726a15a52-001 \
  --provider claude-code --force
```

A chapter title is book metadata, not prose: it lives in the TOC, in chapter metadata, and in every
snapshot manifest, and no chunk ever contained one. `translate-chapter-titles` translates the
titles of one run's frozen snapshot under the terminology that snapshot froze and publishes one
immutable artifact. It touches no translation run, job, attempt, accepted translation, or QA state
— the run is opened read-only only to learn which snapshot it froze.

One provider call carries one English title plus the canonical terminology decisions that title
actually contains; no chapter prose, no context, no other title. Identical English titles are asked
once and the answer reused, so two chapters cannot end up with different Persian titles for the
same heading. `--dry-run` renders every request, contacts nobody, and writes nothing; `--show-plan`
lists the chapters with their terminology counts and request digests. `--output` overrides the
destination, and `--force` replaces an existing artifact only after confirming its manifest names
the same artifact ID.

```
workspace/resources/chapter-titles/<ct-id>/
    chapter-titles.yaml
    manifest.yaml
    provenance.json
```

`chapter-titles.yaml` carries no timestamp, so the same titles always serialize to the same bytes
and the same `content_sha256`. `manifest.yaml` indexes the files by hash and holds the single
timestamp. `provenance.json` records, per title, both hashes, the origin, provider, adapter version
and model, the request digest, the snapshot and terminology it was bound by, the canonical
decisions enforced, the QA verdict, and when it was translated — and never a prompt or a raw
response.

Title QA is pure and provider-free and runs before anything is published: every chapter has a
title, none is empty, echoes its source, lacks Persian script, or is over-long, both hashes address
their own titles, the source title has not drifted from the snapshot's, one English title maps to
one Persian title, and every canonical name the source title contains survives into the
translation. Any error refuses the artifact outright.

### Assemble a release from a completed run

```bash
book-translator export-translation-run tr-9df726a15a52-001 \
  --output workspace/releases/book5-fa-v1 --dry-run
book-translator export-translation-run tr-9df726a15a52-001 \
  --output workspace/releases/book5-fa-v1 --show-manifest
book-translator export-translation-run tr-9df726a15a52-001 \
  --output workspace/releases/book5-fa-v1 --force
```

`export-translation-run` turns one completed translation run into a canonical Markdown book. It is
provider-free and mutates nothing: it reads the run, the frozen snapshot the run answers, and the
accepted translations it produced, and writes only into the release directory named by `--output`
(whose last path component becomes the release ID).

The export refuses anything it cannot prove. The run must be `completed`, its counters and status
must agree with its jobs, its frozen snapshot must still validate and still match, every job must be
`succeeded` and `accepted` with none QA-rejected or outstanding, chunk coverage must be exactly one
job per frozen chunk in frozen order with no hole and no duplicate, and every accepted translation
must still exist, still hash to what its acceptance evidence recorded, answer the same frozen source,
and be canonical Markdown. Effective acceptance is resolved through the same supersession chain the
repair path uses, so re-evaluated, structurally repaired, reused, and carried-forward translations
are released as the run accepted them.

Assembly preserves accepted text exactly. A chapter is its heading paragraph followed by the
paragraphs of its chunks in frozen order, joined in the canonical Markdown form the chunk engine
already conserves — nothing is normalized, re-wrapped, or rewritten after acceptance, and scene
separators and Persian bytes pass through untouched. The heading is the only added text and is never
invented. Precedence is the workspace's own Persian title, then a governed chapter-title artifact
(see above), then the English title verbatim, with the choice recorded per chapter as
`heading_source`. When an artifact supplies headings the release manifest also records its
`chapter_title_artifact_id` and `chapter_title_artifact_sha256`; discovery is automatic,
`--chapter-titles <id|path>` names one explicitly, and `--no-chapter-titles` ignores them. `book.md` is byte-identical to the chapter files
joined with a newline.

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

`metadata.yaml` and `terminology.yaml` are byte-exact copies of the frozen resources, verified
against the snapshot's hashes before copying. `manifest.yaml` indexes every published file with its
SHA-256 and byte count and carries a `content_sha256` over the sorted `(path, sha256)` pairs; it is
the only file holding a timestamp, so a repeated export reproduces every other byte and the same
content address. `translation-report.yaml` is counters only — attempts, repairs, recoveries,
re-evaluations, reuses, carry-forwards, acceptance origins, QA versions. `provenance/`
`translation-lineage.json` records per chunk the source and translation hashes, acceptance origin and
evidence, attempt identity, originating provider and how it was established, QA versions, and the
paragraph offset where the chunk begins — and never prompt or response material.

`--dry-run` renders the whole release and writes nothing. `--show-manifest` prints the file
inventory. `--force` replaces a destination only after confirming its manifest names the same
release, and a release may never be written into canonical workspace state. Releases are gitignored:
they contain the complete translated book. EPUB generation is not implemented and will consume a
release rather than a translation run.

### Verify a release before publishing it

```bash
book-translator verify-release workspace/releases/book5-fa-v1
book-translator verify-release workspace/releases/book5-fa-v1 --verbose
book-translator verify-release workspace/releases/book5-fa-v1 --json
book-translator verify-release workspace/releases/book5-fa-v1 --output verification.yaml
```

`verify-release` is the gate between assembly and publication. It proves — from the bytes on disk,
not from what the assembly claimed — that a release is complete, internally consistent, immutable
since publication, and traceable to the run and snapshot it names. It is read-only everywhere,
contacts no provider, and mutates nothing; the only thing it writes is the report you ask for with
`--output`, and it refuses to write that inside the release it just judged.

Seven checks run, each counting what it examined against what the manifest said to expect:

| Check | What it proves |
| --- | --- |
| `manifest` | indexed files present, hashed, sized, nothing unindexed, `content_sha256` still addresses the index |
| `book_integrity` | `book.md` present, UTF-8, canonical Markdown, byte-identical to its chapters joined |
| `chapters` | every chapter present once, in order, with matching counts and the recorded heading |
| `translations` | every frozen chunk accepted and tiling its chapter's paragraphs with no gap |
| `titles` | referenced title artifact present, hashed, complete, and still QA-clean |
| `terminology` | released frozen resources are byte-identical to the resources named |
| `provenance` | every chunk names its hashes, acceptance, attempt, and provider resolution |

The verdict is `READY_FOR_PUBLICATION` exactly when no check failed and no error finding stands;
anything else is `NOT_READY_FOR_PUBLICATION` and the command exits non-zero. A check whose evidence
is unreachable — the chapter-title artifact, when a release has been copied away from its workspace
— is reported `SKIPPED`, never `PASS`.

Everything in the report except `created_at` is a pure function of the bytes on disk, and its
`content_sha256` covers exactly that, so two runs over an unchanged release produce the same content
address and a differing one is evidence that something moved. `--output` writes JSON when the path
ends in `.json` and YAML otherwise. EPUB generation is not implemented and will consume a *verified*
release.

### Build the EPUB

```bash
book-translator build-epub workspace/releases/book5-fa-v1 --dry-run
book-translator build-epub workspace/releases/book5-fa-v1 --show-manifest
book-translator build-epub workspace/releases/book5-fa-v1 --output dist/book.epub --force
```

`build-epub` turns one verified release into an RTL EPUB 3. It is a publication format converter and
nothing more: its only input is a release directory, it makes no editorial decision, and it never
reads a translation run, job, attempt, provider artifact, QA record, or snapshot — the package holds
no import of any of them and no provider import at all.

Release verification runs inside the build. Anything short of `READY_FOR_PUBLICATION` refuses the
book outright and names the failed checks, so an invalid manifest, a missing chapter, missing
provenance, a changed hash, or a title artifact that no longer holds all stop the build before a
byte is rendered. The builder adds two demands of its own: the release must carry book metadata, and
each chapter must open with exactly the heading the release recorded.

```
mimetype                       (first entry, stored uncompressed)
META-INF/container.xml
OEBPS/content.opf
OEBPS/nav.xhtml
OEBPS/styles/rtl.css
OEBPS/chapters/000-prologue.xhtml, 001.xhtml, ...
```

Chapters are named by canonical chapter ID, so each archive entry maps one-to-one onto a release
chapter. The package is EPUB 3.0 with `page-progression-direction="rtl"`, every document carries
`dir="rtl"` and the book's language, and `rtl.css` sets `direction: rtl` with right-aligned text. The
navigation document lists every chapter in order under its Persian title. A chapter's first paragraph
becomes the `<h1>` and every other paragraph a `<p>` in published order; a multi-line paragraph keeps
its lines with `<br/>`, a scene separator keeps its text, and the only change to any text is the XML
escaping the format requires.

**Building one release twice produces byte-identical archives.** The `dc:identifier` is a UUIDv5
derived from the release's content address, `dcterms:modified` is the release manifest's `created_at`
rather than the clock, archive timestamps and attributes are fixed, and entries are sorted, so
filesystem order never reaches the archive. The archive is rendered in memory, read back, and
validated — structure, XML well-formedness, UTF-8, package metadata, spine and manifest order,
navigation, per-chapter titles, and its own content address — *before* it is written, so a build that
fails its checks writes nothing.

`--dry-run` renders the whole book and writes nothing. `--force` is required to replace an existing
file. An EPUB may never be written inside the release it came from. The default output is
`output/<release-id>.epub`, which Git already ignores.

## Governance and development

Project development is governed by the [architecture contract](ARCHITECTURE.md),
[developer charter](docs/developer-charter.md), [coding standards](docs/coding-standards.md),
[testing policy](docs/testing.md), [review process](docs/review-process.md),
[benchmark policy](docs/benchmark.md), [release process](docs/release-process.md), and
[provider privacy policy](docs/privacy.md). See the [roadmap](ROADMAP.md) for nonbinding planned
direction and the [changelog](CHANGELOG.md) for release
history. `AGENTS.md` is the concise mandatory entry point for Codex sessions.

## Directory overview

- `input/`: source PDF files (ignored by Git)
- `config/`: book metadata, glossary, style guide, and translation prompt
- `extracted/`: extracted chapters and source text artifacts
- `chunks/`: translation-sized source chunks
- `translated/`: draft and assembled translated chapters
- `review/`: review artifacts and approvals
- `output/`: generated books (EPUB files ignored by Git)
- `state/`: resumable pipeline state
- `logs/`: runtime logs
- `assets/`: output styling such as RTL EPUB CSS
- `src/book_translator/`: Python package and CLI
- `tests/`: automated tests
- `workspace/`: canonical chapter/artifact representation of the first book (created by pipeline operations)
- `workspace/books/<book-id>/`: the identical canonical layout for each further book, selected with `--book`
- `config/books/<book-id>.yaml`: the configuration of each further book (`config/book.yaml` stays the first book's)
- `workspace/releases/`: assembled Markdown releases (ignored by Git)
- `workspace/literary-runs/`: immutable private literary derivatives and blind comparisons (ignored by Git)
- `workspace/resources/chapter-titles/`: immutable translated chapter-title artifacts
- `schemas/`: committed JSON Schemas generated from the canonical Pydantic models
# Audit source quality before a full-book snapshot

Run the deterministic local audit without writing artifacts:

```bash
book-translator audit-source-quality --fail-on high
```

Narrow inspection with `--chapter`, `--confidence`, or `--category`. Use `--json` for machine-readable
output. Persistence is explicit: `--output workspace/audits/source-quality/<directory>` writes the
reproducible YAML report and findings. The audit never translates or repairs text.
Audit Engine 1.2.0 and interrupted-token detector 1.1.0 exclude structurally independent new-speaker turns from high-confidence
continuation promotion while retaining uncommon short interruption-edge tokens as extraction-owned
signals.

List only unresolved high-confidence blockers and inspect one finding without searching chapters:

```bash
book-translator audit-source-quality --confidence high --blocking-only
book-translator show-source-quality-finding sqf-EXAMPLE
```
# Source-edition anomalies

Known edition defects live in `config/source-anomalies.yaml`; immutable extraction is not repaired.
Use `list-source-anomalies`, `show-source-anomaly ID`, and `register-source-anomaly --help`.
Registration reads exact source evidence from a UTF-8 file. Source Quality Audit links exact matches
and applies explicit blocking policy.
