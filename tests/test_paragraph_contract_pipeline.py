"""The numbered paragraph contract end to end, and the boundaries it is allowed to change."""

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from book_translator.models import (
    TranslationAcceptanceStatus,
    TranslationAttempt,
    TranslationJob,
    TranslationJobStatus,
)
from book_translator.paragraph_alignment.workflow import (
    execute_alignment_audit,
    plan_alignment_audit,
)
from book_translator.paragraph_markers import mark_paragraphs, marker_for
from book_translator.prompt_packaging import (
    MARKED_OUTPUT_CONTRACT_VERSION,
    OUTPUT_CONTRACT_VERSION,
    execute_prompt_packages,
    plan_prompt_packages,
)
from book_translator.prompt_packaging.rendering import MARKED_SYSTEM_MESSAGE, SYSTEM_MESSAGE
from book_translator.structural_repair import plan_structural_repair
from book_translator.translation_queue import create_run, load_run_summary, plan_run
from book_translator.translation_repair import (
    MARKED_REPAIR_PROMPT_VERSION,
    REPAIR_PROMPT_VERSION,
    build_repair_request,
)
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import run_translation_worker
from tests.test_translation_auto_repair import ScriptedProvider
from tests.test_translation_queue import NOW, SNAPSHOT_NOW
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import _options, _run_path

FIRST = "آلفا بتا."
SECOND = "گاما دلتا."
MARKED_ANSWER = f"{marker_for(1)}\n{FIRST}\n\n{marker_for(2)}\n{SECOND}\n"
PLAIN_ANSWER = f"{FIRST}\n\n{SECOND}\n"


def _clock(offset: int) -> Callable[[], datetime]:
    return lambda: NOW + timedelta(seconds=offset)


def _run(root: Path, *, marked: bool) -> str:
    root.mkdir(parents=True, exist_ok=True)
    synthetic_project(root)
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    created = create_run(
        plan_run(
            snapshot.snapshot_id,
            project_root=root,
            output_contract_version=(
                MARKED_OUTPUT_CONTRACT_VERSION if marked else OUTPUT_CONTRACT_VERSION
            ),
            now=lambda: NOW,
        )
    )
    execute_prompt_packages(
        plan_prompt_packages(created.run_id, project_root=root, now=lambda: NOW)
    )
    return created.run_id


def _translate(root: Path, run_id: str, answer: str, *, offset: int = 10) -> None:
    run_translation_worker(
        run_id,
        project_root=root,
        options=_options(max_jobs=1),
        provider_factory=lambda _job: ScriptedProvider((answer,)),
        now=_clock(offset),
    )


def _job(root: Path, run_id: str) -> TranslationJob:
    return load_run_summary(run_id, project_root=root).jobs[0]


def _package_files(root: Path, run_id: str) -> tuple[str, str]:
    directory = _run_path(root, run_id) / "jobs/001-001/prompt-package"
    return (
        (directory / "system.md").read_text(encoding="utf-8"),
        (directory / "user.md").read_text(encoding="utf-8"),
    )


# --------------------------------------------------------------------------------------------
# What a marked run sends
# --------------------------------------------------------------------------------------------


def test_a_marked_run_sends_marked_source_and_unmissable_rules(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root, marked=True)

    system, user = _package_files(root, run_id)
    assert system == MARKED_SYSTEM_MESSAGE
    assert "[[P0001]]\nAlpha beta." in user
    assert "[[P0002]]\nGamma delta." in user
    assert f'<output-contract name="{MARKED_OUTPUT_CONTRACT_VERSION}" markers="2"' in user
    assert "Never merge two source paragraphs" in user
    run = load_run_summary(run_id, project_root=root).run
    assert run.output_contract_version == MARKED_OUTPUT_CONTRACT_VERSION


