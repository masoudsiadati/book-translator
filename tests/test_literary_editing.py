"""Phase 4 literary editing contracts and safety orchestration.

All story content in this module is synthetic.  Tests intentionally exercise refusal and fallback
paths because literary fluency is never allowed to weaken the accepted semantic layer.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from book_translator.literary_editing import (
    LiteraryCorrectionPolicy,
    LiteraryEditPolicy,
    LiteraryResponseError,
    ScriptedLiteraryProvider,
    TransportLiteraryRoles,
    execute_literary_run,
    parse_continuity_response,
    parse_correction_response,
    parse_editor_response,
    plan_literary_run,
    render_continuity_message,
    render_editor_message,
)
from book_translator.literary_editing.blind import BlindChapterInput, build_blind_comparison
from book_translator.literary_editing.engine import (
    ChapterParagraph,
    assess_mechanical_candidate,
    canonical_document,
    marked_paragraphs,
    measure_diff,
    merge_editor_responses,
    plan_sections,
    terminology_differences,
    terminology_json,
)
from book_translator.literary_editing.parsing import (
    CONTINUITY_SCHEMA_VERSION,
    CORRECTION_SCHEMA_VERSION,
    EDITOR_SCHEMA_VERSION,
)
from book_translator.models import (
    BookLiteraryProfile,
    CandidateStatus,
    ContinuityFindingCode,
    DecisionSource,
    LiteraryDiffClass,
    LiteraryEditReason,
    LiteraryParagraphFidelity,
    PersianLiteraryStyleContract,
    ProviderErrorCategory,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyEnforcement,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.provider_execution.errors import ErrorClassification, ProviderExecutionError
from book_translator.provider_execution.protocol import ProviderRequest
from book_translator.release_assembly import execute_release_assembly, plan_release_assembly
from book_translator.semantic_qa import TransportSemanticJudge
from tests.test_paragraph_contract_pipeline import FIRST, SECOND
from tests.test_semantic_gate import ALL_PASS, ScriptedTransport, _gate, _run, _verdicts

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
RUN_ID = "lr-" + "a" * 24


def _paragraph(index: int, *, width: int = 1) -> ChapterParagraph:
    suffix = " بلند" * width
    return ChapterParagraph(
        paragraph_id=f"P{index:04d}",
        paragraph_index=index,
        chunk_id="001-001",
        chunk_paragraph_index=index,
        source_text=f"Synthetic source {index}{suffix}.",
        faithful_text=f"متن وفادار {index}{suffix}.",
    )


def _editor_payload(
    *paragraphs: tuple[str, str, bool, list[str]], requests: list[dict[str, object]] | None = None
) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "editor_schema_version": EDITOR_SCHEMA_VERSION,
            "paragraphs": [
                {
                    "paragraph_id": paragraph_id,
                    "edited_text": text,
                    "changed": changed,
                    "reasons": reasons,
                }
                for paragraph_id, text, changed, reasons in paragraphs
            ],
            "multi_paragraph_edit_requests": requests or [],
        },
        ensure_ascii=False,
    )


def _correction_payload(paragraph_id: str, text: str) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "correction_schema_version": CORRECTION_SCHEMA_VERSION,
            "paragraphs": [
                {
                    "paragraph_id": paragraph_id,
                    "edited_text": text,
                    "changed": True,
                    "reasons": ["awkward_wording"],
                }
            ],
        },
        ensure_ascii=False,
    )


def _continuity_payload(*, observation: str | None = None) -> str:
    findings = []
    if observation is not None:
        findings.append(
            {
                "code": "unnatural_persian",
                "severity": "advisory",
                "paragraph_ids": ["P0001"],
                "observation": observation,
            }
        )
    return json.dumps(
        {
            "schema_version": "1.0",
            "continuity_schema_version": CONTINUITY_SCHEMA_VERSION,
            "findings": findings,
        },
        ensure_ascii=False,
    )


def _style() -> PersianLiteraryStyleContract:
    path = Path(__file__).parents[1] / "config/persian-literary-style.yaml"
    import yaml

    return PersianLiteraryStyleContract.model_validate(yaml.safe_load(path.read_text("utf-8")))


def _registry(enforcement: TerminologyEnforcement) -> TerminologyRegistry:
    candidate_id = "tc-" + "1" * 24
    return TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
        candidates=[
            TerminologyCandidate(
                candidate_id=candidate_id,
                source_term="Moon Tower",
                classification=TerminologyType.PLACE,
                confidence=1,
                status=CandidateStatus.APPROVED,
                provenance=DecisionSource.USER,
                aliases=["Tower of the Moon"],
            )
        ],
        decisions=[
            TerminologyDecision(
                decision_id="td-" + "2" * 24,
                candidate_id=candidate_id,
                target_term="برج ماه",
                decision_source=DecisionSource.USER,
                locked=enforcement is TerminologyEnforcement.EXACT,
                enforcement=enforcement,
                contextual_notes=["عنوان مکان است."],
            )
        ],
    )


def test_editor_response_preserves_exact_identity_order_and_unchanged_bytes() -> None:
    payload = _editor_payload(
        ("P0001", "متن بهتر.", True, ["flow"]),
        ("P0002", "بدون تغییر.", False, ["no_change"]),
    )

    response = parse_editor_response(
        payload,
        expected_ids=("P0001", "P0002"),
        faithful_texts=("متن اولیه.", "بدون تغییر."),
    )

    assert [item.paragraph_id for item in response.paragraphs] == ["P0001", "P0002"]
    assert response.paragraphs[1].edited_text.encode() == "بدون تغییر.".encode()


@pytest.mark.parametrize(
    "paragraphs",
    [
        (("P0002", "دو", True, ["flow"]), ("P0001", "یک", True, ["flow"])),
        (("P0001", "یک", True, ["flow"]),),
        (
            ("P0001", "یک", True, ["flow"]),
            ("P0002", "دو", True, ["flow"]),
            ("P0003", "سه", True, ["flow"]),
        ),
    ],
)
def test_editor_response_rejects_reordered_missing_and_extra_identities(
    paragraphs: tuple[tuple[str, str, bool, list[str]], ...],
) -> None:
    with pytest.raises(LiteraryResponseError, match="identities"):
        parse_editor_response(
            _editor_payload(*paragraphs),
            expected_ids=("P0001", "P0002"),
            faithful_texts=("الف", "ب"),
        )


def test_editor_response_derives_and_audits_a_false_changed_flag() -> None:
    response = parse_editor_response(
        _editor_payload(("P0001", "متن دیگر", False, ["no_change"])),
        expected_ids=("P0001",),
        faithful_texts=("متن",),
    )

    paragraph = response.paragraphs[0]
    assert paragraph.changed
    assert paragraph.changed_flag_normalized
    assert paragraph.reasons == []


def test_editor_contract_has_no_fidelity_or_reasoning_escape_hatch() -> None:
    payload = json.loads(_editor_payload(("P0001", "متن", False, ["no_change"])))
    payload["fidelity_verdict"] = "PASS"
    with pytest.raises(LiteraryResponseError, match="contract"):
        parse_editor_response(json.dumps(payload), expected_ids=("P0001",), faithful_texts=("متن",))
    payload.pop("fidelity_verdict")
    payload["chain_of_thought"] = "hidden reasoning"
    with pytest.raises(LiteraryResponseError, match="contract"):
        parse_editor_response(json.dumps(payload), expected_ids=("P0001",), faithful_texts=("متن",))


def test_multi_paragraph_restructure_is_explicit_and_keeps_requested_text_unchanged() -> None:
    paragraphs = (_paragraph(1), _paragraph(2))
    sections = plan_sections(paragraphs)
    response = parse_editor_response(
        _editor_payload(
            ("P0001", "بازنویسی یک", True, ["flow"]),
            ("P0002", "بازنویسی دو", True, ["flow"]),
            requests=[{"paragraph_ids": ["P0001", "P0002"], "reason": "cross_paragraph_syntax"}],
        ),
        expected_ids=("P0001", "P0002"),
        faithful_texts=tuple(item.faithful_text for item in paragraphs),
    )
    values, _, governed = merge_editor_responses(paragraphs, sections, (response,))
    assert values == [item.faithful_text for item in paragraphs]
    assert governed == {"P0001", "P0002"}


def test_whole_chapter_is_the_default_editing_unit() -> None:
    paragraphs = tuple(_paragraph(index) for index in range(1, 8))
    sections = plan_sections(paragraphs)
    assert len(sections) == 1
    assert sections[0].visible_indexes == sections[0].editable_indexes == tuple(range(7))


def test_oversized_chapter_sections_overlap_but_core_ownership_tiles_once() -> None:
    paragraphs = tuple(_paragraph(index, width=8) for index in range(1, 8))
    policy = LiteraryEditPolicy(max_request_characters=220, section_overlap_paragraphs=1)
    sections = plan_sections(paragraphs, policy=policy)
    assert len(sections) > 1
    assert [index for section in sections for index in section.editable_indexes] == list(range(7))
    assert set(sections[0].visible_indexes) & set(sections[1].visible_indexes)
    assert not (set(sections[0].editable_indexes) & set(sections[1].editable_indexes))


def test_section_planner_rebalances_a_tiny_final_core() -> None:
    paragraphs = tuple(_paragraph(index, width=12) for index in range(1, 25))
    policy = LiteraryEditPolicy(
        max_request_characters=1_700,
        section_overlap_paragraphs=2,
        minimum_section_core_paragraphs=8,
    )
    sections = plan_sections(paragraphs, policy=policy)
    if len(sections) > 1:
        assert len(sections[-1].editable_indexes) >= 8
    assert [index for section in sections for index in section.editable_indexes] == list(range(24))


def test_marked_context_uses_chapter_global_identity_in_sections() -> None:
    paragraphs = (_paragraph(7), _paragraph(8))
    rendered = marked_paragraphs(paragraphs, source=False)
    assert rendered.count("[[P0007]]") == 1
    assert rendered.count("[[P0008]]") == 1
    assert "[[P0001]]" not in rendered


def test_diff_policy_allows_broad_edits_and_marks_only_extremes_suspicious() -> None:
    normal = measure_diff("او به خانه رفت.", "او آرام به خانه رفت.")
    broad = measure_diff(
        "او به خانه رفت و در را بست.", "او راه خانه را پیش گرفت و در را هم پشت سر بست."
    )
    suspicious = measure_diff("او رفت.", "شب تا صبح، سپاهی بزرگ بی‌هیچ هشداری شهر را ویران کرد.")
    assert normal.diff_class is LiteraryDiffClass.NORMAL
    assert broad.diff_class in {LiteraryDiffClass.BROAD, LiteraryDiffClass.SUSPICIOUS}
    assert suspicious.diff_class is LiteraryDiffClass.SUSPICIOUS


def test_terminology_prompt_preserves_type_alias_lock_mode_and_notes() -> None:
    prompt = json.loads(terminology_json(_registry(TerminologyEnforcement.EXACT), "Moon Tower"))
    assert prompt == [
        {
            "candidate_id": "tc-" + "1" * 24,
            "canonical_target": "برج ماه",
            "classification": "place",
            "contextual_notes": ["عنوان مکان است."],
            "decision_id": "td-" + "2" * 24,
            "enforcement": "exact",
            "locked": True,
            "required": True,
            "source_aliases": ["Tower of the Moon"],
            "source_term": "Moon Tower",
        }
    ]


def test_terminology_differences_detect_canonical_count_changes() -> None:
    registry = _registry(TerminologyEnforcement.EXACT)
    differences = terminology_differences(
        "They entered Moon Tower.", "به برج ماه رفتند.", "به برج خورشید رفتند.", registry
    )
    assert len(differences) == 1
    assert differences[0].before_count == 1
    assert differences[0].after_count == 0


def test_mechanical_qa_localizes_a_new_locked_term_regression() -> None:
    paragraph = ChapterParagraph(
        paragraph_id="P0001",
        paragraph_index=1,
        chunk_id="001-001",
        chunk_paragraph_index=1,
        source_text="They entered Moon Tower.",
        faithful_text="به برج ماه رفتند.",
    )
    result = assess_mechanical_candidate(
        (paragraph,), ["به برج خورشید رفتند."], registry=_registry(TerminologyEnforcement.EXACT)
    )
    assert any(code.startswith("terminology_regression:") for code in result.paragraph_refusals[1])


def test_style_contract_is_generic_and_book_profile_is_separate() -> None:
    style = _style()
    serialized = json.dumps(style.model_dump(mode="json"), ensure_ascii=False)
    assert style.contract_version == "1.0.0"
    assert "Wheel of Time" not in serialized and "Samara" not in serialized
    profile_path = Path(__file__).parents[1] / "config/literary-profile.yaml"
    import yaml

    profile = BookLiteraryProfile.model_validate(yaml.safe_load(profile_path.read_text("utf-8")))
    chapter_048 = next(item for item in profile.chapter_guidance if item.chapter_id == "048")
    assert chapter_048.negative_controls


def test_editor_prompt_assigns_persian_quality_but_not_fidelity_grading() -> None:
    paragraph = _paragraph(1)
    message = render_editor_message(
        chapter_id="001",
        paragraph_ids=("P0001",),
        editable_ids=("P0001",),
        marked_source=marked_paragraphs((paragraph,), source=True),
        marked_faithful=marked_paragraphs((paragraph,), source=False),
        terminology_json="[]",
        style=_style(),
        profile=None,
        section_label="section-001",
    )
    assert "faithful-persian-draft" in message
    assert "english-source-context" in message
    assert "semantic-findings" not in message
    assert "previous literary score" not in message
    for reason in LiteraryEditReason:
        assert reason.value in message


def test_continuity_contract_is_read_only_and_rejects_unknown_identities() -> None:
    payload = json.loads(_continuity_payload(observation="ترکیب ناآشناست."))
    payload["findings"][0]["edited_text"] = "بازنویسی"
    with pytest.raises(LiteraryResponseError, match="contract"):
        parse_continuity_response(json.dumps(payload), known_ids=frozenset({"P0001"}))
    payload["findings"][0].pop("edited_text")
    payload["findings"][0]["paragraph_ids"] = ["P9999"]
    with pytest.raises(LiteraryResponseError, match="unknown"):
        parse_continuity_response(json.dumps(payload), known_ids=frozenset({"P0001"}))


def test_continuity_prompt_does_not_request_source_fidelity_or_rewriting() -> None:
    message = render_continuity_message(
        chapter_id="001",
        marked_chapter="[[P0001]]\nمتن.\n",
        terminology_json="[]",
        style=_style(),
        profile=None,
    )
    assert "edited_text" not in message
    assert "english-source" not in message
    assert "at most 4 paragraph_ids" in message
    assert "1 to 1000" in message
    for code in ContinuityFindingCode:
        assert code.value in message


def test_correction_contract_cannot_expand_the_failed_identity_set() -> None:
    payload = _correction_payload("P0002", "اصلاح")
    with pytest.raises(LiteraryResponseError, match="identity"):
        parse_correction_response(payload, expected_ids=("P0001",))


def test_continuity_observation_is_bounded_but_allows_a_compact_paragraph() -> None:
    payload = json.loads(_continuity_payload(observation="ن" * 800))
    response = parse_continuity_response(json.dumps(payload), known_ids=frozenset({"P0001"}))
    assert len(response.findings[0].observation) == 800
    payload["findings"][0]["observation"] = "ن" * 1001
    with pytest.raises(LiteraryResponseError, match="contract"):
        parse_continuity_response(json.dumps(payload), known_ids=frozenset({"P0001"}))


def test_transport_preserves_a_prose_free_provider_failure_category() -> None:
    class FailingProvider:
        provider_name = "fake"
        adapter_version = "1.0.0"

        def execute(self, request: ProviderRequest):  # type: ignore[no-untyped-def]
            raise ProviderExecutionError(
                ErrorClassification(
                    ProviderErrorCategory.SERVER_ERROR,
                    retryable=True,
                    ambiguous=False,
                    safe_automatic_retry=False,
                    message="synthetic upstream timeout",
                )
            )

    from book_translator.literary_editing import LiteraryProviderError

    roles = TransportLiteraryRoles(FailingProvider())
    with pytest.raises(LiteraryProviderError) as caught:
        roles.edit(
            run_id="tr-aaaaaaaaaaaa-001",
            chapter_id="001",
            sequence=1,
            user_message="synthetic",
            expected_ids=("P0001",),
            faithful_texts=("متن",),
        )
    assert caught.value.code == "server_error"
    assert not caught.value.retryable
    assert "synthetic upstream timeout" in str(caught.value)


def test_paragraph_record_rejects_changed_text_without_fresh_semantic_pass() -> None:
    from book_translator.models import LiteraryEvidenceReference, LiteraryParagraphRecord

    reference = LiteraryEvidenceReference(
        kind="existing_semantic_gate", file=Path("evidence.yaml"), sha256="a" * 64
    )
    with pytest.raises(ValidationError, match="fresh semantic pass"):
        LiteraryParagraphRecord(
            paragraph_id="P0001",
            paragraph_index=1,
            chunk_id="001-001",
            chunk_paragraph_id="P0001",
            chunk_paragraph_index=1,
            source_sha256="a" * 64,
            faithful_sha256="b" * 64,
            final_sha256="c" * 64,
            changed=True,
            attempted_change=True,
            reasons=[LiteraryEditReason.FLOW],
            diff_class=LiteraryDiffClass.NORMAL,
            fidelity=LiteraryParagraphFidelity.REUSED,
            semantic_evidence=[reference],
        )


def test_blind_comparison_hides_mapping_and_keeps_answer_key_separate() -> None:
    build = build_blind_comparison(
        RUN_ID,
        (
            BlindChapterInput(
                chapter_id="001",
                paragraph_ids=("P0001", "P0002"),
                v1_blocks=("نسخه قدیم یک.", "نسخه قدیم دو."),
                literary_blocks=("نسخه تازه یک.", "نسخه تازه دو."),
                changed_indexes=(0, 1),
            ),
        ),
        document_file=Path("blind/comparison.md"),
        answer_key_file=Path("blind/answer-key.yaml"),
        created_at=NOW,
    )
    assert "v1_published" not in build.document
    assert "v2_literary_edited" not in build.document
    assert "Version A" in build.document and "Version B" in build.document
    assert set(build.artifact.rubric) == {
        "روانی",
        "طبیعی بودن فارسی",
        "لحن رمان",
        "دیالوگ",
        "انسجام",
        "وفاداری ادراک‌شده",
        "ترجیح کلی",
    }
    assert build.key.entries[0].version_a != build.key.entries[0].version_b


def _synthetic_plan(root: Path):  # type: ignore[no-untyped-def]
    run_id = _run(root)
    _gate(root, run_id, ScriptedTransport((ALL_PASS,)))
    release_plan = plan_release_assembly(
        run_id,
        output="workspace/releases/synthetic-v1",
        project_root=root,
        now=lambda: NOW,
    )
    execute_release_assembly(release_plan, project_root=root)
    style_file = Path(__file__).parents[1] / "config/persian-literary-style.yaml"
    return plan_literary_run(
        run_id,
        project_root=root,
        chapter_ids=("001",),
        provider="fake",
        model="fake-literary-v1",
        style_file=style_file,
        historical_run_id=run_id,
        historical_release=release_plan.output_path,
        now=lambda: NOW,
    )


def test_changed_paragraph_gets_fresh_evidence_and_unchanged_reuses_prior_evidence(
    tmp_path: Path,
) -> None:
    plan = _synthetic_plan(tmp_path / "project")
    editor = _editor_payload(
        ("P0001", "آلفا و بتا.", True, ["flow"]),
        ("P0002", SECOND, False, ["no_change"]),
    )
    invalid_continuity = json.loads(_continuity_payload(observation="synthetic"))
    invalid_continuity["findings"][0]["code"] = "invented_voice_code"
    literary_provider = ScriptedLiteraryProvider(
        [editor, json.dumps(invalid_continuity), _continuity_payload()]
    )
    result = execute_literary_run(
        plan,
        literary=TransportLiteraryRoles(literary_provider, model="fake-literary-v1"),
        judge=TransportSemanticJudge(
            ScriptedTransport((_verdicts(("P0001", "PASS", [])),)),
            model="fake-gate-v1",
        ),
        now=lambda: NOW,
    )

    chapter = result.artifact.chapters[0]
    assert chapter.changed_paragraph_ids == ["P0001"]
    assert chapter.paragraphs[0].fidelity is LiteraryParagraphFidelity.FRESH_PASS
    assert chapter.paragraphs[1].fidelity is LiteraryParagraphFidelity.REUSED
    assert chapter.paragraphs[0].semantic_evidence[0].kind == "literary_semantic_rejudge"
    assert chapter.paragraphs[1].semantic_evidence[0].kind == "existing_semantic_gate"
    assert result.artifact.semantic_rejudge_calls == 1
    assert result.artifact.literary_editor_calls == 1
    assert result.artifact.continuity_reviewer_calls == 2
    assert result.artifact.literary_correction_calls == 0
    continuity_calls = [
        call for call in chapter.provider_calls if call.role.value == "continuity_reviewer"
    ]
    assert [call.status for call in continuity_calls] == ["output_rejected", "completed"]
    assert all(call.model_requested is not None for call in chapter.provider_calls)
    assert all(call.model_returned is not None for call in chapter.provider_calls)
    assert result.destination is not None and (result.destination / "blind/comparison.md").is_file()


def test_completed_chapter_is_carried_without_repeating_provider_calls(tmp_path: Path) -> None:
    first_plan = _synthetic_plan(tmp_path / "project")
    editor = _editor_payload(
        ("P0001", "آلفا و بتا.", True, ["flow"]),
        ("P0002", SECOND, False, ["no_change"]),
    )
    first_literary = ScriptedLiteraryProvider([editor, _continuity_payload()])
    first = execute_literary_run(
        first_plan,
        literary=TransportLiteraryRoles(first_literary, model="fake-literary-v1"),
        judge=TransportSemanticJudge(
            ScriptedTransport((_verdicts(("P0001", "PASS", [])),)),
            model="fake-literary-v1",
        ),
        now=lambda: NOW,
    )
    style_file = Path(__file__).parents[1] / "config/persian-literary-style.yaml"
    second_plan = plan_literary_run(
        first_plan.input_run_id,
        project_root=first_plan.project_root,
        chapter_ids=("001",),
        provider="fake",
        model="fake-literary-v1",
        style_file=style_file,
        historical_run_id=first_plan.historical_run_id,
        historical_release=first_plan.project_root / (first_plan.historical_release or Path()),
        reuse_literary_run_id=first.artifact.literary_run_id,
        now=lambda: NOW,
    )
    second_literary = ScriptedLiteraryProvider([])
    second_judge = ScriptedTransport(())

    second = execute_literary_run(
        second_plan,
        literary=TransportLiteraryRoles(second_literary, model="fake-literary-v1"),
        judge=TransportSemanticJudge(second_judge, model="fake-literary-v1"),
        now=lambda: NOW,
    )

    assert second_literary.requests == [] and second_judge.judge_calls == []
    carried = second.artifact.chapters[0]
    assert carried.reused_from_literary_run_id == first.artifact.literary_run_id
    assert carried.output_sha256 == first.artifact.chapters[0].output_sha256
    assert all(
        call.reused_from_literary_run_id == first.artifact.literary_run_id
        for call in carried.provider_calls
    )
    assert second.destination is not None


@pytest.mark.parametrize(
    "finding_code",
    (
        "meaning_omitted",
        "meaning_added",
        "negation_changed",
        "speaker_attribution_risk",
    ),
)
def test_fidelity_regressions_exhaust_retry_and_restore_faithful_paragraph(
    tmp_path: Path, finding_code: str
) -> None:
    plan = _synthetic_plan(tmp_path / "project")
    editor = _editor_payload(
        ("P0001", "معنای افزوده.", True, ["flow"]),
        ("P0002", SECOND, False, ["no_change"]),
    )
    literary_provider = ScriptedLiteraryProvider(
        [
            editor,
            _correction_payload("P0001", "هنوز معنای افزوده."),
            _correction_payload("P0001", "باز هم معنای افزوده."),
            _continuity_payload(),
        ]
    )
    revise = _verdicts(
        (
            "P0001",
            "REVISE",
            [
                {
                    "code": finding_code,
                    "severity": "HIGH",
                    "source_evidence": "synthetic",
                    "explanation": "added material",
                    "confidence": 0.99,
                }
            ],
        )
    )
    result = execute_literary_run(
        plan,
        literary=TransportLiteraryRoles(literary_provider, model="fake-literary-v1"),
        judge=TransportSemanticJudge(
            ScriptedTransport((revise, revise, revise)), model="fake-gate-v1"
        ),
        correction_policy=LiteraryCorrectionPolicy(max_correction_rounds=2),
        now=lambda: NOW,
    )

    chapter = result.artifact.chapters[0]
    first = chapter.paragraphs[0]
    assert first.fidelity is LiteraryParagraphFidelity.FALLBACK
    assert first.fallback_reason == "retry_budget_exhausted"
    assert first.faithful_sha256 == first.final_sha256
    assert not first.changed and first.attempted_change
    assert chapter.fallback_count == 1
    assert result.artifact.literary_correction_calls == 2
    assert result.artifact.semantic_rejudge_calls == 3
    assert result.destination is not None
    chapter_text = (result.destination / "chapters/001/chapter.md").read_text("utf-8")
    assert chapter_text == canonical_document([FIRST, SECOND])


def test_dry_run_makes_no_provider_calls_and_creates_no_literary_directory(
    tmp_path: Path,
) -> None:
    plan = _synthetic_plan(tmp_path / "project")
    literary_provider = ScriptedLiteraryProvider([])
    result = execute_literary_run(
        plan,
        literary=TransportLiteraryRoles(literary_provider),
        judge=TransportSemanticJudge(ScriptedTransport(()), model="fake-gate-v1"),
        dry_run=True,
    )
    assert result.destination is None
    assert literary_provider.requests == []
    assert not plan.destination.exists() and not plan.staging.exists()
