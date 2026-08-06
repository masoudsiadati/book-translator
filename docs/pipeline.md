# Canonical pipeline

The ordered pipeline is:

1. Inspect the PDF without emitting book text.
2. Discover chapters from the embedded TOC and persist a normalized TOC.
3. Extract each chapter to immutable `source.md` using inclusive 1-based boundaries.
4. Normalize each source into `cleaned.md` without changing `source.md`.
5. Validate normalized structure, counts, lineage, and hashes.
6. Split hash-current normalized chapters into deterministic generic document chunks.
7. Freeze and validate a future extraction/chunk snapshot before external processing.
8. Translate chunks and record provider, model, prompt, glossary, hashes, and attempts.
9. Validate translations; failures become flagged work, never approval.
10. Review and correct flagged chunks.
11. Merge only approved, hash-current chunks into chapter `final.md`.
12. Build an RTL EPUB 3 from final workspace chapters and metadata.
13. Validate the EPUB structure and conformance.

Stages 1–5 may access the PDF as required. Chunking and every later stage operate only on workspace
Markdown and metadata and must not reopen the PDF. Future external/translation stages must reference a
validated snapshot ID; snapshot support is not implemented yet.

Stage 3 reads boundaries exclusively from canonical `workspace/toc.yaml` and verifies its PDF
hash and page-count provenance before a batch begins. It preserves ordered PDF text blocks and
page markers with only newline, non-breaking-space, trailing-whitespace, and excessive-blank-line
mechanical normalization; prose cleanup belongs to stage 4.

Stage 4 is deterministic and non-AI. Its fixed pass order is:

1. Parse canonical page markers, pages, extracted blocks, and physical lines; reject malformed
   canonical-looking markers and retain 1-based transitions for reporting.
2. Repair an ASCII hyphen only at a physical line boundary with a nonblank continuation in the same
   block. This occurs before any line flattening.
3. Join visually wrapped prose with one space, only inside one extracted source block. Preserve page
   and block boundaries and conserve poetry-like short-line blocks.
4. Render logical blocks with exactly one blank line between them and one final newline.
5. Validate hashes, block correspondence, marker removal, characters, punctuation, newlines, and the
   pure `extracted → normalized` lifecycle transition.

This stage does not remove headers or footers, infer heading markup, rewrite semantics, detect
speakers, or perform sophisticated poetry classification. Ambiguous short-line blocks yield warnings.

## Document Chunk Engine

The generic Document Chunk Engine 1.0.0 consumes only canonical `cleaned.md` and matching chapter
metadata. It parses exact blank-line-separated Markdown paragraphs and never classifies semantics,
splits a paragraph, changes paragraph order, or discards a separator.

Planning considers every paragraph boundary that keeps a normal chunk at or below the maximum. An
explicit standalone separator (`***`, `* * *`, `# # #`, or `— — —`) is preferred as the chunk end
when the resulting chunk is between 60% and 140% of target, bounded by the maximum. Otherwise the
closest boundary to target is chosen; ties prefer the larger chunk, then source order. A paragraph
larger than the maximum becomes one warned oversize chunk.

After initial planning, if the final normal chunk is below 60% of target, only the final two chunks are
considered for deterministic rebalance. Whole paragraphs may move when both results stay within the
maximum and the smaller result strictly improves; candidates prefer both chunks above the minimum,
then minimum total distance from target. No semantic or random optimization occurs.

Chunk sources use one blank line between original paragraphs and one final newline. Concatenating the
parsed paragraphs from chunk sources and rerendering by this rule must reproduce `cleaned.md` exactly,
including its SHA-256. Context is derived only from preceding paragraphs, newest first for selection
but restored to source order: complete paragraphs are preferred, then a whitespace-word tail from the
next older paragraph fills the remaining allowance. Context never affects source counts or
reconstruction.

Chunking validates the existing `normalized → validated → chunked` transition path during preflight
and publishes only the final `chunked` state after artifacts pass conservation and hash validation. It
does not advance to translation.

## Lifecycle

Normal chapter flow is `discovered → extracted → normalized → validated → chunked → translating
→ translated → review_required → reviewed → approved`. Normal chunk flow is `pending →
translating → translated → review_required → reviewed → approved`. Translation completion does
not imply approval. Validation may move a translated chunk to `validation_failed`, then to
`review_required`.

Active processing states may move to `failed`. Explicit retries allow chapter `failed →
extracted` or `failed → normalized`, and chunk `failed → pending`. A reviewed artifact may return
to `review_required`. Idempotent same-state checks are accepted. Direct `translated → approved`,
`pending → approved`, `extracted → approved`, and all other skipped prerequisite transitions are
forbidden. Approved is terminal until a future explicit invalidation operation is defined.

The exact pure policy is `src/book_translator/status_transitions.py`. Status changes never delete
artifacts. Chapter `final.md` cannot be generated if any selected chunk is unapproved.

## Snapshot boundary

A validated snapshot freezes the PDF hash, normalized TOC and page boundaries, every chapter's
source and cleaned hashes, and extraction/normalization algorithm versions. Translation runs
must record the snapshot ID. Any PDF, TOC, boundary, algorithm, source, or cleaned-content change
requires a new snapshot ID; an old snapshot is never silently modified.
