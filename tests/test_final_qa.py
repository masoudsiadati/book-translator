from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.final_qa import (
    MAX_ESCALATED_REPAIR_ATTEMPTS,
    CompetingVariantAnalysis,
    FinalQAAssessment,
    FinalQAError,
    analyze_competing_variants,
    assess_final_qa,
    diagnose_paragraphs,
    execute_final_qa_audit,
    finalize_translation_run,
    operator_required_terminology,
    plan_final_qa_audit,
)
from book_translator.final_qa.policy import FINAL_QA_RESOLUTION_POLICY_VERSION
from book_translator.final_qa.workflow import _Options, _repair
from book_translator.models import (
    CompetingVariantClass,
    DecisionSource,
    DecisionState,
    FinalQAVerdict,
    ProviderName,
    SnapshotResourceKind,
    StructuralDefectClass,
    TerminologyEnforcement,
    TerminologyFinding,
    TerminologyRegistry,
    TerminologyType,
    TerminologyValidationReport,
    TranslationAcceptanceEvidence,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationJob,
    TranslationJobStatus,
    TranslationQAFinding,
    TranslationQAReport,
    TranslationRepairEscalation,
    TranslationRunStatus,
)
from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
from book_translator.terminology import load_registry, save_registry, set_decision
from book_translator.terminology.lexical import (
    carries_canonical_identity,
    enforceable_competing_variants,
)
from book_translator.terminology.validation import (
    STRUCTURAL_QA_VERSION,
    TERMINOLOGY_QA_VERSION,
    paragraph_count,
    validate_terminology,
    validate_translation_qa,
)
from book_translator.translation_queue import create_run, load_run_summary, plan_run
from book_translator.translation_repair_policy import RepairBudget
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import run_translation_worker
from tests.test_translation_auto_repair import ScriptedProvider
from tests.test_translation_queue import NOW, SNAPSHOT_NOW
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import FailingProvider, _options, _queue_bytes, _run_path

runner = CliRunner()

# The synthetic source is ``Alpha beta.\n\nGamma delta.\n``: two paragraphs, one terminology
# candidate.  Persian targets below are long enough for the derivational stemmer's minimum stem,
# which is why they are not two-letter placeholders.
CANONICAL = "نگهبان"
CONTAINING_VARIANT = "نگهبان بزرگ"
DERIVED_VARIANT = "نگهبانان"
RIVAL_VARIANT = "پاسدارها"

CANONICAL_TRANSLATION = f"{CANONICAL} در بند نخست.\n\nبند دوم.\n"
CONTAINING_TRANSLATION = f"{CONTAINING_VARIANT} در بند نخست.\n\nبند دوم.\n"
DERIVED_TRANSLATION = f"{CANONICAL} و {DERIVED_VARIANT} در بند نخست.\n\nبند دوم.\n"
RIVAL_ONLY_TRANSLATION = f"{RIVAL_VARIANT} در بند نخست.\n\nبند دوم.\n"
# The rival stands in the block that translates the governed source occurrence and the canonical
# stands in the block that does not, which is what makes this a *rendering* of that occurrence
# rather than the two strings merely sharing a chunk.  Since terminology QA 1.6.0 that difference
# is the finding: co-occurrence anywhere in the chunk is not evidence about any occurrence.
RIVAL_AND_CANONICAL_TRANSLATION = f"{RIVAL_VARIANT} در بند نخست.\n\n{CANONICAL} در بند دوم.\n"
ONE_PARAGRAPH = "تنها یک بند.\n"


def _registry(
    *,
    variants: tuple[str, ...] = (),
    decision_source: DecisionSource = DecisionSource.AI,
    locked: bool = False,
    decision_state: DecisionState = DecisionState.CONFIRMED,
    rival: tuple[str, str, DecisionState, DecisionSource, bool] | None = None,
) -> TerminologyRegistry:
    """Build the smallest registry that can produce a ``competing_variant`` finding."""
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )
    registry = set_decision(
        registry,
        source_term="Alpha",
        target_term=CANONICAL,
        classification=TerminologyType.STORY_TERM,
        decision_source=decision_source,
        locked=locked,
        decision_state=decision_state,
        competing_variants=variants,
    )
    if rival is not None:
        source_term, target_term, state, source, rival_locked = rival
        registry = set_decision(
            registry,
            source_term=source_term,
            target_term=target_term,
            classification=TerminologyType.STORY_TERM,
            decision_source=source,
            locked=rival_locked,
            decision_state=state,
        )
    return registry


SOURCE = "Alpha beta.\n\nGamma delta.\n"


def _analyses(
    registry: TerminologyRegistry,
    translation: str,
    *,
    effective: TerminologyRegistry | None = None,
    historical: dict[str, tuple[str, ...]] | None = None,
) -> tuple[CompetingVariantAnalysis, ...]:
    return analyze_competing_variants(
        source_text=SOURCE,
        translation_text=translation,
        frozen_registry=registry,
        effective_registry=effective,
        historical_variants=historical,
    )


def _assess(
    registry: TerminologyRegistry,
    translation: str,
    *,
    effective: TerminologyRegistry | None = None,
    historical: dict[str, tuple[str, ...]] | None = None,
    budget: RepairBudget | None = None,
) -> FinalQAAssessment:
    return assess_final_qa(
        terminology_report=validate_terminology(SOURCE, translation, registry),
        structural_report=validate_translation_qa(SOURCE, translation, target_language="fa"),
        competing_variants=_analyses(
            registry, translation, effective=effective, historical=historical
        ),
        budget=budget or RepairBudget(2, 0, 3, 1),
    )


