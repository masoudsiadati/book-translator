"""Governed carry-forward and re-evaluation of historically QA-rejected provider outputs."""

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from book_translator.historical_reevaluation_policy import (
    HISTORICAL_REEVALUATION_POLICY_VERSION,
    TerminologyDriftFacts,
    evaluate_historical_compatibility,
)
from book_translator.models import (
    DecisionSource,
    DecisionState,
    HistoricalCompatibilityTier,
    PromptPackage,
    PromptPackageMessageReference,
    PromptPackageResourceReference,
    PromptPackageStatus,
    SnapshotResourceKind,
    TerminologyType,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationHistoricalReevaluationEvidence,
    TranslationJobStatus,
)
from book_translator.prompt_packaging.identity import (
    OUTPUT_CONTRACT_VERSION,
    PROMPT_PACKAGE_RENDERER_VERSION,
)
from book_translator.provider_execution.artifacts import load_yaml
from book_translator.terminology import load_registry, save_registry, set_decision
from book_translator.terminology.question_workflow import (
    execute_decision_correction,
    plan_decision_correction,
    terminology_path,
)
from book_translator.terminology.validation import STRUCTURAL_QA_VERSION, TERMINOLOGY_QA_VERSION
from book_translator.translation_historical_reevaluation import (
    ALREADY_APPLIED,
    BLOCKED,
    ELIGIBLE,
    HISTORICAL_REEVALUATION_ENGINE_VERSION,
    HistoricalReevaluationError,
    plan_historical_output_reevaluation,
    reevaluate_historical_output,
)
from book_translator.translation_queue import load_run_summary
from book_translator.translation_worker import run_translation_worker
from tests.test_translation_queue import NOW
from tests.test_translation_reuse import _bytes, _rendered_run, _write_registry
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import FailingProvider, SyntheticProvider, _options

# The synthetic chunk source is "Alpha beta.\n\nGamma delta.\n". The historical provider rendered
# "Alpha" as الفا while the registry of the day required آلفا, so the output was provider
# successful and QA rejected — the exact shape this workflow exists for.
ALPHA_AI: dict[str, Any] = {
    "source_term": "Alpha",
    "target_term": "آلفا",
    "classification": TerminologyType.STORY_TERM,
    "decision_source": DecisionSource.AI,
    "locked": False,
}
CORRECTED_TARGET = "الفا"
HISTORICAL_OUTPUT = "الفا نخست.\n\nترجمه دوم.\n"
# One paragraph against a two-paragraph source: a real structural defect no terminology move can
# resolve. This is the synthetic stand-in for the 000-prologue-002 control.
STRUCTURALLY_DEFECTIVE_OUTPUT = "الفا نخست.\n"


def _rejected_old_run(root: Path, translation: str = HISTORICAL_OUTPUT) -> str:
    synthetic_project(root)
    _write_registry(root, [ALPHA_AI])
    run_id = _rendered_run(root)
    run_translation_worker(
        run_id,
        project_root=root,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider(translation),
        now=lambda: NOW + timedelta(seconds=10),
    )
    return run_id


def _govern_correction(
    root: Path,
    *,
    target_term: str = CORRECTED_TARGET,
    decision_source: DecisionSource = DecisionSource.AI,
    locked: bool = False,
) -> None:
    """Move the registry the way the project's own governed writer does, leaving evidence."""
    plan = plan_decision_correction(
        root,
        source_term="Alpha",
        target_term=target_term,
        decision_source=decision_source,
        decision_state=DecisionState.CONFIRMED,
        locked=locked,
        reason="synthetic governed correction of a wrong canonical target",
    )
    execute_decision_correction(plan, now=lambda: NOW + timedelta(seconds=20))


def _corrected_successor_run(root: Path, **updates: Any) -> str:
    _govern_correction(root, **updates)
    return _rendered_run(root)


def _job_of(root: Path, run_id: str) -> Any:
    return load_run_summary(run_id, project_root=root).jobs[0]


