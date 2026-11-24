import hashlib
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import typer
import yaml
from rich.console import Console

from book_translator.artifact_regeneration import (
    ArtifactRegenerationError,
    ArtifactRegenerationExecution,
    ArtifactRegenerationPlan,
    execute_artifact_regeneration,
    plan_artifact_regeneration,
)
from book_translator.atomic_write import atomic_write_text
from book_translator.book_identity import (
    BookIdentityMigrationError,
    BookIdentityMigrationPlan,
    execute_book_identity_migration,
    plan_book_identity_migration,
)
from book_translator.book_metadata import (
    BOOK_METADATA_ENGINE_VERSION,
    BookMetadataError,
    BookMetadataPlan,
    OperatorAssertion,
    execute_book_metadata,
    plan_book_metadata,
)
from book_translator.chapter_extraction import (
    ChapterExtractionError,
    ExtractionPlan,
    ExtractionResult,
    execute_extraction,
    plan_extraction,
)
from book_translator.chapter_titles import (
    CHAPTER_TITLE_ENGINE_VERSION,
    CHAPTER_TITLE_PROMPT_VERSION,
    CHAPTER_TITLE_QA_VERSION,
    ChapterTitleError,
    ChapterTitlePlan,
    execute_chapter_titles,
    plan_chapter_titles,
)
from book_translator.config import (
    BookConfig,
    ProjectDataError,
    load_book_config,
    require_epub_path,
    require_pdf_path,
)
from book_translator.document_chunking import CHUNK_ENGINE_VERSION, ChunkingConfig
from book_translator.document_chunking.workflow import (
    ChapterChunkingPlan,
    ChunkingExecution,
    execute_chapter_chunking,
    plan_chapter_chunking,
)
from book_translator.epub import (
    EPUB_BUILDER_VERSION,
    EpubBuildError,
    EpubBuildPlan,
    execute_epub_build,
    plan_epub_build,
)
from book_translator.epub_ingestion import (
    EpubImportPlan,
    EpubImportResult,
    EpubIngestionError,
    execute_epub_import,
    plan_epub_import,
)
from book_translator.execution_resolution import (
    EXECUTION_RESOLUTION_ENGINE_VERSION,
    ExecutionResolutionError,
    ReservationResolutionPlan,
    ReservationResolutionResult,
    execute_reservation_resolution,
    inspect_execution_reservations,
    plan_reservation_resolution,
    planned_result,
    resolution_outcome,
)
from book_translator.final_qa import (
    DEFAULT_MAX_CYCLES,
    FinalQAError,
    execute_final_qa_audit,
    finalize_translation_run,
    operator_required_terminology,
    plan_final_qa_audit,
)
from book_translator.hashing import sha256_file
from book_translator.literary_editing import (
    LiteraryProviderError,
    LiteraryWorkflowError,
    TransportLiteraryRoles,
    execute_literary_run,
    plan_literary_run,
)
from book_translator.models import (
    ACTIONABLE_VERDICTS,
    PROVIDER_FREE_ACTION_VERDICTS,
    BookMetadataProvenance,
    DecisionSource,
    DecisionState,
    EvidenceProvenance,
    EvidenceStatus,
    ExecutionReservationResolutionKind,
    FinalQAAuditReport,
    FinalQAVerdict,
    LiteraryRunArtifact,
    ProviderName,
    QuestionCategory,
    QuestionResearch,
    QuestionResolution,
    QuestionStatus,
    ResearchState,
    TerminologyAcceptancePolicy,
    TerminologyEnforcement,
    TerminologyOccurrence,
    TerminologyReview,
    TerminologyType,
    TranslationAcceptanceEvidence,
    TranslationAcceptanceStatus,
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationJobStatus,
)
from book_translator.models.source_anomaly import (
    SourceAnomalyCategory,
    SourceAnomalyEvidence,
    SourceAnomalySeverity,
    SourceAnomalyStatus,
)
from book_translator.models.source_audit import AuditCategory, AuditConfidence
from book_translator.models.terminology_reconciliation import (
    ConflictGraph,
    ReconciliationProvenanceCorrection,
    ReconciliationRun,
)
from book_translator.models.terminology_remediation import (
    TerminologyExactnessDisposition,
    TerminologyFindingFamily,
    TerminologyRemediationPlan,
)
from book_translator.normalization import NORMALIZATION_VERSION
from book_translator.normalization.inspection import (
    NormalizationChangeInspection,
    inspect_normalization_changes,
)
from book_translator.normalization.workflow import (
    ChapterNormalizationError,
    NormalizationExecution,
    NormalizationPlan,
    execute_normalization,
    plan_normalization,
)
from book_translator.paragraph_alignment.workflow import (
    AlignmentAuditError,
    execute_alignment_audit,
    plan_alignment_audit,
)
from book_translator.paths import ProjectPaths, chapter_id_from_selector
from book_translator.pdf_extractor import extract_pages
from book_translator.pdf_inspector import PdfError, PdfInspectionResult, inspect_pdf
from book_translator.prompt_packaging import (
    MARKED_OUTPUT_CONTRACT_VERSION,
    OUTPUT_CONTRACT_VERSION,
    PromptPackageError,
    execute_prompt_packages,
    list_prompt_packages,
    plan_prompt_packages,
    validate_prompt_package,
)
from book_translator.prompt_packaging.workflow import PromptPackagePlan
from book_translator.provider_execution import (
    ExecutorError,
    ProviderExecutionError,
    TranslationProvider,
    execute_translation_job,
    list_attempts,
    plan_execution,
    preflight_provider,
    validate_attempt,
)
from book_translator.provider_recovery_policy import (
    DEFAULT_MAX_PROVIDER_RECOVERY_ATTEMPTS,
    PROVIDER_RECOVERY_POLICY_VERSION,
)
from book_translator.release_assembly import (
    RELEASE_ASSEMBLY_ENGINE_VERSION,
    ReleaseAssemblyError,
    ReleaseAssemblyPlan,
    execute_release_assembly,
    plan_release_assembly,
)
from book_translator.release_assembly.waiver import ReleaseWaiverError
from book_translator.release_verification import (
    RELEASE_VERIFICATION_ENGINE_VERSION,
    ReleaseCheckStatus,
    ReleaseVerificationError,
    ReleaseVerificationReport,
    ReleaseVerificationStatus,
    serialize_verification,
    verify_release_artifact,
    write_verification_report,
)
from book_translator.semantic_gate import (
    SEMANTIC_GATE_POLICY_VERSION,
    SemanticGateError,
    execute_semantic_gate,
    plan_semantic_gate,
)
from book_translator.semantic_qa import (
    ChunkSelection,
    SemanticCoverageError,
    SemanticTaskError,
    TransportSemanticJudge,
    execute_semantic_coverage,
    plan_semantic_coverage,
)
from book_translator.semantic_qa.workflow import SemanticCoveragePlan, SemanticCoverageResult
from book_translator.semantic_revision import TransportSemanticReviser
from book_translator.source_anomalies import (
    SourceAnomalyError,
    load_source_anomalies,
    register_anomaly,
    save_source_anomalies,
)
from book_translator.source_quality_audit import (
    SourceAuditError,
    audit_source_quality,
    filter_findings,
    inspect_finding,
    persist_report,
    serialize_report,
)
from book_translator.status import load_progress, render_status
from book_translator.structural_repair import StructuralRepairError
from book_translator.terminology import (
    CodexCliTerminologyProvider,
    FakeTerminologyProvider,
    TerminologyAnalysisError,
    TerminologyError,
    add_candidate,
    candidate_identity,
    load_registry,
    registry_identity,
    reject_candidate,
    save_registry,
    set_decision,
    validate_terminology,
    validate_translation_qa,
)
from book_translator.terminology.analysis import apply_proposals
from book_translator.terminology.canonical import canonical_view
from book_translator.terminology.conflict_graph import build_conflict_graph
from book_translator.terminology.exactness_audit import ExactnessRisk, audit_exact_enforcement
from book_translator.terminology.question_workflow import (
    analyze_impact,
    execute_decision_correction,
    load_or_empty_questions,
    plan_decision_correction,
    publish_questions,
    readiness,
)
from book_translator.terminology.questions import (
    QuestionError,
    question_identity,
    questions_identity,
    resolve_question,
    upsert_question,
)
from book_translator.terminology.reconciliation import serialize_graph
from book_translator.terminology.remediation import (
    TerminologyRemediationCategory,
    TerminologyRemediationReport,
    audit_registry_remediation,
    serialize_remediation_report,
)
from book_translator.terminology.remediation_engine import (
    TerminologyRemediationPlanError,
    execute_remediation_plan,
    plan_registry_remediation,
    serialize_plan,
    serialize_simulation,
    simulate_remediation_plan,
)
from book_translator.terminology.workflow import (
    TerminologyWorkflowError,
    audit_proposal_run,
    audit_review_run,
    execute_discovery,
    execute_proposals,
    execute_reviews,
    load_discovery_output,
    load_proposal_output,
    load_review_output,
    plan_discovery,
    plan_proposals,
    plan_ready_corpus_discovery,
    plan_reviews,
)
from book_translator.toc_discovery import (
    TocDiscoveryError,
    TocDiscoveryResult,
    discover_embedded_toc,
    discover_text_heading_toc,
)
from book_translator.toc_writer import write_toc
from book_translator.translation_auto_repair import AutoRepairError
from book_translator.translation_comparison import (
    TranslationComparisonError,
    execute_translation_comparison,
    plan_translation_comparison,
)
from book_translator.translation_historical_reevaluation import (
    HistoricalReevaluationError,
    reevaluate_historical_output,
)
from book_translator.translation_provider_recovery import (
    ProviderRecoveryError,
)
from book_translator.translation_queue import (
    QueueError,
    claim_job,
    create_run,
    list_jobs,
    list_runs,
    load_run_summary,
    plan_repair_target_restoration,
    plan_run,
    recover_run,
    release_job,
    resolve_run_book,
    restore_repair_target,
    retry_job,
)
from book_translator.translation_queue.policy import attempt_budget_exhausted
from book_translator.translation_queue.workflow import PlannedTranslationRun
from book_translator.translation_reevaluation import (
    TranslationReevaluationError,
    inspect_translation_reevaluations,
    reevaluate_translation_job,
)
from book_translator.translation_repair import (
    TranslationRepairError,
    repair_translation_job,
)
from book_translator.translation_repair_policy import (
    AUTO_REPAIR_POLICY_VERSION,
    DEFAULT_MAX_REPAIR_ATTEMPTS,
)
from book_translator.translation_reuse import (
    TranslationReuseError,
    reuse_accepted_translations,
)
from book_translator.translation_snapshot import (
    SnapshotError,
    create_snapshot,
    execute_resource_freeze,
    list_snapshots,
    plan_resource_freeze,
    plan_snapshot,
    validate_snapshot,
)
from book_translator.translation_snapshot.workflow import TranslationSnapshotPlan
from book_translator.translation_worker import (
    TranslationWorkerError,
    WorkerOptions,
    WorkerProgress,
    generated_worker_id,
    run_translation_worker,
)

app = typer.Typer(help="Manage the local book translation pipeline.", no_args_is_help=True)
console = Console()
error_console = Console(stderr=True)

#: The one shared way to run a source-processing command against a book-scoped workspace.
#: Omitted, every command resolves the legacy `workspace/` root and `config/book.yaml` exactly as
#: before; given, it resolves `workspace/books/<id>/` and `config/books/<id>.yaml` instead.
BookOption = Annotated[
    str | None,
    typer.Option(
        "--book",
        help=(
            "Book workspace ID: use workspace/books/<id>/ and config/books/<id>.yaml instead of "
            "the legacy workspace/ root and config/book.yaml."
        ),
    ),
]


def _project_paths(book: str | None) -> ProjectPaths:
    try:
        return ProjectPaths(Path.cwd().resolve(), book)
    except ValueError as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


@app.callback()
def main() -> None:
    """Manage the local book translation pipeline."""


def _source_anomaly_path() -> Path:
    return Path("config/source-anomalies.yaml")


@app.command("list-source-anomalies")
def list_source_anomalies_command() -> None:
    """List registered source-edition anomalies without changing state."""
    try:
        registry = load_source_anomalies(_source_anomaly_path())
    except SourceAnomalyError as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    for item in registry.anomalies:
        console.print(
            f"{item.anomaly_id} {item.chapter_id} {item.category.value} "
            f"{item.status.value} blocking={'yes' if item.blocking else 'no'}"
        )


@app.command("show-source-anomaly")
def show_source_anomaly_command(anomaly_id: Annotated[str, typer.Argument()]) -> None:
    """Show one registered anomaly and its verification evidence."""
    try:
        registry = load_source_anomalies(_source_anomaly_path())
        item = next(entry for entry in registry.anomalies if entry.anomaly_id == anomaly_id)
    except (SourceAnomalyError, StopIteration) as exc:
        error_console.print(f"[bold red]Error:[/bold red] source anomaly not found: {anomaly_id}")
        raise typer.Exit(code=1) from exc
    for label, value in (
        ("Anomaly", item.anomaly_id),
        ("Chapter", item.chapter_id),
        ("Pages", f"{item.page_from}-{item.page_to}"),
        ("Category", item.category.value),
        ("Status", item.status.value),
        ("Severity", item.severity.value),
        ("Blocking", "yes" if item.blocking else "no"),
        ("Immutable extraction correct", "yes" if not item.evidence.extraction_at_fault else "no"),
        ("Replacement known", "yes" if item.evidence.replacement_known else "no"),
        ("Original SHA-256", item.original_sha256),
        ("Source SHA-256", item.source_file_sha256),
    ):
        console.print(f"[bold]{label}:[/bold] {value}")


