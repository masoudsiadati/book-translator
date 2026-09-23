import hashlib
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from book_translator.models import (
    DecisionSource,
    PromptPackage,
    PromptPackageMessageReference,
    PromptPackageResourceReference,
    PromptPackageStatus,
    ReuseCompatibilityTier,
    SnapshotResourceKind,
    TerminologyEnforcement,
    TerminologyFinding,
    TerminologyRegistry,
    TerminologyType,
    TerminologyValidationReport,
    TranslationAcceptanceEvidence,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationReuseEvidence,
)
from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
from book_translator.prompt_packaging.identity import (
    OUTPUT_CONTRACT_VERSION,
    PROMPT_PACKAGE_RENDERER_VERSION,
)
from book_translator.provider_execution.artifacts import load_yaml
from book_translator.terminology import save_registry, set_decision
from book_translator.terminology.validation import STRUCTURAL_QA_VERSION, TERMINOLOGY_QA_VERSION
from book_translator.translation_queue import create_run, load_run_summary, plan_run
from book_translator.translation_reevaluation import reevaluate_translation_job
from book_translator.translation_reuse import (
    TRANSLATION_REUSE_ENGINE_VERSION,
    plan_accepted_translation_reuse,
    reuse_accepted_translations,
)
from book_translator.translation_reuse_policy import (
    REUSE_COMPATIBILITY_POLICY_VERSION,
    classify_rendered_divergence,
    evaluate_request_compatibility,
)
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import run_translation_worker
from tests.test_translation_queue import NOW, SNAPSHOT_NOW
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import SyntheticProvider, _options, _prepared_run

# The synthetic chunk source is "Alpha beta.\n\nGamma delta.\n", so a decision on Alpha is
# projected into that job's prompt and a decision on Omega never is.
ALPHA_LOCKED: dict[str, Any] = {
    "source_term": "Alpha",
    "target_term": "آلفا",
    "classification": TerminologyType.STORY_TERM,
    "decision_source": DecisionSource.USER,
    "locked": True,
}
OMEGA_IRRELEVANT: dict[str, Any] = {
    "source_term": "Omega",
    "target_term": "امگا",
    "classification": TerminologyType.STORY_TERM,
    "decision_source": DecisionSource.AI,
    "locked": False,
}
ACCEPTED_TRANSLATION = "آلفا نخست.\n\nترجمه دوم.\n"


def _write_registry(root: Path, decisions: Iterable[dict[str, Any]]) -> None:
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )
    for decision in decisions:
        registry = set_decision(registry, **decision)
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


def _rendered_run(root: Path) -> str:
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    created = create_run(plan_run(snapshot.snapshot_id, project_root=root, now=lambda: NOW))
    execute_prompt_packages(
        plan_prompt_packages(created.run_id, project_root=root, now=lambda: NOW)
    )
    return created.run_id


def _accepted_old_run(
    root: Path,
    decisions: Iterable[dict[str, Any]],
    translation: str = ACCEPTED_TRANSLATION,
) -> str:
    synthetic_project(root)
    _write_registry(root, decisions)
    run_id = _rendered_run(root)
    run_translation_worker(
        run_id,
        project_root=root,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider(translation),
        now=lambda: NOW + timedelta(seconds=10),
    )
    return run_id


def _successor_run(root: Path, decisions: Iterable[dict[str, Any]]) -> str:
    _write_registry(root, decisions)
    return _rendered_run(root)


def _bytes(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _terminology_sha256(root: Path, run_id: str) -> str:
    package = load_yaml(
        root / "workspace/translation-runs" / run_id / "jobs/001-001/prompt-package/package.yaml",
        PromptPackage,
    )
    assert package.terminology is not None
    return package.terminology.sha256


def _reuse_evidence(root: Path, run_id: str) -> TranslationReuseEvidence:
    path = root / "workspace/translation-runs" / run_id / "jobs/001-001/reuse/001/evidence.yaml"
    return load_yaml(path, TranslationReuseEvidence)


# --- pure policy -------------------------------------------------------------------------


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


def test_resource_drift_alone_leaves_the_effective_request_identical() -> None:
    old = _synthetic_package()
    new = _synthetic_package(terminology=_reference("8"), renderer_version="9.9.9")
    verdict = evaluate_request_compatibility(old, new)
    assert verdict.tier is ReuseCompatibilityTier.EXACT_RENDERED_REQUEST
    assert verdict.identity_reasons == ()
    assert "terminology_hash_mismatch" in verdict.resource_drift_reasons
    assert "renderer_version_mismatch" in verdict.resource_drift_reasons
    assert "user_message_hash" in verdict.verified_identity_checks


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"chunk_source_sha256": "9" * 64}, "source_hash_mismatch"),
        ({"context_sha256": "9" * 64}, "context_hash_mismatch"),
        ({"system_message": _message("system", "9")}, "system_message_hash_mismatch"),
        ({"user_message": _message("user", "9")}, "user_message_hash_mismatch"),
        ({"target_language": "de"}, "target_language_mismatch"),
        ({"output_contract_version": "other/1.0.0"}, "output_contract_mismatch"),
    ],
)
def test_changed_effective_request_inputs_are_never_waived(
    updates: dict[str, Any], reason: str
) -> None:
    verdict = evaluate_request_compatibility(_synthetic_package(), _synthetic_package(**updates))
    assert verdict.tier is ReuseCompatibilityTier.INCOMPATIBLE
    assert reason in verdict.identity_reasons


