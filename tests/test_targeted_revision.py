"""Targeted semantic revision: one operator-named paragraph, proven, or nothing at all."""

from pathlib import Path

import pytest
import yaml

from book_translator.models import (
    OperatorDefectStatement,
    SemanticFindingCode,
    SemanticSeverity,
    TranslationAcceptanceStatus,
    TranslationSemanticRevisionEvidence,
)
from book_translator.paragraph_markers import marker_for
from book_translator.provider_execution.artifacts import serialize_yaml
from book_translator.release_assembly.workflow import load_effective_acceptance
from book_translator.semantic_qa import TransportSemanticJudge
from book_translator.semantic_revision import TransportSemanticReviser
from book_translator.semantic_revision.targeted import (
    TARGETED_SEMANTIC_REVISION_VERSION,
    TargetedRevisionError,
)
from book_translator.semantic_revision.workflow import (
    execute_targeted_revision,
    plan_targeted_revision,
)
from tests.test_paragraph_contract_pipeline import FIRST, SECOND
from tests.test_semantic_gate import (
    BOTH_P2_REVISE,
    GOOD_REVISION,
    P2_PASS,
    P2_REVISE,
    REVISED_SECOND,
    ScriptedTransport,
    _clock,
    _gate,
    _job,
    _run,
    _run_bytes,
)
from tests.test_translation_worker import _run_path

CHUNK = "001-001"
DEFECT = OperatorDefectStatement(
    code=SemanticFindingCode.FACTUAL_CHANGE,
    severity=SemanticSeverity.HIGH,
    source_evidence="the second sentence",
    reason="the Persian says something the English does not say",
)
MECHANICAL = ("job.yaml", "run.yaml", "semantic-qa", "semantic-revision", "semantic-gate")


def _plan(root: Path, run_id: str, **kwargs: object):  # type: ignore[no-untyped-def]
    return plan_targeted_revision(
        run_id,
        kwargs.pop("job", CHUNK),  # type: ignore[arg-type]
        paragraph_index=kwargs.pop("paragraph", 2),  # type: ignore[arg-type]
        statement=kwargs.pop("statement", DEFECT),  # type: ignore[arg-type]
        project_root=root,
        **kwargs,  # type: ignore[arg-type]
    )


def _revise(root: Path, run_id: str, transport: ScriptedTransport, **kwargs: object):  # type: ignore[no-untyped-def]
    plan = _plan(root, run_id, **kwargs)
    return execute_targeted_revision(
        plan,
        reviser=TransportSemanticReviser(transport, model="fake-gate-v1"),
        judge=TransportSemanticJudge(transport, model="fake-gate-v1"),
        now=_clock(200),
    )


def _standing(root: Path, run_id: str) -> str:
    acceptance = load_effective_acceptance(_job(root, run_id), project_root=root)
    return (root / acceptance.translation_file).read_text(encoding="utf-8")


# --- the happy path -------------------------------------------------------------------------


def test_only_the_named_paragraph_changes_and_supersedes_the_accepted_text(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    before = _run_bytes(root, run_id, exclude=MECHANICAL)
    transport = ScriptedTransport((P2_PASS,), (GOOD_REVISION,))

    result = _revise(root, run_id, transport)

    assert result.adopted
    assert result.provider_calls == 2
    assert len(transport.reviser_calls) == 1 and len(transport.judge_calls) == 1
    assert _standing(root, run_id) == f"{FIRST}\n\n{REVISED_SECOND}\n"
    # Every other paragraph, and every mechanical artifact, is byte-identical.
    assert _run_bytes(root, run_id, exclude=MECHANICAL) == before
    job = _job(root, run_id)
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.accepted_attempt == 1
    assert job.semantic_status is None
    assert job.semantic_revision_count == 1


def test_the_record_binds_the_paragraph_the_operator_named(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    plan = _plan(root, run_id)
    result = execute_targeted_revision(
        plan,
        reviser=TransportSemanticReviser(ScriptedTransport((P2_PASS,), (GOOD_REVISION,))),
        judge=TransportSemanticJudge(ScriptedTransport((P2_PASS,), (GOOD_REVISION,))),
        now=_clock(200),
    )

    evidence = result.evidence
    assert evidence.defect_origin == "operator"
    assert evidence.operator_defect == DEFECT
    assert evidence.revision_engine_version == TARGETED_SEMANTIC_REVISION_VERSION
    assert evidence.window.reason == "single_paragraph"
    assert evidence.window.indexes == (2,)
    assert evidence.superseded_semantic_file is None
    paragraph = evidence.revised_paragraph
    assert paragraph is not None
    assert paragraph.paragraph_id == "P0002"
    assert paragraph.target_paragraph_sha256_before == plan.identity.target_paragraph_sha256_before
    assert paragraph.target_paragraph_sha256_after not in {
        None,
        paragraph.target_paragraph_sha256_before,
    }
    assert evidence.snapshot_id == plan.snapshot_id
    assert evidence.verification is not None
    assert evidence.verification.outcome.paragraph_index == 2
    assert evidence.bounds is not None and evidence.bounds.unchanged_outside_window


# --- refusals: the revision must earn adoption ------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(
            f"{marker_for(1)}\n{FIRST}\n\n{marker_for(2)}\n{REVISED_SECOND}\n", id="outside"
        ),
        pytest.param(f"{marker_for(1)}\nدیگر.\n", id="wrong_paragraph"),
        pytest.param(REVISED_SECOND + "\n", id="unmarked"),
    ],
)
def test_an_answer_reaching_outside_the_window_is_refused(tmp_path: Path, answer: str) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    standing = _standing(root, run_id)

    result = _revise(root, run_id, ScriptedTransport((P2_PASS,), (answer,)))

    assert not result.adopted
    assert any(item.startswith("markers:") for item in result.evidence.refusals)
    assert result.evidence.translation_file is None
    assert not (result.evidence_file.parent / "translation.md").exists()
    assert _standing(root, run_id) == standing
    # A refusal is evidence on disk; the job keeps pointing at the text that still stands.
    assert _job(root, run_id).semantic_revision_count == 0
    assert _job(root, run_id).latest_semantic_revision_file is None


