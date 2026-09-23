"""Durable contracts for provider-free structural repair of a rejected translation.

A structural repair is the one remedy for a QA rejection that changes the translation without
asking anyone to write anything: it re-emits the provider's own lines under the frozen source's
blank-line placement. Because it changes stored translated bytes it needs the same kind of
evidence a provider attempt does — what it derived from, what it decided, and what QA made of the
result — and because it changes only whitespace, the record can also *prove* that, which is what
``content_preserved`` and the recorded digests are for.

Nothing here carries book prose. Line and block counts, indexes, and SHA-256 digests are the
whole vocabulary.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.paragraph_alignment import TranslationAlignmentReport
from book_translator.models.terminology import (
    TerminologyValidationReport,
    TranslationQAReport,
)
from book_translator.models.translation_worker import (
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
)


class StructuralDefectClass(StrEnum):
    """Why a source and its translation disagree about block structure."""

    #: The two sides already agree; there is nothing structural to repair.
    NONE = "none"
    #: Every line exists on both sides in the same order and only blank-line placement differs.
    BLANK_LINE_GEOMETRY = "blank_line_geometry"
    #: One or more source lines were written as several translation lines.
    TRANSLATION_LINE_SPLIT = "translation_line_split"
    #: Several source lines were written as one translation line; where the break belongs is a
    #: judgement about meaning, so a provider answers it.
    TRANSLATION_LINE_MERGE = "translation_line_merge"
    #: The line counts differ and no alignment was proven well enough to act on.
    LINE_COUNT_DIVERGENCE = "line_count_divergence"
    #: The two sides differ by more than separators; content is missing, added, or reordered.
    CONTENT_DIVERGENCE = "content_divergence"


#: The classes a deterministic repair may act on. Everything else needs a provider or a person.
DETERMINISTIC_DEFECT_CLASSES = frozenset(
    {StructuralDefectClass.BLANK_LINE_GEOMETRY, StructuralDefectClass.TRANSLATION_LINE_SPLIT}
)


class StructuralOperationKind(StrEnum):
    """What one aligned group of lines says happened to the source lines it covers."""

    ALIGNED = "aligned"
    JOIN_TRANSLATION_LINES = "join_translation_lines"
    SPLIT_TRANSLATION_LINE = "split_translation_line"


class StructuralOperationRecord(ContractModel):
    """One non-identity correspondence, named precisely enough to re-derive by hand."""

    kind: StructuralOperationKind
    source_index: int = Field(ge=0)
    source_lines: int = Field(ge=1)
    translation_index: int = Field(ge=0)
    translation_lines: int = Field(ge=1)
    cost_milli: int = Field(ge=0)


class StructuralRepairDiagnosis(ContractModel):
    """The complete structural answer for one translation, before anything is changed.

    Costs and margins are recorded in thousandths as integers rather than as floats, because a
    persisted contract that has to round-trip byte-for-byte cannot depend on float formatting.
    """

    structural_repair_engine_version: str = Field(min_length=1)
    defect_class: StructuralDefectClass
    source_paragraphs: int = Field(ge=0)
    translation_paragraphs: int = Field(ge=0)
    paragraph_delta: int
    source_lines: int = Field(ge=0)
    translation_lines: int = Field(ge=0)
    line_delta: int
    blank_line_artifact: bool
    deterministic: bool
    alignment_cost_milli: int | None = None
    alignment_margin_milli: int | None = None
    operations: list[StructuralOperationRecord] = Field(default_factory=list)
    refusals: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def the_classification_agrees_with_itself(self) -> "StructuralRepairDiagnosis":
        if self.paragraph_delta != self.translation_paragraphs - self.source_paragraphs:
            raise ValueError("paragraph delta must follow from the counts")
        if self.line_delta != self.translation_lines - self.source_lines:
            raise ValueError("line delta must follow from the counts")
        if self.deterministic and self.defect_class not in DETERMINISTIC_DEFECT_CLASSES:
            raise ValueError("only a separator defect may be repaired deterministically")
        if self.deterministic and self.refusals:
            raise ValueError("a deterministic repair has nothing to refuse")
        if (
            not self.deterministic
            and not self.refusals
            and self.defect_class is not StructuralDefectClass.NONE
        ):
            raise ValueError("a refused structural repair must say why")
        if (
            any(
                operation.kind is StructuralOperationKind.SPLIT_TRANSLATION_LINE
                for operation in self.operations
            )
            and self.deterministic
        ):
            raise ValueError("splitting a merged translation line is never deterministic")
        return self


class TranslationStructuralRepairEvidence(ContractModel):
    """Immutable provenance for one adopted provider-free structural repair.

    The field names deliberately match the acceptance and re-evaluation sidecars, because this
    record takes their place as one job's effective mechanical verdict and every reader that
    resolves an effective rejection has to be able to read all three the same way.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    structural_repair_engine_version: str = Field(min_length=1)
    terminology_qa_version: str = Field(min_length=1)
    structural_qa_version: str = Field(min_length=1)
    alignment_qa_version: str | None = None
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    job_id: str = Field(min_length=1)
    repair_number: int = Field(ge=1)
    provider_contacted: Literal[False] = False
    content_preserved: Literal[True] = True
    #: The effective rejection this repair answered, and the origin that had defined it.
    superseded_evidence_file: Path
    superseded_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    superseded_origin: TranslationAcceptanceOrigin
    #: The provider attempt the repaired bytes still descend from; it is never modified.
    attempt_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    attempt_file: Path
    attempt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file: Path
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_translation_file: Path
    original_translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_file: Path
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminology_file: Path | None = None
    terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    diagnosis: StructuralRepairDiagnosis
    superseded_blocking_codes: list[str] = Field(default_factory=list)
    terminology_report: TerminologyValidationReport
    structural_report: TranslationQAReport
    alignment_report: TranslationAlignmentReport | None = None
    previous_status: TranslationAcceptanceStatus
    resulting_status: TranslationAcceptanceStatus
    accepted: bool
    origin: Literal["structural_repair"] = "structural_repair"
    created_at: AwareDatetime

    @model_validator(mode="after")
    def outcome_and_lineage_are_consistent(self) -> "TranslationStructuralRepairEvidence":
        expected = self.terminology_report.valid and self.structural_report.valid
        if self.accepted != expected:
            raise ValueError("accepted must equal the combined QA validity")
        if (self.resulting_status is TranslationAcceptanceStatus.ACCEPTED) != self.accepted:
            raise ValueError("structural repair status must match accepted")
        if not self.diagnosis.deterministic:
            raise ValueError("only a deterministic structural repair may be published")
        if self.original_translation_sha256 == self.translation_sha256:
            raise ValueError("a published structural repair must change the translation bytes")
        if (
            self.terminology_report.source_sha256 != self.source_sha256
            or self.structural_report.source_sha256 != self.source_sha256
            or self.terminology_report.translation_sha256 != self.translation_sha256
            or self.structural_report.translation_sha256 != self.translation_sha256
        ):
            raise ValueError("QA report hashes must match the repaired lineage")
        if self.structural_report.source_paragraph_count != (
            self.structural_report.translation_paragraph_count
        ):
            raise ValueError("an adopted structural repair must restore block correspondence")
        if (self.terminology_file is None) != (self.terminology_sha256 is None):
            raise ValueError("terminology file and hash must be present together")
        current = {
            finding.code
            for finding in self.terminology_report.findings
            if finding.severity == "error"
        } | {
            finding.code
            for finding in self.structural_report.findings
            if finding.severity == "error"
        }
        if not current <= set(self.superseded_blocking_codes):
            raise ValueError("a structural repair must not introduce a new blocking finding")
        return self