def test_an_unmarked_run_is_byte_identical_to_the_pre_contract_renderer(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root, marked=False)

    system, user = _package_files(root, run_id)
    assert system == SYSTEM_MESSAGE
    assert "[[P" not in user
    assert "output_contract" not in user
    assert "Alpha beta.\n\nGamma delta.\n" in user


def test_a_run_created_without_a_contract_is_the_legacy_contract(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    synthetic_project(root)
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    created = create_run(plan_run(snapshot.snapshot_id, project_root=root, now=lambda: NOW))

    assert load_run_summary(created.run_id, project_root=root).run.output_contract_version is None


# --------------------------------------------------------------------------------------------
# What a marked run accepts
# --------------------------------------------------------------------------------------------


def test_a_conformant_marked_answer_is_accepted_with_marker_free_text(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root, marked=True)

    _translate(root, run_id, MARKED_ANSWER)

    job = _job(root, run_id)
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    translation = _run_path(root, run_id) / "jobs/001-001/attempts/001/translation.md"
    assert translation.read_text(encoding="utf-8") == PLAIN_ANSWER
    assert "[[P" not in translation.read_text(encoding="utf-8")
    attempt = TranslationAttempt.model_validate(
        yaml.safe_load(
            (_run_path(root, run_id) / "jobs/001-001/attempts/001/attempt.yaml").read_text(
                encoding="utf-8"
            )
        )
    )
    assert attempt.output_contract == MARKED_OUTPUT_CONTRACT_VERSION


def test_the_acceptance_evidence_records_the_alignment_verdict(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root, marked=True)
    _translate(root, run_id, MARKED_ANSWER)

    job = _job(root, run_id)
    assert job.acceptance_file is not None
    evidence = yaml.safe_load((root / job.acceptance_file).read_text(encoding="utf-8"))
    assert evidence["alignment_qa_version"] == "1.0.0"
    assert evidence["alignment_report"]["contract_mode"] == "marked"
    assert evidence["alignment_report"]["valid"] is True
    assert evidence["alignment_report"]["aligned_pairs"] == 2


def test_a_legacy_run_records_a_positional_alignment_verdict(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root, marked=False)
    _translate(root, run_id, PLAIN_ANSWER)

    job = _job(root, run_id)
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.acceptance_file is not None
    evidence = yaml.safe_load((root / job.acceptance_file).read_text(encoding="utf-8"))
    assert evidence["alignment_report"]["contract_mode"] == "legacy_positional"


# --------------------------------------------------------------------------------------------
# What a marked run refuses: the four simulated defects
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "answer"),
    [
        ("merge", f"{marker_for(1)}\n{FIRST} {SECOND}\n"),
        ("split", f"{marker_for(1)}\n{FIRST}\n\n{marker_for(1)}\n{SECOND}\n"),
        ("reorder", f"{marker_for(2)}\n{SECOND}\n\n{marker_for(1)}\n{FIRST}\n"),
        ("omission", f"{marker_for(1)}\n{FIRST}\n"),
        ("unmarked", PLAIN_ANSWER),
    ],
)
def test_a_broken_marked_answer_fails_and_publishes_no_translation(
    tmp_path: Path, label: str, answer: str
) -> None:
    root = tmp_path / "project"
    run_id = _run(root, marked=True)

    _translate(root, run_id, answer)

    job = _job(root, run_id)
    # A contract violation is a retryable shape failure: the same request asked again is the
    # remedy, so the queue schedules a retry rather than ending the job.  What matters here is
    # that nothing became a translation.
    assert job.status is TranslationJobStatus.RETRY_PENDING, label
    assert job.acceptance_status is None
    attempts = _run_path(root, run_id) / "jobs/001-001/attempts/001"
    assert not (attempts / "translation.md").exists()
    attempt = TranslationAttempt.model_validate(
        yaml.safe_load((attempts / "attempt.yaml").read_text(encoding="utf-8"))
    )
    assert attempt.sanitized_error_message is not None
    assert "numbered paragraph contract" in attempt.sanitized_error_message
    assert attempt.translation_file is None


