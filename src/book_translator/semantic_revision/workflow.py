"""Plan and execute one targeted semantic revision, provider call included.

The boundary is deliberately narrow. A person names one job and one paragraph and states what is
wrong with it; the reviser is asked to correct exactly that, inside a window of exactly that
paragraph; the answer is spliced so every other paragraph is copied from the standing
translation; and the result is adopted only after the bounds, the full mechanical acceptance
battery, and an independent judgement of the revised paragraph all agree. Nothing here can
replace text the operator supplies, because the operator supplies none.

Planning writes nothing and contacts nobody. Execution publishes one immutable record — adopted
or refused — beside the gate's own revisions, and moves only the job's semantic-revision
pointers. The accepted attempt, its acceptance evidence, and every earlier revision stay exactly
as they were.
"""

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from book_translator.models import (
    SCHEMA_VERSION,
    OperatorDefectStatement,
    RevisedParagraphIdentity,
    SemanticParagraphOutcome,
    SemanticRevisionKind,
    SemanticRevisionVerification,
    SemanticRevisionWindow,
    SourceAnomalyRegistry,
    TerminologyRegistry,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
    TranslationSemanticRevisionEvidence,
)
from book_translator.paragraph_markers import ParagraphMarkerError, source_paragraphs
from book_translator.prompt_packaging.identity import uses_paragraph_markers
from book_translator.provider_execution.artifacts import (
    load_yaml,
    private_write,
    project_relative,
    serialize_yaml,
    sha256_bytes,
)
from book_translator.release_assembly import ReleaseAssemblyError
from book_translator.release_assembly.workflow import load_effective_acceptance
from book_translator.semantic_qa.engine import (
    CONTEXT_RADIUS,
    JudgeBatch,
    TerminologyConstraint,
    build_tasks,
)
from book_translator.semantic_qa.policy import EN_FA_JUDGE_V1, apply_policy
from book_translator.semantic_qa.providers import (
    SemanticJudgeError,
    TransportSemanticJudge,
)
from book_translator.semantic_qa.rendering import (
    JUDGE_PROMPT_VERSION,
    JUDGE_SCHEMA_VERSION,
    render_user_message,
)
from book_translator.semantic_qa.workflow import SEMANTIC_JUDGE_ENGINE_VERSION
from book_translator.semantic_revision.bounds import (
    RevisionScopeError,
    measure_bounds,
    splice_window,
)
from book_translator.semantic_revision.policy import SEMANTIC_REVISION_POLICY_VERSION
from book_translator.semantic_revision.providers import (
    SemanticReviserError,
    TransportSemanticReviser,
)
from book_translator.semantic_revision.rendering import (
    REVISER_SYSTEM_MESSAGE,
    REVISION_PROMPT_VERSION,
    render_revision_request,
)
from book_translator.semantic_revision.targeted import (
    TARGETED_SEMANTIC_REVISION_VERSION,
    AdoptionInputs,
    TargetedRevisionError,
    adoption_refusals,
    operator_findings,
    paragraph_identity,
    remaining_revision_budget,
    single_paragraph_window,
)
from book_translator.terminology import load_registry
from book_translator.terminology.exemption_workflow import frozen_occurrence_exemptions
from book_translator.terminology.registry import ExemptOccurrences
from book_translator.translation_acceptance_qa import assess_translation
from book_translator.translation_queue.locking import queue_lock
from book_translator.translation_queue.workflow import (
    LOCK_TIMEOUT_SECONDS,
    QueueState,
    _append_event,
    _event,
    _load_queue,
    _persist_job_and_run,
    load_settled_queue,
)

__all__ = [
    "TargetedRevisionPlan",
    "TargetedRevisionResult",
    "execute_targeted_revision",
    "plan_targeted_revision",
]

REVISION_DIRECTORY = "semantic-revision"


