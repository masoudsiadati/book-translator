# EPUB Ingestion Design

## Intent

Add a provider-free EPUB source boundary so an EPUB can populate the canonical per-book workspace
used by normalization and chunking. The first controlled use is Book 8, but all parsing,
classification, merging, and text extraction rules are format-generic.

## Goals

- Resolve the OPF through `META-INF/container.xml`, then use its manifest and spine as the only
  reading-order authority.
- Read NCX navigation when present and use paired structural/title labels to recover narrative
  titles.
- Discover prologue, numbered chapters, and epilogue; exclude front matter, maps, publication
  pages, glossaries, and about pages.
- Merge consecutive split spine resources with the same logical narrative identity.
- Convert HTML/XHTML blocks to canonical Markdown source while preserving paragraph order,
  punctuation, and inline emphasis.
- Publish the existing `source.md` and `metadata.yaml` chapter contract, then reuse unchanged
  normalization and chunking stages.
- Preserve existing PDF configuration and workspace behavior.

## Non-goals

- Translation, terminology, semantic QA, chapter-title translation, snapshots, or provider calls.
- EPUB rendering or repair.
- Inferring narrative order from ZIP member order or filenames.
- A migration of existing PDF artifacts.

## Constraints and anti-goals

- Book prose remains local and must never be logged or included in fixtures.
- Tests use synthetic EPUBs only.
- Import is deterministic, resumable, hash-tracked, and refuses conflicting or downstream state.
- `--force` cannot replace an imported source once normalized/chunk/translation artifacts exist.
- Existing Book 5, 6, and 7 files remain byte-identical.

## Approaches

1. Extend PDF extraction to dispatch by suffix. Rejected because ZIP/XML/HTML ingestion has no PDF
   page model and would couple two unrelated parsers.
2. Add a dedicated EPUB importer that adapts its output to canonical chapter artifacts. Selected:
   it isolates format parsing while reusing every downstream contract.
3. Build a parallel EPUB workspace format. Rejected because it would duplicate normalization,
   chunking, lineage, and recovery logic.

## Design

`book_translator.epub_ingestion` owns archive safety, container/OPF/NCX parsing, spine discovery,
logical-unit grouping, and HTML-to-Markdown extraction. It returns typed immutable plans before any
write. Narrative identity is derived from navigation labels where available, with normalized spine
resource stems as a conservative fallback for split resources; iteration order always remains the
OPF spine order. A split suffix such as `_split_001` affects grouping only, never ordering.

The HTML renderer treats paragraph-like and heading elements as blocks, ignores scripts, styles,
navigation, images, and empty decoration, and maps `em`/`i` to Markdown emphasis. Exact leading
structural headings and the recovered chapter title are removed as EPUB navigation decoration;
all remaining text is conserved apart from HTML whitespace normalization. Each contributing spine
resource gets an increasing canonical `source-page` marker so the established normalizer can retain
source-boundary provenance without a second source model.

`BookConfig` gains mutually exclusive optional `pdf_path` and `epub_path`; exactly one is required.
PDF-only commands resolve through an explicit helper and fail clearly for EPUB books. Existing PDF
YAML remains valid and unchanged. The canonical TOC gains an additive `epub_spine` source value and
generic source-document path/hash/count fields; legacy PDF fields remain unchanged. For EPUB TOC
entries, existing start/end page integers carry 1-based spine positions, matching the markers used
in imported source.

Publication order is TOC first, then per-unit source and metadata. Existing artifacts are validated
by source hash and structural metadata and skipped; conflicts fail closed. A partial import can be
rerun to complete missing units. Chapter source is published before metadata, and a failed metadata
write removes a newly created source so metadata never claims missing bytes.

## Risks and critique

- Filename fallback could misclassify unusual EPUBs. NCX labels take precedence, and unknown items
  are excluded rather than guessed.
- HTML has broad layout variability. The renderer supports semantic block tags and deliberately
  avoids CSS interpretation; unsupported nonempty structures fail tests as new fixtures arise.
- Reusing page markers for spine positions is an adapter compromise. It keeps normalization and
  lineage deterministic without introducing a competing source contract; documentation calls the
  values source positions for EPUB.
- OPF/NCX paths can be malicious. Archive members and resolved hrefs must stay relative, normalized,
  and within the archive namespace.

