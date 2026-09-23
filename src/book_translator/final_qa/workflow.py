"""The only final-QA layer that touches the filesystem.

``plan_final_qa_audit`` reads a completed run and diagnoses every standing QA rejection without
writing anything; ``execute_final_qa_audit`` publishes that diagnosis; ``finalize_translation_run``
drives the bounded resolution loop. The split is the ordinary stage convention, and it matters more
than usual here because the read half is what an operator is expected to run first: nothing about
diagnosing a rejection requires permission to change it.

The loop itself is deliberately thin. It owns no remedy of its own — re-evaluation belongs to
:mod:`book_translator.translation_reevaluation`, repair to
:mod:`book_translator.translation_auto_repair`, recovery to the recovery boundary — and it may only
act on the verdicts :data:`ACTIONABLE_VERDICTS` names. Everything else is reported, which is the
correct outcome for a frozen run whose remaining blockers are governance questions: re-freezing
terminology and carrying accepted work forward is a new snapshot's job, not something to be forced
onto a run whose resource hashes are part of its identity.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from book_translator.final_qa.analysis import (
    CompetingVariantAnalysis,
    ParagraphDiagnosis,
    analyze_competing_variants,
    diagnose_paragraphs,
)
from book_translator.final_qa.policy import (
    FINAL_QA_RESOLUTION_POLICY_VERSION,
    FinalQAAssessment,
    assess_final_qa,
)
from book_translator.hashing import sha256_file
from book_translator.models import (
    SCHEMA_VERSION,
    CompetingVariantClass,
    FinalQAAuditReport,
    FinalQABudgetRecord,
    FinalQACompetingVariantRecord,
    FinalQAFinalizationReport,
    FinalQAFindingRecord,
    FinalQAJobDiagnosis,
    FinalQAJobOutcome,
    FinalQAStructuralRecord,
    FinalQAVerdict,
    ProviderName,
    TerminologyRegistry,
    TranslationAcceptanceStatus,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
    TranslationRepairEscalation,
)
from book_translator.models.provider_execution import TranslationExecutionKind
from book_translator.models.translation_provider_recovery import (
    ProviderPreflightRecord,
    ProviderRecoveryVerdict,
)
from book_translator.paragraph_alignment import (
    LEGACY_POSITIONAL,
    MARKED,
    assess_alignment,
    build_alignment_report,
    policy_for,
)
from book_translator.paths import ProjectPaths
from book_translator.prompt_packaging.identity import uses_paragraph_markers
from book_translator.provider_execution import TranslationProvider
from book_translator.provider_execution.artifacts import (
    load_yaml,
    private_write,
    project_relative,
    serialize_yaml,
)
from book_translator.provider_execution.preflight import ProviderPreflightReport
from book_translator.provider_recovery_policy import (
    DEFAULT_MAX_PROVIDER_RECOVERY_ATTEMPTS,
)
from book_translator.structural_repair import (
    StructuralRepairError,
    diagnosis_record,
    repair_translation_structure,
)
from book_translator.terminology import load_registry
from book_translator.terminology import validation as qa_validation
from book_translator.terminology.exemption_workflow import frozen_occurrence_exemptions
from book_translator.translation_auto_repair import (
    ACCEPTED,
    PROVIDER_FAILED,
    AutoRepairError,
    _repair_attempts_used,
    auto_repair_job,
)
from book_translator.translation_provider_recovery import (
    ProviderRecoveryError,
    ProviderRecoveryPlan,
    authorize_provider_recovery,
    plan_provider_recovery,
    record_preflight,
    recovery_execution_kind,
)
from book_translator.translation_queue.locking import queue_lock
from book_translator.translation_queue.workflow import (
    LOCK_TIMEOUT_SECONDS,
    QueueState,
    _append_event,
    _event,
    _load_queue,
    _persist_job_and_run,
    _resolve_run_path,
)
from book_translator.translation_reevaluation import reevaluate_translation_job
from book_translator.translation_repair import load_effective_rejection
from book_translator.translation_repair_policy import (
    DEFAULT_MAX_REPAIR_ATTEMPTS,
    RepairBudget,
    RepairFindingClass,
    override_key,
)
from book_translator.translation_snapshot import validate_snapshot

FINAL_QA_AUDIT_ENGINE_VERSION = "1.1.0"
FINAL_QA_ORCHESTRATOR_VERSION = "1.1.0"
REPAIR_ESCALATION_VERSION = "1.1.0"

#: How many extra provider attempts one governed escalation may ever grant to one job. The bound
#: is the point of the record: a budget that can be widened without limit is not a budget.
MAX_ESCALATED_REPAIR_ATTEMPTS = 1

#: How many resolve-and-re-assess cycles one job may go through in a single finalization. Four is
#: the length of the longest legitimate chain: a provider-free structural repair, the provider
#: repair of whatever it did not resolve, a recovery of that repair if the provider never
#: delivered it, and the re-sent repair.
DEFAULT_MAX_CYCLES = 4

#: The live registry a frozen run's terminology is compared against.
DEFAULT_EFFECTIVE_REGISTRY = Path("config/terminology.yaml")


class FinalQAError(ValueError):
    """Raised when a final QA rejection cannot be diagnosed or resolved safely."""


@dataclass(frozen=True, slots=True)
class JobDiagnosis:
    """One diagnosed rejection, before it is reduced to a persisted record."""

    job: TranslationJob
    assessment: FinalQAAssessment
    diagnosis: FinalQAJobDiagnosis
    analyses: tuple[CompetingVariantAnalysis, ...]
    paragraphs: ParagraphDiagnosis | None


def _repair_overrides(item: "JobDiagnosis") -> dict[str, RepairFindingClass]:
    """Tell the repair policy what the final-QA analyzer proved about each competing variant.

    The repair policy refuses ``competing_variant`` on sight, and it is right to: the code alone
    cannot distinguish a contested canonical from a provider that simply used the wrong word.
    Where the analyzer has settled that question — the canonical is nowhere in the translation
    and the rival is — the classification is handed over explicitly, per candidate, and lands in
    the persisted repair authorization where it can be read back.
    """
    overrides: dict[str, RepairFindingClass] = {}
    for analysis in item.analyses:
        if not analysis.enforceable:
            continue
        if analysis.variant_class is CompetingVariantClass.CANONICAL_ABSENT:
            overrides[override_key("competing_variant", analysis.candidate.candidate_id)] = (
                RepairFindingClass.AUTO_REPAIRABLE
            )
    return overrides


@dataclass(frozen=True, slots=True)
class FinalQAAuditPlan:
    """The complete read-only answer for one run."""

    run_path: Path
    report: FinalQAAuditReport
    diagnoses: tuple[JobDiagnosis, ...]

    @property
    def run_id(self) -> str:
        return self.report.run_id


def _frozen_terminology(
    manifest: TranslationManifest, project_root: Path
) -> tuple[TerminologyRegistry, Path | None, str | None]:
    if manifest.terminology is None:
        return (
            TerminologyRegistry(
                schema_version=SCHEMA_VERSION,
                version="1.0.0",
                book_id=manifest.book.book_id,
                source_language=manifest.book.source_language,
                target_language=manifest.book.target_language,
            ),
            None,
            None,
        )
    frozen_file = manifest.terminology.frozen_file or manifest.terminology.file
    path = project_root / frozen_file
    if sha256_file(path) != manifest.terminology.sha256:
        raise FinalQAError("frozen terminology resource hash mismatch")
    return load_registry(path), frozen_file, manifest.terminology.sha256


def _escalations(
    project_root: Path, job: TranslationJob
) -> tuple[TranslationRepairEscalation, ...]:
    """Load every escalation already granted to one job, oldest first."""
    paths = ProjectPaths(project_root)
    root = paths.translation_repair_escalation(job.run_id, job.chunk_id, 1).parent
    if not root.is_dir():
        return ()
    records = []
    for index, directory in enumerate(sorted(path for path in root.iterdir() if path.is_dir()), 1):
        if directory.name != f"{index:03d}":
            raise FinalQAError("repair escalation numbering is not contiguous")
        records.append(load_yaml(directory / "escalation.yaml", TranslationRepairEscalation))
    return tuple(records)


def _finding_index(
    *, terminology: tuple[str, ...], structural: tuple[str, ...]
) -> tuple[tuple[Literal["terminology", "structural"], str], ...]:
    """Order every distinct blocking code once, per origin, so the record is deterministic."""
    entries: list[tuple[Literal["terminology", "structural"], str]] = [
        ("terminology", code) for code in sorted(set(terminology))
    ]
    entries.extend(("structural", code) for code in sorted(set(structural)))
    return tuple(entries)


def _diagnose(
    state: QueueState,
    job: TranslationJob,
    *,
    project_root: Path,
    frozen_registry: TerminologyRegistry,
    effective_registry: TerminologyRegistry,
    max_repair_attempts: int,
) -> JobDiagnosis:
    """Diagnose one standing rejection against current QA, mutating nothing."""
    # A repair the provider never delivered leaves the job failed with its rejection standing.
    # That state is diagnosable — the translation and the findings are all still there — and an
    # audit that crashed on it would be useless exactly when something has gone wrong.
    recovery_required = job.status is not TranslationJobStatus.SUCCEEDED
    rejection = load_effective_rejection(job, project_root=project_root, allow_failed_repair=True)
    attempt = rejection.attempt
    if attempt.translation_file is None or attempt.translation_sha256 is None:
        raise FinalQAError("rejected attempt has no translation to diagnose")
    # The standing translation is whatever the *effective* verdict judged. That is the provider
    # attempt's own output until a provider-free structural repair has republished the verdict
    # over derived bytes, and reading the attempt directly would diagnose the superseded draft.
    standing_file = rejection.evidence.translation_file
    standing_sha256 = rejection.evidence.translation_sha256
    source_path = project_root / job.chunk_source_file
    translation_path = project_root / standing_file
    if sha256_file(source_path) != job.chunk_source_sha256:
        raise FinalQAError("frozen source hash mismatch")
    if sha256_file(translation_path) != standing_sha256:
        raise FinalQAError("standing translation hash mismatch")
    source_text = source_path.read_text(encoding="utf-8")
    translation_text = translation_path.read_text(encoding="utf-8")

    exemptions = frozen_occurrence_exemptions(
        load_yaml(project_root / state.run.snapshot_path / "manifest.yaml", TranslationManifest),
        project_root,
        registry=frozen_registry,
        chunk_id=job.chunk_id,
        source_text=source_text,
    )
    terminology_report = qa_validation.validate_terminology(
        source_text, translation_text, frozen_registry, exemptions
    )
    structural_report = qa_validation.validate_translation_qa(
        source_text, translation_text, target_language=job.target_language
    )
    alignment_report = build_alignment_report(
        assess_alignment(
            source_text,
            translation_text,
            policy=policy_for(job.target_language),
            contract_mode=MARKED
            if uses_paragraph_markers(attempt.output_contract)
            else LEGACY_POSITIONAL,
        ),
        source_text=source_text,
        translation_text=translation_text,
    )
    analyses = analyze_competing_variants(
        source_text=source_text,
        translation_text=translation_text,
        frozen_registry=frozen_registry,
        effective_registry=effective_registry,
        exemptions=exemptions,
    )
    escalations = _escalations(project_root, job)
    budget = RepairBudget(
        max_repair_attempts=max_repair_attempts,
        repair_attempts_used=_repair_attempts_used(state.path, job),
        max_provider_attempts=job.max_attempts,
        provider_attempts_used=job.provider_attempt_count,
    )
    paragraphs = diagnose_paragraphs(source_text, translation_text)
    # A repair already adopted has spent this job's one structural remedy: the pointer is what
    # says so, and offering the verdict again would loop on a job that cannot take it.
    structural_repair_available = (
        paragraphs.deterministic
        and job.latest_structural_repair_file is None
        and not recovery_required
    )
    assessment = assess_final_qa(
        terminology_report=terminology_report,
        structural_report=structural_report,
        competing_variants=analyses,
        alignment_report=alignment_report,
        budget=budget,
        escalation_available=len(escalations) < MAX_ESCALATED_REPAIR_ATTEMPTS,
        deterministic_structural_repair=structural_repair_available,
    )
    if recovery_required:
        assessment = replace(
            assessment,
            verdict=FinalQAVerdict.OPERATOR_REQUIRED,
            reasons=(*assessment.reasons, "provider_recovery_required"),
        )

    historical_terminology = tuple(
        finding.code
        for finding in rejection.terminology_report.findings
        if finding.severity == "error"
    )
    historical_structural = tuple(
        finding.code
        for finding in rejection.structural_report.findings
        if finding.severity == "error"
    )
    historical_codes = (*historical_terminology, *historical_structural)
    current_codes = assessment.blocking_codes
    findings = [
        FinalQAFindingRecord(
            origin=origin,
            code=code,
            severity="error",
            historical=code in historical_codes,
            current=code in current_codes,
        )
        for origin, code in _finding_index(
            terminology=(
                *historical_terminology,
                *(
                    finding.code
                    for finding in terminology_report.findings
                    if finding.severity == "error"
                ),
            ),
            structural=(
                *historical_structural,
                *(
                    finding.code
                    for finding in structural_report.findings
                    if finding.severity == "error"
                ),
            ),
        )
    ]
    triggered_now = {
        finding.candidate_id
        for finding in terminology_report.findings
        if finding.code == "competing_variant"
    }
    diagnosis = FinalQAJobDiagnosis(
        job_id=job.job_id,
        chapter_id=job.chapter_id,
        chunk_id=job.chunk_id,
        effective_attempt=rejection.evidence.attempt_number,
        acceptance_origin=rejection.origin,
        evidence_file=rejection.evidence_file,
        evidence_sha256=rejection.evidence_sha256,
        attempt_file=rejection.attempt_file,
        attempt_sha256=rejection.attempt_sha256,
        source_sha256=job.chunk_source_sha256,
        translation_sha256=standing_sha256,
        recorded_terminology_qa_version=rejection.terminology_qa_version,
        recorded_structural_qa_version=rejection.structural_qa_version,
        installed_terminology_qa_version=qa_validation.TERMINOLOGY_QA_VERSION,
        installed_structural_qa_version=qa_validation.STRUCTURAL_QA_VERSION,
        historical_blocking_codes=sorted(set(historical_codes)),
        current_blocking_codes=sorted(set(current_codes)),
        reproduces_under_current_qa=set(historical_codes) == set(current_codes),
        current_terminology_valid=terminology_report.valid,
        current_structural_valid=structural_report.valid,
        findings=findings,
        competing_variants=[
            FinalQACompetingVariantRecord(
                candidate_id=analysis.candidate.candidate_id,
                decision_id=analysis.decision.decision_id,
                source_term=analysis.candidate.source_term,
                canonical_target=analysis.decision.target_term,
                competing_variant=analysis.variant,
                variant_class=analysis.variant_class,
                decision_state=analysis.decision.decision_state,
                decision_source=analysis.decision.decision_source,
                candidate_provenance=analysis.candidate.provenance,
                enforcement=analysis.enforcement,
                locked=analysis.decision.locked,
                canonical_present=analysis.canonical_present,
                variant_present=analysis.variant_present,
                source_occurrences=analysis.source_occurrences,
                canonical_occurrences=analysis.canonical_occurrences,
                variant_occurrences=analysis.variant_occurrences,
                recorded_in_effective_registry=analysis.recorded_in_effective_registry,
                rival_candidate_id=analysis.rival_candidate_id,
                rival_locked=analysis.rival_locked,
                reproduces_under_current_qa=(
                    analysis.enforceable and analysis.candidate.candidate_id in triggered_now
                ),
            )
            for analysis in analyses
        ],
        structural=FinalQAStructuralRecord(
            source_paragraphs=paragraphs.source_paragraphs,
            translation_paragraphs=paragraphs.translation_paragraphs,
            paragraph_delta=paragraphs.paragraph_delta,
            blank_line_artifact=paragraphs.blank_line_artifact,
            deterministically_normalizable=paragraphs.deterministic,
            codes=sorted(
                {
                    finding.code
                    for finding in structural_report.findings
                    if finding.severity == "error"
                }
            ),
            diagnosis=diagnosis_record(paragraphs),
        ),
        budget=FinalQABudgetRecord(
            repair_attempts_used=budget.repair_attempts_used,
            max_repair_attempts=budget.max_repair_attempts,
            provider_attempts_used=budget.provider_attempts_used,
            max_provider_attempts=budget.max_provider_attempts,
            remaining=budget.remaining,
            escalations_granted=len(escalations),
        ),
        verdict=assessment.verdict,
        verdict_reasons=list(assessment.reasons),
        accepts_without_provider=assessment.accepts_without_provider,
        locked_terminology_involved=assessment.locked_terminology_involved,
        provider_recovery_required=recovery_required,
    )
    return JobDiagnosis(job, assessment, diagnosis, analyses, paragraphs)


def plan_final_qa_audit(
    run_id_or_path: str | Path,
    *,
    project_root: Path,
    effective_registry_path: Path = DEFAULT_EFFECTIVE_REGISTRY,
    max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
    now: Callable[[], datetime] | None = None,
) -> FinalQAAuditPlan:
    """Diagnose every standing QA rejection in one run. Reads only."""
    project_root = project_root.resolve()
    clock = now or (lambda: datetime.now(UTC))
    run_path = _resolve_run_path(run_id_or_path, project_root)
    state = _load_queue(run_path, project_root)
    # The snapshot is validated in full, once, before anything is read out of it: every
    # diagnosis below is only as trustworthy as the frozen inputs it is derived from.
    snapshot_result = validate_snapshot(
        project_root / state.run.snapshot_path, project_root=project_root, now=clock
    )
    if (
        not snapshot_result.report.valid
        or snapshot_result.snapshot is None
        or snapshot_result.manifest is None
        or snapshot_result.snapshot.snapshot_id != state.run.snapshot_id
        or snapshot_result.snapshot.manifest_sha256 != state.run.snapshot_manifest_sha256
    ):
        raise FinalQAError("frozen translation snapshot failed validation")
    manifest = snapshot_result.manifest
    frozen_registry, terminology_file, terminology_sha256 = _frozen_terminology(
        manifest, project_root
    )
    live_path = project_root / effective_registry_path
    if live_path.is_file():
        effective_registry = load_registry(live_path)
        effective_file: Path | None = effective_registry_path
        effective_sha256: str | None = sha256_file(live_path)
    else:
        # Without a live registry the frozen one is the only governed state there is, and saying
        # a competitor was "retired" on that basis would be an invention.
        effective_registry = frozen_registry
        effective_file = terminology_file
        effective_sha256 = terminology_sha256

    diagnoses = tuple(
        _diagnose(
            state,
            job,
            project_root=project_root,
            frozen_registry=frozen_registry,
            effective_registry=effective_registry,
            max_repair_attempts=max_repair_attempts,
        )
        for job in sorted(state.jobs, key=lambda item: item.job_id)
        if job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    )
    verdict_counts: dict[str, int] = {}
    variant_counts: dict[str, int] = {}
    for item in diagnoses:
        key = item.diagnosis.verdict.value
        verdict_counts[key] = verdict_counts.get(key, 0) + 1
        for record in item.diagnosis.competing_variants:
            if not record.reproduces_under_current_qa:
                continue
            variant_counts[record.variant_class.value] = (
                variant_counts.get(record.variant_class.value, 0) + 1
            )
    report = FinalQAAuditReport(
        schema_version=SCHEMA_VERSION,
        final_qa_audit_engine_version=FINAL_QA_AUDIT_ENGINE_VERSION,
        final_qa_resolution_policy_version=FINAL_QA_RESOLUTION_POLICY_VERSION,
        terminology_qa_version=qa_validation.TERMINOLOGY_QA_VERSION,
        structural_qa_version=qa_validation.STRUCTURAL_QA_VERSION,
        run_id=state.run.run_id,
        snapshot_id=state.run.snapshot_id,
        manifest_sha256=state.run.snapshot_manifest_sha256,
        terminology_file=terminology_file,
        terminology_sha256=terminology_sha256,
        effective_registry_file=effective_file,
        effective_registry_sha256=effective_sha256,
        frozen_registry_is_current=effective_sha256 == terminology_sha256,
        job_count=len(state.jobs),
        accepted_count=sum(
            1 for job in state.jobs if job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
        ),
        qa_rejected_count=len(diagnoses),
        verdict_counts=verdict_counts,
        variant_class_counts=variant_counts,
        jobs=[item.diagnosis for item in diagnoses],
        created_at=clock(),
    )
    return FinalQAAuditPlan(run_path, report, diagnoses)


def _publish(destination: Path, filename: str, payload: bytes, *, overwrite: bool) -> Path:
    """Publish one artifact into its own directory without disturbing anything beside it.

    The audit is derived and is republished in place; an escalation is an authorization and is
    written once. Only the named file is replaced, because the audit directory also holds the
    finalization history and replacing the directory would take that with it.
    """
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    path = destination / filename
    if path.exists() and not overwrite:
        raise FinalQAError(f"final QA artifact already exists: {path.name}")
    private_write(path, payload, overwrite=overwrite)
    return path


def execute_final_qa_audit(plan: FinalQAAuditPlan, *, project_root: Path) -> Path:
    """Publish the audit. The audit is derived, so re-running it replaces its own artifact."""
    destination = ProjectPaths(project_root.resolve()).final_qa_audit(plan.run_id)
    return _publish(
        destination, "audit.yaml", serialize_yaml(plan.report).encode("utf-8"), overwrite=True
    )


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    """What one finalization run did, and what is left."""

    run_id: str
    report: FinalQAFinalizationReport
    report_file: Path | None

    @property
    def dry_run(self) -> bool:
        return self.report.dry_run


def _grant_escalation(
    run_path: Path,
    item: JobDiagnosis,
    *,
    project_root: Path,
    now: datetime,
) -> TranslationRepairEscalation:
    """Publish and apply one bounded exceptional repair-budget escalation.

    The record is written before the ceiling moves, so an interruption leaves evidence of an
    unspent grant rather than a widened budget nobody authorized. It records the structural
    diagnosis alongside the findings because the grant's justification is not only "these
    findings are repairable" but "and nothing free would have repaired them".
    """
    job = item.job
    granted = len(_escalations(project_root, job))
    if granted >= MAX_ESCALATED_REPAIR_ATTEMPTS:
        raise FinalQAError("this job has already used its exceptional repair escalation")
    if item.assessment.verdict is not FinalQAVerdict.EXHAUSTED:
        raise FinalQAError("only an exhausted repairable rejection may be escalated")
    if item.paragraphs is not None and item.paragraphs.deterministic:
        raise FinalQAError("a deterministic structural repair must be spent before an escalation")
    rejection = load_effective_rejection(job, project_root=project_root)
    defect = item.paragraphs.defect_class.value if item.paragraphs is not None else "unknown"
    record = TranslationRepairEscalation(
        schema_version=SCHEMA_VERSION,
        repair_escalation_version=REPAIR_ESCALATION_VERSION,
        final_qa_resolution_policy_version=item.assessment.policy_version,
        run_id=job.run_id,
        job_id=job.job_id,
        escalation_number=granted + 1,
        original_max_attempts=job.max_attempts,
        granted_attempts=MAX_ESCALATED_REPAIR_ATTEMPTS,
        resulting_max_attempts=job.max_attempts + MAX_ESCALATED_REPAIR_ATTEMPTS,
        provider_attempts_used=job.provider_attempt_count,
        repair_attempts_used=item.assessment.budget.repair_attempts_used,
        triggering_codes=sorted(set(item.assessment.blocking_codes)),
        rejection_evidence_file=rejection.evidence_file,
        rejection_evidence_sha256=rejection.evidence_sha256,
        authorized_attempt_number=job.provider_attempt_count + 1,
        reason=(f"{item.assessment.budget.limiting_bound}_exhausted_behind_repairable_{defect}"),
        structural_diagnosis=(
            None if item.paragraphs is None else diagnosis_record(item.paragraphs)
        ),
        deterministic_repair_available=False,
        authorized_by="operator",
        created_at=now,
    )
    destination = ProjectPaths(project_root).translation_repair_escalation(
        job.run_id, job.chunk_id, record.escalation_number
    )
    _publish(
        destination, "escalation.yaml", serialize_yaml(record).encode("utf-8"), overwrite=False
    )
    with queue_lock(run_path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        state = _load_queue(run_path, project_root)
        selected = next((one for one in state.jobs if one.job_id == job.job_id), None)
        if selected is None or selected.max_attempts != record.original_max_attempts:
            raise FinalQAError("job attempt ceiling changed while the escalation was authorized")
        changed = selected.model_copy(
            update={
                "max_attempts": record.resulting_max_attempts,
                "updated_at": now,
                "recent_events": _append_event(
                    selected,
                    _event(
                        "repair_budget_escalated",
                        now,
                        reason=f"{record.escalation_number}:{record.granted_attempts}",
                    ),
                ),
            }
        )
        jobs = tuple(changed if one.job_id == changed.job_id else one for one in state.jobs)
        _persist_job_and_run(state, jobs, changed, now)
    return record


def finalize_translation_run(
    run_id_or_path: str | Path,
    *,
    project_root: Path,
    dry_run: bool = True,
    provider: ProviderName | None = None,
    model: str | None = None,
    worker_id: str = "book-translator-final-qa",
    lease_seconds: int = 900,
    timeout_seconds: int = 600,
    max_output_tokens: int = 8192,
    reasoning_effort: str | None = None,
    max_cycles: int = DEFAULT_MAX_CYCLES,
    max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
    allow_repair_escalation: bool = False,
    allow_provider_recovery: bool = True,
    max_provider_recovery_attempts: int = DEFAULT_MAX_PROVIDER_RECOVERY_ATTEMPTS,
    effective_registry_path: Path = DEFAULT_EFFECTIVE_REGISTRY,
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None = None,
    preflight: Callable[[ProviderName], ProviderPreflightReport] | None = None,
    now: Callable[[], datetime] | None = None,
) -> FinalizationResult:
    """Resolve every standing QA rejection in one run as far as governed evidence allows.

    One command drives the whole ladder, cheapest rung first: re-evaluate what current QA already
    accepts, repair block structure without a provider where the source proves how, spend a
    provider repair on what is left, recover a repair the provider failed to deliver, and — only
    when explicitly authorized — grant one bounded exceptional attempt to a job whose budget is
    spent. Every rung is an existing governed boundary; nothing here is a second implementation
    of one.

    A dry run is a complete diagnosis and nothing else: no provider is contacted, no registry is
    read for writing, no queue field moves, and no acceptance changes. It is the default, because
    the first question about a finished book is always what is wrong with it.
    """
    if max_cycles < 1:
        raise FinalQAError("max cycles must be at least one")
    project_root = project_root.resolve()
    clock = now or (lambda: datetime.now(UTC))
    plan = plan_final_qa_audit(
        run_id_or_path,
        project_root=project_root,
        effective_registry_path=effective_registry_path,
        max_repair_attempts=max_repair_attempts,
        now=clock,
    )
    options = _Options(
        dry_run=dry_run,
        provider=provider,
        model=model,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
        max_cycles=max_cycles,
        max_repair_attempts=max_repair_attempts,
        allow_repair_escalation=allow_repair_escalation,
        allow_provider_recovery=allow_provider_recovery,
        max_provider_recovery_attempts=max_provider_recovery_attempts,
        effective_registry_path=effective_registry_path,
        provider_factory=provider_factory,
    )
    session = _RecoverySession(provider=provider, preflight=preflight)
    outcomes = [
        _resolve_one(plan.run_path, item, options, session, project_root=project_root, now=clock)
        for item in plan.diagnoses
    ]

    final_state = _load_queue(plan.run_path, project_root)
    accepted = sum(
        1
        for job in final_state.jobs
        if job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    )
    rejected = sum(
        1
        for job in final_state.jobs
        if job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    )
    existing = _finalization_history(project_root, plan.run_id)
    report = FinalQAFinalizationReport(
        schema_version=SCHEMA_VERSION,
        final_qa_orchestrator_version=FINAL_QA_ORCHESTRATOR_VERSION,
        final_qa_resolution_policy_version=FINAL_QA_RESOLUTION_POLICY_VERSION,
        run_id=plan.run_id,
        finalization_number=existing + 1,
        dry_run=dry_run,
        provider=provider.value if provider is not None else None,
        max_cycles=max_cycles,
        repair_escalation_allowed=allow_repair_escalation,
        provider_recovery_allowed=allow_provider_recovery,
        audit=plan.report,
        outcomes=outcomes,
        accepted_count=accepted,
        qa_rejected_count=rejected,
        operator_required_job_ids=sorted(
            outcome.job_id
            for outcome in outcomes
            if outcome.acceptance_status_after == TranslationAcceptanceStatus.QA_REJECTED.value
        ),
        provider_calls=sum(outcome.provider_attempts_spent for outcome in outcomes),
        structural_repairs_applied=sum(outcome.structural_repairs_applied for outcome in outcomes),
        escalations_granted=sum(1 for outcome in outcomes if outcome.escalation_granted),
        run_status_after=final_state.run.status.value,
        created_at=clock(),
    )
    if dry_run:
        return FinalizationResult(plan.run_id, report, None)
    destination = ProjectPaths(project_root).final_qa_finalization(
        plan.run_id, report.finalization_number
    )
    published = _publish(
        destination, "report.yaml", serialize_yaml(report).encode("utf-8"), overwrite=False
    )
    return FinalizationResult(plan.run_id, report, project_relative(published, project_root))


def _finalization_history(project_root: Path, run_id: str) -> int:
    root = ProjectPaths(project_root).final_qa_finalization(run_id, 1).parent
    if not root.is_dir():
        return 0
    return sum(1 for path in root.iterdir() if path.is_dir())


@dataclass(slots=True)
class _RecoverySession:
    """One finalization's provider-health evidence, taken lazily and at most once.

    A recovery decision needs *current* health, but taking a preflight per job would run a
    subprocess per job for an answer that does not change that fast. The record is dropped again
    the moment a recovered attempt fails, so a provider that lapsed mid-run can never authorize a
    second recovery on evidence that predates the lapse.
    """

    provider: ProviderName | None
    preflight: Callable[[ProviderName], ProviderPreflightReport] | None = None
    record: ProviderPreflightRecord | None = None

    def evidence(self, now: datetime) -> ProviderPreflightRecord | None:
        if self.provider is None:
            return None
        if self.record is None:
            self.record = record_preflight(
                self.provider, performed_at=now, preflight=self.preflight
            )
        return self.record

    def invalidate(self) -> None:
        self.record = None


@dataclass(frozen=True, slots=True)
class _Options:
    """Everything one job's resolution needs that is the same for every job in the run."""

    dry_run: bool
    provider: ProviderName | None
    model: str | None
    worker_id: str
    lease_seconds: int
    timeout_seconds: int
    max_output_tokens: int
    reasoning_effort: str | None
    max_cycles: int
    max_repair_attempts: int
    allow_repair_escalation: bool
    allow_provider_recovery: bool
    max_provider_recovery_attempts: int
    effective_registry_path: Path
    provider_factory: Callable[[TranslationJob], TranslationProvider] | None


