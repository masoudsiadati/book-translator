"""Regression tests for remediating exact-enforcement defects through the durable plan workflow.

Every fixture here is synthetic.  The invented world terms exist only to reproduce the *shapes* the
exact-enforcement audit reports — a finite clause, a bare common noun contradicted by compounds, an
elliptical shorthand, nested descriptions — because those shapes, not any particular book, are what
the remediation rules classify.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    QuestionCategory,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyEnforcement,
    TerminologyOccurrence,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.models.terminology_remediation import (
    TerminologyDecisionRetirement,
    TerminologyExactnessDisposition,
    TerminologyFindingFamily,
    TerminologyRemediationAction,
    TerminologyRemediationCategory,
    TerminologyRemediationOperation,
    TerminologyRemediationPlan,
    TerminologyRemediationResolution,
    TerminologyTargetCorrection,
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
    TerminologyRemediationPlanError,
    apply_remediation_plan,
    execute_remediation_plan,
    load_applied_remediations,
    plan_registry_remediation,
    simulate_remediation_plan,
)

ROOT = Path(__file__).parents[1]
FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)
EMPTY_GLOSSARY: dict[str, object] = {
    "terms": {},
    "characters": {},
    "places": {},
    "groups": {},
    "unresolved_terms": [],
}


def _candidate(
    source: str,
    *,
    classification: TerminologyType,
    occurrences: int = 1,
    aliases: tuple[str, ...] = (),
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
        occurrences=[
            TerminologyOccurrence(chapter_id="001", chunk_id="001-001", count=occurrences)
        ],
    )


def _decision(
    source: str,
    target: str,
    *,
    decision_source: DecisionSource = DecisionSource.AI,
    locked: bool = False,
    enforcement: TerminologyEnforcement | None = None,
) -> TerminologyDecision:
    candidate_id = candidate_identity(source, "en")
    return TerminologyDecision(
        decision_id=decision_identity(candidate_id, "fa"),
        candidate_id=candidate_id,
        target_term=target,
        decision_source=decision_source,
        locked=locked,
        enforcement=enforcement,
    )


# Each pair below reproduces exactly one audited shape.  The Persian targets are invented
# transliterations chosen so the containment relations the rules read are unambiguous.
_FIXTURE: tuple[tuple[TerminologyCandidate, TerminologyDecision], ...] = (
    # A finite clause promoted to a historical event.
    (
        _candidate("Kaldor fell", classification=TerminologyType.HISTORICAL_EVENT),
        _decision("Kaldor fell", "سقوط کالدور"),
    ),
    # A bare common noun contradicted by two independent named compounds.
    (
        _candidate("stone", classification=TerminologyType.ARTIFACT, occurrences=6),
        _decision("stone", "سنگ"),
    ),
    (
        _candidate("Aren's heavy stone", classification=TerminologyType.ARTIFACT),
        _decision("Aren's heavy stone", "گوهر سنگین آرن"),
    ),
    (
        _candidate("stone of the Vale", classification=TerminologyType.ARTIFACT),
        _decision("stone of the Vale", "گوهر ویل"),
    ),
    # An elliptical shorthand: the short target spells out the longer name it elides, and a third
    # compound reusing the ordinary colour word contradicts it.
    (
        _candidate("Crimson", classification=TerminologyType.ORGANIZATION, occurrences=5),
        _decision("Crimson", "دستهٔ سرخ"),
    ),
    (
        _candidate("Crimson Order", classification=TerminologyType.ORGANIZATION, occurrences=8),
        _decision("Crimson Order", "دستهٔ سرخ"),
    ),
    (
        _candidate("Order", classification=TerminologyType.ORGANIZATION, occurrences=9),
        _decision("Order", "دسته"),
    ),
    (
        _candidate("Crimson Tower", classification=TerminologyType.PLACE),
        _decision("Crimson Tower", "برج سرخ"),
    ),
    # Two nested wholly descriptive phrasings of one object.
    (
        _candidate("iron gate", classification=TerminologyType.ARTIFACT, occurrences=2),
        _decision("iron gate", "دروازهٔ آهنی"),
    ),
    (
        _candidate("rusted iron gate", classification=TerminologyType.ARTIFACT),
        _decision("rusted iron gate", "دروازهٔ آهنی زنگ‌زده"),
    ),
    # An open description built around a canonical name it already honors.
    (
        _candidate("Kandor", classification=TerminologyType.PLACE, occurrences=12),
        _decision("Kandor", "کندور"),
    ),
    (
        _candidate("broken Kandor", classification=TerminologyType.PLACE, occurrences=3),
        _decision("broken Kandor", "کندور شکسته"),
    ),
    # A compound that renders its canonical component in the right place with the wrong spelling.
    (
        _candidate("Kandor stone", classification=TerminologyType.ARTIFACT),
        _decision("Kandor stone", "سنگ کاندور"),
    ),
    # A compound reusing the ordinary word an elliptical shorthand elides: the shorthand's target
    # must never be substituted into it.
    (
        _candidate("Blade", classification=TerminologyType.ARTIFACT, occurrences=4),
        _decision("Blade", "تیغ"),
    ),
    (
        _candidate("Crimson Blade", classification=TerminologyType.ARTIFACT, occurrences=2),
        _decision("Crimson Blade", "تیغ سرخ"),
    ),
    # A containment contradiction between two names, which only a human can settle.
    (
        _candidate("Silver Crown", classification=TerminologyType.ARTIFACT, occurrences=4),
        _decision("Silver Crown", "تاج سیمین"),
    ),
    (
        _candidate("Silver Crown of Malden", classification=TerminologyType.ARTIFACT),
        _decision("Silver Crown of Malden", "دیهیم نقره‌ای مالدن"),
    ),
    # A name that is merely a fragment of a longer name it agrees with.
    (
        _candidate("Lord Aren", classification=TerminologyType.PERSON, occurrences=6),
        _decision("Lord Aren", "لرد آرن"),
    ),
    (
        _candidate("Lord Aren Vale", classification=TerminologyType.PERSON, occurrences=2),
        _decision("Lord Aren Vale", "لرد آرن ویل"),
    ),
    # A locked user decision the audit flags and remediation must never touch.
    (
        _candidate("Ajah", classification=TerminologyType.ORGANIZATION, occurrences=18),
        _decision("Ajah", "آجای", decision_source=DecisionSource.USER, locked=True),
    ),
    (
        _candidate("Ajah of the Tower", classification=TerminologyType.ORGANIZATION),
        _decision("Ajah of the Tower", "انجمن برج"),
    ),
    # A locked decision carrying a defect no plan may repair: a real finding with no lawful remedy.
    (
        _candidate("Malden fell", classification=TerminologyType.HISTORICAL_EVENT),
        _decision("Malden fell", "سقوط مالدن", decision_source=DecisionSource.USER, locked=True),
    ),
    # A decision no audit family reports at all.
    (
        _candidate("Nynaeve", classification=TerminologyType.PERSON, occurrences=7),
        _decision("Nynaeve", "ناینیو"),
    ),
)


def _registry(
    *extra: tuple[TerminologyCandidate, TerminologyDecision],
) -> TerminologyRegistry:
    pairs = (*_FIXTURE, *extra)
    registry = TerminologyRegistry(
        schema_version="1.0",
        registry_kind="terminology",
        version="1.0.0",
        book_id="synthetic-exactness-book",
        source_language="en",
        target_language="fa",
        candidates=[candidate for candidate, _ in pairs],
        decisions=[decision for _, decision in pairs],
    )
    # A persisted registry is always identity-ordered and canonical-view tie-breaking depends on
    # that order, so the fixture must match what load_registry would return.
    return TerminologyRegistry.model_validate_json(canonical_registry_bytes(registry))


def _minimal_registry() -> TerminologyRegistry:
    """A registry whose every audited finding has a deterministic remediation."""
    pairs = (
        (
            _candidate("Kaldor fell", classification=TerminologyType.HISTORICAL_EVENT),
            _decision("Kaldor fell", "سقوط کالدور"),
        ),
        (
            _candidate("Nynaeve", classification=TerminologyType.PERSON, occurrences=7),
            _decision("Nynaeve", "ناینیو"),
        ),
        (
            _candidate("Ajah", classification=TerminologyType.ORGANIZATION, occurrences=18),
            _decision("Ajah", "آجای", decision_source=DecisionSource.USER, locked=True),
        ),
    )
    registry = TerminologyRegistry(
        schema_version="1.0",
        registry_kind="terminology",
        version="1.0.0",
        book_id="synthetic-exactness-book",
        source_language="en",
        target_language="fa",
        candidates=[candidate for candidate, _ in pairs],
        decisions=[decision for _, decision in pairs],
    )
    return TerminologyRegistry.model_validate_json(canonical_registry_bytes(registry))


def _plan(
    registry: TerminologyRegistry | None = None, **overrides: object
) -> TerminologyRemediationPlan:
    registry = registry if registry is not None else _registry()
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


def _operation(plan: TerminologyRemediationPlan, source: str) -> TerminologyRemediationOperation:
    return next(
        item
        for item in plan.operations
        if item.source_term == source
        and item.finding_family is TerminologyFindingFamily.EXACT_ENFORCEMENT
    )


def _decision_of(registry: TerminologyRegistry, source: str) -> TerminologyDecision | None:
    candidate_id = candidate_identity(source, "en")
    return next((item for item in registry.decisions if item.candidate_id == candidate_id), None)


def _candidate_of(registry: TerminologyRegistry, source: str) -> TerminologyCandidate:
    candidate_id = candidate_identity(source, "en")
    return next(item for item in registry.candidates if item.candidate_id == candidate_id)


def test_fixture_reproduces_every_exactness_disposition() -> None:
    report = audit_registry_remediation(_registry(), EMPTY_GLOSSARY)

    assert report.exactness_finding_count > 0
    assert set(report.exactness_disposition_counts) == {
        "definitely_invalid",
        "definitely_valid",
        "context_review_required",
        "unresolved",
    }


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("Kaldor fell", "finite_clause_source_form"),
        ("stone", "generic_lexeme_contradicted_by_compounds"),
        ("Crimson", "elliptical_fragment_of_compound"),
        ("iron gate", "nested_descriptive_phrasings"),
        ("rusted iron gate", "nested_descriptive_phrasings"),
    ],
)
def test_high_confidence_defects_become_deterministic_retirements(source: str, rule: str) -> None:
    operation = _operation(_plan(), source)

    assert operation.exactness_rule == rule
    assert operation.exactness_disposition is TerminologyExactnessDisposition.DEFINITELY_INVALID
    assert operation.action is TerminologyRemediationAction.RETIRE_AND_REJECT_CANDIDATE
    assert operation.resolution is TerminologyRemediationResolution.DETERMINISTIC
    assert operation.mutates_registry is True
    assert operation.retirement is not None
    assert operation.retirement.reject_candidate is True


def test_plan_expresses_retire_and_reject_with_full_provenance() -> None:
    operation = _operation(_plan(), "stone")
    retirement = operation.retirement

    assert retirement is not None
    assert retirement.source_term == "stone"
    assert retirement.retired_target_term == "سنگ"
    assert retirement.locked is False
    assert retirement.decision_source == "ai"
    assert retirement.occurrence_count == 6
    assert retirement.candidate_status_before == "approved"
    assert retirement.candidate_status_after == "rejected"
    assert retirement.rejection_reason
    assert len(retirement.decision_record_sha256) == 64
    assert len(retirement.candidate_record_sha256) == 64


def test_overbroad_exact_enforcement_is_relaxed_without_touching_the_target() -> None:
    registry = _registry()
    operation = _operation(_plan(registry), "broken Kandor")

    assert operation.action is TerminologyRemediationAction.CHANGE_ENFORCEMENT
    assert operation.enforcement_change is not None
    assert operation.enforcement_change.enforcement_after is TerminologyEnforcement.CONTEXTUAL

    applied = apply_remediation_plan(registry, _plan(registry))
    decision = _decision_of(applied, "broken Kandor")

    assert decision is not None
    assert decision.enforcement is TerminologyEnforcement.CONTEXTUAL
    assert decision.target_term == "کندور شکسته"


def test_a_divergent_component_spelling_is_corrected_token_for_token() -> None:
    registry = _registry()
    operation = _operation(_plan(registry), "Kandor stone")
    correction = operation.target_correction

    assert operation.exactness_rule == "divergent_component_spelling"
    assert operation.action is TerminologyRemediationAction.CORRECT_TARGET
    assert operation.resolution is TerminologyRemediationResolution.DETERMINISTIC
    assert correction is not None
    assert correction.target_before == "سنگ کاندور"
    assert correction.target_after == "سنگ کندور"
    assert correction.component_source_term == "Kandor"
    assert correction.component_target_term == "کندور"
    assert correction.replaced_token == "کاندور"

    applied = apply_remediation_plan(registry, _plan(registry))
    decision = _decision_of(applied, "Kandor stone")

    assert decision is not None
    assert decision.target_term == "سنگ کندور"
    # Only the compound moves; the component that defines the spelling is untouched.
    assert _decision_of(applied, "Kandor") == _decision_of(registry, "Kandor")


def test_a_correction_settles_the_over_broad_claim_it_uncovers() -> None:
    """Respelling makes the compound honor its component for the first time, which is exactly when
    its exact claim becomes over-broad; the operation that causes that must also settle it."""

    registry = _registry()
    operation = _operation(_plan(registry), "Kandor stone")
    change = operation.enforcement_change

    assert operation.action is TerminologyRemediationAction.CORRECT_TARGET
    assert change is not None
    assert change.target_term == "سنگ کندور"
    assert change.enforcement_after is TerminologyEnforcement.CONTEXTUAL

    applied = apply_remediation_plan(registry, _plan(registry))
    decision = _decision_of(applied, "Kandor stone")

    assert decision is not None
    assert decision.target_term == "سنگ کندور"
    assert decision.enforcement is TerminologyEnforcement.CONTEXTUAL

    # A second plan against the applied registry has nothing left to do for this decision.
    assert not [item for item in _plan(applied).operations if item.source_term == "Kandor stone"]


def test_the_established_component_is_upheld_while_the_compound_is_respelled() -> None:
    plan = _plan()

    assert _operation(plan, "Kandor").action is TerminologyRemediationAction.NO_CHANGE_EXACT_UPHELD
    assert _operation(plan, "Kandor stone").action is TerminologyRemediationAction.CORRECT_TARGET


def test_an_elliptical_shorthand_never_drives_a_target_correction() -> None:
    """A shorthand's target spells a longer name; substituting it into a compound is nonsense."""

    registry = _registry()
    plan = _plan(registry)
    operation = _operation(plan, "Crimson Blade")

    assert operation.action is TerminologyRemediationAction.NO_CHANGE_EXACT_UPHELD
    assert operation.target_correction is None
    assert not [
        item
        for item in plan.operations
        if item.target_correction is not None and item.source_term == "Crimson Blade"
    ]
    assert _decision_of(apply_remediation_plan(registry, plan), "Crimson Blade") == _decision_of(
        registry, "Crimson Blade"
    )


