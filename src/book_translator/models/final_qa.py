"""Durable contracts for final QA resolution of a completed translation run.

A run that ends ``completed_with_failures`` holds two very different things under one status: a
translation the provider got wrong, and a QA verdict that has since stopped being true. Telling
them apart is the whole point of this stage, and the answer has to be persisted, because deciding
to spend a provider attempt — or deciding *not* to — is exactly the kind of judgement that has to
stay explainable after the queue's event ring buffer has rotated.

Nothing here carries book prose. Terminology targets and competing variants are registry data and
are already committed in the terminology registry; source and translated text appear only as
counts and SHA-256 digests.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.structural_repair import StructuralRepairDiagnosis
from book_translator.models.terminology import (
    DecisionSource,
    DecisionState,
    TerminologyEnforcement,
)
from book_translator.models.translation_worker import TranslationAcceptanceOrigin


class CompetingVariantClass(StrEnum):
    """Why a ``competing_variant`` finding exists, decided from governed evidence alone.

    The classes are ordered from "the finding is an artefact" to "only a person can answer this",
    and every one of them is derived from the registry, the frozen resources, and the immutable
    attempt — never from a provider and never from a guess.
    """

    #: The variant is the canonical target's own lexeme: it contains it, or derives from it.
    CANONICAL_IDENTITY = "canonical_identity"
    #: The decision that recorded the variant no longer records it in the effective registry.
    RETIRED_COMPETITOR = "retired_competitor"
    #: The variant is attested only by historical output, not by any current registry decision.
    HISTORICAL_OUTPUT_ONLY = "historical_output_only"
    #: The translation carries the variant and *not* the canonical: a real rival rendering.
    CANONICAL_ABSENT = "canonical_absent"
    #: Another active decision claims the variant as its canonical target, and one side is locked
    #: or user-owned; the locked canonical wins and the translation is what may change.
    LOCKED_CANONICAL_CONFLICT = "locked_canonical_conflict"
    #: Two active, unlocked, AI-owned decisions claim the same target and exactly one of them is
    #: settled, so the settled one is a deterministic winner.
    RIVAL_AI_DECISIONS = "rival_ai_decisions"
    #: Two active decisions of equal standing render the same source expression differently.
    GENUINE_CANONICAL_CONFLICT = "genuine_canonical_conflict"
    #: The canonical is present, the variant is present, and nothing governed says which source
    #: expression the variant renders. Fail closed.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


#: Classes a provider-free path can clear on its own.
PROVIDER_FREE_VARIANT_CLASSES = frozenset(
    {
        CompetingVariantClass.CANONICAL_IDENTITY,
        CompetingVariantClass.RETIRED_COMPETITOR,
        CompetingVariantClass.HISTORICAL_OUTPUT_ONLY,
    }
)

#: Classes a governed terminology resolution can settle deterministically, without a provider and
#: without touching any locked or user-owned decision.
GOVERNED_RESOLUTION_VARIANT_CLASSES = frozenset({CompetingVariantClass.RIVAL_AI_DECISIONS})

#: Classes no automatic process may resolve.
OPERATOR_VARIANT_CLASSES = frozenset(
    {
        CompetingVariantClass.GENUINE_CANONICAL_CONFLICT,
        CompetingVariantClass.INSUFFICIENT_EVIDENCE,
    }
)


class FinalQAVerdict(StrEnum):
    """What the final-QA policy says should happen to one QA-rejected job."""

    #: Current QA already accepts the standing translation; nothing is left to decide.
    ACCEPTED_NO_CHANGE = "accepted_no_change"
    #: Current QA no longer reproduces the rejection; republish the verdict, provider-free.
    REEVALUATE = "reevaluate"
    #: The block structure diverged by separators alone, which is repairable without a provider.
    REPAIR_STRUCTURE = "repair_structure"
    #: The translation itself conflicts with canonical terminology or structure.
    REPAIR_TRANSLATION = "repair_translation"
    #: A terminology decision must be resolved first; the translation then passes unchanged.
    RESOLVE_TERMINOLOGY_THEN_REEVALUATE = "resolve_terminology_then_reevaluate"
    #: A terminology decision must be resolved first, and the translation must then conform.
    RESOLVE_TERMINOLOGY_THEN_REPAIR = "resolve_terminology_then_repair"
    #: A repair is the right answer but no attempt budget remains for it.
    EXHAUSTED = "exhausted"
    #: Nothing governed decides this; a person must.
    OPERATOR_REQUIRED = "operator_required"


#: Verdicts that reach acceptance in this run without contacting a provider.
ACCEPTS_WITHOUT_PROVIDER_VERDICTS = frozenset(
    {FinalQAVerdict.ACCEPTED_NO_CHANGE, FinalQAVerdict.REEVALUATE}
)

#: Verdicts the automatic orchestrator is allowed to act on at all.
ACTIONABLE_VERDICTS = frozenset(
    {
        FinalQAVerdict.ACCEPTED_NO_CHANGE,
        FinalQAVerdict.REEVALUATE,
        FinalQAVerdict.REPAIR_STRUCTURE,
        FinalQAVerdict.REPAIR_TRANSLATION,
    }
)

#: Verdicts the orchestrator can act on without contacting a provider. This is deliberately not
#: :data:`ACCEPTS_WITHOUT_PROVIDER_VERDICTS`: a structural repair contacts nobody, but it does not
#: on its own promise acceptance, because the repaired text is judged by QA like any other.
PROVIDER_FREE_ACTION_VERDICTS = frozenset(
    {
        FinalQAVerdict.ACCEPTED_NO_CHANGE,
        FinalQAVerdict.REEVALUATE,
        FinalQAVerdict.REPAIR_STRUCTURE,
    }
)


class FinalQACompetingVariantRecord(ContractModel):
    """One classified ``competing_variant`` finding with the evidence that classified it."""

    candidate_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    source_term: str = Field(min_length=1)
    canonical_target: str = Field(min_length=1)
    competing_variant: str = Field(min_length=1)
    variant_class: CompetingVariantClass
    decision_state: DecisionState
    decision_source: DecisionSource
    candidate_provenance: DecisionSource
    enforcement: TerminologyEnforcement
    locked: bool
    canonical_present: bool
    variant_present: bool
    source_occurrences: int = Field(ge=0)
    canonical_occurrences: int = Field(ge=0)
    variant_occurrences: int = Field(ge=0)
    recorded_in_effective_registry: bool
    rival_candidate_id: str | None = None
    rival_locked: bool = False
    reproduces_under_current_qa: bool


class FinalQAStructuralRecord(ContractModel):
    """The structural half of one rejection, diagnosed before any provider attempt is spent."""

    source_paragraphs: int = Field(ge=0)
    translation_paragraphs: int = Field(ge=0)
    paragraph_delta: int
    blank_line_artifact: bool
    deterministically_normalizable: bool
    codes: list[str] = Field(default_factory=list)
    #: The full structural answer: which lines diverged, how, and why a provider is or is not
    #: needed. It is what turns "the counts differ" into something a reviewer can act on.
    diagnosis: StructuralRepairDiagnosis | None = None

    @model_validator(mode="after")
    def the_summary_matches_the_diagnosis(self) -> "FinalQAStructuralRecord":
        if self.diagnosis is None:
            return self
        if (
            self.diagnosis.source_paragraphs != self.source_paragraphs
            or self.diagnosis.translation_paragraphs != self.translation_paragraphs
        ):
            raise ValueError("the structural summary must match its own diagnosis")
        if self.deterministically_normalizable != self.diagnosis.deterministic:
            raise ValueError("deterministic repair availability must match the diagnosis")
        return self


class FinalQAFindingRecord(ContractModel):
    """One blocking QA finding as the final-QA policy sees it."""

    origin: Literal["terminology", "structural"]
    code: str = Field(min_length=1)
    severity: Literal["warning", "error"]
    historical: bool
    current: bool


class FinalQABudgetRecord(ContractModel):
    """The attempt budget one job still has, and whether an escalation was ever granted."""

    repair_attempts_used: int = Field(ge=0)
    max_repair_attempts: int = Field(ge=0)
    provider_attempts_used: int = Field(ge=0)
    max_provider_attempts: int = Field(ge=1)
    remaining: int = Field(ge=0)
    escalations_granted: int = Field(ge=0)


class FinalQAJobDiagnosis(ContractModel):
    """The complete, self-contained answer for one QA-rejected job."""

    job_id: str = Field(min_length=1)
    chapter_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    effective_attempt: int = Field(ge=1)
    acceptance_origin: TranslationAcceptanceOrigin
    evidence_file: Path
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_file: Path
    attempt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_terminology_qa_version: str | None = None
    recorded_structural_qa_version: str | None = None
    installed_terminology_qa_version: str = Field(min_length=1)
    installed_structural_qa_version: str = Field(min_length=1)
    historical_blocking_codes: list[str] = Field(default_factory=list)
    current_blocking_codes: list[str] = Field(default_factory=list)
    reproduces_under_current_qa: bool
    current_terminology_valid: bool
    current_structural_valid: bool
    findings: list[FinalQAFindingRecord] = Field(default_factory=list)
    competing_variants: list[FinalQACompetingVariantRecord] = Field(default_factory=list)
    structural: FinalQAStructuralRecord | None = None
    budget: FinalQABudgetRecord
    verdict: FinalQAVerdict
    verdict_reasons: list[str] = Field(default_factory=list)
    accepts_without_provider: bool
    locked_terminology_involved: bool
    #: A repair the provider never delivered left this job failed with its rejection intact.
    #: Restoring the draft is the recovery boundary's decision, so final QA stops here.
    provider_recovery_required: bool = False

    @model_validator(mode="after")
    def verdict_matches_its_evidence(self) -> "FinalQAJobDiagnosis":
        if self.accepts_without_provider != (self.verdict in ACCEPTS_WITHOUT_PROVIDER_VERDICTS):
            raise ValueError("accepts_without_provider must follow from the verdict")
        if self.verdict is not FinalQAVerdict.ACCEPTED_NO_CHANGE and not self.verdict_reasons:
            raise ValueError("every verdict other than accepted_no_change must state its reasons")
        if self.provider_recovery_required:
            if self.verdict is not FinalQAVerdict.OPERATOR_REQUIRED:
                raise ValueError("a job awaiting provider recovery is operator_required")
            return self
        clean = not self.current_blocking_codes
        if clean and self.verdict not in ACCEPTS_WITHOUT_PROVIDER_VERDICTS:
            raise ValueError("a job with no current blocking finding cannot require intervention")
        if not clean and self.verdict in ACCEPTS_WITHOUT_PROVIDER_VERDICTS:
            raise ValueError("a job with a current blocking finding is not provider-free")
        return self


class FinalQAAuditReport(ContractModel):
    """The read-only audit of every QA-rejected job in one run.

    Producing it mutates nothing. It is the artifact an operator reads before authorizing any
    part of the cleanup, and the artifact a later reader uses to check that what happened matches
    what was planned.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    final_qa_audit_engine_version: str = Field(min_length=1)
    final_qa_resolution_policy_version: str = Field(min_length=1)
    terminology_qa_version: str = Field(min_length=1)
    structural_qa_version: str = Field(min_length=1)
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    snapshot_id: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminology_file: Path | None = None
    terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    effective_registry_file: Path | None = None
    effective_registry_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    frozen_registry_is_current: bool
    job_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    qa_rejected_count: int = Field(ge=0)
    verdict_counts: dict[str, int] = Field(default_factory=dict)
    variant_class_counts: dict[str, int] = Field(default_factory=dict)
    jobs: list[FinalQAJobDiagnosis] = Field(default_factory=list)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def counts_match_the_jobs(self) -> "FinalQAAuditReport":
        if len(self.jobs) != self.qa_rejected_count:
            raise ValueError("the audit must diagnose exactly the QA-rejected jobs")
        expected: dict[str, int] = {}
        for job in self.jobs:
            expected[job.verdict.value] = expected.get(job.verdict.value, 0) + 1
        if self.verdict_counts != expected:
            raise ValueError("verdict counts must be derived from the diagnoses")
        return self


