# Provider execution privacy

Book content may leave the local workspace only through an explicitly selected and configured
translation provider. The OpenAI adapter uses the official Python SDK Responses API. The `codex-cli`
adapter instead invokes the user's existing ChatGPT-authenticated local Codex CLI session; it does not
use an API key, third-party router, direct API call, alternate base URL, or persisted authentication
copy. It strips API-key and OpenAI/Azure base-URL variables from the child environment while retaining
the CLI's normal authenticated session lookup.

Set `OPENAI_API_KEY` in the shell or an uncommitted `.env` file. Keys, claim tokens, authorization
headers, SDK objects, and absolute paths are never persisted. Request artifacts reference existing
private prompt files by project-relative path and SHA-256. Sanitized response and translation files
remain sensitive, use owner-only permissions where supported, and are ignored by Git.

CLI list, show, status, errors, events, benchmarks, and reports expose structural identifiers,
counters, hashes, classifications, timing, and usage only. They never print prompt, source, context,
response, or translated prose. Platform permission semantics vary; local operators must secure the
workspace and backups appropriately.

A provider answer that arrived whole but was refused by structural validation is retained as the
private `rejected-output.md` beside its failed attempt, with the same owner-only permissions and Git
exclusion as `translation.md`, and it is the only place that prose lives: `response.json` stays
sanitized metadata, `attempt.yaml` records its path, hash, and prose-free marker findings (codes,
expected and observed marker tokens, indexes, counts), the error message and queue history carry
finding codes only, and the CLI reports its presence, never its content.

SDK retries are disabled in executor version 1. Rate limits and clear transient failures end the
attempt and require an explicit queue retry. Any post-send uncertainty is ambiguous and blocks replay
to avoid duplicate billable calls.

The `claude-code` adapter is the Claude Code equivalent and is explicitly not the Anthropic API. It
invokes the user's installed `claude` executable under their existing Claude Code login and never
reads, requests, or persists an Anthropic API key: every `*_API_KEY` variable is stripped from the
child environment, together with `ANTHROPIC_AUTH_TOKEN`, Anthropic base-URL overrides, and the
Bedrock, Vertex, and Foundry routing variables, so the call cannot silently reach an API account or a
third-party gateway. Claude Code receives only the rendered system prompt and user message for the
one job being translated: no claim token, no unrelated chunk, no environment values, no repository
credentials. Tools, MCP servers, skills, plugins, hooks, project settings, and session persistence
are disabled for the call, so the model cannot read or modify this repository while translating.

Claude Code execution uses a private temporary working directory as its working directory, stages the
system prompt as an owner-only file that is deleted when the adapter returns, and passes the user
message on standard input rather than on the command line. The translation is reconstructed from
the `stream-json` transcript by joining the text blocks of the primary session's assistant messages
in order; the transcript exists only in the subprocess capture and is never written to disk, and
thinking blocks, tool blocks, user and system events, subagent output, banners, ANSI sequences,
progress messages, stderr, and cost are neither parsed into nor persisted as `translation.md`.
`response.json` records the turn and message counts beside the usage counters and never a message
body, and a reconstruction refusal names the structural rule that failed, never a line of the
transcript. Recorded provenance is limited to
structural values — CLI version, authentication method, first-party inference provider, requested and
returned model, usage counters, timings — and never includes account email, organization identity, or
credentials.

A failing exit is the one place process output is retained, and only as a short sanitized diagnostic
inside the attempt's `sanitized_error_message`. It is taken from the CLI's structured result object
when one exists and otherwise from the trailing stderr lines, never from a successful `result` field,
and it is redacted before retention: ANSI sequences, emails, URLs, POSIX and Windows paths, bearer,
labelled, and prefixed secrets, session UUIDs, and long token-shaped runs are removed, the remainder
is capped at 240 characters, and a candidate containing Arabic-script characters is discarded on the
grounds that translated prose is not a diagnostic. Full stderr, full stdout, and the prompt are never
persisted. Its preflight (`check-translation-provider --provider claude-code`) reads local
installation and login status only and sends no book content.