def _carry_forward_evidence(root: Path, run_id: str) -> TranslationHistoricalReevaluationEvidence:
    run_path = root / "workspace/translation-runs" / run_id
    path = run_path / "jobs/001-001/carry-forward/001/evidence.yaml"
    return load_yaml(path, TranslationHistoricalReevaluationEvidence)


def _carry_forward(root: Path, new: str, old: str, **updates: Any) -> Any:
    return reevaluate_historical_output(
        new,
        from_run=old,
        job_id="001-001",
        attempt_number=1,
        project_root=root,
        now=lambda: NOW + timedelta(seconds=30),
        **updates,
    )


# --- pure policy ---------------------------------------------------------------------------


def _reference(letter: str) -> PromptPackageResourceReference:
    return PromptPackageResourceReference(
        resource_kind=SnapshotResourceKind.TERMINOLOGY,
        file=Path(f"config/{letter}.yaml"),
        sha256=letter * 64,
        version="1.0.0",
    )


def _message(role: str, letter: str) -> PromptPackageMessageReference:
    return PromptPackageMessageReference(
        role=role,  # type: ignore[arg-type]
        file=Path(f"{role}.md"),
        sha256=letter * 64,
        character_count=1,
        estimated_tokens=1,
    )


def _synthetic_package(**updates: Any) -> PromptPackage:
    values: dict[str, Any] = {
        "schema_version": "1.0",
        "package_id": "pp-" + "1" * 24,
        "package_manifest_sha256": "2" * 64,
        "renderer_version": PROMPT_PACKAGE_RENDERER_VERSION,
        "run_id": "tr-0123456789ab-001",
        "job_id": "001-001",
        "snapshot_id": "translation-fa-0123456789ab000000000000",
        "manifest_sha256": "3" * 64,
        "chapter_id": "001",
        "chunk_id": "001-001",
        "chunk_sequence": 1,
        "total_chapter_chunks": 1,
        "system_message": _message("system", "a"),
        "user_message": _message("user", "b"),
        "chunk_source_sha256": "c" * 64,
        "context_sha256": "d" * 64,
        "prompt": _reference("e"),
        "glossary": _reference("f"),
        "style_guide": _reference("0"),
        "terminology": _reference("9"),
        "source_language": "en",
        "target_language": "fa",
        "source_word_count": 4,
        "context_word_count": 0,
        "estimated_input_tokens": 10,
        "output_contract_version": OUTPUT_CONTRACT_VERSION,
        "status": PromptPackageStatus.VALIDATED,
        "created_at": NOW,
    }
    values.update(updates)
    return PromptPackage.model_validate(values)


BASE_SECTIONS: Mapping[str, str] = {
    "translation-prompt-rules": "1" * 64,
    "style-guide": "2" * 64,
    "glossary-data": "3" * 64,
    "previous-context": "4" * 64,
    "source-text": "5" * 64,
}
GOVERNED = TerminologyDriftFacts("a" * 64, "b" * 64, governed=True, transition_ids=("tdc-x",))


def _classify(
    old: PromptPackage,
    new: PromptPackage,
    *,
    old_sections: Mapping[str, str] = BASE_SECTIONS,
    new_sections: Mapping[str, str] = BASE_SECTIONS,
    historical_output_contract: str = OUTPUT_CONTRACT_VERSION,
    terminology_drift: TerminologyDriftFacts = GOVERNED,
) -> Any:
    return evaluate_historical_compatibility(
        old,
        new,
        old_sections=old_sections,
        new_sections=new_sections,
        historical_output_contract=historical_output_contract,
        terminology_drift=terminology_drift,
    )


