"""Contracts for targeted semantic terminology conflict reconciliation."""

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel


class ConflictRelation(StrEnum):
    """Why two terminology records belong to the same conflict component."""

    EXACT_CANONICAL_CONFLICT = "exact_canonical_conflict"
    ALIAS_RELATIONSHIP = "alias_relationship"
    CANONICAL_SUBTERM = "canonical_subterm"
    COMPETING_VARIANT = "competing_variant"
    NORMALIZATION_IDENTITY = "normalization_identity"
    SHARED_TARGET = "shared_target"


class ConflictNodeKind(StrEnum):
    DECISION = "decision"
    CANDIDATE_WITHOUT_DECISION = "candidate_without_decision"


class ConflictNode(ContractModel):
    node_id: str = Field(min_length=1)
    kind: ConflictNodeKind
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    decision_id: str | None = Field(default=None, pattern=r"^td-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    target_term: str | None = None
    aliases: list[str] = Field(default_factory=list)
    competing_variants: list[str] = Field(default_factory=list)
    decision_source: str | None = None
    locked: bool = False
    candidate_status: str = Field(min_length=1)


class ConflictEdge(ContractModel):
    relation: ConflictRelation
    source_node_id: str = Field(min_length=1)
    target_node_id: str = Field(min_length=1)
    detail: str = Field(min_length=1)


class ConflictComponent(ContractModel):
    """One connected terminology family that must be resolved as a whole."""

    component_id: str = Field(pattern=r"^tcc-[0-9a-f]{24}$")
    node_ids: list[str] = Field(min_length=1)
    decision_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    source_terms: list[str] = Field(default_factory=list)
    relations: list[ConflictRelation] = Field(default_factory=list)
    unresolved_operation_ids: list[str] = Field(default_factory=list)
    unresolved_finding_ids: list[str] = Field(default_factory=list)
    size: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_size(self) -> "ConflictComponent":
        if self.size != len(self.node_ids):
            raise ValueError("component size must match its node count")
        if len(set(self.node_ids)) != len(self.node_ids):
            raise ValueError("component node identities must be unique")
        return self


class ConflictGraph(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    graph_id: str = Field(pattern=r"^tcg-[0-9a-f]{24}$")
    registry_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(pattern=r"^trp-[0-9a-f]{24}$")
    nodes: list[ConflictNode] = Field(default_factory=list)
    edges: list[ConflictEdge] = Field(default_factory=list)
    components: list[ConflictComponent] = Field(default_factory=list)
    unresolved_operation_count: int = Field(ge=0)
    covered_operation_count: int = Field(ge=0)
    component_count: int = Field(ge=0)
    component_size_histogram: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_coverage(self) -> "ConflictGraph":
        if self.component_count != len(self.components):
            raise ValueError("component_count must match the recorded components")
        covered = [
            value for component in self.components for value in component.unresolved_operation_ids
        ]
        if len(covered) != len(set(covered)):
            raise ValueError("an unresolved operation may belong to only one component")
        if self.covered_operation_count != len(covered):
            raise ValueError("covered_operation_count must match the recorded components")
        if self.covered_operation_count != self.unresolved_operation_count:
            raise ValueError("conflict components must cover every unresolved operation exactly")
        return self


class TermOccurrenceEvidence(ContractModel):
    """Bounded corpus evidence for one source form; never full prose passages."""

    source_form: str = Field(min_length=1)
    occurrence_count: int = Field(ge=0)
    chapter_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    chapters: list[str] = Field(default_factory=list)
    capitalized: bool = False
    always_capitalized: bool = False
    representative_contexts: list[str] = Field(default_factory=list)
    nearby_entities: list[str] = Field(default_factory=list)


class ComponentEvidence(ContractModel):
    component_id: str = Field(pattern=r"^tcc-[0-9a-f]{24}$")
    terms: list[TermOccurrenceEvidence] = Field(default_factory=list)
    legacy_glossary_entries: dict[str, str] = Field(default_factory=dict)
    user_owned_decisions: list[str] = Field(default_factory=list)
    locked_decisions: list[str] = Field(default_factory=list)
    related_canonical_terms: dict[str, str] = Field(default_factory=dict)
    historical_review_dispositions: dict[str, str] = Field(default_factory=dict)
    source_anomaly_ids: list[str] = Field(default_factory=list)
    corpus_chapter_count: int = Field(ge=0)
    corpus_chunk_count: int = Field(ge=0)


class ReconciliationClassification(StrEnum):
    """The semantic relationship the reviewer established for a component."""

    SAME_CONCEPT_ALIAS = "same_concept_alias"
    DISTINCT_CONCEPTS = "distinct_concepts"
    CANONICAL_AND_COMPOUND = "canonical_and_compound"
    WRONG_TARGET = "wrong_target"
    INVALID_RELATIONSHIP_ONLY = "invalid_relationship_only"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class RegistryEditKind(StrEnum):
    SET_TARGET = "set_target"
    ADD_ALIAS = "add_alias"
    REMOVE_ALIAS = "remove_alias"
    CREATE_DECISION = "create_decision"
    REMOVE_COMPETING_VARIANT = "remove_competing_variant"


class RegistryEdit(ContractModel):
    """One typed, auditable registry change derived from a semantic resolution."""

    kind: RegistryEditKind
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    decision_id: str | None = Field(default=None, pattern=r"^td-[0-9a-f]{24}$")
    value: str = Field(min_length=1)
    previous_value: str | None = None
    reason: str = Field(min_length=1)


class ReconciliationResolution(ContractModel):
    """The machine-readable outcome for exactly one conflict component."""

    component_id: str = Field(pattern=r"^tcc-[0-9a-f]{24}$")
    classification: ReconciliationClassification
    affected_decision_ids: list[str] = Field(default_factory=list)
    affected_candidate_ids: list[str] = Field(default_factory=list)
    canonical_source_forms: list[str] = Field(default_factory=list)
    existing_targets: dict[str, str] = Field(default_factory=dict)
    chosen_canonical_decisions: dict[str, str] = Field(default_factory=dict)
    aliases_to_retain: dict[str, list[str]] = Field(default_factory=dict)
    aliases_to_remove: dict[str, list[str]] = Field(default_factory=dict)
    variants_to_retain: dict[str, list[str]] = Field(default_factory=dict)
    variants_to_remove: dict[str, list[str]] = Field(default_factory=dict)
    targets_to_change: dict[str, str] = Field(default_factory=dict)
    composed_targets: dict[str, str] = Field(default_factory=dict)
    edits: list[RegistryEdit] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_references: list[str] = Field(default_factory=list)
    reasoning_summary: str = Field(min_length=1)
    escalated: bool = False
    escalation_reason: str | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> "ReconciliationResolution":
        insufficient = self.classification is ReconciliationClassification.INSUFFICIENT_EVIDENCE
        if insufficient is not self.escalated:
            raise ValueError("insufficient evidence and escalation must agree")
        if self.escalated and not self.escalation_reason:
            raise ValueError("an escalated component must record why it is ambiguous")
        if self.escalated and self.edits:
            raise ValueError("an escalated component must not carry registry edits")
        if not self.escalated and not self.evidence_references:
            raise ValueError("a resolved component must cite its evidence")
        for source, target in self.composed_targets.items():
            if self.targets_to_change.get(source) != target:
                raise ValueError("a composed target must also appear in targets_to_change")
        return self


class InferenceLocality(StrEnum):
    """Where the reasoning that produced a resolution actually ran."""

    LOCAL_PROCESS = "local_process"
    REMOTE_HOSTED = "remote_hosted"
    DETERMINISTIC_NONE = "deterministic_none"


class DisclosedMaterial(StrEnum):
    """Categories of source-derived material, ordered from least to most revealing."""

    TERMINOLOGY_NAMES = "terminology_names"
    TARGET_TRANSLATIONS = "target_translations"
    AGGREGATE_CORPUS_STATISTICS = "aggregate_corpus_statistics"
    ENTITY_COOCCURRENCE = "entity_cooccurrence"
    SENTENCE_EXCERPTS = "sentence_excerpts"
    PARAGRAPH_EXCERPTS = "paragraph_excerpts"
    CHUNK_EXCERPTS = "chunk_excerpts"


class ReviewerProvenance(ContractModel):
    """Separates local orchestration from the service that performed model inference."""

    reviewer: str = Field(min_length=1)
    reviewer_kind: str = Field(min_length=1)
    inference_locality: InferenceLocality
    inference_provider: str | None = None
    account_type: str | None = None
    local_orchestration: bool
    external_service_contacted: bool
    disclosed_material: list[DisclosedMaterial] = Field(default_factory=list)
    withheld_material: list[DisclosedMaterial] = Field(default_factory=list)
    prior_model_knowledge_used: bool = False
    disclosure_note: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_locality(self) -> "ReviewerProvenance":
        remote = self.inference_locality is InferenceLocality.REMOTE_HOSTED
        if remote and not self.external_service_contacted:
            raise ValueError("remote hosted inference always contacts an external service")
        if remote and not self.inference_provider:
            raise ValueError("remote hosted inference must name its inference provider")
        if self.inference_locality is InferenceLocality.LOCAL_PROCESS and (
            self.external_service_contacted
        ):
            raise ValueError("a local process reviewer must not report an external service")
        overlap = set(self.disclosed_material) & set(self.withheld_material)
        if overlap:
            raise ValueError("material cannot be both disclosed and withheld")
        return self


class ReconciliationProvenanceCorrection(ContractModel):
    """An explicit superseding provenance record for an immutable reconciliation run."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    correction_id: str = Field(pattern=r"^tsrc-[0-9a-f]{24}$")
    supersedes_run_id: str = Field(pattern=r"^tsr-run-[0-9a-f]{24}$")
    original_reviewer: str = Field(min_length=1)
    original_reviewer_kind: str = Field(min_length=1)
    original_external_service_contacted: bool
    corrected_provenance: ReviewerProvenance
    reason: str = Field(min_length=1)
    resolution_count: int = Field(ge=0)
    resolution_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolutions_unchanged: bool
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_correction(self) -> "ReconciliationProvenanceCorrection":
        if not self.resolutions_unchanged:
            raise ValueError(
                "a provenance correction must never alter the semantic resolutions it supersedes"
            )
        unchanged = (
            self.original_reviewer == self.corrected_provenance.reviewer
            and self.original_reviewer_kind == self.corrected_provenance.reviewer_kind
            and self.original_external_service_contacted
            == self.corrected_provenance.external_service_contacted
        )
        if unchanged:
            raise ValueError("a correction must change at least one provenance fact")
        return self


class ReconciliationShard(ContractModel):
    shard_id: str = Field(pattern=r"^tsr-[0-9a-f]{24}$")
    index: int = Field(ge=0)
    component_ids: list[str] = Field(min_length=1)
    decision_count: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    attempts: int = Field(default=0, ge=0)
    validated: bool = False


class ReconciliationRun(ContractModel):
    """An immutable record of one targeted semantic reconciliation."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    run_id: str = Field(pattern=r"^tsr-run-[0-9a-f]{24}$")
    engine_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    graph_id: str = Field(pattern=r"^tcg-[0-9a-f]{24}$")
    plan_id: str = Field(pattern=r"^trp-[0-9a-f]{24}$")
    registry_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1)
    reviewer_kind: str = Field(min_length=1)
    external_service_contacted: bool
    provenance: ReviewerProvenance | None = None
    shards: list[ReconciliationShard] = Field(default_factory=list)
    shard_count: int = Field(ge=0)
    provider_call_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    resolutions: list[ReconciliationResolution] = Field(default_factory=list)
    classification_counts: dict[str, int] = Field(default_factory=dict)
    escalation_count: int = Field(ge=0)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_coverage(self) -> "ReconciliationRun":
        if self.shard_count != len(self.shards):
            raise ValueError("shard_count must match the recorded shards")
        components = [item.component_id for item in self.resolutions]
        if len(components) != len(set(components)):
            raise ValueError("each conflict component must have exactly one resolution")
        sharded = [value for shard in self.shards for value in shard.component_ids]
        if sorted(sharded) != sorted(components):
            raise ValueError("shard coverage must exactly match the produced resolutions")
        if self.escalation_count != sum(1 for item in self.resolutions if item.escalated):
            raise ValueError("escalation_count must match the recorded resolutions")
        return self


class ResidualResolutionOutcome(StrEnum):
    """The disposition a residual reconciliation may reach for one component."""

    VALID_DISTINCT_TERMS = "valid_distinct_terms"
    CANONICAL_PLUS_COMPOUND = "canonical_plus_compound"
    CANONICAL_PLUS_SOURCE_ALIAS = "canonical_plus_source_alias"
    SINGULAR_PLURAL_NORMALIZATION = "singular_plural_normalization"
    REMOVE_FALSE_ALIAS = "remove_false_alias"
    REMOVE_FALSE_VARIANT = "remove_false_variant"
    REJECT_NOT_TERMINOLOGY = "reject_not_terminology"
    CHANGE_TARGET = "change_target"
    VALIDATOR_FALSE_POSITIVE = "validator_false_positive"
    ESCALATE = "escalate"


class ResidualComponent(ContractModel):
    """One residual terminology family that survived a combined remediation simulation."""

    component_id: str = Field(pattern=r"^trc-[0-9a-f]{24}$")
    source_terms: list[str] = Field(min_length=1)
    candidate_ids: list[str] = Field(default_factory=list)
    decision_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    relations: list[ConflictRelation] = Field(default_factory=list)
    relation_details: list[str] = Field(default_factory=list)
    alias_cycle: list[str] = Field(default_factory=list)
    size: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_component(self) -> "ResidualComponent":
        if self.size != len(self.source_terms):
            raise ValueError("size must match the recorded source terms")
        if not self.finding_ids and not self.alias_cycle:
            raise ValueError("a residual component must carry a finding or an alias cycle")
        return self


class ResidualResolution(ContractModel):
    """The machine-readable disposition for exactly one residual component."""

    component_id: str = Field(pattern=r"^trc-[0-9a-f]{24}$")
    outcome: ResidualResolutionOutcome
    classification: ReconciliationClassification
    finding_ids: list[str] = Field(default_factory=list)
    edits: list[RegistryEdit] = Field(default_factory=list)
    validator_rule_change: str | None = None
    resolves_alias_cycle: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_references: list[str] = Field(min_length=1)
    prior_model_knowledge_used: bool = False
    prior_model_knowledge_note: str | None = None
    reasoning_summary: str = Field(min_length=1)
    escalated: bool = False
    escalation_reason: str | None = None

    @model_validator(mode="after")
    def validate_residual_resolution(self) -> "ResidualResolution":
        escalate = self.outcome is ResidualResolutionOutcome.ESCALATE
        if escalate is not self.escalated:
            raise ValueError("the escalate outcome and the escalated flag must agree")
        if self.escalated and not self.escalation_reason:
            raise ValueError("an escalated component must record why it is ambiguous")
        if self.escalated and self.edits:
            raise ValueError("an escalated component must not carry registry edits")
        false_positive = self.outcome is ResidualResolutionOutcome.VALIDATOR_FALSE_POSITIVE
        if false_positive and self.edits:
            raise ValueError("a validator false positive must not mutate the registry")
        if false_positive and not self.validator_rule_change:
            raise ValueError("a validator false positive must name the general rule it fixed")
        if self.prior_model_knowledge_used and not self.prior_model_knowledge_note:
            raise ValueError("prior model knowledge must be described where it was used")
        return self


class ResidualReconciliationRun(ContractModel):
    """An immutable second-round reconciliation over the residual blockers only.

    The lineage fields are the point of the record: this run reasons against the *prospective*
    registry produced by a previous plan in simulation, so it must name the run it follows, the
    resolution set that run produced, the plan that was simulated, and the prospective hash the
    analysis actually saw.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    run_id: str = Field(pattern=r"^tsr-residual-[0-9a-f]{24}$")
    engine_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    previous_run_id: str = Field(pattern=r"^tsr-run-[0-9a-f]{24}$")
    previous_resolution_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_plan_id: str = Field(pattern=r"^trp-[0-9a-f]{24}$")
    provenance_correction_id: str | None = Field(default=None, pattern=r"^tsrc-[0-9a-f]{24}$")
    source_registry_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_registry_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prospective_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    residual_finding_ids: list[str] = Field(default_factory=list)
    residual_alias_cycles: list[list[str]] = Field(default_factory=list)
    components: list[ResidualComponent] = Field(min_length=1)
    resolutions: list[ResidualResolution] = Field(min_length=1)
    outcome_counts: dict[str, int] = Field(default_factory=dict)
    escalation_count: int = Field(ge=0)
    provenance: ReviewerProvenance
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_residual_run(self) -> "ResidualReconciliationRun":
        component_ids = [item.component_id for item in self.components]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("each residual component must appear exactly once")
        resolved = [item.component_id for item in self.resolutions]
        if sorted(resolved) != sorted(component_ids):
            raise ValueError("every residual component must have exactly one resolution")
        covered = sorted({value for item in self.components for value in item.finding_ids})
        if covered != sorted(set(self.residual_finding_ids)):
            raise ValueError("components must cover exactly the recorded residual findings")
        cycles = sorted(tuple(item.alias_cycle) for item in self.components if item.alias_cycle)
        if cycles != sorted(tuple(item) for item in self.residual_alias_cycles):
            raise ValueError("components must cover exactly the recorded residual alias cycles")
        if self.escalation_count != sum(1 for item in self.resolutions if item.escalated):
            raise ValueError("escalation_count must match the recorded resolutions")
        return self


class ProtectedLookupCheck(ContractModel):
    """Both halves of the protected-term question: did it change, and is it correct?"""

    source_form: str = Field(min_length=1)
    expected_target: str = Field(min_length=1)
    before_target: str | None = None
    after_target: str | None = None
    lookup_changed: bool
    lookup_correct: bool
    owner_source_term_before: str | None = None
    owner_source_term_after: str | None = None
    detail: str = Field(min_length=1)


class LineageArtifactKind(StrEnum):
    """What role a published lineage file plays in re-verifying a plan."""

    RECONCILIATION_RUN = "reconciliation_run"
    PROVENANCE_CORRECTION = "provenance_correction"
    RESIDUAL_RECONCILIATION_RUN = "residual_reconciliation_run"
    SUPERSEDED_PLAN = "superseded_plan"
    SUPERSEDED_SIMULATION = "superseded_simulation"
    REMEDIATION_AUDIT = "remediation_audit"
    CONFLICT_GRAPH = "conflict_graph"
    COMPONENT_EVIDENCE = "component_evidence"
    FROZEN_SOURCE_REGISTRY = "frozen_source_registry"


class LineageArtifact(ContractModel):
    """One immutable published file, addressed by its exact bytes."""

    artifact_id: str = Field(min_length=1)
    kind: LineageArtifactKind
    file: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_relative_posix(self) -> "LineageArtifact":
        if self.file.startswith("/") or "\\" in self.file:
            raise ValueError("a lineage file must be a project-relative POSIX path")
        return self


class TerminologyRemediationLineage(ContractModel):
    """Everything needed to re-verify one remediation plan without the session that built it."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    plan_id: str = Field(pattern=r"^trp-[0-9a-f]{24}$")
    plan_file: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    simulation_file: str = Field(min_length=1)
    simulation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    supersedes_plan_ids: list[str] = Field(default_factory=list)
    audit_id: str = Field(min_length=1)
    source_registry_file: str = Field(min_length=1)
    source_registry_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_registry_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    simulated_registry_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_count: int = Field(ge=0)
    artifacts: list[LineageArtifact] = Field(default_factory=list)
    protected_lookups: list[ProtectedLookupCheck] = Field(default_factory=list)
    lookup_regression_count: int = Field(ge=0)
    accepted_conditions: list[str] = Field(default_factory=list)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_lineage(self) -> "TerminologyRemediationLineage":
        ids = [item.artifact_id for item in self.artifacts]
        if len(ids) != len(set(ids)):
            raise ValueError("each lineage artifact must be published exactly once")
        if self.plan_id in self.supersedes_plan_ids:
            raise ValueError("a plan cannot supersede itself")
        if any(not item.lookup_correct for item in self.protected_lookups):
            raise ValueError("a published lineage must record only correct protected lookups")
        return self
