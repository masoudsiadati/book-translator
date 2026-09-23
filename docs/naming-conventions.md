# Naming and workspace conventions

The future canonical layout is:

```text
workspace/
├── metadata.yaml
├── toc.yaml
├── snapshot.yaml
└── chapters/
    ├── 000-prologue/
    │   ├── metadata.yaml
    │   ├── source.md
    │   ├── cleaned.md
    │   ├── chunking-report.yaml
    │   ├── chunks/
    │   │   └── 001/
    │   │       ├── metadata.yaml
    │   │       ├── source.md
    │   │       ├── context.md
    │   │       └── translation.md
    │   ├── review/
    │   │   ├── report.yaml
    │   │   └── unresolved-terms.yaml
    │   └── final.md
    ├── 001/
    ├── 002/
    └── ...
```

Do not pre-create chapter directories. Operations create only required parents at write time.
The legacy top-level artifact directories are temporary compatibility locations, not alternatives
to this layout.

The prologue name and ID are `000-prologue` and the epilogue name and ID are `999-epilogue`;
both are narrative units alongside the numbered chapters and sort first and last respectively.
Numbered chapter names/IDs are exactly three digits (`001` through `999`); the first book uses
`001` through `056`. Chunk directories are exactly three digits (`001` through `999`), and chunk
IDs combine chapter ID and sequence, for example `042-003`, `000-prologue-001`, or
`999-epilogue-001`. The shared selector forms are `prologue`/`0`, `epilogue`, `N`, and
`chapter-N` (`paths.chapter_id_from_selector`). Invalid, zero, negative, missing, or greater-than-999
numbered values are rejected. Machine names contain only safe ASCII; English and Persian titles
belong in metadata.

The Document Chunk Engine creates `metadata.yaml`, `source.md`, and `context.md` only. The
`translation.md` path is reserved in metadata for a future stage and is not pre-created.

Mutable regeneration uses sibling `.regeneration-staging.<random>/` directories and fixed
`.regeneration-backup-{metadata.yaml,cleaned.md,normalization-report.yaml,chunks,chunking-report.yaml}`
paths inside one chapter directory. Staging directories are never canonical inputs. Any surviving
backup is explicit recovery evidence and blocks another apply until diagnosed.

Translation snapshots live at
`workspace/snapshots/translation/translation-<target-language>-<24-hex-digest>/` and contain only
`snapshot.yaml`, `manifest.yaml`, and `validation-report.yaml`. The complete 64-character manifest
hash is persisted. Snapshot manifests reference canonical artifacts and never copy chunk prose.

Translation runs live at `workspace/translation-runs/tr-<12-manifest-hash-chars>-<sequence>/`.
Sequences are zero-padded, monotonic within the output root, and never timestamp-derived. Job
directories use the snapshot chunk ID because it is unique within a run; job IDs are
`<run-id>__<chunk-id>`. Hidden `.allocation.lock` and `.queue.lock` files coordinate local processes
and contain no content or credentials.

An eligible job may contain `prompt-package/` with `package.yaml`, `system.md`, `user.md`, and
`validation-report.yaml`. Package IDs are `pp-<24-lowercase-hex>` from the complete canonical
identity hash. Persisted message paths remain project-relative. Hidden prompt-package staging and
backup names are stage-owned recovery evidence, not usable packages.

Provider attempts live under `jobs/<chunk-id>/attempts/<three-digit-attempt>/` and contain
`attempt.yaml`, `request.json`, optional `response.json`, optional `translation.md`, optional
`rejected-output.md` (the private refused candidate of a failed attempt, never a translation), and
`validation-report.yaml`. Attempt IDs are `<job-id>__attempt-<three digits>`. Fixed hidden
`.NNN.reservation.yaml` files are private execution ownership evidence and are never provider inputs.

Worker acceptance evidence lives separately under
`jobs/<chunk-id>/acceptance/<three-digit-attempt>/evidence.yaml`. The directory and file are private.
Evidence references and hashes the immutable provider attempt, translation, frozen source, and frozen
terminology resource; it does not replace or add files to the attempt directory.

Explicit operator resolutions of incomplete executions use the job-local immutable path
`jobs/<chunk-id>/resolutions/<three-digit-attempt>/resolution.yaml`, numbered by the attempt slot
they retire. Resolution IDs are `xrr-<24-lowercase-hex>`, derived from the resolver engine version,
run/job/attempt identity, the reservation's exact bytes, the reserved request hash, and the selected
resolution — so the same assertion about the same reservation always yields the same identifier and a
different assertion cannot hide behind it. The resolved `.NNN.reservation.yaml` is preserved verbatim.

QA re-evaluations use the job-local immutable path
`jobs/<chunk-id>/reevaluations/<three-digit-sequence>/evidence.yaml`. The sequence is monotonic and
independent of provider attempt numbers. Re-evaluation IDs are `qre-<24-lowercase-hex>`, derived from
the exact historical lineage and QA engine versions.

All paths are derived by `ProjectPaths`, relative to its project root, with no creation side
effect. Persisted paths are project-relative rather than machine-specific absolute paths.

## Book workspaces

