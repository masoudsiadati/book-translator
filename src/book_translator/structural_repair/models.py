"""Typed plans and results for the structural repair stage."""

from dataclasses import dataclass
from pathlib import Path

from book_translator.models import (
    StructuralDefectClass,
    TranslationAcceptanceStatus,
    TranslationJob,
    TranslationStructuralRepairEvidence,
)
from book_translator.structural_repair.engine import StructuralDiagnosis


@dataclass(frozen=True, slots=True)
class StructuralRepairPlan:
    """What a provider-free structural repair of one job would do, having changed nothing.

    ``evidence`` is present exactly when the repair is applicable: building it requires the
    repaired bytes and the QA that judged them, so a plan carrying one has already proven the
    remedy rather than predicted it.
    """

    run_id: str
    job_id: str
    chunk_id: str
    job: TranslationJob
    diagnosis: StructuralDiagnosis
    applicable: bool
    refusals: tuple[str, ...]
    repair_number: int
    previous_status: TranslationAcceptanceStatus
    projected_status: TranslationAcceptanceStatus
    resolved_blocking_codes: tuple[str, ...]
    persisting_blocking_codes: tuple[str, ...]
    repaired_text: str | None
    evidence: TranslationStructuralRepairEvidence | None
    existing_evidence_file: Path | None

    @property
    def defect_class(self) -> StructuralDefectClass:
        return self.diagnosis.defect_class

    @property
    def already_applied(self) -> bool:
        return self.existing_evidence_file is not None


@dataclass(frozen=True, slots=True)
class StructuralRepairResult:
    """What one structural repair actually did to one job."""

    plan: StructuralRepairPlan
    applied: bool
    reused: bool
    dry_run: bool
    resulting_status: TranslationAcceptanceStatus
    evidence_file: Path | None
    translation_file: Path | None

    @property
    def provider_calls(self) -> int:
        """Always zero. The stage exists precisely because this remedy contacts nobody."""
        return 0
