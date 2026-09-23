from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import (
    AutoRepairVerdict,
    DecisionSource,
    ProviderErrorCategory,
    ProviderName,
    RepairFindingClass,
    SnapshotResourceKind,
    TerminologyEnforcement,
    TerminologyFinding,
    TerminologyRegistry,
    TerminologyType,
    TerminologyValidationReport,
    TranslationAcceptanceStatus,
    TranslationAutoRepairDecision,
    TranslationExecutionKind,
    TranslationJobStatus,
    TranslationQAFinding,
    TranslationQAReport,
    TranslationRepairInitiator,
)
from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
from book_translator.provider_execution import (
    ProviderExecutionError,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
    list_attempts,
)
from book_translator.provider_execution.errors import ErrorClassification
from book_translator.terminology import save_registry, set_decision
from book_translator.terminology.validation import (
    STRUCTURAL_QA_VERSION,
    TERMINOLOGY_QA_VERSION,
)
from book_translator.translation_auto_repair import (
    AutoRepairError,
    _publish_decision,
    auto_repair_job,
    plan_auto_repair,
)
from book_translator.translation_queue import create_run, load_run_summary, plan_run
from book_translator.translation_repair import repair_translation_job
from book_translator.translation_repair_policy import (
    AUTO_REPAIR_POLICY_VERSION,
    AutoRepairAssessment,
    RepairBudget,
    assess_auto_repair,
    classify_structural_finding,
    classify_terminology_finding,
)
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import WorkerResult, run_translation_worker
from tests.test_translation_queue import NOW, SNAPSHOT_NOW
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import (
    SyntheticProvider,
    _options,
    _prepared_run,
    _queue_bytes,
    _run_path,
)

runner = CliRunner()

ONE_PARAGRAPH = "فقط یک بند.\n"
TWO_PARAGRAPHS = "بند نخست.\n\nبند دوم.\n"
CANONICAL_TWO_PARAGRAPHS = "آلفا در بند نخست.\n\nبند دوم.\n"


class ScriptedProvider:
    """A provider whose answer changes per call, so a repair round can be scripted."""

    provider_name = "fake"
    adapter_version = "test-1.0.0"

    def __init__(self, texts: tuple[str, ...], calls: list[str] | None = None) -> None:
        self.texts = texts
        self.calls = calls if calls is not None else []

    def execute(self, request: ProviderRequest) -> ProviderResult:
        index = min(len(self.calls), len(self.texts) - 1)
        self.calls.append(request.job_id)
        return ProviderResult(
            response_id="synthetic-response",
            model=request.model or "fake-v1",
            status="completed",
            text_outputs=(self.texts[index],),
            usage=ProviderUsage(10, 10, 20),
            sanitized_response={"status": "completed"},
        )


class FailingAfterFirstProvider:
    """Succeed once with a rejected translation, then fail or go ambiguous on every repair."""

    provider_name = "fake"
    adapter_version = "test-1.0.0"

    def __init__(self, *, ambiguous: bool, calls: list[str] | None = None) -> None:
        self.ambiguous = ambiguous
        self.calls = calls if calls is not None else []

    def execute(self, request: ProviderRequest) -> ProviderResult:
        self.calls.append(request.job_id)
        if len(self.calls) == 1:
            return ProviderResult(
                response_id="synthetic-response",
                model=request.model or "fake-v1",
                status="completed",
                text_outputs=(ONE_PARAGRAPH,),
                usage=ProviderUsage(10, 10, 20),
                sanitized_response={"status": "completed"},
            )
        raise ProviderExecutionError(
            ErrorClassification(
                ProviderErrorCategory.UNKNOWN
                if self.ambiguous
                else ProviderErrorCategory.RATE_LIMIT,
                False,
                self.ambiguous,
                False,
                "synthetic repair failure",
            )
        )


def _competing_variant_run(root: Path) -> str:
    """A run whose canonical decision has a known rival rendering QA can detect."""
    synthetic_project(root)
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )
    registry = set_decision(
        registry,
        source_term="Alpha",
        target_term="آلفا",
        classification=TerminologyType.STORY_TERM,
        decision_source=DecisionSource.USER,
        locked=True,
        competing_variants=("الفا",),
    )
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
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    created = create_run(plan_run(snapshot.snapshot_id, project_root=root, now=lambda: NOW))
    execute_prompt_packages(
        plan_prompt_packages(created.run_id, project_root=root, now=lambda: NOW)
    )
    return created.run_id


