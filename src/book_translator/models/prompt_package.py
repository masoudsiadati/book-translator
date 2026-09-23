"""Contracts for deterministic provider-ready prompt packages."""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from book_translator.models.base import ContractModel
from book_translator.models.translation_snapshot import SnapshotResourceKind


class PromptPackageStatus(StrEnum):
    RENDERED = "rendered"
    VALIDATED = "validated"
    INVALID = "invalid"


class PromptPackageResourceReference(ContractModel):
    resource_kind: SnapshotResourceKind
    file: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class PromptPackageMessageReference(ContractModel):
    role: Literal["system", "user"]
    file: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    character_count: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    exact_tokens: int | None = Field(default=None, ge=0)


class PromptPackage(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    package_id: str = Field(pattern=r"^pp-[0-9a-f]{24}$")
    package_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    renderer_version: str
    run_id: str
    job_id: str
    snapshot_id: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chapter_id: str
    chunk_id: str
    chunk_sequence: int = Field(ge=1)
    total_chapter_chunks: int = Field(ge=1)
    system_message: PromptPackageMessageReference
    user_message: PromptPackageMessageReference
    chunk_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt: PromptPackageResourceReference
    glossary: PromptPackageResourceReference
    style_guide: PromptPackageResourceReference
    terminology: PromptPackageResourceReference | None = None
    source_anomalies: PromptPackageResourceReference | None = None
    series_terminology: PromptPackageResourceReference | None = None
    reference_context: PromptPackageResourceReference | None = None
    terminology_context_contract_version: str | None = None
    #: The frozen occurrence exemptions the job was rendered under. Omitted, not null, when the
    #: snapshot carries none, so earlier packages keep their bytes.
    terminology_exemptions: PromptPackageResourceReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    source_word_count: int = Field(ge=0)
    context_word_count: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    exact_input_tokens: int | None = Field(default=None, ge=0)
    output_contract_version: str
    status: PromptPackageStatus
    created_at: AwareDatetime
    validated_at: AwareDatetime | None = None
    warnings: list[str] = Field(default_factory=list)


class PromptPackageValidationReport(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    package_id: str
    valid: bool
    checks_performed: list[str]
    snapshot_valid: bool
    queue_job_valid: bool
    resources_valid: bool
    system_hash_valid: bool
    user_hash_valid: bool
    source_conserved: bool
    context_conserved: bool
    placeholders_valid: bool
    delimiters_valid: bool
    paths_valid: bool
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    created_at: AwareDatetime


class PromptPackageRenderSummary(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    run_id: str
    selected_jobs: int = Field(ge=0)
    rendered: int = Field(ge=0)
    skipped: int = Field(ge=0)
    stale: int = Field(ge=0)
    conflicting: int = Field(ge=0)
    valid: int = Field(ge=0)