# --------------------------------------------------------------------------------------------
# Validator semantics: a variant carrying the canonical's own identity is not a rival
# --------------------------------------------------------------------------------------------


def test_a_variant_containing_or_deriving_from_the_canonical_is_not_enforceable() -> None:
    assert carries_canonical_identity(CANONICAL, CONTAINING_VARIANT, target_language="fa")
    assert carries_canonical_identity(CANONICAL, DERIVED_VARIANT, target_language="fa")
    assert not carries_canonical_identity(CANONICAL, RIVAL_VARIANT, target_language="fa")
    assert enforceable_competing_variants(
        CANONICAL, (CONTAINING_VARIANT, DERIVED_VARIANT, RIVAL_VARIANT), target_language="fa"
    ) == (RIVAL_VARIANT,)


def test_harmless_morphological_variant_raises_no_conflict() -> None:
    registry = _registry(variants=(DERIVED_VARIANT,))
    report = validate_terminology(SOURCE, DERIVED_TRANSLATION, registry)
    assert report.valid
    assert [finding.code for finding in report.findings] == []


def test_relaxing_the_variant_rule_can_only_remove_findings() -> None:
    """The correction is monotone, so nothing that passed QA before can start failing."""
    registry = _registry(variants=(CONTAINING_VARIANT, DERIVED_VARIANT, RIVAL_VARIANT))
    for translation in (
        CANONICAL_TRANSLATION,
        CONTAINING_TRANSLATION,
        DERIVED_TRANSLATION,
        RIVAL_AND_CANONICAL_TRANSLATION,
    ):
        relaxed = {
            (finding.code, finding.candidate_id)
            for finding in validate_terminology(SOURCE, translation, registry).findings
        }
        strict = relaxed | {
            ("competing_variant", item.candidate_id)
            for item in registry.candidates
            for variant in (CONTAINING_VARIANT, DERIVED_VARIANT)
            if variant in translation
        }
        assert relaxed <= strict


# --------------------------------------------------------------------------------------------
# Competing-variant classification
# --------------------------------------------------------------------------------------------


def test_canonical_identity_variant_is_classified_and_never_fires() -> None:
    registry = _registry(variants=(CONTAINING_VARIANT,))
    (analysis,) = _analyses(registry, CONTAINING_TRANSLATION)
    assert analysis.variant_class is CompetingVariantClass.CANONICAL_IDENTITY
    assert not analysis.enforceable
    assert validate_terminology(SOURCE, CONTAINING_TRANSLATION, registry).valid


def test_retired_competitor_is_resolved_without_a_provider() -> None:
    frozen = _registry(variants=(RIVAL_VARIANT,))
    effective = _registry()
    (analysis,) = _analyses(frozen, RIVAL_AND_CANONICAL_TRANSLATION, effective=effective)
    assert analysis.variant_class is CompetingVariantClass.RETIRED_COMPETITOR
    assert not analysis.recorded_in_effective_registry
    assessment = _assess(frozen, RIVAL_AND_CANONICAL_TRANSLATION, effective=effective)
    assert assessment.verdict is FinalQAVerdict.RESOLVE_TERMINOLOGY_THEN_REEVALUATE
    assert not assessment.actionable


def test_historical_output_only_competitor_is_told_apart_from_a_retired_one() -> None:
    frozen = _registry(variants=(RIVAL_VARIANT,))
    effective = _registry()
    candidate_id = frozen.candidates[0].candidate_id
    (analysis,) = _analyses(
        frozen,
        RIVAL_AND_CANONICAL_TRANSLATION,
        effective=effective,
        historical={candidate_id: (RIVAL_VARIANT,)},
    )
    assert analysis.variant_class is CompetingVariantClass.HISTORICAL_OUTPUT_ONLY
    assessment = _assess(
        frozen,
        RIVAL_AND_CANONICAL_TRANSLATION,
        effective=effective,
        historical={candidate_id: (RIVAL_VARIANT,)},
    )
    assert assessment.verdict is FinalQAVerdict.RESOLVE_TERMINOLOGY_THEN_REEVALUATE


def test_a_missing_canonical_beside_a_rival_is_a_translation_defect() -> None:
    registry = _registry(variants=(RIVAL_VARIANT,))
    (analysis,) = _analyses(registry, RIVAL_ONLY_TRANSLATION)
    assert analysis.variant_class is CompetingVariantClass.CANONICAL_ABSENT
    assert not analysis.canonical_present
    assessment = _assess(registry, RIVAL_ONLY_TRANSLATION)
    assert assessment.verdict is FinalQAVerdict.REPAIR_TRANSLATION
    assert assessment.actionable


def test_a_locked_canonical_conflict_never_touches_the_locked_decision() -> None:
    frozen = _registry(
        variants=(RIVAL_VARIANT,),
        rival=("Omega", RIVAL_VARIANT, DecisionState.CONFIRMED, DecisionSource.USER, True),
    )
    (analysis,) = _analyses(frozen, RIVAL_AND_CANONICAL_TRANSLATION)
    assert analysis.variant_class is CompetingVariantClass.LOCKED_CANONICAL_CONFLICT
    assert analysis.rival_locked
    assessment = _assess(frozen, RIVAL_AND_CANONICAL_TRANSLATION)
    assert assessment.verdict is FinalQAVerdict.RESOLVE_TERMINOLOGY_THEN_REPAIR
    assert assessment.locked_terminology_involved
    # The locked decision is registry state, and no verdict the policy can reach edits it.
    rival = next(item for item in frozen.decisions if item.target_term == RIVAL_VARIANT)
    assert rival.locked and rival.target_term == RIVAL_VARIANT