@dataclass(slots=True)
class _Progress:
    """What one job's resolution has done so far, accumulated across its cycles."""

    action: Literal[
        "none",
        "reevaluated",
        "structurally_repaired",
        "repaired",
        "recovered_and_repaired",
        "escalated_and_repaired",
        "reported",
    ] = "none"
    planned: Literal[
        "none",
        "reevaluate",
        "repair_structure",
        "repair_translation",
        "recover_provider_failure",
        "escalate_and_repair",
        "report",
    ] = "none"
    status: TranslationAcceptanceStatus = TranslationAcceptanceStatus.QA_REJECTED
    provider_calls: int = 0
    structural_repairs: int = 0
    escalated: bool = False
    recovered: bool = False
    cycles: int = 0
    reasons: list[str] = field(default_factory=list)


def _planned_action(
    item: JobDiagnosis, options: _Options
) -> tuple[
    Literal[
        "none",
        "reevaluate",
        "repair_structure",
        "repair_translation",
        "recover_provider_failure",
        "escalate_and_repair",
        "report",
    ],
    tuple[str, ...],
]:
    """Say what an applying run would do to this job, and why, having done nothing.

    This is the whole answer a dry run owes an operator, so it is derived here once and used by
    both halves: the applying path takes the same first step this names.
    """
    verdict = item.diagnosis.verdict
    if item.diagnosis.provider_recovery_required:
        if options.allow_provider_recovery and options.provider is not None:
            return "recover_provider_failure", ("provider_recovery_would_be_attempted",)
        return "report", ("provider_recovery_required",)
    if verdict is FinalQAVerdict.REPAIR_STRUCTURE:
        return "repair_structure", ("deterministic_structural_repair_would_be_applied",)
    if verdict in {FinalQAVerdict.REEVALUATE, FinalQAVerdict.ACCEPTED_NO_CHANGE}:
        return "reevaluate", ()
    if verdict is FinalQAVerdict.REPAIR_TRANSLATION:
        if options.provider is None:
            return "report", ("provider_required",)
        return "repair_translation", ()
    if verdict is FinalQAVerdict.EXHAUSTED:
        escalatable = (
            options.allow_repair_escalation
            and item.diagnosis.budget.escalations_granted < MAX_ESCALATED_REPAIR_ATTEMPTS
            and options.provider is not None
        )
        if escalatable:
            return "escalate_and_repair", ("exceptional_repair_escalation_would_be_granted",)
        return "report", ("escalation_not_authorized",)
    return "report", ()