def test_a_target_correction_must_replace_rather_than_nest_the_divergent_token() -> None:
    with pytest.raises(ValueError, match="replaced token must differ"):
        TerminologyTargetCorrection(
            decision_id="td-" + "0" * 24,
            candidate_id="tc-" + "0" * 24,
            source_term="Kandor stone",
            target_before="سنگ کاندور",
            target_after="سنگ کاندور کندور",
            component_source_term="Kandor",
            component_decision_id="td-" + "1" * 24,
            component_target_term="کاندور",
            replaced_token="کاندور",
            decision_record_sha256="a" * 64,
            reason="synthetic",
        )


def test_a_target_correction_must_be_exactly_the_recorded_substitution() -> None:
    with pytest.raises(ValueError, match="exactly the replaced token"):
        TerminologyTargetCorrection(
            decision_id="td-" + "0" * 24,
            candidate_id="tc-" + "0" * 24,
            source_term="Kandor stone",
            target_before="سنگ کاندور",
            target_after="سنگ کندور بزرگ",
            component_source_term="Kandor",
            component_decision_id="td-" + "1" * 24,
            component_target_term="کندور",
            replaced_token="کاندور",
            decision_record_sha256="a" * 64,
            reason="synthetic",
        )


def test_a_target_correction_whose_record_drifted_is_refused() -> None:
    registry = _registry()
    plan = _plan(registry)
    tampered = plan.model_copy(
        update={
            "operations": [
                item.model_copy(
                    update={
                        "target_correction": item.target_correction.model_copy(
                            update={"decision_record_sha256": "c" * 64}
                        )
                    }
                )
                if item.target_correction is not None
                else item
                for item in plan.operations
            ]
        }
    )

    with pytest.raises(TerminologyRemediationPlanError, match="drifted"):
        apply_remediation_plan(registry, tampered)


