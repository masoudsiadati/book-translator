# Release process

The application follows semantic versioning and stores its release version in `VERSION`,
`pyproject.toml`, and `book_translator.__version__`. A contract test keeps these copies synchronized;
the build does not derive a version from Git.

## Application versions

- **Patch**: compatible bug fixes, tests, documentation, and internal improvements.
- **Minor**: a new pipeline stage or compatible user-facing capability.
- **Major**: a breaking CLI, schema, workspace, stored-data, or supported-migration change.

Pre-1.0 minor releases may establish substantial compatible capabilities, but stored-data and
immutability guarantees still apply. Breaking changes always require explicit migration planning; a
pre-1.0 version is not permission to rewrite user data silently.

## Independent engine versions

Each pipeline engine has a version independent of the application version. New engines use full
semantic versions, for example extraction engine `1.0.0`, normalization engine `1.0.0`, future
Document Chunk Engine `1.0.0`, and future translation engine `1.0.0`.

Existing extraction and normalization artifacts currently persist the historical two-component
engine version `1.0`. This is a harmless legacy representation already protected by stale detection;
it must not be rewritten solely for formatting. The next intentional algorithm-version change should
adopt a documented semantic version and handle existing artifacts explicitly.

For engine algorithms:

- **Patch**: a bug fix that restores already expected behavior and does not intentionally redefine
  the artifact contract.
- **Minor**: a compatible enhancement that may intentionally change derived bytes; existing artifacts
  become stale and require explicit regeneration under stage safety rules.
- **Major**: an incompatible input/output, report, metadata, identifier, or artifact-format contract.

Pass versions may remain independently explicit where a composite engine records pass provenance.

## Release checklist

Before a release:

1. Install the project and development dependencies from the candidate tree.
2. Run all tests.
3. Run Ruff linting.
4. Run the Ruff formatting check.
5. Run strict mypy.
6. Run relevant benchmarks.
7. Confirm documentation is current and links resolve.
8. Confirm committed JSON Schemas match the canonical Pydantic models.
9. Document every required migration and its recovery path.
10. Update `CHANGELOG.md`.
11. Update and synchronize `VERSION`, `pyproject.toml`, and package `__version__`.
12. Review a clean, scoped Git diff and verify immutable real artifacts when they were in scope.
13. Have the user manually commit and create the release tag.

Codex does not create a commit, Git tag, published package, or release without explicit user
instruction. Release tags use `v<application-version>`, such as `v0.5.0`.
