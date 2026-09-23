"""Tests for the second-round residual terminology reconciliation.

Every fixture below is synthetic.  The shapes reproduce real defects - an Ajah colour shorthand, a
reciprocal short-name alias cycle, a singular/plural artifact pair - but no book prose is used.
"""

import hashlib
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
    ConflictRelation,
    DisclosedMaterial,
    InferenceLocality,
    LineageArtifactKind,
    ReconciliationClassification,
    RegistryEdit,
    RegistryEditKind,
    ResidualReconciliationRun,
    ResidualResolution,
    ResidualResolutionOutcome,
    ReviewerProvenance,
)
from book_translator.models.terminology_remediation import TerminologyRemediationPlan
from book_translator.terminology.canonical import (
    canonical_constraints,
    canonical_view,
    canonicalization_confidence,
    elliptical_shorthand_sources,
    proposal_conflicts,
)
from book_translator.terminology.conflict_graph import detect_alias_cycles
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
    _registry_invariants,
    apply_remediation_plan,
    canonical_lookup,
    execute_remediation_plan,
    plan_registry_remediation,
    simulate_remediation_plan,
)
from book_translator.terminology.residual_reconciliation import (
    ResidualFinding,
    alias_cycle_finding_id,
    build_residual_components,
    build_residual_run,
    final_remediation_plan,
    harden_remediation_plan,
    publish_lineage,
    residual_operations,
    variant_invariant_operations,
    verify_lineage,
)
from book_translator.terminology.target_style import canonical_targets_equal

EMPTY_GLOSSARY: dict[str, object] = {
    "terms": {},
    "characters": {},
    "places": {},
    "groups": {},
    "unresolved_terms": [],
}
FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)

RESIDUAL_PROVENANCE = ReviewerProvenance(
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
    disclosure_note="synthetic fixture resolutions authored offline by the test itself",
)

PREVIOUS_RUN_ID = "tsr-run-" + "1" * 24
PREVIOUS_RESOLUTION_SET_SHA = "c" * 64


def _candidate(
    source: str,
    *,
    aliases: tuple[str, ...] = (),
    classification: TerminologyType = TerminologyType.PERSON,
    context_sensitive: bool = False,
    status: CandidateStatus = CandidateStatus.APPROVED,
) -> TerminologyCandidate:
    return TerminologyCandidate(
        candidate_id=candidate_identity(source, "en"),
        source_term=source,
        classification=classification,
        confidence=1.0,
        status=status,
        provenance=DecisionSource.AI,
        aliases=list(aliases),
        context_sensitive=context_sensitive,
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
    """A synthetic registry carrying exactly the residual defect shapes."""
    candidates = [
        # An Ajah colour shorthand whose target renders the longer name it elides.
        _candidate("Ajah", classification=TerminologyType.ORGANIZATION),
        _candidate("Red", classification=TerminologyType.ORGANIZATION),
        _candidate("Red Ajah", classification=TerminologyType.ORGANIZATION),
        # Unrelated proper names that merely contain the ordinary adjective.
        _candidate("Red Eagle", classification=TerminologyType.ARTIFACT),
        _candidate("Band of the Red Hand", classification=TerminologyType.ORGANIZATION),
        _candidate("Red Shields", classification=TerminologyType.ORGANIZATION),
        # A genuinely meaningful canonical component and its compound.
        _candidate("Whitecloak", classification=TerminologyType.PEOPLE),
        _candidate("Whitecloak War", classification=TerminologyType.HISTORICAL_EVENT),
        # A reciprocal short-name/full-name alias cycle.
        _candidate("Hadnan", aliases=("Hadnan Kadere",)),
        _candidate("Hadnan Kadere", aliases=("Hadnan", "Kadere")),
        _candidate("Kadere"),
        # A singular/plural artifact pair joined by a false alias.
        _candidate("silver arrow", classification=TerminologyType.ARTIFACT),
        _candidate(
            "silver arrows", aliases=("silver arrow",), classification=TerminologyType.ARTIFACT
        ),
        # Ordinary vocabulary that must never be globally canonicalized.
        _candidate(
            "stone", classification=TerminologyType.CONTEXT_SENSITIVE, context_sensitive=True
        ),
        _candidate(
            "stones", classification=TerminologyType.CONTEXT_SENSITIVE, context_sensitive=True
        ),
    ]
    decisions = [
        _decision("Ajah", "آجای", decision_source=DecisionSource.USER, locked=True),
        _decision("Red", "آجای سرخ"),
        _decision("Red Ajah", "آجای سرخ"),
        _decision("Red Eagle", "عقاب سرخ"),
        _decision("Band of the Red Hand", "دستهٔ دست سرخ"),
        _decision("Red Shields", "سپرهای سرخ"),
        _decision("Whitecloak", "فرزند نور"),
        _decision("Whitecloak War", "جنگ فرزند نور"),
        _decision("Hadnan", "هادنان"),
        _decision("Hadnan Kadere", "هادنان کادیر"),
        _decision("Kadere", "کادیر"),
        _decision("silver arrow", "تیر نقره‌ای"),
        _decision("silver arrows", "تیرهای نقره‌ای"),
    ]
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-residual-book",
        source_language="en",
        target_language="fa",
        candidates=candidates,
        decisions=decisions,
    )
    return TerminologyRegistry.model_validate_json(canonical_registry_bytes(registry))