def _repaired_worker(
    root: Path,
    run_id: str,
    texts: tuple[str, ...],
    *,
    calls: list[str] | None = None,
    max_repair_attempts: int = 2,
    include_qa_rejected: bool = False,
    max_jobs: int = 1,
    offset: int = 10,
) -> WorkerResult:
    provider = ScriptedProvider(texts, calls)
    return run_translation_worker(
        run_id,
        project_root=root,
        options=_options(
            auto_repair=True,
            include_qa_rejected=include_qa_rejected,
            max_repair_attempts=max_repair_attempts,
            max_jobs=max_jobs,
        ),
        provider_factory=lambda _job: provider,
        now=lambda: NOW + timedelta(seconds=offset),
    )


def _decision(
    root: Path, run_id: str, chunk_id: str, attempt: int
) -> TranslationAutoRepairDecision:
    path = _run_path(root, run_id) / "jobs" / chunk_id / "auto-repair" / f"{attempt:03d}"
    return TranslationAutoRepairDecision.model_validate(
        yaml.safe_load((path / "decision.yaml").read_text(encoding="utf-8"))
    )


def _report(*codes: str) -> TranslationQAReport:
    return TranslationQAReport(
        schema_version="1.0",
        valid=not codes,
        source_sha256="0" * 64,
        translation_sha256="1" * 64,
        source_paragraph_count=2,
        translation_paragraph_count=1 if codes else 2,
        findings=[
            TranslationQAFinding(code=code, severity="error", message=code) for code in codes
        ],
    )


def _terminology(*findings: TerminologyFinding) -> TerminologyValidationReport:
    return TerminologyValidationReport(
        schema_version="1.0",
        valid=not any(item.severity == "error" for item in findings),
        source_sha256="0" * 64,
        translation_sha256="1" * 64,
        checked_decisions=1,
        findings=list(findings),
    )


def _assess(
    terminology: TerminologyValidationReport | None = None,
    structural: TranslationQAReport | None = None,
    *,
    budget: RepairBudget | None = None,
    recorded: tuple[str | None, str | None] = (None, None),
) -> AutoRepairAssessment:
    return assess_auto_repair(
        terminology_report=terminology or _terminology(),
        structural_report=structural or _report("paragraph_count_mismatch"),
        budget=budget or RepairBudget(2, 0, 3, 1),
        recorded_terminology_qa_version=recorded[0],
        recorded_structural_qa_version=recorded[1],
        installed_terminology_qa_version=TERMINOLOGY_QA_VERSION,
        installed_structural_qa_version=STRUCTURAL_QA_VERSION,
    )


def test_policy_classifies_each_finding_class_and_fails_closed_on_unknown_codes() -> None:
    assert (
        classify_structural_finding("paragraph_count_mismatch")
        is RepairFindingClass.AUTO_REPAIRABLE
    )
    assert classify_structural_finding("known_source_anomaly") is RepairFindingClass.NON_REPAIRABLE
    assert classify_structural_finding("qa_code_from_the_future") is (
        RepairFindingClass.OPERATOR_REQUIRED
    )
    assert (
        classify_terminology_finding("canonical_term_missing", TerminologyEnforcement.EXACT)
        is RepairFindingClass.AUTO_REPAIRABLE
    )
    assert (
        classify_terminology_finding("canonical_term_missing", TerminologyEnforcement.CONTEXTUAL)
        is RepairFindingClass.OPERATOR_REQUIRED
    )
    assert classify_terminology_finding("canonical_term_missing", None) is (
        RepairFindingClass.OPERATOR_REQUIRED
    )
    assert (
        classify_terminology_finding("competing_variant", TerminologyEnforcement.EXACT)
        is RepairFindingClass.OPERATOR_REQUIRED
    )
    assert (
        classify_terminology_finding("canonical_term_unverified", TerminologyEnforcement.CONTEXTUAL)
        is RepairFindingClass.REEVALUATION_CANDIDATE
    )


