# Data model

The authoritative Python contracts are in `src/book_translator/models/`; machine-readable
counterparts are in `schemas/`. Models reject unknown fields. Paths stored in metadata are
project-relative canonical paths, use `/` separators when serialized, and must not escape the
project root.

## Documents

- `workspace/metadata.yaml` — `BookWorkspaceMetadata`: schema version, stable book ID, PDF path
  and hash, titles, author, languages, expected output, and timestamps.
- `workspace/toc.yaml` — `TocDocument`: schema version, embedded-source provenance (source PDF,
  byte-exact hash, PDF page count, and entry count), and ordered normalized `TocEntry` values.
  Pages are inclusive and 1-based. Kinds are `front_matter`, `prologue`, `chapter`, `appendix`,
  and `unknown`. `source_toc_title` preserves the embedded TOC label with surrounding whitespace
  removed. Entries with the same start page share an end page determined by the next later
  distinct start page; the final distinct group ends at the PDF page count.
- Chapter `metadata.yaml` — `ChapterMetadata`: identity and titles, inclusive PDF boundaries,
  source/cleaned paths and hashes, counts, lifecycle status, warnings, algorithm versions, and
  timestamps.
- Chapter `normalization-report.yaml` — compact schema 1.0 provenance: chapter and source/cleaned
  hashes, algorithm version, ordered passes, physical-line and paragraph counts, page transitions,
  aggregate marker/join/hyphen/ambiguity counters, warnings, timestamp, and structural change records.
  It stores no book paragraphs or snippets.
- Chapter `chunking-report.yaml` — Document Chunk Engine schema 1.0 lineage, configuration, source and
  chunk counts, scene/tail decisions, oversize warnings, context totals, reconstruction hash, and
  conservation result. It contains no text snippets.
- Chunk `metadata.yaml` — `ChunkMetadata`: identity and order, artifact paths and hashes, counts,
  lifecycle status, attempt counts, provider/model/version provenance, error, and timestamps.
- `workspace/snapshot.yaml` — `ExtractionSnapshot`: snapshot/book IDs, PDF and normalized TOC
  hashes, algorithm versions, count, source and cleaned hashes for every chapter, creation time,
  validation flag, and warnings.

Timestamps are timezone-aware ISO 8601 values in serialized YAML/JSON. Hashes are lowercase,
64-character SHA-256 hex strings. IDs are lowercase filesystem-safe ASCII matching
`[a-z0-9][a-z0-9._-]*`. Deterministic chapter IDs equal their directory name; chunk IDs are
`<chapter-id>-<three-digit-sequence>`. Titles, including Persian text, live only in metadata.

Chapter extraction counts source words by splitting each nonempty extracted PyMuPDF text block
on Unicode whitespace; Markdown page-marker comments are never counted. The source paragraph
count is the number of nonempty extracted text blocks, not a linguistic paragraph inference.
The `source_sha256` is calculated from the exact final persisted UTF-8 `source.md` bytes after
the atomic source write. At extraction time `cleaned_file` records its canonical future path,
while cleaned hashes/counts are null and `normalization_version` is empty.

Normalization counts nonblank text physical lines and excludes marker comments. The cleaned paragraph
count is the number of source blocks represented in output; blocks are never merged. Cleaned words are
the final Markdown split on Unicode whitespace. The cleaned SHA-256 covers exact persisted UTF-8 bytes,
including one final newline. Marker removals, wrapped joins, and hyphen repairs count individual
structural operations, and reported page transitions remain 1-based.

Success updates cleaned path/hash/counts, algorithm version, warnings, `status: normalized`, and
`updated_at`. It preserves source metadata/hash/counts, extraction version, and `created_at`. These use
existing schema 1.0 fields, so no stored-data migration is required.

Chunking treats each canonical blank-line-separated cleaned block as one paragraph. Paragraph and
chunk word counts split on Unicode whitespace; context counts are separate. Chunk `source.md` preserves
exact paragraph text with canonical separators and a final newline. `context.md` is empty for the first
chunk and otherwise contains only preceding text; it is never translated as chunk source and does not
participate in reconstruction.

`ChunkMetadata` records source/context paths and hashes, source/context counts, optional context
paragraph provenance, cleaned-source lineage, engine version, oversize state, warnings, and future
translation paths without creating `translation.md`. Chunk IDs retain the canonical
`<chapter-id>-<three-digit-sequence>` form. Successful chapter publication sets `chunk_count`,
`chunk_engine_version`, `status: chunked`, and `updated_at` while preserving extraction and
normalization lineage and `created_at`.

### Compatible chunk metadata extension

Document Chunk Engine 1.0.0 adds defaulted fields to schema 1.0: chunk context hash/count/range,
chunk-engine version, cleaned-source hash, oversize flag, and warnings, plus a defaulted chapter
chunk-engine version. This is additive and existing stored chapter/chunk metadata remains valid; no
rewrite or data migration is required. The committed schemas are regenerated from the canonical
models. Removing or making these fields required later would require a breaking migration.

## Artifact mutability

- Chapter `source.md` is conservative raw extraction. After a successful atomic write it is
  immutable to normalization, translation, and review. Replacement requires an explicit forced
  extraction rerun.
- `cleaned.md` derives only from the recorded `source.md` hash.
- Chunk `source.md` derives only from the recorded chapter `cleaned.md` hash; `context.md` is
  supporting input and not translated as source.
- Chunk `translation.md` is a draft until its chunk status is `approved`.
- Chapter `final.md` is assembled only when every included chunk is approved and hash-current.
- Failed or review-required records retain all previous artifacts.

## Schema versioning

Version `1.0` uses `major.minor`. An additive optional field or newly accepted enum value may be
a compatible minor change, provided old readers and writers remain safe. Removing or renaming a
field, changing meaning/type, making an optional field required, or changing stored layout is a
breaking major change.

Existing data is never silently rewritten. Breaking changes require a documented, explicit,
idempotent, testable migration with backup/recovery guidance before consumers change. Future
commands must accept supported older versions deliberately and refuse unsupported newer
versions. No migration engine is implemented yet. The legacy `state/progress.json` model remains
unchanged and powers the existing `status` CLI; it is not a canonical workspace document.

The optional TOC provenance fields are a compatible additive clarification of schema 1.0.
Writers populate them; their defaults preserve validation compatibility with earlier schema 1.0
TOCs containing only `schema_version` and `entries`.