def test_uncertain_finding_becomes_a_question_rather_than_a_mutation() -> None:
    registry = _registry()
    operation = _operation(_plan(registry), "Silver Crown")

    assert operation.exactness_disposition is (
        TerminologyExactnessDisposition.CONTEXT_REVIEW_REQUIRED
    )
    assert operation.action is TerminologyRemediationAction.OPEN_TERMINOLOGY_QUESTION
    assert operation.mutates_registry is False
    assert operation.question_request is not None
    assert operation.question_request.category is QuestionCategory.WORLD_TERM_SEMANTICS
    assert "Silver Crown of Malden" in operation.question_request.related_source_terms

    applied = apply_remediation_plan(registry, _plan(registry))
    before = _decision_of(registry, "Silver Crown")
    after = _decision_of(applied, "Silver Crown")

    assert after is not None
    assert after == before


def test_a_name_that_only_repeats_structure_keeps_its_exact_target() -> None:
    registry = _registry()
    operation = _operation(_plan(registry), "Lord Aren")

    assert operation.exactness_disposition is TerminologyExactnessDisposition.DEFINITELY_VALID
    assert operation.action is TerminologyRemediationAction.NO_CHANGE_EXACT_UPHELD
    assert operation.mutates_registry is False

    applied = apply_remediation_plan(registry, _plan(registry))

    assert _decision_of(applied, "Lord Aren") == _decision_of(registry, "Lord Aren")


