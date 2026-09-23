"""Persisted contracts for deterministic paragraph alignment QA."""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel


class ParagraphAlignmentFinding(ContractModel):
    """One alignment or marker-contract defect, recorded without any translated content."""

    code: str = Field(min_length=1)
    severity: Literal["error", "warning"]
    source_paragraph_index: int | None = Field(default=None, ge=1)
    related_paragraph_index: int | None = Field(default=None, ge=1)
    expected_marker: str | None = None
    observed_marker: str | None = None
    message: str = Field(min_length=1)


class TranslationAlignmentReport(ContractModel):
    """The alignment verdict for one translated chunk.

    ``contract_mode`` records how the paragraph identities being judged were established, because
    the same measurements mean different things under the two.  Under ``marked`` a provider
    returned the identities it was given and the measurements are a residual check; under
    ``legacy_positional`` the identities are inferred from block order, which is exactly the
    assumption a boundary defect breaks, so the measurements are the only evidence there is.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    alignment_qa_version: str
    policy_id: str = Field(min_length=1)
    policy_version: str
    contract_mode: Literal["marked", "legacy_positional"]
    marker_contract_version: str | None = None
    evaluated: bool
    valid: bool
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    aligned_pairs: int = Field(ge=0)
    measured_pairs: int = Field(ge=0)
    flagged_pairs: int = Field(ge=0)
    findings: list[ParagraphAlignmentFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def validity_matches_findings(self) -> "TranslationAlignmentReport":
        if self.valid != all(item.severity != "error" for item in self.findings):
            raise ValueError("alignment validity must equal the absence of error findings")
        if self.measured_pairs > self.aligned_pairs:
            raise ValueError("measured pairs cannot exceed aligned pairs")
        if not self.evaluated and self.aligned_pairs:
            raise ValueError("an unevaluated alignment report cannot carry aligned pairs")
        return self


class ParagraphAlignmentAuditEntry(ContractModel):
    """One assessed job in a read-only alignment audit."""

    job_id: str = Field(min_length=1)
    chapter_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    acceptance_origin: str = Field(min_length=1)
    contract_mode: Literal["marked", "legacy_positional"]
    evaluated: bool
    aligned_pairs: int = Field(ge=0)
    measured_pairs: int = Field(ge=0)
    flagged_pairs: int = Field(ge=0)
    error_findings: int = Field(ge=0)
    warning_findings: int = Field(ge=0)
    findings: list[ParagraphAlignmentFinding] = Field(default_factory=list)


class ParagraphAlignmentAuditReport(ContractModel):
    """The complete read-only alignment verdict over one translation run.

    The audit changes nothing: no acceptance, no status, no counter, no translated byte.  It
    exists so a marker-less historical run can be measured under the same policy that governs new
    work, which is the only way to know whether the policy reproduces what a human audit found.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    alignment_qa_version: str
    policy_id: str = Field(min_length=1)
    policy_version: str
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    snapshot_id: str = Field(min_length=1)
    run_output_contract_version: str | None = None
    provider_contacted: Literal[False] = False
    mutated_state: Literal[False] = False
    assessed_jobs: int = Field(ge=0)
    skipped_jobs: int = Field(ge=0)
    aligned_pairs: int = Field(ge=0)
    measured_pairs: int = Field(ge=0)
    jobs_with_findings: int = Field(ge=0)
    jobs_with_blocking_findings: int = Field(ge=0)
    finding_counts: dict[str, int] = Field(default_factory=dict)
    entries: list[ParagraphAlignmentAuditEntry] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    created_at: AwareDatetime
