"""Second-round semantic reconciliation over the residual blockers of a simulated plan.

The first reconciliation round reasons about the registry as committed.  Once its combined plan is
simulated, a *smaller* set of blockers survives in the prospective registry, and those blockers can
only be judged against that prospective state.  This module models that second round: it groups the
residual findings and alias cycles into components, records an immutable artifact whose lineage
names the round it follows, and folds the residual edits into a superseding final plan.

Nothing here mutates the real registry; every function takes a registry value and returns a value.
"""

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from book_translator.atomic_write import atomic_write_text
from book_translator.models import TerminologyRegistry
from book_translator.models.terminology_reconciliation import (
    ConflictRelation,
    LineageArtifact,
    LineageArtifactKind,
    ProtectedLookupCheck,
    RegistryEdit,
    ResidualComponent,
    ResidualReconciliationRun,
    ResidualResolution,
    ReviewerProvenance,
    TerminologyRemediationLineage,
)
from book_translator.models.terminology_remediation import (
    TerminologyRemediationAction,
    TerminologyRemediationCategory,
    TerminologyRemediationOperation,
    TerminologyRemediationPlan,
    TerminologyRemediationResolution,
    TerminologyRemediationSimulation,
    TerminologyVariantRemoval,
)
from book_translator.paths import ProjectPaths
from book_translator.terminology.reconciliation import TerminologyReconciliationError
from book_translator.terminology.registry import registry_identity
from book_translator.terminology.remediation_engine import (
    _apply_operations,
    _before_state_hash,
    _bump_patch,
    _stable_id,
    serialize_plan,
    serialize_simulation,
)

RESIDUAL_RECONCILIATION_ENGINE_VERSION = "1.0.0"
RESIDUAL_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class ResidualFinding:
    """One blocking finding observed in a prospective (simulated) registry."""

    finding_id: str
    source_term: str
    candidate_id: str
    decision_id: str
    related_source_terms: tuple[str, ...]
    relation: ConflictRelation
    detail: str


def alias_cycle_finding_id(prospective_semantic_sha256: str, cycle: tuple[str, ...]) -> str:
    """Derive the deterministic residual finding identity for one alias cycle.

    Alias cycles are found by a structural check rather than by the proposal validator, so they
    carry no audit finding of their own.  A residual round still needs a stable key to hang an
    operation on, and it is derived here exactly the way audit findings derive theirs.
    """
    return _stable_id(
        "rmf",
        {
            "prospective_semantic_sha256": prospective_semantic_sha256,
            "kind": "alias_cycle",
            "cycle": sorted(cycle),
        },
    )