def test_unflagged_canonical_terms_are_untouched_by_remediation() -> None:
    registry = _registry()
    plan = _plan(registry)
    applied = apply_remediation_plan(registry, plan)

    assert not [item for item in plan.operations if item.source_term == "Nynaeve"]
    assert _decision_of(applied, "Nynaeve") == _decision_of(registry, "Nynaeve")
    assert _candidate_of(applied, "Nynaeve") == _candidate_of(registry, "Nynaeve")


def test_locked_user_decision_is_reported_but_never_retired() -> None:
    registry = _registry()
    operation = _operation(_plan(registry), "Ajah")

    assert operation.remediation_category is TerminologyRemediationCategory.OWNER_GOVERNED_DECISION
    assert operation.action is TerminologyRemediationAction.NO_CHANGE_OWNER_AUTHORITY
    assert operation.mutates_registry is False
    assert operation.retirement is None

    applied = apply_remediation_plan(registry, _plan(registry))
    decision = _decision_of(applied, "Ajah")

    assert decision == _decision_of(registry, "Ajah")
    assert decision is not None
    assert decision.locked is True
    assert decision.decision_source is DecisionSource.USER
    assert decision.target_term == "آجای"


def test_user_owned_decision_cannot_be_silently_retired() -> None:
    owned = (
        _candidate("Kaldor rose", classification=TerminologyType.HISTORICAL_EVENT),
        _decision("Kaldor rose", "برآمدن کالدور", decision_source=DecisionSource.USER),
    )
    registry = _registry(owned)
    operation = _operation(_plan(registry), "Kaldor rose")

    assert operation.action is TerminologyRemediationAction.NO_CHANGE_OWNER_AUTHORITY
    assert operation.retirement is None
    applied = apply_remediation_plan(registry, _plan(registry))
    assert _decision_of(applied, "Kaldor rose") is not None