def test_policy_verdicts_cover_repair_refusal_budget_and_reevaluation() -> None:
    assert _assess().verdict is AutoRepairVerdict.REPAIRABLE
    assert _assess(structural=_report()).verdict is AutoRepairVerdict.NOT_REJECTED

    unsafe = _terminology(
        TerminologyFinding(
            code="competing_variant",
            severity="error",
            source_term="Alpha",
            enforcement=TerminologyEnforcement.EXACT,
            message="rival rendering",
        )
    )
    refused = _assess(unsafe)
    assert refused.verdict is AutoRepairVerdict.REFUSED
    assert refused.blocking_reasons == ("terminology:competing_variant:operator_required",)
    assert set(refused.triggering_codes) == {"competing_variant", "paragraph_count_mismatch"}

    exhausted = _assess(budget=RepairBudget(2, 2, 3, 2))
    assert exhausted.verdict is AutoRepairVerdict.BUDGET_EXHAUSTED
    assert exhausted.blocking_reasons == ("repair_attempts_exhausted",)
    provider_bound = _assess(budget=RepairBudget(2, 0, 3, 3))
    assert provider_bound.verdict is AutoRepairVerdict.BUDGET_EXHAUSTED
    assert provider_bound.blocking_reasons == ("attempts_exhausted",)

    moved = _assess(recorded=("0.9.0", STRUCTURAL_QA_VERSION))
    assert moved.verdict is AutoRepairVerdict.REEVALUATION_REQUIRED
    assert moved.blocking_reasons == ("terminology_qa_version_moved",)
    assert _assess(recorded=(TERMINOLOGY_QA_VERSION, STRUCTURAL_QA_VERSION)).verdict is (
        AutoRepairVerdict.REPAIRABLE
    )


def test_policy_refuses_a_mixture_containing_one_unsafe_finding() -> None:
    mixed = _assess(
        _terminology(
            TerminologyFinding(
                code="canonical_term_missing",
                severity="error",
                source_term="Alpha",
                enforcement=TerminologyEnforcement.EXACT,
                message="missing",
            ),
            TerminologyFinding(
                code="competing_variant",
                severity="error",
                source_term="Alpha",
                enforcement=TerminologyEnforcement.EXACT,
                message="rival",
            ),
        ),
        _report(),
    )

    assert mixed.verdict is AutoRepairVerdict.REFUSED
    assert mixed.blocking_reasons == ("terminology:competing_variant:operator_required",)


