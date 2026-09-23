"""The production semantic gate: judge, route, revise, re-judge, and stop.

This is the only layer of the gate with I/O, and the only place the four stages beneath it are
composed. The composition is a bounded state machine over paragraphs rather than a loop over a
job, because every rule the gate enforces is a rule about a paragraph: each one is judged, an
``INCONCLUSIVE`` one is granted exactly one wider look, a ``REVISE`` one is corrected inside an
explicit window at most twice and re-judged independently after every correction, and whatever
is still unproven when those grants are spent is recorded as *unresolved* — never accepted,
never quietly handed to a person.

The gate mutates a job's semantic dimension and nothing else. Mechanical acceptance, its origin,
its attempt, the provider attempt records, and every earlier evidence file stay byte-identical.
A run created before the gate existed records no gate policy and is refused here outright; its
translations are measured by the read-only audit instead.
"""

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from book_translator.models import (
    SCHEMA_VERSION,
    SemanticFinding,
    SemanticGateJobRecord,
    SemanticGateReport,
    SemanticGateStatus,
    SemanticJudgeRequestRecord,
    SemanticParagraphOutcome,
    SemanticQAEvidence,
    SemanticRevisionKind,
    SemanticRevisionWindow,
    SemanticVerdict,
    TerminologyRegistry,
    TranslationAcceptanceStatus,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
    TranslationSemanticRevisionEvidence,
)
from book_translator.paragraph_alignment import (
    MARKED,
    assess_alignment,
    build_alignment_report,
    policy_for,
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
from book_translator.semantic_gate.policy import PRODUCTION_GATE_V1, SemanticGatePolicy
from book_translator.semantic_qa.displacement import (
    DISPLACEMENT_CLASSIFIER_VERSION,
    classify_displacement,
    lexical_content,
    resegment_window,
)
from book_translator.semantic_qa.engine import (
    CONTEXT_RADIUS,
    build_batches,
    build_tasks,
)
from book_translator.semantic_qa.policy import EN_FA_JUDGE_V1, apply_policy
from book_translator.semantic_qa.providers import (
    JudgeCall,
    SemanticJudgeError,
    TransportSemanticJudge,
)
from book_translator.semantic_qa.rendering import (
    JUDGE_PROMPT_VERSION,
    JUDGE_SCHEMA_VERSION,
    render_user_message,
)
from book_translator.semantic_revision import (
    REVISION_PROMPT_VERSION,
    RevisionScopeError,
    SemanticReviserError,
    TransportSemanticReviser,
    measure_bounds,
    render_revision_request,
    splice_window,
    terminology_regressed,
)
from book_translator.terminology import load_registry
from book_translator.terminology import validation as qa_validation
from book_translator.terminology.exemption_workflow import frozen_occurrence_exemptions
from book_translator.terminology.registry import ExemptOccurrences
from book_translator.translation_queue import load_run_summary
from book_translator.translation_queue.locking import queue_lock
from book_translator.translation_queue.workflow import (
    LOCK_TIMEOUT_SECONDS,
    QueueState,
    _append_event,
    _event,
    _load_queue,
    _persist_job_and_run,
)

SEMANTIC_GATE_ENGINE_VERSION = "1.0.0"

__all__ = [
    "SEMANTIC_GATE_ENGINE_VERSION",
    "SemanticGateError",
    "SemanticGatePlan",
    "SemanticGateResult",
    "execute_semantic_gate",
    "plan_semantic_gate",
]


class SemanticGateError(ValueError):
    """Raised when the gate cannot run safely against a run's own evidence."""


@dataclass(frozen=True, slots=True)
class GatedJob:
    """One job resolved against the workspace and ready for the gate."""

    job: TranslationJob
    source_text: str
    source_file: Path
    source_sha256: str
    standing_text: str
    standing_file: Path
    standing_sha256: str
    terminology_file: Path | None
    terminology_sha256: str | None
    registry: TerminologyRegistry | None
    #: The occurrence exemptions this job's snapshot froze for its source, already proven.
    exemptions: ExemptOccurrences | None = None


@dataclass(frozen=True, slots=True)
class SemanticGatePlan:
    project_root: Path
    run_path: Path
    run_id: str
    source_language: str
    target_language: str
    policy: SemanticGatePolicy
    jobs: tuple[GatedJob, ...]
    skipped: tuple[str, ...]

    @property
    def planned_first_look_calls(self) -> int:
        return sum(
            -(-len(source_paragraphs(item.source_text)) // self.policy.batch_size)
            for item in self.jobs
            if item.job.semantic_status is None
        )


@dataclass(slots=True)
class SemanticGateResult:
    report: SemanticGateReport
    written: Path | None = None


@dataclass(slots=True)
class _Tally:
    judge_calls: int = 0
    reviser_calls: int = 0
    second_looks: int = 0
    resegmentations: int = 0
    adopted: int = 0
    refused: int = 0
    models: set[str] = field(default_factory=set)


def _verified_text(path: Path, expected_sha256: str, label: str) -> str:
    content = path.read_bytes()
    if sha256_bytes(content) != expected_sha256:
        raise SemanticGateError(f"{label} hash mismatch: {path}")
    return content.decode("utf-8")


def _frozen_terminology(
    manifest: TranslationManifest, project_root: Path
) -> tuple[Path | None, str | None, TerminologyRegistry | None]:
    if manifest.terminology is None:
        return None, None, None
    reference = manifest.terminology.frozen_file or manifest.terminology.file
    path = project_root / reference
    if sha256_bytes(path.read_bytes()) != manifest.terminology.sha256:
        raise SemanticGateError("frozen terminology resource hash mismatch")
    return reference, manifest.terminology.sha256, load_registry(path)


def plan_semantic_gate(
    run_id_or_path: str | Path,
    *,
    project_root: Path,
    job_id: str | None = None,
    chapter_id: str | None = None,
    policy: SemanticGatePolicy = PRODUCTION_GATE_V1,
) -> SemanticGatePlan:
    """Resolve every job the gate may act on. Reads only; contacts nothing."""
    project_root = project_root.resolve()
    state = load_run_summary(run_id_or_path, project_root=project_root)
    if state.run.semantic_gate_policy_version is None:
        raise SemanticGateError(
            "this run records no semantic gate policy; it predates the gate and can only be"
            " measured by the read-only semantic coverage audit"
        )
    if not uses_paragraph_markers(state.run.output_contract_version):
        raise SemanticGateError("the semantic gate governs marked-contract runs only")
    manifest = load_yaml(
        project_root / state.run.snapshot_path / "manifest.yaml", TranslationManifest
    )
    terminology_file, terminology_sha256, registry = _frozen_terminology(manifest, project_root)
    selected = tuple(
        job
        for job in state.jobs
        if (job_id is None or job.job_id == job_id)
        and (chapter_id is None or job.chapter_id == chapter_id)
    )
    if not selected:
        raise SemanticGateError("no translation job matches the semantic gate selection")
    gated: list[GatedJob] = []
    skipped: list[str] = []
    for job in selected:
        if (
            job.status is not TranslationJobStatus.SUCCEEDED
            or job.acceptance_status is not TranslationAcceptanceStatus.ACCEPTED
        ):
            skipped.append(f"{job.chunk_id}: not mechanically accepted")
            continue
        if job.semantic_status in {SemanticGateStatus.PASS, SemanticGateStatus.UNRESOLVED}:
            skipped.append(f"{job.chunk_id}: already {job.semantic_status.value}")
            continue
        try:
            acceptance = load_effective_acceptance(job, project_root=project_root)
            source_text = _verified_text(
                project_root / acceptance.source_file, acceptance.source_sha256, "frozen source"
            )
            standing_text = _verified_text(
                project_root / acceptance.translation_file,
                acceptance.translation_sha256,
                "standing translation",
            )
            if len(source_paragraphs(source_text)) != len(source_paragraphs(standing_text)):
                raise SemanticGateError("standing translation is not paragraph-aligned")
            exemptions = (
                frozen_occurrence_exemptions(
                    manifest,
                    project_root,
                    registry=registry,
                    chunk_id=job.chunk_id,
                    source_text=source_text,
                )
                if registry is not None
                else None
            )
        except (ReleaseAssemblyError, SemanticGateError, OSError) as exc:
            skipped.append(f"{job.chunk_id}: {exc}")
            continue
        gated.append(
            GatedJob(
                job=job,
                source_text=source_text,
                source_file=acceptance.source_file,
                source_sha256=acceptance.source_sha256,
                standing_text=standing_text,
                standing_file=acceptance.translation_file,
                standing_sha256=acceptance.translation_sha256,
                terminology_file=terminology_file,
                terminology_sha256=terminology_sha256,
                registry=registry,
                exemptions=exemptions,
            )
        )
    return SemanticGatePlan(
        project_root=project_root,
        run_path=state.path,
        run_id=state.run.run_id,
        source_language=state.run.source_language,
        target_language=state.run.target_language,
        policy=policy,
        jobs=tuple(gated),
        skipped=tuple(skipped),
    )


# --------------------------------------------------------------------------------------------
# Evidence publication
# --------------------------------------------------------------------------------------------


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
        raise SemanticGateError(f"evidence destination already exists: {directory}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _next_number(parent: Path) -> int:
    if not parent.is_dir():
        return 1
    return (
        max((int(p.name) for p in parent.iterdir() if p.is_dir() and p.name.isdigit()), default=0)
        + 1
    )


def _reload(state_path: Path, project_root: Path, job_id: str) -> tuple[QueueState, TranslationJob]:
    state = _load_queue(state_path, project_root)
    job = next(item for item in state.jobs if item.job_id == job_id)
    return state, job


def _update_job(
    run_path: Path,
    project_root: Path,
    job_id: str,
    update: dict[str, object],
    event: str,
    now: datetime,
) -> TranslationJob:
    with queue_lock(run_path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        state, job = _reload(run_path, project_root, job_id)
        changed = job.model_copy(
            update={
                **update,
                "updated_at": now,
                "recent_events": _append_event(job, _event(event, now)),
            }
        )
        _persist_job_and_run(
            state,
            tuple(changed if item.job_id == job_id else item for item in state.jobs),
            changed,
            now,
        )
        return changed


# --------------------------------------------------------------------------------------------
# Judging
# --------------------------------------------------------------------------------------------


def _outcomes_from(
    call: JudgeCall, item: GatedJob, standing: str, indexes: dict[str, int]
) -> list[SemanticParagraphOutcome]:
    source_blocks = source_paragraphs(item.source_text)
    target_blocks = source_paragraphs(standing)
    produced: list[SemanticParagraphOutcome] = []
    for verdict in call.response.verdicts:
        index = indexes[verdict.paragraph_id]
        outcome, material, advisory = apply_policy(verdict, policy=EN_FA_JUDGE_V1)
        produced.append(
            SemanticParagraphOutcome(
                paragraph_id=verdict.paragraph_id,
                paragraph_index=index,
                judge_verdict=verdict.verdict,
                policy_outcome=outcome,
                judge_confidence=verdict.confidence,
                source_word_count=len(source_blocks[index - 1].split()),
                target_word_count=len(target_blocks[index - 1].split()),
                material_findings=list(material),
                advisory_findings=list(advisory),
            )
        )
    return produced


def _judge(
    plan: SemanticGatePlan,
    item: GatedJob,
    standing: str,
    *,
    judge: TransportSemanticJudge,
    indexes: tuple[int, ...],
    radius: int,
    batch_size: int,
    look: str,
    tally: _Tally,
    failures: list[str],
) -> tuple[list[SemanticParagraphOutcome], list[SemanticJudgeRequestRecord]]:
    tasks = build_tasks(
        item.source_text,
        standing,
        registry=item.registry,
        paragraph_indexes=indexes,
        context_radius=radius,
        exemptions=item.exemptions,
    )
    by_id = {task.paragraph_id: task.paragraph_index for task in tasks}
    outcomes: list[SemanticParagraphOutcome] = []
    records: list[SemanticJudgeRequestRecord] = []
    for batch in build_batches(tasks, batch_size=batch_size):
        message = render_user_message(
            batch, chapter_id=item.job.chapter_id, chunk_id=item.job.chunk_id
        )
        tally.judge_calls += 1
        try:
            call = judge.judge(
                run_id=plan.run_id,
                job_id=item.job.job_id,
                chunk_id=item.job.chunk_id,
                user_message=message,
                paragraph_ids=batch.paragraph_ids,
                source_language=plan.source_language,
                target_language=plan.target_language,
                repetition=1,
                batch_size=batch_size,
            )
        except SemanticJudgeError as exc:
            failures.append(f"{look} judge {','.join(batch.paragraph_ids)}: {exc.code}")
            continue
        if call.record.model_returned:
            tally.models.add(call.record.model_returned)
        records.append(call.record)
        outcomes.extend(_outcomes_from(call, item, standing, by_id))
    return outcomes, records


def _publish_judgement(
    plan: SemanticGatePlan,
    item: GatedJob,
    standing_file: Path,
    standing_sha256: str,
    outcomes: list[SemanticParagraphOutcome],
    records: list[SemanticJudgeRequestRecord],
    *,
    look: str,
    radius: int,
    now: datetime,
) -> Path:
    parent = plan.run_path / "jobs" / item.job.chunk_id / "semantic-qa"
    number = _next_number(parent)
    counts = {verdict: 0 for verdict in SemanticVerdict}
    for outcome in outcomes:
        counts[outcome.policy_outcome] += 1
    evidence = SemanticQAEvidence(
        schema_version=SCHEMA_VERSION,
        judge_engine_version=SEMANTIC_GATE_ENGINE_VERSION,
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        judge_schema_version=JUDGE_SCHEMA_VERSION,
        policy_version=EN_FA_JUDGE_V1.policy_version,
        run_id=plan.run_id,
        job_id=item.job.job_id,
        chapter_id=item.job.chapter_id,
        chunk_id=item.job.chunk_id,
        evaluation_number=number,
        repetition=1,
        look="second" if look == "second" else "first",
        context_radius=radius,
        context_policy_version=plan.policy.second_look_policy_version if look == "second" else None,
        identity_mode="marked",
        source_file=item.source_file,
        source_sha256=item.source_sha256,
        translation_file=standing_file,
        translation_sha256=standing_sha256,
        terminology_file=item.terminology_file,
        terminology_sha256=item.terminology_sha256,
        context_sha256=item.job.context_sha256,
        paragraph_count=len(source_paragraphs(item.source_text)),
        judged_paragraphs=len(outcomes),
        pass_count=counts[SemanticVerdict.PASS],
        revise_count=counts[SemanticVerdict.REVISE],
        inconclusive_count=counts[SemanticVerdict.INCONCLUSIVE],
        requests=records,
        outcomes=sorted(outcomes, key=lambda o: o.paragraph_index),
        created_at=now,
    )
    destination = parent / f"{number:03d}"
    _publish(destination, {"evidence.yaml": serialize_yaml(evidence).encode("utf-8")})
    return destination / "evidence.yaml"


# --------------------------------------------------------------------------------------------
# Revision
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Candidate:
    text: str
    kind: SemanticRevisionKind
    request: SemanticJudgeRequestRecord | None
    lexical_preserved: bool | None


def _mechanical_reports(item: GatedJob, text: str, target_language: str):  # type: ignore[no-untyped-def]
    registry = item.registry or TerminologyRegistry(
        schema_version=SCHEMA_VERSION,
        version="1.0.0",
        book_id="unregistered",
        source_language="en",
        target_language=target_language,
    )
    terminology = qa_validation.validate_terminology(
        item.source_text, text, registry, item.exemptions
    )
    structural = qa_validation.validate_translation_qa(
        item.source_text, text, target_language=target_language
    )
    alignment = build_alignment_report(
        assess_alignment(
            item.source_text, text, policy=policy_for(target_language), contract_mode=MARKED
        ),
        source_text=item.source_text,
        translation_text=text,
    )
    return terminology, structural, alignment


def _revise_window(
    plan: SemanticGatePlan,
    item: GatedJob,
    standing_text: str,
    standing_file: Path,
    standing_sha256: str,
    semantic_file: Path,
    *,
    window: SemanticRevisionWindow,
    findings: dict[int, tuple[SemanticFinding, ...]],
    reviser: TransportSemanticReviser,
    try_resegmentation: bool,
    tally: _Tally,
    now: datetime,
) -> tuple[Path, str | None]:
    """Produce, judge mechanically, and publish one revision attempt. Returns (evidence, text)."""
    source_blocks = source_paragraphs(item.source_text)
    standing_blocks = source_paragraphs(standing_text)
    indexes = window.indexes
    refusals: list[str] = []
    candidate: _Candidate | None = None
    classifier_version: str | None = None
    if try_resegmentation:
        classifier_version = DISPLACEMENT_CLASSIFIER_VERSION
        result = resegment_window(
            tuple(source_blocks[i - 1] for i in indexes),
            tuple(standing_blocks[i - 1] for i in indexes),
            first_index=window.first_index,
            policy=policy_for(plan.target_language),
        )
        if result.applicable:
            blocks = list(standing_blocks)
            for index, block in zip(indexes, result.blocks, strict=True):
                blocks[index - 1] = block
            candidate = _Candidate(
                "\n\n".join(blocks) + "\n",
                SemanticRevisionKind.DETERMINISTIC_RESEGMENTATION,
                None,
                lexical_content(standing_text) == lexical_content("\n\n".join(blocks) + "\n"),
            )
            tally.resegmentations += 1
        else:
            refusals.append("resegmentation:" + ",".join(result.refusals))
    if candidate is None:
        refusals = [entry for entry in refusals if not entry.startswith("resegmentation:")] + [
            entry for entry in refusals if entry.startswith("resegmentation:")
        ]
        tasks = build_tasks(
            item.source_text,
            standing_text,
            registry=item.registry,
            paragraph_indexes=indexes,
            exemptions=item.exemptions,
        )
        terminology_constraints = tuple(
            {
                (c.source_term, c.target_term, c.required, c.allowed_target_forms): c
                for task in tasks
                for c in task.terminology
            }.values()
        )
        first = tasks[0]
        last = tasks[-1]
        message = render_revision_request(
            chapter_id=item.job.chapter_id,
            chunk_id=item.job.chunk_id,
            window_indexes=indexes,
            source_blocks=tuple(source_blocks[i - 1] for i in indexes),
            target_blocks=tuple(standing_blocks[i - 1] for i in indexes),
            findings=findings,
            terminology=terminology_constraints,
            previous_source=first.previous_source,
            previous_target=first.previous_target,
            next_source=last.next_source,
            next_target=last.next_target,
        )
        tally.reviser_calls += 1
        attempt = _next_number(plan.run_path / "jobs" / item.job.chunk_id / "semantic-revision")
        try:
            call = reviser.revise(
                run_id=plan.run_id,
                job_id=item.job.job_id,
                chunk_id=item.job.chunk_id,
                user_message=message,
                window_indexes=indexes,
                source_language=plan.source_language,
                target_language=plan.target_language,
                attempt=attempt,
            )
        except SemanticReviserError as exc:
            refusals.append(f"reviser:{exc.code}")
            call = None
        if call is not None:
            if call.record.model_returned:
                tally.models.add(call.record.model_returned)
            try:
                spliced = splice_window(standing_text, call.text, window_indexes=indexes)
                candidate = _Candidate(
                    spliced.text, SemanticRevisionKind.PROVIDER_REVISION, call.record, None
                )
            except (ParagraphMarkerError, RevisionScopeError) as exc:
                refusals.append(f"markers:{exc}")
                candidate = _Candidate(
                    "", SemanticRevisionKind.PROVIDER_REVISION, call.record, None
                )
    marker_findings: list[str] = [entry for entry in refusals if entry.startswith("markers:")]
    bounds = None
    reports = None
    adopted = False
    if candidate is not None and candidate.text:
        standing_terms, _, _ = _mechanical_reports(item, standing_text, plan.target_language)
        terminology, structural, alignment = _mechanical_reports(
            item, candidate.text, plan.target_language
        )
        regressed = terminology_regressed(standing_terms, terminology)
        try:
            bounds = measure_bounds(
                standing_text,
                candidate.text,
                window_indexes=indexes,
                terminology_blocking_before=tuple(
                    f.code for f in standing_terms.findings if f.severity == "error"
                ),
                terminology_blocking_after=tuple(
                    f.code for f in terminology.findings if f.severity == "error"
                ),
                lexical_content_preserved=candidate.lexical_preserved,
            )
        except RevisionScopeError as exc:
            refusals.append(f"scope:{exc}")
        if regressed:
            refusals.append("terminology_regression:" + ",".join(regressed))
        if not structural.valid:
            refusals.append(
                "structural:"
                + ",".join(f.code for f in structural.findings if f.severity == "error")
            )
        if not alignment.valid:
            refusals.append(
                "alignment:" + ",".join(f.code for f in alignment.findings if f.severity == "error")
            )
        reports = (terminology, structural, alignment)
        adopted = not refusals
    parent = plan.run_path / "jobs" / item.job.chunk_id / "semantic-revision"
    number = _next_number(parent)
    destination = parent / f"{number:03d}"
    translation_path = destination / "translation.md" if adopted else None
    evidence = TranslationSemanticRevisionEvidence(
        schema_version=SCHEMA_VERSION,
        revision_engine_version=SEMANTIC_GATE_ENGINE_VERSION,
        revision_prompt_version=REVISION_PROMPT_VERSION
        if candidate is not None and candidate.kind is SemanticRevisionKind.PROVIDER_REVISION
        else None,
        revision_policy_version=plan.policy.revision_policy_version,
        displacement_classifier_version=classifier_version,
        run_id=plan.run_id,
        job_id=item.job.job_id,
        chunk_id=item.job.chunk_id,
        revision_number=number,
        kind=candidate.kind if candidate is not None else SemanticRevisionKind.PROVIDER_REVISION,
        provider_contacted=candidate is not None
        and candidate.kind is SemanticRevisionKind.PROVIDER_REVISION,
        window=window,
        triggering_findings=[f for index in indexes for f in findings.get(index, ())],
        superseded_translation_file=standing_file,
        superseded_translation_sha256=standing_sha256,
        superseded_semantic_file=semantic_file,
        superseded_semantic_sha256=sha256_bytes((plan.project_root / semantic_file).read_bytes()),
        source_file=item.source_file,
        source_sha256=item.source_sha256,
        translation_file=project_relative(translation_path, plan.project_root)
        if translation_path
        else None,
        translation_sha256=sha256_bytes(candidate.text.encode("utf-8"))
        if adopted and candidate
        else None,
        terminology_file=item.terminology_file,
        terminology_sha256=item.terminology_sha256,
        request=candidate.request if candidate is not None else None,
        bounds=bounds,
        marker_findings=marker_findings,
        terminology_report=reports[0] if reports else None,
        structural_report=reports[1] if reports else None,
        alignment_report=reports[2] if reports else None,
        adopted=adopted,
        refusals=refusals,
        created_at=now,
    )
    files = {"revision.yaml": serialize_yaml(evidence).encode("utf-8")}
    if adopted and candidate is not None:
        files["translation.md"] = candidate.text.encode("utf-8")
        tally.adopted += 1
    else:
        tally.refused += 1
    _publish(destination, files)
    return destination / "revision.yaml", candidate.text if adopted and candidate else None


# --------------------------------------------------------------------------------------------
# The per-job state machine
# --------------------------------------------------------------------------------------------


def _load_history(
    plan: SemanticGatePlan, chunk_id: str
) -> tuple[list[SemanticQAEvidence], list[TranslationSemanticRevisionEvidence]]:
    judgements: list[SemanticQAEvidence] = []
    revisions: list[TranslationSemanticRevisionEvidence] = []
    base = plan.run_path / "jobs" / chunk_id
    for path in sorted((base / "semantic-qa").glob("*/evidence.yaml")):
        judgements.append(load_yaml(path, SemanticQAEvidence))
    for path in sorted((base / "semantic-revision").glob("*/revision.yaml")):
        revisions.append(load_yaml(path, TranslationSemanticRevisionEvidence))
    return judgements, revisions


def _job_status(
    current: dict[int, SemanticParagraphOutcome], unresolved: set[int]
) -> SemanticGateStatus:
    outcomes = {index: item.policy_outcome for index, item in current.items()}
    if all(value is SemanticVerdict.PASS for value in outcomes.values()) and not unresolved:
        return SemanticGateStatus.PASS
    if unresolved and all(
        value is SemanticVerdict.PASS or index in unresolved for index, value in outcomes.items()
    ):
        return SemanticGateStatus.UNRESOLVED
    if any(value is SemanticVerdict.REVISE for value in outcomes.values()):
        return SemanticGateStatus.REVISE
    return SemanticGateStatus.INCONCLUSIVE


def _process_job(
    plan: SemanticGatePlan,
    item: GatedJob,
    *,
    judge: TransportSemanticJudge,
    reviser: TransportSemanticReviser,
    tally: _Tally,
    now: datetime,
) -> SemanticGateJobRecord:
    policy = plan.policy
    failures: list[str] = []
    record_calls_before = tally.judge_calls
    seconds_before = tally.second_looks
    revisions_before = tally.reviser_calls + tally.resegmentations
    adopted_before = tally.adopted
    reseg_before = tally.resegmentations
    judgements, revisions = _load_history(plan, item.job.chunk_id)
    standing_text = item.standing_text
    standing_file = item.standing_file
    standing_sha256 = item.standing_sha256
    total = len(source_paragraphs(item.source_text))
    current: dict[int, SemanticParagraphOutcome] = {}
    for evidence in judgements:
        for outcome in evidence.outcomes:
            current[outcome.paragraph_index] = outcome
    second_looked = {
        outcome.paragraph_index
        for evidence in judgements
        if evidence.look == "second"
        for outcome in evidence.outcomes
    }
    revision_attempts: dict[int, int] = {}
    resegmented: set[tuple[int, int]] = set()
    for revision in revisions:
        for index in revision.window.indexes:
            if revision.provider_contacted:
                revision_attempts[index] = revision_attempts.get(index, 0) + 1
        if revision.kind is SemanticRevisionKind.DETERMINISTIC_RESEGMENTATION:
            resegmented.add((revision.window.first_index, revision.window.last_index))
    latest_semantic: Path | None = item.job.latest_semantic_file
    evidence_count = item.job.semantic_evidence_count
    revision_count = item.job.semantic_revision_count
    latest_revision: Path | None = item.job.latest_semantic_revision_file
    unresolved: set[int] = set()

    def record(status: SemanticGateStatus, semantic_file: Path) -> None:
        nonlocal latest_semantic, evidence_count
        evidence_count += 1
        latest_semantic = project_relative(semantic_file, plan.project_root)
        _update_job(
            plan.run_path,
            plan.project_root,
            item.job.job_id,
            {
                "semantic_status": status,
                "semantic_evidence_count": evidence_count,
                "latest_semantic_file": latest_semantic,
                "latest_semantic_sha256": sha256_bytes(semantic_file.read_bytes()),
            },
            f"semantic_{status.value}",
            now,
        )

    # First look: judge every paragraph the job has never had a verdict for.
    missing = tuple(index for index in range(1, total + 1) if index not in current)
    if missing:
        outcomes, records = _judge(
            plan,
            item,
            standing_text,
            judge=judge,
            indexes=missing,
            radius=CONTEXT_RADIUS,
            batch_size=policy.batch_size,
            look="first",
            tally=tally,
            failures=failures,
        )
        if outcomes:
            path = _publish_judgement(
                plan,
                item,
                standing_file,
                standing_sha256,
                outcomes,
                records,
                look="first",
                radius=CONTEXT_RADIUS,
                now=now,
            )
            for outcome in outcomes:
                current[outcome.paragraph_index] = outcome
            record(_job_status(current, unresolved), path)
    if len(current) < total:
        return SemanticGateJobRecord(
            job_id=item.job.job_id,
            chunk_id=item.job.chunk_id,
            initial_status=item.job.semantic_status,
            final_status=_job_status(current, unresolved) if current else None,
            judge_calls=tally.judge_calls - record_calls_before,
            second_looks=0,
            revisions_attempted=0,
            revisions_adopted=0,
            resegmentations=0,
            paragraphs_judged=len(current),
            unresolved_paragraphs=[],
            failures=failures,
        )

    guard = 0
    while guard < 2 * policy.max_revisions_per_window + policy.max_second_looks + 2:
        guard += 1
        progressed = False
        # Second looks for INCONCLUSIVE paragraphs.
        pending = [
            index
            for index, outcome in sorted(current.items())
            if outcome.policy_outcome is SemanticVerdict.INCONCLUSIVE and index not in unresolved
        ]
        for index in pending:
            if index in second_looked or policy.max_second_looks < 1:
                unresolved.add(index)
                continue
            tally.second_looks += 1
            second_looked.add(index)
            outcomes, records = _judge(
                plan,
                item,
                standing_text,
                judge=judge,
                indexes=(index,),
                radius=policy.second_look_radius,
                batch_size=1,
                look="second",
                tally=tally,
                failures=failures,
            )
            if not outcomes:
                unresolved.add(index)
                continue
            path = _publish_judgement(
                plan,
                item,
                standing_file,
                standing_sha256,
                outcomes,
                records,
                look="second",
                radius=policy.second_look_radius,
                now=now,
            )
            current[index] = outcomes[0]
            progressed = True
            if outcomes[0].policy_outcome is SemanticVerdict.INCONCLUSIVE:
                unresolved.add(index)
            record(_job_status(current, unresolved), path)
        # Revisions for REVISE paragraphs, displacement windows first.
        revising = [
            index
            for index, outcome in sorted(current.items())
            if outcome.policy_outcome is SemanticVerdict.REVISE and index not in unresolved
        ]
        if not revising:
            break
        windows: list[tuple[SemanticRevisionWindow, bool]] = []
        covered: set[int] = set()
        for displacement in classify_displacement(tuple(current.values())):
            span = SemanticRevisionWindow(
                first_index=displacement.first_index,
                last_index=displacement.last_index,
                reason="boundary_displacement",
            )
            windows.append((span, policy.prefer_deterministic_resegmentation))
            covered.update(span.indexes)
        for index in revising:
            if index not in covered:
                windows.append(
                    (
                        SemanticRevisionWindow(
                            first_index=index, last_index=index, reason="single_paragraph"
                        ),
                        False,
                    )
                )
        for window, resegment in windows:
            attempts = max(revision_attempts.get(index, 0) for index in window.indexes)
            already = (window.first_index, window.last_index) in resegmented
            try_reseg = resegment and not already
            if attempts >= policy.max_revisions_per_window and not try_reseg:
                unresolved.update(
                    index
                    for index in window.indexes
                    if current[index].policy_outcome is not SemanticVerdict.PASS
                )
                continue
            assert latest_semantic is not None
            findings = {
                index: tuple(current[index].material_findings)
                for index in window.indexes
                if index in current
            }
            evidence_path, revised = _revise_window(
                plan,
                item,
                standing_text,
                standing_file,
                standing_sha256,
                latest_semantic,
                window=window,
                findings=findings,
                reviser=reviser,
                try_resegmentation=try_reseg,
                tally=tally,
                now=now,
            )
            revision = load_yaml(evidence_path, TranslationSemanticRevisionEvidence)
            revision_count += 1
            latest_revision = project_relative(evidence_path, plan.project_root)
            if revision.provider_contacted:
                for index in window.indexes:
                    revision_attempts[index] = revision_attempts.get(index, 0) + 1
            if try_reseg:
                resegmented.add((window.first_index, window.last_index))
            update: dict[str, object] = {
                "semantic_revision_count": revision_count,
                "latest_semantic_revision_file": latest_revision,
                "latest_semantic_revision_sha256": sha256_bytes(evidence_path.read_bytes()),
            }
            # An attempt was spent whether or not it was adopted, so the state machine has
            # progressed: a refused provider answer is retried while budget remains and the
            # budget, not the refusal, is what ends the loop.
            progressed = True
            if revised is None:
                # A refused revision is still counted and pointed at, so a resumed gate can see
                # the budget it spent; the standing translation is unchanged.
                _update_job(
                    plan.run_path,
                    plan.project_root,
                    item.job.job_id,
                    update,
                    "semantic_revision_refused",
                    now,
                )
                continue
            standing_text = revised
            standing_file = project_relative(
                evidence_path.parent / "translation.md", plan.project_root
            )
            standing_sha256 = sha256_bytes(revised.encode("utf-8"))
            _update_job(
                plan.run_path,
                plan.project_root,
                item.job.job_id,
                update,
                "semantic_revision_adopted",
                now,
            )
            progressed = True
            # Independent re-judge of exactly the revised window against the new candidate.
            outcomes, records = _judge(
                plan,
                item,
                standing_text,
                judge=judge,
                indexes=window.indexes,
                radius=CONTEXT_RADIUS,
                batch_size=policy.batch_size,
                look="first",
                tally=tally,
                failures=failures,
            )
            if not outcomes:
                continue
            path = _publish_judgement(
                plan,
                item,
                standing_file,
                standing_sha256,
                outcomes,
                records,
                look="first",
                radius=CONTEXT_RADIUS,
                now=now,
            )
            for outcome in outcomes:
                current[outcome.paragraph_index] = outcome
                if (
                    outcome.policy_outcome is SemanticVerdict.INCONCLUSIVE
                    and outcome.paragraph_index in second_looked
                ):
                    unresolved.add(outcome.paragraph_index)
            record(_job_status(current, unresolved), path)
        if not progressed:
            break
    # Anything still REVISE with no budget, or INCONCLUSIVE after its look, is terminal.
    for index, outcome in current.items():
        if (
            outcome.policy_outcome is SemanticVerdict.REVISE
            and revision_attempts.get(index, 0) >= policy.max_revisions_per_window
        ):
            unresolved.add(index)
        if outcome.policy_outcome is SemanticVerdict.INCONCLUSIVE and index in second_looked:
            unresolved.add(index)
    final = _job_status(current, unresolved)
    if latest_semantic is not None:
        _update_job(
            plan.run_path,
            plan.project_root,
            item.job.job_id,
            {"semantic_status": final},
            f"semantic_{final.value}",
            now,
        )
    return SemanticGateJobRecord(
        job_id=item.job.job_id,
        chunk_id=item.job.chunk_id,
        initial_status=item.job.semantic_status,
        final_status=final,
        judge_calls=tally.judge_calls - record_calls_before,
        second_looks=tally.second_looks - seconds_before,
        revisions_attempted=(tally.reviser_calls + tally.resegmentations) - revisions_before,
        revisions_adopted=tally.adopted - adopted_before,
        resegmentations=tally.resegmentations - reseg_before,
        paragraphs_judged=len(current),
        unresolved_paragraphs=[f"P{index:04d}" for index in sorted(unresolved)],
        failures=failures,
    )


def execute_semantic_gate(
    plan: SemanticGatePlan,
    *,
    judge: TransportSemanticJudge,
    reviser: TransportSemanticReviser,
    now: Callable[[], datetime] | None = None,
    dry_run: bool = False,
) -> SemanticGateResult:
    timestamp = (now or (lambda: datetime.now(UTC)))()
    tally = _Tally()
    records: list[SemanticGateJobRecord] = []
    if not dry_run:
        for item in plan.jobs:
            records.append(
                _process_job(plan, item, judge=judge, reviser=reviser, tally=tally, now=timestamp)
            )
    counts: dict[str, int] = {}
    for record in records:
        key = record.final_status.value if record.final_status else "not_judged"
        counts[key] = counts.get(key, 0) + 1
    report = SemanticGateReport(
        schema_version=SCHEMA_VERSION,
        gate_engine_version=SEMANTIC_GATE_ENGINE_VERSION,
        gate_policy_version=plan.policy.policy_version,
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        judge_schema_version=JUDGE_SCHEMA_VERSION,
        judge_policy_version=EN_FA_JUDGE_V1.policy_version,
        second_look_policy_version=plan.policy.second_look_policy_version,
        revision_prompt_version=REVISION_PROMPT_VERSION,
        revision_policy_version=plan.policy.revision_policy_version,
        displacement_classifier_version=DISPLACEMENT_CLASSIFIER_VERSION,
        run_id=plan.run_id,
        provider=judge.provider_name,
        adapter_version=judge.adapter_version,
        models_returned=sorted(tally.models),
        dry_run=dry_run,
        jobs_selected=len(plan.jobs),
        jobs_processed=len(records),
        judge_calls=tally.judge_calls,
        reviser_calls=tally.reviser_calls,
        second_looks=tally.second_looks,
        resegmentations=tally.resegmentations,
        revisions_adopted=tally.adopted,
        revisions_refused=tally.refused,
        status_counts=dict(sorted(counts.items())),
        jobs=records,
        created_at=timestamp,
    )
    if dry_run:
        return SemanticGateResult(report=report)
    parent = plan.run_path / "semantic-gate"
    destination = parent / f"{_next_number(parent):03d}"
    _publish(destination, {"report.yaml": serialize_yaml(report).encode("utf-8")})
    return SemanticGateResult(report=report, written=destination / "report.yaml")
