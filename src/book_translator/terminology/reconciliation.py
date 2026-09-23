"""Targeted semantic reconciliation of terminology conflict components."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.models import TerminologyRegistry
from book_translator.models.terminology_reconciliation import (
    ComponentEvidence,
    ConflictComponent,
    ConflictGraph,
    ProtectedLookupCheck,
    ReconciliationClassification,
    ReconciliationProvenanceCorrection,
    ReconciliationResolution,
    ReconciliationRun,
    ReconciliationShard,
    RegistryEditKind,
    ReviewerProvenance,
)
from book_translator.models.terminology_remediation import (
    TerminologyRemediationAction,
    TerminologyRemediationCategory,
    TerminologyRemediationOperation,
    TerminologyRemediationPlan,
    TerminologyRemediationResolution,
)
from book_translator.terminology.canonical import canonical_view
from book_translator.terminology.registry import TerminologyError, registry_identity
from book_translator.terminology.remediation_engine import (
    REMEDIATION_ENGINE_VERSION,
    _apply_operations,
    _bump_patch,
    canonical_lookup,
)
from book_translator.terminology.target_style import canonical_targets_equal

RECONCILIATION_ENGINE_VERSION = "1.0.0"
RECONCILIATION_SCHEMA_VERSION = "1.0"
SHARD_MAX_COMPONENTS = 8
SHARD_MAX_DECISIONS = 24
SHARD_MAX_ATTEMPTS = 3

RECONCILIATION_SYSTEM_MESSAGE = (
    "Reconcile conflicting canonical book terminology decisions. You receive whole conflict "
    "families, never isolated records. Decide from source-language story evidence whether two "
    "source forms denote the same entity. Never infer an alias from matching target translations. "
    "Reject impossible cross-character aliases, geographic conflation, alias cycles, and titles or "
    "disguises treated as name aliases. Preserve established canonical target components and "
    "transliterations across compound forms. Never modify a locked user decision. Return only the "
    "required structured schema with one resolution per supplied component."
)

# Protected terms whose authoritative target is asserted by governance, not inferred.
PROTECTED_EXPECTATIONS: tuple[tuple[str, str], ...] = (
    ("Ajah", "آجای"),
    ("Red Ajah", "آجای سرخ"),
    ("Rand", "رند"),
    ("Rand al'Thor", "رند ال ثور"),
    ("Aes Sedai", "آیزسدای"),
    ("Forsaken", "مطرودین"),
    ("Whitecloak", "فرزند نور"),
    ("The One Power", "قدرت یگانه"),
    ("Saidar", "سایدار"),
    ("Saidin", "ساییدین"),
    ("Amyrlin Seat", "مقام امیرلین"),
    ("Nynaeve", "ناینیو"),
)


class TerminologyReconciliationError(TerminologyError):
    """Raised when semantic reconciliation cannot proceed safely."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        + b"\n"
    )


def _stable_id(prefix: str, payload: Any) -> str:
    return f"{prefix}-{hashlib.sha256(_canonical_bytes(payload)).hexdigest()[:24]}"


class ReconciliationRequest:
    """One provider-neutral shard request carrying whole conflict families."""

    def __init__(self, shard_id: str, system_message: str, user_message: str) -> None:
        self.shard_id = shard_id
        self.system_message = system_message
        self.user_message = user_message

    @property
    def request_sha256(self) -> str:
        return hashlib.sha256(
            _canonical_bytes({"system": self.system_message, "user": self.user_message})
        ).hexdigest()


class ReconciliationReviewer(Protocol):
    @property
    def provenance(self) -> ReviewerProvenance: ...

    def review(self, request: ReconciliationRequest) -> Any: ...


