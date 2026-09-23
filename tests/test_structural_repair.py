"""Deterministic structural repair: the engine, the boundary, and the finalization ladder.

The pure tests state what the engine may and may not conclude from two texts; the workspace tests
state what may reach a queue as a result. Everything is synthetic — the Persian below is
placeholder prose long enough for the stemmer's minimum stem, never book content.
"""

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.final_qa import (
    MAX_ESCALATED_REPAIR_ATTEMPTS,
    finalize_translation_run,
    plan_final_qa_audit,
)
from book_translator.models import (
    FinalQAVerdict,
    ProviderErrorCategory,
    ProviderName,
    StructuralDefectClass,
    StructuralOperationKind,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationJob,
    TranslationRepairEscalation,
    TranslationRunStatus,
    TranslationStructuralRepairEvidence,
)
from book_translator.provider_execution import (
    ProviderExecutionError,
    ProviderPreflightReport,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from book_translator.provider_execution.errors import ErrorClassification
from book_translator.structural_repair import (
    MIN_ALIGNMENT_MARGIN,
    diagnose_structure,
    lines,
    plan_structural_repair,
    repair_translation_structure,
)
from book_translator.structural_repair.workflow import StructuralRepairError
from book_translator.translation_auto_repair import auto_repair_job
from book_translator.translation_queue import load_run_summary
from book_translator.translation_reevaluation import (
    TranslationReevaluationError,
    reevaluate_translation_job,
)
from tests.test_final_qa import (
    CANONICAL,
    SOURCE,
    _prepared,
    _registry,
    _rejected_run,
    _translate,
)
from tests.test_translation_auto_repair import ScriptedProvider
from tests.test_translation_queue import NOW
from tests.test_translation_worker import _queue_bytes, _run_path

runner = CliRunner()

# ``SOURCE`` is ``Alpha beta.\n\nGamma delta.\n``: two single-line blocks, one governed term.
# The target blocks below are proportioned like their source blocks on purpose: the engine
# refuses an alignment it cannot prove, so a fixture that is genuinely ambiguous tests the
# refusal rather than the repair, and both deserve their own tests.
FIRST = f"{CANONICAL} و بتا"
SECOND = "گاما دلتا اینجاست"
MATCHED = f"{FIRST}.\n\n{SECOND}.\n"
#: One source line written as two translation lines.
SPLIT = f"{CANONICAL}\n\nو بتا.\n\n{SECOND}.\n"
#: Two source blocks written as two lines of one block: only the blank line went missing.
SEPARATOR_LOST = f"{FIRST}.\n{SECOND}.\n"
#: Two source lines genuinely fused into one line of prose.
SEMANTIC_MERGE = f"{FIRST} و {SECOND}.\n"

# A three-line source block: a heading whose lines are separated by plain line breaks, which is
# the shape a provider most often rewrites into three paragraphs.
HEADING_SOURCE = "CHAPTER ONE\nThe Opening\nA subtitle line\n\nAlpha beta.\n\nGamma delta.\n"


def _content(text: str) -> str:
    """Return *text* with every run of whitespace removed, which is what a repair must preserve."""
    return "".join(text.split())


# --------------------------------------------------------------------------------------------
# The pure engine
# --------------------------------------------------------------------------------------------


def test_an_accidental_paragraph_split_is_deterministically_merged() -> None:
    diagnosis = diagnose_structure(SOURCE, SPLIT)

    assert diagnosis.defect_class is StructuralDefectClass.TRANSLATION_LINE_SPLIT
    assert diagnosis.paragraph_delta == 1
    assert diagnosis.deterministic
    assert [group.kind for group in diagnosis.operations] == [
        StructuralOperationKind.JOIN_TRANSLATION_LINES
    ]
    assert diagnosis.repaired_text == MATCHED


def test_an_accidental_paragraph_merge_restores_the_missing_separator() -> None:
    # Both source blocks are present as lines; only the blank line between them was dropped.
    diagnosis = diagnose_structure(SOURCE, SEPARATOR_LOST)

    assert diagnosis.defect_class is StructuralDefectClass.BLANK_LINE_GEOMETRY
    assert diagnosis.paragraph_delta == -1
    assert diagnosis.deterministic
    assert not diagnosis.operations
    assert diagnosis.repaired_text == MATCHED


def test_a_blank_line_run_is_absorbed_by_segmentation_and_needs_no_repair() -> None:
    diagnosis = diagnose_structure(SOURCE, f"{FIRST}.\n\n\n\n   \n\n{SECOND}.\n")

    assert diagnosis.blank_line_artifact
    assert diagnosis.matched
    assert diagnosis.defect_class is StructuralDefectClass.NONE
    assert not diagnosis.deterministic


def test_a_blank_line_artifact_beside_a_real_split_is_still_repaired() -> None:
    translation = f"{CANONICAL}\n\n\n\nو بتا.\n\n{SECOND}.\n"
    diagnosis = diagnose_structure(SOURCE, translation)

    assert diagnosis.blank_line_artifact
    assert diagnosis.deterministic
    assert diagnosis.repaired_text == MATCHED


def test_a_dialogue_opening_split_off_into_its_own_block_is_rejoined() -> None:
    source = (
        'He spoke. "Alpha beta gamma delta epsilon," she said in reply to him.\n\nGamma delta.\n'
    )
    translation = f"«سلام»\n\nاو گفت {CANONICAL} و سپس ادامه داد و چیز دیگری نگفت.\n\n{SECOND}.\n"
    diagnosis = diagnose_structure(source, translation)

    assert diagnosis.defect_class is StructuralDefectClass.TRANSLATION_LINE_SPLIT
    assert diagnosis.deterministic
    assert diagnosis.repaired_text is not None
    assert diagnosis.repaired_text.count("\n\n") == 1


def test_a_scene_separator_keeps_its_own_block() -> None:
    source = "Alpha beta.\n\n* * *\n\nGamma delta.\n"
    translation = f"{FIRST}.\n* * *\n{SECOND}.\n"
    diagnosis = diagnose_structure(source, translation)

    assert diagnosis.defect_class is StructuralDefectClass.BLANK_LINE_GEOMETRY
    assert diagnosis.deterministic
    assert diagnosis.repaired_text == f"{FIRST}.\n\n* * *\n\n{SECOND}.\n"


def test_a_multi_line_source_block_keeps_its_own_line_breaks() -> None:
    translation = f"فصل یک\n\nگشایش\n\nیک زیرعنوان\n\n{FIRST}.\n\n{SECOND}.\n"
    diagnosis = diagnose_structure(HEADING_SOURCE, translation)

    assert diagnosis.defect_class is StructuralDefectClass.BLANK_LINE_GEOMETRY
    assert diagnosis.deterministic
    assert diagnosis.repaired_text == (f"فصل یک\nگشایش\nیک زیرعنوان\n\n{FIRST}.\n\n{SECOND}.\n")


def test_omitted_content_is_never_repaired_deterministically() -> None:
    diagnosis = diagnose_structure(SOURCE + "\nEpsilon zeta.\n", f"{FIRST}.\n")

    assert not diagnosis.deterministic
    assert diagnosis.defect_class in {
        StructuralDefectClass.TRANSLATION_LINE_MERGE,
        StructuralDefectClass.CONTENT_DIVERGENCE,
        StructuralDefectClass.LINE_COUNT_DIVERGENCE,
    }
    assert diagnosis.repaired_text is None
    assert diagnosis.refusals


def test_semantically_merged_source_lines_require_a_provider() -> None:
    diagnosis = diagnose_structure(SOURCE, SEMANTIC_MERGE)

    assert diagnosis.defect_class is StructuralDefectClass.TRANSLATION_LINE_MERGE
    assert not diagnosis.deterministic
    assert "translation_merged_source_lines" in diagnosis.refusals
    assert any(
        group.kind is StructuralOperationKind.SPLIT_TRANSLATION_LINE
        for group in diagnosis.operations
    )


def test_a_deterministic_repair_changes_whitespace_and_nothing_else() -> None:
    translation = SPLIT
    diagnosis = diagnose_structure(SOURCE, translation)

    assert diagnosis.repaired_text is not None
    assert _content(diagnosis.repaired_text) == _content(translation)
    assert re.sub(r"[^\s]", "", diagnosis.repaired_text) != re.sub(r"[^\s]", "", translation)


def test_a_wide_divergence_fails_closed_rather_than_guessing() -> None:
    source = "\n\n".join(f"Source line number {index} here." for index in range(12)) + "\n"
    translation = "\n\n".join(f"سطر شمارهٔ {index} اینجاست." for index in range(4)) + "\n"
    diagnosis = diagnose_structure(source, translation)

    assert not diagnosis.deterministic
    assert diagnosis.defect_class is StructuralDefectClass.CONTENT_DIVERGENCE


def test_an_ambiguous_alignment_is_refused_instead_of_picked() -> None:
    # Four interchangeable source lines and five identical target lines: no evidence anywhere
    # says which target line was split off, and the engine must not invent one.
    source = "\n\n".join(["Alpha beta gamma delta."] * 4) + "\n"
    translation = "\n\n".join(["واژهٔ یکسان اینجا."] * 5) + "\n"
    diagnosis = diagnose_structure(source, translation)

    assert not diagnosis.deterministic
    assert "alignment_is_not_uniquely_best" in diagnosis.refusals
    assert diagnosis.alignment_margin_milli is not None
    assert diagnosis.alignment_margin_milli < round(1000 * MIN_ALIGNMENT_MARGIN)


def test_line_segmentation_records_the_block_each_line_belongs_to() -> None:
    segmented = lines(HEADING_SOURCE)

    assert [item.block_index for item in segmented] == [0, 0, 0, 1, 2]
    assert [item.starts_block for item in segmented] == [True, False, False, True, True]


# --------------------------------------------------------------------------------------------
# The workspace boundary
# --------------------------------------------------------------------------------------------


def _clock(offset: int) -> Callable[[], datetime]:
    return lambda: NOW + timedelta(seconds=offset)


def _split_run(root: Path, *, variants: tuple[str, ...] = ()) -> str:
    """A run whose single job was rejected for a repairable separator defect."""
    return _rejected_run(root, _registry(variants=variants), SPLIT)


def _job(root: Path, run_id: str) -> TranslationJob:
    return load_run_summary(run_id, project_root=root).jobs[0]


def test_a_deterministic_repair_is_published_adopted_and_accepted(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _split_run(root)
    before = _job(root, run_id)
    assert before.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED

    result = repair_translation_structure(
        run_id, before.job_id, project_root=root, dry_run=False, now=_clock(100)
    )

    assert result.applied
    assert result.provider_calls == 0
    assert result.resulting_status is TranslationAcceptanceStatus.ACCEPTED
    after = _job(root, run_id)
    assert after.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert after.acceptance_origin is TranslationAcceptanceOrigin.STRUCTURAL_REPAIR
    assert after.structural_repair_count == 1
    assert after.latest_structural_repair_file is not None
    assert after.provider_attempt_count == before.provider_attempt_count


def test_the_repair_reruns_both_validators_over_the_repaired_bytes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _split_run(root)
    job = _job(root, run_id)

    plan = plan_structural_repair(run_id, job.job_id, project_root=root, now=_clock(100))

    assert plan.evidence is not None
    evidence = plan.evidence
    assert evidence.terminology_report.translation_sha256 == evidence.translation_sha256
    assert evidence.structural_report.translation_sha256 == evidence.translation_sha256
    assert evidence.structural_report.source_paragraph_count == (
        evidence.structural_report.translation_paragraph_count
    )
    assert "paragraph_count_mismatch" in evidence.superseded_blocking_codes


def test_the_historical_attempt_and_its_acceptance_stay_byte_identical(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _split_run(root)
    job = _job(root, run_id)
    attempts = _run_path(root, run_id) / "jobs/001-001/attempts"
    acceptance = _run_path(root, run_id) / "jobs/001-001/acceptance"
    before = {
        path: path.read_bytes()
        for path in (*attempts.rglob("*"), *acceptance.rglob("*"))
        if path.is_file()
    }

    repair_translation_structure(
        run_id, job.job_id, project_root=root, dry_run=False, now=_clock(100)
    )

    assert {path: path.read_bytes() for path in before} == before
    assert job.acceptance_file is not None
    after = _job(root, run_id)
    assert after.acceptance_file == job.acceptance_file
    assert after.accepted_attempt == job.accepted_attempt


def test_a_structural_repair_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _split_run(root)
    job = _job(root, run_id)
    before = _queue_bytes(root, run_id)

    result = repair_translation_structure(
        run_id, job.job_id, project_root=root, dry_run=True, now=_clock(100)
    )

    assert not result.applied
    assert result.plan.applicable
    assert result.plan.projected_status is TranslationAcceptanceStatus.ACCEPTED
    assert _queue_bytes(root, run_id) == before
    assert not (_run_path(root, run_id) / "jobs/001-001/structural-repair").exists()


def test_a_second_structural_repair_of_the_same_job_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _split_run(root)
    job = _job(root, run_id)
    repair_translation_structure(
        run_id, job.job_id, project_root=root, dry_run=False, now=_clock(100)
    )

    with pytest.raises(StructuralRepairError, match="QA-rejected"):
        repair_translation_structure(
            run_id, job.job_id, project_root=root, dry_run=False, now=_clock(200)
        )


def test_a_content_defect_is_refused_at_the_boundary_too(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _rejected_run(root, _registry(), SEMANTIC_MERGE)
    job = _job(root, run_id)

    result = repair_translation_structure(
        run_id, job.job_id, project_root=root, dry_run=False, now=_clock(100)
    )

    assert not result.applied
    assert "translation_merged_source_lines" in result.plan.refusals
    assert _job(root, run_id).acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert _job(root, run_id).structural_repair_count == 0


def test_a_repaired_job_is_no_longer_re_evaluated_against_its_superseded_draft(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _split_run(root)
    job = _job(root, run_id)
    repair_translation_structure(
        run_id, job.job_id, project_root=root, dry_run=False, now=_clock(100)
    )

    with pytest.raises(TranslationReevaluationError, match="structural repair"):
        reevaluate_translation_job(run_id, job.job_id, 1, project_root=root, now=_clock(200))


# --------------------------------------------------------------------------------------------
# Terminology enforcement across a structural repair
# --------------------------------------------------------------------------------------------


def test_a_terminology_finding_that_only_broken_alignment_caused_clears(tmp_path: Path) -> None:
    """Occurrence-anchored terminology QA fails closed without a block correspondence.

    The rival rendering sits in the block that translates the governed occurrence, and the
    canonical sits in a block that the split has pushed out of alignment. Once the blocks
    correspond again the validator can see which occurrence is which, and decides normally.
    """
    root = tmp_path / "project"
    run_id = _prepared(root, _registry(variants=("پاسدارها",)))
    _translate(root, run_id, (f"{CANONICAL}\n\nو بتا.\n\nپاسدارها و گاما دلتا.\n",))
    job = _job(root, run_id)
    plan = plan_final_qa_audit(run_id, project_root=root, now=_clock(50))
    before = plan.report.jobs[0]
    assert "paragraph_count_mismatch" in before.current_blocking_codes

    result = repair_translation_structure(
        run_id, job.job_id, project_root=root, dry_run=False, now=_clock(100)
    )

    assert result.applied
    assert result.plan.evidence is not None
    # Whatever terminology QA now says, it said it about aligned blocks and it is recorded.
    assert result.plan.evidence.terminology_report.translation_sha256 == (
        result.plan.evidence.translation_sha256
    )
    assert "paragraph_count_mismatch" in result.plan.resolved_blocking_codes


def test_a_structural_repair_never_publishes_a_new_blocking_finding(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _split_run(root)
    job = _job(root, run_id)

    plan = plan_structural_repair(run_id, job.job_id, project_root=root, now=_clock(100))

    assert plan.evidence is not None
    introduced = (
        {
            finding.code
            for finding in plan.evidence.terminology_report.findings
            if finding.severity == "error"
        }
        | {
            finding.code
            for finding in plan.evidence.structural_report.findings
            if finding.severity == "error"
        }
    ) - set(plan.evidence.superseded_blocking_codes)
    assert not introduced
    assert not plan.persisting_blocking_codes


def test_a_repair_that_would_introduce_a_finding_cannot_be_published(tmp_path: Path) -> None:
    """The adoption guard is a contract, not a comment: an evidence record cannot state it."""
    root = tmp_path / "project"
    run_id = _split_run(root)
    job = _job(root, run_id)
    plan = plan_structural_repair(run_id, job.job_id, project_root=root, now=_clock(100))
    assert plan.evidence is not None
    payload = plan.evidence.model_dump()
    payload["superseded_blocking_codes"] = []
    payload["structural_report"]["findings"] = [
        {"code": "tab", "severity": "error", "message": "forged blocking finding"}
    ]
    payload["structural_report"]["valid"] = False
    payload["accepted"] = False
    payload["resulting_status"] = TranslationAcceptanceStatus.QA_REJECTED.value

    with pytest.raises(ValueError, match="new blocking finding"):
        TranslationStructuralRepairEvidence.model_validate(payload)


# --------------------------------------------------------------------------------------------
# The finalization ladder
# --------------------------------------------------------------------------------------------


def test_finalization_resolves_a_separator_defect_without_a_provider(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _split_run(root)

    result = finalize_translation_run(run_id, project_root=root, dry_run=False, now=_clock(100))

    assert result.report.provider_calls == 0
    assert result.report.structural_repairs_applied == 1
    outcome = result.report.outcomes[0]
    assert outcome.verdict is FinalQAVerdict.REPAIR_STRUCTURE
    assert outcome.action == "structurally_repaired"
    assert outcome.acceptance_status_after == TranslationAcceptanceStatus.ACCEPTED.value
    assert result.report.qa_rejected_count == 0


def test_a_finalization_dry_run_names_the_step_it_would_take(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _split_run(root)
    before = _queue_bytes(root, run_id)

    result = finalize_translation_run(
        run_id, project_root=root, dry_run=True, provider=ProviderName.FAKE, now=_clock(100)
    )

    outcome = result.report.outcomes[0]
    assert outcome.planned_action == "repair_structure"
    assert outcome.action == "none"
    assert result.report.provider_calls == 0
    assert result.report.structural_repairs_applied == 0
    assert _queue_bytes(root, run_id) == before
    assert not (_run_path(root, run_id) / "jobs/001-001/structural-repair").exists()


def test_a_structural_repair_and_a_provider_repair_compose(tmp_path: Path) -> None:
    """The free rung runs first and the provider only answers what it could not resolve."""
    root = tmp_path / "project"
    run_id = _prepared(root, _registry())
    # Split blocks *and* a missing canonical: only the first half is free to fix.
    _translate(root, run_id, (f"واژهٔ دیگر\n\nو بتا.\n\n{SECOND}.\n",))

    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        provider_factory=lambda _job: ScriptedProvider((MATCHED,)),
        now=_clock(200),
    )

    outcome = result.report.outcomes[0]
    assert result.report.structural_repairs_applied == 1
    assert outcome.provider_attempts_spent == 1
    assert outcome.action == "repaired"
    assert outcome.acceptance_status_after == TranslationAcceptanceStatus.ACCEPTED.value


def test_no_accepted_job_regresses_when_a_sibling_is_structurally_repaired(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _prepared(root, _registry(), max_attempts=3)
    _translate(root, run_id, (SPLIT,))
    summary = load_run_summary(run_id, project_root=root)
    accepted = [
        job for job in summary.jobs if job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    ]
    frozen = {job.job_id: job.model_dump_json() for job in accepted}

    finalize_translation_run(run_id, project_root=root, dry_run=False, now=_clock(300))

    after = {
        job.job_id: job.model_dump_json()
        for job in load_run_summary(run_id, project_root=root).jobs
        if job.job_id in frozen
    }
    assert after == frozen


def test_a_run_reaches_completed_only_when_every_job_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _split_run(root)

    result = finalize_translation_run(run_id, project_root=root, dry_run=False, now=_clock(100))

    assert result.report.qa_rejected_count == 0
    assert result.report.accepted_count == result.report.audit.job_count
    assert result.report.run_status_after == TranslationRunStatus.COMPLETED.value


# --------------------------------------------------------------------------------------------
# The bounded exceptional escalation
# --------------------------------------------------------------------------------------------


def _exhausted_content_defect(root: Path) -> str:
    """A run whose only job has spent every attempt on a defect no free remedy can reach."""
    run_id = _prepared(root, _registry())
    for index in range(4):
        _translate(root, run_id, (SEMANTIC_MERGE,), offset=10 + index * 10)
        job = _job(root, run_id)
        if job.provider_attempt_count >= job.max_attempts:
            return run_id
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
            provider_factory=lambda _job: ScriptedProvider((SEMANTIC_MERGE,)),
            now=_clock(200 + index * 10),
        )
    raise AssertionError("the synthetic job never exhausted its provider budget")


def test_an_exhausted_content_defect_is_escalated_with_its_structural_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _exhausted_content_defect(root)
    plan = plan_final_qa_audit(run_id, project_root=root, now=_clock(50))
    assert plan.report.jobs[0].verdict is FinalQAVerdict.EXHAUSTED

    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        allow_repair_escalation=True,
        provider_factory=lambda _job: ScriptedProvider((MATCHED,)),
        now=_clock(400),
    )

    outcome = result.report.outcomes[0]
    assert outcome.escalation_granted
    assert outcome.action == "escalated_and_repaired"
    assert result.report.escalations_granted == 1
    record = TranslationRepairEscalation.model_validate(
        yaml.safe_load(
            (
                _run_path(root, run_id) / "jobs/001-001/repair-escalation/001/escalation.yaml"
            ).read_text(encoding="utf-8")
        )
    )
    assert record.provider_contacted is False
    assert not record.deterministic_repair_available
    assert record.structural_diagnosis is not None
    assert record.structural_diagnosis.defect_class is (
        StructuralDefectClass.TRANSLATION_LINE_MERGE
    )
    assert record.granted_attempts == MAX_ESCALATED_REPAIR_ATTEMPTS
    assert record.resulting_max_attempts == record.original_max_attempts + 1
    assert record.authorized_attempt_number == record.provider_attempts_used + 1
    assert "paragraph_count_mismatch" in record.triggering_codes


def test_an_escalation_never_resets_the_attempt_counters(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _exhausted_content_defect(root)
    before = _job(root, run_id)

    finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        allow_repair_escalation=True,
        provider_factory=lambda _job: ScriptedProvider((MATCHED,)),
        now=_clock(400),
    )

    after = _job(root, run_id)
    assert after.max_attempts == before.max_attempts + MAX_ESCALATED_REPAIR_ATTEMPTS
    assert after.attempt_count == before.attempt_count + 1
    assert after.abandoned_attempt_count == before.abandoned_attempt_count


def test_an_escalation_is_never_granted_while_a_free_remedy_remains(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _prepared(root, _registry())
    _translate(root, run_id, (SPLIT,))

    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        allow_repair_escalation=True,
        provider_factory=lambda _job: ScriptedProvider((MATCHED,)),
        now=_clock(400),
    )

    assert result.report.escalations_granted == 0
    assert result.report.provider_calls == 0
    assert result.report.structural_repairs_applied == 1
    assert not (_run_path(root, run_id) / "jobs/001-001/repair-escalation").exists()


def test_an_unauthorized_escalation_is_reported_and_nothing_is_spent(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _exhausted_content_defect(root)
    before = _queue_bytes(root, run_id)

    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        provider_factory=lambda _job: ScriptedProvider((MATCHED,)),
        now=_clock(400),
    )

    assert result.report.escalations_granted == 0
    assert result.report.provider_calls == 0
    assert result.report.outcomes[0].action == "reported"
    assert "escalation_not_authorized" in result.report.outcomes[0].reasons
    assert _queue_bytes(root, run_id) == before


# --------------------------------------------------------------------------------------------
# Provenance and the CLI
# --------------------------------------------------------------------------------------------


def test_the_published_evidence_carries_the_whole_derivation(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _split_run(root)
    job = _job(root, run_id)
    result = repair_translation_structure(
        run_id, job.job_id, project_root=root, dry_run=False, now=_clock(100)
    )

    assert result.evidence_file is not None
    record = TranslationStructuralRepairEvidence.model_validate(
        yaml.safe_load((root / result.evidence_file).read_text(encoding="utf-8"))
    )
    assert record.provider_contacted is False
    assert record.content_preserved is True
    assert record.superseded_evidence_file == job.acceptance_file
    assert record.attempt_number == job.accepted_attempt
    assert record.original_translation_sha256 != record.translation_sha256
    assert record.diagnosis.deterministic
    assert record.diagnosis.paragraph_delta == 1
    assert record.diagnosis.line_delta == 1
    assert record.terminology_qa_version and record.structural_qa_version
    assert result.translation_file is not None
    published = (root / result.translation_file).read_text(encoding="utf-8")
    original = (root / record.original_translation_file).read_text(encoding="utf-8")
    assert _content(published) == _content(original)


def test_the_cli_reports_the_structural_diagnosis_without_printing_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    run_id = _split_run(root)
    monkeypatch.chdir(root)

    audited = runner.invoke(app, ["audit-final-qa", run_id, "--dry-run"])
    finalized = runner.invoke(app, ["finalize-translation-run", run_id, "--dry-run"])

    assert audited.exit_code == 0, audited.output
    assert finalized.exit_code == 0, finalized.output
    assert "Structural Diagnosis" in audited.output
    assert "repair_structure" in audited.output
    assert "Structural repairs" in finalized.output
    for output in (audited.output, finalized.output):
        assert CANONICAL not in output
        assert SECOND not in output
        assert "Gamma delta" not in output


# --------------------------------------------------------------------------------------------
# Provider recovery inside the ladder
# --------------------------------------------------------------------------------------------


class _FailThenSucceedProvider:
    """Fail the first repair the way a rate limit does, then deliver the second."""

    provider_name = "fake"
    adapter_version = "test-1.0.0"

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[str] = []

    def execute(self, request: ProviderRequest) -> ProviderResult:
        self.calls.append(request.job_id)
        if len(self.calls) == 1:
            raise ProviderExecutionError(
                ErrorClassification(
                    ProviderErrorCategory.RATE_LIMIT,
                    False,
                    False,
                    False,
                    "synthetic repair failure",
                )
            )
        return ProviderResult(
            response_id="synthetic-response",
            model=request.model or "fake-v1",
            status="completed",
            text_outputs=(self.text,),
            usage=ProviderUsage(10, 10, 20),
            sanitized_response={"status": "completed"},
        )


def _healthy_preflight(provider: ProviderName) -> ProviderPreflightReport:
    return ProviderPreflightReport(
        provider=provider.value,
        supported=True,
        passed=True,
        details=(("authenticated", "yes"),),
    )


def _advancing(start: int, step: int = 300) -> Callable[[], datetime]:
    """A clock that moves forward on every read, so a failure cooldown can actually elapse."""
    ticks = iter(range(start, start + 100_000, step))
    return lambda: NOW + timedelta(seconds=next(ticks))


def test_a_repair_the_provider_fails_to_deliver_is_recovered_and_re_sent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _prepared(root, _registry())
    # Structurally sound, but the canonical is missing: a provider repair and nothing else.
    _translate(root, run_id, (f"واژهٔ دیگر و بتا.\n\n{SECOND}.\n",))
    provider = _FailThenSucceedProvider(MATCHED)

    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        allow_provider_recovery=True,
        provider_factory=lambda _job: provider,
        preflight=_healthy_preflight,
        now=_advancing(1000),
    )

    outcome = result.report.outcomes[0]
    assert outcome.provider_recovery_authorized
    assert outcome.action == "recovered_and_repaired"
    assert outcome.acceptance_status_after == TranslationAcceptanceStatus.ACCEPTED.value
    assert len(provider.calls) == 2
    assert result.report.provider_recovery_allowed


def test_a_failed_repair_is_reported_when_recovery_is_switched_off(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _prepared(root, _registry())
    # Structurally sound, but the canonical is missing: a provider repair and nothing else.
    _translate(root, run_id, (f"واژهٔ دیگر و بتا.\n\n{SECOND}.\n",))
    provider = _FailThenSucceedProvider(MATCHED)

    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        allow_provider_recovery=False,
        provider_factory=lambda _job: provider,
        now=_advancing(1000),
    )

    outcome = result.report.outcomes[0]
    assert not outcome.provider_recovery_authorized
    assert outcome.acceptance_status_after == TranslationAcceptanceStatus.QA_REJECTED.value
    assert "provider_recovery_required" in outcome.reasons
    assert len(provider.calls) == 1


def test_a_dry_run_says_it_would_recover_without_touching_anything(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _prepared(root, _registry())
    # Structurally sound, but the canonical is missing: a provider repair and nothing else.
    _translate(root, run_id, (f"واژهٔ دیگر و بتا.\n\n{SECOND}.\n",))
    provider = _FailThenSucceedProvider(MATCHED)
    finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=False,
        provider=ProviderName.FAKE,
        model="fake-v1",
        allow_provider_recovery=False,
        provider_factory=lambda _job: provider,
        now=_advancing(1000),
    )
    before = _queue_bytes(root, run_id)

    result = finalize_translation_run(
        run_id,
        project_root=root,
        dry_run=True,
        provider=ProviderName.FAKE,
        model="fake-v1",
        provider_factory=lambda _job: provider,
        now=_advancing(5000),
    )

    assert result.report.outcomes[0].planned_action == "recover_provider_failure"
    assert result.report.provider_calls == 0
    assert len(provider.calls) == 1
    assert _queue_bytes(root, run_id) == before
