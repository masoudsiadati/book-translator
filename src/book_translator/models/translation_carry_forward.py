"""Contracts for provider-free carry-forward of historically QA-rejected outputs.

This is deliberately a separate contract from :mod:`translation_worker`'s reuse evidence. Reuse
carries an *effectively accepted* translation into a new run under proven request identity. The
record here carries a translation that its own run **rejected**, so the historical rejection and
its findings are part of the persisted evidence rather than something the carry-forward erases,
and the resulting acceptance origin is its own value.
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
    TranslationAcceptanceStatus,
)


class HistoricalCompatibilityTier(StrEnum):
    """How a historical provider request relates to the request the new run would send."""

    EXACT_RENDERED_REQUEST = "exact_rendered_request"
    GOVERNED_RESOURCE_DRIFT = "governed_resource_drift"
    #: The request differs only because user-adjudicated occurrence exemptions removed decisions
    #: from it; rendering the new request without them reproduces the historical one exactly.
    OCCURRENCE_EXEMPTION_DRIFT = "occurrence_exemption_drift"
    CONTRACT_CHANGED = "contract_changed"
    UNSAFE_DRIFT = "unsafe_drift"

    @property
    def eligible(self) -> bool:
        """Whether this tier may be re-evaluated at all; the other two are always refused."""
        return self in {
            HistoricalCompatibilityTier.EXACT_RENDERED_REQUEST,
            HistoricalCompatibilityTier.GOVERNED_RESOURCE_DRIFT,
            HistoricalCompatibilityTier.OCCURRENCE_EXEMPTION_DRIFT,
        }


class GovernedRegistryTransitionRecord(ContractModel):
    """One published governed registry edit on the path between two frozen registries."""

    event_id: str = Field(min_length=1)
    event_kind: str = Field(min_length=1)
    semantic_sha256_before: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")


class GovernedTerminologyDrift(ContractModel):
    """Whether the terminology move behind a changed request is governed end to end.

    ``registry`` lineage connects two registry semantic identities through decision corrections and
    applied remediation plans. ``series_policy`` lineage connects two frozen series-policy *files*
    (their SHA-256, stored in the same fields) through published policy amendments; there the
    registry is only a projection of the policy, so the policy is what has to be explained.
    """

    from_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    to_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    explained: bool
    transitions: list[GovernedRegistryTransitionRecord] = Field(default_factory=list)
    locked_decision_drift: list[str] = Field(default_factory=list)
    lineage_kind: Literal["registry", "series_policy"] = "registry"
    #: Series concepts whose change reaches this job and was proven not to have altered what the
    #: historical request asked for: the target and force are unchanged, authorized forms only
    #: widened, and every withdrawn alias the job contains is still governed by a decision that
    #: authorizes the target the historical request named for it.
    waived_term_changes: list[str] = Field(
        default_factory=list, exclude_if=lambda values: not values
    )

    @model_validator(mode="after")
    def path_is_contiguous_when_explained(self) -> "GovernedTerminologyDrift":
        if not self.explained:
            return self
        identity = self.from_semantic_sha256
        for transition in self.transitions:
            if transition.semantic_sha256_before != identity:
                raise ValueError("explained terminology drift requires a contiguous governed path")
            identity = transition.semantic_sha256_after
        if identity != self.to_semantic_sha256:
            raise ValueError("explained terminology drift must reach the recorded destination")
        return self


class HistoricalRequestCompatibility(ContractModel):
    """The compatibility proof recorded beside one carried-forward historical output."""

    policy_version: str
    tier: HistoricalCompatibilityTier
    provider_contacted: Literal[False] = False
    source_snapshot_id: str
    target_snapshot_id: str
    source_prompt_package_id: str = Field(pattern=r"^pp-[0-9a-f]{24}$")
    target_prompt_package_id: str = Field(pattern=r"^pp-[0-9a-f]{24}$")
    source_system_message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_system_message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_user_message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_user_message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_output_contract: str
    target_output_contract: str
    source_terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    target_terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )
    target_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )
    source_series_policy_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )
    target_series_policy_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )
    verified_identity_checks: list[str]
    changed_framed_sections: list[str] = Field(default_factory=list)
    observed_resource_drift: list[str] = Field(default_factory=list)
    terminology_drift: GovernedTerminologyDrift | None = None
    rationale: str = Field(min_length=1)
    #: The exemptions that reach this job's new request (occurrence-exemption tier only).
    occurrence_exemption_ids: list[str] = Field(
        default_factory=list, exclude_if=lambda values: not values
    )
    #: The new request re-rendered with those exemptions withheld; it must equal the historical
    #: user message, which is what proves the exemptions are the whole difference.
    counterfactual_user_message_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def tier_is_proven(self) -> "HistoricalRequestCompatibility":
        if not self.tier.eligible:
            raise ValueError("only an eligible compatibility tier may be persisted")
        if self.source_system_message_sha256 != self.target_system_message_sha256:
            raise ValueError("carry-forward requires an unchanged system contract")
        if self.source_output_contract != self.target_output_contract:
            raise ValueError("carry-forward requires an unchanged output contract")
        identical = self.source_user_message_sha256 == self.target_user_message_sha256
        exemption_tier = self.tier is HistoricalCompatibilityTier.OCCURRENCE_EXEMPTION_DRIFT
        if exemption_tier != bool(self.occurrence_exemption_ids) or exemption_tier != (
            self.counterfactual_user_message_sha256 is not None
        ):
            raise ValueError("occurrence exemptions and their counterfactual belong to one tier")
        if exemption_tier:
            if identical:
                raise ValueError("an exemption tier requires a changed rendered user message")
            if self.counterfactual_user_message_sha256 != self.source_user_message_sha256:
                raise ValueError(
                    "exemptions must be the whole difference from the historical request"
                )
            if self.terminology_drift is not None:
                raise ValueError("an exemption tier admits no terminology move")
            return self
        if self.tier is HistoricalCompatibilityTier.EXACT_RENDERED_REQUEST:
            if not identical:
                raise ValueError("exact rendered-request tier requires identical user messages")
            if self.changed_framed_sections:
                raise ValueError("an identical request cannot have changed framed sections")
            return self
        if identical:
            raise ValueError("governed-drift tier requires a changed rendered user message")
        drift = self.terminology_drift
        if drift is None or not drift.explained:
            raise ValueError("governed-drift tier requires an explained terminology lineage")
        if drift.locked_decision_drift:
            raise ValueError("locked or user-owned terminology drift is never waived")
        return self


class TranslationHistoricalReevaluationEvidence(ContractModel):
    """Immutable new-run provenance for one re-evaluated historical rejected output."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    carry_forward_engine_version: str
    execution_kind: Literal["historical_reevaluation"] = "historical_reevaluation"
    provider_contacted: Literal[False] = False
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    job_id: str
    carry_forward_number: int = Field(ge=1)
    source_run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    source_job_id: str
    source_attempt_id: str
    source_attempt_number: int = Field(ge=1)
    source_attempt_file: Path
    source_attempt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_provider: str
    source_provider_adapter_version: str
    source_provider_response_id: str | None = None
    source_model_returned: str | None = None
    source_translation_file: Path
    source_translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_acceptance_file: Path
    historical_acceptance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_reevaluation_file: Path | None = None
    historical_reevaluation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    historical_status: TranslationAcceptanceStatus
    historical_terminology_report: TerminologyValidationReport
    historical_structural_report: TranslationQAReport
    compatibility: HistoricalRequestCompatibility
    source_file: Path
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_file: Path
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminology_file: Path
    terminology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_file: Path
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    style_file: Path
    style_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminology_qa_version: str
    structural_qa_version: str
    terminology_report: TerminologyValidationReport
    structural_report: TranslationQAReport
    #: Absent on evidence from engine 1.0.0, which did not run alignment QA; present from 1.1.0,
    #: where acceptance requires it exactly as a fresh attempt's acceptance does.
    alignment_qa_version: str | None = Field(default=None, exclude_if=lambda value: value is None)
    alignment_report: TranslationAlignmentReport | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    resulting_status: TranslationAcceptanceStatus
    accepted: bool
    translation_file: Path
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def outcome_and_lineage_are_consistent(self) -> "TranslationHistoricalReevaluationEvidence":
        if self.historical_status is TranslationAcceptanceStatus.ACCEPTED:
            # An accepted output is carried here only when its request moved; an identical
            # request belongs to accepted-translation reuse and its own provenance.
            if self.compatibility.tier is HistoricalCompatibilityTier.EXACT_RENDERED_REQUEST:
                raise ValueError("an accepted output with an identical request is reused instead")
        elif self.historical_status is not TranslationAcceptanceStatus.QA_REJECTED:
            raise ValueError("only an assessed historical output can be carried forward")
        if (self.alignment_qa_version is None) != (self.alignment_report is None):
            raise ValueError("alignment QA version and report must be present together")
        if self.source_translation_sha256 != self.translation_sha256:
            raise ValueError("a carried-forward output must preserve the historical bytes")
        if (self.historical_reevaluation_file is None) != (
            self.historical_reevaluation_sha256 is None
        ):
            raise ValueError("historical re-evaluation file and hash must be present together")
        expected = (
            self.terminology_report.valid
            and self.structural_report.valid
            and (self.alignment_report is None or self.alignment_report.valid)
        )
        if self.accepted != expected:
            raise ValueError("accepted must equal the combined current QA validity")
        if (self.resulting_status is TranslationAcceptanceStatus.ACCEPTED) != self.accepted:
            raise ValueError("resulting status must match accepted")
        for report in (self.terminology_report, self.structural_report):
            if (
                report.source_sha256 != self.source_sha256
                or report.translation_sha256 != self.translation_sha256
            ):
                raise ValueError("current QA report hashes must match the carry-forward lineage")
        for historical in (
            self.historical_terminology_report,
            self.historical_structural_report,
        ):
            if historical.translation_sha256 != self.source_translation_sha256:
                raise ValueError("historical QA reports must describe the carried-forward output")
        return self