def _view(registry: TerminologyRegistry) -> tuple[Any, ...]:
    return canonical_view(registry, EMPTY_GLOSSARY)


def _by_source(registry: TerminologyRegistry, source: str) -> TerminologyCandidate:
    return next(item for item in registry.candidates if item.source_term == source)


# --------------------------------------------------------------------------------------------
# Validator safety: ordinary adjective components versus named canonical components
# --------------------------------------------------------------------------------------------


def test_ordinary_adjective_component_is_recognised_as_an_elliptical_shorthand() -> None:
    """'Red' elides 'Red Ajah'; 'Whitecloak' is a component in its own right."""
    shorthands = elliptical_shorthand_sources(_view(_registry()))

    assert "Red" in shorthands
    assert "Whitecloak" not in shorthands
    assert "Red Ajah" not in shorthands


def test_red_does_not_impose_the_red_ajah_target_on_unrelated_names() -> None:
    registry = _registry()
    view = _view(registry)

    for source in ("Red Eagle", "Band of the Red Hand", "Red Shields"):
        constraints = canonical_constraints(_by_source(registry, source), view)
        assert "Red" not in {item.source_term for item in constraints}, source


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("Red Eagle", "عقاب سرخ"),
        ("Band of the Red Hand", "دستهٔ دست سرخ"),
        ("Red Shields", "سپرهای سرخ"),
    ],
)
def test_unrelated_compounds_containing_red_raise_no_conflict(source: str, target: str) -> None:
    registry = _registry()
    report = audit_registry_remediation(registry, EMPTY_GLOSSARY)

    flagged = {item.source_term for item in report.findings if item.blocking}
    assert source not in flagged
    assert _decision(source, target).target_term == target


def test_valid_canonical_compound_is_still_detected() -> None:
    """A meaningful component must keep constraining its compound."""
    registry = _registry()
    view = _view(registry)

    constraints = canonical_constraints(_by_source(registry, "Whitecloak War"), view)
    assert "Whitecloak" in {item.source_term for item in constraints}


def test_a_compound_that_drops_its_canonical_component_still_conflicts() -> None:
    """The general rule must not be weakened into silence."""
    registry = _registry()
    view = _view(registry)
    candidate = _by_source(registry, "Whitecloak War")
    proposal = next(
        item for item in registry.decisions if item.candidate_id == candidate.candidate_id
    )

    from book_translator.models import TerminologyProposal

    broken = TerminologyProposal(
        proposal_id="tp-" + "0" * 24,
        candidate_id=candidate.candidate_id,
        source_term=candidate.source_term,
        proposed_target="جنگ ردا سفید",
        confidence=1.0,
        classification=candidate.classification,
        known_variants=proposal.competing_variants,
    )
    assert "canonical_subterm_conflict" in proposal_conflicts(broken, candidate, view, "fa")