def test_one_settled_decision_against_a_provisional_rival_has_a_deterministic_winner() -> None:
    frozen = _registry(
        variants=(RIVAL_VARIANT,),
        rival=("Omega", RIVAL_VARIANT, DecisionState.PROVISIONAL, DecisionSource.AI, False),
    )
    (analysis,) = _analyses(frozen, RIVAL_AND_CANONICAL_TRANSLATION)
    assert analysis.variant_class is CompetingVariantClass.RIVAL_AI_DECISIONS
    assessment = _assess(frozen, RIVAL_AND_CANONICAL_TRANSLATION)
    assert assessment.verdict is FinalQAVerdict.RESOLVE_TERMINOLOGY_THEN_REEVALUATE
    assert not assessment.locked_terminology_involved


def test_two_settled_canonical_targets_require_an_operator() -> None:
    frozen = _registry(
        variants=(RIVAL_VARIANT,),
        rival=("Omega", RIVAL_VARIANT, DecisionState.CONFIRMED, DecisionSource.AI, False),
    )
    (analysis,) = _analyses(frozen, RIVAL_AND_CANONICAL_TRANSLATION)
    assert analysis.variant_class is CompetingVariantClass.GENUINE_CANONICAL_CONFLICT
    assert _assess(frozen, RIVAL_AND_CANONICAL_TRANSLATION).verdict is (
        FinalQAVerdict.OPERATOR_REQUIRED
    )


def test_an_unexplained_competitor_fails_closed() -> None:
    registry = _registry(variants=(RIVAL_VARIANT,))
    (analysis,) = _analyses(registry, RIVAL_AND_CANONICAL_TRANSLATION)
    assert analysis.variant_class is CompetingVariantClass.INSUFFICIENT_EVIDENCE
    assert analysis.canonical_present and analysis.variant_present
    assessment = _assess(registry, RIVAL_AND_CANONICAL_TRANSLATION)
    assert assessment.verdict is FinalQAVerdict.OPERATOR_REQUIRED
    assert not assessment.actionable


def test_a_finding_the_analyzer_cannot_account_for_fails_closed() -> None:
    registry = _registry(variants=(RIVAL_VARIANT,))
    report = TerminologyValidationReport(
        schema_version="1.0",
        valid=False,
        source_sha256="0" * 64,
        translation_sha256="1" * 64,
        checked_decisions=1,
        findings=[
            TerminologyFinding(
                code="competing_variant",
                severity="error",
                candidate_id="tc-not-analyzed",
                source_term="Ghost",
                enforcement=TerminologyEnforcement.EXACT,
                message="synthetic",
            )
        ],
    )
    assessment = assess_final_qa(
        terminology_report=report,
        structural_report=validate_translation_qa(
            SOURCE, CANONICAL_TRANSLATION, target_language="fa"
        ),
        competing_variants=_analyses(registry, CANONICAL_TRANSLATION),
        budget=RepairBudget(2, 0, 3, 1),
    )
    assert assessment.verdict is FinalQAVerdict.OPERATOR_REQUIRED
    assert any("unanalyzable" in reason for reason in assessment.reasons)


def test_a_rejection_current_qa_no_longer_reproduces_is_reevaluated() -> None:
    registry = _registry(variants=(DERIVED_VARIANT,))
    assessment = _assess(registry, DERIVED_TRANSLATION)
    assert assessment.verdict is FinalQAVerdict.REEVALUATE
    assert assessment.accepts_without_provider
    assert assessment.policy_version == FINAL_QA_RESOLUTION_POLICY_VERSION


# --------------------------------------------------------------------------------------------
# Structural diagnosis
# --------------------------------------------------------------------------------------------


def test_a_blank_line_run_is_typography_and_not_a_paragraph() -> None:
    assert paragraph_count("a\n\n\n\nb\n") == 2
    assert paragraph_count("a\n\n   \n\nb\n") == 2
    assert paragraph_count("") == 0
    diagnosis = diagnose_paragraphs(SOURCE, "یک.\n\n\n\nدو.\n")
    assert diagnosis.blank_line_artifact
    assert diagnosis.matched
    # A blank-line run is a typography artefact that segmentation already absorbs, so the two
    # sides correspond and there is nothing for a structural repair to do.
    assert diagnosis.defect_class is StructuralDefectClass.NONE
    assert not diagnosis.deterministic
    assert validate_translation_qa(SOURCE, "یک.\n\n\n\nدو.\n", target_language="fa").valid


def test_a_content_paragraph_mismatch_is_not_deterministically_normalizable() -> None:
    diagnosis = diagnose_paragraphs(SOURCE, ONE_PARAGRAPH)
    assert not diagnosis.blank_line_artifact
    assert diagnosis.defect_class is StructuralDefectClass.TRANSLATION_LINE_MERGE
    assert not diagnosis.deterministic
    assert diagnosis.paragraph_delta == -1
    assessment = assess_final_qa(
        terminology_report=validate_terminology(SOURCE, ONE_PARAGRAPH, _registry()),
        structural_report=validate_translation_qa(SOURCE, ONE_PARAGRAPH, target_language="fa"),
        competing_variants=(),
        budget=RepairBudget(2, 0, 3, 1),
    )
    assert assessment.verdict is FinalQAVerdict.REPAIR_TRANSLATION


def test_an_exhausted_budget_behind_a_repairable_defect_is_exhausted_not_repairable() -> None:
    assessment = assess_final_qa(
        terminology_report=validate_terminology(SOURCE, ONE_PARAGRAPH, _registry()),
        structural_report=validate_translation_qa(SOURCE, ONE_PARAGRAPH, target_language="fa"),
        competing_variants=(),
        budget=RepairBudget(2, 0, 3, 3),
        escalation_available=True,
    )
    assert assessment.verdict is FinalQAVerdict.EXHAUSTED
    assert any("escalation_available" in reason for reason in assessment.reasons)
    assert not assessment.actionable


