"""Public pure API for the generic deterministic Document Chunk Engine."""

from book_translator.document_chunking.models import ChunkingConfig, DocumentChunkPlan
from book_translator.document_chunking.paragraphs import parse_document, render_paragraphs
from book_translator.document_chunking.planning import build_chunks

CHUNK_ENGINE_NAME = "document-chunk-engine"
CHUNK_ENGINE_VERSION = "1.0.0"


class ChunkConservationError(ValueError):
    """Raised when planned chunks do not reconstruct the canonical input exactly."""


def reconstruct_document(chunk_sources: tuple[str, ...]) -> str:
    if not chunk_sources:
        raise ChunkConservationError("cannot reconstruct a document without chunks")
    paragraphs = tuple(
        paragraph for source in chunk_sources for paragraph in parse_document(source)
    )
    return render_paragraphs(paragraphs)


def plan_document(text: str, config: ChunkingConfig) -> DocumentChunkPlan:
    paragraphs = parse_document(text)
    chunks, tail_rebalanced, warnings = build_chunks(paragraphs, config)
    reconstructed = reconstruct_document(tuple(chunk.source_text for chunk in chunks))
    if reconstructed.encode("utf-8") != text.encode("utf-8"):
        raise ChunkConservationError("chunk reconstruction does not match cleaned input bytes")
    return DocumentChunkPlan(
        config=config,
        paragraphs=paragraphs,
        chunks=chunks,
        source_word_count=sum(paragraph.word_count for paragraph in paragraphs),
        scene_separators_found=sum(paragraph.is_scene_separator for paragraph in paragraphs),
        scene_boundaries_used=sum(chunk.ended_at_scene_boundary for chunk in chunks),
        tail_rebalanced=tail_rebalanced,
        reconstructed_text=reconstructed,
        warnings=warnings,
    )
