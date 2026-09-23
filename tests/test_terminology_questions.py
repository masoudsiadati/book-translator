"""Provisional terminology decisions and the durable Open Question Registry.

All content here is synthetic; no real book prose appears in any fixture.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    DecisionState,
    EvidenceProvenance,
    EvidenceStatus,
    OpenQuestionRegistry,
    QuestionCategory,
    QuestionResearch,
    QuestionResolution,
    QuestionStatus,
    ResearchState,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyDecisionCorrection,
    TerminologyOccurrence,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.models.terminology_remediation import TerminologyRemediationPlan
from book_translator.prompt_packaging.rendering import render_terminology
from book_translator.terminology import (
    TerminologyError,
    candidate_identity,
    correct_decision,
    registry_identity,
    reject_candidate,
    save_registry,
    set_decision,
    validate_terminology,
)
from book_translator.terminology.lineage import (
    RegistryEventKind,
    RegistryLineageError,
    explain_registry_lineage,
    registry_transitions,
)
from book_translator.terminology.question_workflow import (
    analyze_impact,
    execute_decision_correction,
    load_corrections,
    load_or_empty_questions,
    plan_decision_correction,
    publish_questions,
    readiness,
)
from book_translator.terminology.questions import (
    QuestionError,
    build_readiness,
    empty_questions,
    load_questions,
    question_identity,
    questions_identity,
    resolve_question,
    save_questions,
    upsert_question,
)

runner = CliRunner()
NOW = datetime(2026, 3, 4, tzinfo=UTC)


def base_registry() -> TerminologyRegistry:
    return TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )


def with_candidate(
    registry: TerminologyRegistry,
    source_term: str,
    *,
    classification: TerminologyType = TerminologyType.PERSON,
    chapter: str = "001",
    chunk: str = "001-002",
) -> TerminologyRegistry:
    candidate = TerminologyCandidate(
        candidate_id=candidate_identity(source_term, registry.source_language),
        source_term=source_term,
        classification=classification,
        confidence=0.9,
        provenance=DecisionSource.AI,
        occurrences=[TerminologyOccurrence(chapter_id=chapter, chunk_id=chunk, count=1)],
    )
    return registry.model_copy(update={"candidates": [*registry.candidates, candidate]})


def provisional_registry() -> TerminologyRegistry:
    registry = with_candidate(base_registry(), "Aunt Veyra")
    return set_decision(
        registry,
        source_term="Aunt Veyra",
        target_term="خاله ویرا",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.AI,
        locked=False,
        decision_state=DecisionState.PROVISIONAL,
    )


def questions_with(registry: TerminologyRegistry, **overrides: object) -> OpenQuestionRegistry:
    candidate_id = candidate_identity("Aunt Veyra", registry.source_language)
    decision = next(item for item in registry.decisions if item.candidate_id == candidate_id)
    payload: dict[str, object] = {
        "candidate_id": candidate_id,
        "source_term": "Aunt Veyra",
        "category": QuestionCategory.KINSHIP_SIDE,
        "question": "Is Veyra a maternal or paternal aunt?",
        "created_at": NOW,
        "decision_id": decision.decision_id,
        "provisional_target": decision.target_term,
    }
    payload.update(overrides)
    return upsert_question(empty_questions("synthetic-book"), **payload)  # type: ignore[arg-type]


# --- decision state -------------------------------------------------------------------


def test_existing_decisions_are_backward_compatible_as_confirmed() -> None:
    """A decision persisted before decision_state existed must load as confirmed."""
    legacy = {
        "decision_id": "td-" + "0" * 24,
        "candidate_id": "tc-" + "0" * 24,
        "target_term": "ویرا",
        "decision_source": "ai",
        "locked": False,
        "required": True,
        "competing_variants": [],
        "contextual_notes": [],
    }
    decision = TerminologyDecision.model_validate(legacy)
    assert decision.decision_state is DecisionState.CONFIRMED


def test_ai_provisional_decision_is_recorded_unlocked() -> None:
    registry = provisional_registry()
    decision = registry.decisions[0]
    assert decision.decision_source is DecisionSource.AI
    assert decision.decision_state is DecisionState.PROVISIONAL
    assert decision.locked is False


def test_provisional_decision_cannot_be_locked_at_the_contract_or_the_writer() -> None:
    with pytest.raises(ValidationError):
        TerminologyDecision(
            decision_id="td-" + "0" * 24,
            candidate_id="tc-" + "0" * 24,
            target_term="ویرا",
            decision_source=DecisionSource.USER,
            decision_state=DecisionState.PROVISIONAL,
            locked=True,
        )
    with pytest.raises(TerminologyError, match="provisional"):
        set_decision(
            with_candidate(base_registry(), "Veyra"),
            source_term="Veyra",
            target_term="ویرا",
            classification=TerminologyType.PERSON,
            decision_source=DecisionSource.USER,
            locked=True,
            decision_state=DecisionState.PROVISIONAL,
        )


def test_ownership_state_and_lock_are_independent() -> None:
    """user+confirmed+locked, ai+confirmed+unlocked and ai+provisional+unlocked all hold."""
    combinations = (
        (DecisionSource.USER, DecisionState.CONFIRMED, True),
        (DecisionSource.AI, DecisionState.CONFIRMED, False),
        (DecisionSource.AI, DecisionState.PROVISIONAL, False),
        (DecisionSource.USER, DecisionState.PROVISIONAL, False),
    )
    for source, state, locked in combinations:
        registry = set_decision(
            with_candidate(base_registry(), "Veyra"),
            source_term="Veyra",
            target_term="ویرا",
            classification=TerminologyType.PERSON,
            decision_source=source,
            locked=locked,
            decision_state=state,
        )
        decision = registry.decisions[0]
        assert (decision.decision_source, decision.decision_state, decision.locked) == (
            source,
            state,
            locked,
        )


# --- open questions -------------------------------------------------------------------


def test_question_ids_are_stable_across_rewording_and_retarget() -> None:
    registry = provisional_registry()
    first = questions_with(registry)
    second = questions_with(
        registry, question="Which side is Veyra on?", provisional_target="عمه ویرا"
    )
    assert first.questions[0].question_id == second.questions[0].question_id
    assert first.questions[0].question_id == question_identity(
        candidate_identity("Aunt Veyra", "en"), QuestionCategory.KINSHIP_SIDE
    )


def test_question_registry_serialization_is_deterministic(tmp_path: Path) -> None:
    registry = questions_with(provisional_registry())
    path = tmp_path / "questions.yaml"
    save_questions(path, registry)
    first = path.read_bytes()
    save_questions(path, load_questions(path))
    assert path.read_bytes() == first
    assert questions_identity(load_questions(path)) == questions_identity(registry)


def test_a_candidate_holds_at_most_one_open_question_per_category() -> None:
    registry = questions_with(provisional_registry())
    duplicate = registry.questions[0].model_copy(update={"question_id": "tq-" + "b" * 24})
    with pytest.raises(ValidationError, match="at most one open question"):
        OpenQuestionRegistry(
            schema_version="1.0",
            version="1.0.0",
            book_id="synthetic-book",
            questions=[registry.questions[0], duplicate],
        )


def test_resolved_question_is_not_silently_overwritten() -> None:
    registry = provisional_registry()
    questions = questions_with(registry)
    resolved = resolve_question(
        questions,
        question_id=questions.questions[0].question_id,
        resolution=QuestionResolution(
            resolved_target="عمه ویرا",
            provenance=EvidenceProvenance.OTHER_VOLUMES,
            rationale="A later volume names the paternal line.",
            resolved_at=NOW,
            supersedes_target="خاله ویرا",
        ),
    )
    assert resolved.questions[0].status is QuestionStatus.RESOLVED
    assert resolved.questions[0].resolution is not None
    assert resolved.questions[0].resolution.supersedes_target == "خاله ویرا"
    with pytest.raises(QuestionError, match="reopened explicitly"):
        upsert_question(
            resolved,
            candidate_id=resolved.questions[0].candidate_id,
            source_term="Aunt Veyra",
            category=QuestionCategory.KINSHIP_SIDE,
            question="Is Veyra a maternal or paternal aunt?",
            created_at=NOW,
        )


def test_research_ladder_records_each_channel_independently() -> None:
    questions = questions_with(
        provisional_registry(),
        research=QuestionResearch(
            current_book=ResearchState.CHECKED_INCONCLUSIVE,
            web=ResearchState.NOT_CHECKED,
            other_volumes=ResearchState.UNAVAILABLE,
        ),
        evidence_status=EvidenceStatus.INCONCLUSIVE,
    )
    research = questions.questions[0].research
    assert research.current_book is ResearchState.CHECKED_INCONCLUSIVE
    assert research.web is ResearchState.NOT_CHECKED
    assert research.other_volumes is ResearchState.UNAVAILABLE


# --- readiness ------------------------------------------------------------------------


def test_non_blocking_question_does_not_block_readiness() -> None:
    registry = provisional_registry()
    report = build_readiness(registry, questions_with(registry, blocking=False))
    assert report.ready is True
    assert report.blocking_reasons == []
    assert report.provisional_decisions == 1
    assert report.confirmed_decisions == 0
    assert report.open_non_blocking_questions == 1
    assert report.open_blocking_questions == 0


def test_blocking_question_blocks_readiness() -> None:
    registry = provisional_registry()
    report = build_readiness(registry, questions_with(registry, blocking=True))
    assert report.ready is False
    assert report.open_blocking_questions == 1
    assert any("blocking open question" in reason for reason in report.blocking_reasons)


def test_provisional_decision_without_a_question_blocks_readiness() -> None:
    """A working choice with nothing recorded about it is exactly the state to refuse."""
    registry = provisional_registry()
    report = build_readiness(registry, empty_questions("synthetic-book"))
    assert report.ready is False
    assert report.provisional_without_question == ["Aunt Veyra"]


def test_confirmed_only_registry_is_ready() -> None:
    registry = set_decision(
        with_candidate(base_registry(), "Veyra"),
        source_term="Veyra",
        target_term="ویرا",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    report = build_readiness(registry, empty_questions("synthetic-book"))
    assert report.ready is True
    assert report.confirmed_decisions == 1
    assert report.provisional_decisions == 0


# --- corrections ----------------------------------------------------------------------


def test_incorrect_user_owned_decision_is_corrected_through_lineage() -> None:
    registry = with_candidate(base_registry(), "Aunt Veyra")
    registry = set_decision(
        registry,
        source_term="Aunt Veyra",
        target_term="خاله ویرا",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    corrected, previous = correct_decision(
        registry,
        source_term="Aunt Veyra",
        target_term="خاله ویرا",
        decision_source=DecisionSource.AI,
        decision_state=DecisionState.PROVISIONAL,
        locked=False,
        reason="ownership was recorded as a user preference in error",
    )
    assert previous["decision_source"] == "user"
    assert previous["locked"] is True
    decision = corrected.decisions[0]
    assert decision.decision_source is DecisionSource.AI
    assert decision.decision_state is DecisionState.PROVISIONAL
    assert decision.locked is False
    assert registry_identity(corrected) != registry_identity(registry)


def test_correction_requires_a_reason_and_a_real_change() -> None:
    registry = provisional_registry()
    with pytest.raises(TerminologyError, match="why the stored record was wrong"):
        correct_decision(
            registry,
            source_term="Aunt Veyra",
            target_term="خاله ویرا",
            decision_source=DecisionSource.AI,
            decision_state=DecisionState.PROVISIONAL,
            locked=False,
            reason="   ",
        )
    with pytest.raises(TerminologyError, match="at least one recorded decision fact"):
        correct_decision(
            registry,
            source_term="Aunt Veyra",
            target_term="خاله ویرا",
            decision_source=DecisionSource.AI,
            decision_state=DecisionState.PROVISIONAL,
            locked=False,
            reason="no actual change",
        )


def test_correction_publishes_durable_superseded_evidence(tmp_path: Path) -> None:
    project = _project(tmp_path, provisional_registry())
    registry = set_decision(
        provisional_registry(),
        source_term="Aunt Veyra",
        target_term="خاله ویرا",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    save_registry(project / "config/terminology.yaml", registry)
    plan = plan_decision_correction(
        project,
        source_term="Aunt Veyra",
        target_term="خاله ویرا",
        decision_source=DecisionSource.AI,
        decision_state=DecisionState.PROVISIONAL,
        locked=False,
        reason="AI choice mistakenly stored as a locked user preference",
    )
    record = execute_decision_correction(plan, now=lambda: NOW)
    stored = load_corrections(project)
    assert len(stored) == 1
    assert stored[0].correction_id == record.correction_id
    assert stored[0].previous_decision_source == "user"
    assert stored[0].previous_locked is True
    assert stored[0].corrected_decision_state == "provisional"
    assert stored[0].registry_sha256_before != stored[0].registry_sha256_after


# --- registry lineage -----------------------------------------------------------------


def _correction(
    identifier: str, *, before: str, after: str, moment: datetime
) -> TerminologyDecisionCorrection:
    return TerminologyDecisionCorrection(
        schema_version="1.0",
        correction_id=identifier,
        candidate_id="tc-" + "1" * 24,
        decision_id="td-" + "2" * 24,
        source_term="Aunt Veyra",
        previous_target_term="خاله ویرا",
        previous_decision_source="user",
        previous_decision_state="confirmed",
        previous_locked=True,
        corrected_target_term="خاله ویرا",
        corrected_decision_source="ai",
        corrected_decision_state="provisional",
        corrected_locked=False,
        reason="synthetic lineage step",
        registry_sha256_before=before,
        registry_sha256_after=after,
        created_at=moment,
    )


def _applied_plan(
    identifier: str, *, before: str, after: str | None, moment: datetime
) -> TerminologyRemediationPlan:
    return TerminologyRemediationPlan(
        schema_version="1.0",
        plan_id=identifier,
        engine_version="1.1.0",
        audit_id="remediation-audit-" + "3" * 24,
        audit_schema_version="1.0",
        registry_file="config/terminology.yaml",
        registry_version_before="1.0.0",
        registry_version_after="1.0.1",
        source_registry_semantic_sha256=before,
        source_registry_raw_sha256="4" * 64,
        glossary_sha256="5" * 64,
        decisions_before=2,
        candidates_before=2,
        operation_count=0,
        deterministic_operation_count=0,
        unresolved_operation_count=0,
        mutating_operation_count=0,
        expected_semantic_sha256=after,
        applicable=True,
        created_at=moment,
    )


def test_a_registry_with_no_governed_event_explains_no_drift() -> None:
    lineage = explain_registry_lineage((), current_semantic_sha256="a" * 64)

    assert lineage.considered_event_ids == ()
    assert lineage.head_event_id is None
    assert lineage.head_event_kind is None
    assert lineage.explained is False


def test_the_live_registry_must_be_the_head_of_the_governed_chain() -> None:
    correction = _correction(
        "tdc-" + "a" * 24, before="a" * 64, after="b" * 64, moment=datetime(2026, 3, 4, tzinfo=UTC)
    )
    transitions = registry_transitions(corrections=(correction,))

    matching = explain_registry_lineage(transitions, current_semantic_sha256="b" * 64)
    drifted = explain_registry_lineage(transitions, current_semantic_sha256="c" * 64)

    assert matching.explained is True
    assert matching.head_event_id == "tdc-" + "a" * 24
    assert matching.head_event_kind is RegistryEventKind.DECISION_CORRECTION
    # An ungoverned edit after the last governed event leaves the live registry unexplained.
    assert drifted.explained is False
    assert drifted.head_semantic_sha256 == "b" * 64


def test_an_applied_remediation_continues_the_chain_a_correction_started() -> None:
    """The two governed writers are one chain, which is what a remediated registry needs."""

    correction = _correction(
        "tdc-" + "a" * 24, before="a" * 64, after="b" * 64, moment=datetime(2026, 3, 4, tzinfo=UTC)
    )
    plan = _applied_plan(
        "trp-" + "b" * 24,
        before="b" * 64,
        after="c" * 64,
        moment=datetime(2026, 3, 5, tzinfo=UTC),
    )

    lineage = explain_registry_lineage(
        registry_transitions(corrections=(correction,), remediations=(plan,)),
        current_semantic_sha256="c" * 64,
    )

    assert lineage.considered_event_ids == ("tdc-" + "a" * 24, "trp-" + "b" * 24)
    assert lineage.head_event_id == "trp-" + "b" * 24
    assert lineage.head_event_kind is RegistryEventKind.REMEDIATION_APPLICATION
    assert lineage.explained is True and lineage.contiguous is True
    # Considering only corrections is exactly the state that used to look ungoverned.
    corrections_only = explain_registry_lineage(
        registry_transitions(corrections=(correction,)), current_semantic_sha256="c" * 64
    )
    assert corrections_only.explained is False


def test_a_plan_that_projects_no_resulting_identity_is_not_lineage() -> None:
    plan = _applied_plan(
        "trp-" + "b" * 24,
        before="b" * 64,
        after=None,
        moment=datetime(2026, 3, 5, tzinfo=UTC),
    )

    with pytest.raises(RegistryLineageError):
        registry_transitions(remediations=(plan,))


def test_events_older_than_the_reference_record_stay_outside_the_window() -> None:
    old = _correction(
        "tdc-" + "a" * 24, before="a" * 64, after="b" * 64, moment=datetime(2026, 3, 1, tzinfo=UTC)
    )
    new = _correction(
        "tdc-" + "b" * 24, before="d" * 64, after="e" * 64, moment=datetime(2026, 3, 6, tzinfo=UTC)
    )

    lineage = explain_registry_lineage(
        registry_transitions(corrections=(old, new)),
        current_semantic_sha256="e" * 64,
        since=datetime(2026, 3, 4, tzinfo=UTC),
    )

    assert lineage.considered_event_ids == ("tdc-" + "b" * 24,)
    assert lineage.explained is True
    # The window opens on a hash the excluded correction never produced, which is exactly what a
    # migration between the two looks like; a gap is reported, not treated as a failure.
    assert lineage.contiguous is True


def test_a_gap_between_two_events_is_reported_without_denying_the_head() -> None:
    first = _correction(
        "tdc-" + "a" * 24, before="a" * 64, after="b" * 64, moment=datetime(2026, 3, 5, tzinfo=UTC)
    )
    second = _correction(
        "tdc-" + "b" * 24, before="d" * 64, after="e" * 64, moment=datetime(2026, 3, 6, tzinfo=UTC)
    )
    contiguous = _correction(
        "tdc-" + "c" * 24, before="b" * 64, after="e" * 64, moment=datetime(2026, 3, 6, tzinfo=UTC)
    )

    gapped = explain_registry_lineage(
        registry_transitions(corrections=(first, second)), current_semantic_sha256="e" * 64
    )
    joined = explain_registry_lineage(
        registry_transitions(corrections=(first, contiguous)), current_semantic_sha256="e" * 64
    )

    assert gapped.explained is True
    assert gapped.contiguous is False
    assert joined.contiguous is True


# --- rejection ------------------------------------------------------------------------


def test_rejection_is_an_explicit_state_not_an_absence() -> None:
    registry = reject_candidate(
        with_candidate(base_registry(), "platform", classification=TerminologyType.STORY_TERM),
        source_term="platform",
        reason="ordinary common noun",
    )
    assert registry.candidates[0].status is CandidateStatus.REJECTED
    assert "ordinary common noun" in registry.candidates[0].evidence_notes


def test_rejecting_a_decided_candidate_is_refused() -> None:
    registry = provisional_registry()
    with pytest.raises(TerminologyError, match="canonical decision cannot be rejected"):
        reject_candidate(registry, source_term="Aunt Veyra", reason="changed my mind")


# --- prompt rendering -----------------------------------------------------------------


def test_prompt_marks_provisional_state_and_the_open_question() -> None:
    registry = provisional_registry()
    rendered = render_terminology(
        registry, "Aunt Veyra spoke.", "", questions_with(registry, blocking=False)
    )
    assert "state: provisional" in rendered
    assert "unresolved:" in rendered
    assert "maternal or paternal" in rendered
    assert "use the provisional canonical form consistently" in rendered


def test_prompt_marks_confirmed_state_without_a_question_note() -> None:
    registry = set_decision(
        with_candidate(base_registry(), "Veyra"),
        source_term="Veyra",
        target_term="ویرا",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    rendered = render_terminology(registry, "Veyra spoke.", "", empty_questions("synthetic-book"))
    assert "state: confirmed" in rendered
    assert "unresolved:" not in rendered


# --- QA -------------------------------------------------------------------------------


def test_qa_enforces_a_provisional_decision_and_records_its_state() -> None:
    registry = provisional_registry()
    report = validate_terminology("Aunt Veyra spoke.", "خاله ویرا صحبت کرد.", registry)
    codes = {finding.code for finding in report.findings}
    assert "provisional_decision_enforced" in codes
    assert report.valid is True


def test_qa_still_fails_when_a_provisional_target_is_absent() -> None:
    registry = provisional_registry()
    report = validate_terminology("Aunt Veyra spoke.", "او صحبت کرد.", registry)
    codes = {finding.code for finding in report.findings}
    assert "canonical_term_missing" in codes
    assert "provisional_decision_enforced" in codes
    assert report.valid is False


# --- impact analysis ------------------------------------------------------------------


def _project(tmp_path: Path, registry: TerminologyRegistry) -> Path:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config/book.yaml").write_text(
        """book_id: synthetic-book
