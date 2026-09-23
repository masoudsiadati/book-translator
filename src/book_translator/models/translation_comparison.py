"""Persisted contract for a provider-free comparison of two runs' effective translations.

The comparison exists so a later stage — a literary editor, a human reader — can put two
generations of the same chapter side by side with their evidence. It judges nothing itself: it
records hashes, counts, identities, and finding codes on both sides and never prose, never a
preference, and never a literary score.
"""

from pathlib import Path

from pydantic import AwareDatetime, Field

from book_translator.models.base import ContractModel


class ComparedTranslation(ContractModel):
    """One side of one chunk: what was accepted, where it came from, and what QA says about it."""

    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    job_id: str = Field(min_length=1)
    output_contract_version: str | None = None
    acceptance_origin: str = Field(min_length=1)
    translation_file: Path
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paragraph_count: int = Field(ge=0)
    provider_attempts: int = Field(ge=0)
    structural_repairs: int = Field(ge=0)
    provider: str | None = None
    model: str | None = None
    structural_blocking_codes: list[str] = Field(default_factory=list)
    terminology_blocking_codes: list[str] = Field(default_factory=list)
    alignment_codes: list[str] = Field(default_factory=list)
    alignment_blocking: int = Field(ge=0)
    semantic_status: str | None = None
    semantic_evidence_count: int = Field(ge=0)
    semantic_judge_calls: int = Field(ge=0)
    semantic_second_looks: int = Field(ge=0)
    semantic_revise_findings: int = Field(ge=0)
    semantic_revisions_attempted: int = Field(ge=0)
    semantic_revisions_adopted: int = Field(ge=0)
    semantic_reviser_calls: int = Field(ge=0)
    revised_paragraph_ids: list[str] = Field(default_factory=list)
    unresolved_paragraph_ids: list[str] = Field(default_factory=list)


class ComparedChunk(ContractModel):
    chunk_id: str = Field(min_length=1)
    chunk_sequence: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_paragraph_count: int = Field(ge=1)
    first: ComparedTranslation
    second: ComparedTranslation
    paragraph_identity_aligned: bool
    differing_paragraph_ids: list[str] = Field(default_factory=list)


class TranslationComparisonReport(ContractModel):
    """Two runs' effective translations of one chapter, side by side, in counts and hashes."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    comparison_engine_version: str
    chapter_id: str = Field(min_length=1)
    first_run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    second_run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    snapshot_id: str = Field(min_length=1)
    same_snapshot: bool
    first_chapter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    second_chapter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    first_release_chapter_file: Path | None = None
    first_release_chapter_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_contacted: bool = False
    literary_judgement: bool = False
    chunks: list[ComparedChunk] = Field(default_factory=list)
    created_at: AwareDatetime
