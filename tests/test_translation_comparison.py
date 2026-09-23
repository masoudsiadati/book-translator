"""Provider-free comparison of two runs' effective translations."""

from pathlib import Path

import pytest
import yaml

from book_translator.models import TranslationComparisonReport
from book_translator.translation_comparison import (
    TranslationComparisonError,
    execute_translation_comparison,
    plan_translation_comparison,
)
from tests.test_semantic_gate import (
    ALL_PASS,
    BOTH_P2_REVISE,
    GOOD_REVISION,
    P2_PASS,
    ScriptedTransport,
    _gate,
    _run,
)
from tests.test_translation_worker import _run_path


def _two_runs(root: Path) -> tuple[str, str]:
    """A legacy-contract run beside a gated marked run of the same synthetic snapshot."""
    first = _run(root, marked=False, gated=False)
    from book_translator.prompt_packaging import (
        MARKED_OUTPUT_CONTRACT_VERSION,
        execute_prompt_packages,
        plan_prompt_packages,
    )
    from book_translator.semantic_gate import SEMANTIC_GATE_POLICY_VERSION
    from book_translator.translation_queue import create_run, load_run_summary, plan_run
    from book_translator.translation_worker import run_translation_worker
    from tests.test_paragraph_contract_pipeline import MARKED_ANSWER
    from tests.test_semantic_gate import _clock
    from tests.test_translation_auto_repair import ScriptedProvider
    from tests.test_translation_queue import NOW
    from tests.test_translation_worker import _options

    snapshot_id = load_run_summary(first, project_root=root).run.snapshot_id
    created = create_run(
        plan_run(
            snapshot_id,
            project_root=root,
            output_contract_version=MARKED_OUTPUT_CONTRACT_VERSION,
            semantic_gate_policy_version=SEMANTIC_GATE_POLICY_VERSION,
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
        provider_factory=lambda _job: ScriptedProvider((MARKED_ANSWER,)),
        now=_clock(10),
    )
    return first, created.run_id


def test_the_comparison_records_both_sides_in_hashes_and_counts(tmp_path: Path) -> None:
    root = tmp_path / "project"
    first, second = _two_runs(root)
    _gate(root, second, ScriptedTransport((BOTH_P2_REVISE, P2_PASS), (GOOD_REVISION,)))

    plan = plan_translation_comparison(first, second, chapter_id="001", project_root=root)
    written = execute_translation_comparison(plan)

    report = TranslationComparisonReport.model_validate(
        yaml.safe_load(written.read_text(encoding="utf-8"))
    )
    assert report.same_snapshot
    assert report.provider_contacted is False and report.literary_judgement is False
    chunk = report.chunks[0]
    assert chunk.first.semantic_status is None
    assert chunk.second.semantic_status == "pass"
    assert chunk.second.semantic_revisions_adopted == 1
    assert chunk.second.revised_paragraph_ids == ["P0002"]
    assert chunk.second.output_contract_version == "persian-translation-marked-paragraphs/1.0.0"
    assert chunk.first.output_contract_version == "persian-translation-text/1.0.0"
    assert chunk.paragraph_identity_aligned
    assert chunk.differing_paragraph_ids == ["P0002"]
    assert report.first_chapter_sha256 != report.second_chapter_sha256
    text = written.read_text(encoding="utf-8")
    assert "آلفا" not in text and "گاما" not in text


def test_the_comparison_reads_the_effective_revised_translation(tmp_path: Path) -> None:
    root = tmp_path / "project"
    first, second = _two_runs(root)
    _gate(root, second, ScriptedTransport((BOTH_P2_REVISE, P2_PASS), (GOOD_REVISION,)))

    plan = plan_translation_comparison(first, second, chapter_id="001", project_root=root)

    side = plan.report.chunks[0].second
    assert "semantic-revision" in side.translation_file.as_posix()
    assert side.acceptance_origin == "initial_qa"


def test_an_unjudged_second_run_reads_as_not_judged_never_passed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    first, second = _two_runs(root)

    plan = plan_translation_comparison(first, second, chapter_id="001", project_root=root)

    assert plan.report.chunks[0].second.semantic_status is None
    assert plan.report.chunks[0].second.semantic_judge_calls == 0


def test_the_comparison_writes_nothing_into_either_run(tmp_path: Path) -> None:
    root = tmp_path / "project"
    first, second = _two_runs(root)
    _gate(root, second, ScriptedTransport((ALL_PASS,)))
    before = {
        p: p.read_bytes()
        for r in (first, second)
        for p in _run_path(root, r).rglob("*")
        if p.is_file()
    }

    written = execute_translation_comparison(
        plan_translation_comparison(first, second, chapter_id="001", project_root=root)
    )

    assert written.is_relative_to(root / "workspace/audits/translation-comparison")
    assert {
        p: p.read_bytes()
        for r in (first, second)
        for p in _run_path(root, r).rglob("*")
        if p.is_file()
    } == before


def test_a_chapter_absent_from_a_run_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    first, second = _two_runs(root)

    with pytest.raises(TranslationComparisonError):
        plan_translation_comparison(first, second, chapter_id="999", project_root=root)