title_en: Synthetic Book
title_fa: کتاب ساختگی
author: Author
source_language: en
target_language: fa
pdf_path: input/book.pdf
chunk_target_words: 10
chunk_max_words: 20
previous_context_words: 2
output_format: epub3
terminology: config/terminology.yaml
terminology_questions: config/terminology-questions.yaml
""",
        encoding="utf-8",
    )
    save_registry(tmp_path / "config/terminology.yaml", registry)
    return tmp_path


def test_impact_analysis_finds_chunks_without_touching_them(tmp_path: Path) -> None:
    project = _project(tmp_path, provisional_registry())
    chunk = project / "workspace/chapters/001/chunks/002"
    chunk.mkdir(parents=True)
    (chunk / "source.md").write_text("Aunt Veyra spoke twice. Aunt Veyra left.\n", encoding="utf-8")
    other = project / "workspace/chapters/002/chunks/001"
    other.mkdir(parents=True)
    (other / "source.md").write_text("Nothing relevant here.\n", encoding="utf-8")
    before = (chunk / "source.md").read_bytes()

    report = analyze_impact(
        project, source_term="Aunt Veyra", proposed_target="عمه ویرا", now=lambda: NOW
    )
    assert [item.chunk_id for item in report.impacted_chunks] == ["001-002"]
    assert report.impacted_chunks[0].occurrence_count == 2
    assert report.current_target_term == "خاله ویرا"
    assert report.current_decision_state == "provisional"
    assert report.proposed_target_term == "عمه ویرا"
    assert (chunk / "source.md").read_bytes() == before


def test_impact_analysis_reports_zero_when_the_term_never_occurs(tmp_path: Path) -> None:
    project = _project(tmp_path, provisional_registry())
    chunk = project / "workspace/chapters/001/chunks/001"
    chunk.mkdir(parents=True)
    (chunk / "source.md").write_text("Unrelated synthetic text.\n", encoding="utf-8")
    report = analyze_impact(project, source_term="Aunt Veyra", now=lambda: NOW)
    assert report.impacted_chunks == []
    assert report.accepted_requiring_reevaluation == 0


def test_impact_analysis_rejects_an_unknown_term(tmp_path: Path) -> None:
    project = _project(tmp_path, provisional_registry())
    with pytest.raises(TerminologyError, match="no terminology candidate"):
        analyze_impact(project, source_term="Nonexistent", now=lambda: NOW)


# --- workflow round trip --------------------------------------------------------------


def test_publish_and_reload_round_trip(tmp_path: Path) -> None:
    project = _project(tmp_path, provisional_registry())
    questions = questions_with(provisional_registry())
    publish_questions(project, questions)
    reloaded = load_or_empty_questions(project)
    assert questions_identity(reloaded) == questions_identity(questions)
    metadata = yaml.safe_load(
        (project / "config/terminology-questions.metadata.yaml").read_text(encoding="utf-8")
    )
    assert metadata["resource_kind"] == "open_questions"
    report = readiness(project)
    assert report.ready is True


def test_missing_question_registry_reads_as_empty(tmp_path: Path) -> None:
    project = _project(tmp_path, provisional_registry())
    assert load_or_empty_questions(project).questions == []


# --- escalation accounting ------------------------------------------------------------


def _escalation_inputs(
    candidate_status: CandidateStatus = CandidateStatus.CANDIDATE,
) -> tuple[TerminologyRegistry, OpenQuestionRegistry]:
    registry = with_candidate(
        base_registry(), "platform", classification=TerminologyType.STORY_TERM
    )
    if candidate_status is CandidateStatus.REJECTED:
        registry = reject_candidate(registry, source_term="platform", reason="ordinary common noun")
    return registry, empty_questions("synthetic-book")


def test_escalation_basis_is_derived_from_current_registry_state() -> None:
    from book_translator.terminology.remediation import _escalation_basis

    registry, questions = _escalation_inputs()
    candidate = registry.candidates[0]
    basis, unresolved = _escalation_basis(candidate, None, None)
    assert (basis, unresolved) == ("undecided", True)

    rejected, _ = _escalation_inputs(CandidateStatus.REJECTED)
    basis, unresolved = _escalation_basis(rejected.candidates[0], None, None)
    assert (basis, unresolved) == ("explicit_rejection", False)

    confirmed = set_decision(
        registry,
        source_term="platform",
        target_term="سکو",
        classification=TerminologyType.STORY_TERM,
        decision_source=DecisionSource.AI,
        locked=False,
    )
    basis, unresolved = _escalation_basis(confirmed.candidates[0], confirmed.decisions[0], None)
    assert (basis, unresolved) == ("confirmed_decision", False)


def test_provisional_escalation_is_closed_unless_its_question_blocks() -> None:
    from book_translator.terminology.remediation import _escalation_basis

    registry = provisional_registry()
    questions = questions_with(registry, blocking=False)
    basis, unresolved = _escalation_basis(
        registry.candidates[0], registry.decisions[0], questions.questions[0]
    )
    assert (basis, unresolved) == ("provisional_decision", False)

    blocking = questions_with(registry, blocking=True)
    basis, unresolved = _escalation_basis(
        registry.candidates[0], registry.decisions[0], blocking.questions[0]
    )
    assert (basis, unresolved) == ("provisional_decision", True)


def test_no_hardcoded_term_table_drives_current_readiness() -> None:
    """Escalation accounting must not name individual source terms in code."""
    source = Path("src/book_translator/terminology/remediation.py").read_text(encoding="utf-8")
    for term in ("Aunt Resara", "staera", '"Peace"', '"platform"', '"wards"', '"Sar"'):
        assert term not in source, f"remediation.py still special-cases {term}"


def test_term_kind_and_interpretations_derive_from_classification() -> None:
    from book_translator.terminology.remediation import _competing_interpretations, _term_kind

    person = with_candidate(base_registry(), "Sarel").candidates[0]
    invented = with_candidate(
        base_registry(), "veyrala", classification=TerminologyType.INVENTED
    ).candidates[0]
    ordinary = with_candidate(
        base_registry(), "platform", classification=TerminologyType.RECURRING_PHRASE
    ).candidates[0]
    assert _term_kind(person) == "proper noun"
    assert _term_kind(invented) == "fictional/world-specific term"
    assert _term_kind(ordinary) == "ordinary vocabulary"
    assert _competing_interpretations(person, "proper noun") == [
        "proper noun",
        "ordinary vocabulary",
    ]


# --- locked user decisions stay intact ------------------------------------------------


def test_a_locked_user_decision_is_unaffected_by_provisional_machinery() -> None:
    registry = set_decision(
        with_candidate(base_registry(), "Ajahn"),
        source_term="Ajahn",
        target_term="آجای",
        classification=TerminologyType.ORGANIZATION,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    decision = registry.decisions[0]
    assert decision.decision_source is DecisionSource.USER
    assert decision.decision_state is DecisionState.CONFIRMED
    assert decision.locked is True
    report = build_readiness(registry, empty_questions("synthetic-book"))
    assert report.confirmed_decisions == 1 and report.ready is True


# --- CLI ------------------------------------------------------------------------------


def test_cli_round_trip_for_questions_readiness_and_correction(tmp_path: Path) -> None:
    project = _project(tmp_path, provisional_registry())
    import os

    cwd = os.getcwd()
    os.chdir(project)
    try:
        opened = runner.invoke(
            app,
            [
                "open-terminology-question",
                "Aunt Veyra",
                "--category",
                "kinship_side",
                "--question",
                "Is Veyra a maternal or paternal aunt?",
                "--non-blocking",
                "--current-book",
                "checked_inconclusive",
            ],
        )
        assert opened.exit_code == 0, opened.output
        assert "Open question recorded" in opened.output

        listed = runner.invoke(app, ["list-terminology-questions", "--status", "open"])
        assert listed.exit_code == 0, listed.output
        assert "kinship_side" in listed.output

        ready = runner.invoke(app, ["report-terminology-readiness"])
        assert ready.exit_code == 0, ready.output
        assert "Translation ready: yes" in ready.output

        impact = runner.invoke(app, ["analyze-terminology-impact", "Aunt Veyra"])
        assert impact.exit_code == 0, impact.output
    finally:
        os.chdir(cwd)


def test_cli_refuses_to_lock_a_provisional_decision(tmp_path: Path) -> None:
    project = _project(tmp_path, with_candidate(base_registry(), "Veyra"))
    import os

    cwd = os.getcwd()
    os.chdir(project)
    try:
        result = runner.invoke(
            app,
            [
                "set-terminology",
                "Veyra",
                "ویرا",
                "--type",
                "person",
                "--state",
                "provisional",
                "--locked",
            ],
        )
        assert result.exit_code == 1
        assert "provisional" in result.output
    finally:
        os.chdir(cwd)
