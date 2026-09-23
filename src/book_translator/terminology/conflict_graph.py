"""Deterministic terminology conflict graph over unresolved remediation operations."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any

from book_translator.models import TerminologyRegistry
from book_translator.models.terminology_reconciliation import (
    ConflictComponent,
    ConflictEdge,
    ConflictGraph,
    ConflictNode,
    ConflictNodeKind,
    ConflictRelation,
)
from book_translator.models.terminology_remediation import (
    TerminologyRemediationPlan,
    TerminologyRemediationResolution,
)
from book_translator.terminology.registry import registry_identity

GRAPH_SCHEMA_VERSION = "1.0"


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        + b"\n"
    )


def _stable_id(prefix: str, payload: Any) -> str:
    return f"{prefix}-{hashlib.sha256(_canonical_bytes(payload)).hexdigest()[:24]}"


class _Union:
    """Union-find with deterministic behaviour independent of insertion order."""

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
            # Attach the lexicographically larger root so grouping never depends on call order.
            low, high = sorted((left_root, right_root))
            self._parent[high] = low

    def groups(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for item in sorted(self._parent):
            grouped[self.find(item)].append(item)
        return grouped


def _node_id(candidate_id: str) -> str:
    return f"n:{candidate_id}"


def build_conflict_graph(
    registry: TerminologyRegistry, plan: TerminologyRemediationPlan
) -> ConflictGraph:
    """Group every unresolved remediation operation into a terminology family."""
    candidates = {item.candidate_id: item for item in registry.candidates}
    decisions_by_candidate = {item.candidate_id: item for item in registry.decisions}
    decision_by_id = {item.decision_id: item for item in registry.decisions}
    source_to_candidate = {item.source_term: item.candidate_id for item in registry.candidates}
    target_to_candidates: dict[str, list[str]] = defaultdict(list)
    for decision in registry.decisions:
        target_to_candidates[decision.target_term].append(decision.candidate_id)

    unresolved = [
        item
        for item in plan.operations
        if item.resolution is TerminologyRemediationResolution.UNRESOLVED
    ]
    seed_candidates: set[str] = set()
    for operation in unresolved:
        seed_candidates.add(operation.candidate_id)
        for decision_id in operation.affected_decision_ids:
            seeded = decision_by_id.get(decision_id)
            if seeded is not None:
                seed_candidates.add(seeded.candidate_id)

    union = _Union()
    edges: list[ConflictEdge] = []
    for operation in unresolved:
        union.add(_node_id(operation.candidate_id))
        for decision_id in sorted(set(operation.affected_decision_ids)):
            related = decision_by_id.get(decision_id)
            if related is None or related.candidate_id == operation.candidate_id:
                continue
            union.union(_node_id(operation.candidate_id), _node_id(related.candidate_id))
            relation = (
                ConflictRelation.EXACT_CANONICAL_CONFLICT
                if operation.remediation_category.value == "alias_misclassification"
                else ConflictRelation.CANONICAL_SUBTERM
            )
            edges.append(
                ConflictEdge(
                    relation=relation,
                    source_node_id=_node_id(operation.candidate_id),
                    target_node_id=_node_id(related.candidate_id),
                    detail=f"audit finding {operation.finding_id}",
                )
            )

    # Alias, competing-variant, and shared-target relations pull in the rest of each family.
    for candidate in sorted(registry.candidates, key=lambda item: item.candidate_id):
        for alias in sorted(candidate.aliases):
            other = source_to_candidate.get(alias)
            if other is None or other == candidate.candidate_id:
                continue
            if candidate.candidate_id not in seed_candidates and other not in seed_candidates:
                continue
            union.union(_node_id(candidate.candidate_id), _node_id(other))
            edges.append(
                ConflictEdge(
                    relation=ConflictRelation.ALIAS_RELATIONSHIP,
                    source_node_id=_node_id(candidate.candidate_id),
                    target_node_id=_node_id(other),
                    detail=f"{candidate.source_term!r} lists alias {alias!r}",
                )
            )
    for decision in sorted(registry.decisions, key=lambda item: item.decision_id):
        for variant in sorted(decision.competing_variants):
            for other in sorted(target_to_candidates.get(variant, [])):
                if other == decision.candidate_id:
                    continue
                if decision.candidate_id not in seed_candidates and other not in seed_candidates:
                    continue
                union.union(_node_id(decision.candidate_id), _node_id(other))
                edges.append(
                    ConflictEdge(
                        relation=ConflictRelation.COMPETING_VARIANT,
                        source_node_id=_node_id(decision.candidate_id),
                        target_node_id=_node_id(other),
                        detail=f"variant {variant!r} is another decision's canonical target",
                    )
                )

    grouped = union.groups()
    member_candidates = {
        node_id.removeprefix("n:") for members in grouped.values() for node_id in members
    }
    nodes = [
        ConflictNode(
            node_id=_node_id(candidate_id),
            kind=(
                ConflictNodeKind.DECISION
                if candidate_id in decisions_by_candidate
                else ConflictNodeKind.CANDIDATE_WITHOUT_DECISION
            ),
            candidate_id=candidate_id,
            decision_id=(
                decisions_by_candidate[candidate_id].decision_id
                if candidate_id in decisions_by_candidate
                else None
            ),
            source_term=candidates[candidate_id].source_term,
            target_term=(
                decisions_by_candidate[candidate_id].target_term
                if candidate_id in decisions_by_candidate
                else None
            ),
            aliases=list(candidates[candidate_id].aliases),
            competing_variants=(
                list(decisions_by_candidate[candidate_id].competing_variants)
                if candidate_id in decisions_by_candidate
                else []
            ),
            decision_source=(
                decisions_by_candidate[candidate_id].decision_source.value
                if candidate_id in decisions_by_candidate
                else None
            ),
            locked=(
                decisions_by_candidate[candidate_id].locked
                if candidate_id in decisions_by_candidate
                else False
            ),
            candidate_status=candidates[candidate_id].status.value,
        )
        for candidate_id in sorted(member_candidates)
    ]
    node_by_id = {item.node_id: item for item in nodes}
    operations_by_candidate: dict[str, list[Any]] = defaultdict(list)
    for operation in unresolved:
        operations_by_candidate[operation.candidate_id].append(operation)

    relations_by_root: dict[str, set[ConflictRelation]] = defaultdict(set)
    for edge in edges:
        relations_by_root[union.find(edge.source_node_id)].add(edge.relation)

    components: list[ConflictComponent] = []
    for root in sorted(grouped):
        members = sorted(grouped[root])
        candidate_ids = [item.removeprefix("n:") for item in members]
        component_operations = [
            operation
            for candidate_id in candidate_ids
            for operation in operations_by_candidate.get(candidate_id, [])
        ]
        source_terms = sorted(node_by_id[item].source_term for item in members)
        components.append(
            ConflictComponent(
                component_id=_stable_id(
                    "tcc",
                    {
                        "registry_identity": registry_identity(registry),
                        "candidate_ids": sorted(candidate_ids),
                    },
                ),
                node_ids=members,
                decision_ids=sorted(
                    value
                    for value in (node_by_id[item].decision_id for item in members)
                    if value is not None
                ),
                candidate_ids=sorted(candidate_ids),
                source_terms=source_terms,
                relations=sorted(relations_by_root.get(root, set()), key=lambda item: item.value),
                unresolved_operation_ids=sorted(item.operation_id for item in component_operations),
                unresolved_finding_ids=sorted(item.finding_id for item in component_operations),
                size=len(members),
            )
        )

    histogram = Counter(str(item.size) for item in components)
    covered = sum(len(item.unresolved_operation_ids) for item in components)
    return ConflictGraph(
        schema_version=GRAPH_SCHEMA_VERSION,
        graph_id=_stable_id(
            "tcg",
            {
                "plan_id": plan.plan_id,
                "registry_identity": registry_identity(registry),
                "component_ids": sorted(item.component_id for item in components),
            },
        ),
        registry_semantic_sha256=registry_identity(registry),
        plan_id=plan.plan_id,
        nodes=nodes,
        edges=sorted(
            edges,
            key=lambda item: (item.relation.value, item.source_node_id, item.target_node_id),
        ),
        components=sorted(components, key=lambda item: (-item.size, item.component_id)),
        unresolved_operation_count=len(unresolved),
        covered_operation_count=covered,
        component_count=len(components),
        component_size_histogram=dict(sorted(histogram.items(), key=lambda item: int(item[0]))),
    )


def detect_alias_cycles(registry: TerminologyRegistry) -> list[list[str]]:
    """Return alias cycles: source forms that mutually claim each other as aliases."""
    source_to_candidate = {item.source_term: item.candidate_id for item in registry.candidates}
    candidates = {item.candidate_id: item for item in registry.candidates}
    decided = {item.candidate_id for item in registry.decisions}
    graph: dict[str, set[str]] = defaultdict(set)
    for candidate in registry.candidates:
        if candidate.candidate_id not in decided:
            continue
        for alias in candidate.aliases:
            other = source_to_candidate.get(alias)
            if other is not None and other != candidate.candidate_id and other in decided:
                graph[candidate.candidate_id].add(other)

    cycles: set[tuple[str, ...]] = set()
    for start in sorted(graph):
        stack = [(start, [start])]
        while stack:
            node, path = stack.pop()
            for neighbour in sorted(graph.get(node, ())):
                if neighbour == start and len(path) >= 2:
                    rotated = _canonical_cycle(path)
                    cycles.add(rotated)
                elif neighbour not in path and len(path) < 6:
                    stack.append((neighbour, [*path, neighbour]))
    return [[candidates[item].source_term for item in cycle] for cycle in sorted(cycles)]


def _canonical_cycle(path: list[str]) -> tuple[str, ...]:
    smallest = min(range(len(path)), key=lambda index: path[index])
    return tuple(path[smallest:] + path[:smallest])
