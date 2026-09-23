"""Contracts for deterministic terminology registry remediation planning and evidence."""

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.terminology import TerminologyEnforcement
from book_translator.models.terminology_question import QuestionCategory
from book_translator.models.terminology_reconciliation import RegistryEdit


class TerminologyFindingFamily(StrEnum):
    """Which read-only audit produced a remediation finding.

    The two families ask different questions of the same registry.  The canonical validator asks
    whether two canonical records contradict each other; exact enforcement asks whether a source
    form is the kind of thing that can have a fixed target lexeme at all.  A defect of the second
    kind raises no validator conflict, which is exactly why the planner needs the discriminator.
    """

    CANONICAL_VALIDATOR = "canonical_validator"
    EXACT_ENFORCEMENT = "exact_enforcement"


class TerminologyExactnessDisposition(StrEnum):
    """How certain the audit is about one exactly enforced decision.

    The disposition judges the *finding*; :class:`TerminologyRemediationResolution` judges whether
    the plan can act on it.  The two are deliberately independent: an ownership boundary makes a
    real defect ``unresolved`` while the plan's answer — change nothing — stays fully determined.
    """

    DEFINITELY_INVALID = "definitely_invalid"
    DEFINITELY_VALID = "definitely_valid"
    CONTEXT_REVIEW_REQUIRED = "context_review_required"
    UNRESOLVED = "unresolved"


class TerminologyRemediationCategory(StrEnum):
    VALID_COMPOUND_OR_MORPHOLOGY = "valid_compound_or_morphology"
    VALID_DISTINCT_TERM = "valid_distinct_term"
    DUPLICATE_CANONICAL = "duplicate_canonical"
    INVALID_COMPETING_VARIANT = "invalid_competing_variant"
    CONFLICTING_TARGET = "conflicting_target"
    ALIAS_MISCLASSIFICATION = "alias_misclassification"
    NORMALIZATION_IDENTITY_BUG = "normalization_identity_bug"
    REQUIRES_CONTEXT_REVIEW = "requires_context_review"
    NON_TERMINOLOGY_SOURCE_FORM = "non_terminology_source_form"
    FRAGMENT_OF_CANONICAL_TERM = "fragment_of_canonical_term"
    OVERBROAD_EXACT_ENFORCEMENT = "overbroad_exact_enforcement"
    EXACT_ENFORCEMENT_UPHELD = "exact_enforcement_upheld"
    EXACT_TARGET_REQUIRES_REVIEW = "exact_target_requires_review"
    COMPONENT_SPELLING_DIVERGENCE = "component_spelling_divergence"
    OWNER_GOVERNED_DECISION = "owner_governed_decision"


class TerminologyRemediationAction(StrEnum):
    NO_CHANGE_VALIDATOR_FIX = "no_change_validator_fix"
    NO_CHANGE_VALID_DISTINCT = "no_change_valid_distinct"
    MERGE_DECISIONS = "merge_decisions"
    REMOVE_INVALID_VARIANT = "remove_invalid_variant"
    CONVERT_TO_ALIAS = "convert_to_alias"
    CHANGE_TARGET_REQUIRES_REVIEW = "change_target_requires_review"
    IDENTITY_MIGRATION = "identity_migration"
    APPLY_SEMANTIC_RESOLUTION = "apply_semantic_resolution"
    MANUAL_CONTEXT_REVIEW = "manual_context_review"
    RETIRE_DECISION = "retire_decision"
    RETIRE_AND_REJECT_CANDIDATE = "retire_and_reject_candidate"
    CHANGE_ENFORCEMENT = "change_enforcement"
    CORRECT_TARGET = "correct_target"
    OPEN_TERMINOLOGY_QUESTION = "open_terminology_question"
    NO_CHANGE_EXACT_UPHELD = "no_change_exact_upheld"
    NO_CHANGE_OWNER_AUTHORITY = "no_change_owner_authority"


RETIREMENT_ACTIONS = frozenset(
    {
        TerminologyRemediationAction.RETIRE_DECISION,
        TerminologyRemediationAction.RETIRE_AND_REJECT_CANDIDATE,
    }
)


