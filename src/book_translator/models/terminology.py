"""Generic contracts for terminology discovery, decisions, and validation."""

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel


class TerminologyType(StrEnum):
    PERSON = "person"
    PLACE = "place"
    ORGANIZATION = "organization"
    TITLE = "title"
    PEOPLE = "people"
    INVENTED = "invented"
    ARTIFACT = "artifact"
    HISTORICAL_EVENT = "historical_event"
    STORY_TERM = "story_term"
    RECURRING_PHRASE = "recurring_phrase"
    CONTEXT_SENSITIVE = "context_sensitive"


class CandidateStatus(StrEnum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"


class DecisionSource(StrEnum):
    """Who owns a canonical decision.

    ``imported`` is the legacy provenance carried by pre-registry glossary entries.  Ownership is
    deliberately independent of :class:`DecisionState` and of ``locked``: an AI may hold a
    confirmed decision, and a user-owned decision may still be provisional.
    """

    AI = "ai"
    USER = "user"
    IMPORTED = "imported"
    INFERRED = "inferred"


class DecisionState(StrEnum):
    """Whether a canonical decision is settled or a working choice pending evidence.

    ``provisional`` means the target is a translation choice made to keep the pipeline moving, not
    a factual claim about the source.  It is enforced exactly like a confirmed decision so that a
    book stays internally consistent, but it remains revisable and must carry an open question.
    """

    CONFIRMED = "confirmed"
    PROVISIONAL = "provisional"


class TerminologyEnforcement(StrEnum):
    """What kind of target evidence proves a canonical decision was honored.

    ``required`` answers whether a decision is in scope for QA at all; this answers the separate
    question of what counts as proof.  Collapsing the two forces every approved decision into
    "the exact canonical string must appear", which is correct for a name and wrong for a form the
    target language legitimately transforms.
    """

    EXACT = "exact"
    MORPHOLOGICAL = "morphological"
    CONTEXTUAL = "contextual"
    ADVISORY = "advisory"


def validate_authorized_target_forms(target_term: str | None, forms: list[str]) -> None:
    """Refuse authorized target forms that are empty, padded, repeated, or the target itself."""
    if any(not form or form != form.strip() for form in forms):
        raise ValueError("authorized target forms must be non-empty and carry no outer whitespace")
    if len(forms) != len(set(forms)) or target_term in forms:
        raise ValueError("authorized target forms must be unique and exclude the canonical target")


class TerminologyOccurrence(ContractModel):
    chapter_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    chunk_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    location: Literal["source", "context"] = "source"
    count: int = Field(default=1, ge=1)


class TerminologyCandidate(ContractModel):
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    classification: TerminologyType
    confidence: float = Field(ge=0.0, le=1.0)
    status: CandidateStatus = CandidateStatus.CANDIDATE
    provenance: DecisionSource
    aliases: list[str] = Field(default_factory=list)
    occurrences: list[TerminologyOccurrence] = Field(default_factory=list)
    context_sensitive: bool = False
    classification_conflicts: list[TerminologyType] = Field(default_factory=list)
    evidence_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_variants(self) -> "TerminologyCandidate":
        if self.source_term in self.aliases or len(self.aliases) != len(set(self.aliases)):
            raise ValueError("candidate aliases must be unique and exclude the primary source term")
        if self.classification is TerminologyType.CONTEXT_SENSITIVE and not self.context_sensitive:
            raise ValueError("context-sensitive classification requires context_sensitive=true")
        if self.classification in self.classification_conflicts or len(
            self.classification_conflicts
        ) != len(set(self.classification_conflicts)):
            raise ValueError("classification conflicts must be unique and exclude the primary type")
        if len(self.evidence_notes) != len(set(self.evidence_notes)):
            raise ValueError("candidate evidence notes must be unique")
        return self


class TerminologyDecision(ContractModel):
    decision_id: str = Field(pattern=r"^td-[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    target_term: str = Field(min_length=1)
    decision_source: DecisionSource
    decision_state: DecisionState = DecisionState.CONFIRMED
    locked: bool = False
    required: bool = True
    enforcement: TerminologyEnforcement | None = None
    competing_variants: list[str] = Field(default_factory=list)
    contextual_notes: list[str] = Field(default_factory=list)
    #: Further target realizations a person or the project explicitly authorized for this one
    #: decision (a participle beside a verbal noun, say). ``target_term`` stays the preferred
    #: rendering; these are permitted grammatical shapes of it, never competing translations, and
    #: they are never inferred. Omitted from serialization when empty, so every registry written
    #: before the field existed keeps its exact bytes and identity.
    allowed_target_forms: list[str] = Field(
        default_factory=list, exclude_if=lambda forms: not forms
    )

    @model_validator(mode="after")
    def validate_variants(self) -> "TerminologyDecision":
        if self.target_term in self.competing_variants or len(self.competing_variants) != len(
            set(self.competing_variants)
        ):
            raise ValueError("competing variants must be unique and exclude the canonical target")
        validate_authorized_target_forms(self.target_term, self.allowed_target_forms)
        if set(self.allowed_target_forms) & set(self.competing_variants):
            raise ValueError("an authorized target form must not also be a competing variant")
        if self.locked and self.decision_source is not DecisionSource.USER:
            raise ValueError("only an explicit user decision may be locked")
        if self.locked and self.decision_state is DecisionState.PROVISIONAL:
            raise ValueError("a provisional decision must not be locked")
        if self.locked and self.enforcement not in {None, TerminologyEnforcement.EXACT}:
            raise ValueError("a locked user decision must be enforced exactly")
        return self


class TerminologyRegistry(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    registry_kind: Literal["terminology"] = "terminology"
    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    book_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    candidates: list[TerminologyCandidate] = Field(default_factory=list)
    decisions: list[TerminologyDecision] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_links(self) -> "TerminologyRegistry":
        candidate_ids = [item.candidate_id for item in self.candidates]
        decision_ids = [item.decision_id for item in self.decisions]
        if len(candidate_ids) != len(set(candidate_ids)) or len(decision_ids) != len(
            set(decision_ids)
        ):
            raise ValueError("terminology candidate and decision IDs must be unique")
        known = set(candidate_ids)
        if any(item.candidate_id not in known for item in self.decisions):
            raise ValueError("every terminology decision must reference a candidate")
        if len({item.candidate_id for item in self.decisions}) != len(self.decisions):
            raise ValueError("a candidate may have at most one canonical decision")
        return self


class TerminologyFinding(ContractModel):
    code: str
    severity: Literal["warning", "error"]
    candidate_id: str | None = None
    source_term: str | None = None
    enforcement: TerminologyEnforcement | None = None
    message: str


class TerminologyValidationReport(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    valid: bool
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checked_decisions: int = Field(ge=0)
    findings: list[TerminologyFinding] = Field(default_factory=list)


class TranslationQAFinding(ContractModel):
    code: str
    severity: Literal["warning", "error"]
    message: str


class TranslationQAReport(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    valid: bool
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_paragraph_count: int = Field(ge=0)
    translation_paragraph_count: int = Field(ge=0)
    findings: list[TranslationQAFinding] = Field(default_factory=list)


class TerminologyDiscoveryEvidence(ContractModel):
    source_term: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    classification: TerminologyType
    confidence: float = Field(ge=0.0, le=1.0)
    context_sensitive: bool = False
    occurrences: list[TerminologyOccurrence] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class TerminologyDiscoveryResponse(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    candidates: list[TerminologyDiscoveryEvidence] = Field(default_factory=list)


class TerminologyCandidateOutput(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    candidates: list[TerminologyCandidate] = Field(default_factory=list)


class TerminologyProposal(ContractModel):
    proposal_id: str = Field(pattern=r"^tp-[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    proposed_target: str = Field(min_length=1)
    provider_proposed_target: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    classification: TerminologyType
    notes: list[str] = Field(default_factory=list)
    known_variants: list[str] = Field(default_factory=list)
    action: Literal["ai_proposal", "reuse_existing"] = "ai_proposal"
    canonical_source: str | None = None
    canonicalization_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    blocked: bool = False
    conflict_codes: list[str] = Field(default_factory=list)


class TerminologyProposalResponse(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    proposals: list[TerminologyProposal] = Field(default_factory=list)


class TerminologyReviewDisposition(StrEnum):
    ACCEPT = "accept"
    REVISE = "revise"
    REJECT = "reject"
    DEFER = "defer"


class TerminologyReview(ContractModel):
    review_id: str = Field(pattern=r"^tr-[0-9a-f]{24}$")
    proposal_id: str = Field(pattern=r"^tp-[0-9a-f]{24}$")
    disposition: TerminologyReviewDisposition
    reviewed_target: str | None
    provider_reviewed_target: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    meaning_fidelity: float = Field(ge=0.0, le=1.0)
    target_naturalness: float = Field(ge=0.0, le=1.0)
    transliteration_stability: float = Field(ge=0.0, le=1.0)
    terminology_consistency: float = Field(ge=0.0, le=1.0)
    register_appropriateness: float = Field(ge=0.0, le=1.0)
    morphology_suitability: float = Field(ge=0.0, le=1.0)
    ambiguity_safety: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str]
    notes: list[str]
    blocked: bool = False
    conflict_codes: list[str] = Field(default_factory=list)


class TerminologyReviewResponse(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    reviews: list[TerminologyReview] = Field(default_factory=list)


class TerminologyAnalysisStage(StrEnum):
    DISCOVERY = "discovery"
    PROPOSAL = "proposal"
    REVIEW = "review"


class TerminologyAnalysisStatus(StrEnum):
    VALIDATED = "validated"
    FAILED = "failed"


class TerminologySourceReference(ContractModel):
    chapter_id: str
    chunk_id: str
    source_file: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_file: str
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_word_count: int | None = Field(default=None, ge=0)


class TerminologyAnalysisRun(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    run_id: str = Field(pattern=r"^ta-[0-9a-f]{24}$")
    stage: TerminologyAnalysisStage
    engine_version: str
    snapshot_id: str
    snapshot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_lineage_kind: Literal["translation_snapshot", "translation_ready_corpus"] = (
        "translation_snapshot"
    )
    source_audit_id: str | None = None
    source_anomalies_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_language: str
    target_language: str
    sources: list[TerminologySourceReference] = Field(default_factory=list)
    discovery_run_id: str | None = None
    proposal_run_id: str | None = None
    proposal_output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    registry_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    canonical_view_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    style_resource_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    target_style_version: str | None = None
    provider: str
    provider_adapter_version: str
    model: str | None = None
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_file: str
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_file: str
    status: TerminologyAnalysisStatus
    contacted: bool = True
    failure_code: str | None = None
    failure_message: str | None = None
    retry_of: str | None = Field(default=None, pattern=r"^ta-[0-9a-f]{24}$")
    created_at: AwareDatetime


class TerminologyAnalysisValidationReport(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    run_id: str
    valid: bool
    stage: TerminologyAnalysisStage
    source_count: int = Field(ge=0)
    chapter_count: int = Field(default=0, ge=0)
    source_word_count: int = Field(default=0, ge=0)
    raw_occurrence_count: int = Field(default=0, ge=0)
    provider_call_count: int = Field(default=0, ge=0)
    item_count: int = Field(ge=0)
    merged_duplicate_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    created_at: AwareDatetime


class TerminologyAcceptancePolicy(StrEnum):
    MANUAL = "manual"
    HIGH_CONFIDENCE = "high-confidence"
    AUTOMATIC = "automatic"


class TerminologyApplicationFinding(ContractModel):
    proposal_id: str
    code: str
    message: str


class TerminologyApplicationResult(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    policy: TerminologyAcceptancePolicy
    threshold: float = Field(ge=0.0, le=1.0)
    considered: int = Field(ge=0)
    applied: int = Field(ge=0)
    skipped: int = Field(ge=0)
    conflicts: int = Field(ge=0)
    findings: list[TerminologyApplicationFinding] = Field(default_factory=list)