def test_identical_request_is_the_exact_tier() -> None:
    verdict = _classify(_synthetic_package(), _synthetic_package())

    assert verdict.tier is HistoricalCompatibilityTier.EXACT_RENDERED_REQUEST
    assert verdict.eligible and verdict.blocking_reasons == ()


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"chunk_source_sha256": "9" * 64}, "source_hash_mismatch"),
        ({"context_sha256": "9" * 64}, "context_hash_mismatch"),
        ({"system_message": _message("system", "9")}, "system_message_hash_mismatch"),
        ({"target_language": "de"}, "target_language_mismatch"),
        ({"source_language": "de"}, "source_language_mismatch"),
        ({"output_contract_version": "other/1.0.0"}, "output_contract_mismatch"),
    ],
)
def test_request_contract_changes_are_never_waived(updates: dict[str, Any], reason: str) -> None:
    verdict = _classify(_synthetic_package(), _synthetic_package(**updates))

    assert verdict.tier is HistoricalCompatibilityTier.CONTRACT_CHANGED
    assert not verdict.eligible and reason in verdict.blocking_reasons


def test_historical_output_contract_incompatibility_is_blocked() -> None:
    verdict = _classify(
        _synthetic_package(),
        _synthetic_package(),
        historical_output_contract="persian-translation-text/0.9.0",
    )

    assert verdict.tier is HistoricalCompatibilityTier.CONTRACT_CHANGED
    assert "historical_output_contract_mismatch" in verdict.blocking_reasons


@pytest.mark.parametrize("section", ["translation-prompt-rules", "style-guide"])
def test_prompt_and_style_contract_drift_is_blocked(section: str) -> None:
    verdict = _classify(
        _synthetic_package(),
        _synthetic_package(user_message=_message("user", "9")),
        new_sections={**BASE_SECTIONS, section: "f" * 64},
    )

    assert verdict.tier is HistoricalCompatibilityTier.CONTRACT_CHANGED
    assert f"effective_{section.replace('-', '_')}_changed" in verdict.blocking_reasons


def test_governed_terminology_drift_reaching_only_the_projection_is_eligible() -> None:
    verdict = _classify(
        _synthetic_package(),
        _synthetic_package(terminology=_reference("8"), user_message=_message("user", "9")),
        new_sections={**BASE_SECTIONS, "glossary-data": "f" * 64},
    )

    assert verdict.tier is HistoricalCompatibilityTier.GOVERNED_RESOURCE_DRIFT
    assert verdict.eligible
    assert verdict.changed_framed_sections == ("effective_glossary_data_changed",)
    assert "terminology_hash_mismatch" in verdict.observed_resource_drift


def test_locked_terminology_drift_is_never_silently_waived() -> None:
    verdict = _classify(
        _synthetic_package(),
        _synthetic_package(terminology=_reference("8"), user_message=_message("user", "9")),
        new_sections={**BASE_SECTIONS, "glossary-data": "f" * 64},
        terminology_drift=TerminologyDriftFacts(
            "a" * 64, "b" * 64, governed=True, locked_decision_drift=("tc-locked",)
        ),
    )

    assert verdict.tier is HistoricalCompatibilityTier.UNSAFE_DRIFT
    assert "locked_terminology_drift" in verdict.blocking_reasons
    assert "tc-locked" in verdict.blocking_reasons


def test_ungoverned_terminology_drift_is_blocked() -> None:
    verdict = _classify(
        _synthetic_package(),
        _synthetic_package(terminology=_reference("8"), user_message=_message("user", "9")),
        new_sections={**BASE_SECTIONS, "glossary-data": "f" * 64},
        terminology_drift=TerminologyDriftFacts("a" * 64, "b" * 64, governed=False),
    )

    assert verdict.tier is HistoricalCompatibilityTier.UNSAFE_DRIFT
    assert "terminology_drift_not_governed" in verdict.blocking_reasons


def test_non_terminology_resource_drift_cannot_explain_a_changed_projection() -> None:
    verdict = _classify(
        _synthetic_package(),
        _synthetic_package(
            terminology=_reference("8"),
            glossary=_reference("7"),
            user_message=_message("user", "9"),
        ),
        new_sections={**BASE_SECTIONS, "glossary-data": "f" * 64},
    )

    assert verdict.tier is HistoricalCompatibilityTier.UNSAFE_DRIFT
    assert "ungoverned_resource_drift" in verdict.blocking_reasons
    assert "glossary_hash_mismatch" in verdict.blocking_reasons