def test_the_worst_verdict_wins_when_a_job_carries_several_blockers() -> None:
    registry = _registry(variants=(RIVAL_VARIANT,))
    translation = f"{CANONICAL} و {RIVAL_VARIANT}.\n"
    assessment = _assess(registry, translation)
    assert set(assessment.blocking_codes) == {"competing_variant", "paragraph_count_mismatch"}
    assert assessment.verdict is FinalQAVerdict.OPERATOR_REQUIRED


# --------------------------------------------------------------------------------------------
# End-to-end against a real queue
# --------------------------------------------------------------------------------------------


def _prepared(root: Path, registry: TerminologyRegistry, *, max_attempts: int = 3) -> str:
    root.mkdir(parents=True, exist_ok=True)
    synthetic_project(root)
    save_registry(root / "config/terminology.yaml", registry)
    (root / "config/terminology.metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "resource_kind": SnapshotResourceKind.TERMINOLOGY.value,
                "resource_file": "config/terminology.yaml",
                "version": "1.0.0",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    created = create_run(
        plan_run(snapshot.snapshot_id, project_root=root, now=lambda: NOW),
    )
    execute_prompt_packages(
        plan_prompt_packages(created.run_id, project_root=root, now=lambda: NOW)
    )
    return created.run_id


def _translate(root: Path, run_id: str, texts: tuple[str, ...], *, offset: int = 10) -> None:
    provider = ScriptedProvider(texts)
    run_translation_worker(
        run_id,
        project_root=root,
        options=_options(max_jobs=1),
        provider_factory=lambda _job: provider,
        now=lambda: NOW + timedelta(seconds=offset),
    )


def _rejected_run(root: Path, registry: TerminologyRegistry, text: str) -> str:
    run_id = _prepared(root, registry)
    _translate(root, run_id, (text,))
    return run_id


def _force_historical_rejection(root: Path, run_id: str, chunk_id: str) -> None:
    """Write the prior state a QA engine correction leaves behind.

    The installed validator judges the historical acceptance and the current re-evaluation
    alike, so a rejection that current QA no longer reproduces cannot be produced by running the
    pipeline twice. It is constructed here instead, through the same contracts the worker uses:
    a standing ``qa_rejected`` sidecar over a translation current QA accepts.
    """
    from book_translator.provider_execution.artifacts import serialize_yaml, sha256_bytes

    run_path = _run_path(root, run_id)
    job_path = run_path / "jobs" / chunk_id / "job.yaml"
    job = TranslationJob.model_validate(yaml.safe_load(job_path.read_text(encoding="utf-8")))
    assert job.acceptance_file is not None
    evidence_path = root / job.acceptance_file
    evidence = TranslationAcceptanceEvidence.model_validate(
        yaml.safe_load(evidence_path.read_text(encoding="utf-8"))
    )
    candidate_id = load_registry(root / "config/terminology.yaml").candidates[0].candidate_id
    rejected = evidence.model_copy(
        update={
            "status": TranslationAcceptanceStatus.QA_REJECTED,
            "accepted": False,
            "terminology_report": evidence.terminology_report.model_copy(
                update={
                    "valid": False,
                    "findings": [
                        TerminologyFinding(
                            code="competing_variant",
                            severity="error",
                            candidate_id=candidate_id,
                            source_term="Alpha",
                            enforcement=TerminologyEnforcement.EXACT,
                            message="historical competing terminology variant",
                        )
                    ],
                }
            ),
        }
    )
    payload = serialize_yaml(rejected).encode("utf-8")
    evidence_path.write_bytes(payload)
    job_path.write_text(
        serialize_yaml(
            job.model_copy(
                update={
                    "acceptance_status": TranslationAcceptanceStatus.QA_REJECTED,
                    "acceptance_sha256": sha256_bytes(payload),
                }
            )
        ),
        encoding="utf-8",
    )
    run_yaml = run_path / "run.yaml"
    run = yaml.safe_load(run_yaml.read_text(encoding="utf-8"))
    run["accepted_count"] -= 1
    run["qa_rejected_count"] += 1
    run["status"] = TranslationRunStatus.COMPLETED_WITH_FAILURES.value
    run_yaml.write_text(yaml.safe_dump(run, sort_keys=False, allow_unicode=True), encoding="utf-8")


def test_a_stale_competing_variant_is_reevaluated_to_accepted_without_a_provider(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _rejected_run(root, _registry(variants=(DERIVED_VARIANT,)), DERIVED_TRANSLATION)
    _force_historical_rejection(root, run_id, "001-001")
    job = load_run_summary(run_id, project_root=root).jobs[0]
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED

    plan = plan_final_qa_audit(run_id, project_root=root, now=lambda: NOW)
    diagnosis = plan.report.jobs[0]
    assert diagnosis.verdict is FinalQAVerdict.REEVALUATE
    assert diagnosis.accepts_without_provider
    assert diagnosis.historical_blocking_codes == ["competing_variant"]
    assert diagnosis.current_blocking_codes == []
    assert not diagnosis.reproduces_under_current_qa

    calls: list[str] = []
    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider_factory=lambda _job: ScriptedProvider((CANONICAL_TRANSLATION,), calls),
        now=lambda: NOW + timedelta(seconds=60),
    )
    assert calls == []
    assert result.report.provider_calls == 0
    assert [outcome.action for outcome in result.report.outcomes] == ["reevaluated"]
    assert result.report.accepted_count == 1
    assert result.report.qa_rejected_count == 0
    assert result.report.run_status_after == TranslationRunStatus.COMPLETED.value

    after = load_run_summary(run_id, project_root=root).jobs[0]
    assert after.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert after.acceptance_origin is TranslationAcceptanceOrigin.QA_REEVALUATION
    assert after.reevaluation_count == 1


def test_a_real_rival_rendering_is_repaired_and_then_accepted(tmp_path: Path) -> None:
    root = tmp_path / "project"
    registry = _registry(variants=(RIVAL_VARIANT,))
    run_id = _rejected_run(root, registry, RIVAL_ONLY_TRANSLATION)
    job = load_run_summary(run_id, project_root=root).jobs[0]
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED

    plan = plan_final_qa_audit(run_id, project_root=root, now=lambda: NOW)
    assert plan.report.verdict_counts == {FinalQAVerdict.REPAIR_TRANSLATION.value: 1}

    provider = ScriptedProvider((CANONICAL_TRANSLATION,))
    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        provider_factory=lambda _job: provider,
        now=lambda: NOW + timedelta(seconds=60),
    )
    assert result.report.provider_calls == 1
    assert [outcome.action for outcome in result.report.outcomes] == ["repaired"]
    assert result.report.accepted_count == 1
    assert result.report.qa_rejected_count == 0
    assert result.report.run_status_after == TranslationRunStatus.COMPLETED.value
    assert result.report_file is not None
    assert not result.report.operator_required_job_ids