@dataclass(frozen=True, slots=True)
class TargetedRevisionPlan:
    """Everything proven before a provider is contacted, and nothing it may change."""

    project_root: Path
    run_path: Path
    run_id: str
    job: TranslationJob
    statement: OperatorDefectStatement
    window: SemanticRevisionWindow
    identity: RevisedParagraphIdentity
    source_file: Path
    source_sha256: str
    source_text: str
    standing_file: Path
    standing_sha256: str
    standing_text: str
    terminology_file: Path | None
    terminology_sha256: str | None
    registry: TerminologyRegistry
    exemptions: ExemptOccurrences | None
    anomaly_registry: SourceAnomalyRegistry | None
    terminology_constraints: tuple[TerminologyConstraint, ...]
    user_message: str
    output_contract: str
    snapshot_id: str
    manifest_sha256: str
    provider_revisions_used: int
    revision_number: int
    action: str
    existing_revision_file: Path | None = None

    @property
    def chunk_id(self) -> str:
        return self.job.chunk_id

    @property
    def paragraph_index(self) -> int:
        return self.window.first_index

    @property
    def destination(self) -> Path:
        return (
            self.run_path
            / "jobs"
            / self.chunk_id
            / REVISION_DIRECTORY
            / f"{self.revision_number:03d}"
        )

    @property
    def budget_remaining(self) -> int:
        return remaining_revision_budget(self.provider_revisions_used)

    @property
    def exemption_ids(self) -> tuple[str, ...]:
        return self.exemptions.exemption_ids if self.exemptions is not None else ()

    @property
    def request_sha256(self) -> str:
        return sha256_bytes(self.user_message.encode("utf-8"))

    @property
    def system_message_sha256(self) -> str:
        return sha256_bytes(REVISER_SYSTEM_MESSAGE.encode("utf-8"))

    @property
    def writable_paragraphs(self) -> tuple[int, ...]:
        return self.window.indexes

    @property
    def protected_paragraphs(self) -> int:
        return len(source_paragraphs(self.standing_text)) - len(self.window.indexes)


@dataclass(frozen=True, slots=True)
class TargetedRevisionResult:
    plan: TargetedRevisionPlan
    evidence_file: Path
    evidence: TranslationSemanticRevisionEvidence
    provider_calls: int
    adopted: bool


def _verified_text(path: Path, expected_sha256: str, label: str) -> str:
    content = path.read_bytes()
    if sha256_bytes(content) != expected_sha256:
        raise TargetedRevisionError(f"{label} hash mismatch: {path}")
    return content.decode("utf-8")


def _job_of(state: QueueState, job_id_or_chunk_id: str) -> TranslationJob:
    for job in state.jobs:
        if job_id_or_chunk_id in {job.job_id, job.chunk_id}:
            return job
    raise TargetedRevisionError(f"no job {job_id_or_chunk_id!r} in run {state.run.run_id}")


def _revision_history(
    run_path: Path, chunk_id: str
) -> tuple[TranslationSemanticRevisionEvidence, ...]:
    parent = run_path / "jobs" / chunk_id / REVISION_DIRECTORY
    return tuple(
        load_yaml(path, TranslationSemanticRevisionEvidence)
        for path in sorted(parent.glob("*/revision.yaml"))
    )


def _next_number(parent: Path) -> int:
    if not parent.is_dir():
        return 1
    return (
        max((int(p.name) for p in parent.iterdir() if p.is_dir() and p.name.isdigit()), default=0)
        + 1
    )


