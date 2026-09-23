"""Contract, unit, and failure-injection tests for the terminology remediation engine."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyOccurrence,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.models.terminology_remediation import (
    TerminologyLookupChangeKind,
    TerminologyRemediationAction,
    TerminologyRemediationCategory,
    TerminologyRemediationOperation,
    TerminologyRemediationPlan,
    TerminologyRemediationResolution,
)
from book_translator.terminology.canonical import canonical_view
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
    REMEDIATION_ENGINE_VERSION,
    TerminologyRemediationPlanError,
    apply_remediation_plan,
    canonical_lookup,
    execute_remediation_plan,
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


def _candidate(
    source: str,
    *,
    aliases: tuple[str, ...] = (),
    occurrences: tuple[TerminologyOccurrence, ...] = (),
    classification: TerminologyType = TerminologyType.PERSON,
    notes: tuple[str, ...] = (),
) -> TerminologyCandidate:
    return TerminologyCandidate(
        candidate_id=candidate_identity(source, "en"),
        source_term=source,
        classification=classification,
        confidence=1.0,
        status=CandidateStatus.APPROVED,
        provenance=DecisionSource.AI,
        aliases=list(aliases),
        occurrences=list(occurrences),
        evidence_notes=list(notes),
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
    """A synthetic registry reproducing every audited remediation category."""
    candidates = [
        # valid compound / morphology: one component target survives inside the compound
        _candidate("Gareth Bryne"),
        _candidate("Lord Gareth"),
        _candidate("Lord Gareth Bryne"),
        # conflicting target: no component target survives
        _candidate("Temaile"),
        _candidate("Temaile Kinderode"),
        # alias misclassification: the rival decision claims this source as an alias, which
        # shadows the flagged decision's own entry in the canonical view
        _candidate("Min", aliases=("Serenla",)),
        _candidate("Serenla"),
        # invalid competing variant: a canonical target stored as another decision's variant
        _candidate("Dragon", classification=TerminologyType.TITLE),
        _candidate("Lord Dragon", classification=TerminologyType.TITLE),
        # a locked user decision that must never move
        _candidate("Ajah", classification=TerminologyType.ORGANIZATION),
        # normalized identity migration
        TerminologyCandidate(
            candidate_id=candidate_identity("Two Rivers' ", "en"),
            source_term="Two Rivers' ",
            classification=TerminologyType.PLACE,
            confidence=0.9,
            status=CandidateStatus.CANDIDATE,
            provenance=DecisionSource.AI,
            aliases=["Two Rivers"],
            occurrences=[TerminologyOccurrence(chapter_id="019", chunk_id="019-003", count=1)],
            evidence_notes=["historical whitespace-bearing evidence"],
        ),
        _candidate("Two Rivers'", classification=TerminologyType.PLACE),
    ]
    decisions = [
        _decision("Gareth Bryne", "گرث براین"),
        _decision("Lord Gareth", "لرد گرت"),
        _decision("Lord Gareth Bryne", "لرد گرث براین"),
        _decision("Temaile", "تمایلی"),
        _decision("Temaile Kinderode", "تمایل کیندرود"),
        _decision("Min", "مین"),
        _decision("Serenla", "سرنلا"),
        _decision("Dragon", "اژدها", variants=("لرد اژدها",)),
        _decision("Lord Dragon", "لرد اژدها"),
        _decision("Ajah", "آجای", decision_source=DecisionSource.USER, locked=True),
        _decision("Two Rivers'", "دو رود"),
    ]
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-remediation-book",
        source_language="en",
        target_language="fa",
        candidates=candidates,
        decisions=decisions,
    )
    # A persisted registry is always identity-ordered, and canonical-view tie-breaking depends
    # on that order, so the fixture must match what load_registry would return.
    return TerminologyRegistry.model_validate_json(canonical_registry_bytes(registry))


def _plan(
    registry: TerminologyRegistry | None = None, **overrides: object
) -> TerminologyRemediationPlan:
    registry = registry or _registry()
    report = audit_registry_remediation(registry, EMPTY_GLOSSARY)
    arguments: dict[str, object] = {
        "registry_raw_sha256": "a" * 64,
        "glossary_sha256": "b" * 64,
        "created_at": FIXED_TIME,
        **overrides,
    }
    return plan_registry_remediation(
        registry,
        EMPTY_GLOSSARY,
        report,
        **arguments,  # type: ignore[arg-type]
    )


def _operation(
    plan: TerminologyRemediationPlan, category: TerminologyRemediationCategory
) -> TerminologyRemediationOperation:
    return next(item for item in plan.operations if item.remediation_category is category)


def test_fixture_reproduces_every_audited_remediation_category() -> None:
    report = audit_registry_remediation(_registry(), EMPTY_GLOSSARY)

    assert report.remediation_category_counts == {
        "alias_misclassification": 1,
        "conflicting_target": 1,
        "exact_enforcement_upheld": 1,
        "exact_target_requires_review": 2,
        "invalid_competing_variant": 1,
        "valid_compound_or_morphology": 1,
    }
    assert report.exactness_finding_count == 3
    assert report.bookkeeping_mismatch is not None


def test_plan_is_deterministic_for_identical_registry_state() -> None:
    first = _plan()
    second = _plan()

    assert first.plan_id == second.plan_id
    assert [item.operation_id for item in first.operations] == [
        item.operation_id for item in second.operations
    ]
    assert first.expected_semantic_sha256 == second.expected_semantic_sha256


def test_plan_identity_changes_when_registry_state_changes() -> None:
    registry = _registry()
    mutated = registry.model_copy(
        update={
            "decisions": [
                item.model_copy(update={"target_term": "تمایل دیگر"})
                if item.candidate_id == candidate_identity("Temaile Kinderode", "en")
                else item
                for item in registry.decisions
            ]
        }
    )

    assert _plan(registry).plan_id != _plan(mutated).plan_id


def test_plan_orders_operations_contiguously_and_uniquely() -> None:
    plan = _plan()

    assert [item.sequence for item in plan.operations] == list(range(1, plan.operation_count + 1))
    assert len({item.finding_id for item in plan.operations}) == plan.operation_count
    assert plan.engine_version == REMEDIATION_ENGINE_VERSION


def test_every_affected_decision_has_exactly_one_primary_action() -> None:
    plan = _plan()
    report = audit_registry_remediation(_registry(), EMPTY_GLOSSARY)

    assert report.bookkeeping_mismatch is not None
    assert plan.operation_count == report.findings_total
    assert sorted(item.finding_id for item in plan.operations) == sorted(
        [*(item.finding_id for item in report.findings), report.bookkeeping_mismatch.finding_id]
    )


def test_valid_compound_case_is_a_no_op() -> None:
    plan = _plan()
    operation = _operation(plan, TerminologyRemediationCategory.VALID_COMPOUND_OR_MORPHOLOGY)

    assert operation.action is TerminologyRemediationAction.NO_CHANGE_VALIDATOR_FIX
    assert operation.resolution is TerminologyRemediationResolution.DETERMINISTIC
    assert operation.mutates_registry is False
    assert operation.source_term == "Lord Gareth Bryne"


def test_valid_compound_target_is_never_rewritten() -> None:
    registry = _registry()
    applied = apply_remediation_plan(registry, _plan(registry))
    decisions = {item.candidate_id: item for item in applied.decisions}

    assert decisions[candidate_identity("Lord Gareth Bryne", "en")].target_term == "لرد گرث براین"


def test_alias_misclassification_is_unresolved_because_lookup_would_change() -> None:
    plan = _plan()
    operation = _operation(plan, TerminologyRemediationCategory.ALIAS_MISCLASSIFICATION)

    assert operation.action is TerminologyRemediationAction.CONVERT_TO_ALIAS
    assert operation.resolution is TerminologyRemediationResolution.UNRESOLVED
    assert operation.mutates_registry is False
    assert "alias_conversion_changes_resolved_target" in operation.blocking_evidence
    assert "both_sides_share_equal_governance_priority" in operation.blocking_evidence


def test_alias_refusal_is_idempotent_across_replanning() -> None:
    registry = _registry()
    first = _operation(_plan(registry), TerminologyRemediationCategory.ALIAS_MISCLASSIFICATION)
    applied = apply_remediation_plan(registry, _plan(registry))
    second = _operation(_plan(applied), TerminologyRemediationCategory.ALIAS_MISCLASSIFICATION)

    assert first.resolution is second.resolution
    assert first.source_term == second.source_term


def test_conflicting_target_is_unresolved_and_records_precedence_evidence() -> None:
    plan = _plan()
    operation = _operation(plan, TerminologyRemediationCategory.CONFLICTING_TARGET)

    assert operation.action is TerminologyRemediationAction.CHANGE_TARGET_REQUIRES_REVIEW
    assert operation.resolution is TerminologyRemediationResolution.UNRESOLVED
    assert "requires_new_target_text" in operation.blocking_evidence
    assert "all_involved_decisions_share_equal_ai_priority" in operation.blocking_evidence
    assert operation.provenance["has_locked_user_authority"] is False
    assert operation.provenance["has_legacy_glossary_authority"] is False


def test_locked_user_decision_outranks_an_unlocked_rival() -> None:
    registry = _registry()
    view = canonical_view(registry, EMPTY_GLOSSARY)
    locked = canonical_lookup(view, "Ajah")

    assert locked is not None
    assert locked.priority == 5
    assert locked.target_term == "آجای"


def test_registry_decision_outranks_a_legacy_glossary_entry() -> None:
    glossary = {**EMPTY_GLOSSARY, "characters": {"Ajah": "آجای قدیمی"}}
    view = canonical_view(_registry(), glossary)
    resolved = canonical_lookup(view, "Ajah")

    assert resolved is not None
    assert resolved.origin == "registry"
    assert resolved.target_term == "آجای"


def test_invalid_competing_variant_removes_only_the_variant_relation() -> None:
    registry = _registry()
    plan = _plan(registry)
    operation = _operation(plan, TerminologyRemediationCategory.INVALID_COMPETING_VARIANT)

    assert operation.action is TerminologyRemediationAction.REMOVE_INVALID_VARIANT
    assert operation.resolution is TerminologyRemediationResolution.DETERMINISTIC
    assert operation.mutates_registry is True
    assert [item.removed_variant for item in operation.variant_removals] == ["لرد اژدها"]

    applied = apply_remediation_plan(registry, plan)
    decisions = {item.candidate_id: item for item in applied.decisions}
    holder = decisions[candidate_identity("Dragon", "en")]

    assert holder.competing_variants == []
    assert holder.target_term == "اژدها"


def test_legitimate_full_alternative_is_preserved() -> None:
    registry = _registry()
    kept = "اژدهای کاملا متفاوت"
    registry = registry.model_copy(
        update={
            "decisions": [
                item.model_copy(update={"competing_variants": [*item.competing_variants, kept]})
                if item.candidate_id == candidate_identity("Dragon", "en")
                else item
                for item in registry.decisions
            ]
        }
    )
    applied = apply_remediation_plan(registry, _plan(registry))
    decisions = {item.candidate_id: item for item in applied.decisions}

    assert decisions[candidate_identity("Dragon", "en")].competing_variants == [kept]


def test_normalized_identity_migration_preserves_provenance() -> None:
    registry = _registry()
    plan = _plan(registry)
    operation = _operation(plan, TerminologyRemediationCategory.NORMALIZATION_IDENTITY_BUG)

    assert operation.action is TerminologyRemediationAction.IDENTITY_MIGRATION
    assert operation.resolution is TerminologyRemediationResolution.DETERMINISTIC
    assert operation.migration is not None
    assert operation.migration.original_source_term == "Two Rivers' "
    assert operation.migration.normalized_source_term == "Two Rivers'"

    applied = apply_remediation_plan(registry, plan)
    candidates = {item.candidate_id: item for item in applied.candidates}
    normalized = candidates[candidate_identity("Two Rivers'", "en")]

    assert candidate_identity("Two Rivers' ", "en") not in candidates
    assert "Two Rivers" in normalized.aliases
    assert "historical whitespace-bearing evidence" in normalized.evidence_notes
    assert [item.chapter_id for item in normalized.occurrences] == ["019"]


def test_historical_candidate_identity_remains_resolvable_after_migration() -> None:
    registry = _registry()
    plan = _plan(registry)
    operation = _operation(plan, TerminologyRemediationCategory.NORMALIZATION_IDENTITY_BUG)
    applied = apply_remediation_plan(registry, plan)

    assert operation.migration is not None
    assert operation.migration.original_candidate_id == candidate_identity("Two Rivers' ", "en")
    assert operation.migration.normalized_candidate_id in {
        item.candidate_id for item in applied.candidates
    }


def test_migration_resolves_the_identity_mismatch_finding() -> None:
    registry = _registry()
    applied = apply_remediation_plan(registry, _plan(registry))

    assert audit_registry_remediation(applied, EMPTY_GLOSSARY).bookkeeping_mismatch is None


def test_apply_aborts_when_the_registry_drifted() -> None:
    registry = _registry()
    plan = _plan(registry)
    drifted = registry.model_copy(update={"version": "9.9.9"})

    with pytest.raises(TerminologyRemediationPlanError, match="drifted"):
        apply_remediation_plan(drifted, plan)


def test_apply_makes_no_partial_mutation_when_an_operation_fails() -> None:
    registry = _registry()
    plan = _plan(registry)
    broken = plan.model_copy(
        update={
            "operations": [
                item.model_copy(
                    update={
                        "variant_removals": [
                            removal.model_copy(update={"holder_decision_id": "td-" + "0" * 24})
                            for removal in item.variant_removals
                        ]
                    }
                )
                if item.variant_removals
                else item
                for item in plan.operations
            ]
        }
    )
    before = registry_identity(registry)

    with pytest.raises(TerminologyRemediationPlanError, match="missing"):
        apply_remediation_plan(registry, broken)
    assert registry_identity(registry) == before


def test_applying_a_plan_twice_aborts_instead_of_double_applying() -> None:
    registry = _registry()
    plan = _plan(registry)
    applied = apply_remediation_plan(registry, plan)

    with pytest.raises(TerminologyRemediationPlanError, match="drifted"):
        apply_remediation_plan(applied, plan)


def test_expected_semantic_hash_matches_the_applied_registry() -> None:
    registry = _registry()
    plan = _plan(registry)

    assert registry_identity(apply_remediation_plan(registry, plan)) == (
        plan.expected_semantic_sha256
    )


def _write_registry(tmp_path: Path) -> Path:
    path = tmp_path / "terminology.yaml"
    save_registry(path, _registry())
    return path


def test_dry_run_simulates_without_touching_the_real_registry(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    registry = load_registry(path)
    before = path.read_bytes()
    plan = _plan(
        registry,
        registry_raw_sha256=hashlib.sha256(before).hexdigest(),
    )

    simulation = simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)

    assert simulation.dry_run is True
    assert simulation.real_registry_unchanged is True
    assert path.read_bytes() == before
    assert simulation.simulated_registry_semantic_sha256 != (
        simulation.source_registry_semantic_sha256
    )


def test_dry_run_reports_post_remediation_audit_and_counts(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    plan = _plan(
        load_registry(path),
        registry_raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    simulation = simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)

    assert simulation.candidates_after == simulation.candidates_before - 1
    assert simulation.identity_mismatches_after == 0
    assert "invalid_competing_variant" not in simulation.category_counts_after
    assert simulation.audit_findings_after < simulation.audit_findings_before


def test_dry_run_reports_no_lookup_regression_and_preserves_ajah(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    plan = _plan(
        load_registry(path),
        registry_raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    simulation = simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)

    assert simulation.lookup_regression_count == 0
    assert simulation.protected_terms_unchanged["Ajah"] is True
    assert simulation.protected_terms_after["Ajah"] == "آجای"
    assert simulation.locked_decisions_preserved is True
    assert all(
        item.change_kind is not TerminologyLookupChangeKind.UNINTENDED_REGRESSION
        for item in simulation.lookup_comparisons
    )


def test_ajah_decision_stays_byte_exact_user_owned_and_locked(tmp_path: Path) -> None:
    registry = _registry()
    applied = apply_remediation_plan(registry, _plan(registry))
    before = next(
        item for item in registry.decisions if item.candidate_id == candidate_identity("Ajah", "en")
    )
    after = next(
        item for item in applied.decisions if item.candidate_id == candidate_identity("Ajah", "en")
    )

    assert after == before
    assert after.target_term == "آجای"
    assert after.decision_source is DecisionSource.USER
    assert after.locked is True


def test_dry_run_reports_unresolved_operations_as_blockers(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    plan = _plan(
        load_registry(path),
        registry_raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    simulation = simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)

    assert plan.applicable is False
    assert simulation.safe_to_apply is False
    assert simulation.skipped_unresolved_count == plan.unresolved_operation_count
    assert simulation.blockers


def test_dry_run_aborts_when_the_registry_file_drifted(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    plan = _plan(load_registry(path), registry_raw_sha256="c" * 64)

    with pytest.raises(TerminologyRemediationPlanError, match="drifted"):
        simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)


def test_dry_run_writes_no_file_anywhere(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    runs = tmp_path / "workspace" / "terminology-runs"
    runs.mkdir(parents=True)
    (runs / "immutable.json").write_text('{"kept": true}', encoding="utf-8")
    before = {item: item.read_bytes() for item in sorted(tmp_path.rglob("*")) if item.is_file()}
    plan = _plan(
        load_registry(path),
        registry_raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)
    after = {item: item.read_bytes() for item in sorted(tmp_path.rglob("*")) if item.is_file()}

    assert after == before


def test_execution_refuses_a_plan_with_unresolved_operations(tmp_path: Path) -> None:
    path = _write_registry(tmp_path)
    before = path.read_bytes()
    plan = _plan(
        load_registry(path),
        registry_raw_sha256=hashlib.sha256(before).hexdigest(),
    )
    simulation = simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)

    with pytest.raises(TerminologyRemediationPlanError, match="unresolved"):
        execute_remediation_plan(path, plan, simulation, evidence_directory=tmp_path / "evidence")
    assert path.read_bytes() == before
    assert not (tmp_path / "evidence").exists()


def _resolvable_registry() -> TerminologyRegistry:
    """A registry whose every audited finding has a deterministic remediation."""
    candidates = [
        _candidate("Dragon", classification=TerminologyType.TITLE),
        _candidate("Lord Dragon", classification=TerminologyType.TITLE),
        _candidate("Ajah", classification=TerminologyType.ORGANIZATION),
        TerminologyCandidate(
            candidate_id=candidate_identity("Two Rivers' ", "en"),
            source_term="Two Rivers' ",
            classification=TerminologyType.PLACE,
            confidence=0.9,
            status=CandidateStatus.CANDIDATE,
            provenance=DecisionSource.AI,
            aliases=["Two Rivers"],
            occurrences=[TerminologyOccurrence(chapter_id="019", chunk_id="019-003", count=1)],
            evidence_notes=["historical whitespace-bearing evidence"],
        ),
        _candidate("Two Rivers'", classification=TerminologyType.PLACE),
    ]
    decisions = [
        _decision("Dragon", "اژدها", variants=("لرد اژدها",)),
        _decision("Lord Dragon", "لرد اژدها"),
        _decision("Ajah", "آجای", decision_source=DecisionSource.USER, locked=True),
        _decision("Two Rivers'", "دو رود"),
    ]
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-remediation-book",
        source_language="en",
        target_language="fa",
        candidates=candidates,
        decisions=decisions,
    )
    return TerminologyRegistry.model_validate_json(canonical_registry_bytes(registry))


def test_a_fully_deterministic_plan_is_applicable() -> None:
    plan = _plan(_resolvable_registry())

    assert plan.unresolved_operation_count == 0
    assert plan.applicable is True
    assert plan.mutating_operation_count == 2


def test_post_remediation_reaudit_reports_no_blockers() -> None:
    registry = _resolvable_registry()
    applied = apply_remediation_plan(registry, _plan(registry))
    report = audit_registry_remediation(applied, EMPTY_GLOSSARY)

    assert report.definitely_invalid_count == 0
    assert report.bookkeeping_mismatch is None
    assert report.remaining_registry_blockers == 0


def test_dry_run_of_a_deterministic_plan_is_safe_to_apply(tmp_path: Path) -> None:
    path = tmp_path / "terminology.yaml"
    save_registry(path, _resolvable_registry())
    plan = _plan(
        load_registry(path), registry_raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )

    simulation = simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)

    assert simulation.safe_to_apply is True
    assert simulation.blockers == []
    assert simulation.blocking_findings_after == 0
    assert simulation.lookup_regression_count == 0
    assert simulation.protected_terms_unchanged["Ajah"] is True


def test_execution_writes_atomically_and_persists_evidence(tmp_path: Path) -> None:
    path = tmp_path / "terminology.yaml"
    save_registry(path, _resolvable_registry())
    plan = _plan(
        load_registry(path), registry_raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )
    simulation = simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)
    evidence = tmp_path / "evidence"

    updated = execute_remediation_plan(path, plan, simulation, evidence_directory=evidence)

    assert registry_identity(updated) == plan.expected_semantic_sha256
    assert registry_identity(load_registry(path)) == plan.expected_semantic_sha256
    assert (evidence / "plan.json").is_file()
    assert (evidence / "dry-run.json").is_file()
    assert not list(tmp_path.glob("*.tmp"))
    assert load_registry(path).version == plan.registry_version_after


def test_execution_is_idempotent_because_reapplying_aborts(tmp_path: Path) -> None:
    path = tmp_path / "terminology.yaml"
    save_registry(path, _resolvable_registry())
    plan = _plan(
        load_registry(path), registry_raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )
    simulation = simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)
    execute_remediation_plan(path, plan, simulation, evidence_directory=tmp_path / "evidence")
    applied = path.read_bytes()

    with pytest.raises(TerminologyRemediationPlanError, match="drifted"):
        execute_remediation_plan(path, plan, simulation, evidence_directory=tmp_path / "again")
    assert path.read_bytes() == applied


def test_execution_refuses_when_the_dry_run_was_for_another_plan(tmp_path: Path) -> None:
    path = tmp_path / "terminology.yaml"
    save_registry(path, _resolvable_registry())
    plan = _plan(
        load_registry(path), registry_raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )
    simulation = simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)
    foreign = simulation.model_copy(update={"plan_id": "trp-" + "0" * 24})
    before = path.read_bytes()

    with pytest.raises(TerminologyRemediationPlanError, match="dry run"):
        execute_remediation_plan(path, plan, foreign, evidence_directory=tmp_path / "evidence")
    assert path.read_bytes() == before