def test_a_final_run_is_only_completed_once_every_job_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "project"
    registry = _registry(variants=(RIVAL_VARIANT,))
    run_id = _rejected_run(root, registry, RIVAL_AND_CANONICAL_TRANSLATION)
    result = finalize_translation_run(run_id, project_root=root, dry_run=True, now=lambda: NOW)
    assert result.report.qa_rejected_count == 1
    assert result.report.run_status_after == TranslationRunStatus.COMPLETED_WITH_FAILURES.value
    assert result.report.operator_required_job_ids == [
        f"{run_id}__001-001",
    ]


def test_unresolved_jobs_are_reported_explicitly_and_grouped_by_decision(tmp_path: Path) -> None:
    root = tmp_path / "project"
    registry = _registry(variants=(RIVAL_VARIANT,))
    run_id = _rejected_run(root, registry, RIVAL_AND_CANONICAL_TRANSLATION)
    plan = plan_final_qa_audit(run_id, project_root=root, now=lambda: NOW)
    assert plan.report.verdict_counts == {FinalQAVerdict.OPERATOR_REQUIRED.value: 1}
    grouped = operator_required_terminology(plan.report)
    assert len(grouped) == 1
    candidate_id, source_term, variant, variant_class, count = grouped[0]
    assert (source_term, variant, count) == ("Alpha", RIVAL_VARIANT, 1)
    assert variant_class is CompetingVariantClass.INSUFFICIENT_EVIDENCE
    assert candidate_id.startswith("tc-")


def test_a_dry_run_changes_no_byte_and_contacts_no_provider(tmp_path: Path) -> None:
    root = tmp_path / "project"
    registry = _registry(variants=(RIVAL_VARIANT,))
    run_id = _rejected_run(root, registry, RIVAL_ONLY_TRANSLATION)
    before = _queue_bytes(root, run_id)
    registry_before = (root / "config/terminology.yaml").read_bytes()
    snapshot_before = {
        str(path): path.read_bytes()
        for path in sorted((root / "workspace/snapshots").rglob("*"))
        if path.is_file()
    }
    calls: list[str] = []

    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=True,
        provider=ProviderName.FAKE,
        provider_factory=lambda _job: ScriptedProvider((CANONICAL_TRANSLATION,), calls),
        now=lambda: NOW,
    )
    assert calls == []
    assert result.report.provider_calls == 0
    assert result.report_file is None
    assert _queue_bytes(root, run_id) == before
    assert (root / "config/terminology.yaml").read_bytes() == registry_before
    assert {
        str(path): path.read_bytes()
        for path in sorted((root / "workspace/snapshots").rglob("*"))
        if path.is_file()
    } == snapshot_before
    assert not (root / "workspace/audits").exists()
    # The plan is still complete: a dry run is a diagnosis, not a silence.
    assert result.report.audit.qa_rejected_count == 1
    assert result.report.outcomes[0].verdict is FinalQAVerdict.REPAIR_TRANSLATION


def test_historical_evidence_and_the_frozen_snapshot_survive_a_real_finalization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    registry = _registry(variants=(RIVAL_VARIANT,))
    run_id = _rejected_run(root, registry, RIVAL_ONLY_TRANSLATION)
    job = load_run_summary(run_id, project_root=root).jobs[0]
    assert job.acceptance_file is not None
    evidence_before = (root / job.acceptance_file).read_bytes()
    attempt_before = {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted((_run_path(root, run_id) / "jobs/001-001/attempts").rglob("*"))
        if path.is_file()
    }
    snapshot_before = {
        str(path): path.read_bytes()
        for path in sorted((root / "workspace/snapshots").rglob("*"))
        if path.is_file()
    }

    finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        provider_factory=lambda _job: ScriptedProvider((CANONICAL_TRANSLATION,)),
        now=lambda: NOW + timedelta(seconds=60),
    )
    assert (root / job.acceptance_file).read_bytes() == evidence_before
    for path, payload in attempt_before.items():
        assert (root / path).read_bytes() == payload
    assert {
        str(path): path.read_bytes()
        for path in sorted((root / "workspace/snapshots").rglob("*"))
        if path.is_file()
    } == snapshot_before
    # The historical rejection is still readable exactly as it was written.
    historical = TranslationAcceptanceEvidence.model_validate(
        yaml.safe_load((root / job.acceptance_file).read_text(encoding="utf-8"))
    )
    assert historical.status is TranslationAcceptanceStatus.QA_REJECTED