def test_a_structurally_broken_revision_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    standing = _standing(root, run_id)
    split = f"{marker_for(2)}\nگاما.\n\nدلتا.\n"

    result = _revise(root, run_id, ScriptedTransport((P2_PASS,), (split,)))

    assert not result.adopted
    assert result.evidence.refusals
    assert _standing(root, run_id) == standing


def test_a_verification_that_is_not_a_pass_refuses_the_revision(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    standing = _standing(root, run_id)

    result = _revise(root, run_id, ScriptedTransport((P2_REVISE,), (GOOD_REVISION,)))

    assert not result.adopted
    assert "verification:REVISE" in result.evidence.refusals
    assert result.evidence.verification is not None
    assert _standing(root, run_id) == standing


def test_the_reviser_cannot_certify_its_own_answer(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    result = _revise(root, run_id, ScriptedTransport((P2_PASS,), (GOOD_REVISION,)))

    # The adopted text was judged by the judge role, in its own call, after the splice.
    assert result.adopted
    assert result.evidence.verification is not None
    assert result.evidence.verification.request.request_id != (
        result.evidence.request.request_id if result.evidence.request else ""
    )


# --- fail closed ------------------------------------------------------------------------------


def test_an_unexpected_paragraph_hash_refuses_before_any_provider_call(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    with pytest.raises(TargetedRevisionError, match="does not hash as the operator expected"):
        _plan(root, run_id, expect_paragraph_sha256="0" * 64)


def test_a_paragraph_outside_the_chunk_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    with pytest.raises(TargetedRevisionError, match="outside the chunk"):
        _plan(root, run_id, paragraph=9)


def test_an_unmarked_run_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root, marked=False)

    with pytest.raises(TargetedRevisionError, match="marked paragraph contract"):
        _plan(root, run_id)


def test_a_moved_standing_translation_is_refused_before_the_provider(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    plan = _plan(root, run_id)
    target = root / plan.standing_file
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(TargetedRevisionError, match="standing translation hash mismatch"):
        execute_targeted_revision(
            plan,
            reviser=TransportSemanticReviser(ScriptedTransport((P2_PASS,), (GOOD_REVISION,))),
            judge=TransportSemanticJudge(ScriptedTransport((P2_PASS,), (GOOD_REVISION,))),
        )


def test_an_unknown_job_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    with pytest.raises(TargetedRevisionError, match="no job"):
        _plan(root, run_id, job="042-007")


# --- budget, idempotence, and the plan's own silence ------------------------------------------


def test_the_revision_budget_is_a_hard_limit(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    broken = f"{marker_for(2)}\nگاما.\n\nدلتا.\n"
    for _ in range(2):
        _revise(root, run_id, ScriptedTransport((P2_PASS,), (broken,)))

    plan = _plan(root, run_id)
    assert plan.provider_revisions_used == 2
    assert plan.budget_remaining == 0
    assert plan.action == "budget_exhausted"
    with pytest.raises(TargetedRevisionError, match="budget_exhausted"):
        execute_targeted_revision(
            plan,
            reviser=TransportSemanticReviser(ScriptedTransport((P2_PASS,), (GOOD_REVISION,))),
            judge=TransportSemanticJudge(ScriptedTransport((P2_PASS,), (GOOD_REVISION,))),
        )


def test_an_already_applied_revision_is_not_repeated(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    _revise(root, run_id, ScriptedTransport((P2_PASS,), (GOOD_REVISION,)))

    plan = _plan(root, run_id)

    assert plan.action == "skip"
    assert plan.existing_revision_file is not None


def test_planning_writes_nothing_and_contacts_nobody(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    before = _run_bytes(root, run_id)
    transport = ScriptedTransport((P2_PASS,), (GOOD_REVISION,))

    plan = _plan(root, run_id)

    assert transport.judge_calls == [] and transport.reviser_calls == []
    assert _run_bytes(root, run_id) == before
    assert plan.action == "revise"
    assert plan.writable_paragraphs == (2,)
    assert plan.protected_paragraphs == 1
    assert plan.budget_remaining == 2


def test_no_prompt_or_reasoning_is_persisted(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    result = _revise(root, run_id, ScriptedTransport((P2_PASS,), (GOOD_REVISION,)))

    text = result.evidence_file.read_text(encoding="utf-8")
    assert "<revision-task>" not in text
    assert "<judge-task>" not in text
    assert SECOND not in text


# --- the gate's own revisions keep verifying --------------------------------------------------


def test_a_gate_revision_record_still_round_trips_byte_identically(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    _gate(root, run_id, ScriptedTransport((BOTH_P2_REVISE, P2_PASS), (GOOD_REVISION,)))

    path = next(
        (_run_path(root, run_id) / f"jobs/{CHUNK}/semantic-revision").glob("*/revision.yaml")
    )
    record = TranslationSemanticRevisionEvidence.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )

    assert record.defect_origin == "judge"
    assert record.operator_defect is None and record.revised_paragraph is None
    assert record.superseded_semantic_file is not None
    assert serialize_yaml(record) == path.read_text(encoding="utf-8")