def test_possessive_and_determiner_pairs_are_not_treated_as_elisions() -> None:
    """Only a clean whole-word split with canonical evidence counts as an elision."""
    registry = _registry()
    extra_candidates = [
        *registry.candidates,
        _candidate("The Two Rivers", classification=TerminologyType.PLACE),
        _candidate("Two Rivers", classification=TerminologyType.PLACE),
        _candidate("Mat's"),
        _candidate("Mat"),
    ]
    extra_decisions = [
        *registry.decisions,
        _decision("The Two Rivers", "دو رود"),
        _decision("Two Rivers", "دو رود"),
        _decision("Mat's", "مت"),
        _decision("Mat", "مت"),
    ]
    widened = registry.model_copy(
        update={"candidates": extra_candidates, "decisions": extra_decisions}
    )

    shorthands = elliptical_shorthand_sources(_view(widened))
    assert "Two Rivers" not in shorthands
    assert "Mat" not in shorthands
    assert "Red" in shorthands


# --------------------------------------------------------------------------------------------
# Singular/plural terminology versus ordinary vocabulary
# --------------------------------------------------------------------------------------------


def test_singular_plural_terminology_pair_is_reconciled_without_stemming() -> None:
    registry = _registry()
    plural = _by_source(registry, "silver arrows")
    assert "silver arrow" in plural.aliases

    cleaned = registry.model_copy(
        update={
            "candidates": [
                item.model_copy(update={"aliases": []})
                if item.source_term == "silver arrows"
                else item
                for item in registry.candidates
            ]
        }
    )
    view = _view(cleaned)
    singular = _by_source(cleaned, "silver arrow")

    # With the false alias gone the two forms no longer match each other on safe boundaries.
    assert "silver arrows" not in {
        item.source_term for item in canonical_constraints(singular, view)
    }
    report = audit_registry_remediation(cleaned, EMPTY_GLOSSARY)
    blocking = {item.source_term for item in report.findings if item.blocking}
    assert "silver arrow" not in blocking


def test_ordinary_singular_plural_vocabulary_is_rejected_for_canonicalization() -> None:
    registry = _registry()

    for source in ("stone", "stones"):
        assert canonicalization_confidence(_by_source(registry, source)) == 0.0


# --------------------------------------------------------------------------------------------
# Alias direction and cycle removal
# --------------------------------------------------------------------------------------------


def test_short_name_and_full_name_alias_cycle_is_detected() -> None:
    assert ["Hadnan", "Hadnan Kadere"] in detect_alias_cycles(_registry())


def test_removing_one_direction_of_a_short_name_alias_clears_the_cycle() -> None:
    registry = _registry()
    without_reverse = registry.model_copy(
        update={
            "candidates": [
                item.model_copy(update={"aliases": []}) if item.source_term == "Hadnan" else item
                for item in registry.candidates
            ]
        }
    )

    assert detect_alias_cycles(without_reverse) == []


def test_independent_short_and_full_names_remain_mutually_consistent() -> None:
    """Both forms keep a canonical decision and neither conflicts with the other."""
    registry = _registry()
    resolved = registry.model_copy(
        update={
            "candidates": [
                item.model_copy(update={"aliases": []})
                if item.source_term in {"Hadnan", "Hadnan Kadere"}
                else item
                for item in registry.candidates
            ]
        }
    )
    view = _view(resolved)

    for source, expected in (
        ("Hadnan", "هادنان"),
        ("Hadnan Kadere", "هادنان کادیر"),
        ("Kadere", "کادیر"),
    ):
        term = canonical_lookup(view, source)
        assert term is not None, source
        assert term.target_term == expected, source
    assert detect_alias_cycles(resolved) == []