class FileReconciliationReviewer:
    """Read structured shard responses authored by an external reviewing agent.

    Reading a response file is local I/O; it says nothing about where the reasoning that
    produced that file ran, so the caller must supply the reviewer's real provenance.
    """

    def __init__(self, directory: Path, *, provenance: ReviewerProvenance) -> None:
        self._directory = directory
        self._provenance = provenance

    @property
    def provenance(self) -> ReviewerProvenance:
        return self._provenance

    def review(self, request: ReconciliationRequest) -> Any:
        path = self._directory / f"{request.shard_id}.response.json"
        if not path.is_file():
            raise TerminologyReconciliationError(
                f"missing reconciliation response for shard {request.shard_id}"
            )
        return json.loads(path.read_text(encoding="utf-8"))


def plan_reconciliation_shards(graph: ConflictGraph) -> tuple[ReconciliationShard, ...]:
    """Bound shards by component and decision count without splitting a component."""
    shards: list[ReconciliationShard] = []
    batch: list[ConflictComponent] = []

    def flush() -> None:
        if not batch:
            return
        component_ids = sorted(item.component_id for item in batch)
        shards.append(
            ReconciliationShard(
                shard_id=_stable_id(
                    "tsr", {"graph_id": graph.graph_id, "components": component_ids}
                ),
                index=len(shards),
                component_ids=component_ids,
                decision_count=sum(len(item.decision_ids) for item in batch),
                estimated_tokens=sum(240 * item.size for item in batch),
            )
        )
        batch.clear()

    for component in graph.components:
        projected = sum(len(item.decision_ids) for item in batch) + len(component.decision_ids)
        if batch and (len(batch) >= SHARD_MAX_COMPONENTS or projected > SHARD_MAX_DECISIONS):
            flush()
        batch.append(component)
    flush()
    covered = [value for shard in shards for value in shard.component_ids]
    if sorted(covered) != sorted(item.component_id for item in graph.components):
        raise TerminologyReconciliationError("shard plan does not exactly cover every component")
    return tuple(shards)