def test_accepted_first_attempt_never_starts_a_repair(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    calls: list[str] = []
    result = _repaired_worker(tmp_path, run_id, ("ترجمه نخست.\n\nترجمه دوم.\n",), calls=calls)
    job = load_run_summary(run_id, project_root=tmp_path).jobs[0]

    assert result.accepted == 1 and result.repair_attempts == 0 and result.auto_repaired == 0
    assert len(calls) == 1
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert not list(_run_path(tmp_path, run_id).glob("jobs/*/auto-repair/*"))


def test_paragraph_count_mismatch_is_repaired_automatically_to_acceptance(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    calls: list[str] = []
    result = _repaired_worker(tmp_path, run_id, (ONE_PARAGRAPH, TWO_PARAGRAPHS), calls=calls)
    state = load_run_summary(run_id, project_root=tmp_path)
    job = state.jobs[0]
    decision = _decision(tmp_path, run_id, job.chunk_id, 2)

    assert len(calls) == 2
    assert result.processed == 1 and result.accepted == 1
    assert result.qa_rejected == 0 and result.auto_repaired == 1 and result.repair_attempts == 1
    assert result.jobs[0].auto_repair_verdict == AutoRepairVerdict.REPAIRABLE.value
    assert result.jobs[0].auto_repair_findings == ("paragraph_count_mismatch",)
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.accepted_attempt == 2
    assert decision.auto_repair_policy_version == AUTO_REPAIR_POLICY_VERSION
    assert decision.initiator is TranslationRepairInitiator.AUTOMATIC
    assert decision.triggering_codes == ["paragraph_count_mismatch"]
    assert decision.repaired_from_attempt == 1 and decision.repair_attempt_number == 2
    assert decision.remaining_repair_attempts == 2


def test_a_repair_that_still_fails_qa_uses_the_remaining_budget(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    calls: list[str] = []
    result = _repaired_worker(
        tmp_path, run_id, (ONE_PARAGRAPH, ONE_PARAGRAPH, TWO_PARAGRAPHS), calls=calls
    )
    job = load_run_summary(run_id, project_root=tmp_path).jobs[0]
    attempts = list_attempts(run_id, job.job_id, project_root=tmp_path)

    assert len(calls) == 3
    assert result.accepted == 1 and result.repair_attempts == 2 and result.auto_repaired == 1
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.accepted_attempt == 3
    assert [item.attempt.execution_kind for item in attempts] == [
        TranslationExecutionKind.TRANSLATION,
        TranslationExecutionKind.REPAIR,
        TranslationExecutionKind.REPAIR,
    ]
    assert _decision(tmp_path, run_id, job.chunk_id, 3).repair_attempts_used == 1


def test_exhausted_repair_budget_leaves_the_job_qa_rejected(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    calls: list[str] = []
    result = _repaired_worker(
        tmp_path, run_id, (ONE_PARAGRAPH,), calls=calls, max_repair_attempts=1
    )
    job = load_run_summary(run_id, project_root=tmp_path).jobs[0]

    assert len(calls) == 2
    assert result.qa_rejected == 1 and result.accepted == 0 and result.repair_attempts == 1
    assert result.jobs[0].auto_repair_verdict == AutoRepairVerdict.BUDGET_EXHAUSTED.value
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert job.provider_attempt_count == 2


def test_an_unsafe_terminology_finding_is_never_repaired_automatically(tmp_path: Path) -> None:
    run_id = _competing_variant_run(tmp_path)
    calls: list[str] = []
    result = _repaired_worker(
        tmp_path, run_id, ("الفا در بند نخست.\n\nآلفا در بند دوم.\n",), calls=calls
    )
    job = load_run_summary(run_id, project_root=tmp_path).jobs[0]

    assert len(calls) == 1
    assert result.qa_rejected == 1 and result.repair_attempts == 0
    assert result.jobs[0].auto_repair_verdict == AutoRepairVerdict.REFUSED.value
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert job.provider_attempt_count == 1
    assert not list(_run_path(tmp_path, run_id).glob("jobs/*/auto-repair/*"))


def test_a_mixture_with_one_unsafe_finding_spends_no_provider_attempt(tmp_path: Path) -> None:
    run_id = _competing_variant_run(tmp_path)
    calls: list[str] = []
    result = _repaired_worker(tmp_path, run_id, ("الفا در یک بند.\n",), calls=calls)
    plan = plan_auto_repair(
        run_id,
        load_run_summary(run_id, project_root=tmp_path).jobs[0].job_id,
        project_root=tmp_path,
    )

    assert len(calls) == 1 and result.repair_attempts == 0
    assert plan.assessment.verdict is AutoRepairVerdict.REFUSED
    assert "paragraph_count_mismatch" in plan.assessment.triggering_codes
    assert "competing_variant" in plan.assessment.triggering_codes
    assert plan.assessment.blocking_reasons == ("terminology:competing_variant:operator_required",)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_a_failing_repair_provider_stops_the_cycle_without_acceptance(
    tmp_path: Path, ambiguous: bool
) -> None:
    run_id = _prepared_run(tmp_path)
    calls: list[str] = []
    provider = FailingAfterFirstProvider(ambiguous=ambiguous, calls=calls)
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_repair=True),
        provider_factory=lambda _job: provider,
        now=lambda: NOW + timedelta(seconds=10),
    )
    job = load_run_summary(run_id, project_root=tmp_path).jobs[0]

    assert len(calls) == 2
    assert result.accepted == 0
    assert result.permanent_failure == (1 if ambiguous else 0)
    assert result.provider_failed == (0 if ambiguous else 1)
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert job.accepted_attempt == 1
    assert job.status is (
        TranslationJobStatus.BLOCKED if ambiguous else TranslationJobStatus.FAILED
    )


def test_repair_provenance_links_attempts_and_distinguishes_the_initiator(tmp_path: Path) -> None:
    automatic_run = _prepared_run(tmp_path / "automatic")
    _repaired_worker(tmp_path / "automatic", automatic_run, (ONE_PARAGRAPH, TWO_PARAGRAPHS))
    automatic = load_run_summary(automatic_run, project_root=tmp_path / "automatic").jobs[0]
    automatic_attempts = list_attempts(
        automatic_run, automatic.job_id, project_root=tmp_path / "automatic"
    )
    decision = _decision(tmp_path / "automatic", automatic_run, automatic.chunk_id, 2)

    manual_run = _prepared_run(tmp_path / "manual")
    run_translation_worker(
        manual_run,
        project_root=tmp_path / "manual",
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider(ONE_PARAGRAPH),
        now=lambda: NOW + timedelta(seconds=10),
    )
    manual = load_run_summary(manual_run, project_root=tmp_path / "manual").jobs[0]
    manual_result = repair_translation_job(
        manual_run,
        manual.job_id,
        project_root=tmp_path / "manual",
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="operator",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        provider_adapter=SyntheticProvider(TWO_PARAGRAPHS),
        now=lambda: NOW + timedelta(seconds=20),
    )

    assert automatic_attempts[1].attempt.execution_kind is TranslationExecutionKind.REPAIR
    assert automatic_attempts[1].attempt.repaired_from_attempt == 1
    assert decision.attempt_file.as_posix().endswith("attempts/001/attempt.yaml")
    assert decision.rejection_evidence_file.as_posix().endswith("acceptance/001/evidence.yaml")
    assert decision.initiator is TranslationRepairInitiator.AUTOMATIC
    assert manual_result.initiator is TranslationRepairInitiator.OPERATOR
    assert manual_result.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    # An operator repair is a decision a person already made; the policy record exists only where
    # a machine made one, which is exactly what distinguishes the two afterwards.
    assert not list(_run_path(tmp_path / "manual", manual_run).glob("jobs/*/auto-repair/*"))


def test_run_counters_count_a_repaired_job_once(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    provider = ScriptedProvider((ONE_PARAGRAPH, TWO_PARAGRAPHS, "ترجمه نخست.\n\nترجمه دوم.\n"))
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_repair=True, max_jobs=2),
        provider_factory=lambda _job: provider,
        now=lambda: NOW + timedelta(seconds=10),
    )
    state = load_run_summary(run_id, project_root=tmp_path)
    repaired = state.jobs[0]

    assert result.processed == 2 and result.accepted == 2 and result.qa_rejected == 0
    assert result.repair_attempts == 1 and result.auto_repaired == 1
    assert state.run.accepted_count == 2 and state.run.qa_rejected_count == 0
    assert state.run.succeeded_count == 2
    assert repaired.provider_attempt_count == 2 and repaired.attempt_count == 2
    assert sum(job.provider_attempt_count for job in state.jobs) == 3


def _reject_first_job(root: Path, run_id: str) -> str:
    run_translation_worker(
        run_id,
        project_root=root,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider(ONE_PARAGRAPH),
        now=lambda: NOW + timedelta(seconds=10),
    )
    job = load_run_summary(run_id, project_root=root).jobs[0]
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    return job.job_id


def test_an_existing_qa_rejected_job_is_resumed_only_when_explicitly_included(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path)
    job_id = _reject_first_job(tmp_path, run_id)
    calls: list[str] = []

    untouched = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_repair=True),
        provider_factory=lambda _job: ScriptedProvider((TWO_PARAGRAPHS,), calls),
        now=lambda: NOW + timedelta(seconds=20),
    )
    assert untouched.processed == 0 and not calls

    resumed = _repaired_worker(
        tmp_path, run_id, (TWO_PARAGRAPHS,), calls=calls, include_qa_rejected=True, offset=30
    )
    job = load_run_summary(run_id, project_root=tmp_path).jobs[0]

    assert resumed.processed == 1 and resumed.accepted == 1 and resumed.auto_repaired == 1
    assert calls == [job_id]
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.accepted_attempt == 2


