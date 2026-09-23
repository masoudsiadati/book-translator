"""Immutable lineage for a governed canonical book identity migration."""

from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel


class DerivedIdentityChange(ContractModel):
    """One book-scoped derived identifier that moved with the book identity."""

    field: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    record: str = Field(min_length=1)
    before: str = Field(min_length=1)
    after: str = Field(min_length=1)

    @model_validator(mode="after")
    def identity_actually_moved(self) -> "DerivedIdentityChange":
        if self.before == self.after:
            raise ValueError("a recorded derived identity change must differ")
        return self


class MigratedRegistry(ContractModel):
    """Before and after evidence for one migrated registry file."""

    file: Path
    registry_kind: str = Field(min_length=1)
    before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # SHA-256 of the canonical registry payload with book identity fields removed. Equal before and
    # after, which is what proves the migration moved identity without touching semantic content.
    semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    book_id_occurrences: int = Field(ge=1)
    changed_line_count: int = Field(ge=1)
    derived_identity_changes: list[DerivedIdentityChange] = Field(default_factory=list)

    @model_validator(mode="after")
    def raw_bytes_changed(self) -> "MigratedRegistry":
        if self.before_sha256 == self.after_sha256:
            raise ValueError("a migrated registry must change its raw bytes")
        if self.file.is_absolute():
            raise ValueError("registry path must be project-relative")
        return self


class BookIdentityMigration(ContractModel):
    """Immutable, independently auditable record of one book identity migration."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    migration_id: str = Field(pattern=r"^bim-[0-9a-f]{24}$")
    engine_version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    provenance: Literal["canonical_book_metadata"] = "canonical_book_metadata"
    book_metadata_file: Path
    book_metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_book_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    book_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    registries: list[MigratedRegistry] = Field(min_length=1)
    semantic_content_unchanged: bool
    created_at: AwareDatetime

    @model_validator(mode="after")
    def identity_actually_moved(self) -> "BookIdentityMigration":
        if self.previous_book_id == self.book_id:
            raise ValueError("a migration must change the book identity")
        files = [item.file for item in self.registries]
        if len(files) != len(set(files)):
            raise ValueError("each migrated registry must appear once")
        if files != sorted(files):
            raise ValueError("migrated registries must be ordered by path")
        if not self.semantic_content_unchanged:
            raise ValueError(
                "a migration that changes semantic registry content is not publishable"
            )
        return self