def test_changed_projection_without_a_terminology_move_is_unattributable() -> None:
    verdict = _classify(
        _synthetic_package(),
        _synthetic_package(user_message=_message("user", "9")),
        new_sections={**BASE_SECTIONS, "glossary-data": "f" * 64},
    )

    assert verdict.tier is HistoricalCompatibilityTier.UNSAFE_DRIFT
    assert "resource_projection_change_unattributable" in verdict.blocking_reasons


# --- eligible carry-forward ----------------------------------------------------------------


def test_governed_drift_over_unchanged_source_and_context_is_eligible(tmp_path: Path) -> None:
    old = _rejected_old_run(tmp_path)
    assert _job_of(tmp_path, old).acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    new = _corrected_successor_run(tmp_path)

    plan = plan_historical_output_reevaluation(
        new, from_run=old, job_id="001-001", attempt_number=1, project_root=tmp_path
    )

    assert plan.outcome == ELIGIBLE
    assert plan.tier is HistoricalCompatibilityTier.GOVERNED_RESOURCE_DRIFT
    assert plan.changed_framed_sections == ("effective_glossary_data_changed",)
    assert plan.observed_resource_drift == ("terminology_hash_mismatch",)
    assert plan.governed_transition_ids and plan.locked_decision_drift == ()
    assert plan.historical_status is TranslationAcceptanceStatus.QA_REJECTED
    assert plan.projected_status is TranslationAcceptanceStatus.ACCEPTED
    assert plan.resolved_historical_findings and plan.persisting_historical_findings == ()
    assert "source_hash" in plan.verified_identity_checks


def test_current_qa_acceptance_creates_no_provider_attempt(tmp_path: Path) -> None:
    old = _rejected_old_run(tmp_path)
    new = _corrected_successor_run(tmp_path)

    result = _carry_forward(tmp_path, new, old)

    assert result.published == 1 and result.provider_calls == 0
    assert result.resulting_status is TranslationAcceptanceStatus.ACCEPTED
    job = _job_of(tmp_path, new)
    assert job.status is TranslationJobStatus.SUCCEEDED
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.acceptance_origin is TranslationAcceptanceOrigin.HISTORICAL_REEVALUATION
    assert job.carry_forward_count == 1 and job.reuse_count == 0
    assert job.attempt_count == 0 and job.successful_attempt is None
    run_path = tmp_path / "workspace/translation-runs" / new
    assert not (run_path / "jobs/001-001/attempts").exists()
    assert not (run_path / "jobs/001-001/reuse").exists()
    assert load_run_summary(new, project_root=tmp_path).run.accepted_count == 1


def test_current_qa_rejection_is_retained_without_a_provider_attempt(tmp_path: Path) -> None:
    old = _rejected_old_run(tmp_path, translation=STRUCTURALLY_DEFECTIVE_OUTPUT)
    new = _corrected_successor_run(tmp_path)

    result = _carry_forward(tmp_path, new, old)

    assert result.published == 1 and result.provider_calls == 0
    assert result.resulting_status is TranslationAcceptanceStatus.QA_REJECTED
    job = _job_of(tmp_path, new)
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert job.acceptance_origin is TranslationAcceptanceOrigin.HISTORICAL_REEVALUATION
    assert job.attempt_count == 0
    assert not (tmp_path / "workspace/translation-runs" / new / "jobs/001-001/attempts").exists()


