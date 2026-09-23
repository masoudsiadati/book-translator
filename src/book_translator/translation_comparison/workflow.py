"""Read-only comparison of two runs' effective translations of one chapter.

It reads both runs through the same canonical acceptance resolver release assembly uses, so what
it compares is exactly what each run would publish. It re-runs only the provider-free validators
in memory so both sides are measured by the same installed engines, reads semantic and revision
evidence where a run has any, and writes one counts-and-hashes report. It contacts no provider,
judges no prose, and records no preference.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from book_translator.atomic_write import atomic_write_text
from book_translator.models import (
    SCHEMA_VERSION,
    ReleaseManifest,
    SemanticQAEvidence,
    TerminologyRegistry,
    TranslationJob,
    TranslationManifest,
    TranslationSemanticRevisionEvidence,
)
from book_translator.models.translation_comparison import (
    ComparedChunk,
    ComparedTranslation,
    TranslationComparisonReport,
)
from book_translator.paragraph_alignment import (
    LEGACY_POSITIONAL,
    MARKED,
    assess_alignment,
    policy_for,
)
from book_translator.paragraph_markers import source_paragraphs
from book_translator.paths import ProjectPaths
from book_translator.prompt_packaging.identity import uses_paragraph_markers
from book_translator.provider_execution.artifacts import load_yaml, serialize_yaml, sha256_bytes
from book_translator.release_assembly import ReleaseAssemblyError
from book_translator.release_assembly.workflow import load_effective_acceptance
from book_translator.terminology import load_registry
from book_translator.terminology import validation as qa_validation
from book_translator.translation_queue import load_run_summary
from book_translator.translation_queue.workflow import QueueState

TRANSLATION_COMPARISON_VERSION = "1.0.0"

__all__ = [
    "TRANSLATION_COMPARISON_VERSION",
    "TranslationComparisonError",
    "TranslationComparisonPlan",
    "execute_translation_comparison",
    "plan_translation_comparison",
]


class TranslationComparisonError(ValueError):
    """Raised when two runs cannot be compared from their own evidence."""


@dataclass(frozen=True, slots=True)
class TranslationComparisonPlan:
    report: TranslationComparisonReport
    destination: Path


def _registry(state: QueueState, project_root: Path) -> TerminologyRegistry | None:
    manifest = load_yaml(
        project_root / state.run.snapshot_path / "manifest.yaml", TranslationManifest
    )
    if manifest.terminology is None:
        return None
    path = project_root / (manifest.terminology.frozen_file or manifest.terminology.file)
    if sha256_bytes(path.read_bytes()) != manifest.terminology.sha256:
        raise TranslationComparisonError("frozen terminology resource hash mismatch")
    return load_registry(path)


def _semantic(job: TranslationJob, state: QueueState) -> dict[str, object]:
    base = state.path / "jobs" / job.chunk_id
    judge_calls = second_looks = revise_findings = 0
    unresolved: set[str] = set()
    latest: dict[int, str] = {}
    for path in sorted((base / "semantic-qa").glob("*/evidence.yaml")):
        evidence = load_yaml(path, SemanticQAEvidence)
        judge_calls += len(evidence.requests)
        if evidence.look == "second":
            second_looks += len(evidence.requests)
        for outcome in evidence.outcomes:
            latest[outcome.paragraph_index] = outcome.policy_outcome.value
            revise_findings += len(outcome.material_findings)
    unresolved = {f"P{index:04d}" for index, value in latest.items() if value != "PASS"}
    attempted = adopted = reviser_calls = 0
    revised: set[str] = set()
    for path in sorted((base / "semantic-revision").glob("*/revision.yaml")):
        revision = load_yaml(path, TranslationSemanticRevisionEvidence)
        attempted += 1
        if revision.provider_contacted:
            reviser_calls += 1
        if revision.adopted:
            adopted += 1
            if revision.bounds is not None:
                revised.update(
                    f"P{index:04d}" for index in revision.bounds.changed_paragraph_indexes
                )
    return {
        "semantic_status": job.semantic_status.value if job.semantic_status else None,
        "semantic_evidence_count": job.semantic_evidence_count,
        "semantic_judge_calls": judge_calls,
        "semantic_second_looks": second_looks,
        "semantic_revise_findings": revise_findings,
        "semantic_revisions_attempted": attempted,
        "semantic_revisions_adopted": adopted,
        "semantic_reviser_calls": reviser_calls,
        "revised_paragraph_ids": sorted(revised),
        "unresolved_paragraph_ids": sorted(unresolved)
        if job.semantic_status == "unresolved"
        else [],
    }


def _side(
    state: QueueState,
    job: TranslationJob,
    source_text: str,
    registry: TerminologyRegistry | None,
    project_root: Path,
) -> tuple[ComparedTranslation, str]:
    acceptance = load_effective_acceptance(job, project_root=project_root)
    content = (project_root / acceptance.translation_file).read_bytes()
    if sha256_bytes(content) != acceptance.translation_sha256:
        raise TranslationComparisonError(f"effective translation hash mismatch for {job.chunk_id}")
    text = content.decode("utf-8")
    marked = uses_paragraph_markers(state.run.output_contract_version)
    structural = qa_validation.validate_translation_qa(
        source_text, text, target_language=job.target_language
    )
    terminology_codes: list[str] = []
    if registry is not None:
        terminology = qa_validation.validate_terminology(source_text, text, registry)
        terminology_codes = sorted({f.code for f in terminology.findings if f.severity == "error"})
    alignment = assess_alignment(
        source_text,
        text,
        policy=policy_for(job.target_language),
        contract_mode=MARKED if marked else LEGACY_POSITIONAL,
    )
    return (
        ComparedTranslation(
            run_id=job.run_id,
            job_id=job.job_id,
            output_contract_version=state.run.output_contract_version,
            acceptance_origin=acceptance.origin.value,
            translation_file=acceptance.translation_file,
            translation_sha256=acceptance.translation_sha256,
            paragraph_count=len(source_paragraphs(text)),
            provider_attempts=job.provider_attempt_count,
            structural_repairs=job.structural_repair_count,
            provider=job.provider,
            model=job.model,
            structural_blocking_codes=sorted(
                {f.code for f in structural.findings if f.severity == "error"}
            ),
            terminology_blocking_codes=terminology_codes,
            alignment_codes=sorted({f.code for f in alignment.findings}),
            alignment_blocking=sum(1 for f in alignment.findings if f.severity == "error"),
            **_semantic(job, state),  # type: ignore[arg-type]
        ),
        text,
    )


def _release_chapter(
    project_root: Path, release: Path | None, chapter_id: str
) -> tuple[Path | None, str | None]:
    if release is None:
        return None, None
    manifest = load_yaml(project_root / release / "manifest.yaml", ReleaseManifest)
    for chapter in manifest.chapters:
        if chapter.chapter_id == chapter_id:
            return chapter.file, chapter.sha256
    raise TranslationComparisonError(f"release does not contain chapter {chapter_id}")


def plan_translation_comparison(
    first_run: str | Path,
    second_run: str | Path,
    *,
    chapter_id: str,
    project_root: Path,
    first_release: str | Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> TranslationComparisonPlan:
    """Compare both runs' effective translations of one chapter. Reads only."""
    project_root = project_root.resolve()
    timestamp = (now or (lambda: datetime.now(UTC)))()
    first = load_run_summary(first_run, project_root=project_root)
    second = load_run_summary(second_run, project_root=project_root)
    first_jobs = {job.chunk_id: job for job in first.jobs if job.chapter_id == chapter_id}
    second_jobs = {job.chunk_id: job for job in second.jobs if job.chapter_id == chapter_id}
    if not first_jobs or set(first_jobs) != set(second_jobs):
        raise TranslationComparisonError("both runs must hold the same chunks for the chapter")
    first_registry = _registry(first, project_root)
    second_registry = _registry(second, project_root)
    chunks: list[ComparedChunk] = []
    first_texts: list[str] = []
    second_texts: list[str] = []
    for chunk_id in sorted(first_jobs):
        job_a, job_b = first_jobs[chunk_id], second_jobs[chunk_id]
        if job_a.chunk_source_sha256 != job_b.chunk_source_sha256:
            raise TranslationComparisonError(
                f"chunk {chunk_id} answers different sources in the two runs"
            )
        source_bytes = (project_root / job_a.chunk_source_file).read_bytes()
        if sha256_bytes(source_bytes) != job_a.chunk_source_sha256:
            raise TranslationComparisonError(f"frozen source hash mismatch for {chunk_id}")
        source_text = source_bytes.decode("utf-8")
        try:
            side_a, text_a = _side(first, job_a, source_text, first_registry, project_root)
            side_b, text_b = _side(second, job_b, source_text, second_registry, project_root)
        except ReleaseAssemblyError as exc:
            raise TranslationComparisonError(f"chunk {chunk_id}: {exc}") from exc
        blocks_a, blocks_b = source_paragraphs(text_a), source_paragraphs(text_b)
        aligned = len(blocks_a) == len(blocks_b) == len(source_paragraphs(source_text))
        differing = (
            [
                f"P{index:04d}"
                for index, (x, y) in enumerate(zip(blocks_a, blocks_b, strict=True), 1)
                if x != y
            ]
            if aligned
            else []
        )
        chunks.append(
            ComparedChunk(
                chunk_id=chunk_id,
                chunk_sequence=job_a.chunk_sequence,
                source_sha256=job_a.chunk_source_sha256,
                source_paragraph_count=len(source_paragraphs(source_text)),
                first=side_a,
                second=side_b,
                paragraph_identity_aligned=aligned,
                differing_paragraph_ids=differing,
            )
        )
        first_texts.append(text_a)
        second_texts.append(text_b)
    release_file, release_sha = _release_chapter(
        project_root, Path(first_release) if first_release is not None else None, chapter_id
    )
    report = TranslationComparisonReport(
        schema_version=SCHEMA_VERSION,
        comparison_engine_version=TRANSLATION_COMPARISON_VERSION,
        chapter_id=chapter_id,
        first_run_id=first.run.run_id,
        second_run_id=second.run.run_id,
        snapshot_id=second.run.snapshot_id,
        same_snapshot=first.run.snapshot_id == second.run.snapshot_id,
        first_chapter_sha256=hashlib.sha256("".join(first_texts).encode("utf-8")).hexdigest(),
        second_chapter_sha256=hashlib.sha256("".join(second_texts).encode("utf-8")).hexdigest(),
        first_release_chapter_file=release_file,
        first_release_chapter_sha256=release_sha,
        chunks=chunks,
        created_at=timestamp,
    )
    destination = (
        ProjectPaths(project_root).workspace
        / "audits"
        / "translation-comparison"
        / second.run.run_id
        / f"{chapter_id}.yaml"
    )
    return TranslationComparisonPlan(report, destination)


def execute_translation_comparison(plan: TranslationComparisonPlan) -> Path:
    plan.destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(plan.destination, serialize_yaml(plan.report), overwrite=True)
    return plan.destination