def _frozen_registry(
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
    reference = manifest.terminology.frozen_file or manifest.terminology.file
    path = project_root / reference
    _verified_text(path, manifest.terminology.sha256, "frozen terminology")
    return load_registry(path), reference, manifest.terminology.sha256


def _frozen_anomalies(
    manifest: TranslationManifest, project_root: Path
) -> SourceAnomalyRegistry | None:
    if manifest.source_anomalies is None:
        return None
    path = project_root / (manifest.source_anomalies.frozen_file or manifest.source_anomalies.file)
    _verified_text(path, manifest.source_anomalies.sha256, "frozen source anomalies")
    return load_yaml(path, SourceAnomalyRegistry)


def plan_targeted_revision(
    run_id_or_path: str | Path,
    job_id_or_chunk_id: str,
    *,
    paragraph_index: int,
    statement: OperatorDefectStatement,
    project_root: Path,
    expect_paragraph_sha256: str | None = None,
) -> TargetedRevisionPlan:
    """Prove every binding this revision rests on. Writes nothing, contacts nobody."""
    project_root = project_root.resolve()
    state = load_settled_queue(run_id_or_path, project_root=project_root)
    if not state.validation.valid or not state.snapshot_valid:
        raise TargetedRevisionError("translation run or frozen snapshot is not valid")
    job = _job_of(state, job_id_or_chunk_id)
    if job.status is not TranslationJobStatus.SUCCEEDED:
        raise TargetedRevisionError(f"job {job.job_id} is {job.status.value}, not succeeded")
    output_contract = state.run.output_contract_version
    if output_contract is None or not uses_paragraph_markers(output_contract):
        raise TargetedRevisionError(
            "a targeted revision needs the marked paragraph contract to bound its window"
        )
    try:
        acceptance = load_effective_acceptance(job, project_root=project_root)
    except ReleaseAssemblyError as exc:
        raise TargetedRevisionError(str(exc)) from exc
    source_text = _verified_text(
        project_root / acceptance.source_file, acceptance.source_sha256, "frozen chunk source"
    )
    if acceptance.source_sha256 != job.chunk_source_sha256:
        raise TargetedRevisionError("effective acceptance names another source for this job")
    standing_text = _verified_text(
        project_root / acceptance.translation_file,
        acceptance.translation_sha256,
        "standing translation",
    )
    manifest = load_yaml(
        project_root / state.run.snapshot_path / "manifest.yaml", TranslationManifest
    )
    registry, terminology_file, terminology_sha256 = _frozen_registry(manifest, project_root)
    exemptions = frozen_occurrence_exemptions(
        manifest,
        project_root,
        registry=registry,
        chunk_id=job.chunk_id,
        source_text=source_text,
    )
    window = single_paragraph_window(
        paragraph_index, paragraph_count=len(source_paragraphs(source_text))
    )
    identity = paragraph_identity(
        source_text=source_text,
        translation_text=standing_text,
        paragraph_index=paragraph_index,
        revised_text=None,
    )
    if (
        expect_paragraph_sha256 is not None
        and identity.target_paragraph_sha256_before != expect_paragraph_sha256
    ):
        raise TargetedRevisionError(
            "the named paragraph does not hash as the operator expected; it has moved or changed"
        )
    history = _revision_history(state.path, job.chunk_id)
    used = sum(
        1
        for record in history
        if record.provider_contacted and paragraph_index in record.window.indexes
    )
    tasks = build_tasks(
        source_text,
        standing_text,
        registry=registry,
        paragraph_indexes=(paragraph_index,),
        context_radius=CONTEXT_RADIUS,
        exemptions=exemptions,
    )
    task = tasks[0]
    source_blocks = source_paragraphs(source_text)
    standing_blocks = source_paragraphs(standing_text)
    user_message = render_revision_request(
        chapter_id=job.chapter_id,
        chunk_id=job.chunk_id,
        window_indexes=window.indexes,
        source_blocks=(source_blocks[paragraph_index - 1],),
        target_blocks=(standing_blocks[paragraph_index - 1],),
        findings=operator_findings(statement, paragraph_index),
        terminology=task.terminology,
        previous_source=task.previous_source,
        previous_target=task.previous_target,
        next_source=task.next_source,
        next_target=task.next_target,
    )
    action = "revise"
    existing: Path | None = None
    applied = next(
        (
            record
            for record in history
            if record.adopted
            and record.defect_origin == "operator"
            and record.operator_defect == statement
            and record.window.indexes == window.indexes
            and record.translation_sha256 == acceptance.translation_sha256
        ),
        None,
    )
    if applied is not None:
        action = "skip"
        existing = (
            state.path
            / "jobs"
            / job.chunk_id
            / REVISION_DIRECTORY
            / f"{applied.revision_number:03d}"
            / "revision.yaml"
        )
    elif used >= remaining_revision_budget(0):
        action = "budget_exhausted"
    return TargetedRevisionPlan(
        project_root=project_root,
        run_path=state.path,
        run_id=state.run.run_id,
        job=job,
        statement=statement,
        window=window,
        identity=identity,
        source_file=acceptance.source_file,
        source_sha256=acceptance.source_sha256,
        source_text=source_text,
        standing_file=acceptance.translation_file,
        standing_sha256=acceptance.translation_sha256,
        standing_text=standing_text,
        terminology_file=terminology_file,
        terminology_sha256=terminology_sha256,
        registry=registry,
        exemptions=exemptions,
        anomaly_registry=_frozen_anomalies(manifest, project_root),
        terminology_constraints=task.terminology,
        user_message=user_message,
        output_contract=output_contract,
        snapshot_id=state.run.snapshot_id,
        manifest_sha256=state.run.snapshot_manifest_sha256,
        provider_revisions_used=used,
        revision_number=_next_number(state.path / "jobs" / job.chunk_id / REVISION_DIRECTORY),
        action=action,
        existing_revision_file=existing,
    )


def _publish(directory: Path, files: dict[str, bytes]) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory.parent, 0o700)
    staging = Path(tempfile.mkdtemp(prefix=f".{directory.name}.staging.", dir=directory.parent))
    os.chmod(staging, 0o700)
    try:
        for name, content in files.items():
            private_write(staging / name, content)
        os.rename(staging, directory)
    except FileExistsError as exc:
        raise TargetedRevisionError(f"revision destination already exists: {directory}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _verify_paragraph(
    plan: TargetedRevisionPlan,
    revised_text: str,
    judge: TransportSemanticJudge,
) -> tuple[SemanticParagraphOutcome | None, SemanticRevisionVerification | None, str | None]:
    """Judge the revised paragraph on its own; adoption rests on this, never on the reviser."""
    tasks = build_tasks(
        plan.source_text,
        revised_text,
        registry=plan.registry,
        paragraph_indexes=(plan.paragraph_index,),
        context_radius=CONTEXT_RADIUS,
        exemptions=plan.exemptions,
    )
    batch = JudgeBatch(tasks=tasks)
    message = render_user_message(batch, chapter_id=plan.job.chapter_id, chunk_id=plan.chunk_id)
    try:
        call = judge.judge(
            run_id=plan.run_id,
            job_id=plan.job.job_id,
            chunk_id=plan.chunk_id,
            user_message=message,
            paragraph_ids=batch.paragraph_ids,
            source_language=plan.job.source_language,
            target_language=plan.job.target_language,
            repetition=1,
            batch_size=1,
        )
    except SemanticJudgeError as exc:
        return None, None, exc.code
    verdict = next(
        (item for item in call.response.verdicts if item.paragraph_id == tasks[0].paragraph_id),
        None,
    )
    if verdict is None:
        return None, None, "verdict_missing"
    policy_outcome, material, advisory = apply_policy(verdict, policy=EN_FA_JUDGE_V1)
    revised_blocks = source_paragraphs(revised_text)
    outcome = SemanticParagraphOutcome(
        paragraph_id=verdict.paragraph_id,
        paragraph_index=plan.paragraph_index,
        judge_verdict=verdict.verdict,
        policy_outcome=policy_outcome,
        judge_confidence=verdict.confidence,
        source_word_count=len(
            source_paragraphs(plan.source_text)[plan.paragraph_index - 1].split()
        ),
        target_word_count=len(revised_blocks[plan.paragraph_index - 1].split()),
        material_findings=list(material),
        advisory_findings=list(advisory),
    )
    verification = SemanticRevisionVerification(
        judge_engine_version=SEMANTIC_JUDGE_ENGINE_VERSION,
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        judge_schema_version=JUDGE_SCHEMA_VERSION,
        policy_version=EN_FA_JUDGE_V1.policy_version,
        outcome=outcome,
        request=call.record,
    )
    return outcome, verification, None