The layout above is one *book workspace*. The legacy root `workspace/` paired with
`config/book.yaml` is the first book's workspace and is never relocated. Every further book lives
at `workspace/books/<book-id>/` paired with `config/books/<book-id>.yaml`, with exactly the same
layout beneath it (`toc.yaml`, `metadata.yaml`, `chapters/`, `audits/`, `resources/`, `snapshots/`,
`translation-runs/`, …). A book workspace ID is filesystem-safe lowercase ASCII
(`^[a-z0-9][a-z0-9._-]*$`) and is chosen by the operator; it is independent of the bibliographic
`book_id` resolved into `metadata.yaml`. `workspace/books/` itself is only a container and is never
scanned as a workspace. Selection is `ProjectPaths(project_root, book)` in code and the shared
`--book <book-id>` option on the CLI; persisted project-relative paths such as
`workspace/books/lord-of-chaos/chapters/000-prologue/source.md` name their workspace, and
`paths.book_workspace_of` recovers it from any such path.

The current terminology registry and metadata are `config/terminology.yaml` and
`config/terminology.metadata.yaml`. Candidate IDs are `tc-<24-hex>` and canonical decision IDs are
`td-<24-hex>`. Occurrences use canonical chapter/chunk IDs and never embed prose.

Exact bytes frozen by new translation snapshots live at
`workspace/resources/<category>/<64-hex-sha256><suffix>`, where the suffix is the source file's own
extension. The filename is the byte-exact content hash; files at this path are immutable and shared
by every snapshot with identical bytes. The categories are `prompts/`, `glossaries/`,
`style-guides/`, `terminology/`, `source-anomalies/`, `open-questions/`, `book-metadata/`,
`series-terminology/`, `reference-corpus/`, and `reference-context-index/`. Per-chunk reference
packages are born immutable under `reference-context/<64-hex-sha256>.yaml`.

Series terminology configuration lives at `config/series/<series-id>/terminology-policy.yaml` and
`config/series/<series-id>/reference-corpus.yaml`, each with a `.metadata.yaml` sidecar. Series
concept IDs are `sc-<24-hex>`. A book's derived registry is
`workspace/books/<book>/terminology/registry.yaml` and its reference-context index is
`workspace/books/<book>/reference-context/index.yaml`.

The canonical resolved book metadata record is `workspace/metadata.yaml`. Its `book_id` is stable and
filesystem-safe (`^[a-z0-9][a-z0-9._-]*$`), normally `slug(title_en)-<source>-<target>`.

Book identity migrations live at `workspace/migrations/book-identity/bim-<24-hex>.yaml`. The ID is
content-addressed over both book identities and the exact pre-migration bytes of every registry it
touches, so replaying the same migration yields the same identifier.

Immutable terminology analysis runs live under `workspace/terminology-runs/ta-<24-hex>/` and contain
`run.yaml`, `request.json`, `output.yaml`, and `validation-report.yaml`. Discovery and proposal IDs
share the request-content-addressed `ta-` namespace; proposal IDs are `tp-<24-hex>`.
## Translation reuse

- Reuse evidence: `workspace/translation-runs/<run-id>/jobs/<chunk-id>/reuse/<NNN>/evidence.yaml`
- Reused translation: the same directory's `translation.md`
- Current implementation uses monotonic reuse sequence `001`; evidence declares
  `execution_kind: reuse` and never uses a provider attempt directory.

## Historical rejected-output carry-forward

- Carry-forward evidence:
  `workspace/translation-runs/<run-id>/jobs/<chunk-id>/carry-forward/<NNN>/evidence.yaml`
- Carried-forward translation: the same directory's `translation.md`
- Current implementation uses monotonic carry-forward sequence `001`; evidence declares
  `execution_kind: historical_reevaluation` and `provider_contacted: false`, and never uses a
  provider attempt directory. The directory name is deliberately distinct from `reuse/`, because the
  two record different provenance: reuse carries an accepted translation, carry-forward carries a
  rejected one together with the rejection it is being re-evaluated against.

## Literary editing runs

Chapter-level literary derivatives live at
`workspace/literary-runs/lr-<24-lowercase-hex>/`. Identity covers engine, prompt, schema and policy
versions; input run/snapshot; selected provider and requested model (or the explicit provider-selected
sentinel); frozen style/profile hashes; historical comparison lineage; and ordered selected chapter
source/faithful hashes. Explicit chapter reuse also contributes the source literary-run identity and
each carried chapter-artifact hash. The hidden sibling `.lr-<id>.staging` is recoverable publication state, never a
canonical input.

Each `chapters/<chapter-id>/` contains private `chapter.md`, prose-free `chapter.yaml`, structured
normalized editor responses under `editor/`, post-edit semantic calls under `semantic/`, correction
responses under `corrections/`, and `continuity.yaml`. Chapter paragraph IDs are global within that
chapter (`P0001`, `P0002`, …); records also retain the original chunk ID and chunk-local paragraph ID.
Semantic evidence is monotonically numbered with three digits. Blind material lives under `blind/` as
`comparison.md`, `manifest.yaml`, and the deliberately separate `answer-key.yaml`.