class _Union:
    """Union-find whose grouping never depends on call order."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            low, high = sorted((left_root, right_root))
            self._parent[high] = low

    def members(self) -> tuple[str, ...]:
        return tuple(sorted(self._parent))

    def groups(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for item in sorted(self._parent):
            grouped[self.find(item)].append(item)
        return grouped


def build_residual_components(
    registry: TerminologyRegistry,
    findings: tuple[ResidualFinding, ...],
    cycles: tuple[tuple[str, ...], ...],
    *,
    prospective_semantic_sha256: str,
) -> tuple[ResidualComponent, ...]:
    """Group residual findings and alias cycles into the smallest closed families.

    A component is pulled together by the relations that actually triggered the residual work, and
    is then closed over shared canonical targets so that a shorthand and the full name it elides are
    reviewed as one family rather than in isolation.
    """
    candidate_by_source = {item.source_term: item for item in registry.candidates}
    decision_by_candidate = {item.candidate_id: item for item in registry.decisions}
    targets_by_source = {
        item.source_term: decision_by_candidate[item.candidate_id].target_term
        for item in registry.candidates
        if item.candidate_id in decision_by_candidate
    }
    sources_by_target: dict[str, list[str]] = defaultdict(list)
    for source, target in targets_by_source.items():
        sources_by_target[target].append(source)

    union = _Union()
    details: dict[str, list[str]] = defaultdict(list)
    relations: dict[str, set[ConflictRelation]] = defaultdict(set)
    finding_ids: dict[str, list[str]] = defaultdict(list)
    cycles_by_source: dict[str, tuple[str, ...]] = {}

    for finding in findings:
        union.add(finding.source_term)
        for related in finding.related_source_terms:
            union.union(finding.source_term, related)
    for cycle in cycles:
        for member in cycle:
            union.add(member)
            union.union(cycle[0], member)
            cycles_by_source[member] = tuple(cycle)

    # Close each family over shared canonical targets: an elided shorthand and its full form are
    # one terminology question, so they must not be reviewed as separate components.
    for source in union.members():
        shared_target = targets_by_source.get(source)
        if shared_target is None:
            continue
        for peer_source in sources_by_target.get(shared_target, []):
            if peer_source != source:
                union.union(source, peer_source)

    # Close over alias edges whose peer also owns a canonical decision.  That combination is the
    # misclassification hazard itself - the alias claims a source form another decision already
    # owns - so resolving one edge of such a family while leaving the others is how a residual
    # round leaves a new blocker behind.
    alias_details: list[tuple[str, str]] = []
    pending = list(union.members())
    seen: set[str] = set()
    while pending:
        source = pending.pop()
        if source in seen:
            continue
        seen.add(source)
        candidate = candidate_by_source.get(source)
        if candidate is None:
            continue
        for alias in candidate.aliases:
            peer_candidate = candidate_by_source.get(alias)
            if peer_candidate is None or peer_candidate.candidate_id not in decision_by_candidate:
                continue
            union.union(source, alias)
            alias_details.append(
                (source, f"{source!r} lists alias {alias!r}, which owns its own canonical decision")
            )
            pending.append(alias)

    for source, detail in alias_details:
        root = union.find(source)
        details[root].append(detail)
        relations[root].add(ConflictRelation.ALIAS_RELATIONSHIP)
    for finding in findings:
        root = union.find(finding.source_term)
        finding_ids[root].append(finding.finding_id)
        relations[root].add(finding.relation)
        details[root].append(finding.detail)
    for cycle in cycles:
        root = union.find(cycle[0])
        relations[root].add(ConflictRelation.ALIAS_RELATIONSHIP)
        details[root].append(f"alias cycle {' <-> '.join(cycle)}")
        finding_ids[root].append(alias_cycle_finding_id(prospective_semantic_sha256, cycle))

    components: list[ResidualComponent] = []
    for root, members in sorted(union.groups().items()):
        source_terms = sorted(members)
        candidate_ids = sorted(
            candidate_by_source[item].candidate_id
            for item in source_terms
            if item in candidate_by_source
        )
        decision_ids = sorted(
            decision_by_candidate[item].decision_id
            for item in candidate_ids
            if item in decision_by_candidate
        )
        shared = {targets_by_source[item] for item in source_terms if item in targets_by_source}
        component_relations = set(relations[root])
        if len(shared) < len([item for item in source_terms if item in targets_by_source]):
            component_relations.add(ConflictRelation.SHARED_TARGET)
        cycle = next(
            (cycles_by_source[item] for item in source_terms if item in cycles_by_source), ()
        )
        components.append(
            ResidualComponent(
                component_id=_stable_id(
                    "trc",
                    {
                        "prospective_semantic_sha256": prospective_semantic_sha256,
                        "source_terms": source_terms,
                    },
                ),
                source_terms=source_terms,
                candidate_ids=candidate_ids,
                decision_ids=decision_ids,
                finding_ids=sorted(set(finding_ids[root])),
                relations=sorted(component_relations, key=lambda item: item.value),
                relation_details=sorted(set(details[root])),
                alias_cycle=list(cycle),
                size=len(source_terms),
            )
        )
    return tuple(sorted(components, key=lambda item: item.component_id))


def build_residual_run(
    components: tuple[ResidualComponent, ...],
    resolutions: tuple[ResidualResolution, ...],
    provenance: ReviewerProvenance,
    *,
    previous_run_id: str,
    previous_resolution_set_sha256: str,
    previous_plan_id: str,
    provenance_correction_id: str | None,
    source_registry_semantic_sha256: str,
    source_registry_raw_sha256: str,
    prospective_semantic_sha256: str,
    created_at: datetime | None = None,
) -> ResidualReconciliationRun:
    """Assemble the immutable residual reconciliation artifact and its explicit lineage."""
    finding_ids = sorted({value for item in components for value in item.finding_ids})
    cycles = [list(item.alias_cycle) for item in components if item.alias_cycle]
    return ResidualReconciliationRun(
        schema_version=RESIDUAL_SCHEMA_VERSION,
        run_id=_stable_id(
            "tsr-residual",
            {
                "engine_version": RESIDUAL_RECONCILIATION_ENGINE_VERSION,
                "previous_run_id": previous_run_id,
                "previous_plan_id": previous_plan_id,
                "prospective_semantic_sha256": prospective_semantic_sha256,
                "component_ids": [item.component_id for item in components],
            },
        ),
        engine_version=RESIDUAL_RECONCILIATION_ENGINE_VERSION,
        previous_run_id=previous_run_id,
        previous_resolution_set_sha256=previous_resolution_set_sha256,
        previous_plan_id=previous_plan_id,
        provenance_correction_id=provenance_correction_id,
        source_registry_semantic_sha256=source_registry_semantic_sha256,
        source_registry_raw_sha256=source_registry_raw_sha256,
        prospective_semantic_sha256=prospective_semantic_sha256,
        residual_finding_ids=finding_ids,
        residual_alias_cycles=cycles,
        components=sorted(components, key=lambda item: item.component_id),
        resolutions=sorted(resolutions, key=lambda item: item.component_id),
        outcome_counts=dict(sorted(Counter(item.outcome.value for item in resolutions).items())),
        escalation_count=sum(1 for item in resolutions if item.escalated),
        provenance=provenance,
        created_at=created_at or datetime.now(UTC),
    )


def residual_operations(
    registry: TerminologyRegistry, run: ResidualReconciliationRun
) -> list[TerminologyRemediationOperation]:
    """Turn each residual resolution into remediation operations against *registry*."""
    candidates = {item.candidate_id: item for item in registry.candidates}
    decision_by_candidate = {item.candidate_id: item for item in registry.decisions}
    components = {item.component_id: item for item in run.components}
    operations: list[TerminologyRemediationOperation] = []
    for resolution in sorted(run.resolutions, key=lambda item: item.component_id):
        component = components[resolution.component_id]
        if resolution.escalated or not resolution.edits:
            # Validator-only and escalated outcomes deliberately leave the registry alone.
            continue
        edits_by_candidate: dict[str, list[RegistryEdit]] = defaultdict(list)
        for edit in resolution.edits:
            edits_by_candidate[edit.candidate_id].append(edit)
        # Every edit of a component rides on its lowest finding so the family is applied once.
        finding_id = sorted(resolution.finding_ids or component.finding_ids)[0]
        anchor = sorted(edits_by_candidate)[0]
        candidate = candidates.get(anchor)
        if candidate is None:
            raise TerminologyReconciliationError(
                f"residual resolution references a missing candidate: {anchor}"
            )
        decision = decision_by_candidate.get(anchor)
        operations.append(
            TerminologyRemediationOperation(
                operation_id=_stable_id(
                    "rmo",
                    {
                        "residual_run_id": run.run_id,
                        "component_id": resolution.component_id,
                        "finding_id": finding_id,
                    },
                ),
                sequence=1,
                finding_id=finding_id,
                remediation_category=TerminologyRemediationCategory.ALIAS_MISCLASSIFICATION,
                action=TerminologyRemediationAction.APPLY_SEMANTIC_RESOLUTION,
                resolution=TerminologyRemediationResolution.DETERMINISTIC,
                mutates_registry=True,
                decision_id=decision.decision_id if decision is not None else None,
                candidate_id=anchor,
                source_term=candidate.source_term,
                target_term=decision.target_term if decision is not None else None,
                affected_decision_ids=list(component.decision_ids),
                affected_candidate_ids=list(component.candidate_ids),
                before_state_sha256=_before_state_hash(
                    registry, list(component.decision_ids), list(component.candidate_ids)
                ),
                edits=list(resolution.edits),
                reason=resolution.reasoning_summary,
                provenance={
                    "residual_reconciliation_run_id": run.run_id,
                    "previous_reconciliation_run_id": run.previous_run_id,
                    "component_id": resolution.component_id,
                    "outcome": resolution.outcome.value,
                    "classification": resolution.classification.value,
                    "reviewer": run.provenance.reviewer,
                    "confidence": resolution.confidence,
                    "prior_model_knowledge_used": resolution.prior_model_knowledge_used,
                },
            )
        )
    return operations


def final_remediation_plan(
    registry: TerminologyRegistry,
    combined: TerminologyRemediationPlan,
    run: ResidualReconciliationRun,
    *,
    created_at: datetime | None = None,
) -> TerminologyRemediationPlan:
    """Compose the superseding final plan: every combined operation plus the residual edits.

    The previous plan is read, never rewritten.  Residual operations sort after the first round's
    semantic operations so that a family the first round already touched is settled before the
    residual edits refine it.
    """
    if registry_identity(registry) != combined.source_registry_semantic_sha256:
        raise TerminologyReconciliationError(
            "the terminology registry drifted since the combined plan was created"
        )
    if run.previous_plan_id != combined.plan_id:
        raise TerminologyReconciliationError(
            "the residual run does not follow the supplied combined plan"
        )
    return _compose_plan(
        registry,
        combined,
        residual_operations(registry, run),
        identity={
            "engine_version": RESIDUAL_RECONCILIATION_ENGINE_VERSION,
            "supersedes_plan_id": combined.plan_id,
            "residual_run_id": run.run_id,
        },
        created_at=created_at,
    )


def _compose_plan(
    registry: TerminologyRegistry,
    base: TerminologyRemediationPlan,
    additional: list[TerminologyRemediationOperation],
    *,
    identity: dict[str, str],
    created_at: datetime | None,
) -> TerminologyRemediationPlan:
    """Re-sequence a base plan plus additional operations into a superseding plan value."""
    ordered = sorted(
        [*base.operations, *additional],
        key=lambda item: (
            0
            if item.action is TerminologyRemediationAction.REMOVE_INVALID_VARIANT
            else 1
            if item.action is TerminologyRemediationAction.APPLY_SEMANTIC_RESOLUTION
            else 2
            if item.action is TerminologyRemediationAction.IDENTITY_MIGRATION
            else 3,
            item.finding_id,
        ),
    )
    sequenced = [
        item.model_copy(update={"sequence": index}) for index, item in enumerate(ordered, start=1)
    ]
    unresolved = [
        item for item in sequenced if item.resolution is TerminologyRemediationResolution.UNRESOLVED
    ]
    mutating = [item for item in sequenced if item.mutates_registry]
    version_after = _bump_patch(registry.version) if mutating else registry.version
    expected = registry_identity(
        _apply_operations(registry, sequenced, target_version=version_after)
    )
    return base.model_copy(
        update={
            "plan_id": _stable_id(
                "trp",
                {**identity, "operation_ids": [item.operation_id for item in sequenced]},
            ),
            "registry_version_after": version_after,
            "operations": sequenced,
            "operation_count": len(sequenced),
            "deterministic_operation_count": len(sequenced) - len(unresolved),
            "unresolved_operation_count": len(unresolved),
            "mutating_operation_count": len(mutating),
            "action_counts": dict(sorted(Counter(item.action.value for item in sequenced).items())),
            "category_counts": dict(
                sorted(Counter(item.remediation_category.value for item in sequenced).items())
            ),
            "expected_semantic_sha256": expected,
            "applicable": not unresolved,
            "created_at": created_at or datetime.now(UTC),
        }
    )


def variant_invariant_operations(
    registry: TerminologyRegistry, *, existing_plan: TerminologyRemediationPlan | None = None
) -> list[TerminologyRemediationOperation]:
    """Plan a removal for every competing variant that is another decision's canonical target.

    ``_plan_variant_removals`` only searches lexically nested families - a holder reachable as the
    exact or component term of an audited finding.  Two unrelated names that merely share a word,
    such as ``Davram Bashere`` holding the canonical target of ``Lord of Bashere``, are never
    compared, so that class survives remediation and is reported only as a standing invariant
    violation.  This closes the gap globally rather than for one term.
    """
    candidates = {item.candidate_id: item for item in registry.candidates}
    # Scan the state the base plan actually leaves behind.  Its semantic resolutions already strip
    # most of these variants through remove_competing_variant edits, and re-planning those would
    # bloat the plan with no-ops instead of remediating what genuinely survives.
    residue = (
        _apply_operations(
            registry,
            list(existing_plan.operations),
            target_version=existing_plan.registry_version_after,
        )
        if existing_plan is not None
        else registry
    )
    owner_by_target = {
        item.target_term: item
        for item in sorted(residue.decisions, key=lambda value: value.decision_id)
    }
    operations: list[TerminologyRemediationOperation] = []
    for holder in sorted(residue.decisions, key=lambda item: item.decision_id):
        holder_candidate = candidates[holder.candidate_id]
        for variant in sorted(set(holder.competing_variants)):
            owner = owner_by_target.get(variant)
            if owner is None or owner.decision_id == holder.decision_id:
                continue
            owner_candidate = candidates[owner.candidate_id]
            removal = TerminologyVariantRemoval(
                holder_decision_id=holder.decision_id,
                holder_candidate_id=holder.candidate_id,
                holder_source_term=holder_candidate.source_term,
                holder_target_term=holder.target_term,
                removed_variant=variant,
                canonical_owner_decision_id=owner.decision_id,
                canonical_owner_source_term=owner_candidate.source_term,
                canonical_owner_target_term=owner.target_term,
            )
            finding_id = _stable_id(
                "rmf",
                {
                    "registry_semantic_sha256": registry_identity(registry),
                    "kind": "competing_variant_invariant",
                    "holder_decision_id": holder.decision_id,
                    "removed_variant": variant,
                },
            )
            operations.append(
                TerminologyRemediationOperation(
                    operation_id=_stable_id(
                        "rmo", {"kind": "variant_invariant", "finding_id": finding_id}
                    ),
                    sequence=1,
                    finding_id=finding_id,
                    remediation_category=(TerminologyRemediationCategory.INVALID_COMPETING_VARIANT),
                    action=TerminologyRemediationAction.REMOVE_INVALID_VARIANT,
                    resolution=TerminologyRemediationResolution.DETERMINISTIC,
                    mutates_registry=True,
                    decision_id=holder.decision_id,
                    candidate_id=holder.candidate_id,
                    source_term=holder_candidate.source_term,
                    target_term=holder.target_term,
                    affected_decision_ids=sorted({holder.decision_id, owner.decision_id}),
                    affected_candidate_ids=sorted({holder.candidate_id, owner.candidate_id}),
                    before_state_sha256=_before_state_hash(
                        registry,
                        [holder.decision_id, owner.decision_id],
                        [holder.candidate_id, owner.candidate_id],
                    ),
                    variant_removals=[removal],
                    reason=(
                        f"{holder_candidate.source_term!r} stores the canonical target of "
                        f"{owner_candidate.source_term!r} as a rejected competing variant, which "
                        "makes a correct rendering of the other term look like a QA violation"
                    ),
                    provenance={
                        "kind": "competing_variant_invariant",
                        "holder_decision_id": holder.decision_id,
                        "canonical_owner_decision_id": owner.decision_id,
                    },
                )
            )
    return operations


def harden_remediation_plan(
    registry: TerminologyRegistry,
    base: TerminologyRemediationPlan,
    *,
    created_at: datetime | None = None,
) -> TerminologyRemediationPlan:
    """Return a superseding plan that also clears every competing-variant invariant violation."""
    if registry_identity(registry) != base.source_registry_semantic_sha256:
        raise TerminologyReconciliationError(
            "the terminology registry drifted since the base plan was created"
        )
    additional = variant_invariant_operations(registry, existing_plan=base)
    if not additional:
        return base
    return _compose_plan(
        registry,
        base,
        additional,
        identity={
            "engine_version": RESIDUAL_RECONCILIATION_ENGINE_VERSION,
            "supersedes_plan_id": base.plan_id,
            "hardening": "competing_variant_invariant",
        },
        created_at=created_at,
    )


def serialize_residual_run(run: ResidualReconciliationRun) -> str:
    """Render the residual artifact deterministically with a single trailing newline."""
    payload = run.model_dump(mode="json", exclude_none=False)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


LINEAGE_SCHEMA_VERSION = "1.0"


def publish_lineage(
    project_root: Path,
    plan: TerminologyRemediationPlan,
    simulation: TerminologyRemediationSimulation,
    sources: dict[LineageArtifactKind, list[tuple[str, Path]]],
    *,
    protected_lookups: list[ProtectedLookupCheck],
    accepted_conditions: list[str],
    registry_path: Path,
    created_at: datetime | None = None,
) -> TerminologyRemediationLineage:
    """Copy exact artifact bytes into the durable plan directory and index them by hash.

    Historical objects are never re-serialized: each source file is published byte-for-byte so a
    reviewer can re-hash it and get the identity the artifact claims for itself.
    """
    paths = ProjectPaths(project_root)
    plan_directory = paths.terminology_remediation_plan(plan.plan_id)
    lineage_directory = paths.terminology_remediation_lineage(plan.plan_id)

    plan_text = serialize_plan(plan)
    simulation_text = serialize_simulation(simulation)
    atomic_write_text(plan_directory / "plan.json", plan_text, overwrite=True)
    atomic_write_text(plan_directory / "dry-run.json", simulation_text, overwrite=True)

    if plan.expected_semantic_sha256 is None:
        raise TerminologyReconciliationError(
            "a plan without an expected semantic identity cannot be published for apply"
        )

    # Freeze the exact source registry bytes in the content-addressed resource store.
    registry_bytes = registry_path.read_bytes()
    registry_sha = hashlib.sha256(registry_bytes).hexdigest()
    if registry_sha != plan.source_registry_raw_sha256:
        raise TerminologyReconciliationError(
            "the registry file does not match the plan's recorded source bytes"
        )
    frozen = paths.terminology_resource(registry_sha)
    if not frozen.is_file():
        atomic_write_text(frozen, registry_bytes.decode("utf-8"))
    elif hashlib.sha256(frozen.read_bytes()).hexdigest() != registry_sha:
        raise TerminologyReconciliationError("a conflicting frozen registry resource exists")

    artifacts: list[LineageArtifact] = [
        LineageArtifact(
            artifact_id=registry_sha,
            kind=LineageArtifactKind.FROZEN_SOURCE_REGISTRY,
            file=frozen.relative_to(project_root).as_posix(),
            sha256=registry_sha,
        )
    ]
    for kind, entries in sorted(sources.items(), key=lambda item: item[0].value):
        for artifact_id, source_path in sorted(entries):
            payload = source_path.read_bytes()
            destination = lineage_directory / f"{artifact_id}.json"
            atomic_write_text(destination, payload.decode("utf-8"), overwrite=True)
            artifacts.append(
                LineageArtifact(
                    artifact_id=artifact_id,
                    kind=kind,
                    file=destination.relative_to(project_root).as_posix(),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            )

    lineage = TerminologyRemediationLineage(
        schema_version=LINEAGE_SCHEMA_VERSION,
        plan_id=plan.plan_id,
        plan_file=(plan_directory / "plan.json").relative_to(project_root).as_posix(),
        plan_sha256=hashlib.sha256(plan_text.encode("utf-8")).hexdigest(),
        simulation_file=(plan_directory / "dry-run.json").relative_to(project_root).as_posix(),
        simulation_sha256=hashlib.sha256(simulation_text.encode("utf-8")).hexdigest(),
        supersedes_plan_ids=sorted(
            artifact.artifact_id
            for artifact in artifacts
            if artifact.kind is LineageArtifactKind.SUPERSEDED_PLAN
        ),
        audit_id=plan.audit_id,
        source_registry_file=registry_path.relative_to(project_root).as_posix(),
        source_registry_raw_sha256=plan.source_registry_raw_sha256,
        source_registry_semantic_sha256=plan.source_registry_semantic_sha256,
        expected_semantic_sha256=plan.expected_semantic_sha256,
        simulated_registry_raw_sha256=simulation.simulated_registry_raw_sha256,
        operation_count=plan.operation_count,
        artifacts=sorted(artifacts, key=lambda item: (item.kind.value, item.artifact_id)),
        protected_lookups=protected_lookups,
        lookup_regression_count=simulation.lookup_regression_count,
        accepted_conditions=accepted_conditions,
        created_at=created_at or datetime.now(UTC),
    )
    atomic_write_text(
        plan_directory / "lineage.json",
        json.dumps(
            lineage.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        overwrite=True,
    )
    return lineage


def verify_lineage(project_root: Path, plan_id: str) -> TerminologyRemediationLineage:
    """Re-hash every published file and confirm it still matches the manifest."""
    paths = ProjectPaths(project_root)
    manifest_path = paths.terminology_remediation_plan(plan_id) / "lineage.json"
    lineage = TerminologyRemediationLineage.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    if lineage.plan_id != plan_id:
        raise TerminologyReconciliationError("the lineage manifest names a different plan")
    for artifact in lineage.artifacts:
        published = project_root / artifact.file
        if not published.is_file():
            raise TerminologyReconciliationError(f"lineage artifact is missing: {artifact.file}")
        if hashlib.sha256(published.read_bytes()).hexdigest() != artifact.sha256:
            raise TerminologyReconciliationError(f"lineage artifact changed: {artifact.file}")
    for path, expected in (
        (project_root / lineage.plan_file, lineage.plan_sha256),
        (project_root / lineage.simulation_file, lineage.simulation_sha256),
    ):
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise TerminologyReconciliationError(f"published file changed: {path}")
    return lineage
