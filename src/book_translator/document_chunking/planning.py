"""Pure deterministic paragraph-boundary chunk planning."""

from dataclasses import dataclass

from book_translator.document_chunking.context import build_context
from book_translator.document_chunking.models import (
    ChunkingConfig,
    DocumentParagraph,
    PlannedDocumentChunk,
)
from book_translator.document_chunking.paragraphs import render_paragraphs


@dataclass(frozen=True, slots=True)
class _Boundary:
    start: int
    end: int


def _word_count(paragraphs: tuple[DocumentParagraph, ...], start: int, end: int) -> int:
    return sum(paragraph.word_count for paragraph in paragraphs[start:end])


def _choose_boundary(
    paragraphs: tuple[DocumentParagraph, ...], start: int, config: ChunkingConfig
) -> int:
    total = 0
    candidates: list[tuple[int, int]] = []
    for end in range(start + 1, len(paragraphs) + 1):
        total += paragraphs[end - 1].word_count
        if total > config.max_words:
            break
        candidates.append((end, total))
    if not candidates:
        return start + 1
    if candidates[-1][0] == len(paragraphs):
        return len(paragraphs)
    scene_upper = min(config.max_words, int(config.target_words * config.scene_near_target_factor))
    scene_candidates = [
        candidate
        for candidate in candidates
        if paragraphs[candidate[0] - 1].is_scene_separator
        and config.minimum_preferred_words <= candidate[1] <= scene_upper
    ]
    pool = scene_candidates or candidates
    return min(pool, key=lambda item: (abs(item[1] - config.target_words), -item[1], item[0]))[0]


def _initial_boundaries(
    paragraphs: tuple[DocumentParagraph, ...], config: ChunkingConfig
) -> list[_Boundary]:
    boundaries: list[_Boundary] = []
    start = 0
    while start < len(paragraphs):
        if paragraphs[start].word_count > config.max_words:
            end = start + 1
        else:
            end = _choose_boundary(paragraphs, start, config)
        boundaries.append(_Boundary(start, end))
        start = end
    return boundaries


def _rebalance_tail(
    paragraphs: tuple[DocumentParagraph, ...],
    boundaries: list[_Boundary],
    config: ChunkingConfig,
) -> tuple[list[_Boundary], bool]:
    if len(boundaries) < 2:
        return boundaries, False
    previous, tail = boundaries[-2:]
    previous_words = _word_count(paragraphs, previous.start, previous.end)
    tail_words = _word_count(paragraphs, tail.start, tail.end)
    if (
        tail_words >= config.minimum_preferred_words
        or previous_words > config.max_words
        or tail_words > config.max_words
    ):
        return boundaries, False
    choices: list[tuple[tuple[int, int, int, int], int]] = []
    for split in range(previous.start + 1, tail.end):
        left = _word_count(paragraphs, previous.start, split)
        right = _word_count(paragraphs, split, tail.end)
        if left > config.max_words or right > config.max_words:
            continue
        if min(left, right) <= tail_words:
            continue
        below_preferred = int(left < config.minimum_preferred_words) + int(
            right < config.minimum_preferred_words
        )
        score = (
            below_preferred,
            abs(left - config.target_words) + abs(right - config.target_words),
            -min(left, right),
            split,
        )
        choices.append((score, split))
    if not choices:
        return boundaries, False
    split = min(choices)[1]
    return [*boundaries[:-2], _Boundary(previous.start, split), _Boundary(split, tail.end)], True


def build_chunks(
    paragraphs: tuple[DocumentParagraph, ...], config: ChunkingConfig
) -> tuple[tuple[PlannedDocumentChunk, ...], bool, tuple[str, ...]]:
    boundaries, rebalanced = _rebalance_tail(
        paragraphs, _initial_boundaries(paragraphs, config), config
    )
    chunks: list[PlannedDocumentChunk] = []
    warnings: list[str] = []
    for sequence, boundary in enumerate(boundaries, start=1):
        selected = paragraphs[boundary.start : boundary.end]
        words = sum(paragraph.word_count for paragraph in selected)
        oversize = words > config.max_words
        chunk_warnings: tuple[str, ...] = ()
        if oversize:
            warning = f"paragraph {selected[0].sequence} exceeds maximum words and was kept intact"
            warnings.append(warning)
            chunk_warnings = (warning,)
        context = build_context(
            paragraphs, current_start=selected[0].sequence, limit_words=config.context_words
        )
        chunks.append(
            PlannedDocumentChunk(
                sequence=sequence,
                paragraph_start=selected[0].sequence,
                paragraph_end=selected[-1].sequence,
                paragraphs=selected,
                source_text=render_paragraphs(selected),
                word_count=words,
                oversize=oversize,
                ended_at_scene_boundary=selected[-1].is_scene_separator,
                context=context,
                warnings=chunk_warnings,
            )
        )
    return tuple(chunks), rebalanced, tuple(warnings)
