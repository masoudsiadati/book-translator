# Developer charter

Codex acts as a senior development partner for `book-translator`, not as a code generator. The goal is
to deliver coherent, evidence-backed changes that preserve the project's architecture, data, and
recoverability. Feature prompts may define the desired outcome; this charter supplies the permanent
development process unless a prompt explicitly imposes stricter requirements.

## Development lifecycle

Every nontrivial task follows this lifecycle:

1. Understand the requested outcome and explicit exclusions.
2. Inspect the current architecture, implementation, tests, and relevant stored contracts.
3. Identify canonical components that can be reused.
4. Propose the design and internal API.
5. Critique the proposal for unnecessary coupling, duplication, and hidden assumptions.
6. Identify correctness, integrity, compatibility, recovery, security, and performance risks.
7. Implement the smallest coherent change.
8. Add focused unit tests.
9. Add integration tests where orchestration or persistence changes.
10. Run all existing tests as regression tests.
11. Run static analysis, linting, and formatting checks.
12. Benchmark when the change can materially affect performance or resource use.
13. Update the relevant documentation and contracts.
14. Perform a final self-review against `docs/review-process.md`.
15. Produce a structured final report.

Codex must not begin a large implementation until the design is internally coherent. For a small,
obvious change, the design and critique may be brief, but they are still performed mentally and the
change remains proportional to the outcome.

## Architecture first

- Read and follow `ARCHITECTURE.md` and the relevant stage documentation before nontrivial work.
- Do not create competing workspace layouts, artifact trees, or duplicate domain models.
- Reuse canonical path helpers, byte-exact hashes, Pydantic models, schemas, and transition policies.
- Update documentation and tests whenever a persisted or user-facing contract changes.
- Require an explicit, documented, idempotent migration before a breaking stored-data change.

`ARCHITECTURE.md`, `docs/data-model.md`, `docs/pipeline.md`, `docs/recovery.md`, and
`docs/naming-conventions.md` remain authoritative for product contracts. This charter governs the
development process and does not replace them.

## Separation of concerns

Do not combine file I/O, parsing, business transformation, validation, state transitions,
persistence, and CLI rendering in one unit. Prefer pure typed domain logic and keep orchestration at
system boundaries. A cohesive boundary module may coordinate these concerns, but it should delegate
their implementations rather than embed them all.

## Determinism

For a deterministic stage, identical input bytes, metadata, and engine versions must produce
identical output bytes and structural counters. Outputs must not accidentally depend on absolute
machine paths, current time, random or filesystem ordering, locale, network state, or concurrency
timing. Any nondeterministic value that belongs in a persisted contract, such as a publication
timestamp, must be explicit and injectable where tests benefit. IDs, paths, serialization order, and
field order remain stable.

## Idempotency and resumability

- Rerunning a complete, valid stage safely skips it.
- Partial output sets are detected and reported explicitly.
- Incomplete or mismatched output is never silently treated as complete.
- Batch preflight finds known blockers before publication begins.
- An interrupted run preserves earlier successful artifacts and supports an explicit recovery path.

## Atomicity and integrity

- Multi-file publication follows the stage's documented safe order.
- Persisted artifacts use atomic writes in their destination filesystem.
- Derived metadata records byte-exact hashes of its inputs and outputs as required by the contract.
- Metadata never claims validity for a missing, partial, stale, or hash-mismatched artifact.
- Large artifacts are hashed with streamed reads.

## Immutability and force behavior

- Extracted `source.md` artifacts are immutable by default.
- Approved translations and final artifacts are immutable by default.
- Destructive invalidation requires a future explicit command with recorded intent and recovery.
- `--force` regenerates only the artifacts owned by its stage and never bypasses downstream safety
  checks.

## Lifecycle correctness

All state changes use the canonical pure transition policies. Processing completion is not review;
review is not approval. Translation completion never implies approval, and approval shortcuts are
prohibited. Status changes occur only after the artifacts required by that state are durably valid.

## Data preservation

- Preserve UTF-8 and Persian text exactly except for transformations explicitly owned by a stage.
- Do not normalize, summarize, or rewrite user or book content during cleanup or refactoring.
- Never delete or replace real source, translated, reviewed, or approved content without explicit
  authority and the contractually required safety checks.
- Tests use generated synthetic content unless the user explicitly authorizes a controlled real-data
  pilot. Real book prose is not committed as a fixture or printed in logs and reports.

## External services

Book content may be sent only through an explicitly configured and approved translation provider. No
new telemetry, API, MCP server, cloud dependency, or external tool may receive book content without
explicit approval. Deterministic local stages do not access external services.

## Related governance

- Implementation conventions: `docs/coding-standards.md`
- Test strategy: `docs/testing.md`
- Completion review: `docs/review-process.md`
- Performance evidence: `docs/benchmark.md`
- Versioning and release: `docs/release-process.md`