def test_a_retirement_record_can_never_describe_a_locked_decision() -> None:
    with pytest.raises(ValueError, match="Input should be False"):
        TerminologyDecisionRetirement(
            decision_id="td-" + "0" * 24,
            candidate_id="tc-" + "0" * 24,
            source_term="stone",
            retired_target_term="سنگ",
            resolved_enforcement=TerminologyEnforcement.EXACT,
            decision_source="ai",
            decision_state="confirmed",
            locked=True,  # type: ignore[arg-type]
            occurrence_count=1,
            reject_candidate=False,
            candidate_status_before="approved",
            candidate_status_after="approved",
            decision_record_sha256="a" * 64,
            candidate_record_sha256="b" * 64,
            reason="synthetic",
        )


def test_a_user_owned_retirement_requires_recorded_authorization() -> None:
    with pytest.raises(ValueError, match="explicit recorded authorization"):
        TerminologyDecisionRetirement(
            decision_id="td-" + "0" * 24,
            candidate_id="tc-" + "0" * 24,
            source_term="stone",
            retired_target_term="سنگ",
            resolved_enforcement=TerminologyEnforcement.EXACT,
            decision_source="user",
            decision_state="confirmed",
            occurrence_count=1,
            reject_candidate=False,
            candidate_status_before="approved",
            candidate_status_after="approved",
            decision_record_sha256="a" * 64,
            candidate_record_sha256="b" * 64,
            reason="synthetic",
        )