def test_run_counters_follow_the_jobs_after_a_finalization(tmp_path: Path) -> None:
    root = tmp_path / "project"
    registry = _registry(variants=(RIVAL_VARIANT,))
    run_id = _rejected_run(root, registry, RIVAL_ONLY_TRANSLATION)
    finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        provider_factory=lambda _job: ScriptedProvider((CANONICAL_TRANSLATION,)),
        now=lambda: NOW + timedelta(seconds=60),
    )
    summary = load_run_summary(run_id, project_root=root)
    accepted = sum(
        1 for job in summary.jobs if job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    )
    assert summary.run.accepted_count == accepted == 1
    assert summary.run.qa_rejected_count == 0
    assert summary.run.succeeded_count == len(summary.jobs)
    assert summary.run.status is TranslationRunStatus.COMPLETED


def test_no_accepted_job_regresses_when_a_rejected_sibling_is_finalized(tmp_path: Path) -> None:
    root = tmp_path / "project"
    registry = _registry(variants=(RIVAL_VARIANT,))
    root.mkdir(parents=True, exist_ok=True)
    synthetic_project(root, chapters=2)
    save_registry(root / "config/terminology.yaml", registry)
    (root / "config/terminology.metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "resource_kind": SnapshotResourceKind.TERMINOLOGY.value,
                "resource_file": "config/terminology.yaml",
                "version": "1.0.0",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    run_id = create_run(plan_run(snapshot.snapshot_id, project_root=root, now=lambda: NOW)).run_id
    execute_prompt_packages(plan_prompt_packages(run_id, project_root=root, now=lambda: NOW))
    _translate(root, run_id, (CANONICAL_TRANSLATION,), offset=10)
    _translate(root, run_id, (RIVAL_ONLY_TRANSLATION,), offset=20)

    jobs = {job.chunk_id: job for job in load_run_summary(run_id, project_root=root).jobs}
    accepted_before = {
        chunk_id
        for chunk_id, job in jobs.items()
        if job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    }
    assert accepted_before

    finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        provider_factory=lambda _job: ScriptedProvider((CANONICAL_TRANSLATION,)),
        now=lambda: NOW + timedelta(seconds=90),
    )
    after = {job.chunk_id: job for job in load_run_summary(run_id, project_root=root).jobs}
    for chunk_id in accepted_before:
        assert after[chunk_id].acceptance_status is TranslationAcceptanceStatus.ACCEPTED


def test_a_repair_the_provider_never_delivers_leaves_recovery_to_the_recovery_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    registry = _registry(variants=(RIVAL_VARIANT,))
    run_id = _rejected_run(root, registry, RIVAL_ONLY_TRANSLATION)
    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        provider_factory=lambda _job: FailingProvider(retryable=True),
        now=lambda: NOW + timedelta(seconds=60),
    )
    assert result.report.outcomes[0].acceptance_status_after == (
        TranslationAcceptanceStatus.QA_REJECTED.value
    )
    job = load_run_summary(run_id, project_root=root).jobs[0]
    # The standing rejection is intact, which is exactly the shape provider recovery acts on.
    assert job.status is TranslationJobStatus.FAILED
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert job.accepted_attempt == 1 and job.last_attempt == 2


# --------------------------------------------------------------------------------------------
# Exceptional repair budget escalation
# --------------------------------------------------------------------------------------------


def _clock(offset: int) -> Callable[[], datetime]:
    """Bind the offset now, so the loop variable cannot leak into a later call."""
    return lambda: NOW + timedelta(seconds=offset)


def _exhausted_run(root: Path) -> str:
    """A job whose provider budget is spent on a structural defect it never fixed."""
    run_id = _prepared(root, _registry(), max_attempts=3)
    for index in range(3):
        _translate(root, run_id, (ONE_PARAGRAPH,), offset=10 + index * 10)
        summary = load_run_summary(run_id, project_root=root)
        job = summary.jobs[0]
        if job.provider_attempt_count >= job.max_attempts:
            break
        from book_translator.translation_auto_repair import auto_repair_job

        auto_repair_job(
            run_id,
            job.job_id,
            project_root=root,
            provider=ProviderName.FAKE,
            model="fake-v1",
            worker_id="worker-test",
            lease_seconds=900,
            timeout_seconds=120,
            max_output_tokens=1000,
            max_repair_attempts=2,
            provider_factory=lambda _job: ScriptedProvider((ONE_PARAGRAPH,)),
            now=_clock(200 + index * 10),
        )
    return run_id


def test_an_exhausted_structural_rejection_is_reported_not_silently_topped_up(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _exhausted_run(root)
    plan = plan_final_qa_audit(run_id, project_root=root, now=lambda: NOW)
    diagnosis = plan.report.jobs[0]
    assert diagnosis.verdict is FinalQAVerdict.EXHAUSTED
    assert diagnosis.budget.remaining == 0
    assert diagnosis.budget.escalations_granted == 0
    assert diagnosis.structural is not None
    assert not diagnosis.structural.deterministically_normalizable

    # Without the explicit authorization nothing is granted and nothing is spent.
    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        provider_factory=lambda _job: ScriptedProvider((CANONICAL_TRANSLATION,)),
        now=lambda: NOW + timedelta(seconds=400),
    )
    assert result.report.provider_calls == 0
    assert result.report.outcomes[0].action == "reported"
    job = load_run_summary(run_id, project_root=root).jobs[0]
    assert job.max_attempts == 3


