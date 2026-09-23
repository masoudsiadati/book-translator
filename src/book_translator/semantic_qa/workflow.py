"""The Semantic Coverage Judge boundary: the only layer here that performs I/O.

It reads a completed or in-progress run, resolves each selected job's effective acceptance through
the same canonical resolver release assembly uses, builds judge tasks from the frozen source and
the accepted translation, asks the provider, validates every answer, applies the policy, and
publishes immutable evidence of its own.

It changes nothing it reads. No acceptance status, no job status, no counter, no run record, no
translated byte, and no release. A run that predates semantic QA has no semantic evidence, and
that absence means *not judged* rather than *passed*; nothing here backfills a verdict onto
history. Evidence is published outside every run directory for the same reason the paragraph
alignment audit is: a measurement of a run must not become part of it.
"""

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from book_translator.models import (
    SCHEMA_VERSION,
    TerminologyRegistry,
    TranslationAttempt,
    TranslationJob,
    TranslationManifest,
)
from book_translator.models.semantic_qa import (
    SemanticCoverageReport,
    SemanticParagraphOutcome,
    SemanticQAEvidence,
    SemanticVerdict,
)
from book_translator.paths import ProjectPaths
from book_translator.prompt_packaging.identity import uses_paragraph_markers
from book_translator.provider_execution import ExecutorError
from book_translator.provider_execution.artifacts import (
    load_yaml,
    private_write,
    serialize_yaml,
    sha256_bytes,
)
from book_translator.release_assembly import ReleaseAssemblyError
from book_translator.release_assembly.workflow import load_effective_acceptance
from book_translator.semantic_qa.engine import (
    CONTEXT_RADIUS,
    SECOND_LOOK_CONTEXT_POLICY_VERSION,
    SemanticTaskError,
    build_batches,
    build_tasks,
)
from book_translator.semantic_qa.policy import (
    EN_FA_JUDGE_V1,
    SemanticPolicy,
    apply_policy,
)
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
from book_translator.terminology import load_registry
from book_translator.translation_queue import load_run_summary

#: The orchestration's own version, independent of the prompt, the schema, and the policy.
SEMANTIC_JUDGE_ENGINE_VERSION = "1.0.0"

__all__ = [
    "SEMANTIC_JUDGE_ENGINE_VERSION",
    "ChunkSelection",
    "SemanticCoverageError",
    "SemanticCoveragePlan",
    "execute_semantic_coverage",
    "plan_semantic_coverage",
]


class SemanticCoverageError(ValueError):
    """Raised when a semantic coverage evaluation cannot be built from a run's own evidence."""