def test_the_same_broken_answer_is_accepted_by_a_legacy_run(tmp_path: Path) -> None:
    # The proof that the contract is what rejects it, not the prose: identical bytes, opposite
    # outcomes, decided only by the contract the run was created against.
    root = tmp_path / "project"
    run_id = _run(root, marked=False)

    _translate(root, run_id, PLAIN_ANSWER)

    assert _job(root, run_id).acceptance_status is TranslationAcceptanceStatus.ACCEPTED


# --------------------------------------------------------------------------------------------
# Repair safety
# --------------------------------------------------------------------------------------------


def _reject(root: Path, run_id: str) -> TranslationJob:
    """Force the standing verdict to a rejection without touching the attempt it judged."""
    job = _job(root, run_id)
    assert job.acceptance_file is not None
    evidence_path = root / job.acceptance_file
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8"))
    evidence["status"] = "qa_rejected"
    evidence["accepted"] = False
    evidence["structural_report"]["valid"] = False
    evidence["structural_report"]["findings"] = [
        {
            "code": "paragraph_count_mismatch",
            "severity": "error",
            "message": "source and translation paragraph counts differ",
        }
    ]
    payload = yaml.safe_dump(evidence, allow_unicode=True, sort_keys=False)
    evidence_path.write_text(payload, encoding="utf-8")
    from book_translator.provider_execution.artifacts import sha256_bytes

    job_path = _run_path(root, run_id) / "jobs/001-001/job.yaml"
    changed = job.model_copy(
        update={
            "acceptance_status": TranslationAcceptanceStatus.QA_REJECTED,
            "acceptance_sha256": sha256_bytes(payload.encode("utf-8")),
        }
    )
    job_path.write_text(
        yaml.safe_dump(
            changed.model_dump(mode="json", exclude_none=False),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return changed


def test_structural_separator_repair_is_refused_under_the_marked_contract(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _run(root, marked=True)
    _translate(root, run_id, MARKED_ANSWER)
    job = _reject(root, run_id)

    plan = plan_structural_repair(run_id, job.job_id, project_root=root, now=_clock(100))

    assert not plan.applicable
    assert "marked_contract_forbids_deterministic_separator_repair" in plan.refusals
    assert plan.evidence is None


def test_a_repair_under_the_marked_contract_re_marks_both_documents(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root, marked=True)
    _translate(root, run_id, MARKED_ANSWER)
    job = _reject(root, run_id)

    request = build_repair_request(_run_path(root, run_id), job, project_root=root)

    assert request.prompt_version == MARKED_REPAIR_PROMPT_VERSION
    assert "MARKED_SOURCE_CHUNK" in request.user
    assert "MARKED_REJECTED_TRANSLATION" in request.user
    assert mark_paragraphs(PLAIN_ANSWER) in request.user
    assert "PREVIOUS_CONTEXT" in request.user
    assert "Never move a sentence" in request.system


def test_a_legacy_repair_derivation_is_unchanged_so_recovery_can_reproduce_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _run(root, marked=False)
    _translate(root, run_id, PLAIN_ANSWER)
    job = _reject(root, run_id)

    request = build_repair_request(_run_path(root, run_id), job, project_root=root)

    assert request.prompt_version == REPAIR_PROMPT_VERSION == "1.0.0"
    assert request.user.startswith("REPAIR_FINDINGS_JSON length=")
    assert "SOURCE_CHUNK length=" in request.user
    assert "REJECTED_TRANSLATION length=" in request.user
    assert "PREVIOUS_CONTEXT" not in request.user
    assert "[[P" not in request.user


# --------------------------------------------------------------------------------------------
# The legacy audit
# --------------------------------------------------------------------------------------------


def test_the_legacy_audit_measures_a_marker_less_run_and_writes_only_its_own_report(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _run(root, marked=False)
    _translate(root, run_id, PLAIN_ANSWER)
    before = {
        path: path.read_bytes()
        for path in sorted(_run_path(root, run_id).rglob("*"))
        if path.is_file()
    }

    plan = plan_alignment_audit(run_id, project_root=root, now=_clock(200))
    written = execute_alignment_audit(plan)

    assert plan.report.assessed_jobs == 1
    assert plan.report.skipped_jobs == 0
    assert plan.report.aligned_pairs == 2
    assert plan.report.entries[0].contract_mode == "legacy_positional"
    assert plan.report.provider_contacted is False
    assert plan.report.mutated_state is False
    assert written.is_relative_to(root / "workspace/audits/paragraph-alignment")
    after = {
        path: path.read_bytes()
        for path in sorted(_run_path(root, run_id).rglob("*"))
        if path.is_file()
    }
    assert after == before


# --------------------------------------------------------------------------------------------
# A repair may not buy count equality by splitting the wrong paragraph
# --------------------------------------------------------------------------------------------

#: Two source paragraphs of forty words each: long enough for the policy to measure, and equal
#: enough that a correct translation of either lands well inside the band.
LONG_SOURCE = " ".join(["alpha"] * 40) + ".\n\n" + " ".join(["beta"] * 40) + ".\n"

#: One block written as two lines with the break in the wrong place. The line counts match the
#: source's, so the deterministic engine restores the source's blank line between them — and the
#: resulting blocks carry eight and eighty words against forty and forty.
MISPLACED_BREAK = " ".join(["آلفا"] * 8) + ".\n" + " ".join(["بتا"] * 80) + ".\n"


def _long_paragraph_project(root: Path) -> None:
    """A synthetic project whose one chunk holds two long paragraphs."""
    import hashlib

    root.mkdir(parents=True, exist_ok=True)
    synthetic_project(root)
    chapter = root / "workspace/chapters/001"
    digest = hashlib.sha256(LONG_SOURCE.encode("utf-8")).hexdigest()
    (chapter / "cleaned.md").write_text(LONG_SOURCE, encoding="utf-8")
    (chapter / "chunks/001/source.md").write_text(LONG_SOURCE, encoding="utf-8")
    words = len(LONG_SOURCE.split())
    for path, updates in (
        (
            chapter / "chunks/001/metadata.yaml",
            {
                "source_sha256": digest,
                "cleaned_source_sha256": digest,
                "source_word_count": words,
                "source_paragraph_count": 2,
            },
        ),
        (
            chapter / "metadata.yaml",
            {"cleaned_sha256": digest, "cleaned_word_count": words, "cleaned_paragraph_count": 2},
        ),
        (
            chapter / "chunking-report.yaml",
            {"cleaned_sha256": digest, "reconstructed_sha256": digest},
        ),
    ):
        record = yaml.safe_load(path.read_text(encoding="utf-8"))
        record.update(updates)
        path.write_text(
            yaml.safe_dump(record, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )


def test_a_repair_that_would_split_the_wrong_paragraph_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _long_paragraph_project(root)
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    created = create_run(
        plan_run(
            snapshot.snapshot_id,
            project_root=root,
            output_contract_version=OUTPUT_CONTRACT_VERSION,
            now=lambda: NOW,
        )
    )
    execute_prompt_packages(
        plan_prompt_packages(created.run_id, project_root=root, now=lambda: NOW)
    )
    _translate(root, created.run_id, MISPLACED_BREAK)
    job = _job(root, created.run_id)
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED

    plan = plan_structural_repair(created.run_id, job.job_id, project_root=root, now=_clock(100))

    # The engine can restore count equality here, and doing so would be wrong: the restored
    # blocks carry complementary word deficits and excesses that cancel back into the band.
    assert plan.diagnosis.deterministic
    assert not plan.applicable
    assert plan.refusals == ("repair_introduces:adjacent_boundary_drift_suspected",)
    assert plan.evidence is None
    after = _job(root, created.run_id)
    assert after.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert after.structural_repair_count == 0