def test_an_authorized_escalation_is_bounded_persisted_and_used_once(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _exhausted_run(root)
    before = load_run_summary(run_id, project_root=root).jobs[0]

    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        allow_repair_escalation=True,
        provider_factory=lambda _job: ScriptedProvider((CANONICAL_TRANSLATION,)),
        now=lambda: NOW + timedelta(seconds=400),
    )
    assert result.report.outcomes[0].action == "escalated_and_repaired"
    assert result.report.provider_calls == 1

    escalation_path = _run_path(root, run_id) / "jobs/001-001/repair-escalation/001/escalation.yaml"
    escalation = TranslationRepairEscalation.model_validate(
        yaml.safe_load(escalation_path.read_text(encoding="utf-8"))
    )
    assert escalation.granted_attempts == MAX_ESCALATED_REPAIR_ATTEMPTS
    assert escalation.original_max_attempts == before.max_attempts
    assert escalation.resulting_max_attempts == before.max_attempts + 1
    assert escalation.authorized_by == "operator"
    assert "paragraph_count_mismatch" in escalation.triggering_codes

    after = load_run_summary(run_id, project_root=root).jobs[0]
    # The ceiling moved; no historical counter was reset.
    assert after.max_attempts == before.max_attempts + 1
    assert after.attempt_count >= before.attempt_count
    assert after.acceptance_status is TranslationAcceptanceStatus.ACCEPTED


def test_a_second_escalation_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _exhausted_run(root)
    finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        allow_repair_escalation=True,
        provider_factory=lambda _job: ScriptedProvider((ONE_PARAGRAPH,)),
        now=lambda: NOW + timedelta(seconds=400),
    )
    job = load_run_summary(run_id, project_root=root).jobs[0]
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    plan = plan_final_qa_audit(run_id, project_root=root, now=lambda: NOW)
    assert plan.report.jobs[0].budget.escalations_granted == MAX_ESCALATED_REPAIR_ATTEMPTS

    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        allow_repair_escalation=True,
        provider_factory=lambda _job: ScriptedProvider((CANONICAL_TRANSLATION,)),
        now=lambda: NOW + timedelta(seconds=500),
    )
    assert result.report.provider_calls == 0
    assert result.report.outcomes[0].action == "reported"
    assert not (_run_path(root, run_id) / "jobs/001-001/repair-escalation/002").exists()


# --------------------------------------------------------------------------------------------
# Compatibility and CLI
# --------------------------------------------------------------------------------------------


