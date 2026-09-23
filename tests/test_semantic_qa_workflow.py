"""The Semantic Coverage Judge against a real queue: evidence, isolation, and refusal."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from book_translator.models import SCHEMA_VERSION, TranslationAcceptanceStatus
from book_translator.models.semantic_qa import SemanticQAEvidence, SemanticVerdict
from book_translator.prompt_packaging import (
    MARKED_OUTPUT_CONTRACT_VERSION,
    OUTPUT_CONTRACT_VERSION,
    execute_prompt_packages,
    plan_prompt_packages,
)
from book_translator.semantic_qa import (
    JUDGE_SCHEMA_VERSION,
    ChunkSelection,
    FakeSemanticJudgeProvider,
    TransportSemanticJudge,
    execute_semantic_coverage,
    plan_semantic_coverage,
)
from book_translator.semantic_qa.workflow import SemanticCoveragePlan, SemanticCoverageResult
from book_translator.translation_queue import create_run, load_run_summary, plan_run
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import run_translation_worker
from tests.test_paragraph_contract_pipeline import MARKED_ANSWER, PLAIN_ANSWER
from tests.test_translation_auto_repair import ScriptedProvider
from tests.test_translation_queue import NOW, SNAPSHOT_NOW
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import _options, _run_path

CHUNK = "001-001"
IDS = ("P0001", "P0002")


def _clock(offset: int) -> Callable[[], datetime]:
    return lambda: NOW + timedelta(seconds=offset)


def _verdicts(*entries: tuple[str, str, list[dict[str, object]]]) -> str:
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "judge_schema_version": JUDGE_SCHEMA_VERSION,
            "verdicts": [
                {
                    "paragraph_id": identifier,
                    "verdict": verdict,
                    "confidence": 0.9,
                    "findings": findings,
                }
                for identifier, verdict, findings in entries
            ],
        },
        ensure_ascii=False,
    )


ALL_PASS = _verdicts(("P0001", "PASS", []), ("P0002", "PASS", []))
ONE_REVISE = _verdicts(
    ("P0001", "PASS", []),
    (
        "P0002",
        "REVISE",
        [
            {
                "code": "negation_changed",
                "severity": "HIGH",
                "source_evidence": "would not go",
                "explanation": "the negation is absent from the Persian paragraph",
                "confidence": 0.9,
            }
        ],
    ),
)
ONE_INCONCLUSIVE = _verdicts(("P0001", "PASS", []), ("P0002", "INCONCLUSIVE", []))


def _run(root: Path, *, marked: bool = False) -> str:
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
    run_translation_worker(
        created.run_id,
        project_root=root,
        options=_options(max_jobs=1),
        provider_factory=lambda _job: ScriptedProvider(
            (MARKED_ANSWER if marked else PLAIN_ANSWER,)
        ),
        now=_clock(10),
    )
    return created.run_id


def _judge(texts: tuple[str, ...]) -> TransportSemanticJudge:
    return TransportSemanticJudge(FakeSemanticJudgeProvider(texts), model="fake-judge-v1")


def _evaluate(
    root: Path, run_id: str, texts: tuple[str, ...], **kwargs: object
) -> tuple[SemanticCoveragePlan, SemanticCoverageResult]:
    plan = plan_semantic_coverage(
        run_id,
        project_root=root,
        selections=[ChunkSelection(CHUNK)],
        **kwargs,  # type: ignore[arg-type]
    )
    return plan, execute_semantic_coverage(plan, _judge(texts), now=_clock(100))


def _snapshot_bytes(root: Path, run_id: str) -> dict[str, bytes]:
    run_path = _run_path(root, run_id)
    return {
        str(path.relative_to(run_path)): path.read_bytes()
        for path in sorted(run_path.rglob("*"))
        if path.is_file()
    }


# --------------------------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------------------------


def test_a_judged_chunk_publishes_immutable_evidence_outside_the_run(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    plan, result = _evaluate(root, run_id, (ALL_PASS,))

    assert result.written is not None
    assert result.written.is_relative_to(root / "workspace/audits/semantic-coverage")
    assert not result.written.is_relative_to(_run_path(root, run_id))
    assert result.report.paragraphs_judged == 2
    assert result.report.pass_count == 2
    assert plan.planned_calls == result.report.judge_calls == 1
    evidence = SemanticQAEvidence.model_validate(
        yaml.safe_load(
            (result.written / "chunks" / f"{CHUNK}-r01.yaml").read_text(encoding="utf-8")
        )
    )
    assert evidence.mutated_acceptance is False
    assert evidence.provider_contacted is True
    assert [item.paragraph_id for item in evidence.outcomes] == list(IDS)


def test_the_evidence_hash_links_the_exact_bytes_it_judged(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    job = load_run_summary(run_id, project_root=root).jobs[0]

    _, result = _evaluate(root, run_id, (ALL_PASS,))

    evidence = result.evidence[0]
    assert evidence.source_sha256 == job.chunk_source_sha256
    assert evidence.context_sha256 == job.context_sha256
    assert evidence.terminology_sha256 is None or len(evidence.terminology_sha256) == 64


def test_the_evidence_records_the_provider_and_model_identity(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    _, result = _evaluate(root, run_id, (ALL_PASS,))

    record = result.evidence[0].requests[0]
    assert record.provider == "fake"
    assert record.model_requested == "fake-judge-v1"
    assert record.model_returned == "fake-judge-v1"
    assert result.report.models_returned == ["fake-judge-v1"]


def test_no_prompt_or_raw_response_is_persisted(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    _, result = _evaluate(root, run_id, (ALL_PASS,))

    assert result.written is not None
    for path in result.written.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        # Only digests of the messages are kept, never the messages, and never the model's
        # own narration of how it decided.
        assert "<judge-task>" not in text
        assert "You are a bilingual fidelity auditor" not in text
        assert "judge-schema" not in text
        assert "system_message_sha256" in text or path.name == "evaluation.yaml"


def test_evidence_files_are_owner_only(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    _, result = _evaluate(root, run_id, (ALL_PASS,))

    assert result.written is not None
    for path in result.written.rglob("*.yaml"):
        assert path.stat().st_mode & 0o077 == 0


# --------------------------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------------------------


def test_a_material_defect_is_recorded_as_revise_without_touching_acceptance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    before = load_run_summary(run_id, project_root=root).jobs[0]

    _, result = _evaluate(root, run_id, (ONE_REVISE,))

    assert result.report.revise_count == 1
    assert result.report.pass_count == 1
    assert result.report.finding_counts == {"negation_changed": 1}
    after = load_run_summary(run_id, project_root=root).jobs[0]
    assert after.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert after.acceptance_status is before.acceptance_status
    assert after.acceptance_sha256 == before.acceptance_sha256


def test_inconclusive_is_persisted_as_itself_and_never_counted_as_a_pass(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    _, result = _evaluate(root, run_id, (ONE_INCONCLUSIVE,))

    assert result.report.inconclusive_count == 1
    assert result.report.pass_count == 1
    outcome = next(
        item
        for item in result.evidence[0].outcomes
        if item.policy_outcome is SemanticVerdict.INCONCLUSIVE
    )
    assert outcome.judge_verdict is SemanticVerdict.INCONCLUSIVE


def test_two_repetitions_publish_two_independent_evidence_records(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    plan = plan_semantic_coverage(
        run_id,
        project_root=root,
        selections=[ChunkSelection(CHUNK)],
        repetitions=2,
    )
    result = execute_semantic_coverage(plan, _judge((ALL_PASS, ONE_REVISE)), now=_clock(100))

    assert plan.planned_calls == 2
    assert [item.repetition for item in result.evidence] == [1, 2]
    assert result.evidence[0].pass_count == 2
    assert result.evidence[1].revise_count == 1
    assert {
        item.request_id for record in result.evidence for item in record.requests
    }.__len__() == 2


def test_a_failed_judge_call_is_recorded_and_never_becomes_a_verdict(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    _, result = _evaluate(root, run_id, ("this is not json",))

    assert result.report.failed_calls == 1
    assert result.report.paragraphs_judged == 0
    assert result.report.pass_count == 0
    assert any("malformed_json" in entry for entry in result.report.skipped)


# --------------------------------------------------------------------------------------------
# Refusal and isolation
# --------------------------------------------------------------------------------------------


def test_a_tampered_translation_is_skipped_rather_than_judged(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    job = load_run_summary(run_id, project_root=root).jobs[0]
    assert job.acceptance_file is not None
    evidence = yaml.safe_load((root / job.acceptance_file).read_text(encoding="utf-8"))
    translation = root / evidence["translation_file"]
    translation.write_text(PLAIN_ANSWER.replace("آلفا", "بتا"), encoding="utf-8")

    plan = plan_semantic_coverage(run_id, project_root=root, selections=[ChunkSelection(CHUNK)])

    assert plan.chunks == ()
    assert any("hash mismatch" in entry for entry in plan.skipped)


def test_an_unknown_chunk_is_skipped_rather_than_guessed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    plan = plan_semantic_coverage(run_id, project_root=root, selections=[ChunkSelection("999-999")])

    assert plan.chunks == ()
    assert plan.skipped == ("999-999: not a job of this run",)


def test_judging_a_run_leaves_every_byte_of_it_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    before = _snapshot_bytes(root, run_id)

    _evaluate(root, run_id, (ONE_REVISE,))

    assert _snapshot_bytes(root, run_id) == before


def test_a_marked_run_records_marked_identity_and_a_legacy_run_records_positional(
    tmp_path: Path,
) -> None:
    marked_root = tmp_path / "marked"
    legacy_root = tmp_path / "legacy"

    _, marked = _evaluate(marked_root, _run(marked_root, marked=True), (ALL_PASS,))
    _, legacy = _evaluate(legacy_root, _run(legacy_root), (ALL_PASS,))

    assert marked.evidence[0].identity_mode == "marked"
    assert legacy.evidence[0].identity_mode == "legacy_positional"


def test_a_dry_run_contacts_nothing_and_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    provider = FakeSemanticJudgeProvider((ALL_PASS,))

    plan = plan_semantic_coverage(run_id, project_root=root, selections=[ChunkSelection(CHUNK)])
    result = execute_semantic_coverage(
        plan, TransportSemanticJudge(provider), now=_clock(100), dry_run=True
    )

    assert provider.calls == []
    assert result.written is None
    assert result.report.judge_calls == 0
    assert plan.planned_calls == 1
    assert not (root / "workspace/audits/semantic-coverage").exists()


def test_the_evaluation_never_writes_into_the_release_tree(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    _, result = _evaluate(root, run_id, (ALL_PASS,))

    assert result.written is not None
    assert not (root / "workspace/releases").exists()
    assert not (root / "output").exists()


def test_a_second_evaluation_gets_its_own_immutable_directory(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    _, first = _evaluate(root, run_id, (ALL_PASS,))
    _, second = _evaluate(root, run_id, (ONE_REVISE,))

    assert first.written is not None and second.written is not None
    assert first.written != second.written
    assert first.written.name == "001" and second.written.name == "002"
    evidence = SemanticQAEvidence.model_validate(
        yaml.safe_load((first.written / "chunks" / f"{CHUNK}-r01.yaml").read_text(encoding="utf-8"))
    )
    assert evidence.pass_count == 2


def test_planning_refuses_a_selection_with_an_impossible_batch(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    with pytest.raises(ValueError):
        plan_semantic_coverage(
            run_id, project_root=root, selections=[ChunkSelection(CHUNK)], batch_size=0
        )