# --------------------------------------------------------------------------------------------
# Residual reconciliation over a prospective state
# --------------------------------------------------------------------------------------------


def _prospective(registry: TerminologyRegistry) -> tuple[TerminologyRemediationPlan, Any]:
    report = audit_registry_remediation(registry, EMPTY_GLOSSARY)
    plan = plan_registry_remediation(
        registry,
        EMPTY_GLOSSARY,
        report,
        registry_raw_sha256="a" * 64,
        glossary_sha256="b" * 64,
        created_at=FIXED_TIME,
    )
    return plan, apply_remediation_plan(registry, plan)


def _residual_inputs(
    prospective: TerminologyRegistry,
) -> tuple[tuple[ResidualFinding, ...], tuple[tuple[str, ...], ...]]:
    findings = tuple(
        ResidualFinding(
            finding_id=item.finding_id,
            source_term=item.source_term,
            candidate_id=item.candidate_id,
            decision_id=item.decision_id,
            related_source_terms=tuple(
                value for value in item.related_source_terms if value != item.source_term
            ),
            relation=ConflictRelation.ALIAS_RELATIONSHIP,
            detail=item.triggered_rule,
        )
        for item in audit_registry_remediation(prospective, EMPTY_GLOSSARY).findings
        if item.blocking
    )
    cycles = tuple(tuple(item) for item in detect_alias_cycles(prospective))
    return findings, cycles


def _residual_run(
    registry: TerminologyRegistry, plan: TerminologyRemediationPlan, prospective: Any
) -> ResidualReconciliationRun:
    findings, cycles = _residual_inputs(prospective)
    prospective_sha = registry_identity(prospective)
    components = build_residual_components(
        prospective, findings, cycles, prospective_semantic_sha256=prospective_sha
    )
    resolutions = []
    for component in components:
        edits = [
            RegistryEdit(
                kind=RegistryEditKind.REMOVE_ALIAS,
                candidate_id=_by_source(prospective, holder).candidate_id,
                source_term=holder,
                value=alias,
                reason="the alias form owns its own canonical decision",
            )
            for holder in component.source_terms
            for alias in _by_source(prospective, holder).aliases
            if alias in component.source_terms
        ]
        resolutions.append(
            ResidualResolution(
                component_id=component.component_id,
                outcome=(
                    ResidualResolutionOutcome.VALID_DISTINCT_TERMS
                    if edits
                    else ResidualResolutionOutcome.VALIDATOR_FALSE_POSITIVE
                ),
                classification=ReconciliationClassification.INVALID_RELATIONSHIP_ONLY,
                finding_ids=list(component.finding_ids),
                edits=edits,
                validator_rule_change=None if edits else "elliptical shorthand suppression",
                resolves_alias_cycle=bool(component.alias_cycle),
                confidence=0.9,
                evidence_references=["synthetic fixture"],
                reasoning_summary="synthetic residual resolution",
            )
        )
    return build_residual_run(
        components,
        tuple(resolutions),
        RESIDUAL_PROVENANCE,
        previous_run_id=PREVIOUS_RUN_ID,
        previous_resolution_set_sha256=PREVIOUS_RESOLUTION_SET_SHA,
        previous_plan_id=plan.plan_id,
        provenance_correction_id=None,
        source_registry_semantic_sha256=registry_identity(registry),
        source_registry_raw_sha256="a" * 64,
        prospective_semantic_sha256=prospective_sha,
        created_at=FIXED_TIME,
    )


def test_residual_components_cover_every_blocker_and_cycle() -> None:
    registry = _registry()
    plan, prospective = _prospective(registry)
    findings, cycles = _residual_inputs(prospective)
    components = build_residual_components(
        prospective, findings, cycles, prospective_semantic_sha256=registry_identity(prospective)
    )

    covered = {value for item in components for value in item.finding_ids}
    assert {item.finding_id for item in findings} <= covered
    assert [list(item) for item in cycles] == [
        item.alias_cycle for item in components if item.alias_cycle
    ]


