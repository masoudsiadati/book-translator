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

The prologue name and ID are `000-prologue`. Numbered chapter names/IDs are exactly three digits
(`001` through `999`); this book is expected to use `001` through `056`. Chunk directories are
exactly three digits (`001` through `999`), and chunk IDs combine chapter ID and sequence, for
example `042-003` or `000-prologue-001`. Invalid, zero, negative, missing, or greater-than-999
numbered values are rejected. Machine names contain only safe ASCII; English and Persian titles
belong in metadata.

The Document Chunk Engine creates `metadata.yaml`, `source.md`, and `context.md` only. The
`translation.md` path is reserved in metadata for a future stage and is not pre-created.

All paths are derived by `ProjectPaths`, relative to its project root, with no creation side
effect. Persisted paths are project-relative rather than machine-specific absolute paths.