@dataclass(frozen=True, slots=True)
class ChunkSelection:
    """One chunk to judge, and optionally which of its paragraphs."""

    chunk_id: str
    paragraph_indexes: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class PlannedChunk:
    """One chunk resolved against the workspace and ready to be judged."""

    job: TranslationJob
    source_text: str
    translation_text: str
    source_file: Path
    source_sha256: str
    translation_file: Path
    translation_sha256: str
    terminology_file: Path | None
    terminology_sha256: str | None
    registry: TerminologyRegistry | None
    identity_mode: str
    paragraph_count: int
    paragraph_indexes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SemanticCoveragePlan:
    """Everything the evaluation will do, decided before a provider is contacted."""

    project_root: Path
    run_id: str
    snapshot_id: str
    source_language: str
    target_language: str
    evaluation_number: int
    destination: Path
    batch_size: int
    repetitions: int
    policy: SemanticPolicy
    chunks: tuple[PlannedChunk, ...]
    context_radius: int = CONTEXT_RADIUS
    skipped: tuple[str, ...] = ()
    identity_sha256: str = ""

    @property
    def paragraph_total(self) -> int:
        return sum(len(chunk.paragraph_indexes) for chunk in self.chunks) * self.repetitions

    @property
    def planned_calls(self) -> int:
        per_repetition = sum(
            -(-len(chunk.paragraph_indexes) // self.batch_size) for chunk in self.chunks
        )
        return per_repetition * self.repetitions


@dataclass(slots=True)
class SemanticCoverageResult:
    """What the evaluation actually did."""

    report: SemanticCoverageReport
    evidence: list[SemanticQAEvidence] = field(default_factory=list)
    written: Path | None = None


def _verified_text(path: Path, expected_sha256: str, label: str) -> str:
    content = path.read_bytes()
    if sha256_bytes(content) != expected_sha256:
        raise SemanticCoverageError(f"{label} hash mismatch: {path}")
    return content.decode("utf-8")


def _identity_mode(attempt_file: Path, project_root: Path, *, run_contract: str | None) -> str:
    """Report how the judged paragraph identities were established, without inferring one."""
    try:
        attempt = load_yaml(project_root / attempt_file, TranslationAttempt)
    except (ExecutorError, ValidationError, OSError):
        return "marked" if uses_paragraph_markers(run_contract) else "legacy_positional"
    return "marked" if uses_paragraph_markers(attempt.output_contract) else "legacy_positional"


def _frozen_terminology(
    manifest: TranslationManifest, project_root: Path
) -> tuple[Path | None, str | None, TerminologyRegistry | None]:
    if manifest.terminology is None:
        return None, None, None
    reference = manifest.terminology.frozen_file or manifest.terminology.file
    path = project_root / reference
    if sha256_bytes(path.read_bytes()) != manifest.terminology.sha256:
        raise SemanticCoverageError("frozen terminology resource hash mismatch")
    return reference, manifest.terminology.sha256, load_registry(path)


def _next_evaluation_number(destination_root: Path) -> int:
    if not destination_root.is_dir():
        return 1
    existing = [
        int(entry.name)
        for entry in destination_root.iterdir()
        if entry.is_dir() and entry.name.isdigit()
    ]
    return max(existing, default=0) + 1


def plan_semantic_coverage(
    run_id_or_path: str | Path,
    *,
    project_root: Path,
    selections: Sequence[ChunkSelection],
    batch_size: int = 4,
    repetitions: int = 1,
    context_radius: int = CONTEXT_RADIUS,
    policy: SemanticPolicy = EN_FA_JUDGE_V1,
    now: Callable[[], datetime] | None = None,
) -> SemanticCoveragePlan:
    """Resolve every selected chunk and decide the whole evaluation before contacting anything.

    Planning is complete on purpose: how many calls an evaluation will cost is knowable before the
    first one is spent, which is what makes a bounded sample a decision rather than a hope.
    """
    project_root = project_root.resolve()
    if batch_size < 1 or repetitions < 1:
        raise SemanticCoverageError("batch size and repetitions must be at least one")
    if not selections:
        raise SemanticCoverageError("no chunk was selected for semantic coverage")
    state = load_run_summary(run_id_or_path, project_root=project_root)
    manifest = load_yaml(
        project_root / state.run.snapshot_path / "manifest.yaml", TranslationManifest
    )
    terminology_file, terminology_sha256, registry = _frozen_terminology(manifest, project_root)
    by_chunk = {job.chunk_id: job for job in state.jobs}
    planned: list[PlannedChunk] = []
    skipped: list[str] = []
    for selection in selections:
        job = by_chunk.get(selection.chunk_id)
        if job is None:
            skipped.append(f"{selection.chunk_id}: not a job of this run")
            continue
        try:
            acceptance = load_effective_acceptance(job, project_root=project_root)
            source_text = _verified_text(
                project_root / acceptance.source_file, acceptance.source_sha256, "frozen source"
            )
            translation_text = _verified_text(
                project_root / acceptance.translation_file,
                acceptance.translation_sha256,
                "accepted translation",
            )
            tasks = build_tasks(
                source_text,
                translation_text,
                registry=registry,
                paragraph_indexes=selection.paragraph_indexes,
            )
        except (
            ReleaseAssemblyError,
            SemanticCoverageError,
            SemanticTaskError,
            ExecutorError,
            OSError,
        ) as exc:
            skipped.append(f"{selection.chunk_id}: {exc}")
            continue
        planned.append(
            PlannedChunk(
                job=job,
                source_text=source_text,
                translation_text=translation_text,
                source_file=acceptance.source_file,
                source_sha256=acceptance.source_sha256,
                translation_file=acceptance.translation_file,
                translation_sha256=acceptance.translation_sha256,
                terminology_file=terminology_file,
                terminology_sha256=terminology_sha256,
                registry=registry,
                identity_mode=_identity_mode(
                    acceptance.attempt_file,
                    project_root,
                    run_contract=state.run.output_contract_version,
                ),
                paragraph_count=len(build_tasks(source_text, translation_text)),
                paragraph_indexes=tuple(task.paragraph_index for task in tasks),
            )
        )
    destination_root = ProjectPaths(project_root).semantic_coverage_audit(state.run.run_id)
    evaluation_number = _next_evaluation_number(destination_root)
    identity = sha256_bytes(
        "\n".join(
            (
                SEMANTIC_JUDGE_ENGINE_VERSION,
                JUDGE_PROMPT_VERSION,
                JUDGE_SCHEMA_VERSION,
                policy.policy_version,
                state.run.run_id,
                str(batch_size),
                str(repetitions),
                str(context_radius),
                *(
                    f"{chunk.job.chunk_id}:{chunk.translation_sha256}:"
                    f"{','.join(str(index) for index in chunk.paragraph_indexes)}"
                    for chunk in planned
                ),
            )
        ).encode("utf-8")
    )
    return SemanticCoveragePlan(
        project_root=project_root,
        run_id=state.run.run_id,
        snapshot_id=state.run.snapshot_id,
        source_language=state.run.source_language,
        target_language=state.run.target_language,
        evaluation_number=evaluation_number,
        destination=destination_root / f"{evaluation_number:03d}",
        batch_size=batch_size,
        repetitions=repetitions,
        policy=policy,
        chunks=tuple(planned),
        skipped=tuple(skipped),
        identity_sha256=identity,
        context_radius=context_radius,
    )


def _outcomes(
    call: JudgeCall, chunk: PlannedChunk, policy: SemanticPolicy, indexes: dict[str, int]
) -> list[SemanticParagraphOutcome]:
    from book_translator.paragraph_markers import source_paragraphs

    source_blocks = source_paragraphs(chunk.source_text)
    target_blocks = source_paragraphs(chunk.translation_text)
    produced: list[SemanticParagraphOutcome] = []
    for verdict in call.response.verdicts:
        index = indexes[verdict.paragraph_id]
        outcome, material, advisory = apply_policy(verdict, policy=policy)
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


def execute_semantic_coverage(
    plan: SemanticCoveragePlan,
    judge: TransportSemanticJudge,
    *,
    now: Callable[[], datetime] | None = None,
    dry_run: bool = False,
) -> SemanticCoverageResult:
    """Run the planned evaluation and publish its immutable evidence.

    A failed call is recorded as a failed call and never as a verdict. The evaluation continues
    past it, because one provider failure in the middle of a sample must not discard the answers
    already paid for.
    """
    timestamp = (now or (lambda: datetime.now(UTC)))()
    evidence: list[SemanticQAEvidence] = []
    skipped = list(plan.skipped)
    calls = 0
    failed = 0
    models: set[str] = set()
    input_tokens = 0
    output_tokens = 0
    findings: dict[str, int] = {}
    severities: dict[str, int] = {}
    totals = {verdict: 0 for verdict in SemanticVerdict}
    if not dry_run:
        for chunk in plan.chunks:
            tasks = build_tasks(
                chunk.source_text,
                chunk.translation_text,
                registry=chunk.registry,
                paragraph_indexes=chunk.paragraph_indexes,
                context_radius=plan.context_radius,
            )
            indexes = {task.paragraph_id: task.paragraph_index for task in tasks}
            for repetition in range(1, plan.repetitions + 1):
                outcomes: list[SemanticParagraphOutcome] = []
                records = []
                for batch in build_batches(tasks, batch_size=plan.batch_size):
                    user_message = render_user_message(
                        batch,
                        chapter_id=chunk.job.chapter_id,
                        chunk_id=chunk.job.chunk_id,
                    )
                    calls += 1
                    try:
                        call = judge.judge(
                            run_id=plan.run_id,
                            job_id=chunk.job.job_id,
                            chunk_id=chunk.job.chunk_id,
                            user_message=user_message,
                            paragraph_ids=batch.paragraph_ids,
                            source_language=plan.source_language,
                            target_language=plan.target_language,
                            repetition=repetition,
                            batch_size=plan.batch_size,
                        )
                    except SemanticJudgeError as exc:
                        failed += 1
                        skipped.append(
                            f"{chunk.job.chunk_id} r{repetition}"
                            f" {','.join(batch.paragraph_ids)}: {exc.code}"
                        )
                        continue
                    records.append(call.record)
                    if call.record.model_returned:
                        models.add(call.record.model_returned)
                    input_tokens += call.record.input_tokens or 0
                    output_tokens += call.record.output_tokens or 0
                    outcomes.extend(_outcomes(call, chunk, plan.policy, indexes))
                if not outcomes:
                    continue
                for outcome in outcomes:
                    totals[outcome.policy_outcome] += 1
                    for finding in (*outcome.material_findings, *outcome.advisory_findings):
                        findings[finding.code.value] = findings.get(finding.code.value, 0) + 1
                        severities[finding.severity.value] = (
                            severities.get(finding.severity.value, 0) + 1
                        )
                counts = {verdict: 0 for verdict in SemanticVerdict}
                for outcome in outcomes:
                    counts[outcome.policy_outcome] += 1
                evidence.append(
                    SemanticQAEvidence(
                        schema_version=SCHEMA_VERSION,
                        judge_engine_version=SEMANTIC_JUDGE_ENGINE_VERSION,
                        judge_prompt_version=JUDGE_PROMPT_VERSION,
                        judge_schema_version=JUDGE_SCHEMA_VERSION,
                        policy_version=plan.policy.policy_version,
                        run_id=plan.run_id,
                        job_id=chunk.job.job_id,
                        chapter_id=chunk.job.chapter_id,
                        chunk_id=chunk.job.chunk_id,
                        evaluation_number=plan.evaluation_number,
                        repetition=repetition,
                        look="second" if plan.context_radius > CONTEXT_RADIUS else "first",
                        context_radius=plan.context_radius,
                        context_policy_version=(
                            SECOND_LOOK_CONTEXT_POLICY_VERSION
                            if plan.context_radius > CONTEXT_RADIUS
                            else None
                        ),
                        identity_mode=(
                            "marked" if chunk.identity_mode == "marked" else "legacy_positional"
                        ),
                        source_file=chunk.source_file,
                        source_sha256=chunk.source_sha256,
                        translation_file=chunk.translation_file,
                        translation_sha256=chunk.translation_sha256,
                        terminology_file=chunk.terminology_file,
                        terminology_sha256=chunk.terminology_sha256,
                        context_sha256=chunk.job.context_sha256,
                        paragraph_count=chunk.paragraph_count,
                        judged_paragraphs=len(outcomes),
                        pass_count=counts[SemanticVerdict.PASS],
                        revise_count=counts[SemanticVerdict.REVISE],
                        inconclusive_count=counts[SemanticVerdict.INCONCLUSIVE],
                        requests=records,
                        outcomes=sorted(outcomes, key=lambda item: item.paragraph_index),
                        created_at=timestamp,
                    )
                )
    report = SemanticCoverageReport(
        schema_version=SCHEMA_VERSION,
        judge_engine_version=SEMANTIC_JUDGE_ENGINE_VERSION,
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        judge_schema_version=JUDGE_SCHEMA_VERSION,
        policy_version=plan.policy.policy_version,
        run_id=plan.run_id,
        snapshot_id=plan.snapshot_id,
        evaluation_number=plan.evaluation_number,
        request_identity_sha256=plan.identity_sha256,
        provider=judge.provider_name,
        adapter_version=judge.adapter_version,
        model_requested=None,
        models_returned=sorted(models),
        batch_size=plan.batch_size,
        repetitions=plan.repetitions,
        chunks_selected=len(plan.chunks),
        chunks_judged=len({item.chunk_id for item in evidence}),
        chunks_skipped=len(plan.skipped),
        paragraphs_judged=sum(item.judged_paragraphs for item in evidence),
        judge_calls=calls,
        failed_calls=failed,
        pass_count=totals[SemanticVerdict.PASS],
        revise_count=totals[SemanticVerdict.REVISE],
        inconclusive_count=totals[SemanticVerdict.INCONCLUSIVE],
        input_tokens=input_tokens or None,
        output_tokens=output_tokens or None,
        finding_counts=dict(sorted(findings.items())),
        severity_counts=dict(sorted(severities.items())),
        skipped=skipped,
        created_at=timestamp,
    )
    if dry_run:
        return SemanticCoverageResult(report=report)
    destination = plan.destination
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    chunks_directory = destination / "chunks"
    chunks_directory.mkdir(exist_ok=True, mode=0o700)
    for item in evidence:
        name = f"{item.chunk_id}-r{item.repetition:02d}.yaml"
        private_write(chunks_directory / name, serialize_yaml(item).encode("utf-8"))
    private_write(destination / "evaluation.yaml", serialize_yaml(report).encode("utf-8"))
    return SemanticCoverageResult(report=report, evidence=evidence, written=destination)