def _recover(
    run_path: Path,
    item: JobDiagnosis,
    options: _Options,
    session: _RecoverySession,
    *,
    project_root: Path,
    now: Callable[[], datetime],
) -> tuple[bool, tuple[str, ...]]:
    """Return one failed repair to the state the repair mechanism acts on, or say why not.

    Nothing is executed here and no request is sent: the recovery boundary owns the decision and
    the re-entry, and this only asks it and reports the answer.
    """
    job = item.job
    if recovery_execution_kind(job) is not TranslationExecutionKind.REPAIR:
        return False, ("job_is_not_a_recoverable_repair",)
    try:
        plan: ProviderRecoveryPlan = plan_provider_recovery(
            run_path,
            job.job_id,
            project_root=project_root,
            max_provider_recovery_attempts=options.max_provider_recovery_attempts,
            max_repair_attempts=options.max_repair_attempts,
            now=now,
        )
        if plan.verdict is ProviderRecoveryVerdict.PREFLIGHT_REQUIRED:
            plan = plan_provider_recovery(
                run_path,
                job.job_id,
                project_root=project_root,
                max_provider_recovery_attempts=options.max_provider_recovery_attempts,
                max_repair_attempts=options.max_repair_attempts,
                preflight=session.evidence(now()),
                now=now,
            )
        if not plan.recoverable:
            return False, (
                f"provider_recovery:{plan.verdict.value}",
                *plan.assessment.blocking_reasons,
            )
        authorize_provider_recovery(run_path, plan, project_root=project_root, now=now)
    except ProviderRecoveryError as exc:
        return False, (f"provider_recovery_refused:{exc}",)
    return True, (f"provider_recovery_authorized:{plan.authorized_attempt_number}",)