def test_alias_closure_pulls_in_a_peer_that_owns_its_own_decision() -> None:
    """Removing only the cycle would leave the surname alias as a fresh blocker."""
    registry = _registry()
    _, prospective = _prospective(registry)
    cycles = tuple(tuple(item) for item in detect_alias_cycles(prospective))
    components = build_residual_components(
        prospective, (), cycles, prospective_semantic_sha256=registry_identity(prospective)
    )

    family = next(item for item in components if "Hadnan" in item.source_terms)
    assert "Kadere" in family.source_terms


def test_alias_cycle_finding_id_is_deterministic_and_order_independent() -> None:
    first = alias_cycle_finding_id("d" * 64, ("Hadnan", "Hadnan Kadere"))
    second = alias_cycle_finding_id("d" * 64, ("Hadnan Kadere", "Hadnan"))

    assert first == second
    assert first.startswith("rmf-")


def test_residual_run_records_its_superseding_lineage() -> None:
    registry = _registry()
    plan, prospective = _prospective(registry)
    run = _residual_run(registry, plan, prospective)

    assert run.run_id.startswith("tsr-residual-")
    assert run.previous_run_id == PREVIOUS_RUN_ID
    assert run.previous_resolution_set_sha256 == PREVIOUS_RESOLUTION_SET_SHA
    assert run.previous_plan_id == plan.plan_id
    assert run.prospective_semantic_sha256 == registry_identity(prospective)
    assert run.source_registry_semantic_sha256 == registry_identity(registry)
    assert run.escalation_count == 0


def test_residual_run_rejects_a_mismatched_combined_plan() -> None:
    registry = _registry()
    plan, prospective = _prospective(registry)
    run = _residual_run(registry, plan, prospective)
    foreign = plan.model_copy(update={"plan_id": "trp-" + "9" * 24})

    with pytest.raises(Exception, match="does not follow"):
        final_remediation_plan(registry, foreign, run)


def test_final_plan_supersedes_without_rewriting_the_previous_plan() -> None:
    registry = _registry()
    plan, prospective = _prospective(registry)
    run = _residual_run(registry, plan, prospective)
    before = plan.model_dump(mode="json")

    final = final_remediation_plan(registry, plan, run, created_at=FIXED_TIME)

    assert final.plan_id != plan.plan_id
    assert final.operation_count == plan.operation_count + len(residual_operations(registry, run))
    assert plan.model_dump(mode="json") == before
    assert {item.operation_id for item in plan.operations} <= {
        item.operation_id for item in final.operations
    }
    assert [item.sequence for item in final.operations] == list(range(1, final.operation_count + 1))


def test_final_plan_clears_every_blocker_and_alias_cycle() -> None:
    registry = _registry()
    plan, prospective = _prospective(registry)
    run = _residual_run(registry, plan, prospective)
    final = final_remediation_plan(registry, plan, run, created_at=FIXED_TIME)

    after = apply_remediation_plan(registry, final)
    report = audit_registry_remediation(after, EMPTY_GLOSSARY)

    assert [item for item in report.findings if item.blocking] == []
    assert detect_alias_cycles(after) == []
    assert report.bookkeeping_mismatch is None