def test_a_persisting_structural_defect_is_never_promoted(tmp_path: Path) -> None:
    """The 000-prologue-002 control: eligible to re-evaluate, still rejected on its own merits."""
    old = _rejected_old_run(tmp_path, translation=STRUCTURALLY_DEFECTIVE_OUTPUT)
    new = _corrected_successor_run(tmp_path)

    plan = plan_historical_output_reevaluation(
        new, from_run=old, job_id="001-001", attempt_number=1, project_root=tmp_path
    )

    assert plan.outcome == ELIGIBLE
    assert plan.projected_status is TranslationAcceptanceStatus.QA_REJECTED
    assert "paragraph_count_mismatch" in plan.structural_finding_codes
    assert "paragraph_count_mismatch" in plan.persisting_historical_findings
    assert plan.terminology_finding_codes == ()

    evidence = _carry_forward(tmp_path, new, old)
    assert evidence.resulting_status is TranslationAcceptanceStatus.QA_REJECTED


# --- provenance ----------------------------------------------------------------------------


def test_persisted_provenance_records_the_historical_rejection_and_no_provider_contact(
    tmp_path: Path,
) -> None:
    old = _rejected_old_run(tmp_path)
    new = _corrected_successor_run(tmp_path)
    _carry_forward(tmp_path, new, old)

    evidence = _carry_forward_evidence(tmp_path, new)
    assert evidence.execution_kind == "historical_reevaluation"
    assert evidence.provider_contacted is False
    assert evidence.carry_forward_engine_version == HISTORICAL_REEVALUATION_ENGINE_VERSION
    assert evidence.source_run_id == old and evidence.run_id == new
    assert evidence.source_attempt_number == 1
    assert evidence.source_provider == "fake"
    assert evidence.historical_status is TranslationAcceptanceStatus.QA_REJECTED
    assert not evidence.historical_terminology_report.valid
    assert any(
        finding.code == "canonical_term_missing"
        for finding in evidence.historical_terminology_report.findings
    )
    assert evidence.resulting_status is TranslationAcceptanceStatus.ACCEPTED
    assert evidence.terminology_report.valid and evidence.structural_report.valid
    assert evidence.terminology_qa_version == TERMINOLOGY_QA_VERSION
    assert evidence.structural_qa_version == STRUCTURAL_QA_VERSION
    assert evidence.translation_sha256 == evidence.source_translation_sha256

    compatibility = evidence.compatibility
    assert compatibility.policy_version == HISTORICAL_REEVALUATION_POLICY_VERSION
    assert compatibility.tier is HistoricalCompatibilityTier.GOVERNED_RESOURCE_DRIFT
    assert compatibility.provider_contacted is False
    assert compatibility.source_snapshot_id != compatibility.target_snapshot_id
    assert compatibility.source_prompt_package_id != compatibility.target_prompt_package_id
    assert compatibility.source_system_message_sha256 == compatibility.target_system_message_sha256
    assert compatibility.source_user_message_sha256 != compatibility.target_user_message_sha256
    assert compatibility.source_terminology_sha256 != compatibility.target_terminology_sha256
    drift = compatibility.terminology_drift
    assert drift is not None and drift.explained and drift.locked_decision_drift == []
    assert [item.event_kind for item in drift.transitions] == ["decision_correction"]


def test_carry_forward_provenance_is_distinct_from_accepted_translation_reuse(
    tmp_path: Path,
) -> None:
    old = _rejected_old_run(tmp_path)
    new = _corrected_successor_run(tmp_path)
    _carry_forward(tmp_path, new, old)

    run_path = tmp_path / "workspace/translation-runs" / new
    assert (run_path / "jobs/001-001/carry-forward/001/evidence.yaml").is_file()
    assert not (run_path / "jobs/001-001/reuse").exists()
    job = _job_of(tmp_path, new)
    assert job.reuse_count == 0 and job.latest_reuse_file is None
    assert job.acceptance_origin is not TranslationAcceptanceOrigin.REUSE_QA
    raw = yaml.safe_load(
        (run_path / "jobs/001-001/carry-forward/001/evidence.yaml").read_text(encoding="utf-8")
    )
    assert raw["execution_kind"] == "historical_reevaluation"
    assert "reuse_engine_version" not in raw
    assert raw["historical_status"] == "qa_rejected"
    acceptance = yaml.safe_load(
        (run_path / "jobs/001-001/carry-forward/001/acceptance.yaml").read_text(encoding="utf-8")
    )
    assert acceptance["acceptance_engine_version"] == HISTORICAL_REEVALUATION_ENGINE_VERSION


