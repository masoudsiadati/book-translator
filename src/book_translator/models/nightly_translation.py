"""Prose-free checkpoint contract for bounded nightly translation."""

from pathlib import Path

from pydantic import AwareDatetime, Field

from book_translator.models.base import ContractModel


class NightlyTranslationCheckpoint(ContractModel):
    schema_version: str = "1.0"
    snapshot_id: str = Field(min_length=1)
    translation_run_id: str = Field(min_length=1)
    provider: str = "codex-cli"
    total_units: int = Field(ge=1)
    total_chunks: int = Field(ge=1)
    units_with_complete_translated_coverage: list[str]
    translated_chunks_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    qa_rejected_count: int = Field(ge=0)
    missing_translation_count: int = Field(ge=0)
    failed_retry_pending_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    last_completed_unit: str | None = None
    next_unit: str | None = None
    last_provider_state: str = "not_contacted"
    next_safe_action: str
    draft_path: Path | None = None
    updated_at: AwareDatetime