def _repair(
    run_path: Path,
    item: JobDiagnosis,
    options: _Options,
    *,
    project_root: Path,
    max_repair_attempts: int,
    now: Callable[[], datetime],
) -> tuple[TranslationAcceptanceStatus, int, bool]:
    """Delegate one provider repair, returning the QA answer, the spend, and whether it failed."""
    if options.provider is None:
        raise FinalQAError("a translation repair requires an explicit provider")
    try:
        repaired = auto_repair_job(
            run_path,
            item.job.job_id,
            project_root=project_root,
            provider=options.provider,
            model=options.model,
            worker_id=options.worker_id,
            lease_seconds=options.lease_seconds,
            timeout_seconds=options.timeout_seconds,
            max_output_tokens=options.max_output_tokens,
            reasoning_effort=options.reasoning_effort,
            max_repair_attempts=max_repair_attempts,
            terminology_overrides=_repair_overrides(item),
            provider_factory=options.provider_factory,
            now=now,
        )
    except AutoRepairError as exc:
        raise FinalQAError(f"automatic repair refused: {exc}") from exc
    status = (
        TranslationAcceptanceStatus.ACCEPTED
        if repaired.outcome == ACCEPTED
        else TranslationAcceptanceStatus.QA_REJECTED
    )
    return status, repaired.repair_attempts, repaired.outcome == PROVIDER_FAILED


