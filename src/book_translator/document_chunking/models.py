"""Immutable models for generic document chunk planning."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    target_words: int
    max_words: int
    context_words: int
    minimum_preferred_fraction: float = 0.6
    scene_near_target_factor: float = 1.4

    def __post_init__(self) -> None:
        if self.target_words <= 0:
            raise ValueError("target words must be greater than zero")
        if self.max_words < self.target_words:
            raise ValueError("maximum words must be greater than or equal to target words")
        if self.context_words < 0:
            raise ValueError("context words must not be negative")
        if self.context_words >= self.max_words:
            raise ValueError("context words must be less than maximum words")
        if not 0 < self.minimum_preferred_fraction < 1:
            raise ValueError("minimum preferred fraction must be between zero and one")
        if self.scene_near_target_factor < 1:
            raise ValueError("scene target factor must be at least one")

    @property
    def minimum_preferred_words(self) -> int:
        return max(1, int(self.target_words * self.minimum_preferred_fraction + 0.999999))


@dataclass(frozen=True, slots=True)
class DocumentParagraph:
    sequence: int
    text: str
    word_count: int
    character_count: int
    byte_count: int
    is_scene_separator: bool
    is_multiline: bool


@dataclass(frozen=True, slots=True)
class ChunkContext:
    text: str
    word_count: int
    source_start_paragraph: int | None
    source_end_paragraph: int | None


@dataclass(frozen=True, slots=True)
class PlannedDocumentChunk:
    sequence: int
    paragraph_start: int
    paragraph_end: int
    paragraphs: tuple[DocumentParagraph, ...]
    source_text: str
    word_count: int
    oversize: bool
    ended_at_scene_boundary: bool
    context: ChunkContext
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentChunkPlan:
    config: ChunkingConfig
    paragraphs: tuple[DocumentParagraph, ...]
    chunks: tuple[PlannedDocumentChunk, ...]
    source_word_count: int
    scene_separators_found: int
    scene_boundaries_used: int
    tail_rebalanced: bool
    reconstructed_text: str
    warnings: tuple[str, ...]

    @property
    def oversize_chunk_count(self) -> int:
        return sum(chunk.oversize for chunk in self.chunks)

    @property
    def total_context_words(self) -> int:
        return sum(chunk.context.word_count for chunk in self.chunks)
