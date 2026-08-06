"""Chunk metadata and lifecycle state."""

from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, Field

from book_translator.models.base import ContractModel


class ChunkStatus(StrEnum):
    PENDING = "pending"
    TRANSLATING = "translating"
    TRANSLATED = "translated"
    VALIDATION_FAILED = "validation_failed"
    REVIEW_REQUIRED = "review_required"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    FAILED = "failed"


class ChunkMetadata(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    chunk_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    chapter_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    sequence: int = Field(ge=1)
    source_file: Path
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_file: Path
    context_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_word_count: int = Field(default=0, ge=0)
    context_source_start_paragraph: int | None = Field(default=None, ge=1)
    context_source_end_paragraph: int | None = Field(default=None, ge=1)
    translation_file: Path
    translation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_word_count: int = Field(ge=0)
    translation_word_count: int | None = Field(default=None, ge=0)
    source_paragraph_count: int = Field(ge=0)
    translation_paragraph_count: int | None = Field(default=None, ge=0)
    status: ChunkStatus
    translation_attempts: int = Field(default=0, ge=0)
    review_attempts: int = Field(default=0, ge=0)
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    glossary_version: str | None = None
    chunk_engine_version: str = ""
    cleaned_source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    oversize: bool = False
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