def _reports(plan: TargetedRevisionPlan, text: str):  # type: ignore[no-untyped-def]
    assessment = assess_translation(
        plan.source_text,
        text,
        registry=plan.registry,
        target_language=plan.job.target_language,
        output_contract=plan.output_contract,
        anomaly_registry=plan.anomaly_registry,
        chapter_id=plan.job.chapter_id,
        occurrence_exemptions=plan.exemptions,
    )
    return assessment.terminology_report, assessment.structural_report, assessment.alignment_report


def execute_targeted_revision(
    plan: TargetedRevisionPlan,
    *,
    reviser: TransportSemanticReviser,
    judge: TransportSemanticJudge,
    now: Callable[[], datetime] | None = None,
) -> TargetedRevisionResult:
    """Contact the reviser once, prove the answer, and publish one immutable record."""
    if plan.action != "revise":
        raise TargetedRevisionError(f"this plan is {plan.action}, not a revision to execute")
    timestamp = (now or (lambda: datetime.now(UTC)))()
    # The standing translation is re-proven against the bytes on disk; nothing may have moved
    # between planning and the provider call.
    _verified_text(
        plan.project_root / plan.standing_file, plan.standing_sha256, "standing translation"
    )
    provider_calls = 0
    provider_error: str | None = None
    marker_error: str | None = None
    scope_error: str | None = None
    revised_text: str | None = None
    request_record = None
    bounds = None
    try:
        call = reviser.revise(
            run_id=plan.run_id,
            job_id=plan.job.job_id,
            chunk_id=plan.chunk_id,
            user_message=plan.user_message,
            window_indexes=plan.window.indexes,
            source_language=plan.job.source_language,
            target_language=plan.job.target_language,
            attempt=plan.revision_number,
        )
        provider_calls += 1
        request_record = call.record
    except SemanticReviserError as exc:
        provider_calls += 1
        provider_error = exc.code
        call = None
    if call is not None:
        try:
            spliced = splice_window(
                plan.standing_text, call.text, window_indexes=plan.window.indexes
            )
            revised_text = spliced.text
        except (ParagraphMarkerError, RevisionScopeError) as exc:
            marker_error = str(exc)
    standing_reports = _reports(plan, plan.standing_text)
    standing_terminology = standing_reports[0]
    terminology, structural, alignment = (
        _reports(plan, revised_text) if revised_text is not None else standing_reports
    )
    if revised_text is not None:
        try:
            bounds = measure_bounds(
                plan.standing_text,
                revised_text,
                window_indexes=plan.window.indexes,
                terminology_blocking_before=tuple(
                    item.code for item in standing_terminology.findings if item.severity == "error"
                ),
                terminology_blocking_after=tuple(
                    item.code for item in terminology.findings if item.severity == "error"
                ),
            )
        except RevisionScopeError as exc:
            scope_error = str(exc)
    outcome = None
    verification = None
    verification_error: str | None = None
    if revised_text is not None and scope_error is None and marker_error is None:
        outcome, verification, verification_error = _verify_paragraph(plan, revised_text, judge)
        provider_calls += 1
    refusals = list(
        adoption_refusals(
            AdoptionInputs(
                standing_terminology=standing_terminology,
                terminology=terminology,
                structural=structural,
                alignment=alignment,
                verification=outcome,
                scope_error=scope_error,
                marker_error=marker_error,
                provider_error=provider_error,
                verification_error=verification_error,
            )
        )
    )
    adopted = not refusals and revised_text is not None
    destination = plan.destination
    identity = paragraph_identity(
        source_text=plan.source_text,
        translation_text=plan.standing_text,
        paragraph_index=plan.paragraph_index,
        revised_text=revised_text if adopted else None,
    )
    evidence = TranslationSemanticRevisionEvidence(
        schema_version=SCHEMA_VERSION,
        revision_engine_version=TARGETED_SEMANTIC_REVISION_VERSION,
        revision_prompt_version=REVISION_PROMPT_VERSION,
        revision_policy_version=SEMANTIC_REVISION_POLICY_VERSION,
        run_id=plan.run_id,
        job_id=plan.job.job_id,
        chunk_id=plan.chunk_id,
        revision_number=plan.revision_number,
        kind=SemanticRevisionKind.PROVIDER_REVISION,
        provider_contacted=True,
        window=plan.window,
        triggering_findings=list(
            operator_findings(plan.statement, plan.paragraph_index)[plan.paragraph_index]
        ),
        superseded_translation_file=plan.standing_file,
        superseded_translation_sha256=plan.standing_sha256,
        source_file=plan.source_file,
        source_sha256=plan.source_sha256,
        translation_file=project_relative(destination / "translation.md", plan.project_root)
        if adopted
        else None,
        translation_sha256=sha256_bytes(revised_text.encode("utf-8"))
        if adopted and revised_text is not None
        else None,
        terminology_file=plan.terminology_file,
        terminology_sha256=plan.terminology_sha256,
        request=request_record,
        bounds=bounds,
        marker_findings=[marker_error] if marker_error is not None else [],
        terminology_report=terminology if revised_text is not None else None,
        structural_report=structural if revised_text is not None else None,
        alignment_report=alignment if revised_text is not None else None,
        adopted=adopted,
        refusals=refusals,
        defect_origin="operator",
        operator_defect=plan.statement,
        revised_paragraph=identity,
        snapshot_id=plan.snapshot_id,
        manifest_sha256=plan.manifest_sha256,
        occurrence_exemption_ids=list(plan.exemption_ids),
        verification=verification,
        created_at=timestamp,
    )
    files = {"revision.yaml": serialize_yaml(evidence).encode("utf-8")}
    if adopted and revised_text is not None:
        files["translation.md"] = revised_text.encode("utf-8")
    _publish(destination, files)
    evidence_file = destination / "revision.yaml"
    # A refused revision is durable evidence on disk and is counted from there, but the job
    # keeps pointing at the translation that is still standing: a pointer to a record that
    # published no translation would make the job's own effective acceptance unresolvable.
    update: dict[str, object] = (
        {
            "semantic_revision_count": plan.job.semantic_revision_count + 1,
            "latest_semantic_revision_file": project_relative(evidence_file, plan.project_root),
            "latest_semantic_revision_sha256": sha256_bytes(evidence_file.read_bytes()),
        }
        if adopted
        else {}
    )
    _update_job(
        plan,
        update,
        "targeted_semantic_revision_adopted" if adopted else "targeted_semantic_revision_refused",
        timestamp,
    )
    return TargetedRevisionResult(
        plan=plan,
        evidence_file=evidence_file,
        evidence=evidence,
        provider_calls=provider_calls,
        adopted=adopted,
    )


def _update_job(
    plan: TargetedRevisionPlan, update: dict[str, object], event: str, now: datetime
) -> TranslationJob:
    with queue_lock(plan.run_path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        state = _load_queue(plan.run_path, plan.project_root)
        job = next(item for item in state.jobs if item.job_id == plan.job.job_id)
        if job.semantic_revision_count != plan.job.semantic_revision_count:
            raise TargetedRevisionError("the job's revision history changed during this revision")
        changed = job.model_copy(
            update={
                **update,
                "updated_at": now,
                "recent_events": _append_event(job, _event(event, now)),
            }
        )
        _persist_job_and_run(
            state,
            tuple(changed if item.job_id == plan.job.job_id else item for item in state.jobs),
            changed,
            now,
        )
        return changed