def test_planner_refuses_a_retirement_aimed_at_an_owner_governed_decision() -> None:
    """The planner proves the ownership rule itself rather than trusting the classifier."""

    registry = _registry()
    report = audit_registry_remediation(registry, EMPTY_GLOSSARY)
    ajah = candidate_identity("Ajah", "en")
    forced = report.model_copy(
        update={
            "findings": [
                item.model_copy(
                    update={
                        "proposed_action": (
                            TerminologyRemediationAction.RETIRE_AND_REJECT_CANDIDATE
                        ),
                        "remediation_category": (
                            TerminologyRemediationCategory.NON_TERMINOLOGY_SOURCE_FORM
                        ),
                    }
                )
                if item.candidate_id == ajah
                and item.finding_family is TerminologyFindingFamily.EXACT_ENFORCEMENT
                else item
                for item in report.findings
            ]
        }
    )
    plan = plan_registry_remediation(
        registry,
        EMPTY_GLOSSARY,
        forced,
        registry_raw_sha256="a" * 64,
        glossary_sha256="b" * 64,
        created_at=FIXED_TIME,
    )
    operation = _operation(plan, "Ajah")

    assert operation.action is TerminologyRemediationAction.NO_CHANGE_OWNER_AUTHORITY
    assert operation.resolution is TerminologyRemediationResolution.UNRESOLVED
    assert operation.blocking_evidence == ["owner_governed_decision"]
    assert plan.applicable is False


def test_an_already_rejected_candidate_is_not_rejected_twice() -> None:
    """A candidate the registry already calls non-terminology keeps its record; only the decision
    is withdrawn, which is why retirement and rejection are separate actions."""

    pairs = (
        (
            _candidate(
                "Kaldor fell",
                classification=TerminologyType.HISTORICAL_EVENT,
                status=CandidateStatus.REJECTED,
            ),
            _decision("Kaldor fell", "سقوط کالدور"),
        ),
        (
            _candidate("Nynaeve", classification=TerminologyType.PERSON, occurrences=7),
            _decision("Nynaeve", "ناینیو"),
        ),
    )
    registry = TerminologyRegistry.model_validate_json(
        canonical_registry_bytes(
            TerminologyRegistry(
                schema_version="1.0",
                registry_kind="terminology",
                version="1.0.0",
                book_id="synthetic-exactness-book",
                source_language="en",
                target_language="fa",
                candidates=[candidate for candidate, _ in pairs],
                decisions=[decision for _, decision in pairs],
            )
        )
    )
    plan = _plan(registry)
    operation = _operation(plan, "Kaldor fell")

    assert operation.action is TerminologyRemediationAction.RETIRE_DECISION
    assert operation.retirement is not None
    assert operation.retirement.reject_candidate is False
    assert plan.candidate_rejection_count == 0

    applied = apply_remediation_plan(registry, plan)

    assert _decision_of(applied, "Kaldor fell") is None
    assert _candidate_of(applied, "Kaldor fell") == _candidate_of(registry, "Kaldor fell")


def test_candidate_rejection_is_atomic_with_decision_retirement() -> None:
    registry = _registry()
    applied = apply_remediation_plan(registry, _plan(registry))
    candidate = _candidate_of(applied, "stone")

    assert _decision_of(applied, "stone") is None
    assert candidate.status is CandidateStatus.REJECTED
    assert any("generic_lexeme" in note for note in candidate.evidence_notes)
    assert candidate.occurrences == _candidate_of(registry, "stone").occurrences


def test_retirement_withdraws_the_lookup_as_an_intended_correction(tmp_path: Path) -> None:
    path = tmp_path / "terminology.yaml"
    save_registry(path, _registry())
    plan = _plan(
        load_registry(path), registry_raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )

    simulation = simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)

    assert simulation.lookup_regression_count == 0
    assert simulation.decisions_retired == plan.retirement_count
    assert simulation.candidates_rejected == plan.candidate_rejection_count
    assert simulation.retired_source_forms_resolved_after["stone"] is None
    assert simulation.retired_source_forms_resolved_after["Crimson"] is None


def test_dry_run_leaves_the_registry_byte_identical(tmp_path: Path) -> None:
    path = tmp_path / "terminology.yaml"
    save_registry(path, _registry())
    before = {item: item.read_bytes() for item in sorted(tmp_path.rglob("*")) if item.is_file()}
    plan = _plan(
        load_registry(path), registry_raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )

    simulation = simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)
    after = {item: item.read_bytes() for item in sorted(tmp_path.rglob("*")) if item.is_file()}

    assert simulation.real_registry_unchanged is True
    assert after == before