def test_included_repairs_never_displace_or_reorder_pending_translation(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=3)
    rejected = f"{run_id}__001-001"
    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(job_id=rejected),
        provider_factory=lambda _job: SyntheticProvider(ONE_PARAGRAPH),
        now=lambda: NOW + timedelta(seconds=10),
    )
    calls: list[str] = []
    provider = ScriptedProvider(("ترجمه نخست.\n\nترجمه دوم.\n",), calls)
    planned = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_repair=True, include_qa_rejected=True, max_jobs=5, dry_run=True),
        provider_factory=lambda _job: provider,
    )
    processed = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_repair=True, include_qa_rejected=True, max_jobs=2),
        provider_factory=lambda _job: provider,
        now=lambda: NOW + timedelta(seconds=20),
    )

    assert tuple(item.job_id for item in planned.jobs) == (
        f"{run_id}__002-001",
        f"{run_id}__003-001",
        rejected,
    )
    assert tuple(item.outcome for item in planned.jobs) == (
        "would_process",
        "would_process",
        "would_auto_repair",
    )
    assert calls == [f"{run_id}__002-001", f"{run_id}__003-001"]
    assert processed.accepted == 2 and processed.repair_attempts == 0


def test_an_accepted_job_is_never_reprocessed_or_repaired(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    _repaired_worker(tmp_path, run_id, (ONE_PARAGRAPH, TWO_PARAGRAPHS))
    before = _queue_bytes(tmp_path, run_id)
    calls: list[str] = []

    result = _repaired_worker(
        tmp_path, run_id, ("must not execute",), calls=calls, include_qa_rejected=True, offset=40
    )
    job = load_run_summary(run_id, project_root=tmp_path).jobs[0]

    assert result.processed == 0 and not calls
    assert _queue_bytes(tmp_path, run_id) == before
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    with pytest.raises(AutoRepairError, match="cannot be repaired"):
        plan_auto_repair(run_id, job.job_id, project_root=tmp_path)


def test_auto_repair_dry_run_contacts_no_provider_and_mutates_nothing(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    job_id = _reject_first_job(tmp_path, run_id)
    before = _queue_bytes(tmp_path, run_id)
    calls: list[str] = []

    planned = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_repair=True, include_qa_rejected=True, job_id=job_id, dry_run=True),
        provider_factory=lambda _job: ScriptedProvider(("must not execute",), calls),
    )

    assert planned.dry_run and planned.processed == 1
    assert planned.jobs[0].outcome == "would_auto_repair"
    assert planned.jobs[0].auto_repair_verdict == AutoRepairVerdict.REPAIRABLE.value
    assert planned.jobs[0].auto_repair_findings == ("paragraph_count_mismatch",)
    assert planned.jobs[0].attempt_number == 2
    assert not calls
    assert _queue_bytes(tmp_path, run_id) == before
    assert not list(_run_path(tmp_path, run_id).glob("jobs/*/auto-repair/*"))


