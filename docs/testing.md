# Testing policy

Tests use synthetic content by default, remain deterministic, and do not depend on network access,
machine-specific absolute paths, or the real book PDF. Every feature change runs the complete existing
suite as regression coverage.

## Unit tests

Unit tests cover parsers, selectors, pure transformations, lifecycle rules, counting, hashing, path
generation, validation, deterministic identifiers, and serialization helpers. They should identify
one contract or edge case clearly and avoid unnecessary filesystem setup.

## Integration tests

Integration tests cover CLI orchestration, metadata and artifact publication, atomic writes, skip and
force behavior, recovery, safe publication ordering, and interaction between pipeline stages. They
use temporary workspaces and generated PDFs or text.

## Regression tests

Every proven bug fix includes the smallest test that fails before the fix and passes afterward. Never
delete or weaken a regression merely because an implementation changes; update it only when the
underlying contract intentionally changes and that impact is documented.

## Contract tests

Contract tests verify committed JSON Schemas against Pydantic models, model round trips, field and enum
compatibility, canonical paths, CLI command compatibility, application and engine versions, strict
unknown-field behavior, deterministic output, and governance files that are meaningful to check
mechanically. Documentation tests assert stable links or headings rather than entire prose documents.

## Failure-injection tests

Where practical, inject write failure, metadata publication failure, provider failure, interrupted
processing, invalid hashes, stale artifacts, and partial workspace state. Verify both the immediate
error and the next-run recovery behavior. Provider failure tests remain synthetic and local.

## Real-data pilot progression

A new major stage does not run across the complete real book immediately. Use this progression:

1. synthetic unit and integration tests;
2. one small controlled real artifact;
3. selected representative chapters with explicit approval;
4. a read-only full-book dry run;
5. a full batch with explicit approval.

Real book prose is never committed as a fixture or printed in terminal output, logs, test failures, or
reports. Hashes, counts, identifiers, and structural warnings are appropriate audit output.

## Required checks

After production changes, run:

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src tests
```

Also run affected CLI commands, schema synchronization checks, and benchmarks required by
`docs/benchmark.md`.