def test_final_dry_run_leaves_the_registry_byte_identical(tmp_path: Path) -> None:
    registry = _registry()
    path = tmp_path / "terminology.yaml"
    save_registry(path, registry)
    raw_before = path.read_bytes()

    report = audit_registry_remediation(registry, EMPTY_GLOSSARY)
    plan = plan_registry_remediation(
        registry,
        EMPTY_GLOSSARY,
        report,
        registry_raw_sha256=__import__("hashlib").sha256(raw_before).hexdigest(),
        glossary_sha256="b" * 64,
        created_at=FIXED_TIME,
    )
    prospective = apply_remediation_plan(registry, plan)
    run = _residual_run(registry, plan, prospective)
    run = run.model_copy(
        update={"source_registry_raw_sha256": __import__("hashlib").sha256(raw_before).hexdigest()}
    )
    final = final_remediation_plan(registry, plan, run, created_at=FIXED_TIME)

    simulation = simulate_remediation_plan(
        path, EMPTY_GLOSSARY, final, project_root=tmp_path, created_at=FIXED_TIME
    )

    assert simulation.dry_run is True
    assert simulation.blocking_findings_after == 0
    assert simulation.identity_mismatches_after == 0
    assert simulation.lookup_regression_count == 0
    assert simulation.real_registry_unchanged is True
    assert path.read_bytes() == raw_before
    assert detect_alias_cycles(load_registry(path)) != []


def test_protected_lookups_stay_correct_and_ajah_stays_locked() -> None:
    registry = _registry()
    plan, prospective = _prospective(registry)
    run = _residual_run(registry, plan, prospective)
    final = final_remediation_plan(registry, plan, run, created_at=FIXED_TIME)
    after = apply_remediation_plan(registry, final)
    view = canonical_view(after, EMPTY_GLOSSARY)

    for source, expected in (
        ("Ajah", "آجای"),
        ("Red Ajah", "آجای سرخ"),
        ("Whitecloak", "فرزند نور"),
        ("Hadnan", "هادنان"),
        ("Hadnan Kadere", "هادنان کادیر"),
    ):
        term = canonical_lookup(view, source)
        assert term is not None, source
        assert canonical_targets_equal(term.target_term, expected, "fa"), source

    ajah = canonical_lookup(view, "Ajah")
    assert ajah is not None
    assert ajah.locked is True
    assert ajah.decision_source is DecisionSource.USER
    assert ajah.target_term == "آجای"


# --------------------------------------------------------------------------------------------
# Competing-variant invariant hardening
# --------------------------------------------------------------------------------------------


def _registry_with_variant_collision() -> TerminologyRegistry:
    """Two unrelated names where one stores the other's canonical target as a rejected variant."""
    registry = _registry()
    candidates = [
        *registry.candidates,
        _candidate("Davram Bashere"),
        _candidate("Lord of Bashere", classification=TerminologyType.TITLE),
    ]
    decisions = [
        *registry.decisions,
        _decision("Davram Bashere", "داورام باشیر", variants=("لرد باشیر",)),
        _decision("Lord of Bashere", "لرد باشیر"),
    ]
    return registry.model_copy(update={"candidates": candidates, "decisions": decisions})


def test_variant_invariant_is_detected_when_families_are_not_nested() -> None:
    """Neither source term contains the other, so the finding-driven planner never compares them."""
    registry = _registry_with_variant_collision()

    operations = variant_invariant_operations(registry)

    assert len(operations) == 1
    removal = operations[0].variant_removals[0]
    assert removal.holder_source_term == "Davram Bashere"
    assert removal.removed_variant == "لرد باشیر"
    assert removal.canonical_owner_source_term == "Lord of Bashere"
    assert operations[0].finding_id.startswith("rmf-")


def test_variant_invariant_scan_ignores_what_the_base_plan_already_removes() -> None:
    """Scanning the pre-plan registry would re-plan no-ops for variants already stripped."""
    registry = _registry_with_variant_collision()
    report = audit_registry_remediation(registry, EMPTY_GLOSSARY)
    base = plan_registry_remediation(
        registry,
        EMPTY_GLOSSARY,
        report,
        registry_raw_sha256="a" * 64,
        glossary_sha256="b" * 64,
        created_at=FIXED_TIME,
    )
    prospective = apply_remediation_plan(registry, base)
    surviving = {
        (decision.decision_id, variant)
        for decision in prospective.decisions
        for variant in decision.competing_variants
    }

    planned = {
        (
            operation.variant_removals[0].holder_decision_id,
            operation.variant_removals[0].removed_variant,
        )
        for operation in variant_invariant_operations(registry, existing_plan=base)
    }
    assert planned <= surviving