def test_the_decision_survives_the_dry_run_and_dies_only_on_explicit_apply(
    tmp_path: Path,
) -> None:
    path = tmp_path / "terminology.yaml"
    save_registry(path, _minimal_registry())
    plan = _plan(
        load_registry(path), registry_raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )
    simulation = simulate_remediation_plan(path, EMPTY_GLOSSARY, plan, created_at=FIXED_TIME)

    assert simulation.safe_to_apply is True
    assert simulation.decisions_retired == 1
    assert _decision_of(load_registry(path), "Kaldor fell") is not None

    execute_remediation_plan(path, plan, simulation, evidence_directory=tmp_path / "evidence")
    applied = load_registry(path)

    assert _decision_of(applied, "Kaldor fell") is None
    assert _candidate_of(applied, "Kaldor fell").status is CandidateStatus.REJECTED
    assert _decision_of(applied, "Ajah") == _decision_of(_minimal_registry(), "Ajah")
    assert registry_identity(applied) == plan.expected_semantic_sha256
    assert (tmp_path / "evidence" / "plan.json").is_file()


def test_a_stale_plan_refuses_to_apply() -> None:
    registry = _registry()
    plan = _plan(registry)
    drifted = registry.model_copy(
        update={
            "decisions": [
                item.model_copy(update={"target_term": "سنگی"})
                if item.candidate_id == candidate_identity("stone", "en")
                else item
                for item in registry.decisions
            ]
        }
    )

    with pytest.raises(TerminologyRemediationPlanError, match="drifted"):
        apply_remediation_plan(drifted, plan)


def test_a_retirement_whose_record_drifted_is_refused() -> None:
    """The registry-wide hash and the per-record hash are independent guards on purpose."""

    registry = _registry()
    plan = _plan(registry)
    tampered = plan.model_copy(
        update={
            "operations": [
                item.model_copy(
                    update={
                        "retirement": item.retirement.model_copy(
                            update={"decision_record_sha256": "c" * 64}
                        )
                    }
                )
                if item.retirement is not None and item.source_term == "stone"
                else item
                for item in plan.operations
            ]
        }
    )

    with pytest.raises(TerminologyRemediationPlanError, match="drifted"):
        apply_remediation_plan(registry, tampered)
    assert _decision_of(registry, "stone") is not None


def test_reapplying_a_retirement_aborts_instead_of_double_applying() -> None:
    registry = _registry()
    plan = _plan(registry)
    applied = apply_remediation_plan(registry, plan)

    with pytest.raises(TerminologyRemediationPlanError, match="drifted"):
        apply_remediation_plan(applied, plan)


def test_planning_is_deterministic_for_identical_registry_state() -> None:
    first, second = _plan(), _plan()

    assert first.plan_id == second.plan_id
    assert first.expected_semantic_sha256 == second.expected_semantic_sha256
    assert [item.operation_id for item in first.operations] == [
        item.operation_id for item in second.operations
    ]


def test_every_finding_has_exactly_one_primary_operation() -> None:
    registry = _registry()
    plan = _plan(registry)
    report = audit_registry_remediation(registry, EMPTY_GLOSSARY)

    assert plan.operation_count == report.findings_total
    assert sorted(item.finding_id for item in plan.operations) == sorted(
        item.finding_id for item in report.findings
    )
    assert plan.exactness_operation_count == report.exactness_finding_count


def test_a_second_plan_after_apply_has_nothing_left_to_retire() -> None:
    registry = _registry()
    applied = apply_remediation_plan(registry, _plan(registry))

    assert _plan(applied).retirement_count == 0


PILOT_RESOURCES = (
    ROOT / "config/terminology.yaml",
    ROOT / "config/glossary.yaml",
    ROOT / "workspace/resources/terminology",
)
requires_pilot_corpus = pytest.mark.skipif(
    not all(path.exists() for path in PILOT_RESOURCES),
    reason="the private pilot corpus is not present in this checkout",
)
FROZEN_RUN = ROOT / "workspace/translation-runs/tr-bc759046a164-001"
APPLIED_REMEDIATION_PLAN_ID = "trp-be33e0bf18466d1c124ed920"
# The six defects the exact-enforcement audit proves from the frozen registry alone.  Pinning the
# set is the point: a rule that grows more permissive must fail here before it can retire a name.
EXPECTED_REAL_RETIREMENTS = {
    "Aldeshar fell",
    "Red",
    "redstone doorframe",
    "ring",
    "took the Stone",
    "twisted redstone doorframe",
}


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        item.relative_to(root).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