def test_rendered_divergence_names_only_the_changed_framed_sections() -> None:
    old = {"source-text": "a" * 64, "glossary-data": "b" * 64, "style-guide": "c" * 64}
    new = {"source-text": "a" * 64, "glossary-data": "d" * 64, "style-guide": "c" * 64}
    assert classify_rendered_divergence(old, new) == ("effective_glossary_data_changed",)


# --- tier 1: exact rendered-request reuse ------------------------------------------------


def test_terminology_hash_drift_with_identical_rendered_request_is_reusable(
    tmp_path: Path,
) -> None:
    old = _accepted_old_run(tmp_path, [ALPHA_LOCKED])
    new = _successor_run(tmp_path, [ALPHA_LOCKED, OMEGA_IRRELEVANT])
    assert _terminology_sha256(tmp_path, old) != _terminology_sha256(tmp_path, new)

    plan = plan_accepted_translation_reuse(new, from_run=old, project_root=tmp_path)
    item = plan.jobs[0]
    assert item.outcome == "reusable"
    assert item.tier is ReuseCompatibilityTier.EXACT_RENDERED_REQUEST
    assert item.reasons == ()
    assert "terminology_hash_mismatch" in item.resource_drift
    assert plan.reasons == {}
    assert plan.resource_drift == {"terminology_hash_mismatch": 1}


def test_reused_translation_accepted_by_current_qa_records_provenance_without_a_provider(
    tmp_path: Path,
) -> None:
    old = _accepted_old_run(tmp_path, [ALPHA_LOCKED])
    new = _successor_run(tmp_path, [ALPHA_LOCKED, OMEGA_IRRELEVANT])
    result = reuse_accepted_translations(new, from_run=old, project_root=tmp_path)

    assert result.published == result.accepted == 1 and result.qa_rejected == 0
    state = load_run_summary(new, project_root=tmp_path)
    job = state.jobs[0]
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.acceptance_origin is TranslationAcceptanceOrigin.REUSE_QA
    assert job.attempt_count == 0
    assert not (state.path / "jobs/001-001/attempts").exists()

    evidence = _reuse_evidence(tmp_path, new)
    compatibility = evidence.rendered_request_compatibility
    assert compatibility is not None
    assert evidence.reuse_engine_version == TRANSLATION_REUSE_ENGINE_VERSION
    assert compatibility.policy_version == REUSE_COMPATIBILITY_POLICY_VERSION
    assert compatibility.tier is ReuseCompatibilityTier.EXACT_RENDERED_REQUEST
    assert compatibility.provider_contacted is False
    assert compatibility.source_snapshot_id != compatibility.target_snapshot_id
    assert compatibility.source_prompt_package_id != compatibility.target_prompt_package_id
    assert compatibility.source_user_message_sha256 == compatibility.target_user_message_sha256
    assert compatibility.source_system_message_sha256 == compatibility.target_system_message_sha256
    assert compatibility.waived_resource_drift == ["terminology_hash_mismatch"]
    assert evidence.source_run_id == old
    assert evidence.source_attempt_number == 1

    assert job.acceptance_file is not None
    acceptance = load_yaml(tmp_path / job.acceptance_file, TranslationAcceptanceEvidence)
    assert acceptance.terminology_qa_version == TERMINOLOGY_QA_VERSION
    assert acceptance.structural_qa_version == STRUCTURAL_QA_VERSION
    assert acceptance.terminology_report.valid and acceptance.structural_report.valid