class TerminologyRemediationResolution(StrEnum):
    """Whether an operation's outcome is provable from stored governance evidence."""

    DETERMINISTIC = "deterministic"
    UNRESOLVED = "unresolved"


class TerminologyVariantRemoval(ContractModel):
    """One competing-variant relation that duplicates another decision's canonical target."""

    holder_decision_id: str = Field(pattern=r"^td-[0-9a-f]{24}$")
    holder_candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    holder_source_term: str = Field(min_length=1)
    holder_target_term: str = Field(min_length=1)
    removed_variant: str = Field(min_length=1)
    canonical_owner_decision_id: str = Field(pattern=r"^td-[0-9a-f]{24}$")
    canonical_owner_source_term: str = Field(min_length=1)
    canonical_owner_target_term: str = Field(min_length=1)


class TerminologyCandidateMigration(ContractModel):
    """An explicit, non-destructive normalized-identity migration record."""

    original_candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    original_source_term: str = Field(min_length=1)
    normalized_candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    normalized_source_term: str = Field(min_length=1)
    merged_occurrence_count: int = Field(ge=0)
    merged_alias_count: int = Field(ge=0)
    merged_evidence_note_count: int = Field(ge=0)
    historical_run_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_identities(self) -> "TerminologyCandidateMigration":
        if self.original_candidate_id == self.normalized_candidate_id:
            raise ValueError("a migration must link two distinct candidate identities")
        if self.original_source_term.strip() != self.normalized_source_term:
            raise ValueError("the normalized source term must be the stripped original source term")
        return self