@requires_pilot_corpus
def test_real_registry_remediation_converged_on_exactly_the_provable_defects() -> None:
    """The end-to-end guard on the authorized pilot registry, planning only and never applying.

    The governed remediation has been applied, so the invariant has two halves and both must hold.
    The published plan is what the rule proved against the registry state it judged, and re-planning
    against the live registry must now propose nothing: a rule that grew more permissive would find
    fresh retirements here, and a rule that stopped working would fail to reproduce the pinned set.
    """

    import yaml

    registry_path = ROOT / "config/terminology.yaml"
    registry_before = registry_path.read_bytes()
    frozen_before = _tree_hashes(ROOT / "workspace/resources/terminology")
    run_before = _tree_hashes(FROZEN_RUN) if FROZEN_RUN.is_dir() else {}
    glossary = yaml.safe_load((ROOT / "config/glossary.yaml").read_text(encoding="utf-8"))
    registry = load_registry(registry_path)
    applied = next(
        item
        for item in load_applied_remediations(ROOT)
        if item.plan_id == APPLIED_REMEDIATION_PLAN_ID
    )
    plan = plan_registry_remediation(
        registry,
        glossary,
        audit_registry_remediation(registry, glossary),
        registry_raw_sha256=hashlib.sha256(registry_before).hexdigest(),
        glossary_sha256="b" * 64,
        created_at=FIXED_TIME,
    )

    # What the rule proved, recorded immutably at the state it judged.
    assert {
        item.retirement.source_term for item in applied.operations if item.retirement is not None
    } == EXPECTED_REAL_RETIREMENTS
    assert {
        item.enforcement_change.source_term
        for item in applied.operations
        if item.enforcement_change is not None
    } >= {"doorframe ter'angreal", "doorway ter'angreal"}
    assert applied.applicable is True

    # What the live registry now needs: nothing. The remediation converged in one round.
    assert [item for item in plan.operations if item.retirement is not None] == []
    assert plan.retirement_count == 0
    assert plan.mutating_operation_count == 0
    assert plan.unresolved_operation_count == 0

    # The corrected Lord Gareth decision is upheld, not re-corrected.
    gareth = next(item for item in plan.operations if item.source_term == "Lord Gareth")
    assert gareth.action is TerminologyRemediationAction.NO_CHANGE_EXACT_UPHELD
    assert _decision_of(registry, "Lord Gareth") is not None
    assert _decision_of(registry, "Lord Gareth").target_term == "لرد گرث"  # type: ignore[union-attr]

    # The locked user decision was out of scope for every operation, then and now.
    ajah = _decision_of(registry, "Ajah")
    assert ajah is not None
    assert ajah.locked is True and ajah.target_term == "آجای"
    for candidate_plan in (applied, plan):
        assert ajah.decision_id not in {
            item.decision_id
            for item in candidate_plan.operations
            if item.retirement is not None or item.enforcement_change is not None
        }

    # Planning is read-only against every governed artifact.
    assert registry_path.read_bytes() == registry_before
    assert _tree_hashes(ROOT / "workspace/resources/terminology") == frozen_before
    assert (_tree_hashes(FROZEN_RUN) if FROZEN_RUN.is_dir() else {}) == run_before


def test_published_remediation_plans_remain_readable_and_self_consistent() -> None:
    """Every published plan is application evidence, so its own counts must still add up.

    Pinning one plan shape here was the original mistake: a second, differently shaped plan is
    exactly what a working remediation workflow produces over time.
    """

    stored = sorted(ROOT.glob("workspace/terminology-remediation/trp-*/plan.json"))
    if not stored:
        pytest.skip("no remediation plan is published in this checkout")
    for path in stored:
        plan = TerminologyRemediationPlan.model_validate_json(path.read_text(encoding="utf-8"))

        assert plan.plan_id == path.parent.name
        assert (path.parent / "dry-run.json").is_file()
        assert plan.operation_count == len(plan.operations)
        assert plan.retirement_count == sum(
            1 for item in plan.operations if item.retirement is not None
        )
        assert plan.enforcement_change_count == sum(
            1 for item in plan.operations if item.enforcement_change is not None
        )
        assert plan.target_correction_count == sum(
            1 for item in plan.operations if item.target_correction is not None
        )
        assert plan.expected_semantic_sha256 is not None
        assert {item.finding_family for item in plan.operations} <= {
            TerminologyFindingFamily.CANONICAL_VALIDATOR,
            TerminologyFindingFamily.EXACT_ENFORCEMENT,
        }