class FinalQAJobOutcome(ContractModel):
    """What the orchestrator actually did to one job, and what QA made of it."""

    job_id: str = Field(min_length=1)
    verdict: FinalQAVerdict
    action: Literal[
        "none",
        "reevaluated",
        "structurally_repaired",
        "repaired",
        "recovered_and_repaired",
        "escalated_and_repaired",
        "reported",
    ]
    #: What an applying run would do, stated even when nothing was done. A dry run answers the
    #: question an operator actually asked — what will happen — rather than only "nothing yet".
    planned_action: Literal[
        "none",
        "reevaluate",
        "repair_structure",
        "repair_translation",
        "recover_provider_failure",
        "escalate_and_repair",
        "report",
    ] = "none"
    acceptance_status_before: str = Field(min_length=1)
    acceptance_status_after: str = Field(min_length=1)
    provider_attempts_spent: int = Field(ge=0)
    structural_repairs_applied: int = Field(default=0, ge=0)
    escalation_granted: bool = False
    provider_recovery_authorized: bool = False
    cycles: int = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)


class FinalQAFinalizationReport(ContractModel):
    """The durable record of one ``finalize-translation-run`` execution."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    final_qa_orchestrator_version: str = Field(min_length=1)
    final_qa_resolution_policy_version: str = Field(min_length=1)
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    finalization_number: int = Field(ge=1)
    dry_run: bool
    provider: str | None = None
    max_cycles: int = Field(ge=1)
    repair_escalation_allowed: bool
    provider_recovery_allowed: bool = False
    audit: FinalQAAuditReport
    outcomes: list[FinalQAJobOutcome] = Field(default_factory=list)
    accepted_count: int = Field(ge=0)
    qa_rejected_count: int = Field(ge=0)
    operator_required_job_ids: list[str] = Field(default_factory=list)
    provider_calls: int = Field(ge=0)
    structural_repairs_applied: int = Field(default=0, ge=0)
    escalations_granted: int = Field(default=0, ge=0)
    run_status_after: str = Field(min_length=1)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def a_dry_run_never_reports_an_action(self) -> "FinalQAFinalizationReport":
        if not self.dry_run:
            return self
        if self.provider_calls:
            raise ValueError("a dry run must not report provider calls")
        if any(outcome.action not in {"none", "reported"} for outcome in self.outcomes):
            raise ValueError("a dry run must not report a mutating action")
        if any(outcome.provider_attempts_spent for outcome in self.outcomes):
            raise ValueError("a dry run must not report spent provider attempts")
        if self.structural_repairs_applied or self.escalations_granted:
            raise ValueError("a dry run must not report an applied repair or a granted budget")
        if any(
            outcome.structural_repairs_applied
            or outcome.escalation_granted
            or outcome.provider_recovery_authorized
            for outcome in self.outcomes
        ):
            raise ValueError("a dry run must not report a mutating outcome")
        return self


class TranslationRepairEscalation(ContractModel):
    """An explicit, bounded, one-shot grant of extra provider attempts to one job.

    A job whose repair budget is exhausted is *finished* as far as ordinary policy is concerned,
    and that has to stay true by default: silently topping the budget up would make the bound
    decorative. This record is the only way past it. It names the original bound it is widening,
    how many attempts it grants, and the blocking findings that justified the grant, and it is
    refused unless every one of those findings is mechanically repairable.

    It never resets a counter. ``attempt_count`` and the immutable attempts stay exactly as they
    are; only the ceiling moves, and only once.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    repair_escalation_version: str = Field(min_length=1)
    final_qa_resolution_policy_version: str = Field(min_length=1)
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    job_id: str = Field(min_length=1)
    escalation_number: int = Field(ge=1)
    original_max_attempts: int = Field(ge=1)
    granted_attempts: int = Field(ge=1)
    resulting_max_attempts: int = Field(ge=2)
    provider_attempts_used: int = Field(ge=1)
    repair_attempts_used: int = Field(ge=0)
    triggering_codes: list[str] = Field(default_factory=list)
    rejection_evidence_file: Path
    rejection_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: The attempt slot the grant authorizes. Recording it makes the grant answerable for one
    #: specific attempt rather than for "one more" in the abstract.
    authorized_attempt_number: int = Field(ge=2)
    #: Why an operator could not have reached acceptance any cheaper. A budget is only widened
    #: after the provider-free remedy has been tried and found inapplicable, and the structural
    #: diagnosis is the evidence for that.
    reason: str = Field(min_length=1)
    structural_diagnosis: StructuralRepairDiagnosis | None = None
    deterministic_repair_available: bool = False
    provider_contacted: Literal[False] = False
    authorized_by: Literal["operator"]
    created_at: AwareDatetime

    @model_validator(mode="after")
    def the_grant_is_self_evident(self) -> "TranslationRepairEscalation":
        if self.resulting_max_attempts != self.original_max_attempts + self.granted_attempts:
            raise ValueError("the resulting ceiling must be the original plus the grant")
        if self.provider_attempts_used < self.original_max_attempts:
            raise ValueError("an escalation only applies to an exhausted budget")
        if not self.triggering_codes:
            raise ValueError("an escalation must name the findings it is spent on")
        if self.deterministic_repair_available:
            raise ValueError("a provider budget is never widened while a free remedy remains")
        if self.structural_diagnosis is not None and self.structural_diagnosis.deterministic:
            raise ValueError("the recorded diagnosis contradicts the grant")
        if self.authorized_attempt_number != self.provider_attempts_used + 1:
            raise ValueError("an escalation authorizes exactly the next attempt slot")
        return self