# --- refusals ------------------------------------------------------------------------------


def _blocking_reasons(root: Path, new: str, old: str) -> tuple[str, ...]:
    plan = plan_historical_output_reevaluation(
        new, from_run=old, job_id="001-001", attempt_number=1, project_root=root
    )
    assert plan.outcome == BLOCKED
    return plan.blocking_reasons


def test_ungoverned_registry_edit_between_snapshots_is_blocked(tmp_path: Path) -> None:
    old = _rejected_old_run(tmp_path)
    registry = load_registry(terminology_path(tmp_path))
    save_registry(
        terminology_path(tmp_path),
        set_decision(registry, **{**ALPHA_AI, "target_term": CORRECTED_TARGET}),
    )
    new = _rendered_run(tmp_path)

    assert "terminology_drift_not_governed" in _blocking_reasons(tmp_path, new, old)


def test_locked_canonical_drift_reaching_the_job_is_blocked(tmp_path: Path) -> None:
    old = _rejected_old_run(tmp_path)
    new = _corrected_successor_run(tmp_path, decision_source=DecisionSource.USER, locked=True)

    reasons = _blocking_reasons(tmp_path, new, old)
    assert "locked_terminology_drift" in reasons


def test_historical_provider_failure_is_blocked(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    _write_registry(tmp_path, [ALPHA_AI])
    old = _rendered_run(tmp_path)
    run_translation_worker(
        old,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: FailingProvider(retryable=False),
        now=lambda: NOW + timedelta(seconds=10),
    )
    assert _job_of(tmp_path, old).acceptance_status is None
    new = _corrected_successor_run(tmp_path)

    assert "historical_attempt_not_successful" in _blocking_reasons(tmp_path, new, old)


def test_historically_ambiguous_attempt_is_blocked(tmp_path: Path) -> None:
    old = _rejected_old_run(tmp_path)
    new = _corrected_successor_run(tmp_path)
    attempt = (
        tmp_path / "workspace/translation-runs" / old / "jobs/001-001/attempts/001/attempt.yaml"
    )
    raw = yaml.safe_load(attempt.read_text(encoding="utf-8"))
    raw["ambiguous"] = True
    attempt.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    assert _blocking_reasons(tmp_path, new, old) == ("historical_attempt_ambiguous",)


def test_tampered_historical_output_is_blocked(tmp_path: Path) -> None:
    old = _rejected_old_run(tmp_path)
    new = _corrected_successor_run(tmp_path)
    translation = (
        tmp_path / "workspace/translation-runs" / old / "jobs/001-001/attempts/001/translation.md"
    )
    translation.write_text("الفا دستکاری‌شده.\n\nترجمه دوم.\n", encoding="utf-8")

    assert _blocking_reasons(tmp_path, new, old) == ("historical_attempt_invalid",)


def _accepted_old_run(root: Path) -> str:
    synthetic_project(root)
    _write_registry(root, [ALPHA_AI])
    old = _rendered_run(root)
    run_translation_worker(
        old,
        project_root=root,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("آلفا نخست.\n\nترجمه دوم.\n"),
        now=lambda: NOW + timedelta(seconds=10),
    )
    assert _job_of(root, old).acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    return old


def test_accepted_output_with_an_identical_request_is_routed_to_reuse(tmp_path: Path) -> None:
    old = _accepted_old_run(tmp_path)
    new = _rendered_run(tmp_path)

    assert "historical_output_reusable_exactly" in _blocking_reasons(tmp_path, new, old)


def test_accepted_output_whose_request_moved_is_reassessed_not_trusted(tmp_path: Path) -> None:
    # Engine 1.1.0: reuse must refuse a changed request, so a governed move is assessed here, and
    # current QA — not the historical acceptance — decides. The correction moved the canonical away
    # from what the output wrote, so the carried output is rejected rather than laundered.
    old = _accepted_old_run(tmp_path)
    new = _corrected_successor_run(tmp_path)

    plan = plan_historical_output_reevaluation(
        new, from_run=old, job_id="001-001", attempt_number=1, project_root=tmp_path
    )

    assert plan.outcome == ELIGIBLE
    assert plan.tier is HistoricalCompatibilityTier.GOVERNED_RESOURCE_DRIFT
    assert plan.historical_status is TranslationAcceptanceStatus.ACCEPTED
    assert plan.projected_status is TranslationAcceptanceStatus.QA_REJECTED


def test_missing_frozen_terminology_is_blocked(tmp_path: Path) -> None:
    old = _rejected_old_run(tmp_path)
    new = _corrected_successor_run(tmp_path)
    snapshot_path = load_run_summary(new, project_root=tmp_path).run.snapshot_path
    manifest = yaml.safe_load(
        (tmp_path / snapshot_path / "manifest.yaml").read_text(encoding="utf-8")
    )
    (tmp_path / manifest["terminology"]["frozen_file"]).unlink()

    assert "frozen_terminology_unavailable" in _blocking_reasons(tmp_path, new, old)


def test_already_attempted_destination_is_blocked(tmp_path: Path) -> None:
    old = _rejected_old_run(tmp_path)
    new = _corrected_successor_run(tmp_path)
    run_translation_worker(
        new,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider(HISTORICAL_OUTPUT),
        now=lambda: NOW + timedelta(seconds=25),
    )

    reasons = _blocking_reasons(tmp_path, new, old)
    assert "destination_not_pending" in reasons
    assert "destination_already_attempted" in reasons
    assert "destination_already_assessed" in reasons
    with pytest.raises(HistoricalReevaluationError):
        _carry_forward(tmp_path, new, old)


def test_repeating_an_applied_carry_forward_is_idempotent(tmp_path: Path) -> None:
    old = _rejected_old_run(tmp_path)
    new = _corrected_successor_run(tmp_path)
    first = _carry_forward(tmp_path, new, old)
    before = _bytes(tmp_path)

    second = _carry_forward(tmp_path, new, old)

    assert first.published == 1 and second.published == 0
    assert second.reused and second.plan.outcome == ALREADY_APPLIED
    assert second.resulting_status is TranslationAcceptanceStatus.ACCEPTED
    assert _bytes(tmp_path) == before


# --- immutability --------------------------------------------------------------------------


def test_dry_run_mutates_nothing(tmp_path: Path) -> None:
    old = _rejected_old_run(tmp_path)
    new = _corrected_successor_run(tmp_path)
    before = _bytes(tmp_path)

    result = _carry_forward(tmp_path, new, old, dry_run=True)

    assert result.dry_run and result.published == 0
    assert result.plan.outcome == ELIGIBLE
    assert result.resulting_status is TranslationAcceptanceStatus.ACCEPTED
    assert result.evidence_file is None and result.acceptance_file is None
    assert _bytes(tmp_path) == before


def test_carry_forward_leaves_the_historical_run_and_frozen_resources_untouched(
    tmp_path: Path,
) -> None:
    old = _rejected_old_run(tmp_path)
    new = _corrected_successor_run(tmp_path)
    historical_before = _bytes(tmp_path / "workspace/translation-runs" / old)
    config_before = _bytes(tmp_path / "config")
    snapshots_before = _bytes(tmp_path / "workspace/snapshots")
    resources_before = _bytes(tmp_path / "workspace/resources")

    _carry_forward(tmp_path, new, old)

    assert _bytes(tmp_path / "workspace/translation-runs" / old) == historical_before
    assert _bytes(tmp_path / "config") == config_before
    assert _bytes(tmp_path / "workspace/snapshots") == snapshots_before
    assert _bytes(tmp_path / "workspace/resources") == resources_before