def test_hardened_plan_clears_every_variant_invariant_violation() -> None:
    registry = _registry_with_variant_collision()
    report = audit_registry_remediation(registry, EMPTY_GLOSSARY)
    base = plan_registry_remediation(
        registry,
        EMPTY_GLOSSARY,
        report,
        registry_raw_sha256="a" * 64,
        glossary_sha256="b" * 64,
        created_at=FIXED_TIME,
    )

    hardened = harden_remediation_plan(registry, base, created_at=FIXED_TIME)
    after = apply_remediation_plan(registry, hardened)

    assert hardened.plan_id != base.plan_id
    assert hardened.operation_count > base.operation_count
    assert _registry_invariants(after) == []
    holder = next(
        item
        for item in after.decisions
        if item.decision_id == _decision("Davram Bashere", "x").decision_id
    )
    assert holder.competing_variants == []
    assert holder.target_term == "داورام باشیر"


def test_hardening_is_a_no_op_when_no_invariant_survives() -> None:
    registry = _registry()
    report = audit_registry_remediation(registry, EMPTY_GLOSSARY)
    base = plan_registry_remediation(
        registry,
        EMPTY_GLOSSARY,
        report,
        registry_raw_sha256="a" * 64,
        glossary_sha256="b" * 64,
        created_at=FIXED_TIME,
    )

    assert harden_remediation_plan(registry, base, created_at=FIXED_TIME).plan_id == base.plan_id


def test_competing_variant_invariant_is_not_weakened_globally() -> None:
    """The invariant itself must still report a collision that has not been remediated."""
    assert _registry_invariants(_registry_with_variant_collision()) != []


# --------------------------------------------------------------------------------------------
# Durable lineage and the real non-dry application path
# --------------------------------------------------------------------------------------------


def _publishable(tmp_path: Path) -> tuple[Path, TerminologyRemediationPlan, Any]:
    """A synthetic project root with a registry, a safe plan, and its proven simulation."""
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    registry_path = root / "config" / "terminology.yaml"
    # Only the variant collision, so the deterministic plan is fully applicable and the
    # application path itself is what the test exercises.
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-apply-book",
        source_language="en",
        target_language="fa",
        candidates=[
            _candidate("Ajah", classification=TerminologyType.ORGANIZATION),
            _candidate("Davram Bashere"),
            _candidate("Lord of Bashere", classification=TerminologyType.TITLE),
        ],
        decisions=[
            _decision("Ajah", "آجای", decision_source=DecisionSource.USER, locked=True),
            _decision("Davram Bashere", "داورام باشیر", variants=("لرد باشیر",)),
            _decision("Lord of Bashere", "لرد باشیر"),
        ],
    )
    registry = TerminologyRegistry.model_validate_json(canonical_registry_bytes(registry))
    save_registry(registry_path, registry)
    raw_sha = hashlib.sha256(registry_path.read_bytes()).hexdigest()

    report = audit_registry_remediation(registry, EMPTY_GLOSSARY)
    base = plan_registry_remediation(
        registry,
        EMPTY_GLOSSARY,
        report,
        registry_raw_sha256=raw_sha,
        glossary_sha256="b" * 64,
        created_at=FIXED_TIME,
    )
    plan = harden_remediation_plan(registry, base, created_at=FIXED_TIME)
    simulation = simulate_remediation_plan(
        registry_path, EMPTY_GLOSSARY, plan, project_root=root, created_at=FIXED_TIME
    )
    return root, plan, simulation