def test_without_a_provider_the_repairs_are_reported_and_nothing_else_is_blocked(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _rejected_run(root, _registry(variants=(RIVAL_VARIANT,)), RIVAL_ONLY_TRANSLATION)
    before = _queue_bytes(root, run_id)
    result = finalize_translation_run(run_id, project_root=root, dry_run=False, now=lambda: NOW)
    outcome = result.report.outcomes[0]
    assert outcome.verdict is FinalQAVerdict.REPAIR_TRANSLATION
    assert outcome.action == "reported"
    assert "provider_required" in outcome.reasons
    assert result.report.provider_calls == 0
    assert _queue_bytes(root, run_id) == before


def test_a_repair_reached_directly_without_a_provider_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _rejected_run(root, _registry(variants=(RIVAL_VARIANT,)), RIVAL_ONLY_TRANSLATION)
    plan = plan_final_qa_audit(run_id, project_root=root, now=lambda: NOW)
    with pytest.raises(FinalQAError, match="explicit provider"):
        _repair(
            plan.run_path,
            plan.diagnoses[0],
            _Options(
                dry_run=False,
                provider=None,
                model=None,
                worker_id="worker-test",
                lease_seconds=900,
                timeout_seconds=120,
                max_output_tokens=1000,
                reasoning_effort=None,
                max_cycles=1,
                max_repair_attempts=2,
                allow_repair_escalation=False,
                allow_provider_recovery=False,
                max_provider_recovery_attempts=0,
                effective_registry_path=Path("config/terminology.yaml"),
                provider_factory=None,
            ),
            project_root=root,
            max_repair_attempts=2,
            now=lambda: NOW,
        )


def test_the_audit_artifact_is_published_and_reproducible(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _rejected_run(
        root, _registry(variants=(RIVAL_VARIANT,)), RIVAL_AND_CANONICAL_TRANSLATION
    )
    plan = plan_final_qa_audit(run_id, project_root=root, now=lambda: NOW)
    written = execute_final_qa_audit(plan, project_root=root)
    assert written.is_file()
    again = execute_final_qa_audit(
        plan_final_qa_audit(run_id, project_root=root, now=lambda: NOW), project_root=root
    )
    assert again.read_bytes() == written.read_bytes()
    persisted = yaml.safe_load(written.read_text(encoding="utf-8"))
    assert persisted["final_qa_resolution_policy_version"] == FINAL_QA_RESOLUTION_POLICY_VERSION
    assert persisted["terminology_qa_version"] == TERMINOLOGY_QA_VERSION
    assert persisted["structural_qa_version"] == STRUCTURAL_QA_VERSION


def test_republishing_the_audit_leaves_the_finalization_history_alone(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _rejected_run(
        root, _registry(variants=(RIVAL_VARIANT,)), RIVAL_AND_CANONICAL_TRANSLATION
    )
    finalize_translation_run(run_id, project_root=root, dry_run=False, now=lambda: NOW)
    history = root / "workspace/audits/final-qa" / run_id / "finalizations/001/report.yaml"
    assert history.is_file()
    execute_final_qa_audit(
        plan_final_qa_audit(run_id, project_root=root, now=lambda: NOW), project_root=root
    )
    assert history.is_file()
    # A second finalization takes the next number instead of replacing the first.
    finalize_translation_run(run_id, project_root=root, dry_run=False, now=lambda: NOW)
    assert (root / "workspace/audits/final-qa" / run_id / "finalizations/002/report.yaml").is_file()


def test_the_audit_records_the_evidence_a_reviewer_needs(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _rejected_run(
        root, _registry(variants=(RIVAL_VARIANT,)), RIVAL_AND_CANONICAL_TRANSLATION
    )
    diagnosis = plan_final_qa_audit(run_id, project_root=root, now=lambda: NOW).report.jobs[0]
    assert diagnosis.acceptance_origin is TranslationAcceptanceOrigin.INITIAL_QA
    assert diagnosis.effective_attempt == 1
    assert diagnosis.installed_terminology_qa_version == TERMINOLOGY_QA_VERSION
    assert diagnosis.installed_structural_qa_version == STRUCTURAL_QA_VERSION
    assert diagnosis.reproduces_under_current_qa
    assert diagnosis.current_blocking_codes == ["competing_variant"]
    (record,) = diagnosis.competing_variants
    assert record.source_term == "Alpha"
    assert record.canonical_target == CANONICAL
    assert record.competing_variant == RIVAL_VARIANT
    assert record.enforcement is TerminologyEnforcement.EXACT
    assert record.decision_state is DecisionState.CONFIRMED
    assert record.decision_source is DecisionSource.AI
    assert record.candidate_provenance is DecisionSource.AI
    assert not record.locked
    assert record.canonical_present and record.variant_present
    assert record.source_occurrences == 1
    assert record.recorded_in_effective_registry
    assert record.reproduces_under_current_qa


def test_existing_reevaluate_and_repair_commands_still_work_beside_the_new_one(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _rejected_run(root, _registry(variants=(RIVAL_VARIANT,)), RIVAL_ONLY_TRANSLATION)
    job_id = load_run_summary(run_id, project_root=root).jobs[0].job_id
    from book_translator.translation_reevaluation import reevaluate_translation_job

    reevaluated = reevaluate_translation_job(
        run_id, job_id, 1, project_root=root, dry_run=True, now=lambda: NOW
    )
    assert reevaluated.resulting_status is TranslationAcceptanceStatus.QA_REJECTED
    assert reevaluated.terminology_qa_version == TERMINOLOGY_QA_VERSION

    from book_translator.translation_auto_repair import plan_auto_repair

    auto_plan = plan_auto_repair(run_id, job_id, project_root=root)
    assert auto_plan.assessment.budget.remaining >= 1


def test_the_cli_reports_the_audit_and_the_dry_run_without_printing_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    run_id = _rejected_run(
        root, _registry(variants=(RIVAL_VARIANT,)), RIVAL_AND_CANONICAL_TRANSLATION
    )
    monkeypatch.chdir(root)
    audited = runner.invoke(app, ["audit-final-qa", run_id])
    assert audited.exit_code == 0, audited.output
    assert "operator_required" in audited.output
    assert "Provider contacted: no" in audited.output
    assert "Gamma delta" not in audited.output
    assert (root / "workspace/audits/final-qa" / run_id / "audit.yaml").is_file()

    finalized = runner.invoke(app, ["finalize-translation-run", run_id, "--dry-run"])
    assert finalized.exit_code == 0, finalized.output
    assert "Dry run" in finalized.output
    assert "Gamma delta" not in finalized.output


def test_the_registry_still_refuses_to_store_a_variant_inside_the_canonical() -> None:
    """The storage rule is unchanged; only enforcement learned to disregard the mirror case."""
    from book_translator.terminology import TerminologyError

    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )
    with pytest.raises(TerminologyError, match="competing variants"):
        set_decision(
            registry,
            source_term="Alpha",
            target_term=CONTAINING_VARIANT,
            classification=TerminologyType.STORY_TERM,
            decision_source=DecisionSource.AI,
            locked=False,
            competing_variants=(CANONICAL,),
        )
    stored = set_decision(
        registry,
        source_term="Alpha",
        target_term=CANONICAL,
        classification=TerminologyType.STORY_TERM,
        decision_source=DecisionSource.AI,
        locked=False,
        competing_variants=(DERIVED_VARIANT,),
    )
    assert stored.decisions[0].competing_variants == [DERIVED_VARIANT]


def test_a_saved_registry_round_trips_through_the_analyzer(tmp_path: Path) -> None:
    path = tmp_path / "terminology.yaml"
    save_registry(path, _registry(variants=(RIVAL_VARIANT,)))
    reloaded = load_registry(path)
    (analysis,) = _analyses(reloaded, RIVAL_AND_CANONICAL_TRANSLATION)
    assert analysis.variant == RIVAL_VARIANT
    assert analysis.enforceable


def test_a_report_with_no_findings_still_yields_a_verdict() -> None:
    clean = TranslationQAReport(
        schema_version="1.0",
        valid=True,
        source_sha256="0" * 64,
        translation_sha256="1" * 64,
        source_paragraph_count=2,
        translation_paragraph_count=2,
        findings=[TranslationQAFinding(code="tab", severity="warning", message="tab")],
    )
    assessment = assess_final_qa(
        terminology_report=validate_terminology(SOURCE, CANONICAL_TRANSLATION, _registry()),
        structural_report=clean,
        competing_variants=(),
        budget=RepairBudget(2, 0, 3, 1),
    )
    assert assessment.verdict is FinalQAVerdict.REEVALUATE
    assert assessment.blocking_codes == ()