def test_reused_translation_rejected_by_current_qa_is_retained_without_a_provider_call(
    tmp_path: Path,
) -> None:
    # Enforcement is a QA rule, not a rendered instruction, so tightening it leaves the effective
    # request identical while changing what the current registry accepts as proof.
    relaxed = dict(
        ALPHA_LOCKED,
        target_term="ایرانیان",
        decision_source=DecisionSource.AI,
        locked=False,
        enforcement=TerminologyEnforcement.MORPHOLOGICAL,
    )
    old = _accepted_old_run(tmp_path, [relaxed], translation="مردم ایران.\n\nترجمه دوم.\n")
    new = _successor_run(tmp_path, [dict(relaxed, enforcement=TerminologyEnforcement.EXACT)])

    plan = plan_accepted_translation_reuse(new, from_run=old, project_root=tmp_path)
    assert plan.jobs[0].outcome == "reusable"
    result = reuse_accepted_translations(new, from_run=old, project_root=tmp_path)

    assert result.published == 1 and result.accepted == 0 and result.qa_rejected == 1
    state = load_run_summary(new, project_root=tmp_path)
    job = state.jobs[0]
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert job.acceptance_origin is TranslationAcceptanceOrigin.REUSE_QA
    assert job.reuse_count == 1 and job.attempt_count == 0
    assert not (state.path / "jobs/001-001/attempts").exists()
    assert _reuse_evidence(tmp_path, new).rendered_request_compatibility is not None


def test_qa_reevaluated_acceptance_is_recognized_as_effectively_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stale_qa(
        source: str, translation: str, registry: TerminologyRegistry
    ) -> TerminologyValidationReport:
        return TerminologyValidationReport(
            schema_version="1.0",
            valid=False,
            source_sha256=hashlib.sha256(source.encode()).hexdigest(),
            translation_sha256=hashlib.sha256(translation.encode()).hexdigest(),
            checked_decisions=1,
            findings=[
                TerminologyFinding(
                    code="canonical_term_missing",
                    severity="error",
                    source_term="Alpha",
                    message="synthetic superseded QA regression",
                )
            ],
        )

    synthetic_project(tmp_path)
    _write_registry(tmp_path, [ALPHA_LOCKED])
    old = _rendered_run(tmp_path)
    monkeypatch.setattr("book_translator.translation_worker.validate_terminology", stale_qa)
    run_translation_worker(
        old,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider(ACCEPTED_TRANSLATION),
        now=lambda: NOW + timedelta(seconds=10),
    )
    monkeypatch.undo()
    old_job = load_run_summary(old, project_root=tmp_path).jobs[0]
    assert old_job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED

    reevaluated = reevaluate_translation_job(
        old,
        old_job.job_id,
        1,
        project_root=tmp_path,
        now=lambda: NOW + timedelta(seconds=20),
    )
    assert reevaluated.resulting_status is TranslationAcceptanceStatus.ACCEPTED
    refreshed = load_run_summary(old, project_root=tmp_path).jobs[0]
    assert refreshed.acceptance_origin is TranslationAcceptanceOrigin.QA_REEVALUATION

    new = _successor_run(tmp_path, [ALPHA_LOCKED, OMEGA_IRRELEVANT])
    result = reuse_accepted_translations(new, from_run=old, project_root=tmp_path)
    assert result.published == result.accepted == 1
    evidence = _reuse_evidence(tmp_path, new)
    assert evidence.source_acceptance_origin is TranslationAcceptanceOrigin.QA_REEVALUATION
    assert evidence.source_reevaluation_file is not None
    assert evidence.rendered_request_compatibility is not None


# --- tier 2 and other blocked outcomes ---------------------------------------------------


def test_changed_rendered_request_is_blocked_and_attributed_to_its_framed_section(
    tmp_path: Path,
) -> None:
    old = _accepted_old_run(tmp_path, [ALPHA_LOCKED])
    new = _successor_run(tmp_path, [dict(ALPHA_LOCKED, target_term="آلفای دوم")])

    plan = plan_accepted_translation_reuse(new, from_run=old, project_root=tmp_path)
    item = plan.jobs[0]
    assert item.outcome == "blocked"
    assert item.tier is ReuseCompatibilityTier.INCOMPATIBLE
    assert "user_message_hash_mismatch" in item.reasons
    assert "effective_glossary_data_changed" in item.reasons
    assert "terminology_hash_mismatch" in item.resource_drift
    assert not (tmp_path / "workspace/translation-runs" / new / "jobs/001-001/reuse").exists()


def test_locked_user_canonical_change_reaching_a_job_is_never_waived(tmp_path: Path) -> None:
    old = _accepted_old_run(tmp_path, [ALPHA_LOCKED])
    new = _successor_run(tmp_path, [dict(ALPHA_LOCKED, target_term="الفا", replace_locked=True)])

    result = reuse_accepted_translations(new, from_run=old, project_root=tmp_path)
    item = result.plan.jobs[0]
    assert result.published == 0 and item.outcome == "blocked"
    assert "user_message_hash_mismatch" in item.reasons
    assert "effective_glossary_data_changed" in item.reasons