def test_dry_run_reports_a_refusal_without_planning_an_attempt(tmp_path: Path) -> None:
    run_id = _competing_variant_run(tmp_path)
    run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("الفا در بند نخست.\n\nآلفا در بند دوم.\n"),
        now=lambda: NOW + timedelta(seconds=10),
    )
    before = _queue_bytes(tmp_path, run_id)
    planned = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(auto_repair=True, include_qa_rejected=True, dry_run=True),
    )

    assert planned.jobs[0].outcome == "would_refuse_repair"
    assert planned.jobs[0].auto_repair_verdict == AutoRepairVerdict.REFUSED.value
    assert planned.jobs[0].attempt_number is None
    assert _queue_bytes(tmp_path, run_id) == before


def test_including_qa_rejected_requires_auto_repair(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    with pytest.raises(Exception, match="enable --auto-repair"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_options(include_qa_rejected=True),
        )


def test_a_targeted_qa_rejected_job_reports_how_to_select_it(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    job_id = _reject_first_job(tmp_path, run_id)
    with pytest.raises(Exception, match="--auto-repair --include-qa-rejected"):
        run_translation_worker(
            run_id,
            project_root=tmp_path,
            options=_options(job_id=job_id, dry_run=True),
        )


def test_an_interrupted_repair_reuses_its_authorization_instead_of_rewriting_it(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path)
    job_id = _reject_first_job(tmp_path, run_id)
    run_path = _run_path(tmp_path, run_id)
    plan = plan_auto_repair(run_id, job_id, project_root=tmp_path)
    _publish_decision(run_path, plan, now=NOW + timedelta(seconds=15))
    decision_path = run_path / "jobs" / plan.job.chunk_id / "auto-repair/002/decision.yaml"
    before = decision_path.read_bytes()

    outcome = auto_repair_job(
        run_id,
        job_id,
        project_root=tmp_path,
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="worker-test",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        provider_factory=lambda _job: SyntheticProvider(TWO_PARAGRAPHS),
        now=lambda: NOW + timedelta(seconds=20),
    )

    assert outcome.outcome == "accepted" and outcome.repair_attempts == 1
    assert decision_path.read_bytes() == before
    assert len(list((run_path / "jobs" / plan.job.chunk_id / "auto-repair").iterdir())) == 1


def test_a_contradicting_authorization_stops_automatic_repair(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path)
    job_id = _reject_first_job(tmp_path, run_id)
    run_path = _run_path(tmp_path, run_id)
    plan = plan_auto_repair(run_id, job_id, project_root=tmp_path)
    _publish_decision(run_path, plan, now=NOW + timedelta(seconds=15))
    decision_path = run_path / "jobs" / plan.job.chunk_id / "auto-repair/002/decision.yaml"
    record = yaml.safe_load(decision_path.read_text(encoding="utf-8"))
    record["rejection_evidence_sha256"] = "0" * 64
    decision_path.write_text(
        yaml.safe_dump(record, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    calls: list[str] = []

    result = _repaired_worker(
        tmp_path, run_id, ("must not execute",), calls=calls, include_qa_rejected=True, offset=20
    )

    assert result.errors == 1 and not calls
    assert (
        load_run_summary(run_id, project_root=tmp_path).jobs[0].acceptance_status
        is TranslationAcceptanceStatus.QA_REJECTED
    )


def test_a_crash_between_repair_and_acceptance_recovers_without_a_provider_call(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path)
    _repaired_worker(tmp_path, run_id, (ONE_PARAGRAPH, TWO_PARAGRAPHS))
    state = load_run_summary(run_id, project_root=tmp_path)
    job_path = state.path / "jobs" / state.jobs[0].chunk_id / "job.yaml"
    job_data = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    job_data.update(
        acceptance_status=None,
        accepted_attempt=None,
        acceptance_file=None,
        acceptance_sha256=None,
        acceptance_origin=None,
    )
    job_path.write_text(
        yaml.safe_dump(job_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    calls: list[str] = []

    restarted = _repaired_worker(
        tmp_path, run_id, ("must not execute",), calls=calls, include_qa_rejected=True, offset=40
    )
    job = load_run_summary(run_id, project_root=tmp_path).jobs[0]

    assert restarted.accepted == 1 and not calls
    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert job.accepted_attempt == 2


def test_automatic_repair_leaves_frozen_resources_and_earlier_evidence_byte_identical(
    tmp_path: Path,
) -> None:
    run_id = _prepared_run(tmp_path)
    _reject_first_job(tmp_path, run_id)
    state = load_run_summary(run_id, project_root=tmp_path)
    chunk = state.jobs[0].chunk_id
    protected = (
        state.path / "jobs" / chunk / "attempts/001",
        state.path / "jobs" / chunk / "acceptance/001",
        state.path / "jobs" / chunk / "prompt-package",
        tmp_path / state.run.snapshot_path,
        tmp_path / "workspace/chapters/001/chunks/001/source.md",
    )
    before = {
        str(path): sorted(
            (item.name, item.read_bytes())
            for item in ([path] if path.is_file() else path.rglob("*"))
            if item.is_file()
        )
        for path in protected
    }

    result = _repaired_worker(
        tmp_path, run_id, (TWO_PARAGRAPHS,), include_qa_rejected=True, offset=30
    )
    after = {
        str(path): sorted(
            (item.name, item.read_bytes())
            for item in ([path] if path.is_file() else path.rglob("*"))
            if item.is_file()
        )
        for path in protected
    }

    assert result.accepted == 1
    assert after == before


def test_worker_cli_reports_the_repair_policy_without_leaking_prose_or_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _prepared_run(tmp_path)
    job_id = _reject_first_job(tmp_path, run_id)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("secrets.token_urlsafe", lambda _size: "worker-secret-token")
    before = _queue_bytes(tmp_path, run_id)

    result = runner.invoke(
        app,
        [
            "run-translation-worker",
            run_id,
            "--provider",
            "fake",
            "--auto-repair",
            "--include-qa-rejected",
            "--job-id",
            job_id,
            "--dry-run",
        ],
    )
    refused = runner.invoke(
        app,
        ["run-translation-worker", run_id, "--provider", "fake", "--include-qa-rejected"],
    )

    assert result.exit_code == 0
    output = result.stdout
    assert f"Auto-repair {job_id}: verdict=repairable findings=paragraph_count_mismatch" in (
        output.replace("\n", "")
    )
    assert "would_auto_" in output
    assert AUTO_REPAIR_POLICY_VERSION in output
    assert "worker-secret-token" not in output and "Claim token" not in output
    assert "فقط یک بند" not in output
    assert _queue_bytes(tmp_path, run_id) == before
    assert refused.exit_code == 1 and "enable --auto-repair" in refused.output
