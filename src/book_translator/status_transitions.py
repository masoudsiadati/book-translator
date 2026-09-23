"""Pure lifecycle transition policy; this module performs no file I/O."""

from book_translator.models.chapter import ChapterStatus
from book_translator.models.chunk import ChunkStatus
from book_translator.models.translation_snapshot import TranslationSnapshotStatus

_CHAPTER_TRANSITIONS: dict[ChapterStatus, frozenset[ChapterStatus]] = {
    ChapterStatus.DISCOVERED: frozenset({ChapterStatus.EXTRACTED, ChapterStatus.FAILED}),
    ChapterStatus.EXTRACTED: frozenset({ChapterStatus.NORMALIZED, ChapterStatus.FAILED}),
    ChapterStatus.NORMALIZED: frozenset({ChapterStatus.VALIDATED, ChapterStatus.FAILED}),
    ChapterStatus.VALIDATED: frozenset({ChapterStatus.CHUNKED, ChapterStatus.FAILED}),
    ChapterStatus.CHUNKED: frozenset({ChapterStatus.TRANSLATING, ChapterStatus.FAILED}),
    ChapterStatus.TRANSLATING: frozenset({ChapterStatus.TRANSLATED, ChapterStatus.FAILED}),
    ChapterStatus.TRANSLATED: frozenset({ChapterStatus.REVIEW_REQUIRED, ChapterStatus.FAILED}),
    ChapterStatus.REVIEW_REQUIRED: frozenset({ChapterStatus.REVIEWED, ChapterStatus.FAILED}),
    ChapterStatus.REVIEWED: frozenset(
        {ChapterStatus.APPROVED, ChapterStatus.REVIEW_REQUIRED, ChapterStatus.FAILED}
    ),
    ChapterStatus.APPROVED: frozenset(),
    ChapterStatus.FAILED: frozenset(),
}

_CHUNK_TRANSITIONS: dict[ChunkStatus, frozenset[ChunkStatus]] = {
    ChunkStatus.PENDING: frozenset({ChunkStatus.TRANSLATING, ChunkStatus.FAILED}),
    ChunkStatus.TRANSLATING: frozenset({ChunkStatus.TRANSLATED, ChunkStatus.FAILED}),
    ChunkStatus.TRANSLATED: frozenset(
        {ChunkStatus.REVIEW_REQUIRED, ChunkStatus.VALIDATION_FAILED, ChunkStatus.FAILED}
    ),
    ChunkStatus.VALIDATION_FAILED: frozenset({ChunkStatus.REVIEW_REQUIRED, ChunkStatus.FAILED}),
    ChunkStatus.REVIEW_REQUIRED: frozenset({ChunkStatus.REVIEWED, ChunkStatus.FAILED}),
    ChunkStatus.REVIEWED: frozenset(
        {ChunkStatus.APPROVED, ChunkStatus.REVIEW_REQUIRED, ChunkStatus.FAILED}
    ),
    ChunkStatus.APPROVED: frozenset(),
    ChunkStatus.FAILED: frozenset(),
}

_TRANSLATION_SNAPSHOT_TRANSITIONS: dict[
    TranslationSnapshotStatus, frozenset[TranslationSnapshotStatus]
] = {
    TranslationSnapshotStatus.DRAFT: frozenset(
        {TranslationSnapshotStatus.VALIDATED, TranslationSnapshotStatus.INVALID}
    ),
    TranslationSnapshotStatus.VALIDATED: frozenset(
        {TranslationSnapshotStatus.FROZEN, TranslationSnapshotStatus.INVALID}
    ),
    TranslationSnapshotStatus.FROZEN: frozenset(),
    TranslationSnapshotStatus.INVALID: frozenset(),
}


def can_transition_chapter(
    current: ChapterStatus, target: ChapterStatus, *, explicit_retry: bool = False
) -> bool:
    """Return whether a chapter transition is permitted by the frozen policy."""
    if current == target:
        return True
    if current is ChapterStatus.FAILED and explicit_retry:
        return target in {ChapterStatus.EXTRACTED, ChapterStatus.NORMALIZED}
    return target in _CHAPTER_TRANSITIONS[current]


def can_transition_chunk(
    current: ChunkStatus, target: ChunkStatus, *, explicit_retry: bool = False
) -> bool:
    """Return whether a chunk transition is permitted by the frozen policy."""
    if current == target:
        return True
    if current is ChunkStatus.FAILED and explicit_retry:
        return target is ChunkStatus.PENDING
    return target in _CHUNK_TRANSITIONS[current]


def can_transition_translation_snapshot(
    current: TranslationSnapshotStatus, target: TranslationSnapshotStatus
) -> bool:
    """Return whether a translation-snapshot lifecycle transition is permitted."""
    if current == target:
        return True
    return target in _TRANSLATION_SNAPSHOT_TRANSITIONS[current]