def test_historically_unaccepted_job_is_blocked(tmp_path: Path) -> None:
    old = _accepted_old_run(tmp_path, [ALPHA_LOCKED], translation="ترجمه نخست.\n\nترجمه دوم.\n")
    assert (
        load_run_summary(old, project_root=tmp_path).jobs[0].acceptance_status
        is TranslationAcceptanceStatus.QA_REJECTED
    )
    new = _successor_run(tmp_path, [ALPHA_LOCKED, OMEGA_IRRELEVANT])

    result = reuse_accepted_translations(new, from_run=old, project_root=tmp_path)
    item = result.plan.jobs[0]
    assert result.published == 0 and item.outcome == "blocked"
    assert item.reasons == ("historical_not_effectively_accepted",)


def test_missing_rendered_request_bytes_block_the_identity_waiver(tmp_path: Path) -> None:
    old = _accepted_old_run(tmp_path, [ALPHA_LOCKED])
    new = _successor_run(tmp_path, [ALPHA_LOCKED, OMEGA_IRRELEVANT])
    (tmp_path / "workspace/translation-runs" / old / "jobs/001-001/prompt-package/user.md").unlink()

    item = plan_accepted_translation_reuse(new, from_run=old, project_root=tmp_path).jobs[0]
    assert item.outcome == "blocked"
    assert item.reasons == ("historical_rendered_request_unavailable",)


# --- immutability ------------------------------------------------------------------------


def test_dry_run_mutates_nothing_and_leaves_history_and_resources_untouched(
    tmp_path: Path,
) -> None:
    old = _accepted_old_run(tmp_path, [ALPHA_LOCKED])
    new = _successor_run(tmp_path, [ALPHA_LOCKED, OMEGA_IRRELEVANT])
    before = _bytes(tmp_path)

    result = reuse_accepted_translations(new, from_run=old, project_root=tmp_path, dry_run=True)

    assert result.dry_run and result.plan.reusable == 1
    assert result.published == result.accepted == result.qa_rejected == 0
    assert _bytes(tmp_path) == before


def test_real_reuse_leaves_the_historical_run_and_frozen_resources_byte_identical(
    tmp_path: Path,
) -> None:
    old = _accepted_old_run(tmp_path, [ALPHA_LOCKED])
    new = _successor_run(tmp_path, [ALPHA_LOCKED, OMEGA_IRRELEVANT])
    old_before = _bytes(tmp_path / "workspace/translation-runs" / old)
    config_before = _bytes(tmp_path / "config")
    snapshots_before = _bytes(tmp_path / "workspace/snapshots")

    reuse_accepted_translations(new, from_run=old, project_root=tmp_path)

    assert _bytes(tmp_path / "workspace/translation-runs" / old) == old_before
    assert _bytes(tmp_path / "config") == config_before
    assert _bytes(tmp_path / "workspace/snapshots") == snapshots_before


def test_exact_compatible_acceptance_is_reused_with_current_qa_and_no_provider_attempt(
    tmp_path: Path,
) -> None:
    old = _prepared_run(tmp_path)
    run_translation_worker(
        old,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("ترجمه نخست.\n\nترجمه دوم.\n"),
        now=lambda: NOW + timedelta(seconds=10),
    )
    old_state = load_run_summary(old, project_root=tmp_path)
    created = create_run(
        plan_run(old_state.run.snapshot_id, project_root=tmp_path, now=lambda: NOW)
    )
    execute_prompt_packages(
        plan_prompt_packages(created.run_id, project_root=tmp_path, now=lambda: NOW)
    )
    new = created.run_id
    old_path = tmp_path / "workspace/translation-runs" / old
    old_before = _bytes(old_path)

    dry = reuse_accepted_translations(new, from_run=old, project_root=tmp_path, dry_run=True)
    assert dry.plan.reusable == 1 and dry.published == 0
    assert dry.plan.resource_drift == {}
    assert not (tmp_path / "workspace/translation-runs" / new / "jobs/001-001/reuse").exists()

    result = reuse_accepted_translations(new, from_run=old, project_root=tmp_path)
    state = load_run_summary(new, project_root=tmp_path)
    job = state.jobs[0]
    assert result.published == result.accepted == 1
    assert job.attempt_count == 0 and job.successful_attempt is None
    assert job.reuse_count == 1
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.acceptance_origin is TranslationAcceptanceOrigin.REUSE_QA
    assert state.run.accepted_count == 1
    assert state.run.provider_succeeded_unassessed_count == 0
    assert not (state.path / "jobs/001-001/attempts").exists()
    assert _bytes(old_path) == old_before

    duplicate = reuse_accepted_translations(new, from_run=old, project_root=tmp_path)
    assert duplicate.published == 0 and duplicate.plan.skipped == 1
