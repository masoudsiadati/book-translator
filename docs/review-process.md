# Review process

Before declaring a task complete, Codex performs a final review and answers these questions with code,
tests, hashes, or command results where possible:

- What changed, and what requested outcome does it satisfy?
- Why was this design selected, and which canonical contracts or components were reused?
- Were reasonable alternatives considered without creating a competing architecture?
- Are there architectural deviations, and are they justified and documented?
- Is there a schema, workspace, CLI, migration, or stored-data compatibility impact?
- Could immutable source, translated, reviewed, approved, or final data be affected?
- Is deterministic behavior preserved and versioned correctly?
- Is the operation idempotent, and does a valid rerun skip safely?
- Can it recover after interruption without losing completed work?
- Does `--force` remain bounded by lineage and downstream safety?
- Are all lifecycle statuses and transitions correct?
- Are errors actionable without exposing user or book content?
- What time, memory, network, or batch-scale impact exists?
- What regression risk remains, and which tests cover it?
- What is the rollback or recovery path?
- Which plausible improvements were intentionally deferred?

## Final task report

The final report states, as applicable:

- files created or changed;
- production behavior added, fixed, or deliberately unchanged;
- unit, integration, regression, contract, and failure-injection tests added;
- complete regression and static-analysis results;
- benchmark method and observed results when materially relevant;
- schema, CLI, workspace, and stored-data compatibility;
- immutable-artifact verification when real data is in scope;
- risks, warnings, limitations, and rollback/recovery behavior;
- the next recommended step.

A report never substitutes for running the checks, and it never prints or summarizes book prose.