def _resolve_one(
    run_path: Path,
    item: JobDiagnosis,
    options: _Options,
    session: _RecoverySession,
    *,
    project_root: Path,
    now: Callable[[], datetime],
) -> FinalQAJobOutcome:
    """Drive one job through the bounded resolve-and-re-assess loop.

    Each cycle takes exactly one step — recover, repair the structure for free, re-evaluate, or
    spend one provider repair — and then re-diagnoses from the workspace rather than reasoning
    forward from what it just did. That is what lets a structural repair and a provider repair
    compose without either one having to know about the other.
    """
    before = TranslationAcceptanceStatus.QA_REJECTED
    verdict = item.diagnosis.verdict
    progress = _Progress(status=before, reasons=list(item.assessment.reasons))
    planned, planned_reasons = _planned_action(item, options)
    progress.planned = planned
    progress.reasons.extend(planned_reasons)

    def outcome() -> FinalQAJobOutcome:
        return FinalQAJobOutcome(
            job_id=item.job.job_id,
            verdict=verdict,
            action=progress.action,
            planned_action=progress.planned,
            acceptance_status_before=before.value,
            acceptance_status_after=progress.status.value,
            provider_attempts_spent=progress.provider_calls,
            structural_repairs_applied=progress.structural_repairs,
            escalation_granted=progress.escalated,
            provider_recovery_authorized=progress.recovered,
            cycles=progress.cycles,
            reasons=progress.reasons,
        )

    if options.dry_run or planned in {"none", "report"}:
        progress.action = "none" if options.dry_run else "reported"
        return outcome()

    current: JobDiagnosis | None = item
    max_repair_attempts = options.max_repair_attempts
    while progress.cycles < options.max_cycles and current is not None:
        progress.cycles += 1
        step, reasons = _planned_action(current, options)
        progress.reasons.extend(reasons)
        if step in {"none", "report"}:
            if progress.action == "none":
                progress.action = "reported"
            break
        if step == "recover_provider_failure":
            recovered, reasons = _recover(
                run_path, current, options, session, project_root=project_root, now=now
            )
            progress.reasons.extend(reasons)
            if not recovered:
                progress.action = "reported"
                break
            progress.recovered = True
        elif step == "repair_structure":
            try:
                result = repair_translation_structure(
                    run_path,
                    current.job.job_id,
                    project_root=project_root,
                    dry_run=False,
                    now=now,
                )
            except StructuralRepairError as exc:
                raise FinalQAError(f"structural repair refused: {exc}") from exc
            if not result.applied:
                progress.reasons.extend(result.plan.refusals)
                progress.action = "reported"
                break
            progress.structural_repairs += 1
            progress.action = "structurally_repaired"
            progress.status = result.resulting_status
            progress.reasons.append(
                f"structural_repair:{result.plan.defect_class.value}:"
                f"{result.resulting_status.value}"
            )
        elif step == "reevaluate":
            reevaluated = reevaluate_translation_job(
                run_path,
                current.job.job_id,
                current.diagnosis.effective_attempt,
                project_root=project_root,
                now=now,
            )
            progress.action = "reevaluated"
            progress.status = reevaluated.resulting_status
            break
        else:
            if step == "escalate_and_repair":
                _grant_escalation(run_path, current, project_root=project_root, now=now())
                progress.escalated = True
                max_repair_attempts += MAX_ESCALATED_REPAIR_ATTEMPTS
            status, spent, provider_failed = _repair(
                run_path,
                current,
                options,
                project_root=project_root,
                max_repair_attempts=max_repair_attempts,
                now=now,
            )
            progress.provider_calls += spent
            progress.status = status
            progress.action = (
                "escalated_and_repaired"
                if progress.escalated
                else ("recovered_and_repaired" if progress.recovered else "repaired")
            )
            if provider_failed:
                # The provider never delivered. The recovery boundary decides whether the same
                # repair may be re-sent, and the next cycle asks it with fresh health evidence.
                session.invalidate()
            if status is TranslationAcceptanceStatus.ACCEPTED:
                break
        current = _rediagnose(
            run_path,
            item.job.job_id,
            project_root=project_root,
            effective_registry_path=options.effective_registry_path,
            max_repair_attempts=max_repair_attempts,
            now=now,
        )
        if current is None:
            progress.status = TranslationAcceptanceStatus.ACCEPTED
            break
    return outcome()


