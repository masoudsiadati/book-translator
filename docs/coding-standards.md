# Coding standards

These standards apply to production code, tests, scripts, and persisted serialization unless a more
specific frozen contract says otherwise.

## Python and typing

- Support Python 3.12 or newer as declared by `pyproject.toml`.
- Add complete type annotations to production functions, methods, and public constants where useful.
- Keep strict mypy clean. Narrow third-party exceptions belong in explicit mypy overrides.
- Use Pydantic models at persisted serialization and external validation boundaries.
- Prefer frozen dataclasses or other typed immutable structures for internal plans, parsed data, and
  pure transformation results where mutation is unnecessary.
- Use `pathlib.Path` rather than ad hoc path strings. Persist project-relative POSIX paths through the
  canonical path helpers.

## Module design

- Keep parsing and transformations pure where practical.
- Give functions and modules a bounded, readable responsibility.
- Prefer clear domain names over abbreviations.
- Avoid hidden global mutable state and import-time side effects.
- Use explicit domain exception classes for expected stage failures.
- Do not swallow broad exceptions. Boundary orchestration may catch broadly only to preserve
  transactional recovery and must wrap the original cause in an actionable domain error.
- Do not use `print()` in library code. Rich belongs only at CLI presentation boundaries. Standalone
  developer scripts may print their own aggregate results and must not print book content.
- Inject time, providers, filesystem operations, and failure points when doing so materially improves
  deterministic or recovery testing.

Comments explain why a choice exists, which invariant it protects, or why a non-obvious constraint is
necessary. They do not narrate straightforward code.

## Tooling and dependencies

- Keep Ruff linting and Ruff formatting clean.
- Do not add a dependency when the standard library or an existing dependency is sufficient.
- New dependencies require a clear ownership, security, reproducibility, and maintenance rationale.
- Keep engine and pass version constants explicit and stable.

## Serialization and artifacts

- Serialize YAML and JSON deterministically with documented key ordering and a single final newline.
- Preserve Unicode; never rely on locale-dependent encoding.
- Persist canonical project-relative paths with `/` separators.
- Publish files atomically using the shared atomic-write utility unless a format requires a documented
  equivalent.
- Use streamed hashing for large files and compare hashes before accepting derived artifacts.

## Engine interface convention

A new engine normally separates these conceptual concerns:

1. input loading and prerequisite validation;
2. pure planning and stale/conflict classification;
3. pure transformation;
4. result and invariant validation;
5. ordered atomic artifact publication;
6. CLI orchestration and aggregate rendering.

An engine has a stable name, an independently managed semantic version, typed inputs and outputs,
deterministic identifiers, structured counters and warnings, explicit stale detection, documented
overwrite/recovery behavior, and a dry-run or pure planning path where meaningful.

This is a convention, not a universal inheritance hierarchy. Do not introduce a base class or engine
framework merely to make implementations look uniform. Reuse small established components and allow
stage-specific plans and results when their contracts differ.