Codex CLI execution uses a private temporary working directory, `--ephemeral`, and
`--sandbox read-only`. Only `--output-last-message` is read as translation output; banners, progress,
reasoning, session identifiers, stdout, and stderr are neither parsed into nor persisted as
`translation.md`. The temporary output and working directory are removed after the adapter returns.
Its preflight (`check-translation-provider --provider codex-cli`) runs only `codex --version`,
`codex exec --help`, and `codex login status` with empty stdin under the same credential-stripped
environment as execution; it never reads the CLI's credential store, and the login status text,
which carries a masked key fragment for an API-key login, is classified and dropped rather than
reported or persisted.

The Semantic Coverage Judge is a third provider contract and the only stage that sends *both*
sides of a translation to a provider. One judge call carries the English source paragraph, its
Persian candidate, at most one neighbouring paragraph on each side of both, and only the canonical
terminology decisions that paragraph's own source text triggers. It never carries another chapter,
the whole terminology registry, the translation prompt, the style guide, a claim token, or any
earlier judgement. It reaches a provider only through the same audited transport translation uses,
so the credential stripping, the ephemeral read-only working directory, and the sanitized result
handling described above apply unchanged.

What the judge persists is deliberately narrower than what it sends. Evidence records digests of
the system and user messages rather than the messages, and never the model's own reasoning. The
one book-derived text it retains is a short source fragment per finding, capped by the contract,
because a later repair stage has to know which content was lost. Semantic coverage evidence is
therefore written owner-only and ignored by Git for the same reason prompt packages and provider
attempts are, and the CLI prints verdict counts, finding codes, and severities only.

The semantic reviser is a fourth contract and the only one that asks a provider to return
translated prose after the translator: one bounded window of marked Persian paragraphs, corrected
for named defects. It travels over the same transport with the same protections and receives the
window, its English source, the findings, the triggering canonical terms, and one neighbour on
each side — never the whole chunk, the translation prompt, or any judgement text. Adopted
revisions are written into the run as private `translation.md` files beside their evidence, with
the same owner-only permissions and Git exclusion as provider attempts.

The Persian literary editor is a fifth provider contract. It receives one semantically accepted
Persian chapter and its English source context under paragraph markers, relevant frozen terminology,
the compact style contract, and the optional book profile. This broader chapter scope is deliberate
and is why the command requires an explicitly selected provider. It receives no translator
chain-of-thought, semantic-judge reasoning, repair rationale, or previous score. Oversized chapters
use bounded overlapping sections rather than isolated chunks.

Changed paragraphs go back through the Semantic Coverage Judge under the existing privacy boundary.
Only paragraphs that fail can reach the literary correction role, together with their material
semantic findings and faithful reference. The final continuity reviewer receives the assembled
Persian chapter plus style/profile and relevant terminology, but no English source; it cannot return
replacement prose. All three literary-role outputs use the same audited transport and Claude Code
isolation rules above. Persisted provider records contain message hashes, models, usage, and latency,
not prompts or reasoning. Edited chapter Markdown, normalized provider responses, semantic evidence,
corrections, continuity observations, blind passages, and the answer key are private, owner-only, and
Git-ignored under `workspace/literary-runs/`. CLI summaries expose only IDs, counts, hashes, rates,
models, and artifact paths.

Terminology analysis is a distinct provider contract. Only an explicitly selected terminology
provider receives selected frozen chunk source. The `fake` provider is local and does not inspect
prose. The `codex-cli` terminology adapter uses the same authenticated, ephemeral, read-only,
credential-sanitized subprocess boundary and captures only its final structured JSON message.
Analysis runs retain source references and hashes rather than copied source prose or raw process
output. CLI summaries never print evidence excerpts or book text.