class TerminologyDecisionRetirement(ContractModel):
    """The governed withdrawal of one canonical decision, with its superseded record preserved.

    There is deliberately no ``delete-terminology-decision`` command.  Removing a canonical
    decision is the most destructive change the registry allows — every downstream snapshot, QA
    report, and acceptance record was produced under it — so it exists only as this record inside
    an immutable plan, carrying the precondition hashes that prove the plan saw the decision it
    claims to withdraw.  ``locked`` is pinned to ``False`` by the contract itself: a locked user
    decision can never even be described as retired, let alone applied.
    """

    decision_id: str = Field(pattern=r"^td-[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    retired_target_term: str = Field(min_length=1)
    retired_enforcement: TerminologyEnforcement | None = None
    resolved_enforcement: TerminologyEnforcement
    decision_source: str = Field(min_length=1)
    decision_state: str = Field(min_length=1)
    locked: Literal[False] = False
    retired_aliases: list[str] = Field(default_factory=list)
    retired_competing_variants: list[str] = Field(default_factory=list)
    occurrence_count: int = Field(ge=0)
    reject_candidate: bool
    candidate_status_before: str = Field(min_length=1)
    candidate_status_after: str = Field(min_length=1)
    rejection_reason: str | None = None
    owner_authorization: str | None = None
    decision_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_retirement(self) -> "TerminologyDecisionRetirement":
        if self.decision_source == "user" and not self.owner_authorization:
            raise ValueError(
                "a user-owned decision may only be retired under explicit recorded authorization"
            )
        if self.reject_candidate:
            if self.candidate_status_after != "rejected":
                raise ValueError("rejecting a candidate must leave it in the rejected status")
            if not self.rejection_reason:
                raise ValueError("a rejection must record why the candidate is not terminology")
        elif self.candidate_status_after != self.candidate_status_before:
            raise ValueError("retiring a decision alone must not change the candidate status")
        return self


class TerminologyEnforcementChange(ContractModel):
    """A relaxation of what target evidence one decision demands, changing no target text."""

    decision_id: str = Field(pattern=r"^td-[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    target_term: str = Field(min_length=1)
    enforcement_before: TerminologyEnforcement | None = None
    resolved_enforcement_before: TerminologyEnforcement
    enforcement_after: TerminologyEnforcement
    locked: Literal[False] = False
    decision_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_change(self) -> "TerminologyEnforcementChange":
        if self.enforcement_after is self.enforcement_before:
            raise ValueError("an enforcement change must change the recorded enforcement mode")
        if self.enforcement_after is TerminologyEnforcement.EXACT:
            raise ValueError("this operation only relaxes enforcement; it never imposes exactness")
        if self.enforcement_after is self.resolved_enforcement_before:
            raise ValueError("an enforcement change must change the enforcement actually in effect")
        return self


class TerminologyTargetCorrection(ContractModel):
    """One canonical target rewritten to spell a decided component the way that component spells it.

    Composing a target is a translation act and stays forbidden.  This is the narrow opposite case:
    the compound already renders its component, in the right place and the right order, using a
    spelling the registry does not recognize — so the correction substitutes the component's own
    canonical target for exactly the one token standing in for it and copies every other token
    verbatim.  No new lexeme is invented, and the recorded ``replaced_token`` makes the whole edit
    re-derivable from the two decisions it joins.
    """

    decision_id: str = Field(pattern=r"^td-[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    target_before: str = Field(min_length=1)
    target_after: str = Field(min_length=1)
    component_source_term: str = Field(min_length=1)
    component_decision_id: str = Field(pattern=r"^td-[0-9a-f]{24}$")
    component_target_term: str = Field(min_length=1)
    replaced_token: str = Field(min_length=1)
    locked: Literal[False] = False
    decision_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_correction(self) -> "TerminologyTargetCorrection":
        if self.target_before == self.target_after:
            raise ValueError("a target correction must change the recorded target")
        if self.replaced_token == self.component_target_term:
            raise ValueError("the replaced token must differ from the canonical component target")
        if self.replaced_token not in self.target_before.split():
            raise ValueError("the replaced token must be a whole token of the previous target")
        if self.target_after != " ".join(
            self.component_target_term if token == self.replaced_token else token
            for token in self.target_before.split()
        ):
            raise ValueError(
                "the corrected target must be the previous target with exactly the replaced token"
                " rewritten as the component's canonical target"
            )
        return self


class TerminologyQuestionRequest(ContractModel):
    """The durable terminology question one unresolvable finding requires, never opened by apply.

    Recording the request rather than writing the question keeps ``apply`` bounded to the registry:
    the plan states exactly which question must exist, deterministically identified, so the
    operator opens it through the question workflow that owns that file.
    """

    question_id: str = Field(pattern=r"^tq-[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    decision_id: str | None = Field(default=None, pattern=r"^td-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    provisional_target: str | None = None
    category: QuestionCategory
    question: str = Field(min_length=1)
    blocking: bool = False
    related_source_terms: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class TerminologyRemediationOperation(ContractModel):
    """One primary remediation action for exactly one audited finding."""

    operation_id: str = Field(pattern=r"^rmo-[0-9a-f]{24}$")
    sequence: int = Field(ge=1)
    finding_id: str = Field(pattern=r"^rmf-[0-9a-f]{24}$")
    finding_family: TerminologyFindingFamily = TerminologyFindingFamily.CANONICAL_VALIDATOR
    remediation_category: TerminologyRemediationCategory
    action: TerminologyRemediationAction
    resolution: TerminologyRemediationResolution
    exactness_disposition: TerminologyExactnessDisposition | None = None
    exactness_risks: list[str] = Field(default_factory=list)
    exactness_rule: str | None = None
    mutates_registry: bool
    decision_id: str | None = Field(default=None, pattern=r"^td-[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    target_term: str | None = None
    affected_decision_ids: list[str] = Field(default_factory=list)
    affected_candidate_ids: list[str] = Field(default_factory=list)
    before_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant_removals: list[TerminologyVariantRemoval] = Field(default_factory=list)
    edits: list[RegistryEdit] = Field(default_factory=list)
    component_id: str | None = Field(default=None, pattern=r"^tcc-[0-9a-f]{24}$")
    migration: TerminologyCandidateMigration | None = None
    retirement: TerminologyDecisionRetirement | None = None
    enforcement_change: TerminologyEnforcementChange | None = None
    target_correction: TerminologyTargetCorrection | None = None
    question_request: TerminologyQuestionRequest | None = None
    reason: str = Field(min_length=1)
    provenance: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    blocked_reason: str | None = None
    blocking_evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_resolution(self) -> "TerminologyRemediationOperation":
        unresolved = self.resolution is TerminologyRemediationResolution.UNRESOLVED
        if unresolved and not self.blocked_reason:
            raise ValueError("an unresolved operation must record why it is blocked")
        if not unresolved and self.blocked_reason:
            raise ValueError("a deterministic operation must not record a blocked reason")
        if unresolved and self.mutates_registry:
            raise ValueError("an unresolved operation must never mutate the registry")
        if self.variant_removals and self.action is not (
            TerminologyRemediationAction.REMOVE_INVALID_VARIANT
        ):
            raise ValueError("variant removals belong only to a remove_invalid_variant operation")
        if self.migration is not None and self.action is not (
            TerminologyRemediationAction.IDENTITY_MIGRATION
        ):
            raise ValueError("a migration record belongs only to an identity_migration operation")
        if self.edits and self.action is not (
            TerminologyRemediationAction.APPLY_SEMANTIC_RESOLUTION
        ):
            raise ValueError("registry edits belong only to a semantic resolution operation")
        self._validate_exactness_records()
        if self.mutates_registry and not (
            self.variant_removals
            or self.migration
            or self.edits
            or self.retirement
            or self.enforcement_change
            or self.target_correction
        ):
            raise ValueError("a mutating operation must record the change it performs")
        return self

    def _validate_exactness_records(self) -> None:
        retires = self.action in RETIREMENT_ACTIONS
        if retires is not (self.retirement is not None):
            raise ValueError("a retirement record belongs to exactly a retiring operation")
        if self.retirement is not None:
            rejects = self.action is TerminologyRemediationAction.RETIRE_AND_REJECT_CANDIDATE
            if self.retirement.reject_candidate is not rejects:
                raise ValueError("candidate rejection must agree with the retiring action")
            if self.retirement.decision_id != self.decision_id:
                raise ValueError("a retirement must withdraw the operation's own decision")
        changes = self.action is TerminologyRemediationAction.CHANGE_ENFORCEMENT
        if changes and self.enforcement_change is None:
            raise ValueError("a change_enforcement operation must record the change it performs")
        # A correction may carry one too: respelling a compound can be what makes its exact claim
        # over-broad, and the operation that causes that must also settle it.
        if self.enforcement_change is not None and not (
            changes or self.action is TerminologyRemediationAction.CORRECT_TARGET
        ):
            raise ValueError(
                "an enforcement change record belongs only to a change_enforcement or"
                " correct_target operation"
            )
        corrects = self.action is TerminologyRemediationAction.CORRECT_TARGET
        if corrects is not (self.target_correction is not None):
            raise ValueError(
                "a target correction record belongs to exactly a correct_target operation"
            )
        if self.target_correction is not None and (
            self.target_correction.decision_id != self.decision_id
        ):
            raise ValueError("a target correction must rewrite the operation's own decision")
        asks = self.action is TerminologyRemediationAction.OPEN_TERMINOLOGY_QUESTION
        if asks is not (self.question_request is not None):
            raise ValueError("a question request belongs to exactly an open_question operation")
        if self.question_request is not None and self.mutates_registry:
            raise ValueError("requesting a terminology question must never mutate the registry")
        if (self.finding_family is TerminologyFindingFamily.EXACT_ENFORCEMENT) is not (
            self.exactness_disposition is not None
        ):
            raise ValueError("an exact-enforcement operation must record its audit disposition")


class TerminologyRemediationPlan(ContractModel):
    """An immutable, content-addressed remediation plan bound to one registry state."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    plan_id: str = Field(pattern=r"^trp-[0-9a-f]{24}$")
    engine_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    audit_id: str = Field(pattern=r"^remediation-audit-[0-9a-f]{24}$")
    audit_schema_version: str = Field(pattern=r"^\d+\.\d+$")
    review_run_id: str | None = Field(default=None, pattern=r"^ta-[0-9a-f]{24}$")
    proposal_run_id: str | None = Field(default=None, pattern=r"^ta-[0-9a-f]{24}$")
    analysis_run_id: str | None = Field(default=None, pattern=r"^ta-[0-9a-f]{24}$")
    registry_file: str = Field(min_length=1)
    registry_version_before: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    registry_version_after: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    source_registry_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_registry_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    glossary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decisions_before: int = Field(ge=0)
    candidates_before: int = Field(ge=0)
    operations: list[TerminologyRemediationOperation] = Field(default_factory=list)
    operation_count: int = Field(ge=0)
    deterministic_operation_count: int = Field(ge=0)
    unresolved_operation_count: int = Field(ge=0)
    mutating_operation_count: int = Field(ge=0)
    exactness_operation_count: int = Field(default=0, ge=0)
    retirement_count: int = Field(default=0, ge=0)
    candidate_rejection_count: int = Field(default=0, ge=0)
    enforcement_change_count: int = Field(default=0, ge=0)
    target_correction_count: int = Field(default=0, ge=0)
    question_request_count: int = Field(default=0, ge=0)
    action_counts: dict[str, int] = Field(default_factory=dict)
    category_counts: dict[str, int] = Field(default_factory=dict)
    disposition_counts: dict[str, int] = Field(default_factory=dict)
    affected_decision_ids: list[str] = Field(default_factory=list)
    affected_candidate_ids: list[str] = Field(default_factory=list)
    expected_semantic_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    applicable: bool
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_counts(self) -> "TerminologyRemediationPlan":
        if self.operation_count != len(self.operations):
            raise ValueError("operation_count must match the recorded operations")
        identifiers = [item.operation_id for item in self.operations]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("remediation operation identities must be unique")
        findings = [item.finding_id for item in self.operations]
        if len(findings) != len(set(findings)):
            raise ValueError("each audited finding must have exactly one primary operation")
        sequences = [item.sequence for item in self.operations]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("remediation operations must be contiguously ordered from one")
        unresolved = sum(
            1
            for item in self.operations
            if item.resolution is TerminologyRemediationResolution.UNRESOLVED
        )
        if unresolved != self.unresolved_operation_count:
            raise ValueError("unresolved_operation_count must match the recorded operations")
        if self.deterministic_operation_count != self.operation_count - unresolved:
            raise ValueError("deterministic_operation_count must match the recorded operations")
        if self.applicable is not (unresolved == 0):
            raise ValueError("a plan is applicable only when no operation is unresolved")
        self._validate_exactness_totals()
        return self

    def _validate_exactness_totals(self) -> None:
        retirements = [item for item in self.operations if item.retirement is not None]
        if self.retirement_count != len(retirements):
            raise ValueError("retirement_count must match the recorded operations")
        if self.candidate_rejection_count != sum(
            1
            for item in retirements
            if item.retirement is not None and item.retirement.reject_candidate
        ):
            raise ValueError("candidate_rejection_count must match the recorded operations")
        if self.enforcement_change_count != sum(
            1 for item in self.operations if item.enforcement_change is not None
        ):
            raise ValueError("enforcement_change_count must match the recorded operations")
        if self.target_correction_count != sum(
            1 for item in self.operations if item.target_correction is not None
        ):
            raise ValueError("target_correction_count must match the recorded operations")
        if self.question_request_count != sum(
            1 for item in self.operations if item.question_request is not None
        ):
            raise ValueError("question_request_count must match the recorded operations")
        if self.exactness_operation_count != sum(
            1
            for item in self.operations
            if item.finding_family is TerminologyFindingFamily.EXACT_ENFORCEMENT
        ):
            raise ValueError("exactness_operation_count must match the recorded operations")
        decisions = [
            item.decision_id
            for item in self.operations
            if item.decision_id is not None
            and (
                item.retirement is not None
                or item.enforcement_change is not None
                or item.target_correction is not None
            )
        ]
        if len(decisions) != len(set(decisions)):
            raise ValueError("a decision may be rewritten by at most one operation")


class TerminologyLookupChangeKind(StrEnum):
    INTENDED_CORRECTION = "intended_correction"
    EQUIVALENT_PRESERVED = "equivalent_preserved"
    UNINTENDED_REGRESSION = "unintended_regression"


class TerminologyLookupComparison(ContractModel):
    """One read-only before/after canonical lookup for an affected source form."""

    source_form: str = Field(min_length=1)
    before_target: str | None = None
    after_target: str | None = None
    change_kind: TerminologyLookupChangeKind
    operation_id: str | None = Field(default=None, pattern=r"^rmo-[0-9a-f]{24}$")
    detail: str = Field(min_length=1)


class TerminologyRemediationSimulation(ContractModel):
    """The result of applying a plan to an isolated registry copy."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    plan_id: str = Field(pattern=r"^trp-[0-9a-f]{24}$")
    engine_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    dry_run: Literal[True] = True
    source_registry_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_registry_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    simulated_registry_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    simulated_registry_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    real_registry_raw_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")
    real_registry_unchanged: bool
    decisions_before: int = Field(ge=0)
    decisions_after: int = Field(ge=0)
    candidates_before: int = Field(ge=0)
    candidates_after: int = Field(ge=0)
    applied_operation_count: int = Field(ge=0)
    skipped_unresolved_count: int = Field(ge=0)
    decisions_retired: int = Field(default=0, ge=0)
    candidates_rejected: int = Field(default=0, ge=0)
    enforcement_changes_applied: int = Field(default=0, ge=0)
    target_corrections_applied: int = Field(default=0, ge=0)
    question_requests_recorded: int = Field(default=0, ge=0)
    exact_enforcement_findings_before: int = Field(default=0, ge=0)
    exact_enforcement_findings_after: int = Field(default=0, ge=0)
    retired_source_forms_resolved_after: dict[str, str | None] = Field(default_factory=dict)
    registry_valid_after: bool
    registry_validation_errors: list[str] = Field(default_factory=list)
    invariant_violations_before: int = Field(ge=0)
    invariant_violations: list[str] = Field(default_factory=list)
    invariant_violations_introduced: list[str] = Field(default_factory=list)
    audit_findings_before: int = Field(ge=0)
    audit_findings_after: int = Field(ge=0)
    blocking_findings_before: int = Field(ge=0)
    blocking_findings_after: int = Field(ge=0)
    # ``None`` records that the engine that produced this simulation never measured accounting, so
    # a historical dry run keeps its original, stricter meaning instead of being re-judged.
    unaccounted_blocking_findings_after: int | None = Field(default=None, ge=0)
    accounted_blocking_source_terms: list[str] = Field(default_factory=list)
    category_counts_after: dict[str, int] = Field(default_factory=dict)
    identity_mismatches_after: int = Field(ge=0)
    lookup_comparisons: list[TerminologyLookupComparison] = Field(default_factory=list)
    lookup_regression_count: int = Field(ge=0)
    protected_terms_unchanged: dict[str, bool] = Field(default_factory=dict)
    protected_terms_correct: dict[str, bool] = Field(default_factory=dict)
    protected_terms_before: dict[str, str | None] = Field(default_factory=dict)
    protected_terms_after: dict[str, str | None] = Field(default_factory=dict)
    locked_decisions_preserved: bool
    safe_to_apply: bool
    blockers: list[str] = Field(default_factory=list)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_safety(self) -> "TerminologyRemediationSimulation":
        # A blocking finding that survives is only tolerable when this very plan already routed
        # that decision to a human — an open question, an ownership refusal, or an unresolved
        # operation.  Anything else means the plan would leave a defect nobody was told about.
        remaining = (
            self.blocking_findings_after
            if self.unaccounted_blocking_findings_after is None
            else self.unaccounted_blocking_findings_after
        )
        unsafe = (
            remaining > 0
            or self.lookup_regression_count > 0
            or not self.registry_valid_after
            or bool(self.invariant_violations_introduced)
            or not self.real_registry_unchanged
            or not self.locked_decisions_preserved
            or self.skipped_unresolved_count > 0
            or not all(self.protected_terms_correct.values())
        )
        if self.safe_to_apply is unsafe:
            raise ValueError("safe_to_apply must reflect the recorded blocking evidence")
        if self.safe_to_apply and self.blockers:
            raise ValueError("a safe simulation must not record blockers")
        if not self.safe_to_apply and not self.blockers:
            raise ValueError("an unsafe simulation must record its blockers")
        return self
