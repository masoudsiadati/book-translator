"""The production semantic gate: acceptance, routing, bounded revision, and its limits."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from book_translator.models import (
    SCHEMA_VERSION,
    SemanticGateStatus,
    SemanticParagraphOutcome,
    SemanticVerdict,
    TranslationAcceptanceStatus,
    TranslationJob,
    TranslationSemanticRevisionEvidence,
)
from book_translator.paragraph_alignment import EN_FA_V1
from book_translator.paragraph_markers import ParagraphMarkerError, marker_for
from book_translator.prompt_packaging import (
    MARKED_OUTPUT_CONTRACT_VERSION,
    OUTPUT_CONTRACT_VERSION,
    execute_prompt_packages,
    plan_prompt_packages,
)
from book_translator.release_assembly import ReleaseAssemblyError
from book_translator.release_assembly.workflow import (
    load_effective_acceptance,
    plan_release_assembly,
)
from book_translator.semantic_gate import (
    PRODUCTION_GATE_V1,
    SEMANTIC_GATE_POLICY_VERSION,
    SemanticGateError,
    execute_semantic_gate,
    plan_semantic_gate,
)
from book_translator.semantic_qa import (
    JUDGE_SCHEMA_VERSION,
    TransportSemanticJudge,
    build_tasks,
)
from book_translator.semantic_qa.displacement import (
    classify_displacement,
    lexical_content,
    resegment_window,
)
from book_translator.semantic_revision import (
    REVISER_SYSTEM_MESSAGE,
    RevisionScopeError,
    TransportSemanticReviser,
    measure_bounds,
    render_revision_request,
    splice_window,
)
from book_translator.translation_queue import create_run, load_run_summary, plan_run
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import run_translation_worker
from tests.test_paragraph_contract_pipeline import FIRST, MARKED_ANSWER, PLAIN_ANSWER, SECOND
from tests.test_translation_auto_repair import ScriptedProvider
from tests.test_translation_queue import NOW, SNAPSHOT_NOW
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import _options, _run_path

CHUNK = "001-001"
REVISED_SECOND = "گاما دلتا اصلاح‌شده."


def _clock(offset: int) -> Callable[[], datetime]:
    return lambda: NOW + timedelta(seconds=offset)


def _finding(
    code: str = "meaning_omitted", severity: str = "HIGH", confidence: float = 0.9
) -> dict[str, object]:
    return {
        "code": code,
        "severity": severity,
        "source_evidence": "delta",
        "explanation": "the second word is missing",
        "confidence": confidence,
    }


def _verdicts(*entries: tuple[str, str, list[dict[str, object]]]) -> str:
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "judge_schema_version": JUDGE_SCHEMA_VERSION,
            "verdicts": [
                {"paragraph_id": pid, "verdict": verdict, "confidence": 0.9, "findings": findings}
                for pid, verdict, findings in entries
            ],
        },
        ensure_ascii=False,
    )


ALL_PASS = _verdicts(("P0001", "PASS", []), ("P0002", "PASS", []))
P2_PASS = _verdicts(("P0002", "PASS", []))
P2_REVISE = _verdicts(("P0002", "REVISE", [_finding()]))
BOTH_P2_REVISE = _verdicts(("P0001", "PASS", []), ("P0002", "REVISE", [_finding()]))
BOTH_P2_INCONCLUSIVE = _verdicts(("P0001", "PASS", []), ("P0002", "INCONCLUSIVE", []))
P2_INCONCLUSIVE = _verdicts(("P0002", "INCONCLUSIVE", []))
GOOD_REVISION = f"{marker_for(2)}\n{REVISED_SECOND}\n"


class ScriptedTransport:
    """One transport answering judge and reviser calls from separate scripts."""

    provider_name = "fake"
    adapter_version = "1.0.0"

    def __init__(self, judge: tuple[str, ...], reviser: tuple[str, ...] = ()) -> None:
        self.judge_texts = judge
        self.reviser_texts = reviser
        self.judge_calls: list[str] = []
        self.reviser_calls: list[str] = []

    def execute(self, request):  # type: ignore[no-untyped-def]
        from book_translator.provider_execution.protocol import ProviderResult

        if request.metadata.get("execution_kind") == "semantic_reviser":
            index = min(len(self.reviser_calls), len(self.reviser_texts) - 1)
            self.reviser_calls.append(request.request_id)
            text = self.reviser_texts[index]
        else:
            index = min(len(self.judge_calls), len(self.judge_texts) - 1)
            self.judge_calls.append(request.request_id)
            text = self.judge_texts[index]
        return ProviderResult(
            response_id="synthetic",
            model="fake-gate-v1",
            status="completed",
            text_outputs=(text,),
        )


def _run(root: Path, *, marked: bool = True, gated: bool = True) -> str:
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
            semantic_gate_policy_version=SEMANTIC_GATE_POLICY_VERSION if gated else None,
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


def _gate(root: Path, run_id: str, transport: ScriptedTransport, *, dry_run: bool = False):  # type: ignore[no-untyped-def]
    plan = plan_semantic_gate(run_id, project_root=root)
    result = execute_semantic_gate(
        plan,
        judge=TransportSemanticJudge(transport, model="fake-gate-v1"),
        reviser=TransportSemanticReviser(transport, model="fake-gate-v1"),
        now=_clock(100),
        dry_run=dry_run,
    )
    return plan, result


def _job(root: Path, run_id: str) -> TranslationJob:
    return load_run_summary(run_id, project_root=root).jobs[0]


def _run_bytes(root: Path, run_id: str, *, exclude: tuple[str, ...] = ()) -> dict[str, bytes]:
    run_path = _run_path(root, run_id)
    return {
        str(p.relative_to(run_path)): p.read_bytes()
        for p in sorted(run_path.rglob("*"))
        if p.is_file() and not any(part in exclude for part in p.relative_to(run_path).parts)
    }


# --------------------------------------------------------------------------------------------
# Acceptance
# --------------------------------------------------------------------------------------------


def test_mechanical_acceptance_without_semantic_evidence_cannot_be_released(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    job = _job(root, run_id)
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.semantic_status is None

    with pytest.raises(ReleaseAssemblyError, match="no semantic acceptance"):
        plan_release_assembly(run_id, output="workspace/releases/r1", project_root=root)


def test_a_semantic_pass_reaches_semantic_acceptance_and_release(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    plan, result = _gate(root, run_id, ScriptedTransport((ALL_PASS,)))

    job = _job(root, run_id)
    assert job.semantic_status is SemanticGateStatus.PASS
    assert job.semantic_evidence_count == 1
    assert result.report.status_counts == {"pass": 1}
    assert plan.planned_first_look_calls == result.report.judge_calls == 1
    release = plan_release_assembly(run_id, output="workspace/releases/r1", project_root=root)
    assert release is not None


def test_acceptance_dimensions_are_recorded_separately(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    _gate(root, run_id, ScriptedTransport((BOTH_P2_INCONCLUSIVE, P2_INCONCLUSIVE)))

    job = _job(root, run_id)
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.acceptance_file is not None
    evidence = yaml.safe_load((root / job.acceptance_file).read_text(encoding="utf-8"))
    assert evidence["terminology_report"]["valid"]
    assert evidence["structural_report"]["valid"]
    assert evidence["alignment_report"]["valid"]
    assert job.semantic_status is SemanticGateStatus.UNRESOLVED


def test_a_pre_gate_run_is_refused_by_the_gate_and_still_releasable(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root, marked=False, gated=False)

    with pytest.raises(SemanticGateError, match="predates the gate"):
        plan_semantic_gate(run_id, project_root=root)
    assert plan_release_assembly(run_id, output="workspace/releases/r1", project_root=root)


# --------------------------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------------------------


def test_inconclusive_does_not_pass_and_gets_exactly_one_wider_second_look(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    transport = ScriptedTransport((BOTH_P2_INCONCLUSIVE, P2_INCONCLUSIVE, P2_INCONCLUSIVE))

    _, result = _gate(root, run_id, transport)

    assert result.report.second_looks == 1
    assert result.report.judge_calls == 2
    job = _job(root, run_id)
    assert job.semantic_status is SemanticGateStatus.UNRESOLVED
    assert result.report.jobs[0].unresolved_paragraphs == ["P0002"]
    records = sorted((_run_path(root, run_id) / "jobs/001-001/semantic-qa").glob("*/evidence.yaml"))
    second = yaml.safe_load(records[1].read_text(encoding="utf-8"))
    assert second["look"] == "second"
    assert second["context_radius"] == PRODUCTION_GATE_V1.second_look_radius == 2
    assert second["context_policy_version"] == "1.0.0"


def test_a_second_look_pass_resolves_the_paragraph(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)

    _, result = _gate(root, run_id, ScriptedTransport((BOTH_P2_INCONCLUSIVE, P2_PASS)))

    assert _job(root, run_id).semantic_status is SemanticGateStatus.PASS
    assert result.report.second_looks == 1
    assert result.report.jobs[0].unresolved_paragraphs == []


def test_a_second_look_widens_context_but_keeps_the_target_identity(tmp_path: Path) -> None:
    source = "A.\n\nB.\n\nC.\n\nD.\n\nE.\n"
    target = "آ.\n\nب.\n\nپ.\n\nت.\n\nث.\n"
    narrow = build_tasks(source, target, paragraph_indexes=(3,), context_radius=1)[0]
    wide = build_tasks(source, target, paragraph_indexes=(3,), context_radius=2)[0]

    assert narrow.paragraph_id == wide.paragraph_id == "P0003"
    assert narrow.previous_source == "B."
    assert wide.previous_source == "A.\n\nB."
    assert wide.next_source == "D.\n\nE."
    assert wide.source_text == "C."


def test_revise_routes_to_bounded_revision_and_an_independent_rejudge(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    transport = ScriptedTransport((BOTH_P2_REVISE, P2_PASS), (GOOD_REVISION,))

    _, result = _gate(root, run_id, transport)

    assert result.report.reviser_calls == 1
    assert result.report.revisions_adopted == 1
    assert result.report.judge_calls == 2
    job = _job(root, run_id)
    assert job.semantic_status is SemanticGateStatus.PASS
    assert job.semantic_revision_count == 1
    acceptance = load_effective_acceptance(job, project_root=root)
    text = (root / acceptance.translation_file).read_text(encoding="utf-8")
    assert text == f"{FIRST}\n\n{REVISED_SECOND}\n"
    assert "[[P" not in text


def test_the_reviser_cannot_self_certify_and_a_rejudge_is_required(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    # The reviser answers well every time; the judge keeps rejecting. Nothing the reviser says
    # or does can turn that into a pass.
    transport = ScriptedTransport(
        (BOTH_P2_REVISE, P2_REVISE, P2_REVISE), (GOOD_REVISION, GOOD_REVISION)
    )

    _, result = _gate(root, run_id, transport)

    assert result.report.reviser_calls == 2
    assert result.report.judge_calls == 3
    assert _job(root, run_id).semantic_status is SemanticGateStatus.UNRESOLVED


def test_the_revision_budget_is_a_hard_limit(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    transport = ScriptedTransport(
        (BOTH_P2_REVISE, P2_REVISE, P2_REVISE, P2_REVISE), (GOOD_REVISION,) * 5
    )

    _, result = _gate(root, run_id, transport)

    assert result.report.reviser_calls == PRODUCTION_GATE_V1.max_revisions_per_window == 2
    assert result.report.jobs[0].unresolved_paragraphs == ["P0002"]
    assert _job(root, run_id).semantic_status is SemanticGateStatus.UNRESOLVED
    assert _job(root, run_id).semantic_revision_count == 2


def test_the_reviser_never_sees_a_verdict_or_a_judge_confidence() -> None:
    from book_translator.models.semantic_qa import (
        SemanticFinding,
        SemanticFindingCode,
        SemanticSeverity,
    )

    finding = SemanticFinding(
        code=SemanticFindingCode.MEANING_OMITTED,
        severity=SemanticSeverity.HIGH,
        source_evidence="delta",
        explanation="missing",
        confidence=0.93,
    )
    message = render_revision_request(
        chapter_id="001",
        chunk_id="001-001",
        window_indexes=(2,),
        source_blocks=("Gamma delta.",),
        target_blocks=(SECOND,),
        findings={2: (finding,)},
        terminology=(),
        previous_source="Alpha beta.",
        previous_target=FIRST,
        next_source=None,
        next_target=None,
    )

    assert "0.93" not in message
    assert "REVISE" not in message
    assert "delta" in message and "[[P0002]]" in message
    assert "not the translator and not an editor" in REVISER_SYSTEM_MESSAGE
    assert "do not polish" in REVISER_SYSTEM_MESSAGE


# --------------------------------------------------------------------------------------------
# Scope and diff bounds
# --------------------------------------------------------------------------------------------


STANDING = "الف.\n\nب.\n\nپ.\n\nت.\n"


def test_a_revision_may_change_only_its_target_paragraph() -> None:
    spliced = splice_window(STANDING, f"{marker_for(2)}\nب اصلاح.\n", window_indexes=(2,))

    assert spliced.blocks == ("الف.", "ب اصلاح.", "پ.", "ت.")
    bounds = measure_bounds(
        STANDING,
        spliced.text,
        window_indexes=(2,),
        terminology_blocking_before=(),
        terminology_blocking_after=(),
    )
    assert bounds.changed_paragraph_indexes == [2]
    assert bounds.unchanged_outside_window and bounds.marker_identity_preserved
    assert bounds.word_delta == 1


def test_an_unrelated_paragraph_change_is_a_hard_failure() -> None:
    tampered = "الف.\n\nب اصلاح.\n\nپ تغییر.\n\nت.\n"

    with pytest.raises(RevisionScopeError):
        measure_bounds(
            STANDING,
            tampered,
            window_indexes=(2,),
            terminology_blocking_before=(),
            terminology_blocking_after=(),
        )


def test_a_bounded_multi_paragraph_window_works_and_is_measured() -> None:
    response = f"{marker_for(2)}\nب دو.\n\n{marker_for(3)}\nپ سه.\n"

    spliced = splice_window(STANDING, response, window_indexes=(2, 3))
    bounds = measure_bounds(
        STANDING,
        spliced.text,
        window_indexes=(2, 3),
        terminology_blocking_before=(),
        terminology_blocking_after=(),
    )

    assert spliced.blocks == ("الف.", "ب دو.", "پ سه.", "ت.")
    assert bounds.changed_paragraph_indexes == [2, 3]
    assert bounds.paragraphs_in_window == 2


def test_a_revision_missing_a_marker_fails() -> None:
    with pytest.raises(ParagraphMarkerError, match="marker_missing"):
        splice_window(STANDING, f"{marker_for(2)}\nب.\n", window_indexes=(2, 3))


def test_a_revision_with_reordered_markers_fails() -> None:
    with pytest.raises(ParagraphMarkerError, match="marker_out_of_order"):
        splice_window(
            STANDING, f"{marker_for(3)}\nپ.\n\n{marker_for(2)}\nب.\n", window_indexes=(2, 3)
        )


def test_a_revision_answering_outside_its_window_fails() -> None:
    with pytest.raises(ParagraphMarkerError, match="marker_unknown"):
        splice_window(
            STANDING, f"{marker_for(1)}\nالف.\n\n{marker_for(2)}\nب.\n", window_indexes=(2,)
        )


def test_a_revision_that_regresses_terminology_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    # The synthetic project carries no terminology, so a regression is simulated through the
    # structural gate the same adoption conjunction enforces: a revision that drops a paragraph
    # into two blocks fails alignment/structure and is refused rather than adopted.
    broken = f"{marker_for(2)}\nگاما.\n\nدلتا.\n"
    transport = ScriptedTransport((BOTH_P2_REVISE, P2_REVISE), (broken, broken))

    _, result = _gate(root, run_id, transport)

    assert result.report.revisions_adopted == 0
    assert result.report.revisions_refused == 2
    revisions = sorted(
        (_run_path(root, run_id) / "jobs/001-001/semantic-revision").glob("*/revision.yaml")
    )
    record = TranslationSemanticRevisionEvidence.model_validate(
        yaml.safe_load(revisions[0].read_text(encoding="utf-8"))
    )
    assert not record.adopted
    assert any(
        r.startswith("markers:") or r.startswith("structural:") or r.startswith("alignment:")
        for r in record.refusals
    )
    assert not (revisions[0].parent / "translation.md").exists()


def test_a_terminology_regression_is_detected_by_the_policy() -> None:
    from book_translator.models import (
        TerminologyEnforcement,
        TerminologyFinding,
        TerminologyValidationReport,
    )
    from book_translator.semantic_revision import terminology_regressed

    empty = "0" * 64
    before = TerminologyValidationReport(
        schema_version="1.0",
        valid=True,
        source_sha256=empty,
        translation_sha256=empty,
        checked_decisions=1,
        findings=[],
    )
    after = before.model_copy(
        update={
            "valid": False,
            "findings": [
                TerminologyFinding(
                    code="canonical_term_missing",
                    severity="error",
                    candidate_id="c1",
                    source_term="Alpha",
                    enforcement=TerminologyEnforcement.EXACT,
                    message="gone",
                )
            ],
        }
    )

    assert terminology_regressed(before, after) == ("canonical_term_missing",)
    assert terminology_regressed(after, after) == ()


# --------------------------------------------------------------------------------------------
# Displacement
# --------------------------------------------------------------------------------------------


def _outcome(
    index: int, codes: tuple[str, ...], verdict: str = "REVISE"
) -> SemanticParagraphOutcome:
    from book_translator.models.semantic_qa import (
        SemanticFinding,
        SemanticFindingCode,
        SemanticSeverity,
    )

    findings = [
        SemanticFinding(
            code=SemanticFindingCode(code),
            severity=SemanticSeverity.HIGH,
            source_evidence="x",
            explanation="y",
            confidence=0.9,
        )
        for code in codes
    ]
    return SemanticParagraphOutcome(
        paragraph_id=f"P{index:04d}",
        paragraph_index=index,
        judge_verdict=SemanticVerdict(verdict),
        policy_outcome=SemanticVerdict(verdict),
        judge_confidence=0.9,
        source_word_count=30,
        target_word_count=30,
        material_findings=findings,
        advisory_findings=[],
    )


def test_paired_omission_and_addition_across_a_run_is_classified_as_displacement() -> None:
    outcomes = (
        _outcome(1, (), "PASS"),
        _outcome(2, ("meaning_omitted", "meaning_added")),
        _outcome(3, ("meaning_omitted", "meaning_added")),
        _outcome(4, ("meaning_omitted",)),
        _outcome(5, (), "PASS"),
        _outcome(7, ("meaning_omitted", "meaning_added")),
    )

    windows = classify_displacement(outcomes)

    assert [(w.first_index, w.last_index, w.paired_paragraphs) for w in windows] == [(2, 4, 2)]
    assert windows[0].classification == "semantic_boundary_displacement"


def test_a_lone_paired_paragraph_is_not_displacement() -> None:
    assert classify_displacement((_outcome(2, ("meaning_omitted", "meaning_added")),)) == ()


def test_deterministic_resegmentation_preserves_lexical_content() -> None:
    source = (" ".join(["alpha"] * 30) + ".", " ".join(["beta"] * 30) + ".")
    # Two source paragraphs written as one long block plus one short one: the sentences are
    # there, the separator is not where the source has it.
    first = " ".join(["آلفا"] * 30) + ". " + " ".join(["بتا"] * 30) + "."
    target = (first, "اضافه.")

    result = resegment_window(source, target, first_index=1, policy=EN_FA_V1)

    assert result.applicable
    assert lexical_content("\n\n".join(target)) == lexical_content("\n\n".join(result.blocks))
    assert [len(b.split()) for b in result.blocks][0] == 30


def test_deterministic_resegmentation_is_refused_without_a_unique_proof() -> None:
    source = (" ".join(["alpha"] * 30) + ".", " ".join(["beta"] * 30) + ".")
    # Six equal sentences: any split into 3+3 or 2+4 or 4+2 is nearly as good, so no split is
    # determined and the engine must refuse rather than pick one.
    sentence = " ".join(["واژه"] * 10) + "."
    target = (" ".join([sentence] * 2), " ".join([sentence] * 3))

    result = resegment_window(source, target, first_index=1, policy=EN_FA_V1)

    assert not result.applicable
    assert (
        "assignment_not_unique" in result.refusals or result.margin is None or result.margin < 0.25
    )


def test_resegmentation_refuses_an_unaligned_window() -> None:
    assert not resegment_window(("a.", "b."), ("x.",), first_index=1, policy=EN_FA_V1).applicable


# --------------------------------------------------------------------------------------------
# Isolation, provenance, and tampering
# --------------------------------------------------------------------------------------------


def test_the_gate_leaves_mechanical_evidence_byte_identical(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    before = _run_bytes(
        root,
        run_id,
        exclude=("job.yaml", "run.yaml", "semantic-qa", "semantic-revision", "semantic-gate"),
    )

    _gate(root, run_id, ScriptedTransport((BOTH_P2_REVISE, P2_PASS), (GOOD_REVISION,)))

    after = _run_bytes(
        root,
        run_id,
        exclude=("job.yaml", "run.yaml", "semantic-qa", "semantic-revision", "semantic-gate"),
    )
    assert after == before
    job = _job(root, run_id)
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.accepted_attempt == 1


def test_release_modules_import_no_reviser_or_provider_code() -> None:
    root = Path(__file__).parents[1] / "src/book_translator"
    for package in ("release_assembly", "release_verification", "epub"):
        for module in (root / package).glob("*.py"):
            text = module.read_text(encoding="utf-8")
            imports = [line for line in text.splitlines() if line.startswith(("from ", "import "))]
            joined = "\n".join(imports)
            assert "semantic_revision" not in joined
            assert "semantic_gate" not in joined
            assert "semantic_qa" not in joined
            assert "provider_execution.providers" not in joined


def test_model_provenance_is_persisted_for_judge_and_reviser_calls(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    _gate(root, run_id, ScriptedTransport((BOTH_P2_REVISE, P2_PASS), (GOOD_REVISION,)))

    revision = yaml.safe_load(
        next(
            (_run_path(root, run_id) / "jobs/001-001/semantic-revision").glob("*/revision.yaml")
        ).read_text(encoding="utf-8")
    )
    assert revision["request"]["provider"] == "fake"
    assert revision["request"]["model_requested"] == "fake-gate-v1"
    assert revision["request"]["model_returned"] == "fake-gate-v1"
    assert revision["revision_prompt_version"] == "1.0.0"
    judged = yaml.safe_load(
        next(
            (_run_path(root, run_id) / "jobs/001-001/semantic-qa").glob("*/evidence.yaml")
        ).read_text(encoding="utf-8")
    )
    assert judged["requests"][0]["model_returned"] == "fake-gate-v1"


def test_tampered_semantic_evidence_is_detected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    _gate(root, run_id, ScriptedTransport((BOTH_P2_REVISE, P2_PASS), (GOOD_REVISION,)))
    job = _job(root, run_id)
    assert job.latest_semantic_revision_file is not None
    path = root / job.latest_semantic_revision_file
    path.write_text(
        path.read_text(encoding="utf-8").replace("adopted: true", "adopted: true\n"),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseAssemblyError, match="semantic revision evidence"):
        load_effective_acceptance(_job(root, run_id), project_root=root)


def test_no_raw_reasoning_or_prompt_is_persisted(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    _gate(root, run_id, ScriptedTransport((BOTH_P2_REVISE, P2_PASS), (GOOD_REVISION,)))

    for path in (_run_path(root, run_id) / "jobs/001-001").rglob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        assert "<judge-task>" not in text
        assert "<revision-task>" not in text
        assert "bilingual fidelity auditor" not in text
        assert "semantic reviser" not in text


def test_a_dry_run_contacts_nothing_and_changes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run(root)
    transport = ScriptedTransport((ALL_PASS,))
    before = _run_bytes(root, run_id)

    plan, result = _gate(root, run_id, transport, dry_run=True)

    assert transport.judge_calls == []
    assert result.written is None
    assert plan.planned_first_look_calls == 1
    assert _run_bytes(root, run_id) == before