def test_published_lineage_verifies_by_rehashing_every_artifact(tmp_path: Path) -> None:
    root, plan, simulation = _publishable(tmp_path)
    source = root / "lineage-source.json"
    source.write_text('{"kind": "synthetic"}\n', encoding="utf-8")

    lineage = publish_lineage(
        root,
        plan,
        simulation,
        {LineageArtifactKind.SUPERSEDED_PLAN: [("trp-" + "0" * 24, source)]},
        protected_lookups=[],
        accepted_conditions=["synthetic"],
        registry_path=root / "config" / "terminology.yaml",
        created_at=FIXED_TIME,
    )

    assert verify_lineage(root, plan.plan_id).plan_id == plan.plan_id
    assert lineage.supersedes_plan_ids == ["trp-" + "0" * 24]
    # The frozen pre-image is content addressed by the exact source bytes.
    frozen = next(
        item
        for item in lineage.artifacts
        if item.kind is LineageArtifactKind.FROZEN_SOURCE_REGISTRY
    )
    assert frozen.sha256 == plan.source_registry_raw_sha256
    assert (root / frozen.file).read_bytes() == (root / "config" / "terminology.yaml").read_bytes()


def test_lineage_verification_fails_when_a_published_artifact_is_edited(tmp_path: Path) -> None:
    root, plan, simulation = _publishable(tmp_path)
    source = root / "lineage-source.json"
    source.write_text('{"kind": "synthetic"}\n', encoding="utf-8")
    lineage = publish_lineage(
        root,
        plan,
        simulation,
        {LineageArtifactKind.RECONCILIATION_RUN: [("tsr-run-" + "0" * 24, source)]},
        protected_lookups=[],
        accepted_conditions=[],
        registry_path=root / "config" / "terminology.yaml",
        created_at=FIXED_TIME,
    )
    tampered = next(
        item for item in lineage.artifacts if item.kind is LineageArtifactKind.RECONCILIATION_RUN
    )
    (root / tampered.file).write_text('{"kind": "tampered"}\n', encoding="utf-8")

    with pytest.raises(Exception, match="lineage artifact changed"):
        verify_lineage(root, plan.plan_id)


def test_non_dry_apply_reaches_the_expected_registry_and_leaves_no_partial_state(
    tmp_path: Path,
) -> None:
    root, plan, simulation = _publishable(tmp_path)
    registry_path = root / "config" / "terminology.yaml"
    evidence = root / "evidence"

    updated = execute_remediation_plan(registry_path, plan, simulation, evidence_directory=evidence)

    assert registry_identity(updated) == plan.expected_semantic_sha256
    assert registry_identity(load_registry(registry_path)) == plan.expected_semantic_sha256
    assert load_registry(registry_path).version == plan.registry_version_after
    assert (evidence / "plan.json").is_file()
    assert (evidence / "dry-run.json").is_file()
    assert _registry_invariants(load_registry(registry_path)) == []
    assert load_registry(registry_path).decisions[0].locked is True
    assert [item for item in root.rglob("*.tmp*")] == []


def test_non_dry_apply_aborts_on_a_stale_source_registry(tmp_path: Path) -> None:
    root, plan, simulation = _publishable(tmp_path)
    registry_path = root / "config" / "terminology.yaml"
    drifted = registry_path.read_text(encoding="utf-8").replace(
        "book_id: synthetic-apply-book", "book_id: synthetic-apply-book-drifted", 1
    )
    registry_path.write_text(drifted, encoding="utf-8")
    before = registry_path.read_bytes()
    evidence = root / "evidence"

    with pytest.raises(Exception, match="drifted"):
        execute_remediation_plan(registry_path, plan, simulation, evidence_directory=evidence)

    assert registry_path.read_bytes() == before
    assert not evidence.exists()


def test_non_dry_apply_refuses_a_simulation_that_did_not_prove_safety(tmp_path: Path) -> None:
    root, plan, simulation = _publishable(tmp_path)
    registry_path = root / "config" / "terminology.yaml"
    before = registry_path.read_bytes()

    with pytest.raises(Exception, match="dry run"):
        execute_remediation_plan(
            registry_path,
            plan,
            simulation.model_copy(update={"safe_to_apply": False}),
            evidence_directory=root / "evidence",
        )

    assert registry_path.read_bytes() == before
