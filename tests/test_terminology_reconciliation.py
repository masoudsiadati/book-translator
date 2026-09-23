"""Tests for the targeted semantic terminology reconciliation workflow."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.models.terminology_reconciliation import (
    DisclosedMaterial,
    InferenceLocality,
    ReconciliationClassification,
    ReconciliationRun,
    ReviewerProvenance,
)
from book_translator.models.terminology_remediation import (
    TerminologyRemediationAction,
    TerminologyRemediationPlan,
    TerminologyRemediationResolution,
)
from book_translator.terminology.canonical import canonical_view
from book_translator.terminology.conflict_evidence import CorpusIndex, collect_component_evidence
from book_translator.terminology.conflict_graph import build_conflict_graph, detect_alias_cycles
from book_translator.terminology.reconciliation import (
    FileReconciliationReviewer,
    TerminologyReconciliationError,
    check_protected_lookups,
    combined_remediation_plan,
    correct_run_provenance,
    execute_reconciliation,
    plan_reconciliation_shards,
    render_shard_request,
    resolution_set_identity,
    validate_shard_response,
)
from book_translator.terminology.registry import (
    candidate_identity,
    canonical_registry_bytes,
    decision_identity,
    load_registry,
    registry_identity,
    save_registry,
)
from book_translator.terminology.remediation import audit_registry_remediation
from book_translator.terminology.remediation_engine import (
    apply_remediation_plan,
    canonical_lookup,
    plan_registry_remediation,
    simulate_remediation_plan,
)

EMPTY_GLOSSARY: dict[str, object] = {
    "terms": {},
    "characters": {},
    "places": {},
    "groups": {},
    "unresolved_terms": [],
}
FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)


TEST_PROVENANCE = ReviewerProvenance(
    reviewer="test-agent",
    reviewer_kind="deterministic_fake",
    inference_locality=InferenceLocality.DETERMINISTIC_NONE,
    inference_provider=None,
    account_type=None,
    local_orchestration=True,
    external_service_contacted=False,
    disclosed_material=[DisclosedMaterial.TERMINOLOGY_NAMES],
    withheld_material=[DisclosedMaterial.CHUNK_EXCERPTS],
    prior_model_knowledge_used=False,
    disclosure_note="synthetic fixture responses authored offline by the test itself",
)


def _candidate(
    source: str,
    *,
    aliases: tuple[str, ...] = (),
    status: CandidateStatus = CandidateStatus.APPROVED,
) -> TerminologyCandidate:
    return TerminologyCandidate(
        candidate_id=candidate_identity(source, "en"),
        source_term=source,
        classification=TerminologyType.PERSON,
        confidence=1.0,
        status=status,
        provenance=DecisionSource.AI,
        aliases=list(aliases),
    )


def _decision(
    source: str,
    target: str,
    *,
    variants: tuple[str, ...] = (),
    decision_source: DecisionSource = DecisionSource.AI,
    locked: bool = False,
) -> TerminologyDecision:
    candidate_id = candidate_identity(source, "en")
    return TerminologyDecision(
        decision_id=decision_identity(candidate_id, "fa"),
        candidate_id=candidate_id,
        target_term=target,
        decision_source=decision_source,
        locked=locked,
        competing_variants=list(variants),
    )


def _registry() -> TerminologyRegistry:
    """A synthetic registry reproducing the real conflict shapes."""
    candidates = [
        # a full name shadowed by the given name's alias list (the Rand baseline defect)
        _candidate("Rand", aliases=("Rand al'Thor", "Lord Dragon")),
        _candidate("Rand al'Thor", status=CandidateStatus.CANDIDATE),
        _candidate("Lord Dragon"),
        # an impossible cross-character alias
        _candidate("Moiraine", aliases=("Egwene al'Vere",)),
        _candidate("Egwene al'Vere"),
        # a reciprocal alias cycle
        _candidate("Asmodean", aliases=("Natael",)),
        _candidate("Natael", aliases=("Asmodean",)),
        # two distinct concepts that happen to share a Persian target
        _candidate("Red Eagle"),
        _candidate("Crimson Eagle"),
        # a locked user decision
        _candidate("Ajah"),
    ]
    decisions = [
        _decision("Rand", "رند", variants=("رند ال ثور",)),
        _decision("Lord Dragon", "لرد اژدها"),
        _decision("Moiraine", "مویرین", variants=("اگوین ال‌ویر",)),
        _decision("Egwene al'Vere", "اگوین ال‌ویر"),
        _decision("Asmodean", "آسمودیان", variants=("ناتائل",)),
        _decision("Natael", "ناتائل", variants=("آسمودیان",)),
        _decision("Red Eagle", "عقاب سرخ"),
        _decision("Crimson Eagle", "عقاب سرخ"),
        _decision("Ajah", "آجای", decision_source=DecisionSource.USER, locked=True),
    ]
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-reconciliation-book",
        source_language="en",
        target_language="fa",
        candidates=candidates,
        decisions=decisions,
    )
    return TerminologyRegistry.model_validate_json(canonical_registry_bytes(registry))


def _plan(registry: TerminologyRegistry) -> TerminologyRemediationPlan:
    report = audit_registry_remediation(registry, EMPTY_GLOSSARY)
    return plan_registry_remediation(
        registry,
        EMPTY_GLOSSARY,
        report,
        registry_raw_sha256="a" * 64,
        glossary_sha256="b" * 64,
        created_at=FIXED_TIME,
    )


def _graph(registry: TerminologyRegistry) -> Any:
    return build_conflict_graph(registry, _plan(registry))


def _component_for(graph: Any, source: str) -> Any:
    node_by_id = {item.node_id: item for item in graph.nodes}
    for component in graph.components:
        if any(node_by_id[node].source_term == source for node in component.node_ids):
            return component
    raise AssertionError(f"no component contains {source!r}")


def _resolution(
    component: Any,
    classification: str,
    edits: list[dict[str, Any]],
    *,
    escalated: bool = False,
) -> dict[str, Any]:
    return {
        "component_id": component.component_id,
        "classification": classification,
        "affected_decision_ids": component.decision_ids,
        "affected_candidate_ids": component.candidate_ids,
        "canonical_source_forms": component.source_terms,
        "edits": edits,
        "confidence": 0.9,
        "evidence_references": [] if escalated else ["synthetic corpus evidence"],
        "reasoning_summary": "synthetic reconciliation decision",
        "escalated": escalated,
        "escalation_reason": "ambiguous" if escalated else None,
    }


def _edit(registry: TerminologyRegistry, kind: str, source: str, value: str) -> dict[str, Any]:
    candidate_id = candidate_identity(source, "en")
    decision = next(
        (item for item in registry.decisions if item.candidate_id == candidate_id), None
    )
    return {
        "kind": kind,
        "candidate_id": candidate_id,
        "source_term": source,
        "decision_id": decision.decision_id if decision else None,
        "value": value,
        "reason": "synthetic reconciliation edit",
    }


def test_conflict_graph_covers_every_unresolved_operation_exactly_once() -> None:
    registry = _registry()
    plan = _plan(registry)
    graph = build_conflict_graph(registry, plan)

    unresolved = [
        item
        for item in plan.operations
        if item.resolution is TerminologyRemediationResolution.UNRESOLVED
    ]
    covered = [
        value for component in graph.components for value in component.unresolved_operation_ids
    ]

    assert graph.unresolved_operation_count == len(unresolved)
    assert sorted(covered) == sorted(item.operation_id for item in unresolved)
    assert len(covered) == len(set(covered))


def test_component_identities_are_deterministic() -> None:
    registry = _registry()
    first = build_conflict_graph(registry, _plan(registry))
    second = build_conflict_graph(registry, _plan(registry))

    assert first.graph_id == second.graph_id
    assert [item.component_id for item in first.components] == [
        item.component_id for item in second.components
    ]


def test_related_records_share_one_component() -> None:
    graph = _graph(_registry())
    component = _component_for(graph, "Rand")

    assert "Rand al'Thor" in component.source_terms
    assert "Lord Dragon" in component.source_terms


def test_alias_cycles_are_detected() -> None:
    cycles = detect_alias_cycles(_registry())

    assert ["Asmodean", "Natael"] in cycles


def test_distinct_concepts_sharing_a_target_are_not_merged() -> None:
    registry = _registry()
    graph = _graph(registry)
    eagles = [
        component
        for component in graph.components
        if "Red Eagle" in component.source_terms or "Crimson Eagle" in component.source_terms
    ]

    # A shared Persian target alone must never make two source forms one family.
    assert all(
        "Crimson Eagle" not in item.source_terms
        for item in eagles
        if "Red Eagle" in item.source_terms
    )


def test_equal_priority_ai_conflicts_reach_semantic_review() -> None:
    registry = _registry()
    plan = _plan(registry)
    graph = build_conflict_graph(registry, plan)

    assert graph.component_count > 0
    assert all(
        item.provenance["flagged_decision_locked"] is False
        for item in plan.operations
        if item.resolution is TerminologyRemediationResolution.UNRESOLVED
    )


def test_shard_plan_covers_components_without_splitting_one() -> None:
    graph = _graph(_registry())
    shards = plan_reconciliation_shards(graph)
    covered = [value for shard in shards for value in shard.component_ids]

    assert sorted(covered) == sorted(item.component_id for item in graph.components)
    assert len(covered) == len(set(covered))


def test_shard_request_carries_whole_families() -> None:
    registry = _registry()
    graph = _graph(registry)
    corpus = CorpusIndex(())
    evidence = {
        item.component_id: collect_component_evidence(registry, graph, item, corpus, EMPTY_GLOSSARY)
        for item in graph.components
    }
    shard = plan_reconciliation_shards(graph)[0]
    request = render_shard_request(graph, evidence, shard, registry)

    assert shard.component_ids[0] in request.user_message
    assert len(request.request_sha256) == 64


def test_response_missing_a_component_is_rejected() -> None:
    registry = _registry()
    graph = _graph(registry)
    shard = plan_reconciliation_shards(graph)[0]

    with pytest.raises(TerminologyReconciliationError, match="exactly one resolution"):
        validate_shard_response({"resolutions": []}, shard, graph, registry)


def test_edit_escaping_its_component_is_rejected() -> None:
    registry = _registry()
    graph = _graph(registry)
    moiraine = _component_for(graph, "Moiraine")
    rand = _component_for(graph, "Rand")
    assert moiraine.component_id != rand.component_id
    shard = plan_reconciliation_shards(graph)[0].model_copy(
        update={"component_ids": [moiraine.component_id]}
    )
    payload = {
        "resolutions": [
            _resolution(
                moiraine,
                "invalid_relationship_only",
                [_edit(registry, "remove_alias", "Rand", "Lord Dragon")],
            )
        ]
    }

    with pytest.raises(TerminologyReconciliationError, match="escapes component"):
        validate_shard_response(payload, shard, graph, registry)


def test_locked_user_decision_edit_is_rejected() -> None:
    registry = _registry()
    graph = _graph(registry)
    component = graph.components[0]
    payload = {
        "resolutions": [
            _resolution(
                component,
                "wrong_target",
                [
                    {
                        "kind": "set_target",
                        "candidate_id": candidate_identity("Ajah", "en"),
                        "source_term": "Ajah",
                        "decision_id": None,
                        "value": "چیز دیگر",
                        "reason": "should be refused",
                    }
                ],
            )
        ]
    }
    shard = plan_reconciliation_shards(graph)[0].model_copy(
        update={"component_ids": [component.component_id]}
    )

    with pytest.raises(TerminologyReconciliationError):
        validate_shard_response(payload, shard, graph, registry)


def _authored_responses(registry: TerminologyRegistry, graph: Any, directory: Path) -> None:
    """Author a full, valid response set for the synthetic registry."""
    plans: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    rand = _component_for(graph, "Rand")
    plans[rand.component_id] = (
        "same_concept_alias",
        [
            _edit(registry, "create_decision", "Rand al'Thor", "رند ال ثور"),
            _edit(registry, "add_alias", "Rand al'Thor", "al'Thor"),
            _edit(registry, "remove_alias", "Rand", "Rand al'Thor"),
            _edit(registry, "remove_alias", "Rand", "Lord Dragon"),
            _edit(registry, "remove_competing_variant", "Rand", "رند ال ثور"),
        ],
    )
    moiraine = _component_for(graph, "Moiraine")
    plans[moiraine.component_id] = (
        "distinct_concepts",
        [
            _edit(registry, "remove_alias", "Moiraine", "Egwene al'Vere"),
            _edit(registry, "remove_competing_variant", "Moiraine", "اگوین ال‌ویر"),
        ],
    )
    asmodean = _component_for(graph, "Asmodean")
    plans[asmodean.component_id] = (
        "invalid_relationship_only",
        [
            _edit(registry, "remove_alias", "Asmodean", "Natael"),
            _edit(registry, "remove_alias", "Natael", "Asmodean"),
            _edit(registry, "remove_competing_variant", "Asmodean", "ناتائل"),
            _edit(registry, "remove_competing_variant", "Natael", "آسمودیان"),
        ],
    )
    directory.mkdir(parents=True, exist_ok=True)
    for shard in plan_reconciliation_shards(graph):
        resolutions = []
        for component_id in shard.component_ids:
            component = next(item for item in graph.components if item.component_id == component_id)
            classification, edits = plans.get(component_id, ("invalid_relationship_only", []))
            resolutions.append(_resolution(component, classification, edits))
        (directory / f"{shard.shard_id}.response.json").write_text(
            json.dumps({"schema_version": "1.0", "resolutions": resolutions}, ensure_ascii=False),
            encoding="utf-8",
        )


def test_reconciliation_run_resolves_every_component(tmp_path: Path) -> None:
    registry = _registry()
    graph = _graph(registry)
    _authored_responses(registry, graph, tmp_path / "responses")
    reviewer = FileReconciliationReviewer(tmp_path / "responses", provenance=TEST_PROVENANCE)

    run = execute_reconciliation(
        registry,
        graph,
        {
            item.component_id: collect_component_evidence(
                registry, graph, item, CorpusIndex(()), EMPTY_GLOSSARY
            )
            for item in graph.components
        },
        reviewer,
        staging=tmp_path / "staging",
        created_at=FIXED_TIME,
    )

    assert len(run.resolutions) == graph.component_count
    assert run.escalation_count == 0
    assert run.external_service_contacted is False


def test_reconciliation_resumes_validated_shards(tmp_path: Path) -> None:
    registry = _registry()
    graph = _graph(registry)
    _authored_responses(registry, graph, tmp_path / "responses")
    evidence = {
        item.component_id: collect_component_evidence(
            registry, graph, item, CorpusIndex(()), EMPTY_GLOSSARY
        )
        for item in graph.components
    }
    reviewer = FileReconciliationReviewer(tmp_path / "responses", provenance=TEST_PROVENANCE)
    first = execute_reconciliation(
        registry,
        graph,
        evidence,
        reviewer,
        staging=tmp_path / "staging",
        created_at=FIXED_TIME,
    )
    # Remove the authored responses; a resumed run must reuse the validated shard results.
    for item in (tmp_path / "responses").glob("*.response.json"):
        item.unlink()
    second = execute_reconciliation(
        registry,
        graph,
        evidence,
        reviewer,
        staging=tmp_path / "staging",
        created_at=FIXED_TIME,
    )

    assert second.run_id == first.run_id
    assert second.provider_call_count == 0


def test_reconciliation_retries_only_the_failing_shard(tmp_path: Path) -> None:
    registry = _registry()
    graph = _graph(registry)
    responses = tmp_path / "responses"
    _authored_responses(registry, graph, responses)
    shards = plan_reconciliation_shards(graph)
    broken = responses / f"{shards[0].shard_id}.response.json"
    broken.write_text(json.dumps({"resolutions": []}), encoding="utf-8")
    reviewer = FileReconciliationReviewer(responses, provenance=TEST_PROVENANCE)

    with pytest.raises(TerminologyReconciliationError, match="exhausted attempts"):
        execute_reconciliation(
            registry,
            graph,
            {
                item.component_id: collect_component_evidence(
                    registry, graph, item, CorpusIndex(()), EMPTY_GLOSSARY
                )
                for item in graph.components
            },
            reviewer,
            staging=tmp_path / "staging",
            created_at=FIXED_TIME,
        )
    attempts = sorted((tmp_path / "staging").glob(f"{shards[0].shard_id}.attempt-*.json"))

    assert len(attempts) == 3


def _combined(tmp_path: Path) -> tuple[TerminologyRegistry, TerminologyRemediationPlan, Any]:
    registry = _registry()
    plan = _plan(registry)
    graph = build_conflict_graph(registry, plan)
    _authored_responses(registry, graph, tmp_path / "responses")
    run = execute_reconciliation(
        registry,
        graph,
        {
            item.component_id: collect_component_evidence(
                registry, graph, item, CorpusIndex(()), EMPTY_GLOSSARY
            )
            for item in graph.components
        },
        FileReconciliationReviewer(tmp_path / "responses", provenance=TEST_PROVENANCE),
        staging=tmp_path / "staging",
        created_at=FIXED_TIME,
    )
    return (
        registry,
        combined_remediation_plan(registry, plan, graph, run, created_at=FIXED_TIME),
        run,
    )


def test_combined_plan_has_no_unresolved_operations(tmp_path: Path) -> None:
    _registry_value, combined, _run = _combined(tmp_path)

    assert combined.unresolved_operation_count == 0
    assert combined.applicable is True
    assert TerminologyRemediationAction.APPLY_SEMANTIC_RESOLUTION.value in combined.action_counts


def test_combined_plan_fixes_the_full_name_lookup(tmp_path: Path) -> None:
    registry, combined, _run = _combined(tmp_path)
    after = apply_remediation_plan(registry, combined)
    before_view = canonical_view(registry, EMPTY_GLOSSARY)
    after_view = canonical_view(after, EMPTY_GLOSSARY)

    before = canonical_lookup(before_view, "Rand al'Thor")
    resolved = canonical_lookup(after_view, "Rand al'Thor")

    assert before is not None and before.target_term == "رند"
    assert resolved is not None and resolved.target_term == "رند ال ثور"
    assert canonical_lookup(after_view, "Rand") is not None
    assert canonical_lookup(after_view, "Rand").target_term == "رند"  # type: ignore[union-attr]


def test_combined_plan_rejects_the_cross_character_alias(tmp_path: Path) -> None:
    registry, combined, _run = _combined(tmp_path)
    after = apply_remediation_plan(registry, combined)
    view = canonical_view(after, EMPTY_GLOSSARY)
    resolved = canonical_lookup(view, "Egwene al'Vere")

    assert resolved is not None
    assert resolved.target_term == "اگوین ال‌ویر"
    assert resolved.source_term == "Egwene al'Vere"


def test_combined_plan_breaks_the_alias_cycle(tmp_path: Path) -> None:
    registry, combined, _run = _combined(tmp_path)
    after = apply_remediation_plan(registry, combined)

    assert detect_alias_cycles(after) == []


def test_combined_plan_preserves_the_locked_decision(tmp_path: Path) -> None:
    registry, combined, _run = _combined(tmp_path)
    after = apply_remediation_plan(registry, combined)
    before = next(
        item for item in registry.decisions if item.candidate_id == candidate_identity("Ajah", "en")
    )
    resolved = next(
        item for item in after.decisions if item.candidate_id == candidate_identity("Ajah", "en")
    )

    assert resolved == before
    assert resolved.locked is True
    assert resolved.target_term == "آجای"


def test_protected_lookup_reports_changed_and_correct(tmp_path: Path) -> None:
    registry, combined, _run = _combined(tmp_path)
    after = apply_remediation_plan(registry, combined)
    checks = {
        item.source_form: item for item in check_protected_lookups(registry, after, EMPTY_GLOSSARY)
    }

    assert checks["Rand al'Thor"].lookup_changed is True
    assert checks["Rand al'Thor"].lookup_correct is True
    assert checks["Ajah"].lookup_changed is False
    assert checks["Ajah"].lookup_correct is True


def test_composed_target_is_applied(tmp_path: Path) -> None:
    registry = _registry()
    plan = _plan(registry)
    graph = build_conflict_graph(registry, plan)
    component = _component_for(graph, "Rand")
    edits = [_edit(registry, "set_target", "Lord Dragon", "لرد اژدهای بازپدید")]
    payload = {
        "resolutions": [_resolution(component, "wrong_target", edits)],
    }
    shard = plan_reconciliation_shards(graph)[0].model_copy(
        update={"component_ids": [component.component_id]}
    )
    accepted = validate_shard_response(payload, shard, graph, registry)

    assert accepted[0].edits[0].value == "لرد اژدهای بازپدید"


def test_escalated_component_keeps_the_plan_unapplicable(tmp_path: Path) -> None:
    registry = _registry()
    plan = _plan(registry)
    graph = build_conflict_graph(registry, plan)
    responses = tmp_path / "responses"
    responses.mkdir(parents=True)
    for shard in plan_reconciliation_shards(graph):
        resolutions = [
            _resolution(
                next(item for item in graph.components if item.component_id == component_id),
                "insufficient_evidence",
                [],
                escalated=True,
            )
            for component_id in shard.component_ids
        ]
        (responses / f"{shard.shard_id}.response.json").write_text(
            json.dumps({"schema_version": "1.0", "resolutions": resolutions}, ensure_ascii=False),
            encoding="utf-8",
        )
    run = execute_reconciliation(
        registry,
        graph,
        {
            item.component_id: collect_component_evidence(
                registry, graph, item, CorpusIndex(()), EMPTY_GLOSSARY
            )
            for item in graph.components
        },
        FileReconciliationReviewer(responses, provenance=TEST_PROVENANCE),
        staging=tmp_path / "staging",
        created_at=FIXED_TIME,
    )
    combined = combined_remediation_plan(registry, plan, graph, run, created_at=FIXED_TIME)

    assert run.escalation_count == graph.component_count
    assert combined.unresolved_operation_count > 0
    assert combined.applicable is False


def test_combined_dry_run_leaves_the_registry_byte_identical(tmp_path: Path) -> None:
    registry_path = tmp_path / "terminology.yaml"
    save_registry(registry_path, _registry())
    before = registry_path.read_bytes()
    registry = load_registry(registry_path)
    plan = _plan(registry)
    graph = build_conflict_graph(registry, plan)
    _authored_responses(registry, graph, tmp_path / "responses")
    run = execute_reconciliation(
        registry,
        graph,
        {
            item.component_id: collect_component_evidence(
                registry, graph, item, CorpusIndex(()), EMPTY_GLOSSARY
            )
            for item in graph.components
        },
        FileReconciliationReviewer(tmp_path / "responses", provenance=TEST_PROVENANCE),
        staging=tmp_path / "staging",
        created_at=FIXED_TIME,
    )
    combined = combined_remediation_plan(
        registry, plan, graph, run, created_at=FIXED_TIME
    ).model_copy(update={"source_registry_raw_sha256": hashlib.sha256(before).hexdigest()})
    simulation = simulate_remediation_plan(
        registry_path, EMPTY_GLOSSARY, combined, created_at=FIXED_TIME
    )

    assert simulation.real_registry_unchanged is True
    assert registry_path.read_bytes() == before
    assert simulation.skipped_unresolved_count == 0
    assert simulation.blocking_findings_after < simulation.blocking_findings_before


def test_combined_simulation_reduces_the_audit_findings(tmp_path: Path) -> None:
    registry, combined, _run = _combined(tmp_path)
    after = apply_remediation_plan(registry, combined)
    before_report = audit_registry_remediation(registry, EMPTY_GLOSSARY)
    after_report = audit_registry_remediation(after, EMPTY_GLOSSARY)

    assert after_report.remaining_registry_blockers < before_report.remaining_registry_blockers
    assert registry_identity(after) == combined.expected_semantic_sha256


def test_reconciliation_run_round_trips_through_its_contract(tmp_path: Path) -> None:
    _registry_value, _combined_plan, run = _combined(tmp_path)
    restored = ReconciliationRun.model_validate_json(run.model_dump_json())

    assert restored == run
    assert all(
        item.classification in set(ReconciliationClassification) for item in restored.resolutions
    )


def _hosted_provenance() -> ReviewerProvenance:
    return ReviewerProvenance(
        reviewer="claude-code",
        reviewer_kind="hosted_model_agent",
        inference_locality=InferenceLocality.REMOTE_HOSTED,
        inference_provider="anthropic",
        account_type="claude_max_consumer",
        local_orchestration=True,
        external_service_contacted=True,
        disclosed_material=[
            DisclosedMaterial.TERMINOLOGY_NAMES,
            DisclosedMaterial.TARGET_TRANSLATIONS,
            DisclosedMaterial.AGGREGATE_CORPUS_STATISTICS,
        ],
        withheld_material=[
            DisclosedMaterial.SENTENCE_EXCERPTS,
            DisclosedMaterial.CHUNK_EXCERPTS,
        ],
        prior_model_knowledge_used=True,
        disclosure_note="hosted inference reviewed names, targets, and aggregate statistics",
    )


def test_remote_inference_must_report_an_external_service() -> None:
    with pytest.raises(ValueError, match="external service"):
        ReviewerProvenance(
            reviewer="claude-code",
            reviewer_kind="hosted_model_agent",
            inference_locality=InferenceLocality.REMOTE_HOSTED,
            inference_provider="anthropic",
            account_type=None,
            local_orchestration=True,
            external_service_contacted=False,
            disclosure_note="contradictory provenance",
        )


def test_remote_inference_must_name_its_provider() -> None:
    with pytest.raises(ValueError, match="inference provider"):
        ReviewerProvenance(
            reviewer="claude-code",
            reviewer_kind="hosted_model_agent",
            inference_locality=InferenceLocality.REMOTE_HOSTED,
            inference_provider=None,
            account_type=None,
            local_orchestration=True,
            external_service_contacted=True,
            disclosure_note="missing provider",
        )


def test_local_process_reviewer_may_not_claim_an_external_service() -> None:
    with pytest.raises(ValueError, match="local process"):
        ReviewerProvenance(
            reviewer="offline",
            reviewer_kind="local_process_agent",
            inference_locality=InferenceLocality.LOCAL_PROCESS,
            inference_provider=None,
            account_type=None,
            local_orchestration=True,
            external_service_contacted=True,
            disclosure_note="contradictory provenance",
        )


def test_material_cannot_be_both_disclosed_and_withheld() -> None:
    with pytest.raises(ValueError, match="disclosed and withheld"):
        ReviewerProvenance(
            reviewer="claude-code",
            reviewer_kind="hosted_model_agent",
            inference_locality=InferenceLocality.REMOTE_HOSTED,
            inference_provider="anthropic",
            account_type=None,
            local_orchestration=True,
            external_service_contacted=True,
            disclosed_material=[DisclosedMaterial.CHUNK_EXCERPTS],
            withheld_material=[DisclosedMaterial.CHUNK_EXCERPTS],
            disclosure_note="contradictory disclosure",
        )


def test_run_records_the_reviewer_provenance(tmp_path: Path) -> None:
    _registry_value, _combined_plan, run = _combined(tmp_path)

    assert run.provenance is not None
    assert run.provenance.reviewer_kind == "deterministic_fake"
    assert run.provenance.external_service_contacted is False


def test_provenance_correction_supersedes_without_touching_resolutions(tmp_path: Path) -> None:
    _registry_value, _combined_plan, run = _combined(tmp_path)
    before = resolution_set_identity(run)

    correction = correct_run_provenance(
        run,
        _hosted_provenance(),
        reason="the run was reviewed by hosted inference, not a local process",
        created_at=FIXED_TIME,
    )

    assert correction.supersedes_run_id == run.run_id
    assert correction.original_reviewer_kind == "deterministic_fake"
    assert correction.corrected_provenance.external_service_contacted is True
    assert correction.corrected_provenance.inference_provider == "anthropic"
    assert correction.resolutions_unchanged is True
    assert correction.resolution_set_sha256 == before
    assert resolution_set_identity(run) == before


def test_provenance_correction_identity_is_deterministic(tmp_path: Path) -> None:
    _registry_value, _combined_plan, run = _combined(tmp_path)
    first = correct_run_provenance(
        run, _hosted_provenance(), reason="same reason", created_at=FIXED_TIME
    )
    second = correct_run_provenance(
        run, _hosted_provenance(), reason="same reason", created_at=FIXED_TIME
    )

    assert first.correction_id == second.correction_id


def test_a_correction_must_change_at_least_one_provenance_fact(tmp_path: Path) -> None:
    _registry_value, _combined_plan, run = _combined(tmp_path)

    with pytest.raises(ValueError, match="at least one provenance fact"):
        correct_run_provenance(run, TEST_PROVENANCE, reason="no-op", created_at=FIXED_TIME)


def test_resolution_identity_ignores_provenance_fields(tmp_path: Path) -> None:
    _registry_value, _combined_plan, run = _combined(tmp_path)
    relabelled = run.model_copy(
        update={
            "reviewer": "someone-else",
            "reviewer_kind": "hosted_model_agent",
            "external_service_contacted": True,
            "provenance": _hosted_provenance(),
        }
    )

    assert resolution_set_identity(relabelled) == resolution_set_identity(run)