def render_shard_request(
    graph: ConflictGraph,
    evidence: dict[str, ComponentEvidence],
    shard: ReconciliationShard,
    registry: TerminologyRegistry,
) -> ReconciliationRequest:
    """Render one shard packet with richer-than-normal bounded family context."""
    components = {item.component_id: item for item in graph.components}
    node_by_id = {item.node_id: item for item in graph.nodes}
    payload: list[dict[str, Any]] = []
    for component_id in shard.component_ids:
        component = components[component_id]
        component_evidence = evidence[component_id]
        payload.append(
            {
                "component_id": component_id,
                "relations": [item.value for item in component.relations],
                "records": [
                    {
                        "candidate_id": node_by_id[node_id].candidate_id,
                        "decision_id": node_by_id[node_id].decision_id,
                        "source_term": node_by_id[node_id].source_term,
                        "target_term": node_by_id[node_id].target_term,
                        "aliases": node_by_id[node_id].aliases,
                        "competing_variants": node_by_id[node_id].competing_variants,
                        "decision_source": node_by_id[node_id].decision_source,
                        "locked": node_by_id[node_id].locked,
                        "candidate_status": node_by_id[node_id].candidate_status,
                    }
                    for node_id in component.node_ids
                ],
                "evidence": component_evidence.model_dump(mode="json"),
            }
        )
    contract = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "resolutions": [
            {
                "component_id": "<copy byte-for-byte>",
                "classification": [item.value for item in ReconciliationClassification],
                "edits": [{"kind": [item.value for item in RegistryEditKind]}],
            }
        ],
    }
    user_message = json.dumps(
        {
            "target_language": registry.target_language,
            "source_language": registry.source_language,
            "required_output_contract": contract,
            "components": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return ReconciliationRequest(shard.shard_id, RECONCILIATION_SYSTEM_MESSAGE, user_message)


def validate_shard_response(
    raw: Any,
    shard: ReconciliationShard,
    graph: ConflictGraph,
    registry: TerminologyRegistry,
) -> tuple[ReconciliationResolution, ...]:
    """Strictly validate one shard's structured resolutions before accepting it."""
    if not isinstance(raw, dict) or "resolutions" not in raw:
        raise TerminologyReconciliationError("reconciliation response must carry resolutions")
    try:
        resolutions = [ReconciliationResolution.model_validate(item) for item in raw["resolutions"]]
    except ValidationError as exc:
        raise TerminologyReconciliationError(f"malformed reconciliation response: {exc}") from exc

    expected = sorted(shard.component_ids)
    produced = sorted(item.component_id for item in resolutions)
    if produced != expected:
        raise TerminologyReconciliationError(
            "a shard must return exactly one resolution per supplied component"
        )

    components = {item.component_id: item for item in graph.components}
    candidates = {item.candidate_id: item for item in registry.candidates}
    decisions_by_candidate = {item.candidate_id: item for item in registry.decisions}
    for resolution in resolutions:
        component = components[resolution.component_id]
        allowed = set(component.candidate_ids)
        for edit in resolution.edits:
            if edit.candidate_id not in allowed:
                raise TerminologyReconciliationError(
                    f"edit escapes component {resolution.component_id}: {edit.candidate_id}"
                )
            decision = decisions_by_candidate.get(edit.candidate_id)
            if decision is not None and decision.locked:
                raise TerminologyReconciliationError(
                    "a locked user decision must never be edited by reconciliation"
                )
            if edit.kind is RegistryEditKind.CREATE_DECISION and decision is not None:
                raise TerminologyReconciliationError(
                    f"cannot create a decision that already exists: {edit.source_term!r}"
                )
            if (
                edit.kind in {RegistryEditKind.ADD_ALIAS, RegistryEditKind.REMOVE_ALIAS}
                and edit.value == candidates[edit.candidate_id].source_term
            ):
                raise TerminologyReconciliationError(
                    "a candidate may not alias its own source term"
                )
            if edit.kind is RegistryEditKind.SET_TARGET and not edit.value.strip():
                raise TerminologyReconciliationError("a target edit must supply a target term")
        if resolution.classification is ReconciliationClassification.SAME_CONCEPT_ALIAS and not any(
            edit.kind is RegistryEditKind.ADD_ALIAS for edit in resolution.edits
        ):
            raise TerminologyReconciliationError(
                "a same-concept alias resolution must record the alias it establishes"
            )
    return tuple(sorted(resolutions, key=lambda item: item.component_id))


def execute_reconciliation(
    registry: TerminologyRegistry,
    graph: ConflictGraph,
    evidence: dict[str, ComponentEvidence],
    reviewer: ReconciliationReviewer,
    *,
    staging: Path,
    created_at: datetime | None = None,
) -> ReconciliationRun:
    """Run every shard, retrying only failures and resuming validated shards."""
    shards = list(plan_reconciliation_shards(graph))
    staging.mkdir(parents=True, exist_ok=True)
    resolutions: list[ReconciliationResolution] = []
    calls = 0
    retries = 0
    completed: list[ReconciliationShard] = []
    for shard in shards:
        validated_path = staging / f"{shard.shard_id}.validated.json"
        if validated_path.is_file():
            stored = json.loads(validated_path.read_text(encoding="utf-8"))
            accepted = validate_shard_response(stored, shard, graph, registry)
            resolutions.extend(accepted)
            completed.append(shard.model_copy(update={"validated": True}))
            continue
        request = render_shard_request(graph, evidence, shard, registry)
        attempts = 0
        last_error: Exception | None = None
        while attempts < SHARD_MAX_ATTEMPTS:
            attempts += 1
            calls += 1
            if attempts > 1:
                retries += 1
            try:
                raw = reviewer.review(request)
                atomic_write_text(
                    staging / f"{shard.shard_id}.attempt-{attempts:02d}.json",
                    json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    overwrite=True,
                )
                accepted = validate_shard_response(raw, shard, graph, registry)
            except (TerminologyReconciliationError, ValueError) as exc:
                last_error = exc
                continue
            atomic_write_text(
                validated_path,
                json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                overwrite=True,
            )
            resolutions.extend(accepted)
            completed.append(shard.model_copy(update={"attempts": attempts, "validated": True}))
            break
        else:
            raise TerminologyReconciliationError(
                f"reconciliation shard {shard.shard_id} exhausted attempts: {last_error}"
            )

    counts = Counter(item.classification.value for item in resolutions)
    return ReconciliationRun(
        schema_version=RECONCILIATION_SCHEMA_VERSION,
        run_id=_stable_id(
            "tsr-run",
            {
                "graph_id": graph.graph_id,
                "reviewer": reviewer.provenance.reviewer,
                "component_ids": sorted(item.component_id for item in resolutions),
            },
        ),
        engine_version=RECONCILIATION_ENGINE_VERSION,
        graph_id=graph.graph_id,
        plan_id=graph.plan_id,
        registry_semantic_sha256=registry_identity(registry),
        reviewer=reviewer.provenance.reviewer,
        reviewer_kind=reviewer.provenance.reviewer_kind,
        external_service_contacted=reviewer.provenance.external_service_contacted,
        provenance=reviewer.provenance,
        shards=completed,
        shard_count=len(completed),
        provider_call_count=calls,
        retry_count=retries,
        resolutions=sorted(resolutions, key=lambda item: item.component_id),
        classification_counts=dict(sorted(counts.items())),
        escalation_count=sum(1 for item in resolutions if item.escalated),
        created_at=created_at or datetime.now(UTC),
    )


def semantic_operations(
    registry: TerminologyRegistry,
    graph: ConflictGraph,
    plan: TerminologyRemediationPlan,
    run: ReconciliationRun,
) -> list[TerminologyRemediationOperation]:
    """Convert component resolutions into per-finding remediation operations."""
    unresolved = {
        item.finding_id: item
        for item in plan.operations
        if item.resolution is TerminologyRemediationResolution.UNRESOLVED
    }
    resolution_by_component = {item.component_id: item for item in run.resolutions}
    operations: list[TerminologyRemediationOperation] = []
    for component in sorted(graph.components, key=lambda item: item.component_id):
        resolution = resolution_by_component.get(component.component_id)
        if resolution is None:
            raise TerminologyReconciliationError(
                f"component {component.component_id} has no semantic resolution"
            )
        findings = sorted(component.unresolved_finding_ids)
        for position, finding_id in enumerate(findings):
            original = unresolved[finding_id]
            # Component edits are applied exactly once, by the lowest finding in the family.
            carries_edits = position == 0 and not resolution.escalated
            edits = list(resolution.edits) if carries_edits else []
            operations.append(
                original.model_copy(
                    update={
                        "action": (
                            TerminologyRemediationAction.MANUAL_CONTEXT_REVIEW
                            if resolution.escalated
                            else TerminologyRemediationAction.APPLY_SEMANTIC_RESOLUTION
                        ),
                        "resolution": (
                            TerminologyRemediationResolution.UNRESOLVED
                            if resolution.escalated
                            else TerminologyRemediationResolution.DETERMINISTIC
                        ),
                        "mutates_registry": bool(edits),
                        "edits": edits,
                        "component_id": component.component_id,
                        "reason": resolution.reasoning_summary,
                        "blocked_reason": (
                            resolution.escalation_reason if resolution.escalated else None
                        ),
                        "blocking_evidence": (
                            ["semantic_review_insufficient_evidence"]
                            if resolution.escalated
                            else []
                        ),
                        "provenance": {
                            **original.provenance,
                            "reconciliation_run_id": run.run_id,
                            "classification": resolution.classification.value,
                            "reviewer": run.reviewer,
                            "confidence": resolution.confidence,
                        },
                    }
                )
            )
    return operations


def combined_remediation_plan(
    registry: TerminologyRegistry,
    plan: TerminologyRemediationPlan,
    graph: ConflictGraph,
    run: ReconciliationRun,
    *,
    created_at: datetime | None = None,
) -> TerminologyRemediationPlan:
    """Merge the deterministic operations with the validated semantic resolutions."""
    deterministic = [
        item
        for item in plan.operations
        if item.resolution is TerminologyRemediationResolution.DETERMINISTIC
    ]
    merged = [*deterministic, *semantic_operations(registry, graph, plan, run)]
    ordered = sorted(
        merged,
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
    return plan.model_copy(
        update={
            "plan_id": _stable_id(
                "trp",
                {
                    "engine_version": REMEDIATION_ENGINE_VERSION,
                    "base_plan_id": plan.plan_id,
                    "reconciliation_run_id": run.run_id,
                    "operation_ids": [item.operation_id for item in sequenced],
                },
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


def check_protected_lookups(
    before: TerminologyRegistry, after: TerminologyRegistry, glossary: Any
) -> list[ProtectedLookupCheck]:
    """Report both whether a protected lookup moved and whether it is now correct."""
    before_view = canonical_view(before, glossary)
    after_view = canonical_view(after, glossary)
    checks: list[ProtectedLookupCheck] = []
    for source, expected in PROTECTED_EXPECTATIONS:
        before_term = canonical_lookup(before_view, source)
        after_term = canonical_lookup(after_view, source)
        before_target = before_term.target_term if before_term else None
        after_target = after_term.target_term if after_term else None
        correct = after_target is not None and canonical_targets_equal(
            after_target, expected, after.target_language
        )
        checks.append(
            ProtectedLookupCheck(
                source_form=source,
                expected_target=expected,
                before_target=before_target,
                after_target=after_target,
                lookup_changed=before_target != after_target,
                lookup_correct=correct,
                owner_source_term_before=before_term.source_term if before_term else None,
                owner_source_term_after=after_term.source_term if after_term else None,
                detail=(
                    "authoritative target resolved"
                    if correct
                    else f"expected {expected!r} but resolved {after_target!r}"
                ),
            )
        )
    return checks


def resolution_set_identity(run: ReconciliationRun) -> str:
    """Hash only the semantic decisions, so provenance edits never change this identity."""
    payload = [
        item.model_dump(mode="json", exclude_none=False)
        for item in sorted(run.resolutions, key=lambda value: value.component_id)
    ]
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def correct_run_provenance(
    run: ReconciliationRun,
    corrected: ReviewerProvenance,
    *,
    reason: str,
    created_at: datetime | None = None,
) -> ReconciliationProvenanceCorrection:
    """Supersede an immutable run's provenance without touching its resolutions."""
    identity = resolution_set_identity(run)
    return ReconciliationProvenanceCorrection(
        schema_version=RECONCILIATION_SCHEMA_VERSION,
        correction_id=_stable_id(
            "tsrc",
            {
                "run_id": run.run_id,
                "resolution_set_sha256": identity,
                "corrected": corrected.model_dump(mode="json"),
            },
        ),
        supersedes_run_id=run.run_id,
        original_reviewer=run.reviewer,
        original_reviewer_kind=run.reviewer_kind,
        original_external_service_contacted=run.external_service_contacted,
        corrected_provenance=corrected,
        reason=reason,
        resolution_count=len(run.resolutions),
        resolution_set_sha256=identity,
        resolutions_unchanged=True,
        created_at=created_at or datetime.now(UTC),
    )


def serialize_provenance_correction(correction: ReconciliationProvenanceCorrection) -> str:
    return (
        json.dumps(
            correction.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def serialize_run(run: ReconciliationRun) -> str:
    return (
        json.dumps(
            run.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def serialize_graph(graph: ConflictGraph) -> str:
    return (
        json.dumps(
            graph.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def component_category(component: ConflictComponent) -> TerminologyRemediationCategory:
    """Best-effort category label for reporting a family's dominant conflict."""
    if TerminologyRemediationCategory.ALIAS_MISCLASSIFICATION.value in {
        item.value for item in component.relations
    }:
        return TerminologyRemediationCategory.ALIAS_MISCLASSIFICATION
    return TerminologyRemediationCategory.CONFLICTING_TARGET