@app.command("register-source-anomaly")
def register_source_anomaly_command(
    chapter: Annotated[str, typer.Option("--chapter")],
    page_from: Annotated[int, typer.Option("--page-from")],
    page_to: Annotated[int, typer.Option("--page-to")],
    source_location: Annotated[str, typer.Option("--source-location")],
    category: Annotated[SourceAnomalyCategory, typer.Option("--category")],
    original_text_file: Annotated[Path, typer.Option("--original-text-file")],
    status: Annotated[
        SourceAnomalyStatus, typer.Option("--status")
    ] = SourceAnomalyStatus.SUSPECTED_SOURCE_DEFECT,
    severity: Annotated[
        SourceAnomalySeverity, typer.Option("--severity")
    ] = SourceAnomalySeverity.LOCAL,
    blocking: Annotated[bool, typer.Option("--blocking/--nonblocking")] = False,
    operator_note: Annotated[str, typer.Option("--operator-note")] = "",
    rendered_pdf_checked: Annotated[bool, typer.Option("--rendered-pdf-checked")] = False,
    pdf_text_layer_matches: Annotated[bool, typer.Option("--pdf-text-layer-matches")] = False,
    immutable_source_matches: Annotated[bool, typer.Option("--immutable-source-matches")] = False,
    extraction_at_fault: Annotated[bool, typer.Option("--extraction-at-fault")] = False,
    replacement_known: Annotated[bool, typer.Option("--replacement-known")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Explicitly register a verified source fact; original text is read from UTF-8 file."""
    try:
        config = load_book_config()
        registry = load_source_anomalies(_source_anomaly_path())
        chapter_id = f"{int(chapter):03d}" if chapter.isdigit() else chapter
        source_file = Path(f"workspace/chapters/{chapter_id}/source.md")
        source_bytes = source_file.read_bytes()
        original_text = original_text_file.read_text(encoding="utf-8").removesuffix("\n")
        if original_text.encode("utf-8") not in source_bytes:
            raise SourceAnomalyError("original evidence does not occur in immutable source")
        updated, anomaly = register_anomaly(
            registry,
            book_id=config.book_id,
            chapter_id=chapter_id,
            page_from=page_from,
            page_to=page_to,
            source_location=source_location,
            category=category,
            status=status,
            severity=severity,
            blocking=blocking,
            original_text=original_text,
            source_file=source_file,
            source_file_sha256=sha256_file(source_file),
            evidence=SourceAnomalyEvidence(
                rendered_pdf_checked=rendered_pdf_checked,
                pdf_text_layer_matches=pdf_text_layer_matches,
                immutable_source_matches=immutable_source_matches,
                extraction_at_fault=extraction_at_fault,
                replacement_known=replacement_known,
            ),
            operator_note=operator_note,
            created_at=datetime.now(UTC),
        )
        if not dry_run:
            save_source_anomalies(_source_anomaly_path(), updated)
    except (SourceAnomalyError, ProjectDataError, OSError, UnicodeError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"{'Would register' if dry_run else 'Registered'} source anomaly: {anomaly.anomaly_id}"
    )


@app.command("audit-source-quality")
def audit_source_quality_command(
    chapter: Annotated[str | None, typer.Option("--chapter")] = None,
    confidence: Annotated[str | None, typer.Option("--confidence")] = None,
    category: Annotated[str | None, typer.Option("--category")] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    fail_on: Annotated[str | None, typer.Option("--fail-on")] = None,
    blocking_only: Annotated[bool, typer.Option("--blocking-only")] = False,
    book: BookOption = None,
) -> None:
    """Audit canonical source, normalization, and chunks without translation or mutation."""
    _project_paths(book)
    try:
        confidence_value = AuditConfidence(confidence) if confidence else None
        category_value = AuditCategory(category) if category else None
        fail_value = AuditConfidence(fail_on) if fail_on else None
        selection = audit_source_quality(Path.cwd(), chapter=chapter, book=book)
        persisted = persist_report(selection, output) if output is not None else None
        report = filter_findings(
            selection.report,
            confidence=confidence_value,
            category=category_value,
            blocking_only=blocking_only,
        )
    except (SourceAuditError, OSError, UnicodeError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if as_json:
        console.print_json(serialize_report(report, as_json=True))
    else:
        from collections import Counter

        confidence_counts = Counter(item.confidence.value for item in report.findings)
        severity_counts = Counter(item.severity.value for item in report.findings)
        console.print(f"Chapters scanned: {report.chapters_scanned}")
        console.print(f"Pages inspected: {report.pages_inspected}")
        console.print(f"Blocks inspected: {report.blocks_inspected}")
        console.print(f"Chunks inspected: {report.chunks_inspected}")
        console.print(f"Findings: {len(report.findings)}")
        console.print(f"Blocking findings: {selection.report.blocking_findings}")
        console.print(
            "Unresolved high-confidence findings: "
            f"{selection.report.unresolved_high_confidence_findings}"
        )
        console.print(
            "Resolved high-confidence findings: "
            f"{selection.report.resolved_high_confidence_findings}"
        )
        console.print(
            f"Chapters missing normalization: {selection.report.chapters_missing_normalization}"
        )
        console.print(f"Chapters missing chunks: {selection.report.chapters_missing_chunks}")
        console.print(f"Chapters translation-ready: {selection.report.chapters_translation_ready}")
        console.print(f"High confidence: {confidence_counts['high']}")
        console.print(f"Medium confidence: {confidence_counts['medium']}")
        console.print(f"Low confidence: {confidence_counts['low']}")
        console.print(
            "Severity: "
            + ", ".join(f"{name}={severity_counts[name]}" for name in ("error", "warning", "info"))
        )
        current_group = None
        for finding in report.findings:
            group = (finding.chapter_id, finding.category.value)
            if group != current_group:
                console.print(f"\n[bold]{group[0]} / {group[1]}[/bold]")
                current_group = group
            location = (
                f"pages {finding.page_from}-{finding.page_to}"
                if finding.page_to
                else f"page {finding.page_from}"
                if finding.page_from
                else finding.chunk_id or "chapter"
            )
            console.print(
                f"  {finding.finding_id} [{finding.confidence.value}/{finding.severity.value}] "
                f"{location}: {finding.message} ({finding.source_layer.value}; "
                f"{finding.resolution_status.value}; "
                f"blocking={'yes' if finding.blocking else 'no'})"
                + (f" -> {finding.anomaly_id}" if finding.anomaly_id else "")
            )
        if persisted:
            console.print(f"\nPersisted report: {persisted}")
    if fail_value is not None:
        ranks = {AuditConfidence.LOW: 1, AuditConfidence.MEDIUM: 2, AuditConfidence.HIGH: 3}
        if any(
            item.blocking and ranks[item.confidence] >= ranks[fail_value]
            for item in report.findings
        ):
            raise typer.Exit(code=2)


@app.command("show-source-quality-finding")
def show_source_quality_finding_command(
    finding_id: Annotated[str, typer.Argument()],
    book: BookOption = None,
) -> None:
    """Show concise source and downstream evidence for one current audit finding."""
    try:
        inspection = inspect_finding(Path.cwd(), finding_id, book=book)
    except (SourceAuditError, OSError, UnicodeError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    finding = inspection.finding
    rows = (
        ("Finding", finding.finding_id),
        ("Chapter", finding.chapter_id),
        ("PDF/source pages", f"{finding.page_from or '-'}–{finding.page_to or '-'}"),
        ("Category", finding.category.value),
        ("Confidence", finding.confidence.value),
        ("Severity", finding.severity.value),
        ("Resolution", finding.resolution_status.value),
        ("Blocking", "yes" if finding.blocking else "no"),
        ("Likely owner", finding.source_layer.value),
        ("Source left block", inspection.source_left or "not applicable"),
        ("Source right block", inspection.source_right or "not applicable"),
        ("Normalized", inspection.normalized_representation),
        ("Chunks", inspection.chunk_representation),
        ("Suggested action", finding.suggested_action),
    )
    for label, value in rows:
        console.print(f"[bold]{label}:[/bold] {value}")


def _terminology_path() -> Path:
    return Path("config/terminology.yaml")


def _load_remediation_report(
    *,
    review_run: str | None = None,
) -> TerminologyRemediationReport:
    registry = load_registry(_terminology_path())
    glossary = yaml.safe_load(Path("config/glossary.yaml").read_text(encoding="utf-8"))
    return audit_registry_remediation(
        registry,
        glossary,
        review_run_id=review_run,
        project_root=Path.cwd(),
    )


def _filtered_remediation_report(
    report: TerminologyRemediationReport,
    *,
    category: str | None,
    decision: str | None,
    blocking_only: bool,
    family: str | None = None,
    disposition: str | None = None,
) -> TerminologyRemediationReport:
    findings = list(report.findings)
    if category is not None:
        selected = TerminologyRemediationCategory(category)
        findings = [item for item in findings if item.remediation_category is selected]
    if family is not None:
        chosen = TerminologyFindingFamily(family)
        findings = [item for item in findings if item.finding_family is chosen]
    if disposition is not None:
        judged = TerminologyExactnessDisposition(disposition)
        findings = [item for item in findings if item.exactness_disposition is judged]
    if decision is not None:
        findings = [
            item
            for item in findings
            if item.decision_id == decision or item.candidate_id == decision
        ]
    if blocking_only:
        findings = [item for item in findings if item.blocking]
    from collections import Counter

    exactness = [
        item
        for item in findings
        if item.finding_family is TerminologyFindingFamily.EXACT_ENFORCEMENT
    ]
    registry_finding_count = len(findings) - len(exactness)
    validator_counter: Counter[str] = Counter()
    remediation_counter: Counter[str] = Counter()
    for finding in findings:
        for item in finding.validator_categories:
            validator_counter[item] += 1
        remediation_counter[finding.remediation_category.value] += 1
    valid_categories = {
        TerminologyRemediationCategory.VALID_COMPOUND_OR_MORPHOLOGY,
        TerminologyRemediationCategory.EXACT_ENFORCEMENT_UPHELD,
    }
    review_categories = {
        TerminologyRemediationCategory.REQUIRES_CONTEXT_REVIEW,
        TerminologyRemediationCategory.EXACT_TARGET_REQUIRES_REVIEW,
        TerminologyRemediationCategory.OWNER_GOVERNED_DECISION,
    }
    filtered = report.model_copy(
        update={
            "findings": findings,
            "findings_total": len(findings) + (1 if report.bookkeeping_mismatch is not None else 0),
            "affected_decisions": len({item.decision_id for item in findings}),
            "registry_finding_count": registry_finding_count,
            "exactness_finding_count": len(exactness),
            "exactness_disposition_counts": dict(
                sorted(
                    Counter(
                        item.exactness_disposition.value
                        for item in exactness
                        if item.exactness_disposition is not None
                    ).items()
                )
            ),
            "exactness_rule_counts": dict(
                sorted(
                    Counter(
                        item.exactness_rule for item in exactness if item.exactness_rule is not None
                    ).items()
                )
            ),
            "owner_governed_count": sum(
                1
                for item in exactness
                if item.remediation_category
                is TerminologyRemediationCategory.OWNER_GOVERNED_DECISION
            ),
            "validator_category_counts": dict(sorted(validator_counter.items())),
            "remediation_category_counts": dict(sorted(remediation_counter.items())),
            "definitely_valid_count": sum(
                1 for item in findings if item.remediation_category in valid_categories
            ),
            "definitely_invalid_count": sum(1 for item in findings if item.blocking),
            "context_review_count": sum(
                1 for item in findings if item.remediation_category in review_categories
            ),
            "identity_migration_count": (1 if report.bookkeeping_mismatch is not None else 0),
            "validator_false_positive_count": sum(
                1 for item in findings if item.remediation_category in valid_categories
            ),
            "remaining_registry_blockers": (
                sum(1 for item in findings if item.blocking)
                + (1 if report.bookkeeping_mismatch is not None else 0)
            ),
            "coverage": report.coverage.model_copy(
                update={
                    "proposal_ids_unique": len(findings)
                    == len({item.finding_id for item in findings}),
                    "disposition_ids_unique": len(findings)
                    == len({item.decision_id for item in findings}),
                    "affected_decision_ids_unique": len({item.decision_id for item in findings})
                    == len(findings),
                    "proposal_set_equals_disposition_set": True,
                    "missing_decision_ids": [],
                    "unexpected_decision_ids": [],
                }
            ),
        }
    )
    return filtered


def _terminology_provider(
    name: str, model: str | None
) -> FakeTerminologyProvider | CodexCliTerminologyProvider:
    if name == "fake":
        return FakeTerminologyProvider()
    if name == "codex-cli":
        config = load_book_config(Path("config/book.yaml"))
        return CodexCliTerminologyProvider(
            model=model, timeout_seconds=config.provider_timeout_seconds
        )
    raise TerminologyWorkflowError(f"unsupported terminology provider: {name}")


@app.command("discover-terminology")
def discover_terminology_command(
    snapshot: Annotated[str | None, typer.Argument()] = None,
    chapter: Annotated[list[str] | None, typer.Option("--chapter")] = None,
    chunk: Annotated[list[str] | None, typer.Option("--chunk")] = None,
    provider: Annotated[str, typer.Option("--provider")] = "fake",
    model: Annotated[str | None, typer.Option("--model")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    retry_of: Annotated[str | None, typer.Option("--retry-of")] = None,
    current_ready: Annotated[bool, typer.Option("--current-ready")] = False,
) -> None:
    """Discover terminology candidates from frozen source chunks."""
    try:
        if current_ready:
            if snapshot is not None or chapter or chunk:
                raise TerminologyWorkflowError(
                    "--current-ready cannot be combined with a snapshot or selectors"
                )
            plan = plan_ready_corpus_discovery(
                project_root=Path.cwd(), provider=provider, model=model, retry_of=retry_of
            )
        else:
            if snapshot is None:
                raise TerminologyWorkflowError("provide SNAPSHOT or use --current-ready")
            plan = plan_discovery(
                snapshot,
                project_root=Path.cwd(),
                selectors=chapter,
                chunk_selectors=chunk,
                provider=provider,
                model=model,
                retry_of=retry_of,
            )
        result = None
        if not dry_run:
            analysis_provider = _terminology_provider(provider, model)
            result = execute_discovery(plan, analysis_provider)
    except (
        TerminologyWorkflowError,
        TerminologyAnalysisError,
        TerminologyError,
        OSError,
    ) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Run ID: {plan.run_id}")
    console.print(f"Selected chunks: {len(plan.sources)}")
    console.print(f"Action: {'dry-run' if dry_run else plan.action}")
    if result is not None:
        console.print(f"Output: {plan.output_directory}")


@app.command("propose-terminology-decisions")
def propose_terminology_decisions_command(
    discovery_run: Annotated[str, typer.Argument()],
    provider: Annotated[str, typer.Option("--provider")] = "fake",
    model: Annotated[str | None, typer.Option("--model")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    retry_of: Annotated[str | None, typer.Option("--retry-of")] = None,
) -> None:
    """Propose canonical forms for undecided discovered candidates."""
    try:
        plan = plan_proposals(
            discovery_run,
            project_root=Path.cwd(),
            provider=provider,
            model=model,
            retry_of=retry_of,
        )
        result = None
        if not dry_run:
            analysis_provider = _terminology_provider(provider, model)
            result = execute_proposals(plan, analysis_provider)
    except (
        TerminologyWorkflowError,
        TerminologyAnalysisError,
        TerminologyError,
        OSError,
    ) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Run ID: {plan.run_id}")
    console.print(f"Undecided candidates: {len(plan.candidates)}")
    console.print(f"Action: {'dry-run' if dry_run else plan.action}")
    if result is not None:
        console.print(f"Output: {plan.output_directory}")


@app.command("review-terminology-proposals")
def review_terminology_proposals_command(
    proposal_run: Annotated[str, typer.Argument()],
    provider: Annotated[str, typer.Option("--provider")] = "fake",
    model: Annotated[str | None, typer.Option("--model")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    retry_of: Annotated[str | None, typer.Option("--retry-of")] = None,
) -> None:
    """Independently review AI terminology proposals for canonical quality."""
    try:
        plan = plan_reviews(
            proposal_run,
            project_root=Path.cwd(),
            provider=provider,
            model=model,
            retry_of=retry_of,
        )
        result = None
        if not dry_run:
            result = execute_reviews(
                plan, _terminology_provider(provider, model), progress=console.print
            )
    except (
        TerminologyWorkflowError,
        TerminologyAnalysisError,
        TerminologyError,
        OSError,
    ) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Run ID: {plan.run_id}")
    console.print(f"Reviewable proposals: {len(plan.proposals)}")
    console.print(f"Action: {'dry-run' if dry_run else plan.action}")
    if result is not None:
        console.print(f"Output: {plan.output_directory}")


@app.command("list-terminology-review-shards")
def list_terminology_review_shards_command(
    review_run: Annotated[str, typer.Argument()],
) -> None:
    """Show immutable shard progress without printing provider responses."""
    root = Path("workspace/terminology-runs")
    staging = root / f".{review_run}.review"
    try:
        manifest = json.loads((staging / "plan.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] invalid review plan: {exc}")
        raise typer.Exit(code=1) from exc
    for shard in manifest["shards"]:
        shard_id = shard["shard_id"]
        valid = (staging / f"{shard_id}.validated.yaml").is_file()
        attempts = len(list(staging.glob(f"{shard_id}.attempt-*.yaml")))
        console.print(
            f"{shard_id}  risk={shard['risk_class']}  "
            f"candidates={len(shard['proposal_ids'])}  "
            f"tokens~={shard['estimated_tokens']}  attempts={attempts}  "
            f"status={'valid' if valid else 'pending'}"
        )


@app.command("retry-terminology-review-shard")
def retry_terminology_review_shard_command(
    review_run: Annotated[str, typer.Argument()],
    shard_id: Annotated[str, typer.Argument()],
) -> None:
    """Retry one failed shard while preserving all validated shard evidence."""
    root = Path.cwd()
    staging = root / "workspace/terminology-runs" / f".{review_run}.review"
    try:
        manifest = json.loads((staging / "plan.json").read_text(encoding="utf-8"))
        plan = plan_reviews(
            manifest["proposal_run_id"],
            project_root=root,
            provider=manifest["provider"],
            provider_adapter_version=manifest["provider_adapter_version"],
            model=manifest["model"],
        )
        if plan.run_id != review_run:
            raise TerminologyWorkflowError("review lineage no longer reproduces parent identity")
        result = execute_reviews(
            plan,
            _terminology_provider(plan.provider, plan.model),
            only_shard_id=shard_id,
            progress=console.print,
        )
    except (
        KeyError,
        OSError,
        json.JSONDecodeError,
        TerminologyWorkflowError,
        TerminologyAnalysisError,
        TerminologyError,
    ) as exc:
        if (
            isinstance(exc, TerminologyWorkflowError)
            and str(exc).startswith("review remains incomplete")
            and (staging / f"{shard_id}.validated.yaml").is_file()
        ):
            console.print(f"Run ID: {review_run}")
            console.print(f"Shard: {shard_id}")
            console.print("Status: valid; aggregate remains incomplete")
            return
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Run ID: {review_run}")
    console.print(f"Status: {result.status}")


@app.command("apply-terminology-proposals")
def apply_terminology_proposals_command(
    proposal_run: Annotated[str, typer.Argument()],
    policy: Annotated[TerminologyAcceptancePolicy | None, typer.Option("--policy")] = None,
    threshold: Annotated[float | None, typer.Option("--confidence-threshold")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    review_run: Annotated[str | None, typer.Option("--review-run")] = None,
) -> None:
    """Apply proposal policy to the current registry; never changes historical artifacts."""
    try:
        root = Path.cwd()
        run_path = Path(proposal_run)
        if not run_path.is_absolute():
            direct = root / run_path
            run_path = direct if direct.is_dir() else root / "workspace/terminology-runs" / run_path
        proposal_metadata, proposals = load_proposal_output(run_path)
        if proposal_metadata.discovery_run_id is None:
            raise TerminologyWorkflowError("proposal run has no discovery lineage")
        discovery_path = run_path.parent / proposal_metadata.discovery_run_id
        _, candidates = load_discovery_output(discovery_path)
        registry = load_registry(_terminology_path())
        glossary = yaml.safe_load(Path("config/glossary.yaml").read_text(encoding="utf-8"))
        current_canonical = canonical_view(registry, glossary)
        config = load_book_config(Path("config/book.yaml"))
        reviews: tuple[TerminologyReview, ...] = ()
        if review_run is not None:
            review_path = Path(review_run)
            if not review_path.is_absolute():
                direct_review = root / review_path
                review_path = (
                    direct_review
                    if direct_review.is_dir()
                    else root / "workspace/terminology-runs" / review_path
                )
            review_metadata, reviews = load_review_output(review_path)
            if review_metadata.proposal_run_id != proposal_metadata.run_id:
                raise TerminologyWorkflowError("review run does not reference proposal run")
        if config.terminology_review_required and review_run is None:
            raise TerminologyWorkflowError("book configuration requires --review-run")
        selected_policy = policy or TerminologyAcceptancePolicy(
            config.terminology_acceptance_policy
        )
        selected_threshold = (
            threshold if threshold is not None else config.terminology_confidence_threshold
        )
        updated, result = apply_proposals(
            registry,
            candidates,
            proposals,
            policy=selected_policy,
            threshold=selected_threshold,
            canonical_terms=current_canonical,
            reviews=reviews,
            review_required=config.terminology_review_required,
        )
        if not dry_run and updated != registry:
            save_registry(_terminology_path(), updated)
    except (TerminologyWorkflowError, TerminologyError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Policy: {result.policy.value}")
    console.print(f"Considered: {result.considered}")
    console.print(f"Applied: {result.applied}")
    console.print(f"Conflicts: {result.conflicts}")
    console.print(f"Registry written: {'no' if dry_run or updated == registry else 'yes'}")


@app.command("list-terminology-analysis-runs")
def list_terminology_analysis_runs_command() -> None:
    """List immutable terminology discovery and proposal runs without source prose."""
    root = Path("workspace/terminology-runs")
    if not root.is_dir():
        console.print("No terminology analysis runs.")
        return
    for run_file in sorted(root.glob("ta-*/run.yaml")):
        raw = yaml.safe_load(run_file.read_text(encoding="utf-8"))
        console.print(
            f"{raw.get('run_id')} {raw.get('stage')} {raw.get('status')} "
            f"provider={raw.get('provider')}"
        )


@app.command("show-terminology-analysis-run")
def show_terminology_analysis_run_command(
    run_id: Annotated[str, typer.Argument()],
) -> None:
    """Review candidate or proposal records from one immutable analysis run."""
    from rich.table import Table

    path = Path("workspace/terminology-runs") / run_id
    try:
        metadata = yaml.safe_load((path / "run.yaml").read_text(encoding="utf-8"))
        stage = metadata["stage"]
        if metadata.get("status") == "failed":
            report = yaml.safe_load(
                (path / metadata["validation_file"]).read_text(encoding="utf-8")
            )
            console.print(f"Run ID: {run_id}")
            console.print("Status: failed")
            console.print(f"Provider contacted: {'yes' if metadata.get('contacted') else 'no'}")
            console.print(f"Output SHA-256: {metadata.get('output_sha256')}")
            for error in report.get("errors", []):
                console.print(f"Validation error: {error}")
            return
        table = Table(title=f"Terminology {stage}: {run_id}")
        if stage == "discovery":
            _, candidates = load_discovery_output(path)
            for column in ("Source", "Classification", "Confidence", "Occurrences", "Conflicts"):
                table.add_column(column)
            for candidate_item in candidates:
                table.add_row(
                    candidate_item.source_term,
                    candidate_item.classification.value,
                    f"{candidate_item.confidence:.3f}",
                    str(sum(value.count for value in candidate_item.occurrences)),
                    ", ".join(value.value for value in candidate_item.classification_conflicts),
                )
        elif stage == "proposal":
            _, proposals = load_proposal_output(path)
            for column in ("Source", "Proposed target", "Classification", "Confidence"):
                table.add_column(column)
            for proposal_item in proposals:
                table.add_row(
                    proposal_item.source_term,
                    proposal_item.proposed_target,
                    proposal_item.classification.value,
                    f"{proposal_item.confidence:.3f}",
                )
        elif stage == "review":
            _, reviews = load_review_output(path)
            for column in ("Proposal", "Disposition", "Confidence", "Blocked", "Conflicts"):
                table.add_column(column)
            for review in reviews:
                table.add_row(
                    review.proposal_id,
                    review.disposition.value,
                    f"{review.confidence:.3f}",
                    "yes" if review.blocked else "no",
                    ", ".join(review.conflict_codes),
                )
        else:
            raise TerminologyWorkflowError("unknown terminology analysis stage")
    except (OSError, KeyError, TypeError, TerminologyWorkflowError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(table)


@app.command("inspect-terminology-request")
def inspect_terminology_request_command(
    run_id: Annotated[str, typer.Argument()],
) -> None:
    """Inspect retained request/failure evidence without contacting a provider."""
    root = Path("workspace/terminology-runs")
    path = root / run_id
    marker = root / f".{run_id}.requesting.json"
    try:
        marker_data = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else None
        if path.is_dir():
            metadata = yaml.safe_load((path / "run.yaml").read_text(encoding="utf-8"))
            console.print(f"Run ID: {run_id}")
            console.print(f"Status: {metadata.get('status')}")
            console.print(f"Provider contacted: {'yes' if metadata.get('contacted') else 'no'}")
            console.print("Response retained: yes")
            console.print(f"Request SHA-256: {metadata.get('request_sha256')}")
            console.print(f"Output SHA-256: {metadata.get('output_sha256')}")
            if metadata.get("failure_message"):
                console.print(f"Validation error: {metadata['failure_message']}")
            return
        if marker_data is None:
            raise TerminologyWorkflowError("no retained request evidence for run ID")
        console.print(f"Run ID: {run_id}")
        console.print("Status: ambiguous legacy request")
        console.print("Provider contacted: yes")
        console.print("Response retained: no")
        console.print(f"Request SHA-256: {marker_data.get('request_sha256')}")
        console.print("Request marker retained: yes")
    except (OSError, ValueError, TypeError, TerminologyWorkflowError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("audit-terminology-proposal-run")
def audit_terminology_proposal_run_command(
    run_id: Annotated[str, typer.Argument()],
) -> None:
    """Read-only audit of historical proposals against current canonical knowledge."""
    from rich.table import Table

    try:
        rows = audit_proposal_run(
            Path("workspace/terminology-runs") / run_id,
            project_root=Path.cwd(),
        )
    except (OSError, KeyError, TerminologyWorkflowError, TerminologyError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    table = Table(title=f"Terminology proposal audit: {run_id}")
    for column in ("Source", "Action", "Canonical source", "Conflicts", "Auto eligible"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row["source_term"]),
            str(row["action"]),
            str(row["canonical_source"] or ""),
            ", ".join(str(value) for value in row["conflicts"]),
            "yes" if row["auto_eligible"] else "no",
        )
    console.print(table)


@app.command("audit-terminology-review-run")
def audit_terminology_review_run_command(
    run_id: Annotated[str, typer.Argument()],
) -> None:
    """Read-only audit of a review run against current canonical knowledge."""
    from rich.table import Table

    try:
        rows = audit_review_run(
            Path("workspace/terminology-runs") / run_id, project_root=Path.cwd()
        )
    except (OSError, KeyError, TerminologyWorkflowError, TerminologyError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    table = Table(title=f"Terminology review audit: {run_id}")
    for column in ("Source", "Disposition", "Blocked", "Conflicts"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row["source_term"]),
            str(row["disposition"]),
            "yes" if row["blocked"] else "no",
            ", ".join(str(value) for value in row["conflicts"]),
        )
    console.print(table)


@app.command("audit-terminology-exact-enforcement")
def audit_terminology_exact_enforcement_command(
    registry: Annotated[Path | None, typer.Option("--registry")] = None,
    risk: Annotated[str | None, typer.Option("--risk")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 25,
) -> None:
    """Read-only audit of which canonical decisions are safe to enforce as exact target strings."""
    from rich.table import Table

    try:
        selected = ExactnessRisk(risk) if risk is not None else None
    except ValueError as exc:
        error_console.print(f"[bold red]Error:[/bold red] unknown risk: {risk}")
        raise typer.Exit(code=1) from exc
    try:
        report = audit_exact_enforcement(load_registry(registry or _terminology_path()))
    except TerminologyError as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    census = Table(title="Exact enforcement census")
    for column in ("Measure", "Count"):
        census.add_column(column)
    for label, value in (
        ("decisions", report.total_decisions),
        ("exact decisions", report.exact_decisions),
        ("multiword exact decisions", report.multiword_exact_decisions),
        ("locked exact decisions", report.locked_exact_decisions),
        ("suspicious decisions", report.suspicious_decisions),
    ):
        census.add_row(label, str(value))
    for item, count in report.risk_counts:
        census.add_row(f"risk: {item.value}", str(count))
    console.print(census)

    findings = [item for item in report.findings if selected is None or item.risk is selected]
    table = Table(title=f"Unsafe exact candidates ({len(findings)} matching)")
    for column in ("Risk", "Source term", "Class", "Occurrences", "Related", "Rationale"):
        table.add_column(column)
    for finding in findings[:limit]:
        table.add_row(
            finding.risk.value,
            finding.source_term,
            finding.classification.value,
            str(finding.occurrence_count),
            ", ".join(finding.related_source_terms),
            finding.rationale,
        )
    console.print(table)
    if len(findings) > limit:
        console.print(f"[dim]{len(findings) - limit} further findings suppressed by --limit[/dim]")


@app.command("audit-terminology-registry-remediation")
def audit_terminology_registry_remediation_command(
    category: Annotated[str | None, typer.Option("--category")] = None,
    decision: Annotated[str | None, typer.Option("--decision")] = None,
    blocking_only: Annotated[bool, typer.Option("--blocking-only")] = False,
    family: Annotated[str | None, typer.Option("--family")] = None,
    disposition: Annotated[str | None, typer.Option("--disposition")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    review_run: Annotated[str | None, typer.Option("--review-run")] = None,
) -> None:
    """Read-only audit of terminology registry remediation risk and bookkeeping."""
    try:
        report = _filtered_remediation_report(
            _load_remediation_report(review_run=review_run),
            category=category,
            decision=decision,
            blocking_only=blocking_only,
            family=family,
            disposition=disposition,
        )
        rendered = serialize_remediation_report(report)
        if output is not None:
            atomic_write_text(output, rendered, overwrite=output.exists())
    except (TerminologyError, OSError, ValueError, TypeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if as_json:
        sys.stdout.write(rendered)
        return
    from rich.table import Table

    console.print(f"Registry decisions scanned: {report.decisions_scanned}")
    console.print(f"Registry findings: {report.registry_finding_count}")
    console.print(f"Exact-enforcement findings: {report.exactness_finding_count}")
    console.print(
        "Exactness dispositions: "
        + ", ".join(f"{key}={value}" for key, value in report.exactness_disposition_counts.items())
    )
    console.print(
        "Exactness rules: "
        + ", ".join(f"{key}={value}" for key, value in report.exactness_rule_counts.items())
    )
    console.print(f"Owner-governed decisions left untouched: {report.owner_governed_count}")
    if report.exactness_deferred_count:
        console.print(
            f"Exactness findings deferred to the canonical validator: "
            f"{report.exactness_deferred_count} "
            f"({', '.join(report.exactness_deferred_source_terms)})"
        )
    console.print(f"Affected decisions: {report.affected_decisions}")
    console.print(
        "Validator categories: "
        + ", ".join(f"{key}={value}" for key, value in report.validator_category_counts.items())
    )
    console.print(
        "Remediation categories: "
        + ", ".join(f"{key}={value}" for key, value in report.remediation_category_counts.items())
    )
    console.print(f"Definitely valid / no change: {report.definitely_valid_count}")
    console.print(f"Definitely invalid / remediation needed: {report.definitely_invalid_count}")
    console.print(f"Context review: {report.context_review_count}")
    console.print(f"Identity migration: {report.identity_migration_count}")
    console.print(f"Validator false positives eliminated: {report.validator_false_positive_count}")
    console.print(f"Remaining registry blockers: {report.remaining_registry_blockers}")
    console.print(f"Escalations: {report.escalation_count}")
    if report.bookkeeping_mismatch is not None:
        console.print(
            "Normalized candidate mismatch: "
            f"{report.bookkeeping_mismatch.original_candidate_id} -> "
            f"{report.bookkeeping_mismatch.normalized_candidate_id}"
        )
    table = Table(title="Terminology remediation findings")
    for column in (
        "Finding",
        "Family",
        "Source",
        "Target",
        "Category",
        "Action",
        "Disposition",
        "Blocking",
    ):
        table.add_column(column)
    for finding in report.findings:
        table.add_row(
            finding.finding_id,
            finding.finding_family.value,
            finding.source_term,
            finding.target_term,
            finding.remediation_category.value,
            finding.proposed_action.value,
            finding.exactness_disposition.value if finding.exactness_disposition else "-",
            "yes" if finding.blocking else "no",
        )
    console.print(table)
    if report.escalations:
        escalation_table = Table(title="Terminology escalations")
        for column in ("Source", "Disposition", "Recommended Persian", "Confidence", "Reason"):
            escalation_table.add_column(column)
        for item in report.escalations:
            escalation_table.add_row(
                item.source_term,
                item.disposition.value,
                item.recommended_persian or "",
                f"{item.confidence:.2f}",
                item.reason,
            )
        console.print(escalation_table)


@app.command("show-terminology-remediation-finding")
def show_terminology_remediation_finding_command(
    finding_id: Annotated[str, typer.Argument()],
    review_run: Annotated[str | None, typer.Option("--review-run")] = None,
) -> None:
    """Show one remediation finding or escalation packet entry."""
    try:
        report = _load_remediation_report(review_run=review_run)
        report = _filtered_remediation_report(
            report,
            category=None,
            decision=None,
            blocking_only=False,
        )
        finding = next((item for item in report.findings if item.finding_id == finding_id), None)
        if finding is not None:
            for label, value in (
                ("Finding", finding.finding_id),
                ("Family", finding.finding_family.value),
                ("Decision", finding.decision_id),
                ("Candidate", finding.candidate_id),
                ("Source", finding.source_term),
                ("Target", finding.target_term),
                ("Category", finding.remediation_category.value),
                ("Validator categories", ", ".join(finding.validator_categories)),
                ("Exactness risks", ", ".join(finding.exactness_risks) or "-"),
                (
                    "Exactness disposition",
                    finding.exactness_disposition.value if finding.exactness_disposition else "-",
                ),
                ("Exactness rule", finding.exactness_rule or "-"),
                ("Occurrences", str(finding.occurrence_count)),
                (
                    "Proposed enforcement",
                    finding.proposed_enforcement.value if finding.proposed_enforcement else "-",
                ),
                ("Question", finding.question or "-"),
                ("Related decisions", ", ".join(finding.related_decision_ids) or "-"),
                ("Related sources", ", ".join(finding.related_source_terms) or "-"),
                ("Related targets", ", ".join(finding.related_target_terms) or "-"),
                ("Rule", finding.triggered_rule),
                ("Morphology involved", "yes" if finding.morphology_involved else "no"),
                ("Blocking", "yes" if finding.blocking else "no"),
                ("Action", finding.proposed_action.value),
            ):
                console.print(f"[bold]{label}:[/bold] {value}")
            return
        if (
            report.bookkeeping_mismatch is not None
            and report.bookkeeping_mismatch.finding_id == finding_id
        ):
            item = report.bookkeeping_mismatch
            for label, value in (
                ("Finding", item.finding_id),
                ("Original candidate", item.original_candidate_id),
                ("Original source", item.original_source_term),
                ("Normalized candidate", item.normalized_candidate_id),
                ("Normalized source", item.normalized_source_term),
                (
                    "Both candidate records exist",
                    "yes" if item.both_candidate_records_exist else "no",
                ),
                (
                    "Immutable references resolve",
                    "yes" if item.immutable_references_resolve else "no",
                ),
                ("Migration strategy", item.safest_migration_strategy),
            ):
                console.print(f"[bold]{label}:[/bold] {value}")
            return
        escalation = next(
            (item for item in report.escalations if item.escalation_id == finding_id),
            None,
        )
        if escalation is not None:
            for label, value in (
                ("Escalation", escalation.escalation_id),
                ("Proposal", escalation.proposal_id),
                ("Candidate", escalation.candidate_id),
                ("Source", escalation.source_term),
                ("Type", escalation.terminology_type),
                ("Disposition", escalation.disposition.value),
                ("Recommended Persian", escalation.recommended_persian or "-"),
                ("Confidence", f"{escalation.confidence:.2f}"),
                ("Reason", escalation.reason),
            ):
                console.print(f"[bold]{label}:[/bold] {value}")
            return
        raise ValueError(f"remediation finding not found: {finding_id}")
    except (TerminologyError, OSError, ValueError, TypeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


def _glossary_path() -> Path:
    return Path("config/glossary.yaml")


def _build_remediation_plan(review_run: str | None) -> TerminologyRemediationPlan:
    registry_path = _terminology_path()
    glossary_path = _glossary_path()
    registry = load_registry(registry_path)
    glossary = yaml.safe_load(glossary_path.read_text(encoding="utf-8"))
    report = audit_registry_remediation(
        registry, glossary, review_run_id=review_run, project_root=Path.cwd()
    )
    return plan_registry_remediation(
        registry,
        glossary,
        report,
        registry_raw_sha256=sha256_file(registry_path),
        glossary_sha256=sha256_file(glossary_path),
        registry_file=registry_path.as_posix(),
        review_run_id=review_run,
    )


def _load_remediation_plan(plan_id: str, plan_file: Path | None) -> TerminologyRemediationPlan:
    path = plan_file or (
        ProjectPaths(Path.cwd()).terminology_remediation_plan(plan_id) / "plan.json"
    )
    plan = TerminologyRemediationPlan.model_validate_json(path.read_text(encoding="utf-8"))
    if plan.plan_id != plan_id:
        raise ValueError(f"plan file does not contain plan {plan_id}")
    return plan


@app.command("plan-terminology-remediation")
def plan_terminology_remediation_command(
    review_run: Annotated[str | None, typer.Option("--review-run")] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Build the deterministic remediation plan implied by the current registry audit."""
    try:
        plan = _build_remediation_plan(review_run)
        rendered = serialize_plan(plan)
        if output is not None:
            destination = output / "plan.json" if output.suffix == "" else output
            atomic_write_text(destination, rendered, overwrite=destination.exists())
    except (TerminologyError, OSError, ValueError, TypeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if as_json:
        sys.stdout.write(rendered)
        return
    console.print(f"Plan: {plan.plan_id}")
    console.print(f"Audit: {plan.audit_id}")
    console.print(f"Source registry semantic SHA-256: {plan.source_registry_semantic_sha256}")
    console.print(f"Source registry raw SHA-256: {plan.source_registry_raw_sha256}")
    console.print(f"Operations: {plan.operation_count}")
    console.print(f"Deterministic: {plan.deterministic_operation_count}")
    console.print(f"Unresolved: {plan.unresolved_operation_count}")
    console.print(f"Registry-mutating: {plan.mutating_operation_count}")
    console.print(
        "Actions: " + ", ".join(f"{key}={value}" for key, value in plan.action_counts.items())
    )
    console.print(f"Applicable: {'yes' if plan.applicable else 'no'}")


@app.command("show-terminology-remediation-plan")
def show_terminology_remediation_plan_command(
    plan_id: Annotated[str, typer.Argument()],
    plan_file: Annotated[Path | None, typer.Option("--plan-file")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one stored remediation plan and its operation summary."""
    from rich.table import Table

    try:
        plan = _load_remediation_plan(plan_id, plan_file)
    except (TerminologyError, OSError, ValueError, TypeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if as_json:
        sys.stdout.write(serialize_plan(plan))
        return
    for label, value in (
        ("Plan", plan.plan_id),
        ("Engine", plan.engine_version),
        ("Audit", plan.audit_id),
        ("Review run", plan.review_run_id or "-"),
        ("Registry version", f"{plan.registry_version_before} -> {plan.registry_version_after}"),
        ("Semantic SHA-256", plan.source_registry_semantic_sha256),
        ("Expected semantic SHA-256", plan.expected_semantic_sha256 or "-"),
        ("Applicable", "yes" if plan.applicable else "no"),
    ):
        console.print(f"[bold]{label}:[/bold] {value}")
    table = Table(title="Remediation operations")
    for column in ("Seq", "Operation", "Action", "Resolution", "Source", "Mutates"):
        table.add_column(column)
    for operation in plan.operations:
        table.add_row(
            str(operation.sequence),
            operation.operation_id,
            operation.action.value,
            operation.resolution.value,
            operation.source_term,
            "yes" if operation.mutates_registry else "no",
        )
    console.print(table)


@app.command("show-terminology-remediation-operation")
def show_terminology_remediation_operation_command(
    plan_id: Annotated[str, typer.Argument()],
    operation_id: Annotated[str, typer.Argument()],
    plan_file: Annotated[Path | None, typer.Option("--plan-file")] = None,
) -> None:
    """Show one remediation operation with its provenance and blocking evidence."""
    try:
        plan = _load_remediation_plan(plan_id, plan_file)
        operation = next(
            (item for item in plan.operations if item.operation_id == operation_id), None
        )
        if operation is None:
            raise ValueError(f"remediation operation not found: {operation_id}")
    except (TerminologyError, OSError, ValueError, TypeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    for label, value in (
        ("Operation", operation.operation_id),
        ("Finding", operation.finding_id),
        ("Family", operation.finding_family.value),
        ("Category", operation.remediation_category.value),
        ("Action", operation.action.value),
        ("Resolution", operation.resolution.value),
        (
            "Exactness disposition",
            operation.exactness_disposition.value if operation.exactness_disposition else "-",
        ),
        ("Exactness rule", operation.exactness_rule or "-"),
        ("Exactness risks", ", ".join(operation.exactness_risks) or "-"),
        ("Source", operation.source_term),
        ("Target", operation.target_term or "-"),
        ("Decision", operation.decision_id or "-"),
        ("Mutates registry", "yes" if operation.mutates_registry else "no"),
        ("Before-state SHA-256", operation.before_state_sha256),
        ("Reason", operation.reason),
        ("Blocked reason", operation.blocked_reason or "-"),
    ):
        console.print(f"[bold]{label}:[/bold] {value}")
    for removal in operation.variant_removals:
        console.print(
            f"  remove variant {removal.removed_variant!r} from {removal.holder_source_term!r} "
            f"(canonical target of {removal.canonical_owner_source_term!r})"
        )
    if operation.migration is not None:
        console.print(
            f"  migrate {operation.migration.original_candidate_id} -> "
            f"{operation.migration.normalized_candidate_id}"
        )
    if (retirement := operation.retirement) is not None:
        console.print(
            f"  retire decision {retirement.decision_id} for {retirement.source_term!r} "
            f"(target {retirement.retired_target_term!r}, {retirement.occurrence_count} "
            f"occurrences, enforced {retirement.resolved_enforcement.value})"
        )
        console.print(
            f"  candidate {retirement.candidate_status_before} -> "
            f"{retirement.candidate_status_after}"
            + (
                f"; aliases withdrawn: {', '.join(retirement.retired_aliases)}"
                if retirement.retired_aliases
                else ""
            )
        )
        console.print(f"  decision record SHA-256: {retirement.decision_record_sha256}")
    if (correction := operation.target_correction) is not None:
        console.print(
            f"  respell {correction.source_term!r}: {correction.target_before!r} -> "
            f"{correction.target_after!r}"
        )
        console.print(
            f"  replaced token {correction.replaced_token!r} with the canonical target of "
            f"{correction.component_source_term!r} ({correction.component_target_term!r})"
        )
        console.print(f"  decision record SHA-256: {correction.decision_record_sha256}")
    if (change := operation.enforcement_change) is not None:
        console.print(
            f"  enforcement {change.resolved_enforcement_before.value} -> "
            f"{change.enforcement_after.value} for {change.source_term!r}; target unchanged"
        )
        console.print(f"  decision record SHA-256: {change.decision_record_sha256}")
    if (request := operation.question_request) is not None:
        console.print(
            f"  open question {request.question_id} ({request.category.value}) for "
            f"{request.source_term!r}"
        )
        console.print(f"  question: {request.question}")
    for item in operation.blocking_evidence:
        console.print(f"  evidence: {item}")


@app.command("apply-terminology-remediation")
def apply_terminology_remediation_command(
    plan_id: Annotated[str, typer.Argument()],
    plan_file: Annotated[Path | None, typer.Option("--plan-file")] = None,
    review_run: Annotated[str | None, typer.Option("--review-run")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Simulate or explicitly apply a remediation plan against the terminology registry."""
    try:
        plan = _load_remediation_plan(plan_id, plan_file)
        glossary = yaml.safe_load(_glossary_path().read_text(encoding="utf-8"))
        simulation = simulate_remediation_plan(
            _terminology_path(),
            glossary,
            plan,
            review_run_id=review_run,
            project_root=Path.cwd(),
        )
        rendered = serialize_simulation(simulation)
        if output is not None:
            destination = output / "dry-run.json" if output.suffix == "" else output
            atomic_write_text(destination, rendered, overwrite=destination.exists())
        if not dry_run:
            if not simulation.safe_to_apply:
                raise TerminologyRemediationPlanError(
                    "the dry run did not prove this plan safe; refusing to apply it"
                )
            execute_remediation_plan(
                _terminology_path(),
                plan,
                simulation,
                evidence_directory=ProjectPaths(Path.cwd()).terminology_remediation_plan(
                    plan.plan_id
                ),
            )
    except (TerminologyError, OSError, ValueError, TypeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if as_json:
        sys.stdout.write(rendered)
        return
    console.print(f"Plan: {simulation.plan_id} ({'dry run' if dry_run else 'applied'})")
    console.print(f"Decisions: {simulation.decisions_before} -> {simulation.decisions_after}")
    console.print(f"Candidates: {simulation.candidates_before} -> {simulation.candidates_after}")
    console.print(
        f"Retired decisions: {simulation.decisions_retired} "
        f"(candidates rejected: {simulation.candidates_rejected})"
    )
    console.print(f"Target corrections: {simulation.target_corrections_applied}")
    console.print(f"Enforcement relaxations: {simulation.enforcement_changes_applied}")
    console.print(f"Terminology questions requested: {simulation.question_requests_recorded}")
    console.print(
        f"Exact-enforcement findings: {simulation.exact_enforcement_findings_before} -> "
        f"{simulation.exact_enforcement_findings_after}"
    )
    console.print(
        f"Blocking findings: {simulation.blocking_findings_before} -> "
        f"{simulation.blocking_findings_after}"
    )
    if simulation.accounted_blocking_source_terms:
        console.print(
            "Blocking findings referred to a human by this plan: "
            + ", ".join(simulation.accounted_blocking_source_terms)
        )
    console.print(f"Simulated semantic SHA-256: {simulation.simulated_registry_semantic_sha256}")
    console.print(f"Lookup regressions: {simulation.lookup_regression_count}")
    console.print(
        f"Real registry unchanged: {'yes' if simulation.real_registry_unchanged else 'no'}"
    )
    console.print(f"Safe to apply: {'yes' if simulation.safe_to_apply else 'no'}")
    for blocker in simulation.blockers:
        console.print(f"  blocker: {blocker}")


@app.command("build-terminology-conflict-graph")
def build_terminology_conflict_graph_command(
    plan_id: Annotated[str, typer.Argument()],
    plan_file: Annotated[Path | None, typer.Option("--plan-file")] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Group the unresolved remediation operations into terminology conflict families."""
    try:
        plan = _load_remediation_plan(plan_id, plan_file)
        registry = load_registry(_terminology_path())
        graph = build_conflict_graph(registry, plan)
        rendered = serialize_graph(graph)
        if output is not None:
            atomic_write_text(output, rendered, overwrite=output.exists())
    except (TerminologyError, OSError, ValueError, TypeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if as_json:
        sys.stdout.write(rendered)
        return
    console.print(f"Graph: {graph.graph_id}")
    console.print(f"Unresolved operations: {graph.unresolved_operation_count}")
    console.print(f"Conflict components: {graph.component_count}")
    console.print(
        "Component sizes: "
        + ", ".join(f"{key}={value}" for key, value in graph.component_size_histogram.items())
    )
    for component in graph.components[:5]:
        console.print(f"  {component.component_id} size={component.size}")


@app.command("show-terminology-conflict-component")
def show_terminology_conflict_component_command(
    component_id: Annotated[str, typer.Argument()],
    graph_file: Annotated[Path, typer.Option("--graph-file")],
) -> None:
    """Show one conflict family and the records it holds together."""
    try:
        graph = ConflictGraph.model_validate_json(graph_file.read_text(encoding="utf-8"))
        component = next(
            (item for item in graph.components if item.component_id == component_id), None
        )
        if component is None:
            raise ValueError(f"conflict component not found: {component_id}")
    except (TerminologyError, OSError, ValueError, TypeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    node_by_id = {item.node_id: item for item in graph.nodes}
    console.print(f"[bold]Component:[/bold] {component.component_id} (size {component.size})")
    console.print(
        "[bold]Relations:[/bold] " + ", ".join(item.value for item in component.relations)
    )
    for node_id in component.node_ids:
        node = node_by_id[node_id]
        console.print(
            f"  {node.source_term!r} -> {node.target_term!r} "
            f"[{node.candidate_status}] aliases={node.aliases}"
        )


@app.command("show-terminology-reconciliation-run")
def show_terminology_reconciliation_run_command(
    run_file: Annotated[Path, typer.Option("--run-file")],
    component: Annotated[str | None, typer.Option("--component")] = None,
) -> None:
    """Show a semantic reconciliation run and its structured resolutions."""
    try:
        run = ReconciliationRun.model_validate_json(run_file.read_text(encoding="utf-8"))
    except (TerminologyError, OSError, ValueError, TypeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[bold]Run:[/bold] {run.run_id}")
    console.print(f"[bold]Reviewer:[/bold] {run.reviewer} ({run.reviewer_kind})")
    console.print(
        f"[bold]External service contacted:[/bold] "
        f"{'yes' if run.external_service_contacted else 'no'}"
    )
    console.print(
        f"[bold]Shards:[/bold] {run.shard_count} | calls {run.provider_call_count} | "
        f"retries {run.retry_count}"
    )
    console.print(
        "[bold]Classifications:[/bold] "
        + ", ".join(f"{key}={value}" for key, value in run.classification_counts.items())
    )
    console.print(f"[bold]Escalations:[/bold] {run.escalation_count}")
    for resolution in run.resolutions:
        if component is not None and resolution.component_id != component:
            continue
        console.print(
            f"  {resolution.component_id} {resolution.classification.value} "
            f"confidence={resolution.confidence:.2f} edits={len(resolution.edits)}"
        )
        if component is not None:
            console.print(f"    {resolution.reasoning_summary}")


@app.command("show-terminology-reconciliation-provenance")
def show_terminology_reconciliation_provenance_command(
    run_file: Annotated[Path, typer.Option("--run-file")],
    correction_file: Annotated[Path | None, typer.Option("--correction-file")] = None,
) -> None:
    """Show a reconciliation run's provenance and any superseding correction."""
    try:
        run = ReconciliationRun.model_validate_json(run_file.read_text(encoding="utf-8"))
        correction = (
            ReconciliationProvenanceCorrection.model_validate_json(
                correction_file.read_text(encoding="utf-8")
            )
            if correction_file is not None
            else None
        )
        if correction is not None and correction.supersedes_run_id != run.run_id:
            raise ValueError("the correction does not supersede this run")
    except (TerminologyError, OSError, ValueError, TypeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[bold]Run:[/bold] {run.run_id}")
    console.print(
        f"[bold]Recorded:[/bold] {run.reviewer} ({run.reviewer_kind}); external service "
        f"{'yes' if run.external_service_contacted else 'no'}"
    )
    if correction is None:
        console.print("[bold]Correction:[/bold] none")
        return
    provenance = correction.corrected_provenance
    for label, value in (
        ("Correction", correction.correction_id),
        ("Reviewer", provenance.reviewer),
        ("Reviewer kind", provenance.reviewer_kind),
        ("Inference locality", provenance.inference_locality.value),
        ("Inference provider", provenance.inference_provider or "-"),
        ("Account type", provenance.account_type or "-"),
        ("Local orchestration", "yes" if provenance.local_orchestration else "no"),
        ("External service contacted", "yes" if provenance.external_service_contacted else "no"),
        ("Prior model knowledge used", "yes" if provenance.prior_model_knowledge_used else "no"),
        ("Disclosed", ", ".join(item.value for item in provenance.disclosed_material)),
        ("Withheld", ", ".join(item.value for item in provenance.withheld_material)),
        ("Resolutions unchanged", "yes" if correction.resolutions_unchanged else "no"),
        ("Resolution set SHA-256", correction.resolution_set_sha256),
        ("Reason", correction.reason),
    ):
        console.print(f"[bold]{label}:[/bold] {value}")


@app.command("list-terminology")
def list_terminology_command() -> None:
    """List terminology candidates and canonical decision metadata without story prose."""
    from rich.table import Table

    try:
        registry = load_registry(_terminology_path())
    except TerminologyError as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    decisions = {item.candidate_id: item for item in registry.decisions}
    table = Table(title="Terminology Registry")
    for column in ("Source", "Type", "Status", "Target", "Provenance", "Locked", "Context"):
        table.add_column(column)
    for candidate in sorted(registry.candidates, key=lambda item: item.source_term.casefold()):
        decision = decisions.get(candidate.candidate_id)
        table.add_row(
            candidate.source_term,
            candidate.classification.value,
            candidate.status.value,
            decision.target_term if decision else "",
            decision.decision_source.value if decision else candidate.provenance.value,
            "yes" if decision and decision.locked else "no",
            "context-sensitive" if candidate.context_sensitive else "global",
        )
    console.print(table)
    console.print(f"Registry SHA-256: {registry_identity(registry)}")


@app.command("add-terminology-candidate")
def add_terminology_candidate_command(
    source: Annotated[str, typer.Argument()],
    classification: Annotated[TerminologyType, typer.Option("--type")],
    confidence: Annotated[float, typer.Option("--confidence")],
    provenance: Annotated[DecisionSource, typer.Option("--provenance")] = DecisionSource.AI,
    alias: Annotated[list[str] | None, typer.Option("--alias")] = None,
    context_sensitive: Annotated[bool, typer.Option("--context-sensitive")] = False,
    chapter_id: Annotated[str | None, typer.Option("--chapter-id")] = None,
    chunk_id: Annotated[str | None, typer.Option("--chunk-id")] = None,
    location: Annotated[Literal["source", "context"], typer.Option("--location")] = "source",
    occurrence_count: Annotated[int, typer.Option("--occurrence-count")] = 1,
) -> None:
    """Persist a discovery-stage candidate without creating a canonical decision."""
    try:
        registry = load_registry(_terminology_path())
        if chunk_id is not None and chapter_id is None:
            raise TerminologyError("--chunk-id requires --chapter-id")
        occurrence = (
            TerminologyOccurrence(
                chapter_id=chapter_id,
                chunk_id=chunk_id,
                location=location,
                count=occurrence_count,
            )
            if chapter_id is not None
            else None
        )
        updated = add_candidate(
            registry,
            source_term=source,
            classification=classification,
            confidence=confidence,
            provenance=provenance,
            aliases=tuple(alias or ()),
            context_sensitive=context_sensitive,
            occurrences=(occurrence,) if occurrence is not None else (),
        )
        save_registry(_terminology_path(), updated)
    except (TerminologyError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Terminology candidate saved: {source}")


@app.command("set-terminology")
def set_terminology_command(
    source: Annotated[str, typer.Argument()],
    target: Annotated[str, typer.Argument()],
    classification: Annotated[TerminologyType, typer.Option("--type")],
    decision_source: Annotated[
        DecisionSource, typer.Option("--decision-source")
    ] = DecisionSource.USER,
    locked: Annotated[bool, typer.Option("--locked/--unlocked")] = True,
    decision_state: Annotated[DecisionState, typer.Option("--state")] = DecisionState.CONFIRMED,
    alias: Annotated[list[str] | None, typer.Option("--alias")] = None,
    competing_variant: Annotated[list[str] | None, typer.Option("--competing-variant")] = None,
    context_sensitive: Annotated[bool, typer.Option("--context-sensitive")] = False,
    enforcement: Annotated[TerminologyEnforcement | None, typer.Option("--enforcement")] = None,
    target_form: Annotated[
        list[str] | None,
        typer.Option(
            "--target-form",
            help="Explicitly authorize a further target realization (repeatable).",
        ),
    ] = None,
) -> None:
    """Create or update a canonical terminology decision atomically."""
    try:
        registry = load_registry(_terminology_path())
        updated = set_decision(
            registry,
            source_term=source,
            target_term=target,
            classification=classification,
            decision_source=decision_source,
            locked=locked,
            decision_state=decision_state,
            aliases=tuple(alias or ()),
            competing_variants=tuple(competing_variant or ()),
            context_sensitive=context_sensitive,
            enforcement=enforcement,
            allowed_target_forms=tuple(target_form or ()),
        )
        save_registry(_terminology_path(), updated)
    except (TerminologyError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Canonical terminology decision saved: {source} -> {target}")
    console.print(f"Registry SHA-256: {registry_identity(updated)}")


@app.command("reject-terminology-candidate")
def reject_terminology_candidate_command(
    source: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option("--reason")],
) -> None:
    """Record that a candidate is deliberately not terminology."""
    try:
        registry = load_registry(_terminology_path())
        updated = reject_candidate(registry, source_term=source, reason=reason)
        save_registry(_terminology_path(), updated)
    except (TerminologyError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Candidate rejected as not terminology: {source}")
    console.print(f"Registry SHA-256: {registry_identity(updated)}")


@app.command("open-terminology-question")
def open_terminology_question_command(
    source: Annotated[str, typer.Argument()],
    category: Annotated[QuestionCategory, typer.Option("--category")],
    question: Annotated[str, typer.Option("--question")],
    provisional_target: Annotated[str | None, typer.Option("--provisional-target")] = None,
    blocking: Annotated[bool, typer.Option("--blocking/--non-blocking")] = False,
    evidence_status: Annotated[
        EvidenceStatus, typer.Option("--evidence-status")
    ] = EvidenceStatus.NOT_GATHERED,
    current_book: Annotated[
        ResearchState, typer.Option("--current-book")
    ] = ResearchState.NOT_CHECKED,
    web: Annotated[ResearchState, typer.Option("--web")] = ResearchState.NOT_CHECKED,
    other_volumes: Annotated[
        ResearchState, typer.Option("--other-volumes")
    ] = ResearchState.NOT_CHECKED,
    note: Annotated[list[str] | None, typer.Option("--note")] = None,
) -> None:
    """Record or update a durable open terminology question."""
    root = Path.cwd()
    try:
        registry = load_registry(_terminology_path())
        candidate_id = candidate_identity(source, registry.source_language)
        candidate = next(
            (item for item in registry.candidates if item.candidate_id == candidate_id), None
        )
        if candidate is None:
            raise TerminologyError(f"no terminology candidate exists for {source!r}")
        decision = next(
            (item for item in registry.decisions if item.candidate_id == candidate_id), None
        )
        questions = load_or_empty_questions(root)
        updated = upsert_question(
            questions,
            candidate_id=candidate_id,
            source_term=source,
            category=category,
            question=question,
            created_at=datetime.now(UTC),
            decision_id=decision.decision_id if decision else None,
            provisional_target=(provisional_target or (decision.target_term if decision else None)),
            blocking=blocking,
            evidence_status=evidence_status,
            chapters=tuple(item.chapter_id for item in candidate.occurrences),
            chunks=tuple(
                item.chunk_id for item in candidate.occurrences if item.chunk_id is not None
            ),
            research=QuestionResearch(
                current_book=current_book,
                web=web,
                other_volumes=other_volumes,
                notes=sorted(set(note or ())),
            ),
        )
        publish_questions(root, updated)
    except (TerminologyError, QuestionError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Open question recorded for {source} ({category.value})")
    console.print(f"Question ID: {question_identity(candidate_id, category)}")
    console.print(f"Questions SHA-256: {questions_identity(updated)}")


@app.command("list-terminology-questions")
def list_terminology_questions_command(
    status: Annotated[QuestionStatus | None, typer.Option("--status")] = None,
) -> None:
    """List durable terminology questions without printing story prose."""
    try:
        questions = load_or_empty_questions(Path.cwd())
    except (QuestionError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Terminology open questions")
    for column in ("question", "term", "category", "status", "blocking", "provisional"):
        table.add_column(column)
    for item in sorted(questions.questions, key=lambda value: value.question_id):
        if status is not None and item.status is not status:
            continue
        table.add_row(
            item.question_id,
            item.source_term,
            item.category.value,
            item.status.value,
            "yes" if item.blocking else "no",
            item.provisional_target or "-",
        )
    console.print(table)
    console.print(f"Questions SHA-256: {questions_identity(questions)}")


@app.command("resolve-terminology-question")
def resolve_terminology_question_command(
    question_id: Annotated[str, typer.Argument()],
    resolved_target: Annotated[str, typer.Option("--target")],
    provenance: Annotated[EvidenceProvenance, typer.Option("--provenance")],
    rationale: Annotated[str, typer.Option("--rationale")],
) -> None:
    """Close one open terminology question with its eventual answer."""
    root = Path.cwd()
    try:
        questions = load_or_empty_questions(root)
        existing = next(
            (item for item in questions.questions if item.question_id == question_id), None
        )
        if existing is None:
            raise QuestionError(f"unknown question {question_id}")
        updated = resolve_question(
            questions,
            question_id=question_id,
            resolution=QuestionResolution(
                resolved_target=resolved_target,
                provenance=provenance,
                rationale=rationale,
                resolved_at=datetime.now(UTC),
                supersedes_target=existing.provisional_target,
            ),
        )
        publish_questions(root, updated)
    except (QuestionError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Question resolved: {question_id} -> {resolved_target}")
    console.print(f"Questions SHA-256: {questions_identity(updated)}")


@app.command("correct-terminology-decision")
def correct_terminology_decision_command(
    source: Annotated[str, typer.Argument()],
    target: Annotated[str, typer.Option("--target")],
    decision_source: Annotated[DecisionSource, typer.Option("--decision-source")],
    decision_state: Annotated[DecisionState, typer.Option("--state")],
    reason: Annotated[str, typer.Option("--reason")],
    locked: Annotated[bool, typer.Option("--locked/--unlocked")] = False,
    question_id: Annotated[str | None, typer.Option("--question-id")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Correct a decision's ownership or state through durable superseding lineage."""
    root = Path.cwd()
    try:
        plan = plan_decision_correction(
            root,
            source_term=source,
            target_term=target,
            decision_source=decision_source,
            decision_state=decision_state,
            locked=locked,
            reason=reason,
            question_id=question_id,
        )
        console.print(f"Correction ID: {plan.correction_id}")
        console.print(
            "Previous: "
            f"{plan.previous['target_term']} "
            f"source={plan.previous['decision_source']} "
            f"state={plan.previous['decision_state']} "
            f"locked={plan.previous['locked']}"
        )
        console.print(
            f"Corrected: {target} source={decision_source.value} "
            f"state={decision_state.value} locked={locked}"
        )
        console.print(f"Registry SHA-256 before: {plan.registry_sha256_before}")
        console.print(f"Registry SHA-256 after:  {plan.registry_sha256_after}")
        if dry_run:
            console.print("Dry run: no correction was published.")
            return
        record = execute_decision_correction(plan)
    except (TerminologyError, QuestionError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Correction published: {record.correction_id}")


@app.command("report-terminology-readiness")
def report_terminology_readiness_command(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report translation readiness split by decision state and question severity."""
    try:
        report = readiness(Path.cwd())
    except (TerminologyError, QuestionError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if as_json:
        console.print_json(report.model_dump_json())
        return
    from rich.table import Table

    table = Table(title="Terminology readiness")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("confirmed decisions", str(report.confirmed_decisions))
    table.add_row("provisional decisions", str(report.provisional_decisions))
    table.add_row("open questions", str(report.open_questions))
    table.add_row("open blocking questions", str(report.open_blocking_questions))
    table.add_row("open non-blocking questions", str(report.open_non_blocking_questions))
    table.add_row("resolved questions", str(report.resolved_questions))
    console.print(table)
    console.print(f"Registry SHA-256:  {report.registry_sha256}")
    console.print(f"Questions SHA-256: {report.questions_sha256}")
    console.print(f"Translation ready: {'yes' if report.ready else 'no'}")
    for entry in report.blocking_reasons:
        console.print(f"  blocked by: {entry}")


@app.command("analyze-terminology-impact")
def analyze_terminology_impact_command(
    source: Annotated[str, typer.Argument()],
    proposed_target: Annotated[str | None, typer.Option("--proposed-target")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report, read-only, what changing one terminology decision would affect."""
    try:
        report = analyze_impact(Path.cwd(), source_term=source, proposed_target=proposed_target)
    except (TerminologyError, QuestionError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if as_json:
        console.print_json(report.model_dump_json())
        return
    console.print(f"Source term: {report.source_term}")
    console.print(f"Current target: {report.current_target_term or '-'}")
    console.print(f"Current state: {report.current_decision_state or '-'}")
    console.print(f"Proposed target: {report.proposed_target_term or '-'}")
    console.print(f"Impacted chunks: {len(report.impacted_chunks)}")
    console.print(f"Impacted snapshots: {len(report.impacted_snapshots)}")
    console.print(f"Impacted translations: {len(report.impacted_translations)}")
    console.print(f"Accepted requiring re-evaluation: {report.accepted_requiring_reevaluation}")
    for chunk in report.impacted_chunks:
        console.print(f"  chunk {chunk.chunk_id}: {chunk.occurrence_count} occurrence(s)")
    for snapshot in report.impacted_snapshots:
        console.print(
            f"  snapshot {snapshot.snapshot_id}: froze_affected="
            f"{'yes' if snapshot.froze_affected_decision else 'no'} "
            f"chunks={snapshot.chunk_count}"
        )


@app.command("validate-translation-qa")
def validate_translation_qa_command(
    source: Annotated[Path, typer.Option("--source")],
    translation: Annotated[Path, typer.Option("--translation")],
    target_language: Annotated[str, typer.Option("--target-language")] = "fa",
) -> None:
    """Run read-only terminology and structural QA checks without printing prose."""
    try:
        source_text = source.read_text(encoding="utf-8")
        translation_text = translation.read_text(encoding="utf-8")
        registry = load_registry(_terminology_path())
        terminology = validate_terminology(source_text, translation_text, registry)
        qa = validate_translation_qa(source_text, translation_text, target_language=target_language)
    except (OSError, UnicodeError, TerminologyError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Terminology valid: {'yes' if terminology.valid else 'no'}")
    console.print(f"Terminology findings: {len(terminology.findings)}")
    console.print(f"Structural QA valid: {'yes' if qa.valid else 'no'}")
    console.print(f"Structural QA findings: {len(qa.findings)}")
    for terminology_finding in terminology.findings:
        console.print(
            f"{terminology_finding.severity}: {terminology_finding.code}: "
            f"{terminology_finding.source_term}: {terminology_finding.message}"
        )
    for qa_finding in qa.findings:
        console.print(f"{qa_finding.severity}: {qa_finding.code}: {qa_finding.message}")
    if not terminology.valid or not qa.valid:
        raise typer.Exit(code=1)


@app.command()
def status(book: BookOption = None) -> None:
    """Display configuration and translation progress."""
    try:
        config = load_book_config(_project_paths(book).book_config)
        progress = load_progress(Path("state/progress.json"))
    except ProjectDataError as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    render_status(config, progress, console, book=book)


def _configured_or_explicit_pdf(pdf_path: Path | None, book: str | None = None) -> Path:
    if pdf_path is not None:
        return pdf_path
    return require_pdf_path(load_book_config(_project_paths(book).book_config))


def _configured_or_explicit_epub(epub_path: Path | None, book: str | None = None) -> Path:
    if epub_path is not None:
        return epub_path
    return require_epub_path(load_book_config(_project_paths(book).book_config))


def _render_pdf_inspection(result: PdfInspectionResult) -> None:
    from rich.table import Table

    table = Table(title="PDF Inspection", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Resolved path", str(result.path))
    table.add_row("File size", f"{result.file_size:,} bytes")
    table.add_row("Total pages", str(result.page_count))
    table.add_row("Encrypted", "yes" if result.encrypted else "no")
    for label, value in (
        ("Title", result.metadata.title),
        ("Author", result.metadata.author),
        ("Subject", result.metadata.subject),
        ("Creator", result.metadata.creator),
    ):
        if value:
            table.add_row(label, value)
    table.add_row("Pages with text", str(result.pages_with_text))
    table.add_row("Pages without text", str(result.pages_without_text))
    table.add_row("Total words", str(result.total_word_count))
    table.add_row("Average words/page", f"{result.average_words_per_page:.2f}")
    table.add_row("Minimum words/page", str(result.minimum_words_per_page))
    table.add_row("Maximum words/page", str(result.maximum_words_per_page))
    table.add_row(
        "First page with text",
        str(result.first_page_with_text) if result.first_page_with_text else "none",
    )
    table.add_row(
        "Last page with text",
        str(result.last_page_with_text) if result.last_page_with_text else "none",
    )
    table.add_row("Classification", result.classification)
    console.print(table)


@app.command("inspect-pdf")
def inspect_pdf_command(
    pdf_path: Annotated[Path | None, typer.Argument()] = None,
    book: BookOption = None,
) -> None:
    """Inspect whether a PDF contains extractable text."""
    try:
        _render_pdf_inspection(inspect_pdf(_configured_or_explicit_pdf(pdf_path, book)))
    except (ProjectDataError, PdfError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("extract-pages")
def extract_pages_command(
    from_page: Annotated[int, typer.Option("--from-page")],
    to_page: Annotated[int, typer.Option("--to-page")],
    output: Annotated[Path, typer.Option("--output")],
    pdf_path: Annotated[Path | None, typer.Argument()] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    book: BookOption = None,
) -> None:
    """Extract an inclusive, 1-based PDF page range as UTF-8 text."""
    try:
        result = extract_pages(
            _configured_or_explicit_pdf(pdf_path, book),
            from_page=from_page,
            to_page=to_page,
            output_path=output,
            force=force,
        )
    except (ProjectDataError, PdfError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[bold]Output path:[/bold] {result.output_path}")
    console.print(f"[bold]Extracted pages:[/bold] {result.page_count}")
    console.print(f"[bold]Extracted words:[/bold] {result.word_count}")
    console.print(f"[bold]Output size:[/bold] {result.byte_size:,} bytes")


def _render_epub_import_summary(
    plan: EpubImportPlan, result: EpubImportResult | None, *, dry_run: bool
) -> None:
    from rich.table import Table

    table = Table(title="EPUB Import", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    rows = (
        ("EPUB path", plan.epub_path),
        ("Package document", plan.document.package_path),
        ("Spine items", plan.document.spine_item_count),
        ("Narrative units", len(plan.units)),
        ("Units planned", sum(item.action != "skip" for item in plan.units)),
        ("Units imported", len(result.imported_ids) if result else 0),
        ("Units skipped", sum(item.action == "skip" for item in plan.units)),
        (
            "Extracted words",
            result.total_words if result else sum(i.word_count for i in plan.units),
        ),
        ("Workspace", "not written (dry run)" if dry_run else plan.workspace_path),
        ("Provider calls", 0),
    )
    for label, value in rows:
        table.add_row(str(label), str(value))
    console.print(table)


@app.command("import-epub")
def import_epub_command(
    epub_path: Annotated[Path | None, typer.Argument()] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    book: BookOption = None,
) -> None:
    """Import an EPUB spine into canonical narrative source artifacts."""
    try:
        selected = _configured_or_explicit_epub(epub_path, book)
        project_root = Path.cwd().resolve()
        plan = plan_epub_import(
            selected,
            project_root=project_root,
            book=book,
            force=force,
        )
        result = None if dry_run else execute_epub_import(plan, project_root=project_root)
    except (ProjectDataError, EpubIngestionError, OSError, UnicodeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _render_epub_import_summary(plan, result, dry_run=dry_run)


def _render_toc_summary(result: TocDiscoveryResult, output: str) -> None:
    from collections import Counter

    from rich.table import Table

    counts = Counter(entry.kind.value for entry in result.document.entries)
    chapters = [entry for entry in result.document.entries if entry.kind.value == "chapter"]
    table = Table(title="TOC Discovery", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("PDF path", str(result.path))
    table.add_row("PDF page count", str(result.page_count))
    table.add_row("TOC source", result.document.source or "embedded")
    table.add_row("Raw entries", str(result.raw_entry_count))
    table.add_row("Normalized entries", str(len(result.document.entries)))
    table.add_row("Front matter", str(counts["front_matter"]))
    table.add_row("Prologue", str(counts["prologue"]))
    table.add_row("Chapters", str(counts["chapter"]))
    table.add_row("Epilogue", str(counts["epilogue"]))
    table.add_row("Appendices", str(counts["appendix"]))
    table.add_row("Unknown", str(counts["unknown"]))
    table.add_row("First chapter", str(chapters[0].number) if chapters else "none")
    table.add_row("Last chapter", str(chapters[-1].number) if chapters else "none")
    table.add_row("Output", output)
    console.print(table)


@app.command("discover-toc")
def discover_toc_command(
    pdf_path: Annotated[Path | None, typer.Argument()] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    book: BookOption = None,
    text_headings: Annotated[
        bool,
        typer.Option(
            "--text-headings",
            help="Discover a line-layout PDF's headings from its text instead of an embedded TOC.",
        ),
    ] = False,
) -> None:
    """Discover and normalize a PDF's embedded table of contents."""
    output_path = output if output is not None else _project_paths(book).toc
    try:
        configured_path = _configured_or_explicit_pdf(pdf_path, book)
        if output_path.exists() and not force and not dry_run:
            raise TocDiscoveryError(
                f"output file already exists: {output_path} (use --force to replace it)"
            )
        if text_headings:
            book_title = load_book_config(_project_paths(book).book_config).title_en
            result = discover_text_heading_toc(
                configured_path, book_title=book_title, source_pdf=configured_path
            )
        else:
            result = discover_embedded_toc(configured_path, source_pdf=configured_path)
        if not dry_run:
            write_toc(result.document, output_path, force=force)
    except (ProjectDataError, TocDiscoveryError, OSError, FileExistsError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    _render_toc_summary(result, "not written (dry run)" if dry_run else str(output_path))


def _render_extraction_summary(
    plan: ExtractionPlan, result: ExtractionResult | None, *, dry_run: bool
) -> None:
    from rich.table import Table

    planned = [item for item in plan.entries if item.action != "skip"]
    skipped = [item for item in plan.entries if item.action == "skip"]
    extracted_count = len(result.extracted_ids) if result is not None else 0
    failed_count = len(result.failed_ids) if result is not None else 0
    table = Table(title="Chapter Extraction", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("PDF path", str(plan.pdf_path))
    table.add_row("TOC path", str(plan.toc_path))
    table.add_row("Selected entries", str(len(plan.entries)))
    table.add_row(
        "Pages covered",
        str(sum(item.entry.end_page - item.entry.start_page + 1 for item in plan.entries)),
    )
    table.add_row("Entries planned", str(len(planned)))
    table.add_row("Entries extracted", str(extracted_count))
    table.add_row("Entries skipped", str(len(skipped)))
    table.add_row("Entries failed", str(failed_count))
    table.add_row("Total extracted words", str(result.total_words if result else 0))
    table.add_row("Total output bytes", str(result.total_bytes if result else 0))
    table.add_row("Workspace path", str(plan.workspace_path))
    table.add_row("Dry run", "yes" if dry_run else "no")
    if len(plan.entries) == 1:
        item = plan.entries[0]
        table.add_row("Canonical entry ID", item.entry.entry_id)
        table.add_row("Title", item.entry.title_en)
        table.add_row("Page range", f"{item.entry.start_page}-{item.entry.end_page}")
        table.add_row("Destination", str(item.paths.directory))
    console.print(table)


@app.command("extract-chapters")
def extract_chapters_command(
    pdf_path: Annotated[Path | None, typer.Argument()] = None,
    toc: Annotated[Path | None, typer.Option("--toc")] = None,
    chapter: Annotated[str | None, typer.Option("--chapter")] = None,
    include_front_matter: Annotated[bool, typer.Option("--include-front-matter")] = False,
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    book: BookOption = None,
) -> None:
    """Extract immutable chapter source artifacts from the canonical TOC."""
    paths = _project_paths(book)
    project_root = paths.project_root
    toc_path = toc if toc is not None else paths.toc
    try:
        selected_pdf = _configured_or_explicit_pdf(pdf_path, book)
        plan = plan_extraction(
            selected_pdf,
            toc_path,
            project_root=project_root,
            selector=chapter,
            include_front_matter=include_front_matter,
            force=force,
            book=book,
        )
        result = None if dry_run else execute_extraction(plan, project_root=project_root)
    except (ProjectDataError, ChapterExtractionError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _render_extraction_summary(plan, result, dry_run=dry_run)


def _render_normalization_summary(
    plan: NormalizationPlan,
    execution: NormalizationExecution | None,
    *,
    dry_run: bool,
    show_diff_summary: bool,
) -> None:
    from rich.table import Table

    normalized = len(execution.normalized_ids) if execution else 0
    skipped = sum(item.action == "skip" for item in plan.chapters)
    table = Table(title="Chapter Normalization", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    rows = (
        ("Selected chapters", len(plan.chapters)),
        (
            "Chapters planned",
            sum(item.action in {"normalize", "replace"} for item in plan.chapters),
        ),
        ("Chapters normalized", normalized),
        ("Chapters skipped", skipped),
        ("Chapters stale", sum(item.action == "stale" for item in plan.chapters)),
        ("Chapters failed", sum(item.action == "conflict" for item in plan.chapters)),
        ("Source words", sum(item.metadata.source_word_count or 0 for item in plan.chapters)),
        ("Cleaned words", sum(item.cleaned_word_count for item in plan.chapters)),
        (
            "Source physical lines",
            sum(item.result.document.source_physical_line_count for item in plan.chapters),
        ),
        ("Wrapped line joins", sum(item.result.wrapped_line_joins for item in plan.chapters)),
        ("Hyphen repairs", sum(item.result.hyphen_repairs for item in plan.chapters)),
        ("Page markers removed", sum(item.result.page_markers_removed for item in plan.chapters)),
        ("Warnings", sum(len(item.result.warnings) for item in plan.chapters)),
        (
            "Total output bytes",
            execution.total_bytes
            if execution
            else sum(
                len(item.result.cleaned_text.encode("utf-8"))
                for item in plan.chapters
                if item.action in {"normalize", "replace"}
            ),
        ),
        ("Normalization version", plan.normalization_version),
        ("Dry run", "yes" if dry_run else "no"),
    )
    for label, value in rows:
        table.add_row(label, str(value))
    if len(plan.chapters) == 1 and not show_diff_summary:
        item = plan.chapters[0]
        table.add_row("Canonical chapter ID", item.metadata.chapter_id)
        table.add_row("Title", item.metadata.title_en)
        table.add_row("Source hash", item.source_sha256)
        table.add_row("Cleaned hash", item.cleaned_sha256)
        table.add_row("Output path", str(item.paths.cleaned))
    console.print(table)


@app.command("normalize-chapters")
def normalize_chapters_command(
    chapter: Annotated[str | None, typer.Option("--chapter")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    show_diff_summary: Annotated[bool, typer.Option("--show-diff-summary")] = False,
    normalization_version: Annotated[
        str, typer.Option("--normalization-version", hidden=True)
    ] = NORMALIZATION_VERSION,
    book: BookOption = None,
) -> None:
    """Create deterministic cleaned chapter artifacts without semantic rewriting."""
    try:
        plan = plan_normalization(
            project_root=Path.cwd().resolve(),
            selector=chapter,
            force=force,
            normalization_version=normalization_version,
            book=book,
        )
        execution = None if dry_run else execute_normalization(plan)
    except (ChapterNormalizationError, OSError, UnicodeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _render_normalization_summary(
        plan, execution, dry_run=dry_run, show_diff_summary=show_diff_summary
    )


def _render_chunk_plan(plan: ChapterChunkingPlan) -> None:
    from rich.table import Table

    table = Table(title="Document Chunk Plan")
    table.add_column("Chapter")
    table.add_column("Chunk", justify="right")
    table.add_column("Paragraphs")
    table.add_column("Words", justify="right")
    table.add_column("Scene end")
    table.add_column("Oversize")
    table.add_column("Context words", justify="right")
    for chapter in plan.chapters:
        for chunk in chapter.document_plan.chunks:
            table.add_row(
                chapter.metadata.chapter_id,
                str(chunk.sequence),
                f"{chunk.paragraph_start}-{chunk.paragraph_end}",
                str(chunk.word_count),
                "yes" if chunk.ended_at_scene_boundary else "no",
                "yes" if chunk.oversize else "no",
                str(chunk.context.word_count),
            )
    console.print(table)


def _render_chunking_summary(
    plan: ChapterChunkingPlan,
    execution: ChunkingExecution | None,
    *,
    dry_run: bool,
) -> None:
    from rich.table import Table

    chunks = [chunk for chapter in plan.chapters for chunk in chapter.document_plan.chunks]
    counts = [chunk.word_count for chunk in chunks]
    table = Table(title="Document Chunking", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    rows = (
        ("Selected chapters", len(plan.chapters)),
        ("Chapters planned", sum(item.action in {"chunk", "replace"} for item in plan.chapters)),
        ("Chapters chunked", len(execution.chunked_ids) if execution else 0),
        ("Chapters skipped", sum(item.action == "skip" for item in plan.chapters)),
        ("Chapters stale", sum(item.action == "stale" for item in plan.chapters)),
        ("Chapters failed", sum(item.action == "conflict" for item in plan.chapters)),
        ("Input paragraphs", sum(len(item.document_plan.paragraphs) for item in plan.chapters)),
        ("Input words", sum(item.document_plan.source_word_count for item in plan.chapters)),
        ("Total chunks", len(chunks)),
        ("Oversize chunks", sum(chunk.oversize for chunk in chunks)),
        ("Minimum chunk words", min(counts)),
        ("Average chunk words", f"{sum(counts) / len(counts):.2f}"),
        ("Maximum chunk words", max(counts)),
        (
            "Scene boundaries used",
            sum(item.document_plan.scene_boundaries_used for item in plan.chapters),
        ),
        ("Tail rebalances", sum(item.document_plan.tail_rebalanced for item in plan.chapters)),
        ("Total context words", sum(chunk.context.word_count for chunk in chunks)),
        ("Conservation valid", "yes"),
        ("Output bytes", execution.total_output_bytes if execution else 0),
        ("Engine version", CHUNK_ENGINE_VERSION),
        ("Dry run", "yes" if dry_run else "no"),
    )
    for label, value in rows:
        table.add_row(label, str(value))
    if len(plan.chapters) == 1:
        item = plan.chapters[0]
        table.add_row("Canonical chapter ID", item.metadata.chapter_id)
        table.add_row("Title", item.metadata.title_en)
        table.add_row("Cleaned hash", item.cleaned_sha256)
        table.add_row("Chunk count", str(len(item.document_plan.chunks)))
        table.add_row("Output directory", str(item.paths.chunks))
    console.print(table)


@app.command("chunk-chapters")
def chunk_chapters_command(
    chapter: Annotated[str | None, typer.Option("--chapter")] = None,
    target_words: Annotated[int | None, typer.Option("--target-words")] = None,
    max_words: Annotated[int | None, typer.Option("--max-words")] = None,
    context_words: Annotated[int | None, typer.Option("--context-words")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    show_plan: Annotated[bool, typer.Option("--show-plan")] = False,
    book: BookOption = None,
) -> None:
    """Create deterministic generic document chunks from normalized chapters."""
    try:
        config = load_book_config(_project_paths(book).book_config)
        chunking_config = ChunkingConfig(
            target_words=config.chunk_target_words if target_words is None else target_words,
            max_words=config.chunk_max_words if max_words is None else max_words,
            context_words=(
                config.previous_context_words if context_words is None else context_words
            ),
        )
        plan = plan_chapter_chunking(
            project_root=Path.cwd().resolve(),
            config=chunking_config,
            selector=chapter,
            force=force,
            book=book,
        )
        execution = None if dry_run else execute_chapter_chunking(plan)
    except (ProjectDataError, ValueError, OSError, UnicodeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if show_plan:
        _render_chunk_plan(plan)
    _render_chunking_summary(plan, execution, dry_run=dry_run)


def _render_artifact_regeneration(
    plan: ArtifactRegenerationPlan,
    execution: ArtifactRegenerationExecution | None,
    *,
    apply: bool,
) -> None:
    from rich.table import Table

    table = Table(title="Mutable Artifact Staleness")
    for column in (
        "Chapter",
        "Source",
        "Normalization",
        "Chunks",
        "Cleaned hash",
        "Chunk hashes",
        "Snapshots",
        "Runs",
        "Plan",
        "Next layer",
    ):
        table.add_column(column)
    for item in plan.chapters:
        table.add_row(
            item.metadata.chapter_id,
            "current" if item.source_current else "unsafe",
            "current" if item.normalization_current else "stale",
            "current" if item.chunking_current else "stale",
            "change" if item.cleaned_hash_would_change else "same",
            "change" if item.chunk_hashes_would_change else "same",
            str(len(item.immutable_lineage.snapshot_ids)),
            str(len(item.immutable_lineage.run_ids)),
            item.action,
            item.next_step,
        )
    console.print(table)
    for item in plan.chapters:
        console.print(
            f"[bold]{item.metadata.chapter_id} plan:[/bold] {item.action}; "
            f"cleaned-hash {'change' if item.cleaned_hash_would_change else 'same'} "
            f"{item.stored_cleaned_sha256 or 'missing'} -> {item.planned_cleaned_sha256}; "
            f"chunk-hashes {'change' if item.chunk_hashes_would_change else 'same'}; "
            f"chunks {item.stored_chunk_count} -> "
            f"{item.planned_chunk_count}; immutable snapshots "
            f"{len(item.immutable_lineage.snapshot_ids)}, runs "
            f"{len(item.immutable_lineage.run_ids)}, prompt-packages "
            f"{item.immutable_lineage.prompt_package_count}, attempts "
            f"{item.immutable_lineage.attempt_count}; next {item.next_step}"
        )
    summary = Table(title="Regeneration Summary", show_header=False)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    summary.add_row("Selected chapters", str(len(plan.chapters)))
    summary.add_row(
        "Normalization replacements",
        str(sum(item.replace_normalization for item in plan.chapters)),
    )
    summary.add_row("Chunk replacements", str(sum(item.replace_chunks for item in plan.chapters)))
    summary.add_row(
        "Immutable lineages preserved",
        str(sum(item.immutable_lineage.exists for item in plan.chapters)),
    )
    summary.add_row("Blocked chapters", str(sum(bool(item.blockers) for item in plan.chapters)))
    summary.add_row("Normalization version", plan.normalization_version)
    summary.add_row("Chunk engine version", CHUNK_ENGINE_VERSION)
    summary.add_row("Mode", "apply" if apply else "read-only dry run")
    summary.add_row("Regenerated chapters", str(len(execution.regenerated_ids) if execution else 0))
    console.print(summary)
    for item in plan.chapters:
        for blocker in item.blockers:
            error_console.print(
                f"[bold red]Blocked {item.metadata.chapter_id}:[/bold red] {blocker}"
            )


@app.command("inspect-artifact-staleness")
def inspect_artifact_staleness_command(
    chapter: Annotated[list[str], typer.Option("--chapter")],
    book: BookOption = None,
) -> None:
    """Inspect mutable chapter staleness and immutable downstream lineage without writes."""
    try:
        plan = plan_artifact_regeneration(
            project_root=Path.cwd(),
            selectors=chapter,
            book=book,
        )
    except (ArtifactRegenerationError, ProjectDataError, OSError, UnicodeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _render_artifact_regeneration(plan, None, apply=False)


def _normalization_inspection_data(
    inspection: NormalizationChangeInspection,
) -> dict[str, object]:
    from dataclasses import asdict

    value: dict[str, object] = asdict(inspection)
    value.pop("planned_bytes")
    return value


@app.command("inspect-normalization-changes")
def inspect_normalization_changes_command(
    chapter: Annotated[list[str], typer.Option("--chapter")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
    book: BookOption = None,
) -> None:
    """Show exact planned cleaned-text changes without writing canonical artifacts."""
    try:
        plan = plan_artifact_regeneration(project_root=Path.cwd(), selectors=chapter, book=book)
        inspections = tuple(inspect_normalization_changes(item) for item in plan.chapters)
    except (ArtifactRegenerationError, ProjectDataError, OSError, UnicodeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(
            json.dumps(
                [_normalization_inspection_data(item) for item in inspections],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    for inspection in inspections:
        typer.echo(f"Chapter: {inspection.chapter_id}")
        typer.echo(f"Current normalization version: {inspection.current_version}")
        typer.echo(f"Planned normalization version: {inspection.planned_version}")
        typer.echo(f"Current cleaned SHA: {inspection.current_sha256}")
        typer.echo(f"Planned cleaned SHA: {inspection.planned_sha256}")
        typer.echo(f"Bytes change: {'yes' if inspection.bytes_change else 'no'}")
        typer.echo(f"Changed regions: {len(inspection.regions)}")
        labels = (
            (
                item.sequence,
                "source+context"
                if item.source and item.context
                else "source"
                if item.source
                else "context",
            )
            for item in inspection.chunk_hash_changes
        )
        changed = ", ".join(f"{sequence:03d} ({label})" for sequence, label in labels)
        typer.echo(f"Chunk hashes changing: {changed or 'none'}")
        for number, region in enumerate(inspection.regions, start=1):
            pages = "–".join(str(value) for value in region.pages) or "unavailable"
            blocks = ", ".join(f"{page}:{block}" for page, block in region.blocks)
            typer.echo(f"\nChange {number}")
            typer.echo(f"Type: {region.kind}")
            typer.echo(f"Pages: {pages}")
            typer.echo(f"Blocks: {blocks or 'unavailable'}")
            typer.echo(f"Pass: {region.pass_name}")
            typer.echo(f"Rule: {region.rule}")
            typer.echo("\nBEFORE\n------")
            typer.echo("\n\n".join(region.before) or "(empty)")
            typer.echo("\nAFTER\n-----")
            typer.echo("\n\n".join(region.after) or "(empty)")


@app.command("regenerate-artifacts")
def regenerate_artifacts_command(
    chapter: Annotated[list[str], typer.Option("--chapter")],
    apply: Annotated[bool, typer.Option("--apply")] = False,
    book: BookOption = None,
) -> None:
    """Plan by default; explicitly replace only stale normalized/chunk artifacts with --apply."""
    try:
        plan = plan_artifact_regeneration(
            project_root=Path.cwd(),
            selectors=chapter,
            book=book,
        )
        execution = execute_artifact_regeneration(plan) if apply else None
    except (ArtifactRegenerationError, ProjectDataError, OSError, UnicodeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _render_artifact_regeneration(plan, execution, apply=apply)


def _render_snapshot_manifest(plan: TranslationSnapshotPlan) -> None:
    from rich.table import Table

    table = Table(title="Translation Snapshot Manifest")
    table.add_column("Chapter")
    table.add_column("Chunk")
    table.add_column("Source SHA-256")
    table.add_column("Context SHA-256")
    for chapter in plan.manifest.chapters:
        for chunk in chapter.chunks:
            table.add_row(
                chapter.chapter_id,
                str(chunk.sequence),
                chunk.source_sha256,
                chunk.context_sha256,
            )
    console.print(table)


def _render_book_metadata(plan: BookMetadataPlan, *, applied: bool) -> None:
    from rich.table import Table

    metadata = plan.metadata
    identity = Table(title="Resolved Book Metadata", show_header=False)
    identity.add_column("Field", style="bold")
    identity.add_column("Value")
    identity.add_row("Book ID", metadata.book_id)
    identity.add_row("English title", metadata.title_en)
    identity.add_row("Persian title", metadata.title_fa or "(unresolved)")
    identity.add_row("Author", metadata.author)
    identity.add_row("Languages", f"{metadata.source_language} → {metadata.target_language}")
    identity.add_row("Series", metadata.series_name or "(unresolved)")
    identity.add_row(
        "Series volume", str(metadata.series_index) if metadata.series_index else "(unresolved)"
    )
    identity.add_row("Publisher", metadata.publisher or "(unresolved)")
    identity.add_row("Edition", metadata.edition or "(unresolved)")
    identity.add_row(
        "Publication year",
        str(metadata.publication_year) if metadata.publication_year else "(unresolved)",
    )
    identity.add_row("ISBN", metadata.isbn or "(unresolved)")
    identity.add_row("Source PDF", f"{metadata.source_pdf_name} ({metadata.source_pdf_sha256})")
    identity.add_row("Engine version", metadata.metadata_version)
    identity.add_row("Canonical file", str(plan.metadata_file))
    identity.add_row("Action", "applied" if applied else plan.action)
    console.print(identity)

    provenance = Table(title="Metadata Provenance")
    for column in ("Field", "Provenance", "Evidence"):
        provenance.add_column(column)
    for item in metadata.provenance:
        provenance.add_row(item.field, item.provenance.value, item.evidence or "")
    console.print(provenance)
    for warning in plan.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")


@app.command("resolve-book-metadata")
def resolve_book_metadata_command(
    assertion: Annotated[
        list[str] | None,
        typer.Option("--set", help="Explicit FIELD=VALUE assertion, repeatable"),
    ] = None,
    provenance: Annotated[
        str, typer.Option("--provenance", help="Provenance recorded for --set assertions")
    ] = "configuration",
    apply: Annotated[bool, typer.Option("--apply")] = False,
    book: BookOption = None,
) -> None:
    """Resolve canonical book metadata from local PDF evidence and project configuration."""
    try:
        claimed_provenance = BookMetadataProvenance(provenance.replace("-", "_"))
        if claimed_provenance is BookMetadataProvenance.UNRESOLVED:
            raise BookMetadataError("an explicit assertion cannot claim unresolved provenance")
        assertions: dict[str, OperatorAssertion] = {}
        for item in assertion or []:
            name, separator, value = item.partition("=")
            if not separator or not name.strip() or not value.strip():
                raise BookMetadataError(f"invalid --set assertion: {item!r}")
            assertions[name.strip()] = OperatorAssertion(value.strip(), claimed_provenance)
        plan = plan_book_metadata(project_root=Path.cwd(), assertions=assertions, book=book)
        if apply:
            execute_book_metadata(plan)
    except (ValueError, BookMetadataError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _render_book_metadata(plan, applied=apply and plan.action != "skip")
    if not apply:
        console.print(
            "[dim]Dry run: rerun with --apply to publish "
            f"(engine {BOOK_METADATA_ENGINE_VERSION}).[/dim]"
        )


def _render_book_identity_migration(plan: BookIdentityMigrationPlan, *, applied: bool) -> None:
    from rich.table import Table

    summary = Table(title="Book Identity Migration", show_header=False)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    summary.add_row("Migration ID", plan.migration_id or "(none required)")
    summary.add_row("Previous book ID", plan.previous_book_id)
    summary.add_row("Canonical book ID", plan.book_id)
    summary.add_row("Registries", str(len(plan.registries)))
    summary.add_row("Action", "applied" if applied else plan.action)
    summary.add_row("Lineage record", str(plan.record_file) if plan.record_file else "")
    console.print(summary)
    if not plan.registries:
        return
    table = Table(title="Migrated Registries")
    for column in ("File", "Kind", "Before SHA-256", "After SHA-256", "Semantic SHA-256", "Lines"):
        table.add_column(column)
    for item in plan.registries:
        record = item.record
        table.add_row(
            item.file.as_posix(),
            record.registry_kind,
            record.before_sha256,
            record.after_sha256,
            record.semantic_sha256,
            str(record.changed_line_count),
        )
    console.print(table)
    changes = [
        (item.file.as_posix(), change)
        for item in plan.registries
        for change in item.record.derived_identity_changes
    ]
    if changes:
        derived = Table(title="Re-derived Book-Scoped Identifiers")
        for column in ("File", "Field", "Record", "Before", "After"):
            derived.add_column(column)
        for file, change in changes:
            derived.add_row(file, change.field, change.record, change.before, change.after)
        console.print(derived)
    for warning in plan.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")


@app.command("migrate-book-identity")
def migrate_book_identity_command(
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Migrate current registries onto the canonical book identity with immutable lineage."""
    try:
        plan = plan_book_identity_migration(project_root=Path.cwd())
        if apply:
            execute_book_identity_migration(plan)
    except (BookIdentityMigrationError, BookMetadataError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _render_book_identity_migration(plan, applied=apply and plan.action != "skip")
    if not apply and plan.action != "skip":
        console.print("[dim]Dry run: rerun with --apply to migrate and record lineage.[/dim]")


@app.command("freeze-translation-resources")
def freeze_translation_resources_command(
    apply: Annotated[bool, typer.Option("--apply")] = False,
    book: BookOption = None,
) -> None:
    """Publish durable content-addressed copies of the current mutable translation inputs."""
    try:
        plan = plan_resource_freeze(project_root=Path.cwd(), book=book)
        if apply:
            execute_resource_freeze(plan)
    except (ProjectDataError, SnapshotError, BookMetadataError, OSError, UnicodeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Frozen Translation Resources")
    for column in ("Resource", "Source", "SHA-256", "Frozen file", "Action"):
        table.add_column(column)
    for item in plan.publications:
        table.add_row(
            item.label,
            item.source_file.as_posix(),
            item.sha256,
            item.frozen_file.as_posix(),
            ("published" if apply else "publish") if item.action == "publish" else "reuse",
        )
    console.print(table)
    console.print(f"[dim]Current snapshot identity: {plan.snapshot_id}[/dim]")
    if not apply:
        console.print("[dim]Dry run: rerun with --apply to publish durable copies.[/dim]")


@app.command("create-translation-snapshot")
def create_translation_snapshot_command(
    chapter: Annotated[list[str] | None, typer.Option("--chapter")] = None,
    label: Annotated[str | None, typer.Option("--label")] = None,
    prompt: Annotated[Path | None, typer.Option("--prompt")] = None,
    glossary: Annotated[Path | None, typer.Option("--glossary")] = None,
    style_guide: Annotated[Path | None, typer.Option("--style-guide")] = None,
    output_root: Annotated[Path | None, typer.Option("--output-root")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    show_manifest: Annotated[bool, typer.Option("--show-manifest")] = False,
    book: BookOption = None,
) -> None:
    """Freeze validated chunk and resource inputs for a future translation run."""
    try:
        plan = plan_snapshot(
            project_root=Path.cwd(),
            selectors=chapter,
            label=label,
            prompt=prompt,
            glossary=glossary,
            style_guide=style_guide,
            output_root=output_root,
            force=force,
            book=book,
        )
        result = None if dry_run else create_snapshot(plan)
        if plan.action == "conflict":
            raise SnapshotError(
                "snapshot publication is incomplete; inspect it and rerun with --force"
            )
    except (ProjectDataError, SnapshotError, OSError, UnicodeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if show_manifest:
        _render_snapshot_manifest(plan)
    from rich.table import Table

    table = Table(title="Translation Snapshot", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Snapshot ID", plan.snapshot_id)
    table.add_row("Manifest SHA-256", plan.manifest_sha256)
    table.add_row("Selection mode", plan.manifest.selection_mode)
    table.add_row("Selected chapters", str(len(plan.manifest.chapters)))
    table.add_row("Selected chunks", str(sum(item.chunk_count for item in plan.manifest.chapters)))
    table.add_row(
        "Action", plan.action if result is None else ("skipped" if result.skipped else "created")
    )
    table.add_row("Output path", str(plan.output_directory))
    table.add_row("Dry run", "yes" if dry_run else "no")
    table.add_row("Warnings", str(len(plan.warnings)))
    console.print(table)


@app.command("validate-translation-snapshot")
def validate_translation_snapshot_command(
    snapshot_id_or_path: Annotated[str, typer.Argument()],
    require_current: Annotated[bool, typer.Option("--require-current")] = False,
    book: BookOption = None,
) -> None:
    """Validate immutable snapshot integrity and report current-lineage readiness."""
    try:
        result = validate_snapshot(snapshot_id_or_path, project_root=Path.cwd(), book=book)
    except SnapshotError as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Translation Snapshot Validation", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Snapshot ID", result.report.snapshot_id)
    table.add_row("Historically valid", "yes" if result.report.valid else "no")
    table.add_row("Current lineage", "yes" if result.lineage_current else "no")
    table.add_row("Selected chapters", str(result.report.selected_chapter_count))
    table.add_row("Selected chunks", str(result.report.selected_chunk_count))
    table.add_row("Hashes validated", str(result.report.artifact_hashes_validated))
    table.add_row("Conservation checks", str(result.report.conservation_checks))
    table.add_row("Errors", str(len(result.report.errors)))
    console.print(table)
    if not result.report.valid:
        for error in result.report.errors:
            error_console.print(f"[bold red]Error:[/bold red] {error}")
        raise typer.Exit(code=1)
    if require_current and not result.lineage_current:
        error_console.print(
            "[bold red]Error:[/bold red] snapshot is valid history but is not current "
            "for new translation work"
        )
        raise typer.Exit(code=1)


@app.command("list-translation-snapshots")
def list_translation_snapshots_command(book: BookOption = None) -> None:
    """List translation snapshots without displaying manifest content."""
    from rich.table import Table

    table = Table(title="Translation Snapshots")
    for column in (
        "Snapshot ID",
        "Label",
        "Status",
        "Selection",
        "Chapters",
        "Chunks",
        "Languages",
        "Prompt",
        "Glossary",
        "Created",
        "Validation",
    ):
        table.add_column(column)
    for item in list_snapshots(project_root=Path.cwd(), book=book):
        snapshot = item.snapshot
        table.add_row(
            snapshot.snapshot_id,
            snapshot.label or "",
            snapshot.status.value,
            snapshot.selection_mode,
            str(snapshot.selected_chapter_count),
            str(snapshot.selected_chunk_count),
            f"{snapshot.source_language} → {snapshot.target_language}",
            snapshot.prompt_version,
            snapshot.glossary_version,
            snapshot.created_at.isoformat(),
            item.validation_state,
        )
    console.print(table)


def _parse_internal_now(value: str | None) -> Callable[[], datetime] | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QueueError("--now must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise QueueError("--now must include a timezone")
    return lambda: parsed


def _render_run_plan(plan: PlannedTranslationRun) -> None:
    from rich.table import Table

    table = Table(title="Translation Run Plan")
    for column in (
        "Job ID",
        "Chapter",
        "Chunk",
        "Source words",
        "Context words",
        "Priority",
        "Status",
    ):
        table.add_column(column)
    for job in plan.jobs:
        table.add_row(
            job.job_id,
            job.chapter_id,
            job.chunk_id,
            str(job.source_word_count),
            str(job.context_word_count),
            str(job.priority),
            job.status.value,
        )
    console.print(table)


@app.command("create-translation-run")
def create_translation_run_command(
    snapshot_id_or_path: Annotated[str, typer.Argument()],
    label: Annotated[str | None, typer.Option("--label")] = None,
    output_root: Annotated[Path | None, typer.Option("--output-root")] = None,
    max_attempts: Annotated[int, typer.Option("--max-attempts")] = 3,
    retry_backoff_seconds: Annotated[int, typer.Option("--retry-backoff-seconds")] = 60,
    paragraph_markers: Annotated[
        bool | None, typer.Option("--paragraph-markers/--no-paragraph-markers")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    show_plan: Annotated[bool, typer.Option("--show-plan")] = False,
    book: BookOption = None,
) -> None:
    """Create a provider-independent persistent queue from a frozen snapshot."""
    try:
        try:
            book_config = ProjectPaths(Path.cwd(), book).book_config
        except ValueError as exc:
            raise QueueError(str(exc)) from exc
        configured = load_book_config(book_config).paragraph_marker_contract
        marked = configured if paragraph_markers is None else paragraph_markers
        plan = plan_run(
            snapshot_id_or_path,
            project_root=Path.cwd(),
            book=book,
            label=label,
            output_root=output_root,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            output_contract_version=(
                MARKED_OUTPUT_CONTRACT_VERSION if marked else OUTPUT_CONTRACT_VERSION
            ),
            # A marked run is held to the production semantic gate from the moment it exists;
            # the policy is frozen here so a later default cannot reinterpret this run.
            semantic_gate_policy_version=SEMANTIC_GATE_POLICY_VERSION if marked else None,
        )
        result = None if dry_run else create_run(plan)
    except (QueueError, ProjectDataError, OSError, TimeoutError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if show_plan:
        _render_run_plan(plan)
    from rich.table import Table

    table = Table(title="Translation Run", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Run ID", plan.run.run_id)
    table.add_row("Snapshot ID", plan.run.snapshot_id)
    table.add_row("Chapters", str(plan.run.chapter_count))
    table.add_row("Jobs", str(plan.run.job_count))
    table.add_row("Pending", str(plan.run.pending_count))
    table.add_row("Output contract", plan.run.output_contract_version or OUTPUT_CONTRACT_VERSION)
    table.add_row("Max attempts", str(plan.run.max_attempts))
    table.add_row("Retry backoff seconds", str(plan.run.retry_backoff_seconds))
    table.add_row("Status", plan.run.status.value)
    table.add_row("Output path", str(plan.output_directory))
    table.add_row("Dry run", "yes" if dry_run else "no")
    table.add_row("Published", "yes" if result is not None else "no")
    console.print(table)


@app.command("list-translation-runs")
def list_translation_runs_command() -> None:
    """List translation runs and aggregate queue progress."""
    from rich.table import Table

    table = Table(title="Translation Runs")
    for column in (
        "Run ID",
        "Snapshot ID",
        "Label",
        "Status",
        "Jobs",
        "Pending",
        "Claimed",
        "Running",
        "Succeeded",
        "Accepted",
        "QA rejected",
        "Failed",
        "Retry pending",
        "Created",
    ):
        table.add_column(column)
    try:
        states = list_runs(project_root=Path.cwd())
    except (QueueError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    for state in states:
        run = state.run
        table.add_row(
            run.run_id,
            run.snapshot_id,
            run.label or "",
            run.status.value,
            str(run.job_count),
            str(run.pending_count),
            str(run.claimed_count),
            str(run.running_count),
            str(run.succeeded_count),
            str(run.accepted_count),
            str(run.qa_rejected_count),
            str(run.failed_count),
            str(run.retry_pending_count),
            run.created_at.isoformat(),
        )
    console.print(table)


@app.command("show-translation-run")
def show_translation_run_command(run_id_or_path: Annotated[str, typer.Argument()]) -> None:
    """Show structural validity and progress for one translation run."""
    try:
        state = load_run_summary(run_id_or_path, project_root=Path.cwd())
    except (QueueError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    run = state.run
    permanent = sum(
        job.status is TranslationJobStatus.FAILED and attempt_budget_exhausted(job)
        for job in state.jobs
    )
    try:
        packages = list_prompt_packages(state.path, project_root=Path.cwd())
    except (PromptPackageError, QueueError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    rendered_packages = sum(item.package is not None for item in packages)
    valid_packages = sum(item.validation_result == "valid" for item in packages)
    invalid_packages = sum(item.validation_result == "invalid" for item in packages)
    attempts = tuple(
        TranslationAttempt.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in state.path.glob("jobs/*/attempts/*/attempt.yaml")
    )
    table = Table(title="Translation Run Summary", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for label, value in (
        ("Run ID", run.run_id),
        ("Snapshot ID", run.snapshot_id),
        ("Status", run.status.value),
        ("Queue valid", "yes" if state.validation.valid else "no"),
        ("Snapshot valid", "yes" if state.snapshot_valid else "no"),
        ("Jobs", run.job_count),
        ("Pending", run.pending_count),
        ("Claimed", run.claimed_count),
        ("Running", run.running_count),
        ("Succeeded", run.succeeded_count),
        ("Provider succeeded, unassessed", run.provider_succeeded_unassessed_count),
        ("Accepted", run.accepted_count),
        ("QA rejected", run.qa_rejected_count),
        ("Failed", run.failed_count),
        ("Retry pending", run.retry_pending_count),
        ("Permanent failures", permanent),
        ("Prompt packages rendered", rendered_packages),
        ("Prompt packages valid", valid_packages),
        ("Prompt packages stale or invalid", invalid_packages),
        ("Provider attempts", len(attempts)),
        (
            "Successful provider attempts",
            sum(item.status is TranslationAttemptStatus.SUCCEEDED for item in attempts),
        ),
        (
            "Failed provider attempts",
            sum(item.status is TranslationAttemptStatus.FAILED for item in attempts),
        ),
        (
            "Ambiguous provider attempts",
            sum(item.status is TranslationAttemptStatus.AMBIGUOUS for item in attempts),
        ),
        ("Provider progress", f"{100 * run.succeeded_count / run.job_count:.2f}%"),
        ("Acceptance progress", f"{100 * run.accepted_count / run.job_count:.2f}%"),
    ):
        table.add_row(str(label), str(value))
    console.print(table)


def _chapter_filter(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"prologue", "0", "000-prologue"}:
        return "000-prologue"
    if normalized.startswith("chapter-"):
        normalized = normalized.removeprefix("chapter-")
    if normalized.isdigit() and 1 <= int(normalized) <= 999:
        return f"{int(normalized):03d}"
    raise QueueError(f"invalid chapter selector {value!r}")


@app.command("list-translation-jobs")
def list_translation_jobs_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    status: Annotated[TranslationJobStatus | None, typer.Option("--status")] = None,
    chapter: Annotated[str | None, typer.Option("--chapter")] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    acceptance_status: Annotated[
        TranslationAcceptanceStatus | None, typer.Option("--acceptance-status")
    ] = None,
) -> None:
    """List structural queue jobs without content or claim tokens."""
    try:
        jobs = list_jobs(
            run_id_or_path,
            project_root=Path.cwd(),
            status=status,
            chapter_id=_chapter_filter(chapter),
            limit=None if acceptance_status is not None else limit,
        )
        if acceptance_status is not None:
            jobs = tuple(job for job in jobs if job.acceptance_status is acceptance_status)
            if limit is not None:
                jobs = jobs[:limit]
    except (QueueError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Translation Jobs")
    for column in (
        "Job ID",
        "Chapter",
        "Chunk",
        "Priority",
        "Status",
        "Acceptance",
        "Attempts",
        "Worker",
        "Lease expires",
    ):
        table.add_column(column)
    rejected_findings: list[tuple[str, str]] = []
    for job in jobs:
        if job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED:
            if job.acceptance_file is None:
                rejected_findings.append((job.job_id, "missing_acceptance_evidence"))
            else:
                evidence = TranslationAcceptanceEvidence.model_validate(
                    yaml.safe_load((Path.cwd() / job.acceptance_file).read_text(encoding="utf-8"))
                )
                terminology_findings = [
                    f"{finding.code}:{finding.source_term}"
                    for finding in evidence.terminology_report.findings
                ]
                structural_findings = [
                    finding.code for finding in evidence.structural_report.findings
                ]
                rejected_findings.append(
                    (job.job_id, ", ".join(terminology_findings + structural_findings))
                )
        table.add_row(
            job.job_id,
            job.chapter_id,
            job.chunk_id,
            str(job.priority),
            job.status.value,
            job.acceptance_status.value if job.acceptance_status else "unassessed",
            f"{job.provider_attempt_count}/{job.max_attempts}",
            job.claimed_by or "",
            job.lease_expires_at.isoformat() if job.lease_expires_at else "",
        )
    console.print(table)
    for job_id, findings in rejected_findings:
        console.print(f"QA findings {job_id}: {findings}")


def _render_worker_progress(progress: WorkerProgress) -> None:
    """Print one finished job and the pool's running totals; identifiers and counts only.

    This is the cheap monitoring surface for a multi-worker pass: everything printed comes from
    results the slots returned, so watching it costs nothing on the queue. Markup is off because
    the job ID is bracketed by the slot tag.
    """
    latest = progress.latest
    if latest is not None and latest.worker_slot is not None:
        console.print(
            f"[worker-{latest.worker_slot}] {latest.job_id} {latest.outcome}",
            markup=False,
            highlight=False,
        )
    failed = progress.provider_failed + progress.permanent_failure + progress.retry_pending
    console.print(
        f"Workers: {progress.workers}  Active: {progress.active}"
        f"  Completed: {progress.completed} / {progress.planned}"
        f"  Accepted: {progress.accepted}  QA rejected: {progress.qa_rejected}"
        f"  Failed: {failed}  Errors: {progress.errors}"
        + ("  Stopping" if progress.stopping else ""),
        markup=False,
        highlight=False,
    )


@app.command("run-nightly-translation")
def run_nightly_translation_command(
    run_id: Annotated[str, typer.Argument()],
    max_units: Annotated[int, typer.Option("--max-units")] = 3,
) -> None:
    """Resume one canonical run during the local 22:00-05:00 window.

    The coordinator is intentionally translation-first: one codex-cli request at a time, at most
    three source-order narrative units per invocation, no semantic QA, repair, or automatic retry.
    """
    from book_translator.nightly_translation import (
        NightlyTranslationError,
        run_nightly_translation,
    )
    from book_translator.translation_queue.locking import QueueLockTimeout

    try:
        checkpoint = run_nightly_translation(run_id, project_root=Path.cwd(), max_units=max_units)
    except (NightlyTranslationError, QueueError, QueueLockTimeout, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Nightly Translation", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Run", checkpoint.translation_run_id)
    table.add_row("Provider", checkpoint.provider)
    table.add_row(
        "Translated coverage",
        f"{checkpoint.translated_chunks_count}/{checkpoint.total_chunks}",
    )
    table.add_row("Complete units", str(len(checkpoint.units_with_complete_translated_coverage)))
    table.add_row("Accepted", str(checkpoint.accepted_count))
    table.add_row("QA rejected (kept)", str(checkpoint.qa_rejected_count))
    table.add_row("Missing", str(checkpoint.missing_translation_count))
    table.add_row("Next unit", checkpoint.next_unit or "none")
    table.add_row("Next safe action", checkpoint.next_safe_action)
    console.print(table)


@app.command("run-translation-worker")
def run_translation_worker_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    provider: Annotated[ProviderName, typer.Option("--provider")],
    worker_id: Annotated[str | None, typer.Option("--worker-id")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    job_id: Annotated[str | None, typer.Option("--job-id")] = None,
    chapter: Annotated[str | None, typer.Option("--chapter")] = None,
    max_jobs: Annotated[int, typer.Option("--max-jobs")] = 1,
    workers: Annotated[int, typer.Option("--workers")] = 1,
    lease_seconds: Annotated[int, typer.Option("--lease-seconds")] = 900,
    timeout_seconds: Annotated[int | None, typer.Option("--timeout-seconds")] = None,
    max_output_tokens: Annotated[int | None, typer.Option("--max-output-tokens")] = None,
    reasoning_effort: Annotated[str | None, typer.Option("--reasoning-effort")] = None,
    store_raw_response: Annotated[
        bool, typer.Option("--store-raw-response/--no-store-raw-response")
    ] = True,
    retry_provider_failures: Annotated[
        bool, typer.Option("--retry-provider-failures/--no-retry-provider-failures")
    ] = True,
    stop_on_error: Annotated[bool, typer.Option("--stop-on-error/--continue-on-error")] = False,
    auto_repair: Annotated[bool, typer.Option("--auto-repair/--no-auto-repair")] = False,
    include_qa_rejected: Annotated[bool, typer.Option("--include-qa-rejected")] = False,
    max_repair_attempts: Annotated[
        int, typer.Option("--max-repair-attempts")
    ] = DEFAULT_MAX_REPAIR_ATTEMPTS,
    auto_recover_provider_failures: Annotated[
        bool,
        typer.Option(
            "--auto-recover-provider-failures/--no-auto-recover-provider-failures",
        ),
    ] = False,
    include_failed: Annotated[bool, typer.Option("--include-failed")] = False,
    max_provider_recovery_attempts: Annotated[
        int, typer.Option("--max-provider-recovery-attempts")
    ] = DEFAULT_MAX_PROVIDER_RECOVERY_ATTEMPTS,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Process provider drafts and QA acceptance without exposing claim tokens.

    --workers N (default 1) runs N execution
    slots as separate processes under one
    coordinator, each claiming through the queue
    lock and processing one job at a time.
    --max-jobs stays a global limit across all
    slots; --stop-on-error stops dispatching new
    jobs and lets in-flight jobs finish; one
    explicit --job-id always runs alone. Start
    with 2-4 workers: provider-side concurrency
    limits usually bind before CPU does.

    Use --job-id to process exactly one explicitly
    selected job, with no fallback to any other job,
    or --chapter to restrict selection to one chapter
    in canonical queue order. The two are mutually
    exclusive because a job ID already identifies
    exactly one job.

    --auto-repair lets the worker answer a QA
    rejection of a job it just processed with a
    bounded policy-governed repair attempt.
    --include-qa-rejected additionally selects jobs
    that were already QA-rejected, after all pending
    translation work, and requires --auto-repair.

    --auto-recover-provider-failures lets the worker
    answer a provider failure with a bounded
    policy-governed recovery attempt, once a fresh
    preflight re-establishes the provider. It never
    rewrites the historical attempt classification.
    --include-failed additionally selects jobs that
    already failed, after all pending translation and
    repair work, and requires
    --auto-recover-provider-failures.

    A provider failure that ended a *repair* is
    recovered as a repair: the same request is
    re-derived, proven byte-identical, and re-sent
    with the same execution kind and the same
    repaired-from lineage. It spends a provider
    attempt and a recovery attempt, but no new QA
    repair decision. Selecting one needs both
    --auto-recover-provider-failures --include-failed
    and --auto-repair, because it is both a recovery
    and a repair execution; these jobs are offered
    last, after pending translations, QA repairs, and
    failed translations.
    """
    try:
        config = _run_book_config(run_id_or_path)
        result = run_translation_worker(
            run_id_or_path,
            project_root=Path.cwd(),
            options=WorkerOptions(
                provider=provider,
                model=model,
                job_id=job_id,
                chapter_id=_chapter_filter(chapter),
                worker_id=worker_id or generated_worker_id(),
                max_jobs=max_jobs,
                workers=workers,
                lease_seconds=lease_seconds,
                timeout_seconds=timeout_seconds or config.provider_timeout_seconds,
                max_output_tokens=max_output_tokens or config.provider_max_output_tokens,
                reasoning_effort=reasoning_effort,
                store_raw_response=store_raw_response,
                retry_provider_failures=retry_provider_failures,
                stop_on_error=stop_on_error,
                auto_repair=auto_repair,
                include_qa_rejected=include_qa_rejected,
                max_repair_attempts=max_repair_attempts,
                auto_recover_provider_failures=auto_recover_provider_failures,
                include_failed=include_failed,
                max_provider_recovery_attempts=max_provider_recovery_attempts,
                dry_run=dry_run,
            ),
            on_progress=_render_worker_progress,
        )
    except (
        TranslationWorkerError,
        ProviderRecoveryError,
        ExecutorError,
        QueueError,
        ProjectDataError,
        OSError,
    ) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Translation Worker", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for label, value in (
        ("Run ID", result.run_id),
        ("Worker ID", result.worker_id),
        ("Workers", result.workers),
        ("Worker failures", result.worker_failures),
        ("Processed", result.processed),
        ("Accepted", result.accepted),
        ("QA rejected", result.qa_rejected),
        ("Provider failed", result.provider_failed),
        ("Retry pending", result.retry_pending),
        ("Permanent failure", result.permanent_failure),
        ("Errors", result.errors),
        ("Auto-repair", "on" if auto_repair else "off"),
        ("Auto-repair policy", AUTO_REPAIR_POLICY_VERSION),
        ("Max repair attempts", max_repair_attempts),
        ("Repair attempts", result.repair_attempts),
        ("Auto-repaired jobs", result.auto_repaired),
        ("Provider recovery", "on" if auto_recover_provider_failures else "off"),
        ("Provider recovery policy", PROVIDER_RECOVERY_POLICY_VERSION),
        ("Max provider recovery attempts", max_provider_recovery_attempts),
        ("Provider recovery attempts", result.provider_recovery_attempts),
        ("Provider-recovered jobs", result.provider_recovered),
        ("Repair decisions", result.repair_decisions),
        ("Repair recovery attempts", result.repair_recovery_attempts),
        ("Repair-recovered jobs", result.repair_recovered),
        ("Dry run", "yes" if result.dry_run else "no"),
    ):
        table.add_row(str(label), str(value))
    console.print(table)
    if job_id is not None or chapter is not None or auto_repair or auto_recover_provider_failures:
        selected = Table(title="Selected Jobs")
        for column in ("Job ID", "Outcome", "Attempt", "Repairs"):
            selected.add_column(column)
        for item in result.jobs:
            selected.add_row(
                item.job_id,
                item.outcome,
                str(item.attempt_number) if item.attempt_number is not None else "",
                str(item.repair_attempts),
            )
        console.print(selected)
        # Printed rather than tabulated: a truncated verdict or finding code is the one thing an
        # operator reading this actually needs in full.
        for item in result.jobs:
            if item.auto_repair_verdict is None:
                continue
            findings = ", ".join(item.auto_repair_findings) or "none"
            console.print(
                f"Auto-repair {item.job_id}: verdict={item.auto_repair_verdict} findings={findings}"
            )
        for item in result.jobs:
            if item.provider_recovery_verdict is None:
                continue
            console.print(
                f"Provider recovery {item.job_id}:"
                f" verdict={item.provider_recovery_verdict}"
                f" failure_class={item.provider_failure_class or 'unknown'}"
                f" kind={item.recovered_execution_kind or 'translation'}"
                f" recoveries={item.provider_recovery_attempts}"
                f" provider_contacted={'yes' if item.provider_contacted else 'no'}"
                + (
                    f" repaired_from={item.repair_root_attempt}"
                    if item.repair_root_attempt is not None
                    else ""
                )
            )
    # Printed for every worker pass, targeted or not: an error count without its cause is exactly
    # what an operator cannot act on. The text is structural (a failure class and its message),
    # never prose or claim material, and markup is off so bracketed lists survive intact.
    for item in result.jobs:
        if item.error is not None:
            console.print(
                f"Error {item.job_id}: {item.error}",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
    if result.errors and stop_on_error:
        raise typer.Exit(code=1)


@app.command("claim-translation-job")
def claim_translation_job_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    worker_id: Annotated[str, typer.Option("--worker-id")],
    lease_seconds: Annotated[int, typer.Option("--lease-seconds")] = 900,
    chapter: Annotated[str | None, typer.Option("--chapter")] = None,
    job_id: Annotated[str | None, typer.Option("--job-id")] = None,
    now: Annotated[str | None, typer.Option("--now", hidden=True)] = None,
) -> None:
    """Atomically claim at most one eligible structural queue job."""
    try:
        result = claim_job(
            run_id_or_path,
            project_root=Path.cwd(),
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            chapter_id=_chapter_filter(chapter),
            job_id=job_id,
            now=_parse_internal_now(now),
        )
    except (QueueError, OSError, TimeoutError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[bold]Job ID:[/bold] {result.job.job_id}")
    console.print(f"[bold]Chapter ID:[/bold] {result.job.chapter_id}")
    console.print(f"[bold]Chunk ID:[/bold] {result.job.chunk_id}")
    console.print(f"[bold]Attempt:[/bold] {result.job.attempt_count}/{result.job.max_attempts}")
    lease_expires = result.job.lease_expires_at
    console.print(
        f"[bold]Lease expires:[/bold] {lease_expires.isoformat() if lease_expires else 'none'}"
    )
    console.print(f"[bold]Claim token:[/bold] {result.claim_token}")


@app.command("release-translation-job")
def release_translation_job_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str, typer.Argument()],
    worker_id: Annotated[str, typer.Option("--worker-id")],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    now: Annotated[str | None, typer.Option("--now", hidden=True)] = None,
) -> None:
    """Administratively release a claim owned by the specified worker."""
    try:
        job = release_job(
            run_id_or_path,
            job_id,
            project_root=Path.cwd(),
            worker_id=worker_id,
            reason=reason,
            now=_parse_internal_now(now),
        )
    except (QueueError, OSError, TimeoutError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Released {job.job_id} to pending; attempt count remains {job.attempt_count}.")


@app.command("retry-translation-job")
def retry_translation_job_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option("--reason")],
    now: Annotated[str | None, typer.Option("--now", hidden=True)] = None,
) -> None:
    """Schedule an eligible failed job for retry after the configured backoff."""
    try:
        job = retry_job(
            run_id_or_path,
            job_id,
            project_root=Path.cwd(),
            reason=reason,
            now=_parse_internal_now(now),
        )
    except (QueueError, OSError, TimeoutError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    retry_at = job.next_retry_at
    console.print(
        f"Scheduled {job.job_id} for retry at {retry_at.isoformat() if retry_at else 'none'}."
    )


@app.command("restore-translation-repair-target")
def restore_translation_repair_target_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option("--reason")],
    successful_attempt: Annotated[int | None, typer.Option("--successful-attempt")] = None,
    apply: Annotated[bool, typer.Option("--apply")] = False,
    now: Annotated[str | None, typer.Option("--now", hidden=True)] = None,
) -> None:
    """Return one failed repair to the standing QA rejection it was answering.

    Plans without writing by default; --apply performs the transition. Nothing is sent to a
    provider and no attempt, acceptance, reservation or resolution record is touched. Omit
    --successful-attempt when the standing draft is a carry-forward, which has no successful
    provider attempt behind it; the settled completion is always read from immutable acceptance
    evidence rather than supplied.
    """
    try:
        plan = plan_repair_target_restoration(
            run_id_or_path,
            job_id,
            project_root=Path.cwd(),
            successful_attempt=successful_attempt,
        )
        result = (
            restore_repair_target(
                run_id_or_path,
                job_id,
                project_root=Path.cwd(),
                successful_attempt=successful_attempt,
                reason=reason,
                now=_parse_internal_now(now),
            )
            if apply
            else plan
        )
    except (QueueError, OSError, TimeoutError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Repair Target Restoration", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for label, value in (
        ("Run ID", result.run_id),
        ("Job ID", result.job_id),
        ("Chunk", result.chunk_id),
        ("Settlement", result.settlement),
        ("Job status", f"{result.previous_status.value} -> {result.resulting_status.value}"),
        (
            "Acceptance status",
            result.acceptance_status.value if result.acceptance_status else "unassessed",
        ),
        (
            "Acceptance origin",
            result.acceptance_origin.value if result.acceptance_origin else "none",
        ),
        ("Accepted attempt", result.accepted_attempt),
        (
            "Carry-forward number",
            result.carry_forward_number if result.carry_forward_number is not None else "none",
        ),
        (
            "Successful attempt",
            f"null -> {result.successful_attempt}"
            if result.successful_attempt is not None
            else "null -> null",
        ),
        ("Completed at", f"null -> {result.completed_at.isoformat()}"),
        ("Attempt slots issued", f"{result.attempt_count} -> {result.attempt_count}"),
        (
            "Abandoned slots",
            f"{result.abandoned_attempt_count} -> {result.abandoned_attempt_count}",
        ),
        (
            "Provider attempts",
            f"{result.provider_attempt_count} -> {result.provider_attempt_count} "
            f"of {result.max_attempts}",
        ),
        ("Last attempt", result.last_attempt if result.last_attempt is not None else "none"),
        ("Provider calls", 0),
        ("Applied", "yes" if result.applied else "no (plan only)"),
    ):
        table.add_row(str(label), str(value))
    console.print(table)


@app.command("recover-translation-run")
def recover_translation_run_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    now: Annotated[str | None, typer.Option("--now", hidden=True)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Recover expired leases, due retries, counters, and derived run status safely."""
    try:
        result = recover_run(
            run_id_or_path, project_root=Path.cwd(), now=_parse_internal_now(now), dry_run=dry_run
        )
    except (QueueError, OSError, TimeoutError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Translation Run Recovery", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Run ID", result.run_id)
    table.add_row("Expired claims", str(len(result.expired_claims)))
    table.add_row("Due retries", str(len(result.due_retries)))
    table.add_row("Prepared executions recovered", str(len(result.prepared_executions)))
    table.add_row("Counter repair", "yes" if result.counter_repair else "no")
    table.add_row("Status repair", "yes" if result.status_repair else "no")
    table.add_row("Blockers", str(len(result.blockers)))
    table.add_row("Dry run", "yes" if result.dry_run else "no")
    console.print(table)
    if result.blockers:
        raise typer.Exit(code=1)


def _render_reservation_resolution(
    plan: ReservationResolutionPlan, result: ReservationResolutionResult
) -> None:
    from rich.table import Table

    table = Table(title="Execution Reservation Resolution", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for label, value in (
        ("Run ID", result.run_id),
        ("Job ID", result.job_id),
        ("Attempt", f"{result.attempt_number:03d}"),
        ("Resolution", result.resolution.value),
        ("Resolution ID", result.resolution_id),
        ("Resolver engine", EXECUTION_RESOLUTION_ENGINE_VERSION),
        ("Provenance", "operator"),
        ("Action", plan.action),
        ("Reservation file", result.reservation_file.as_posix()),
        ("Reservation SHA-256", result.reservation_sha256),
        ("Reservation status", plan.record.reservation_status.value),
        ("Reservation preserved", "yes"),
        ("Provider", plan.provider.value),
        ("Prompt package", plan.record.prompt_package_id),
        ("Request SHA-256", plan.record.request_sha256),
        (
            "Job status",
            f"{result.previous_job_status.value} -> {result.resulting_job_status.value}",
        ),
        (
            "Attempt slots issued",
            f"{plan.previous_attempt_count} -> {plan.resulting_attempt_count}",
        ),
        (
            "Abandoned slots",
            f"{plan.previous_abandoned_attempt_count} -> {plan.resulting_abandoned_attempt_count}",
        ),
        (
            "Provider attempts",
            f"{result.previous_provider_attempt_count} -> "
            f"{result.resulting_provider_attempt_count} of {plan.max_attempts}",
        ),
        ("Provider attempt consumed", "yes" if plan.provider_attempt_consumed else "no"),
        ("Replay permitted", "yes" if result.replay_permitted else "no"),
        ("Semantics", plan.semantics),
        ("Resolution lineage", result.resolution_file.as_posix()),
        (
            "Lineage published" if not result.dry_run else "Would publish lineage",
            "yes" if result.lineage_published else "no",
        ),
        (
            "Job reconciled" if not result.dry_run else "Would reconcile job",
            "yes" if result.job_reconciled else "no",
        ),
        ("Applied", "no (plan only)" if result.dry_run else "yes"),
    ):
        table.add_row(str(label), str(value))
    console.print(table)
    for warning in plan.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")


@app.command("resolve-translation-reservation")
def resolve_translation_reservation_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str, typer.Argument()],
    attempt_number: Annotated[int, typer.Option("--attempt-number")],
    resolution: Annotated[ExecutionReservationResolutionKind, typer.Option("--resolution")],
    reason: Annotated[str, typer.Option("--reason")],
    apply: Annotated[bool, typer.Option("--apply")] = False,
    now: Annotated[str | None, typer.Option("--now", hidden=True)] = None,
) -> None:
    """Resolve one incomplete execution reservation with an explicit operator assertion.

    Plans without writing by default; --apply publishes immutable resolution lineage beside the
    untouched reservation and applies only the accounting the assertion justifies. The reason is
    audit metadata and must never contain book prose.
    """
    try:
        plan = plan_reservation_resolution(
            run_id_or_path,
            job_id,
            attempt_number=attempt_number,
            resolution=resolution,
            reason=reason,
            project_root=Path.cwd(),
            now=_parse_internal_now(now),
        )
        result = (
            execute_reservation_resolution(
                plan, project_root=Path.cwd(), now=_parse_internal_now(now)
            )
            if apply
            else planned_result(plan)
        )
    except (ExecutionResolutionError, QueueError, OSError, TimeoutError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _render_reservation_resolution(plan, result)


@app.command("inspect-translation-reservations")
def inspect_translation_reservations_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str | None, typer.Option("--job-id")] = None,
) -> None:
    """List execution reservations, their resolutions, and whether replay is permitted."""
    try:
        reservations = inspect_execution_reservations(
            run_id_or_path, project_root=Path.cwd(), job_id=job_id
        )
    except (ExecutionResolutionError, QueueError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Execution Reservations")
    for column in (
        "Job ID",
        "Attempt",
        "Job status",
        "Reservation",
        "Provider",
        "Automatic recovery",
        "Resolution",
        "Replay",
        "Integrity",
        "Resolution ID",
    ):
        table.add_column(column)
    for item in reservations:
        table.add_row(
            item.job_id,
            f"{item.attempt_number:03d}",
            item.job_status.value,
            item.reservation_status.value,
            item.provider.value,
            "yes" if item.automatically_recoverable else "no",
            item.resolution.value if item.resolution else "unresolved",
            "yes" if item.replay_permitted else "no",
            item.integrity,
            item.resolution_id or "",
        )
    console.print(table)
    for item in reservations:
        if item.resolution is not None:
            console.print(
                f"{item.job_id} attempt {item.attempt_number:03d}: "
                f"{resolution_outcome(item.resolution).summary}"
            )
    if not reservations:
        console.print("No execution reservations exist for this run.")


def _render_prompt_package_plan(plan: PromptPackagePlan) -> None:
    from rich.table import Table

    table = Table(title="Prompt Package Plan")
    for column in (
        "Job ID",
        "Chapter",
        "Chunk",
        "Package ID",
        "Source words",
        "Context words",
        "System chars",
        "User chars",
        "Estimated tokens",
        "Action",
    ):
        table.add_column(column)
    for item in plan.items:
        package = item.package
        table.add_row(
            item.job.job_id,
            item.job.chapter_id,
            item.job.chunk_id,
            package.package_id,
            str(package.source_word_count),
            str(package.context_word_count),
            str(package.system_message.character_count),
            str(package.user_message.character_count),
            str(package.estimated_input_tokens),
            item.action,
        )
    console.print(table)


@app.command("render-prompt-packages")
def render_prompt_packages_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str | None, typer.Option("--job-id")] = None,
    chapter: Annotated[str | None, typer.Option("--chapter")] = None,
    status: Annotated[TranslationJobStatus | None, typer.Option("--status")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    show_plan: Annotated[bool, typer.Option("--show-plan")] = False,
) -> None:
    """Render deterministic provider-ready inputs without executing translation."""
    try:
        plan = plan_prompt_packages(
            run_id_or_path,
            project_root=Path.cwd(),
            job_id=job_id,
            chapter_id=_chapter_filter(chapter),
            status=status,
            force=force,
        )
        execution = None if dry_run else execute_prompt_packages(plan)
    except (PromptPackageError, QueueError, OSError, UnicodeError, TimeoutError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    if show_plan:
        _render_prompt_package_plan(plan)
    from rich.table import Table

    table = Table(title="Prompt Packages", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Run ID", plan.run_id)
    table.add_row("Selected jobs", str(len(plan.items)))
    table.add_row(
        "Planned renders", str(sum(item.action in {"render", "replace"} for item in plan.items))
    )
    table.add_row("Rendered", str(len(execution.rendered_job_ids) if execution is not None else 0))
    table.add_row("Skipped", str(sum(item.action == "skip" for item in plan.items)))
    table.add_row("Stale", str(sum(item.action == "stale" for item in plan.items)))
    table.add_row("Conflicts", str(sum(item.action == "conflict" for item in plan.items)))
    table.add_row(
        "Estimated input tokens",
        str(sum(item.package.estimated_input_tokens for item in plan.items)),
    )
    table.add_row("Output bytes", str(execution.output_bytes if execution else 0))
    table.add_row("Dry run", "yes" if dry_run else "no")
    console.print(table)


@app.command("validate-prompt-package")
def validate_prompt_package_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str, typer.Argument()],
) -> None:
    """Live-revalidate one prompt package without writing."""
    try:
        report = validate_prompt_package(run_id_or_path, job_id, project_root=Path.cwd())
    except (PromptPackageError, QueueError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Prompt Package Validation", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Package ID", report.package_id)
    table.add_row("Valid", "yes" if report.valid else "no")
    table.add_row("Snapshot valid", "yes" if report.snapshot_valid else "no")
    table.add_row("Queue job valid", "yes" if report.queue_job_valid else "no")
    table.add_row("Resources valid", "yes" if report.resources_valid else "no")
    table.add_row("Source conserved", "yes" if report.source_conserved else "no")
    table.add_row("Context conserved", "yes" if report.context_conserved else "no")
    table.add_row("Errors", str(len(report.errors)))
    console.print(table)
    if not report.valid:
        raise typer.Exit(code=1)


@app.command("list-prompt-packages")
def list_prompt_packages_command(
    run_id_or_path: Annotated[str, typer.Argument()],
) -> None:
    """List structural prompt-package metadata without message content."""
    try:
        items = list_prompt_packages(run_id_or_path, project_root=Path.cwd())
    except (PromptPackageError, QueueError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Prompt Packages")
    for column in (
        "Job ID",
        "Package ID",
        "Status",
        "Renderer",
        "Prompt",
        "Glossary",
        "Style",
        "Estimated tokens",
        "Validation",
    ):
        table.add_column(column)
    for item in items:
        package = item.package
        table.add_row(
            item.job_id,
            package.package_id if package else "",
            package.status.value if package else "missing",
            package.renderer_version if package else "",
            package.prompt.version if package else "",
            package.glossary.version if package else "",
            package.style_guide.version if package else "",
            str(package.estimated_input_tokens) if package else "",
            item.validation_result,
        )
    console.print(table)


def _run_book_config(run_id_or_path: str) -> BookConfig:
    """Load the configuration of the book a translation run belongs to, never a fixed file.

    Runs are stored project-wide, so the run ID alone does not say which book it serves; its
    frozen lineage does. A legacy-root run resolves `config/book.yaml` exactly as before and a
    book-scoped run resolves `config/books/<book>.yaml`.
    """
    project_root = Path.cwd()
    book = resolve_run_book(run_id_or_path, project_root=project_root)
    return load_book_config(ProjectPaths(project_root, book).book_config)


def _require_provider_preflight(provider: ProviderName, *, dry_run: bool) -> None:
    """Refuse an unusable provider before a claim is taken or an attempt is consumed."""
    if dry_run:
        return
    try:
        preflight_provider(provider)
    except ProviderExecutionError as exc:
        raise ExecutorError(
            f"provider preflight failed for {provider.value}: {exc.classification.message}"
        ) from exc


@app.command("check-translation-provider")
def check_translation_provider_command(
    provider: Annotated[ProviderName, typer.Option("--provider")],
) -> None:
    """Check local provider readiness without claiming a job or sending any book content."""
    try:
        report = preflight_provider(provider)
    except ProviderExecutionError as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc.classification.message}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Translation Provider Preflight", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Provider", report.provider)
    table.add_row("Preflight implemented", "yes" if report.supported else "no")
    table.add_row("Passed", "yes" if report.passed else "no")
    for label, value in report.details:
        table.add_row(label.replace("_", " ").capitalize(), value)
    console.print(table)


@app.command("execute-translation-job")
def execute_translation_job_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str, typer.Argument()],
    worker_id: Annotated[str, typer.Option("--worker-id")],
    claim_token: Annotated[str, typer.Option("--claim-token")],
    provider: Annotated[ProviderName, typer.Option("--provider")],
    model: Annotated[str | None, typer.Option("--model")] = None,
    timeout_seconds: Annotated[int | None, typer.Option("--timeout-seconds")] = None,
    max_output_tokens: Annotated[int | None, typer.Option("--max-output-tokens")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    no_provider_retry: Annotated[bool, typer.Option("--no-provider-retry")] = True,
    reasoning_effort: Annotated[str | None, typer.Option("--reasoning-effort")] = None,
    store_raw_response: Annotated[
        bool, typer.Option("--store-raw-response/--no-store-raw-response")
    ] = True,
    fake_scenario: Annotated[str, typer.Option("--fake-scenario", hidden=True)] = "success",
) -> None:
    """Execute exactly one claimed job through an explicitly selected provider."""
    try:
        config = _run_book_config(run_id_or_path)
        timeout = timeout_seconds or config.provider_timeout_seconds
        output_limit = max_output_tokens or config.provider_max_output_tokens
        if not no_provider_retry:
            raise ExecutorError("provider SDK retries remain disabled in executor version 1")
        _require_provider_preflight(provider, dry_run=dry_run)
        plan = plan_execution(
            run_id_or_path,
            job_id,
            project_root=Path.cwd(),
            worker_id=worker_id,
            claim_token=claim_token,
            provider=provider,
            model=model,
            timeout_seconds=timeout,
            max_output_tokens=output_limit,
            reasoning_effort=reasoning_effort,
            store_raw_response=store_raw_response,
            sdk_retry_count=0,
        )
        result = execute_translation_job(
            plan,
            worker_id=worker_id,
            claim_token=claim_token,
            fake_scenario=fake_scenario,
            dry_run=dry_run,
        )
    except (ExecutorError, QueueError, ProjectDataError, OSError, TimeoutError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Translation Provider Execution", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for label, value in (
        ("Run ID", result.run_id),
        ("Job ID", result.job_id),
        ("Attempt", result.attempt_number),
        ("Provider", provider.value),
        ("Model", model),
        ("Request ID", result.request_id),
        ("Status", result.attempt_status.value if result.attempt_status else "planned"),
        ("Provider contacted", "yes" if result.provider_contacted else "no"),
        ("Dry run", "yes" if result.dry_run else "no"),
    ):
        table.add_row(str(label), str(value))
    console.print(table)


@app.command("repair-translation-job")
def repair_translation_job_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str, typer.Argument()],
    provider: Annotated[ProviderName, typer.Option("--provider")],
    model: Annotated[str | None, typer.Option("--model")] = None,
    worker_id: Annotated[str | None, typer.Option("--worker-id")] = None,
    lease_seconds: Annotated[int, typer.Option("--lease-seconds")] = 900,
    timeout_seconds: Annotated[int | None, typer.Option("--timeout-seconds")] = None,
    max_output_tokens: Annotated[int | None, typer.Option("--max-output-tokens")] = None,
    reasoning_effort: Annotated[str | None, typer.Option("--reasoning-effort")] = None,
    store_raw_response: Annotated[
        bool, typer.Option("--store-raw-response/--no-store-raw-response")
    ] = True,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Explicitly repair one QA-rejected provider-successful translation."""
    try:
        config = _run_book_config(run_id_or_path)
        _require_provider_preflight(provider, dry_run=dry_run)
        result = repair_translation_job(
            run_id_or_path,
            job_id,
            project_root=Path.cwd(),
            provider=provider,
            model=model,
            worker_id=worker_id or generated_worker_id(),
            lease_seconds=lease_seconds,
            timeout_seconds=timeout_seconds or config.provider_timeout_seconds,
            max_output_tokens=max_output_tokens or config.provider_max_output_tokens,
            reasoning_effort=reasoning_effort,
            store_raw_response=store_raw_response,
            dry_run=dry_run,
        )
    except (TranslationRepairError, ExecutorError, QueueError, ProjectDataError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Translation Repair", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for label, value in (
        ("Run ID", result.run_id),
        ("Job ID", result.job_id),
        ("Execution kind", "repair"),
        ("Initiator", result.initiator.value),
        (
            "Repaired from",
            f"attempt {result.repaired_from_attempt}"
            if result.repaired_from_attempt is not None
            else f"carry-forward {result.repaired_from_carry_forward}",
        ),
        ("Attempt", result.attempt_number),
        ("Provider", result.provider.value),
        ("Provider status", result.attempt_status.value if result.attempt_status else "planned"),
        (
            "Acceptance",
            result.acceptance_status.value if result.acceptance_status else "unassessed",
        ),
        ("Provider contacted", "yes" if result.provider_contacted else "no"),
        ("Dry run", "yes" if result.dry_run else "no"),
    ):
        table.add_row(str(label), str(value))
    console.print(table)


@app.command("reevaluate-translation-job")
def reevaluate_translation_job_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str, typer.Argument()],
    attempt_number: Annotated[int, typer.Option("--attempt")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Re-run current local QA against one immutable provider-successful attempt."""
    try:
        result = reevaluate_translation_job(
            run_id_or_path,
            job_id,
            attempt_number,
            project_root=Path.cwd(),
            dry_run=dry_run,
        )
    except (TranslationReevaluationError, ExecutorError, QueueError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Translation QA Re-evaluation", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for label, value in (
        ("Run ID", result.run_id),
        ("Job ID", result.job_id),
        ("Attempt", result.attempt_number),
        ("Re-evaluation ID", result.reevaluation_id),
        ("Re-evaluation number", result.reevaluation_number),
        ("Previous acceptance", result.previous_status.value),
        ("Resulting acceptance", result.resulting_status.value),
        ("Terminology QA version", result.terminology_qa_version),
        ("Structural QA version", result.structural_qa_version),
        ("Terminology findings", ", ".join(result.terminology_finding_codes) or "none"),
        ("Structural findings", ", ".join(result.structural_finding_codes) or "none"),
        ("Evidence", result.evidence_file or "not written"),
        ("Reused", "yes" if result.reused else "no"),
        ("Provider contacted", "no"),
        ("Dry run", "yes" if result.dry_run else "no"),
    ):
        table.add_row(str(label), str(value))
    console.print(table)


@app.command("show-translation-reevaluations")
def show_translation_reevaluations_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str, typer.Argument()],
) -> None:
    """Inspect historical and effective acceptance without translation prose."""
    try:
        history = inspect_translation_reevaluations(run_id_or_path, job_id, project_root=Path.cwd())
    except (TranslationReevaluationError, ExecutorError, QueueError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    summary = Table(title="Translation Acceptance Resolution", show_header=False)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    summary.add_row("Historical acceptance", history.historical_status.value)
    summary.add_row("Effective acceptance", history.effective_status.value)
    summary.add_row("Acceptance origin", history.acceptance_origin.value)
    summary.add_row("Attempt", str(history.attempt_number))
    summary.add_row("Re-evaluations", str(len(history.records)))
    console.print(summary)
    records = Table(title="QA Re-evaluation History")
    for column in (
        "Number",
        "ID",
        "Attempt",
        "Previous",
        "Result",
        "Terminology QA",
        "Structural QA",
        "Reason",
        "Created",
    ):
        records.add_column(column)
    for evidence in history.records:
        records.add_row(
            str(evidence.reevaluation_number),
            evidence.reevaluation_id,
            str(evidence.attempt_number),
            evidence.previous_status.value,
            evidence.resulting_status.value,
            evidence.terminology_qa_version,
            evidence.structural_qa_version,
            evidence.origin,
            evidence.created_at.isoformat(),
        )
    console.print(records)


@app.command("reevaluate-historical-output")
def reevaluate_historical_output_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    from_run: Annotated[str, typer.Option("--from-run")],
    job_id: Annotated[str, typer.Option("--job")],
    attempt_number: Annotated[int, typer.Option("--attempt")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Re-evaluate one historically QA-rejected provider output in a newer run."""
    from rich.table import Table

    try:
        result = reevaluate_historical_output(
            run_id_or_path,
            from_run=from_run,
            job_id=job_id,
            attempt_number=attempt_number,
            project_root=Path.cwd(),
            dry_run=dry_run,
        )
    except (
        HistoricalReevaluationError,
        TranslationReevaluationError,
        QueueError,
        ExecutorError,
        OSError,
        ValueError,
    ) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    plan = result.plan
    summary = Table(title="Historical Output Re-evaluation", show_header=False)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    for label, value in (
        ("Destination run", plan.run_id),
        ("Destination job", plan.job_id),
        ("Source run", plan.source_run_id),
        ("Source job", plan.source_job_id or "not found"),
        ("Source attempt", plan.source_attempt_number),
        ("Outcome", plan.outcome),
        (
            "Compatibility tier",
            plan.tier.value if plan.verified_identity_checks else "not evaluated",
        ),
        ("Blocking reasons", ", ".join(plan.blocking_reasons) or "none"),
        ("Changed framed sections", ", ".join(plan.changed_framed_sections) or "none"),
        ("Observed resource drift", ", ".join(plan.observed_resource_drift) or "none"),
        ("Governed registry events", ", ".join(plan.governed_transition_ids) or "none"),
        ("Locked terminology drift", ", ".join(plan.locked_decision_drift) or "none"),
        (
            "Historical acceptance",
            plan.historical_status.value if plan.historical_status else "unknown",
        ),
        ("Historical blocking findings", ", ".join(plan.historical_finding_codes) or "none"),
        ("Historical findings resolved", ", ".join(plan.resolved_historical_findings) or "none"),
        (
            "Historical findings persisting",
            ", ".join(plan.persisting_historical_findings) or "none",
        ),
        ("Current terminology findings", ", ".join(plan.terminology_finding_codes) or "none"),
        ("Current structural findings", ", ".join(plan.structural_finding_codes) or "none"),
        (
            "Resulting acceptance",
            result.resulting_status.value if result.resulting_status else "not evaluated",
        ),
        ("Carry-forward evidence", result.evidence_file or "not written"),
        ("Acceptance evidence", result.acceptance_file or "not written"),
        ("Provider calls", result.provider_calls),
        ("Already applied", "yes" if result.reused else "no"),
        ("Dry run", "yes" if result.dry_run else "no"),
        ("Mutations", "none" if result.dry_run else result.published),
    ):
        summary.add_row(str(label), str(value))
    console.print(summary)


@app.command("reuse-accepted-translations")
def reuse_accepted_translations_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    from_run: Annotated[str, typer.Option("--from-run")],
    job_id: Annotated[str | None, typer.Option("--job")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Carry forward compatible accepted translations and run current QA."""
    from rich.table import Table

    try:
        result = reuse_accepted_translations(
            run_id_or_path,
            from_run=from_run,
            project_root=Path.cwd(),
            job_id=job_id,
            dry_run=dry_run,
        )
    except (TranslationReuseError, QueueError, ExecutorError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    summary = Table(title="Accepted Translation Reuse", show_header=False)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    for label, value in (
        ("New run", result.plan.run_id),
        ("Source run", result.plan.source_run_id),
        ("Jobs inspected", result.plan.inspected),
        ("Reusable", result.plan.reusable),
        (
            "Reusable by tier",
            ", ".join(f"{tier}={count}" for tier, count in result.plan.tiers.items()) or "none",
        ),
        ("Blocked/not compatible", result.plan.blocked),
        ("Already completed/skipped", result.plan.skipped),
        ("Published", result.published),
        ("Accepted by current QA", result.accepted),
        ("Rejected by current QA", result.qa_rejected),
        ("Provider calls", 0),
        ("Mutations", "none" if result.dry_run else result.published),
    ):
        summary.add_row(label, str(value))
    console.print(summary)
    reasons = Table(title="Blocking Incompatibility Reasons")
    reasons.add_column("Reason")
    reasons.add_column("Jobs")
    for reason, count in result.plan.reasons.items():
        reasons.add_row(reason, str(count))
    console.print(reasons)
    drift = Table(title="Observed Resource Drift (never blocking on its own)")
    drift.add_column("Resource difference")
    drift.add_column("Jobs")
    for reason, count in result.plan.resource_drift.items():
        drift.add_row(reason, str(count))
    console.print(drift)
    details = Table(title="Job Reuse Decisions")
    for column in (
        "Job",
        "Outcome",
        "Tier",
        "Blocking reasons",
        "Resource drift",
        "Old source",
        "New source",
        "Old context",
        "New context",
    ):
        details.add_column(column)
    for item in result.plan.jobs:
        details.add_row(
            item.chunk_id,
            item.outcome,
            item.tier.value if item.outcome == "reusable" else "-",
            ", ".join(item.reasons),
            ", ".join(item.resource_drift),
            item.source_sha256[0],
            item.source_sha256[1],
            item.context_sha256[0],
            item.context_sha256[1],
        )
    console.print(details)


@app.command("validate-translation-attempt")
def validate_translation_attempt_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str, typer.Argument()],
    attempt_number: Annotated[int, typer.Argument()],
) -> None:
    """Revalidate attempt lineage, hashes, state, and private permissions."""
    report = validate_attempt(run_id_or_path, job_id, attempt_number, project_root=Path.cwd())
    from rich.table import Table

    table = Table(title="Translation Attempt Validation", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Attempt ID", report.attempt_id)
    table.add_row("Valid", "yes" if report.valid else "no")
    table.add_row("Errors", str(len(report.errors)))
    table.add_row("Warnings", str(len(report.warnings)))
    console.print(table)
    if not report.valid:
        raise typer.Exit(code=1)


def _attempt_table(
    run_id_or_path: str, job_id: str, *, title: str, number: int | None = None
) -> None:
    from rich.table import Table

    attempts = list_attempts(run_id_or_path, job_id, project_root=Path.cwd())
    if number is not None:
        attempts = tuple(item for item in attempts if item.attempt.attempt_number == number)
        if not attempts:
            raise ExecutorError(f"translation attempt not found: {number}")
    table = Table(title=title)
    for column in (
        "Attempt",
        "Kind",
        "Repaired from",
        "Provider",
        "Model",
        "Status",
        "Latency ms",
        "Input tokens",
        "Output tokens",
        "Error",
        "Retryable",
        "Ambiguous",
        "Rejected output",
        "Marker findings",
        "Validation",
        "Completed",
    ):
        table.add_column(column)
    for item in attempts:
        attempt = item.attempt
        table.add_row(
            str(attempt.attempt_number),
            attempt.execution_kind.value,
            str(attempt.repaired_from_attempt or ""),
            attempt.provider.value,
            attempt.model_returned or attempt.model_requested,
            attempt.status.value,
            str(attempt.latency_milliseconds or ""),
            str(attempt.input_tokens or ""),
            str(attempt.output_tokens or ""),
            attempt.error_category.value if attempt.error_category else "",
            "yes" if attempt.retryable else "no",
            "yes" if attempt.ambiguous else "no",
            "yes" if attempt.rejected_output_file is not None else "",
            # Codes only: which marker failed is structural evidence, the answer itself is not.
            ", ".join(sorted({finding.code for finding in attempt.marker_findings})),
            item.validation,
            attempt.completed_at.isoformat() if attempt.completed_at else "",
        )
    console.print(table)


@app.command("list-translation-attempts")
def list_translation_attempts_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str, typer.Argument()],
) -> None:
    """List provider attempt metadata without request, response, or translation prose."""
    try:
        _attempt_table(run_id_or_path, job_id, title="Translation Attempts")
    except (ExecutorError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("show-translation-attempt")
def show_translation_attempt_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str, typer.Argument()],
    attempt_number: Annotated[int, typer.Argument()],
) -> None:
    """Show one attempt's structural metadata without sensitive content."""
    try:
        _attempt_table(
            run_id_or_path,
            job_id,
            title="Translation Attempt",
            number=attempt_number,
        )
    except (ExecutorError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


def _final_qa_summary(report: FinalQAAuditReport) -> None:
    """Render one final-QA audit without printing any source or translated text."""
    from rich.table import Table

    summary = Table(title="Final QA Audit", show_header=False)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    for label, value in (
        ("Run ID", report.run_id),
        ("Snapshot", report.snapshot_id),
        ("Policy version", report.final_qa_resolution_policy_version),
        ("Terminology QA", report.terminology_qa_version),
        ("Structural QA", report.structural_qa_version),
        ("Frozen registry is current", "yes" if report.frozen_registry_is_current else "no"),
        ("Jobs", report.job_count),
        ("Accepted", report.accepted_count),
        ("QA rejected", report.qa_rejected_count),
    ):
        summary.add_row(str(label), str(value))
    console.print(summary)

    verdicts = Table(title="Planned Resolution")
    verdicts.add_column("Verdict")
    verdicts.add_column("Jobs", justify="right")
    verdicts.add_column("Automatic")
    verdicts.add_column("Contacts provider")
    for verdict in FinalQAVerdict:
        count = report.verdict_counts.get(verdict.value, 0)
        if not count:
            continue
        verdicts.add_row(
            verdict.value,
            str(count),
            "yes" if verdict in ACTIONABLE_VERDICTS else "no",
            "no" if verdict in PROVIDER_FREE_ACTION_VERDICTS else "yes",
        )
    console.print(verdicts)

    structural = [job for job in report.jobs if job.structural is not None]
    if structural:
        blocks = Table(title="Structural Diagnosis")
        for column in (
            "Job",
            "Source blocks",
            "Translation blocks",
            "Lines",
            "Defect",
            "Deterministic",
            "Operations",
        ):
            blocks.add_column(column)
        for job in structural:
            record = job.structural
            assert record is not None
            diagnosis = record.diagnosis
            blocks.add_row(
                job.chunk_id,
                str(record.source_paragraphs),
                str(record.translation_paragraphs),
                "-"
                if diagnosis is None
                else f"{diagnosis.source_lines}/{diagnosis.translation_lines}",
                "-" if diagnosis is None else diagnosis.defect_class.value,
                "yes" if record.deterministically_normalizable else "no",
                "-" if diagnosis is None else str(len(diagnosis.operations)),
            )
        console.print(blocks)

    if report.variant_class_counts:
        classes = Table(title="Competing Variant Root Causes")
        classes.add_column("Class")
        classes.add_column("Findings", justify="right")
        for name, count in sorted(report.variant_class_counts.items()):
            classes.add_row(name, str(count))
        console.print(classes)

    grouped = operator_required_terminology(report)
    if grouped:
        questions = Table(title="Terminology Decisions Requiring an Operator")
        for column in ("Candidate", "Source term", "Competing variant", "Class", "Jobs"):
            questions.add_column(column)
        for candidate_id, source_term, variant, variant_class, count in grouped:
            questions.add_row(candidate_id, source_term, variant, variant_class.value, str(count))
        console.print(questions)

    locked = [job.job_id for job in report.jobs if job.locked_terminology_involved]
    console.print(f"Locked or user-owned terminology conflicts: {len(locked)}")

    jobs = Table(title="QA-Rejected Jobs")
    for column in ("Job", "Attempt", "Verdict", "Current blockers", "Reproduces", "Budget left"):
        jobs.add_column(column)
    for job in report.jobs:
        jobs.add_row(
            job.chunk_id,
            str(job.effective_attempt),
            job.verdict.value,
            ", ".join(job.current_blocking_codes) or "none",
            "yes" if job.reproduces_under_current_qa else "no",
            str(job.budget.remaining),
        )
    console.print(jobs)


@app.command("audit-final-qa")
def audit_final_qa_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Read-only diagnosis of every standing QA rejection in one translation run."""
    try:
        plan = plan_final_qa_audit(run_id_or_path, project_root=Path.cwd())
        written = None if dry_run else execute_final_qa_audit(plan, project_root=Path.cwd())
    except (FinalQAError, TerminologyError, ExecutorError, QueueError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _final_qa_summary(plan.report)
    console.print(f"Audit artifact: {written or 'not written'}")
    console.print("Provider contacted: no")


@app.command("finalize-translation-run")
def finalize_translation_run_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    dry_run: Annotated[bool, typer.Option("--dry-run/--apply")] = True,
    provider: Annotated[ProviderName | None, typer.Option("--provider")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    worker_id: Annotated[str | None, typer.Option("--worker-id")] = None,
    lease_seconds: Annotated[int, typer.Option("--lease-seconds")] = 900,
    timeout_seconds: Annotated[int | None, typer.Option("--timeout-seconds")] = None,
    max_output_tokens: Annotated[int | None, typer.Option("--max-output-tokens")] = None,
    reasoning_effort: Annotated[str | None, typer.Option("--reasoning-effort")] = None,
    max_cycles: Annotated[int, typer.Option("--max-cycles")] = DEFAULT_MAX_CYCLES,
    allow_repair_escalation: Annotated[bool, typer.Option("--allow-repair-escalation")] = False,
    provider_recovery: Annotated[
        bool, typer.Option("--provider-recovery/--no-provider-recovery")
    ] = True,
) -> None:
    """Resolve every standing QA rejection as far as governed evidence allows."""
    try:
        config = _run_book_config(run_id_or_path)
        if provider is not None and not dry_run:
            _require_provider_preflight(provider, dry_run=False)
        result = finalize_translation_run(
            run_id_or_path,
            project_root=Path.cwd(),
            dry_run=dry_run,
            provider=provider,
            model=model,
            worker_id=worker_id or generated_worker_id(),
            lease_seconds=lease_seconds,
            timeout_seconds=timeout_seconds or config.provider_timeout_seconds,
            max_output_tokens=max_output_tokens or config.provider_max_output_tokens,
            reasoning_effort=reasoning_effort,
            max_cycles=max_cycles,
            allow_repair_escalation=allow_repair_escalation,
            allow_provider_recovery=provider_recovery,
        )
    except (
        FinalQAError,
        StructuralRepairError,
        ProviderRecoveryError,
        AutoRepairError,
        TranslationReevaluationError,
        TranslationRepairError,
        TerminologyError,
        ExecutorError,
        QueueError,
        ProjectDataError,
        OSError,
        ValueError,
    ) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    _final_qa_summary(result.report.audit)
    outcomes = Table(title="Finalization Outcomes")
    for column in (
        "Job",
        "Verdict",
        "Planned",
        "Action",
        "Before",
        "After",
        "Structural",
        "Escalated",
        "Provider attempts",
    ):
        outcomes.add_column(column)
    for outcome in result.report.outcomes:
        outcomes.add_row(
            outcome.job_id.rpartition("__")[2],
            outcome.verdict.value,
            outcome.planned_action,
            outcome.action,
            outcome.acceptance_status_before,
            outcome.acceptance_status_after,
            str(outcome.structural_repairs_applied),
            "yes" if outcome.escalation_granted else "no",
            str(outcome.provider_attempts_spent),
        )
    console.print(outcomes)

    footer = Table(title="Run After Finalization", show_header=False)
    footer.add_column("Field", style="bold")
    footer.add_column("Value")
    for label, value in (
        ("Dry run", "yes" if result.report.dry_run else "no"),
        ("Provider calls", result.report.provider_calls),
        ("Structural repairs", result.report.structural_repairs_applied),
        ("Escalations granted", result.report.escalations_granted),
        ("Accepted", result.report.accepted_count),
        ("QA rejected", result.report.qa_rejected_count),
        ("Run status", result.report.run_status_after),
        ("Unresolved jobs", len(result.report.operator_required_job_ids)),
        ("Report", result.report_file or "not written"),
    ):
        footer.add_row(str(label), str(value))
    console.print(footer)


def _release_summary(plan: ReleaseAssemblyPlan, *, show_manifest: bool) -> None:
    from rich.table import Table

    summary = Table(title=f"Release {plan.release_id}", show_header=False)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    manifest = plan.manifest
    for label, value in (
        ("Release ID", manifest.release_id),
        ("Book", manifest.book_id),
        ("Languages", f"{manifest.source_language} → {manifest.target_language}"),
        ("Source run", manifest.source_translation_run),
        ("Snapshot", manifest.snapshot_id),
        ("Chapters", manifest.chapter_count),
        ("Chunks", manifest.chunk_count),
        ("Accepted translations", manifest.accepted_translation_count),
        ("Paragraphs", manifest.paragraph_count),
        ("Words", manifest.word_count),
        ("Terminology", manifest.terminology_version or "not frozen"),
        ("Chapter titles", manifest.chapter_title_artifact_id or "not supplied"),
        ("Content address", manifest.content_sha256),
        ("Output", plan.output_path),
    ):
        summary.add_row(str(label), str(value))
    console.print(summary)

    report = plan.report
    provenance = Table(title="Translation Report", show_header=False)
    provenance.add_column("Field", style="bold")
    provenance.add_column("Value")
    for label, value in (
        ("Run status", report.run_status),
        ("Accepted / total", f"{report.accepted_chunks}/{report.total_chunks}"),
        ("QA rejected", report.qa_rejected_chunks),
        ("Provider attempts", report.provider_attempts),
        ("Provider repairs", report.provider_repairs),
        ("Provider recoveries", report.provider_recoveries),
        ("Structural repairs", report.structural_repairs),
        ("QA re-evaluations", report.qa_reevaluations),
        ("Cross-run reuses", report.cross_run_reuses),
        ("Carry-forwards", report.historical_carry_forwards),
        ("Acceptance origins", ", ".join(f"{k}={v}" for k, v in report.acceptance_origins.items())),
        ("Providers", ", ".join(f"{k}={v}" for k, v in report.providers.items())),
        ("Terminology QA", ", ".join(report.terminology_qa_versions) or "unrecorded"),
        ("Structural QA", ", ".join(report.structural_qa_versions) or "unrecorded"),
    ):
        provenance.add_row(str(label), str(value))
    console.print(provenance)

    if show_manifest:
        artifacts = Table(title="Release Manifest")
        for column in ("File", "Role", "Bytes", "SHA-256"):
            artifacts.add_column(column)
        for artifact in manifest.artifacts:
            artifacts.add_row(
                artifact.file.as_posix(),
                artifact.role.value,
                str(artifact.byte_count),
                artifact.sha256,
            )
        console.print(artifacts)


@app.command("authorize-release-waiver")
def authorize_release_waiver_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option("--reason", help="Why publication is authorized.")],
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Record an explicit authorization to release a run whose jobs the judge never saw.

    The waiver names one run, one snapshot and one gate policy, states the semantic and
    mechanical counts it was authorized against, and is addressed by all of it. It never turns an
    unjudged job into a passed one, and it may not cover a job that was judged and failed.
    Without --apply nothing is written.
    """
    from book_translator.release_assembly.waiver_workflow import (
        execute_release_waiver,
        plan_release_waiver,
    )
    from book_translator.translation_queue.workflow import _load_queue, _resolve_run_path

    try:
        state = _load_queue(_resolve_run_path(run_id_or_path, Path.cwd()), Path.cwd())
        plan = plan_release_waiver(
            state.run, tuple(state.jobs), project_root=Path.cwd(), reason=reason
        )
        if apply:
            execute_release_waiver(plan)
    except (ReleaseWaiverError, QueueError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    waiver = plan.waiver
    table = Table(title="Release Semantic-Gate Waiver", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for label, value in (
        ("Waiver", waiver.waiver_id),
        ("Content SHA-256", waiver.content_sha256),
        ("Run", waiver.translation_run_id),
        ("Snapshot", waiver.snapshot_id),
        ("Gate policy", waiver.semantic_gate_policy_version),
        ("Jobs", str(waiver.job_count)),
        ("Judged PASS", str(waiver.judged_pass)),
        ("Judged not PASS", str(waiver.judged_not_pass)),
        ("Not judged", str(waiver.not_judged)),
        ("Mechanically accepted", str(waiver.accepted)),
        ("QA rejected", str(waiver.qa_rejected)),
        ("Decided by", waiver.decided_by),
        ("Reason", waiver.reason),
        ("Record", plan.path.relative_to(plan.project_root).as_posix()),
        ("Action", plan.action if apply else f"{plan.action} (dry run)"),
    ):
        table.add_row(label, value)
    console.print(table)
    console.print("Semantic verdicts created or changed: 0")
    if not apply:
        console.print("[dim]Dry run: nothing written.[/dim]")


@app.command("export-translation-run")
def export_translation_run_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    output: Annotated[str, typer.Option("--output")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    force: Annotated[bool, typer.Option("--force")] = False,
    show_manifest: Annotated[bool, typer.Option("--show-manifest")] = False,
    chapter_titles: Annotated[str | None, typer.Option("--chapter-titles")] = None,
    no_chapter_titles: Annotated[bool, typer.Option("--no-chapter-titles")] = False,
    semantic_waiver: Annotated[
        str | None,
        typer.Option(
            "--semantic-waiver",
            help="Operator-authorized waiver ID admitting unjudged jobs to this release.",
        ),
    ] = None,
) -> None:
    """Assemble a canonical Markdown release from one completed translation run."""
    try:
        plan = plan_release_assembly(
            run_id_or_path,
            output=output,
            project_root=Path.cwd(),
            force=force,
            chapter_titles=chapter_titles,
            use_chapter_titles=not no_chapter_titles,
            semantic_waiver=semantic_waiver,
        )
        result = None if dry_run else execute_release_assembly(plan, project_root=Path.cwd())
    except (
        ReleaseWaiverError,
        ReleaseAssemblyError,
        ChapterTitleError,
        SnapshotError,
        QueueError,
        OSError,
        ValueError,
    ) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _release_summary(plan, show_manifest=show_manifest)
    console.print(f"Release assembly engine: {RELEASE_ASSEMBLY_ENGINE_VERSION}")
    console.print(f"Dry run: {'yes' if dry_run else 'no'}")
    console.print(f"Files written: {0 if result is None else len(result.written_files)}")
    console.print(f"Manifest: {'not written' if result is None else result.manifest_file}")
    console.print("Provider contacted: no")
    console.print("Translation run, snapshot, and registry mutations: 0")


def _chapter_title_provider(
    provider: ProviderName, *, scenario: str = "success"
) -> "TranslationProvider":
    """Build the adapter for one chapter-title translation.

    Chapter titles reuse the ordinary provider boundary rather than inventing a second one: the
    exchange is text in, one line of text out, which is exactly what that protocol already is.
    """
    from book_translator.provider_execution.providers import (
        ClaudeCodeTranslationProvider,
        CodexCliTranslationProvider,
        FakeTranslationProvider,
        OpenAIResponsesProvider,
    )

    if provider is ProviderName.FAKE:
        return FakeTranslationProvider(scenario)
    if provider is ProviderName.CODEX_CLI:
        return CodexCliTranslationProvider()
    if provider is ProviderName.CLAUDE_CODE:
        return ClaudeCodeTranslationProvider()
    return OpenAIResponsesProvider()


def _chapter_title_summary(plan: ChapterTitlePlan, *, show_plan: bool) -> None:
    from rich.table import Table

    summary = Table(title=f"Chapter titles {plan.artifact_id}", show_header=False)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    recorded = sum(1 for item in plan.titles if item.already_recorded)
    for label, value in (
        ("Artifact ID", plan.artifact_id),
        ("Book", plan.book_id),
        ("Languages", f"{plan.source_language} → {plan.target_language}"),
        ("Source run", plan.run_id or "not recorded"),
        ("Snapshot", plan.snapshot_id),
        ("Terminology", plan.terminology_version or "not frozen"),
        ("Chapters", plan.chapter_count),
        ("Already recorded", recorded),
        ("Distinct titles to translate", len(plan.distinct_source_titles)),
        ("Planned provider calls", plan.planned_provider_calls),
        ("Output", plan.output_path),
        ("Destination exists", "yes" if plan.destination_exists else "no"),
    ):
        summary.add_row(str(label), str(value))
    console.print(summary)

    if show_plan:
        rows = Table(title="Planned Chapter Titles")
        for column in ("#", "Chapter", "Kind", "Terminology", "Recorded", "Request SHA-256"):
            rows.add_column(column)
        for item in plan.titles:
            rows.add_row(
                str(item.chapter_sequence),
                item.chapter_id,
                item.kind.value,
                str(len(item.enforced_decision_ids)),
                "yes" if item.already_recorded else "no",
                item.request_sha256[:16],
            )
        console.print(rows)


@app.command("revise-chapter-titles")
def revise_chapter_titles_command(
    parent_artifact: Annotated[str, typer.Argument(help="Parent chapter-title artifact ID.")],
    set_title: Annotated[
        list[str],
        typer.Option("--set", help="CHAPTER_ID=Persian title; repeat once per correction."),
    ],
    reason: Annotated[str, typer.Option("--reason", help="Why these titles are being replaced.")],
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Publish a provider-free revision of a chapter-title artifact, correcting named titles.

    Every other title is carried from the parent byte for byte, the parent is never rewritten,
    and the revision is published as its own immutable artifact that names what it revised. No
    provider is contacted. Without --apply nothing is written.
    """
    from book_translator.chapter_titles.revision import TitleOverride
    from book_translator.chapter_titles.revision_workflow import (
        execute_chapter_title_revision,
        plan_chapter_title_revision,
    )

    result = None
    try:
        overrides: dict[str, TitleOverride] = {}
        for item in set_title:
            chapter_id, separator, title = item.partition("=")
            if not separator or not chapter_id.strip() or not title.strip():
                raise ChapterTitleError(f"--set expects CHAPTER_ID=title, not {item!r}")
            if chapter_id.strip() in overrides:
                raise ChapterTitleError(f"chapter {chapter_id.strip()} is named twice")
            overrides[chapter_id.strip()] = TitleOverride(
                chapter_id=chapter_id.strip(), title_fa=title.strip(), reason=reason
            )
        plan = plan_chapter_title_revision(
            parent_artifact, project_root=Path.cwd(), overrides=overrides
        )
        if apply:
            result = execute_chapter_title_revision(plan)
    except (ChapterTitleError, TerminologyError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Chapter Title Revision", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for label, value in (
        ("Parent artifact", plan.parent.artifact_id),
        ("Parent content SHA-256", plan.parent.content_sha256),
        ("Revision artifact", plan.artifact_id),
        ("Revision content SHA-256", plan.revised.content_sha256),
        ("Snapshot", plan.parent.document.snapshot_id),
        ("Titles carried unchanged", str(plan.unchanged_count)),
        ("Titles replaced", str(len(plan.overrides))),
        ("Title QA", "valid" if plan.qa.valid else "invalid"),
        ("Provider calls", "0"),
        ("Output", plan.destination.as_posix()),
        ("Action", "published" if apply else "dry run"),
    ):
        table.add_row(label, str(value))
    console.print(table)
    changes = Table(title="Replaced Titles")
    for column in ("Chapter", "English", "Parent Persian", "Revised Persian"):
        changes.add_column(column)
    parent_titles = plan.parent.overlay()
    english = {item.chapter_id: item.title_en for item in plan.parent.document.chapters}
    for override in plan.overrides:
        changes.add_row(
            override.chapter_id,
            english[override.chapter_id],
            parent_titles[override.chapter_id],
            override.title_fa,
        )
    console.print(changes)
    if result is not None:
        console.print(f"Files written: {len(result.written_files)}")
    console.print("Chapter metadata, TOC, snapshot, and translation mutations: 0")
    if not apply:
        console.print("[dim]Dry run: nothing written, no provider contacted.[/dim]")


@app.command("translate-chapter-titles")
def translate_chapter_titles_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    provider: Annotated[ProviderName | None, typer.Option("--provider")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    output: Annotated[str | None, typer.Option("--output")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    show_plan: Annotated[bool, typer.Option("--show-plan")] = False,
    timeout_seconds: Annotated[int, typer.Option("--timeout-seconds")] = 120,
    max_output_tokens: Annotated[int, typer.Option("--max-output-tokens")] = 400,
) -> None:
    """Translate every chapter title of one run's snapshot into an immutable artifact."""
    try:
        plan = plan_chapter_titles(
            run_id_or_path, project_root=Path.cwd(), output=output, force=force
        )
        result = None
        if not dry_run:
            if provider is None:
                raise ChapterTitleError("--provider is required unless --dry-run is used")
            _require_provider_preflight(provider, dry_run=False)
            result = execute_chapter_titles(
                plan,
                project_root=Path.cwd(),
                provider=_chapter_title_provider(provider),
                model=model,
                timeout_seconds=timeout_seconds,
                max_output_tokens=max_output_tokens,
            )
    except (
        ChapterTitleError,
        TerminologyError,
        SnapshotError,
        ExecutorError,
        ProviderExecutionError,
        OSError,
        ValueError,
    ) as exc:
        message = getattr(getattr(exc, "classification", None), "message", None) or exc
        error_console.print(f"[bold red]Error:[/bold red] {message}")
        raise typer.Exit(code=1) from exc
    _chapter_title_summary(plan, show_plan=show_plan)
    console.print(
        f"Chapter title engine: {CHAPTER_TITLE_ENGINE_VERSION} "
        f"(prompt {CHAPTER_TITLE_PROMPT_VERSION}, QA {CHAPTER_TITLE_QA_VERSION})"
    )
    console.print(f"Dry run: {'yes' if dry_run else 'no'}")
    console.print(f"Provider calls: {0 if result is None else result.provider_calls}")
    if result is not None:
        console.print(f"QA valid: {'yes' if result.qa.valid else 'no'}")
        console.print(f"Files written: {len(result.written_files)}")
        console.print(f"Artifact: {result.output_path}")
    else:
        console.print("Files written: 0")
        console.print("Artifact: not written")
    console.print("Translation run, job, attempt, and acceptance mutations: 0")


def _release_verification_summary(report: ReleaseVerificationReport, *, verbose: bool) -> None:
    from rich.table import Table

    ready = report.status is ReleaseVerificationStatus.READY_FOR_PUBLICATION
    header = Table(title="Release Verification", show_header=False)
    header.add_column("Field", style="bold")
    header.add_column("Value")
    for label, value in (
        ("Release", report.release_id),
        ("Path", report.release_path.as_posix()),
        ("Source run", report.source_translation_run or "not recorded"),
        ("Snapshot", report.snapshot_id or "not recorded"),
        ("Chapter titles", report.chapter_title_artifact_id or "not supplied"),
        ("Workspace reachable", "yes" if report.workspace_reachable else "no"),
        (
            "Status",
            f"[bold green]{report.status.value}[/bold green]"
            if ready
            else f"[bold red]{report.status.value}[/bold red]",
        ),
    ):
        header.add_row(str(label), str(value))
    console.print(header)

    checks = Table(title="Checks")
    for column in ("Check", "Result", "Examined"):
        checks.add_column(column)
    styles = {
        ReleaseCheckStatus.PASS: "[green]PASS[/green]",
        ReleaseCheckStatus.FAIL: "[bold red]FAIL[/bold red]",
        ReleaseCheckStatus.SKIPPED: "[yellow]SKIPPED[/yellow]",
    }
    for check in report.checks:
        checks.add_row(
            check.check.value.replace("_", " ").title(),
            styles[check.status],
            f"{check.examined}/{check.expected}",
        )
    console.print(checks)

    totals = Table(title="Coverage", show_header=False)
    totals.add_column("Field", style="bold")
    totals.add_column("Value")
    for label, verified, expected in (
        ("Files", report.files_verified, report.files_expected),
        ("Chapters", report.chapters_verified, report.chapters_expected),
        ("Chunks", report.chunks_verified, report.chunks_expected),
        ("Titles", report.titles_verified, report.titles_expected),
    ):
        totals.add_row(str(label), f"{verified}/{expected}")
    console.print(totals)

    if report.findings:
        problems = Table(title="Findings")
        for column in ("Check", "Code", "Subject", "Message"):
            problems.add_column(column)
        for item in report.findings:
            problems.add_row(item.check.value, item.code, item.subject or "-", item.message)
        console.print(problems)

    if verbose:
        detail = Table(title="What Each Check Proved")
        for column in ("Check", "Detail"):
            detail.add_column(column)
        for check in report.checks:
            detail.add_row(check.check.value, check.detail)
        console.print(detail)
        digests = Table(title="Hashes", show_header=False)
        digests.add_column("Field", style="bold")
        digests.add_column("Value")
        digest_rows: tuple[tuple[str, str | None], ...] = (
            ("Manifest SHA-256", report.manifest_sha256),
            ("Release content address", report.release_content_sha256),
            ("book.md SHA-256", report.book_sha256),
            ("Terminology SHA-256", report.terminology_sha256),
            ("Title artifact SHA-256", report.chapter_title_artifact_sha256),
            ("Verification content address", report.content_sha256),
        )
        for digest_label, digest in digest_rows:
            digests.add_row(digest_label, digest or "not recorded")
        console.print(digests)


@app.command("audit-paragraph-alignment")
def audit_paragraph_alignment_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    chapter_id: Annotated[str | None, typer.Option("--chapter-id")] = None,
    job_id: Annotated[str | None, typer.Option("--job-id")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    show_findings: Annotated[bool, typer.Option("--show-findings")] = False,
) -> None:
    """Read-only paragraph alignment audit of every accepted translation in one run."""
    try:
        plan = plan_alignment_audit(
            run_id_or_path, project_root=Path.cwd(), chapter_id=chapter_id, job_id=job_id
        )
        written = None if dry_run else execute_alignment_audit(plan)
    except (AlignmentAuditError, ReleaseAssemblyError, QueueError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    report = plan.report
    table = Table(title="Paragraph Alignment Audit", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Run ID", report.run_id)
    table.add_row("Policy", f"{report.policy_id}/{report.policy_version}")
    table.add_row("Alignment QA", report.alignment_qa_version)
    table.add_row("Assessed jobs", str(report.assessed_jobs))
    table.add_row("Skipped jobs", str(report.skipped_jobs))
    table.add_row("Aligned pairs", str(report.aligned_pairs))
    table.add_row("Measured pairs", str(report.measured_pairs))
    table.add_row("Jobs with findings", str(report.jobs_with_findings))
    table.add_row("Jobs with blocking findings", str(report.jobs_with_blocking_findings))
    console.print(table)
    if report.finding_counts:
        counts = Table(title="Findings by code")
        counts.add_column("Code")
        counts.add_column("Count", justify="right")
        for code, total in report.finding_counts.items():
            counts.add_row(code, str(total))
        console.print(counts)
    if show_findings:
        detail = Table(title="Flagged chunks")
        detail.add_column("Chunk")
        detail.add_column("Mode")
        detail.add_column("Blocking", justify="right")
        detail.add_column("Codes")
        for entry in report.entries:
            if not entry.findings:
                continue
            detail.add_row(
                entry.chunk_id,
                entry.contract_mode,
                str(entry.error_findings),
                ", ".join(sorted({item.code for item in entry.findings})),
            )
        console.print(detail)
    console.print(f"Audit artifact: {written or 'not written'}")
    console.print("Provider contacted: no")
    console.print("Acceptance state changed: no")


def _semantic_selections(
    run_id_or_path: str,
    *,
    project_root: Path,
    chunks: tuple[str, ...],
    chapter_id: str | None,
    paragraph_indexes: tuple[int, ...] | None,
) -> list[ChunkSelection]:
    """Turn CLI selection options into explicit per-chunk selections."""
    if chunks:
        return [ChunkSelection(value, paragraph_indexes) for value in chunks]
    state = load_run_summary(run_id_or_path, project_root=project_root)
    matching = [
        job.chunk_id for job in state.jobs if chapter_id is None or job.chapter_id == chapter_id
    ]
    if not matching:
        raise SemanticCoverageError("no chunk matches the semantic coverage selection")
    return [ChunkSelection(value, paragraph_indexes) for value in matching]


def _semantic_transport(provider: ProviderName) -> TranslationProvider:
    """Return the audited transport the judge asks its own question over."""
    if provider is ProviderName.CLAUDE_CODE:
        from book_translator.provider_execution.providers import ClaudeCodeTranslationProvider

        return ClaudeCodeTranslationProvider()
    if provider is ProviderName.CODEX_CLI:
        from book_translator.provider_execution.providers import CodexCliTranslationProvider

        return CodexCliTranslationProvider()
    if provider is ProviderName.FAKE:
        from book_translator.semantic_qa import FakeSemanticJudgeProvider, passing_response

        return FakeSemanticJudgeProvider((passing_response(("P0001",)),))
    raise SemanticCoverageError(f"provider {provider.value} has no semantic coverage transport")


def _render_semantic_report(plan: SemanticCoveragePlan, result: SemanticCoverageResult) -> None:
    from rich.table import Table

    report = result.report
    table = Table(title="Semantic Coverage Evaluation", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Run ID", report.run_id)
    table.add_row("Evaluation", f"{report.evaluation_number:03d}")
    table.add_row("Judge engine", report.judge_engine_version)
    table.add_row("Judge prompt", report.judge_prompt_version)
    table.add_row("Judge schema", report.judge_schema_version)
    table.add_row("Policy", report.policy_version)
    table.add_row("Provider", f"{report.provider} {report.adapter_version}")
    table.add_row("Models returned", ", ".join(report.models_returned) or "none reported")
    table.add_row("Batch size", str(report.batch_size))
    table.add_row("Repetitions", str(report.repetitions))
    table.add_row("Chunks selected", str(report.chunks_selected))
    table.add_row("Chunks judged", str(report.chunks_judged))
    table.add_row("Planned calls", str(plan.planned_calls))
    table.add_row("Judge calls", str(report.judge_calls))
    table.add_row("Failed calls", str(report.failed_calls))
    table.add_row("Paragraphs judged", str(report.paragraphs_judged))
    table.add_row("PASS", str(report.pass_count))
    table.add_row("REVISE", str(report.revise_count))
    table.add_row("INCONCLUSIVE", str(report.inconclusive_count))
    console.print(table)
    if report.finding_counts:
        counts = Table(title="Findings by code")
        counts.add_column("Code")
        counts.add_column("Count", justify="right")
        for code, total in report.finding_counts.items():
            counts.add_row(code, str(total))
        console.print(counts)
    console.print(f"Evidence: {result.written or 'not written'}")
    console.print("Acceptance state changed: no")


@app.command("audit-semantic-coverage")
def audit_semantic_coverage_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    chunk: Annotated[list[str] | None, typer.Option("--chunk")] = None,
    chapter_id: Annotated[str | None, typer.Option("--chapter-id")] = None,
    paragraphs: Annotated[str | None, typer.Option("--paragraphs")] = None,
    batch_size: Annotated[int, typer.Option("--batch-size")] = 4,
    repetitions: Annotated[int, typer.Option("--repetitions")] = 1,
    context_radius: Annotated[int, typer.Option("--context-radius")] = 1,
    provider: Annotated[ProviderName, typer.Option("--provider")] = ProviderName.FAKE,
    model: Annotated[str | None, typer.Option("--model")] = None,
    timeout_seconds: Annotated[int | None, typer.Option("--timeout-seconds")] = None,
    max_output_tokens: Annotated[int, typer.Option("--max-output-tokens")] = 8000,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Judge whether accepted translations preserve their source meaning. Never changes state."""
    try:
        selected_indexes = (
            tuple(int(value) for value in paragraphs.split(",")) if paragraphs else None
        )
        selections = _semantic_selections(
            run_id_or_path,
            project_root=Path.cwd(),
            chunks=tuple(chunk or ()),
            chapter_id=chapter_id,
            paragraph_indexes=selected_indexes,
        )
        config = _run_book_config(run_id_or_path)
        plan = plan_semantic_coverage(
            run_id_or_path,
            project_root=Path.cwd(),
            selections=selections,
            batch_size=batch_size,
            repetitions=repetitions,
            context_radius=context_radius,
        )
        if not dry_run and provider is not ProviderName.FAKE:
            _require_provider_preflight(provider, dry_run=False)
        judge = TransportSemanticJudge(
            _semantic_transport(provider),
            model=model,
            timeout_seconds=timeout_seconds or config.provider_timeout_seconds,
            max_output_tokens=max_output_tokens,
        )
        result = execute_semantic_coverage(plan, judge, dry_run=dry_run)
    except (
        SemanticCoverageError,
        SemanticTaskError,
        QueueError,
        ExecutorError,
        ProjectDataError,
        OSError,
        ValueError,
    ) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _render_semantic_report(plan, result)


@app.command("run-semantic-gate")
def run_semantic_gate_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id: Annotated[str | None, typer.Option("--job")] = None,
    chapter_id: Annotated[str | None, typer.Option("--chapter-id")] = None,
    provider: Annotated[ProviderName, typer.Option("--provider")] = ProviderName.FAKE,
    model: Annotated[str | None, typer.Option("--model")] = None,
    timeout_seconds: Annotated[int | None, typer.Option("--timeout-seconds")] = None,
    max_output_tokens: Annotated[int, typer.Option("--max-output-tokens")] = 8000,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Judge, route, and boundedly revise a marked run's accepted translations for fidelity."""
    try:
        config = _run_book_config(run_id_or_path)
        plan = plan_semantic_gate(
            run_id_or_path, project_root=Path.cwd(), job_id=job_id, chapter_id=chapter_id
        )
        if not dry_run and provider is not ProviderName.FAKE:
            _require_provider_preflight(provider, dry_run=False)
        transport = _semantic_transport(provider)
        timeout = timeout_seconds or config.provider_timeout_seconds
        result = execute_semantic_gate(
            plan,
            judge=TransportSemanticJudge(
                transport, model=model, timeout_seconds=timeout, max_output_tokens=max_output_tokens
            ),
            reviser=TransportSemanticReviser(
                transport, model=model, timeout_seconds=timeout, max_output_tokens=max_output_tokens
            ),
            dry_run=dry_run,
        )
    except (
        SemanticGateError,
        SemanticCoverageError,
        SemanticTaskError,
        QueueError,
        ExecutorError,
        ProjectDataError,
        OSError,
        ValueError,
    ) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    report = result.report
    table = Table(title="Semantic Gate", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Run ID", report.run_id)
    table.add_row("Gate policy", report.gate_policy_version)
    table.add_row(
        "Judge prompt / schema", f"{report.judge_prompt_version} / {report.judge_schema_version}"
    )
    table.add_row(
        "Revision prompt / policy",
        f"{report.revision_prompt_version} / {report.revision_policy_version}",
    )
    table.add_row("Provider", f"{report.provider} {report.adapter_version}")
    table.add_row("Models returned", ", ".join(report.models_returned) or "none reported")
    table.add_row("Jobs selected", str(report.jobs_selected))
    table.add_row("Jobs skipped", str(len(plan.skipped)))
    table.add_row("Planned first-look calls", str(plan.planned_first_look_calls))
    table.add_row("Judge calls", str(report.judge_calls))
    table.add_row("Reviser calls", str(report.reviser_calls))
    table.add_row("Second looks", str(report.second_looks))
    table.add_row("Re-segmentations", str(report.resegmentations))
    table.add_row(
        "Revisions adopted / refused", f"{report.revisions_adopted} / {report.revisions_refused}"
    )
    for status, count in report.status_counts.items():
        table.add_row(f"Jobs {status}", str(count))
    console.print(table)
    console.print(f"Report: {result.written or 'not written'}")
    console.print("Mechanical acceptance changed: no")


@app.command("revise-translation-paragraph")
def revise_translation_paragraph_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    job_id_or_chunk_id: Annotated[str, typer.Argument()],
    paragraph: Annotated[int, typer.Option("--paragraph", help="1-based source paragraph.")],
    code: Annotated[str, typer.Option("--code", help="Semantic finding code for the defect.")],
    source_evidence: Annotated[str, typer.Option("--source-evidence")],
    reason: Annotated[str, typer.Option("--reason")],
    severity: Annotated[str, typer.Option("--severity")] = "material",
    expect_paragraph_sha256: Annotated[
        str | None, typer.Option("--expect-paragraph-sha256")
    ] = None,
    provider: Annotated[ProviderName, typer.Option("--provider")] = ProviderName.FAKE,
    model: Annotated[str | None, typer.Option("--model")] = None,
    timeout_seconds: Annotated[int | None, typer.Option("--timeout-seconds")] = None,
    max_output_tokens: Annotated[int, typer.Option("--max-output-tokens")] = 8000,
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Revise exactly one operator-named paragraph of an already-accepted translation.

    The operator states which paragraph is wrong and why; the reviser corrects only that
    paragraph, every other paragraph is copied from the standing translation, and the result is
    adopted only if the bounds, the full mechanical acceptance battery, and an independent
    judgement of the revised paragraph all agree. Without --apply nothing is written and no
    provider is contacted.
    """
    from book_translator.models import (
        OperatorDefectStatement,
        SemanticFindingCode,
        SemanticSeverity,
    )
    from book_translator.semantic_qa.providers import TransportSemanticJudge
    from book_translator.semantic_revision.providers import TransportSemanticReviser
    from book_translator.semantic_revision.targeted import TargetedRevisionError
    from book_translator.semantic_revision.workflow import (
        execute_targeted_revision,
        plan_targeted_revision,
    )

    result = None
    try:
        statement = OperatorDefectStatement(
            code=SemanticFindingCode(code),
            severity=SemanticSeverity(severity),
            source_evidence=source_evidence,
            reason=reason,
        )
        config = _run_book_config(run_id_or_path)
        plan = plan_targeted_revision(
            run_id_or_path,
            job_id_or_chunk_id,
            paragraph_index=paragraph,
            statement=statement,
            project_root=Path.cwd(),
            expect_paragraph_sha256=expect_paragraph_sha256,
        )
        if apply:
            if plan.action != "revise":
                raise TargetedRevisionError(
                    f"this revision is {plan.action}; nothing was sent to a provider"
                )
            if provider is not ProviderName.FAKE:
                _require_provider_preflight(provider, dry_run=False)
            transport = _semantic_transport(provider)
            timeout = timeout_seconds or config.provider_timeout_seconds
            result = execute_targeted_revision(
                plan,
                reviser=TransportSemanticReviser(
                    transport,
                    model=model,
                    timeout_seconds=timeout,
                    max_output_tokens=max_output_tokens,
                ),
                judge=TransportSemanticJudge(
                    transport,
                    model=model,
                    timeout_seconds=timeout,
                    max_output_tokens=max_output_tokens,
                ),
            )
    except (
        TargetedRevisionError,
        SemanticCoverageError,
        SemanticTaskError,
        QueueError,
        ExecutorError,
        ProjectDataError,
        OSError,
        ValueError,
    ) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Targeted Semantic Revision", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    rows: list[tuple[str, str]] = [
        ("Run", plan.run_id),
        ("Job", plan.job.job_id),
        ("Paragraph", f"{plan.identity.paragraph_id} (index {plan.paragraph_index})"),
        ("Standing translation", plan.standing_file.as_posix()),
        ("Standing translation SHA-256", plan.standing_sha256),
        ("Source paragraph SHA-256", plan.identity.source_paragraph_sha256),
        ("Target paragraph SHA-256", plan.identity.target_paragraph_sha256_before),
        ("Writable paragraphs", ", ".join(str(index) for index in plan.writable_paragraphs)),
        ("Paragraphs outside the window", str(plan.protected_paragraphs)),
        ("Occurrence exemptions", ", ".join(plan.exemption_ids) or "none"),
        ("Canonical terms sent", str(len(plan.terminology_constraints))),
        ("Defect", f"{plan.statement.code.value} / {plan.statement.severity.value}"),
        ("Revision budget", f"{plan.budget_remaining} of 2 remaining"),
        ("Reviser request SHA-256", plan.request_sha256),
        ("Artifact path", plan.destination.relative_to(plan.project_root).as_posix()),
        ("Provider calls if applied", "2 (reviser, then verification judge)"),
        ("Action", plan.action if apply else f"{plan.action} (dry run)"),
    ]
    if result is not None:
        rows.extend(
            [
                ("Provider calls made", str(result.provider_calls)),
                ("Adopted", "yes" if result.adopted else "no"),
                ("Refusals", ", ".join(result.evidence.refusals) or "none"),
                (
                    "Revised paragraph SHA-256",
                    (result.evidence.revised_paragraph.target_paragraph_sha256_after or "none")
                    if result.evidence.revised_paragraph is not None
                    else "none",
                ),
                (
                    "Resulting translation SHA-256",
                    result.evidence.translation_sha256 or "unchanged",
                ),
                (
                    "Evidence",
                    result.evidence_file.relative_to(plan.project_root).as_posix(),
                ),
            ]
        )
    for label, value in rows:
        table.add_row(label, value)
    console.print(table)
    if plan.terminology_constraints:
        terms = Table(title="Canonical Terminology Sent To The Reviser")
        for column in ("Source term", "Canonical target", "Required", "Authorized forms"):
            terms.add_column(column)
        for item in plan.terminology_constraints:
            terms.add_row(
                item.source_term,
                item.target_term,
                "yes" if item.required else "no",
                ", ".join(item.allowed_target_forms) or "-",
            )
        console.print(terms)
    if not apply:
        console.print("[dim]Dry run: nothing written, no provider contacted.[/dim]")


@app.command("run-literary-edit")
def run_literary_edit_command(
    run_id_or_path: Annotated[str, typer.Argument()],
    chapter_id: Annotated[list[str] | None, typer.Option("--chapter-id")] = None,
    provider: Annotated[ProviderName, typer.Option("--provider")] = ProviderName.CLAUDE_CODE,
    model: Annotated[str | None, typer.Option("--model")] = None,
    style_file: Annotated[str, typer.Option("--style-contract")] = (
        "config/persian-literary-style.yaml"
    ),
    profile_file: Annotated[str | None, typer.Option("--book-profile")] = (
        "config/literary-profile.yaml"
    ),
    historical_run_id: Annotated[str | None, typer.Option("--historical-run")] = None,
    historical_release: Annotated[str | None, typer.Option("--historical-release")] = None,
    reuse_literary_run_id: Annotated[str | None, typer.Option("--reuse-literary-run")] = None,
    timeout_seconds: Annotated[int | None, typer.Option("--timeout-seconds")] = None,
    max_output_tokens: Annotated[int, typer.Option("--max-output-tokens")] = 32000,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Edit explicit complete chapters, independently re-judge changes, and publish a new layer."""
    try:
        chapters = tuple(_chapter_filter(value) or "" for value in chapter_id or ())
        if not chapters:
            raise LiteraryWorkflowError(
                "at least one --chapter-id is required; whole-book editing is disabled"
            )
        if provider is ProviderName.FAKE and not dry_run:
            raise LiteraryWorkflowError(
                "the CLI fake provider is available only for literary dry runs"
            )
        config = _run_book_config(run_id_or_path)
        plan = plan_literary_run(
            run_id_or_path,
            project_root=Path.cwd(),
            chapter_ids=chapters,
            provider=provider.value,
            model=model,
            style_file=Path(style_file),
            profile_file=Path(profile_file) if profile_file is not None else None,
            historical_run_id=historical_run_id,
            historical_release=(
                Path(historical_release) if historical_release is not None else None
            ),
            reuse_literary_run_id=reuse_literary_run_id,
        )
        _require_provider_preflight(provider, dry_run=dry_run)
        transport = _semantic_transport(provider)
        timeout = timeout_seconds or max(1200, config.provider_timeout_seconds)
        result = execute_literary_run(
            plan,
            literary=TransportLiteraryRoles(
                transport,
                model=model,
                timeout_seconds=timeout,
                max_output_tokens=max_output_tokens,
            ),
            judge=TransportSemanticJudge(
                transport,
                model=model,
                timeout_seconds=config.provider_timeout_seconds,
                max_output_tokens=8000,
            ),
            dry_run=dry_run,
        )
    except (
        LiteraryWorkflowError,
        LiteraryProviderError,
        SemanticTaskError,
        ExecutorError,
        QueueError,
        ProjectDataError,
        OSError,
        ValueError,
    ) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    report = result.artifact
    table = Table(title="Chapter-level Persian Literary Editor", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for label, value in (
        ("Literary run", report.literary_run_id),
        ("Status", report.status.value),
        ("Input run", report.input_translation_run_id),
        ("Chapters", ", ".join(report.chapter_ids)),
        ("Provider", report.provider),
        ("Requested model", report.model_requested or "provider-selected"),
        ("Editor calls", report.literary_editor_calls),
        ("Semantic re-judge calls", report.semantic_rejudge_calls),
        ("Correction calls", report.literary_correction_calls),
        ("Continuity calls", report.continuity_reviewer_calls),
        ("Paragraphs", report.paragraph_count),
        ("Changed", report.changed_paragraph_count),
        ("Changed percentage", f"{report.changed_paragraph_percentage:.2f}%"),
        ("Semantic regressions", report.semantic_regression_count),
        ("Fallbacks", report.fallback_count),
    ):
        table.add_row(str(label), str(value))
    console.print(table)
    console.print(f"Artifact: {result.destination or 'not written'}")
    console.print("Input translation run changed: no")
    console.print("Release and EPUB changed: no")


@app.command("show-literary-run")
def show_literary_run_command(literary_run_id: Annotated[str, typer.Argument()]) -> None:
    """Show hashes, counts, provenance, and findings without printing book prose or the A/B key."""
    try:
        root = ProjectPaths(Path.cwd()).literary_run(literary_run_id)
        report = LiteraryRunArtifact.model_validate(
            yaml.safe_load((root / "run.yaml").read_text(encoding="utf-8"))
        )
    except (OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    summary = Table(title=f"Literary Run {report.literary_run_id}")
    for column in ("Chapter", "Paragraphs", "Changed", "Regressions", "Fallbacks", "Continuity"):
        summary.add_column(column)
    for chapter in report.chapters:
        summary.add_row(
            chapter.chapter_id,
            str(chapter.paragraph_count),
            str(chapter.changed_paragraph_count),
            str(chapter.semantic_regressions_caught),
            str(chapter.fallback_count),
            str(len(chapter.continuity_findings)),
        )
    console.print(summary)
    console.print(f"Blind comparison: {root / 'blind' / 'comparison.md'}")
    console.print("Version identity: hidden until the reader scores the comparison")


@app.command("compare-translation-runs")
def compare_translation_runs_command(
    first_run: Annotated[str, typer.Argument()],
    second_run: Annotated[str, typer.Argument()],
    chapter_id: Annotated[str, typer.Option("--chapter-id")],
    first_release: Annotated[str | None, typer.Option("--first-release")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Compare two runs' effective translations of one chapter in hashes and counts only."""
    try:
        plan = plan_translation_comparison(
            first_run,
            second_run,
            chapter_id=chapter_id,
            project_root=Path.cwd(),
            first_release=first_release,
        )
        written = None if dry_run else execute_translation_comparison(plan)
    except (TranslationComparisonError, QueueError, ExecutorError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    report = plan.report
    table = Table(title=f"Translation Comparison {report.chapter_id}")
    table.add_column("Chunk")
    table.add_column("Side")
    table.add_column("Origin")
    table.add_column("Attempts", justify="right")
    table.add_column("Align", justify="right")
    table.add_column("Term")
    table.add_column("Semantic")
    table.add_column("Revised")
    for chunk in report.chunks:
        for label, side in (("v1", chunk.first), ("v2", chunk.second)):
            table.add_row(
                chunk.chunk_id if label == "v1" else "",
                label,
                side.acceptance_origin,
                str(side.provider_attempts),
                f"{side.alignment_blocking}/{len(side.alignment_codes)}",
                ",".join(side.terminology_blocking_codes) or "-",
                side.semantic_status or "not judged",
                ",".join(side.revised_paragraph_ids) or "-",
            )
    console.print(table)
    console.print(
        f"Chapter hashes: v1 {report.first_chapter_sha256[:12]}"
        f"  v2 {report.second_chapter_sha256[:12]}"
    )
    console.print(f"Artifact: {written or 'not written'}")
    console.print("Provider contacted: no")


@app.command("verify-release")
def verify_release_command(
    release_path: Annotated[str, typer.Argument()],
    as_json: Annotated[bool, typer.Option("--json")] = False,
    output: Annotated[str | None, typer.Option("--output")] = None,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Prove one assembled release is complete, consistent, immutable, and publishable."""
    try:
        verification = verify_release_artifact(release_path, project_root=Path.cwd())
        written = (
            write_verification_report(verification, output, project_root=Path.cwd())
            if output is not None
            else None
        )
    except (ReleaseVerificationError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    report = verification.report
    if as_json:
        console.print_json(serialize_verification(report, as_json=True))
    else:
        _release_verification_summary(report, verbose=verbose)
        console.print(f"Release verification engine: {RELEASE_VERIFICATION_ENGINE_VERSION}")
        console.print(f"Report: {written or 'not written'}")
        console.print("Provider contacted: no")
        console.print("Release, run, snapshot, and registry mutations: 0")
    if report.status is not ReleaseVerificationStatus.READY_FOR_PUBLICATION:
        raise typer.Exit(code=1)


def _epub_summary(plan: EpubBuildPlan, *, show_manifest: bool) -> None:
    from rich.table import Table

    summary = Table(title=f"EPUB {plan.release_id}", show_header=False)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    for label, value in (
        ("Release", plan.release_id),
        ("Release verification", plan.verification.status.value),
        ("Release content address", plan.release_content_sha256),
        ("Source run", plan.source_translation_run or "not recorded"),
        ("Title", plan.title),
        ("Author", plan.author),
        ("Language", plan.language),
        ("Reading direction", "rtl"),
        ("Book identifier", plan.book_identifier),
        ("Modified", plan.modified),
        ("Chapters", plan.chapter_count),
        ("Paragraphs", plan.paragraph_count),
        ("Archive entries", len(plan.files)),
        ("EPUB content address", plan.content_sha256),
        ("Output", plan.output_path),
        ("Destination exists", "yes" if plan.destination_exists else "no"),
    ):
        summary.add_row(str(label), str(value))
    console.print(summary)

    if show_manifest:
        from book_translator.epub import epub_content_address  # noqa: F401

        entries = Table(title="Archive Manifest")
        for column in ("Entry", "Bytes", "Stored"):
            entries.add_column(column)
        for item in plan.files:
            entries.add_row(
                item.path, str(item.byte_count), "deflated" if item.compress else "stored"
            )
        console.print(entries)

        chapters = Table(title="Chapters")
        for column in ("#", "Chapter", "Title", "Paragraphs", "Archive path"):
            chapters.add_column(column)
        for record in plan.chapters:
            chapters.add_row(
                str(record.chapter_sequence),
                record.chapter_id,
                record.title,
                str(record.paragraph_count),
                record.archive_path,
            )
        console.print(chapters)


@app.command("build-epub")
def build_epub_command(
    release_path: Annotated[str, typer.Argument()],
    output: Annotated[str | None, typer.Option("--output")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    show_manifest: Annotated[bool, typer.Option("--show-manifest")] = False,
) -> None:
    """Build an RTL EPUB 3 from one release that verification has passed."""
    try:
        plan = plan_epub_build(release_path, project_root=Path.cwd(), output=output, force=force)
        result = None if dry_run else execute_epub_build(plan, project_root=Path.cwd())
    except (EpubBuildError, ReleaseVerificationError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _epub_summary(plan, show_manifest=show_manifest)
    console.print(f"EPUB builder: {EPUB_BUILDER_VERSION} (EPUB 3.0, page-progression rtl)")
    console.print(f"Dry run: {'yes' if dry_run else 'no'}")
    if result is None:
        console.print("EPUB written: no")
        console.print("Archive SHA-256: not written")
    else:
        console.print(f"EPUB written: {result.output_path}")
        console.print(f"Archive bytes: {result.byte_count}")
        console.print(f"Archive SHA-256: {result.archive_sha256}")
        console.print(f"Archive entries: {result.entry_count}")
    console.print("Provider contacted: no")
    console.print("Release, run, snapshot, and registry mutations: 0")


# --------------------------------------------------------------------------------------------
# Series terminology: policy import, book derivation, reference context, inspection
# --------------------------------------------------------------------------------------------


def _series_errors() -> tuple[type[Exception], ...]:
    from book_translator.series_terminology.policy import SeriesTerminologyError
    from book_translator.series_terminology.retrieval import ReferenceRetrievalError

    return (
        ProjectDataError,
        SeriesTerminologyError,
        ReferenceRetrievalError,
        TerminologyError,
        OSError,
        UnicodeError,
        ValueError,
    )


def _series_book_config(book: str | None) -> tuple[Path, BookConfig]:
    paths = _project_paths(book)
    config = load_book_config(paths.book_config)
    if config.series_terminology_policy is None or config.reference_corpus is None:
        error_console.print(
            "[bold red]Error:[/bold red] this book configuration names no "
            "series_terminology_policy and reference_corpus"
        )
        raise typer.Exit(code=1)
    return Path.cwd().resolve(), config


@app.command("import-series-terminology-policy")
def import_series_terminology_policy_command(
    book: BookOption = None,
    explicit_registry: Annotated[Path | None, typer.Option("--explicit-registry")] = None,
    glossary: Annotated[Path | None, typer.Option("--glossary")] = None,
    prior_volume_registry: Annotated[Path | None, typer.Option("--prior-volume-registry")] = None,
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Build the series terminology policy from explicit decisions and the reference corpora.

    User decisions in --explicit-registry and entries of --glossary become LOCKED; the corpora
    supply STRONG_REFERENCE promotions and observed variants; --prior-volume-registry adds an
    earlier volume's non-user decisions as evidence only. Conflicts are recorded, never resolved.
    """
    from book_translator.series_terminology.retrieval import load_reference_corpus_config
    from book_translator.series_terminology.workflow import (
        execute_policy_import,
        open_corpus_reader,
        plan_policy_import,
    )

    project_root, config = _series_book_config(book)
    close = None
    try:
        corpus_config = load_reference_corpus_config(
            project_root / (config.reference_corpus or Path())
        )
        reader, close, sources = open_corpus_reader(corpus_config, project_root=project_root)
        plan = plan_policy_import(
            project_root=project_root,
            policy_path=config.series_terminology_policy or Path(),
            series_id=corpus_config.series_id,
            source_language=config.source_language,
            target_language=config.target_language,
            corpus_concepts=reader.concepts(),
            corpus_sources=sources,
            explicit_registry=explicit_registry,
            glossary=glossary,
            prior_volume_registry=prior_volume_registry,
        )
        if apply:
            execute_policy_import(plan)
    except _series_errors() as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        if close is not None:
            close()
    from rich.table import Table

    table = Table(title="Series Terminology Policy Import", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Policy file", plan.policy_path.relative_to(project_root).as_posix())
    table.add_row("Series", plan.policy.series_id)
    table.add_row("Terms", str(len(plan.policy.terms)))
    table.add_row("LOCKED", str(plan.report.locked))
    table.add_row("STRONG_REFERENCE", str(plan.report.strong_reference))
    table.add_row("OBSERVED_VARIANTS", str(plan.report.observed_variants))
    table.add_row("Conflicts recorded", str(len(plan.report.conflicts)))
    table.add_row("Action", plan.action if apply else f"{plan.action} (dry run)")
    console.print(table)
    for source_term, detail in plan.report.conflicts:
        console.print(f"[yellow]conflict[/yellow] {source_term}: {detail}")
    if not apply:
        console.print("[dim]Dry run: rerun with --apply to write the policy.[/dim]")


@app.command("set-series-terminology")
def set_series_terminology_command(
    source: str,
    target: str,
    classification: Annotated[TerminologyType, typer.Option("--type")],
    rationale: Annotated[str, typer.Option("--rationale")],
    status: Annotated[str, typer.Option("--status")] = "LOCKED",
    alias: Annotated[list[str] | None, typer.Option("--alias")] = None,
    target_form: Annotated[
        list[str] | None,
        typer.Option(
            "--target-form",
            help="Explicitly authorize a further target realization (repeatable).",
        ),
    ] = None,
    book: BookOption = None,
) -> None:
    """Record an explicit user decision in the series policy (LOCKED or STRONG_REFERENCE)."""
    from book_translator.models import SeriesTermStatus
    from book_translator.series_terminology.policy import (
        load_policy,
        save_policy,
        set_explicit_term,
    )

    project_root, config = _series_book_config(book)
    try:
        policy_path = project_root / (config.series_terminology_policy or Path())
        policy = set_explicit_term(
            load_policy(policy_path),
            source_term=source,
            target_term=target,
            classification=classification,
            status=SeriesTermStatus(status),
            rationale=rationale,
            aliases=tuple(alias or ()),
            allowed_target_forms=tuple(target_form or ()),
        )
        save_policy(policy, policy_path)
    except _series_errors() as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Recorded {status} decision for {source!r}; run derive-book-terminology next.")


@app.command("migrate-translation-run")
def migrate_translation_run_command(
    run_id: str,
    from_run: Annotated[str, typer.Option("--from-run", help="Historical run to migrate from.")],
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Carry a historical run's usable provider work into a successor run, provider-free.

    Every successor job is classified from immutable evidence: identical-request accepted outputs
    are reused, moved-request and rejected outputs are re-assessed under the successor's frozen QA
    through governed historical re-evaluation (a still-rejected output becomes ready for an
    explicit repair), and jobs without a usable output stay pending. Render the successor's prompt
    packages first. Without --apply nothing is written. No provider is contacted.
    """
    from book_translator.translation_migration import MigrationClass, migrate_run

    try:
        result = migrate_run(run_id, from_run=from_run, project_root=Path.cwd(), dry_run=not apply)
    except (ValueError, OSError, QueueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    plan = result.plan
    summary = Table(title="Translation Run Migration", show_header=False)
    summary.add_column("Field", style="bold")
    summary.add_column("Value")
    summary.add_row("Successor run", plan.run_id)
    summary.add_row("Historical run", plan.source_run_id)
    for migration_class in MigrationClass:
        count = len(plan.of(migration_class))
        if count:
            summary.add_row(migration_class.value, str(count))
    summary.add_row("Provider outputs carried", str(plan.provider_attempts_preserved))
    summary.add_row("Provider calls", str(result.provider_calls))
    summary.add_row(
        "Mode",
        "applied: " + ", ".join(f"{k} {v}" for k, v in result.published.items())
        if apply
        else "dry run",
    )
    console.print(summary)
    detail = Table(title="Jobs With Historical Evidence")
    for column in ("Job", "Class", "Historical", "Projected", "Reasons"):
        detail.add_column(column)
    for item in plan.jobs:
        if item.historical_status is None and item.historical_attempts_consumed == 0:
            continue
        detail.add_row(
            item.chunk_id,
            item.migration_class.value,
            item.historical_status.value
            if item.historical_status
            else (item.historical_job_status.value if item.historical_job_status else "-"),
            item.projected_status.value if item.projected_status else "-",
            ", ".join(item.reasons) or "-",
        )
    console.print(detail)
    if not apply:
        console.print("[dim]Dry run: rerun with --apply to publish.[/dim]")


@app.command("amend-series-terminology")
def amend_series_terminology_command(
    request: Annotated[Path, typer.Argument(help="Amendment request YAML.")],
    book: BookOption = None,
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Apply a batch of explicit series decisions atomically, with an immutable lineage record.

    Every item restates an existing term's complete intended decision (target, force, aliases,
    authorized forms). The whole batch is refused, and nothing written, if any item would create a
    term or override an explicit user decision. Without --apply this is a dry run.
    """
    from book_translator.series_terminology.workflow import (
        execute_policy_amendment,
        plan_policy_amendment,
    )

    _series_book_config(book)
    try:
        plan = plan_policy_amendment(project_root=Path.cwd(), request_path=request, book=book)
        if apply:
            execute_policy_amendment(plan)
    except _series_errors() as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    record = plan.record
    table = Table(title="Series Policy Amendment", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Amendment", record.amendment_id)
    table.add_row("Policy", record.policy_file.as_posix())
    table.add_row("Policy SHA-256 before", record.policy_sha256_before)
    table.add_row("Policy SHA-256 after", record.policy_sha256_after)
    table.add_row(
        "Policy version", f"{record.policy_version_before} → {record.policy_version_after}"
    )
    table.add_row("Terms", str(len(record.changes)))
    table.add_row("Action", plan.action if apply else f"{plan.action} (dry run)")
    console.print(table)
    changes = Table(title="Term Changes")
    for column in ("Term", "Status", "Target", "Aliases", "Authorized forms"):
        changes.add_column(column)
    for change in record.changes:
        changes.add_row(
            change.source_term,
            f"{change.status_before.value} ({change.provenance_before.value}) → "
            f"{change.status_after.value} ({change.provenance_after.value})",
            f"{change.target_before} → {change.target_after}",
            " ".join(
                [*(f"+{alias}" for alias in change.aliases_added)]
                + [f"-{alias}" for alias in change.aliases_removed]
            )
            or "unchanged",
            " ".join(
                [*(f"+{form}" for form in change.forms_added)]
                + [f"-{form}" for form in change.forms_removed]
            )
            or "unchanged",
        )
    console.print(changes)
    if not apply:
        console.print(
            "[dim]Dry run: rerun with --apply to publish the record and the policy.[/dim]"
        )
    else:
        console.print("Run derive-book-terminology --apply next for every book using this policy.")


@app.command("derive-book-terminology")
def derive_book_terminology_command(
    book: BookOption = None,
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Project the series policy onto this book's own chunks as its terminology registry."""
    from book_translator.series_terminology.workflow import (
        execute_book_terminology,
        plan_book_terminology,
    )

    _series_book_config(book)
    try:
        plan = plan_book_terminology(project_root=Path.cwd(), book=book)
        if apply:
            execute_book_terminology(plan)
    except _series_errors() as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Derived Book Terminology", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Registry", plan.registry_path.relative_to(plan.project_root).as_posix())
    table.add_row("Book", plan.registry.book_id)
    table.add_row("Policy terms", str(plan.summary.policy_terms))
    table.add_row("Terms occurring in this book", str(plan.summary.terms_with_occurrences))
    table.add_row("Decisions", str(len(plan.registry.decisions)))
    table.add_row("Occurrences", str(plan.summary.occurrences))
    table.add_row("Chapters with occurrences", str(plan.summary.chapters))
    table.add_row("Chunks with occurrences", str(plan.summary.chunks))
    table.add_row("Action", plan.action if apply else f"{plan.action} (dry run)")
    console.print(table)


@app.command("exempt-terminology-occurrence")
def exempt_terminology_occurrence_command(
    chunk_id: Annotated[str, typer.Argument(help="Chunk ID, e.g. 012-001.")],
    spelling: Annotated[str, typer.Option("--spelling", help="Byte-exact governed spelling.")],
    occurrence: Annotated[
        int, typer.Option("--occurrence", help="1-based occurrence of the spelling in the chunk.")
    ],
    rationale: Annotated[str, typer.Option("--rationale")],
    sense: Annotated[str, typer.Option("--sense")] = "ordinary_english",
    book: BookOption = None,
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Record the user's adjudication that one governed source occurrence is not the term.

    The exemption names exactly one occurrence, bound to the chunk's bytes, span, and decision;
    every other occurrence of the spelling stays governed. It reaches translations only through
    a new snapshot. Without --apply this is a dry run.
    """
    from book_translator.models import OccurrenceSense
    from book_translator.terminology.exemption_workflow import (
        execute_terminology_exemption,
        plan_terminology_exemption,
    )
    from book_translator.terminology.exemptions import TerminologyExemptionError

    try:
        plan = plan_terminology_exemption(
            project_root=Path.cwd(),
            book=book,
            chunk_id=chunk_id,
            spelling=spelling,
            occurrence_index=occurrence,
            sense=OccurrenceSense(sense),
            rationale=rationale,
        )
        if apply:
            execute_terminology_exemption(plan)
    except (TerminologyExemptionError, ProjectDataError, OSError, ValueError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    record = plan.planned.record
    table = Table(title="Terminology Occurrence Exemption", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for label, value in (
        ("Exemption", record.exemption_id),
        ("Chunk", record.chunk_id),
        ("Chunk source SHA-256", record.chunk_source_sha256),
        ("Spelling", record.spelling),
        ("Occurrence", str(record.occurrence_index)),
        ("Span", f"{record.start}-{record.end}"),
        ("Paragraph", f"{record.paragraph_number} ({record.paragraph_sha256[:16]}…)"),
        ("Candidate / decision", f"{record.candidate_id} / {record.decision_id}"),
        ("Series concept", record.concept_id or "none"),
        ("Sense", record.sense.value),
        ("Same spelling still governed in chunk", str(plan.planned.remaining_occurrences)),
        ("Registry", plan.registry_path.relative_to(plan.project_root).as_posix()),
        ("Resource version", f"{plan.version_before or 'none'} → {plan.version_after}"),
        ("Action", plan.action if apply else f"{plan.action} (dry run)"),
    ):
        table.add_row(label, value)
    console.print(table)
    if not apply:
        console.print("[dim]Dry run: rerun with --apply to publish the exemption.[/dim]")
    else:
        console.print("A new translation snapshot is required before any job is judged under it.")


@app.command("prepare-reference-context")
def prepare_reference_context_command(
    book: BookOption = None,
    chapter: Annotated[list[str] | None, typer.Option("--chapter")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Retrieve one immutable cross-book reference package per chunk from the pinned corpora."""
    from book_translator.series_terminology.workflow import (
        execute_reference_context,
        plan_reference_context,
    )

    _series_book_config(book)
    try:
        selected = [chapter_id_from_selector(item) for item in chapter] if chapter else None
        plan = plan_reference_context(
            project_root=Path.cwd(), book=book, chapters=selected, force=force
        )
        result = None
        if apply:

            def progress(chunk_id: str, position: int, total: int) -> None:
                console.print(f"[dim]retrieved {chunk_id} ({position}/{total})[/dim]")

            result = execute_reference_context(plan, progress=progress)
    except _series_errors() as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Reference Context", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Index", plan.index_path.relative_to(plan.project_root).as_posix())
    table.add_row("Corpora", ", ".join(c.corpus_id for c in plan.identity.corpora))
    table.add_row("Retriever SHA-256", plan.identity.retriever_sha256)
    table.add_row("Chunks", str(len(plan.chunks)))
    table.add_row("Reusable packages", str(len(plan.reusable)))
    table.add_row("Pending retrievals", str(len(plan.pending)))
    if result is not None:
        table.add_row("Retrieved", str(result.retrieved))
        table.add_row("Index SHA-256", result.index_sha256)
        table.add_row("Chunks with retrieval warnings", str(len(result.warnings)))
    table.add_row("Mode", "applied" if apply else "dry run")
    console.print(table)


@app.command("show-terminology-context")
def show_terminology_context_command(
    chunk_id: str,
    book: BookOption = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Render one chunk's effective terminology context from the current policy and packages.

    Counts and term-level decisions are printed; the full rendered section, which quotes
    reference prose, is written only to --output.
    """
    from book_translator.series_terminology.context import (
        build_effective_context,
        render_effective_context,
    )
    from book_translator.series_terminology.policy import load_policy
    from book_translator.series_terminology.workflow import (
        load_book_chunks,
        load_reference_package,
    )

    project_root, config = _series_book_config(book)
    try:
        policy = load_policy(project_root / (config.series_terminology_policy or Path()))
        chunks = {c.chunk_id: c for c in load_book_chunks(project_root, book)}
        if chunk_id not in chunks:
            raise ValueError(f"unknown chunk {chunk_id}")
        chunk = chunks[chunk_id]
        package = load_reference_package(project_root, book, chunk_id)
        context = build_effective_context(
            policy,
            source_text=chunk.source_text,
            context_text=chunk.context_text,
            reference=package,
        )
        rendered = render_effective_context(context)
        if output is not None:
            atomic_write_text(output, rendered, overwrite=True)
    except _series_errors() as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title=f"Effective Terminology Context {chunk_id}", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Contract", context.contract_version)
    table.add_row("LOCKED", ", ".join(f"{t.source_term} → {t.target_term}" for t in context.locked))
    table.add_row(
        "STRONG_REFERENCE",
        ", ".join(f"{t.source_term} → {t.target_term}" for t in context.strong),
    )
    table.add_row(
        "OBSERVED_VARIANTS",
        ", ".join(f"{t.source_term} ({len(t.variants)} variants)" for t in context.observed),
    )
    reference = context.reference
    if reference is not None:
        table.add_row(
            "Reference evidence",
            f"terms {len(reference.terminology)}, entities {len(reference.entities)}, "
            f"TM {len(reference.translation_memory)}, phrases {len(reference.phrases)}, "
            f"voice {len(reference.character_voice)}, style {len(reference.style)}",
        )
        excluded = reference.exclusions
        table.add_row(
            "Excluded",
            f"reference-only TM {excluded.reference_only_tm}, anomalies {excluded.anomalies}, "
            f"uncertain forms {excluded.uncertain_forms_withheld}, "
            f"ambiguous {excluded.ambiguous_matches}",
        )
    else:
        table.add_row("Reference evidence", "none (no package for this chunk)")
    table.add_row("Rendered bytes", str(len(rendered.encode("utf-8"))))
    table.add_row("Rendered SHA-256", hashlib.sha256(rendered.encode("utf-8")).hexdigest())
    table.add_row("Written to", output.as_posix() if output is not None else "(not written)")
    console.print(table)


@app.command("validate-series-terminology")
def validate_series_terminology_command(book: BookOption = None) -> None:
    """Prove, read-only, that this book's terminology lineage is complete and current."""
    from book_translator.series_terminology.workflow import resolve_terminology_lineage

    project_root, config = _series_book_config(book)
    try:
        lineage = resolve_terminology_lineage(project_root=project_root, book=book, config=config)
    except _series_errors() as exc:
        error_console.print(f"[bold red]Not ready:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    from rich.table import Table

    table = Table(title="Series Terminology Lineage", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Lineage", lineage.kind)
    table.add_row("Policy", (lineage.series_policy or Path()).as_posix())
    table.add_row("Derived registry", (lineage.terminology or Path()).as_posix())
    table.add_row("Reference context index", (lineage.reference_context_index or Path()).as_posix())
    if lineage.retrieval is not None:
        for corpus in lineage.retrieval.corpora:
            table.add_row(f"Corpus {corpus.corpus_id}", f"{corpus.corpus_version} {corpus.sha256}")
        table.add_row("Retriever", lineage.retrieval.retriever_sha256)
    table.add_row("Context contract", lineage.context_contract_version or "")
    console.print(table)
