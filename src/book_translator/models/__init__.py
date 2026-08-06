"""Versioned domain contracts for the canonical workspace."""

from book_translator.models.book import BookWorkspaceMetadata, TocDocument, TocEntry, TocKind
from book_translator.models.chapter import ChapterMetadata, ChapterStatus
from book_translator.models.chunk import ChunkMetadata, ChunkStatus
from book_translator.models.progress import PipelineRunStatus, Progress
from book_translator.models.snapshot import ExtractionSnapshot

SCHEMA_VERSION = "1.0"

__all__ = [
    "SCHEMA_VERSION",
    "BookWorkspaceMetadata",
    "ChapterMetadata",
    "ChapterStatus",
    "ChunkMetadata",
    "ChunkStatus",
    "ExtractionSnapshot",
    "PipelineRunStatus",
    "Progress",
    "TocEntry",
    "TocDocument",
    "TocKind",
]
