"""Durable provenance for automatically repaired mechanical QA rejections.

A repair attempt already records what was executed (`execution_kind`, `repaired_from_attempt`)
and the acceptance sidecar already records what QA made of it. Neither records *why* an
unattended worker was allowed to spend a provider attempt on it. This contract is that missing
half: the versioned policy verdict, the classified findings that triggered it, and the bounded
budget it was drawn against.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.translation_worker import TranslationAcceptanceOrigin


class TranslationRepairInitiator(StrEnum):
    """Who decided that a repair attempt should be spent."""

    AUTOMATIC = "automatic"
    OPERATOR = "operator"


class RepairFindingClass(StrEnum):
    """What kind of remedy a single QA finding admits.

    The classes are about the *remedy*, not the severity: a warning is never blocking, but it
    still belongs to a class, because the same code can appear as an error once a QA engine
    tightens.
    """

    #: A provider repair carrying the finding can mechanically resolve it.
    AUTO_REPAIRABLE = "auto_repairable"
    #: The finding rests on a QA judgement that may have moved; re-assess before paying a provider.
    REEVALUATION_CANDIDATE = "reevaluation_candidate"
    #: No repair of the translation can resolve it, because its cause is not in the translation.
    NON_REPAIRABLE = "non_repairable"
    #: A person must decide; automatic repair is refused.
    OPERATOR_REQUIRED = "operator_required"


class AutoRepairVerdict(StrEnum):
    """The policy's decision for one QA-rejected job."""

    REPAIRABLE = "repairable"
    NOT_REJECTED = "not_rejected"
    REFUSED = "refused"
    BUDGET_EXHAUSTED = "budget_exhausted"
    REEVALUATION_REQUIRED = "reevaluation_required"


class AutoRepairFindingRecord(ContractModel):
    """One classified QA finding, without any translated content."""

    origin: Literal["terminology", "structural"]
    code: str = Field(min_length=1)
    severity: Literal["warning", "error"]
    classification: RepairFindingClass
    blocking: bool


class TranslationAutoRepairDecision(ContractModel):
    """Immutable authorization for exactly one automatic repair attempt.

    Only an authorization is ever persisted. The outcome is deliberately not copied here: the
    attempt and the acceptance sidecar for the same attempt number already hold it, and
    duplicating evidence is how evidence starts disagreeing with itself.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    auto_repair_engine_version: str
    auto_repair_policy_version: str
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    job_id: str
    initiator: TranslationRepairInitiator
    verdict: AutoRepairVerdict
    repaired_from_attempt: int = Field(ge=1)
    repair_attempt_number: int = Field(ge=2)
    repair_attempts_used: int = Field(ge=0)
    max_repair_attempts: int = Field(ge=1)
    provider_attempts_used: int = Field(ge=1)
    max_provider_attempts: int = Field(ge=1)
    remaining_repair_attempts: int = Field(ge=1)
    rejection_origin: TranslationAcceptanceOrigin
    rejection_evidence_file: Path
    rejection_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_file: Path
    attempt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rejected_translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminology_qa_version: str | None = None
    structural_qa_version: str | None = None
    findings: list[AutoRepairFindingRecord] = Field(default_factory=list)
    triggering_codes: list[str] = Field(default_factory=list)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def authorization_is_self_evident(self) -> "TranslationAutoRepairDecision":
        if self.verdict is not AutoRepairVerdict.REPAIRABLE:
            raise ValueError("only an authorized repair decision is persisted")
        if not self.triggering_codes:
            raise ValueError("an authorized repair decision must name what triggered it")
        if self.repair_attempt_number <= self.repaired_from_attempt:
            raise ValueError("a repair attempt must follow the attempt it repairs")
        if self.repair_attempts_used >= self.max_repair_attempts:
            raise ValueError("an authorized repair decision requires remaining repair budget")
        if self.provider_attempts_used >= self.max_provider_attempts:
            raise ValueError("an authorized repair decision requires remaining provider budget")
        blocking = {finding.code for finding in self.findings if finding.blocking}
        if set(self.triggering_codes) != blocking:
            raise ValueError("triggering codes must be exactly the blocking findings")
        if any(
            finding.blocking and finding.classification is not RepairFindingClass.AUTO_REPAIRABLE
            for finding in self.findings
        ):
            raise ValueError("automatic repair requires every blocking finding to be repairable")
        return self