def _rediagnose(
    run_path: Path,
    job_id: str,
    *,
    project_root: Path,
    effective_registry_path: Path,
    max_repair_attempts: int,
    now: Callable[[], datetime],
) -> JobDiagnosis | None:
    """Re-read one job after acting on it, or return ``None`` once it is no longer rejected."""
    plan = plan_final_qa_audit(
        run_path,
        project_root=project_root,
        effective_registry_path=effective_registry_path,
        max_repair_attempts=max_repair_attempts,
        now=now,
    )
    return next((item for item in plan.diagnoses if item.job.job_id == job_id), None)


def operator_required_terminology(
    report: FinalQAAuditReport,
) -> tuple[tuple[str, str, str, CompetingVariantClass, int], ...]:
    """Group the unresolved competing variants by the decision that has to answer for them.

    Nineteen rejected chunks are not nineteen questions. They are a handful of terminology
    decisions each appearing in several chapters, and an operator who is shown the decisions
    rather than the chunks answers each one once.
    """
    from book_translator.models import OPERATOR_VARIANT_CLASSES

    grouped: dict[tuple[str, str, str, CompetingVariantClass], int] = {}
    for job in report.jobs:
        for record in job.competing_variants:
            if not record.reproduces_under_current_qa:
                continue
            if record.variant_class not in OPERATOR_VARIANT_CLASSES:
                continue
            key = (
                record.candidate_id,
                record.source_term,
                record.competing_variant,
                record.variant_class,
            )
            grouped[key] = grouped.get(key, 0) + 1
    return tuple(
        (candidate_id, source_term, variant, variant_class, count)
        for (candidate_id, source_term, variant, variant_class), count in sorted(
            grouped.items(), key=lambda entry: (-entry[1], entry[0][0])
        )
    )


__all__ = [
    "DEFAULT_MAX_CYCLES",
    "FINAL_QA_AUDIT_ENGINE_VERSION",
    "FINAL_QA_ORCHESTRATOR_VERSION",
    "MAX_ESCALATED_REPAIR_ATTEMPTS",
    "REPAIR_ESCALATION_VERSION",
    "FinalQAAuditPlan",
    "FinalQAError",
    "FinalizationResult",
    "JobDiagnosis",
    "execute_final_qa_audit",
    "finalize_translation_run",
    "operator_required_terminology",
    "plan_final_qa_audit",
]
