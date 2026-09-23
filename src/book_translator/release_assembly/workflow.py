"""The only release assembly layer that touches the filesystem.

``plan_release_assembly`` reads one completed translation run and renders the entire release in
memory without writing a byte; ``execute_release_assembly`` publishes it. The split is the
ordinary stage convention, and here the read half is genuinely the useful one: ``--dry-run``
answers "would this run export, and what exactly would come out" without creating anything.

Three properties bound what this stage may do.

**It consumes, and never produces, translation state.** The run, its jobs, its attempts, the
frozen snapshot, the frozen resources, and the terminology registry are opened read-only and are
never rewritten, re-hashed, re-judged, or repaired. A release is a *view* of accepted evidence.

**It contacts nothing.** There is no provider import in this module and no network path through
it. A release is assembled from bytes that already exist or it is not assembled at all.

**It fails closed.** Every precondition — the run is completed, every job is accepted, none is
QA-rejected, the snapshot still validates against the workspace, the chunk coverage is exactly
one accepted translation per frozen chunk, and every translation file still hashes to what its
acceptance evidence recorded — is checked before anything is rendered. A release that cannot be
proven is refused, never approximated.
"""

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.chapter_titles.artifact import (
    ChapterTitleArtifact,
    discover_chapter_title_artifact,
    load_chapter_title_artifact,
)
from book_translator.chapter_titles.engine import ChapterTitleError
from book_translator.chapter_titles.qa import validate_chapter_titles
from book_translator.models import (
    SCHEMA_VERSION,
    ReleaseSemanticWaiverReference,
    SemanticGateStatus,
    TerminologyRegistry,
    TranslationAcceptanceEvidence,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationAttempt,
    TranslationHistoricalReevaluationEvidence,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
    TranslationQAReevaluationEvidence,
    TranslationReuseEvidence,
    TranslationRun,
    TranslationRunStatus,
    TranslationSemanticRevisionEvidence,
    TranslationStructuralRepairEvidence,
)
from book_translator.models.release import (
    ReleaseArtifactRole,
    ReleaseChapterRecord,
    ReleaseChunkLineage,
    ReleaseManifest,
    ReleaseProviderResolution,
)
from book_translator.models.translation_snapshot import (
    SnapshotChapterReference,
    SnapshotChunkReference,
)
from book_translator.paths import ProjectPaths
from book_translator.release_assembly.engine import (
    ReleaseAssemblyError,
    assemble_book,
    assemble_chapter,
    chapter_filename,
    chapter_heading,
    validate_chunk_coverage,
    validate_release_id,
    word_count,
)
from book_translator.release_assembly.models import (
    ReleaseAssemblyPlan,
    ReleaseAssemblyResult,
    ReleaseFile,
)
from book_translator.release_assembly.reporting import (
    BOOK_FILENAME,
    CHAPTERS_DIRECTORY,
    LINEAGE_FILENAME,
    MANIFEST_FILENAME,
    METADATA_FILENAME,
    TERMINOLOGY_FILENAME,
    TRANSLATION_REPORT_FILENAME,
    build_lineage_document,
    build_manifest,
    build_translation_report,
    release_file,
    serialize_release_json,
    serialize_release_yaml,
    sha256_bytes,
)
from book_translator.release_assembly.waiver import semantic_coverage, unjudged_jobs
from book_translator.release_assembly.waiver_workflow import verified_waiver
from book_translator.terminology import load_registry
from book_translator.translation_queue.workflow import QueueError, QueueState, _load_queue
from book_translator.translation_queue.workflow import (
    _resolve_run_path as _resolve_translation_run_path,
)
from book_translator.translation_snapshot import validate_snapshot

#: Workspace roots a release directory may never be written into. A release is derived output;
#: pointing it at canonical or frozen state would make an assembly capable of destroying its own
#: inputs, which no amount of ``--force`` should ever be able to authorize.
PROTECTED_WORKSPACE_ROOTS = (
    "chapters",
    "translation-runs",
    "snapshots",
    "resources",
    "audits",
    "migrations",
    "terminology-runs",
    "terminology-corrections",
    "terminology-remediation",
)


def _load_contract[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ReleaseAssemblyError(f"unreadable translation artifact {path}: {exc}") from exc


def _verified_bytes(path: Path, expected_sha256: str, label: str) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ReleaseAssemblyError(f"{label} is missing: {path}") from exc
    if sha256_bytes(content) != expected_sha256:
        raise ReleaseAssemblyError(f"{label} hash mismatch: {path}")
    return content


def _project_relative(path: Path, project_root: Path) -> Path:
    try:
        return Path(path.resolve().relative_to(project_root.resolve()).as_posix())
    except ValueError as exc:
        raise ReleaseAssemblyError(f"release output must live inside the project: {path}") from exc


@dataclass(frozen=True, slots=True)
class EffectiveAcceptance:
    """The hash-verified acceptance a release publishes, without any translated content.

    Effective acceptance is not always the original sidecar. A provider-free re-evaluation or a
    provider-free structural repair republishes the verdict under its own evidence and names the
    record it supersedes, so resolving the chain — and proving each link — is what makes "this is
    the accepted text" checkable rather than assumed.
    """

    origin: TranslationAcceptanceOrigin
    evidence_file: Path
    evidence_sha256: str
    attempt_id: str
    attempt_number: int
    attempt_file: Path
    attempt_sha256: str
    source_file: Path
    source_sha256: str
    translation_file: Path
    translation_sha256: str
    terminology_qa_version: str | None
    structural_qa_version: str | None


def load_effective_acceptance(job: TranslationJob, *, project_root: Path) -> EffectiveAcceptance:
    """Resolve and hash-verify the effective mechanical acceptance of one job."""
    if (
        job.status is not TranslationJobStatus.SUCCEEDED
        or job.acceptance_status is not TranslationAcceptanceStatus.ACCEPTED
        or job.accepted_attempt is None
        or job.acceptance_file is None
        or job.acceptance_sha256 is None
        or job.acceptance_origin is None
    ):
        raise ReleaseAssemblyError(
            f"job {job.job_id} is not a succeeded, accepted translation and cannot be released"
        )
    evidence_file = job.acceptance_file
    evidence_sha256 = job.acceptance_sha256
    acceptance_path = project_root / evidence_file
    _verified_bytes(acceptance_path, evidence_sha256, "acceptance evidence")
    base = _load_contract(acceptance_path, TranslationAcceptanceEvidence)
    evidence: (
        TranslationAcceptanceEvidence
        | TranslationQAReevaluationEvidence
        | TranslationStructuralRepairEvidence
    ) = base
    status = base.status
    if job.latest_reevaluation_file is not None:
        if job.latest_reevaluation_sha256 is None:
            raise ReleaseAssemblyError("re-evaluation evidence hash is missing")
        path = project_root / job.latest_reevaluation_file
        _verified_bytes(path, job.latest_reevaluation_sha256, "re-evaluation evidence")
        reevaluation = _load_contract(path, TranslationQAReevaluationEvidence)
        if (
            reevaluation.reevaluated_from_file != evidence_file
            or reevaluation.reevaluated_from_sha256 != evidence_sha256
        ):
            raise ReleaseAssemblyError("re-evaluation evidence lineage mismatch")
        evidence = reevaluation
        evidence_file = job.latest_reevaluation_file
        evidence_sha256 = job.latest_reevaluation_sha256
        status = reevaluation.resulting_status
    if job.latest_structural_repair_file is not None:
        if job.latest_structural_repair_sha256 is None:
            raise ReleaseAssemblyError("structural repair evidence hash is missing")
        path = project_root / job.latest_structural_repair_file
        _verified_bytes(path, job.latest_structural_repair_sha256, "structural repair evidence")
        repair = _load_contract(path, TranslationStructuralRepairEvidence)
        if (
            repair.superseded_evidence_file != evidence_file
            or repair.superseded_evidence_sha256 != evidence_sha256
        ):
            raise ReleaseAssemblyError("structural repair evidence lineage mismatch")
        evidence = repair
        evidence_file = job.latest_structural_repair_file
        evidence_sha256 = job.latest_structural_repair_sha256
        status = repair.resulting_status
    if status is not TranslationAcceptanceStatus.ACCEPTED:
        raise ReleaseAssemblyError(
            f"effective acceptance for job {job.job_id} is {status.value}, not accepted"
        )
    if evidence.attempt_number != job.accepted_attempt:
        raise ReleaseAssemblyError(
            f"acceptance evidence for job {job.job_id} names a different attempt"
        )
    _verified_bytes(
        project_root / evidence.attempt_file, evidence.attempt_sha256, "accepted attempt record"
    )
    translation_file = evidence.translation_file
    translation_sha256 = evidence.translation_sha256
    if job.latest_semantic_revision_file is not None:
        # An adopted semantic revision supersedes the translated bytes the mechanical verdict
        # judged, the way a structural repair does, and it names what it superseded so the
        # supersession is checkable here rather than trusted. Only the bytes move: the mechanical
        # acceptance, its origin, and its attempt stay exactly what they were.
        if job.latest_semantic_revision_sha256 is None:
            raise ReleaseAssemblyError("semantic revision evidence hash is missing")
        path = project_root / job.latest_semantic_revision_file
        _verified_bytes(path, job.latest_semantic_revision_sha256, "semantic revision evidence")
        revision = _load_contract(path, TranslationSemanticRevisionEvidence)
        if not revision.adopted or revision.translation_file is None:
            raise ReleaseAssemblyError("latest semantic revision was not adopted")
        if revision.translation_sha256 is None:
            raise ReleaseAssemblyError("adopted semantic revision lacks a translation hash")
        translation_file = revision.translation_file
        translation_sha256 = revision.translation_sha256
    return EffectiveAcceptance(
        origin=job.acceptance_origin,
        evidence_file=evidence_file,
        evidence_sha256=evidence_sha256,
        attempt_id=evidence.attempt_id,
        attempt_number=evidence.attempt_number,
        attempt_file=evidence.attempt_file,
        attempt_sha256=evidence.attempt_sha256,
        source_file=evidence.source_file,
        source_sha256=evidence.source_sha256,
        translation_file=translation_file,
        translation_sha256=translation_sha256,
        terminology_qa_version=evidence.terminology_qa_version,
        structural_qa_version=evidence.structural_qa_version,
    )


def _provider_lineage(
    job: TranslationJob, acceptance: EffectiveAcceptance, *, project_root: Path
) -> tuple[str | None, str | None, ReleaseProviderResolution, str | None, str | None]:
    """Resolve who produced one released translation, or record that it is unknowable.

    A translation carried forward or reused from an earlier run was produced by that run's
    provider, not by this one — the job's own ``provider`` is null precisely because this run
    never called anybody. The lineage record follows the evidence to the originating attempt and
    says which of the three routes it took, so an unrecorded provider is visibly unrecorded
    rather than silently blank.
    """
    if job.latest_carry_forward_file is not None and job.latest_carry_forward_sha256 is not None:
        path = project_root / job.latest_carry_forward_file
        _verified_bytes(path, job.latest_carry_forward_sha256, "carry-forward evidence")
        carried = _load_contract(path, TranslationHistoricalReevaluationEvidence)
        return (
            carried.source_provider,
            carried.source_model_returned,
            ReleaseProviderResolution.CARRY_FORWARD_SOURCE,
            carried.source_run_id,
            carried.source_job_id,
        )
    if job.latest_reuse_file is not None and job.latest_reuse_sha256 is not None:
        path = project_root / job.latest_reuse_file
        _verified_bytes(path, job.latest_reuse_sha256, "reuse evidence")
        reused = _load_contract(path, TranslationReuseEvidence)
        source_attempt = project_root / reused.source_attempt_file
        if (
            source_attempt.is_file()
            and sha256_bytes(source_attempt.read_bytes()) == reused.source_attempt_sha256
        ):
            attempt = _load_contract(source_attempt, TranslationAttempt)
            return (
                attempt.provider.value,
                attempt.model_returned or attempt.model_requested,
                ReleaseProviderResolution.REUSE_SOURCE,
                reused.source_run_id,
                reused.source_job_id,
            )
        return (
            None,
            None,
            ReleaseProviderResolution.UNRESOLVED,
            reused.source_run_id,
            reused.source_job_id,
        )
    attempt_path = project_root / acceptance.attempt_file
    if attempt_path.name != "attempt.yaml":
        return (job.provider, job.model, ReleaseProviderResolution.UNRESOLVED, None, None)
    attempt = _load_contract(attempt_path, TranslationAttempt)
    return (
        attempt.provider.value,
        attempt.model_returned or attempt.model_requested,
        ReleaseProviderResolution.ATTEMPT,
        None,
        None,
    )


def _require_releasable_run(
    state: QueueState, *, waived_jobs: frozenset[str] = frozenset()
) -> TranslationRun:
    """Refuse anything but a completed run whose every job is an accepted translation.

    ``waived_jobs`` are the job IDs an explicit, verified semantic-gate waiver authorized to be
    released *unjudged*. Nothing else about the run is relaxed by it, and a job that was judged
    and did not pass is never in that set.
    """
    run = state.run
    if run.status is not TranslationRunStatus.COMPLETED:
        raise ReleaseAssemblyError(
            f"only a completed translation run may be released; {run.run_id} is {run.status.value}"
        )
    if not state.validation.valid:
        raise ReleaseAssemblyError(
            "translation run counters or status do not match its jobs; recover the run first"
        )
    if not state.snapshot_valid:
        raise ReleaseAssemblyError("the run's frozen translation snapshot no longer validates")
    if run.qa_rejected_count:
        raise ReleaseAssemblyError(
            f"{run.qa_rejected_count} job(s) are still QA-rejected and cannot be released"
        )
    if run.accepted_count != run.job_count or run.succeeded_count != run.job_count:
        raise ReleaseAssemblyError(
            f"only {run.accepted_count} of {run.job_count} jobs are accepted"
        )
    outstanding = (
        run.pending_count
        + run.claimed_count
        + run.running_count
        + run.failed_count
        + run.retry_pending_count
        + run.cancelled_count
        + run.blocked_count
        + run.provider_succeeded_unassessed_count
    )
    if outstanding:
        raise ReleaseAssemblyError(f"{outstanding} job(s) are not finished and cannot be released")
    for job in state.jobs:
        if (
            job.status is not TranslationJobStatus.SUCCEEDED
            or job.acceptance_status is not TranslationAcceptanceStatus.ACCEPTED
        ):
            raise ReleaseAssemblyError(
                f"job {job.job_id} is not an accepted translation and cannot be released"
            )
        # A run created under a semantic gate policy is releasable only when every job has
        # semantic evidence and that evidence says PASS. Absent evidence is "not judged", which
        # is not a pass, and a run created before the gate existed records no policy and is held
        # to exactly the requirement it always was.
        if run.semantic_gate_policy_version is not None and (
            job.semantic_status is not SemanticGateStatus.PASS
        ):
            state_name = job.semantic_status.value if job.semantic_status else "not judged"
            if job.semantic_status is None and job.job_id in waived_jobs:
                # Released under an explicit waiver, which authorizes publishing work the judge
                # never saw. It stays "not judged": nothing here turns it into a pass.
                continue
            raise ReleaseAssemblyError(
                f"job {job.job_id} has no semantic acceptance ({state_name}) and cannot be"
                " released under the run's semantic gate policy"
            )
    return run


def _resolve_output(output: str | Path, project_root: Path) -> tuple[Path, str]:
    candidate = Path(output)
    resolved = (candidate if candidate.is_absolute() else project_root / candidate).resolve()
    relative = _project_relative(resolved, project_root)
    release_id = validate_release_id(resolved.name)
    parts = relative.parts
    if len(parts) < 2:
        raise ReleaseAssemblyError("a release directory must not be the project root")
    if parts[0] == "workspace" and (len(parts) < 2 or parts[1] in PROTECTED_WORKSPACE_ROOTS):
        raise ReleaseAssemblyError(
            f"a release may not be written into canonical workspace state: {relative.as_posix()}"
        )
    return resolved, release_id


def _count_records(root: Path) -> int:
    return len([path for path in root.iterdir() if path.is_dir()]) if root.is_dir() else 0


def _governed_action_counts(state: QueueState, project_root: Path) -> dict[str, int]:
    """Count the governed remedies the run spent, from their own durable records."""
    paths = ProjectPaths(project_root)
    counts = dict.fromkeys(("provider_repairs", "provider_recoveries", "repair_escalations"), 0)
    for job in state.jobs:
        counts["provider_repairs"] += _count_records(
            paths.translation_auto_repair_decision(job.run_id, job.chunk_id, 2).parent
        )
        counts["provider_recoveries"] += _count_records(
            paths.translation_provider_recovery_decision(job.run_id, job.chunk_id, 2).parent
        )
        counts["repair_escalations"] += _count_records(
            paths.translation_repair_escalation(job.run_id, job.chunk_id, 1).parent
        )
    return counts


def _frozen_resource_bytes(
    file: Path | None,
    frozen_file: Path | None,
    sha256: str | None,
    *,
    project_root: Path,
    label: str,
) -> bytes | None:
    """Copy one frozen resource verbatim, after proving it is the resource the snapshot froze."""
    if sha256 is None:
        return None
    relative = frozen_file or file
    if relative is None:
        raise ReleaseAssemblyError(f"the snapshot records a {label} hash without a file")
    return _verified_bytes(project_root / relative, sha256, f"frozen {label}")


def _chapter_plan(
    chapter: SnapshotChapterReference,
    chapter_sequence: int,
    *,
    jobs_by_chunk: dict[str, TranslationJob],
    project_root: Path,
    title_overlay: dict[str, str],
) -> tuple[ReleaseChapterRecord, str, tuple[ReleaseChunkLineage, ...]]:
    """Assemble one chapter and record where every chunk in it came from."""
    heading, heading_source = chapter_heading(
        title_target=chapter.title_fa,
        title_source=chapter.title_en,
        title_artifact=title_overlay.get(chapter.chapter_id),
    )
    resolved: list[tuple[SnapshotChunkReference, TranslationJob, EffectiveAcceptance, bytes]] = []
    for chunk in chapter.chunks:
        job = jobs_by_chunk.get(chunk.chunk_id)
        if job is None:
            raise ReleaseAssemblyError(f"no translation job exists for chunk {chunk.chunk_id}")
        acceptance = load_effective_acceptance(job, project_root=project_root)
        if acceptance.source_sha256 != chunk.source_sha256:
            raise ReleaseAssemblyError(
                f"accepted translation for chunk {chunk.chunk_id} answers a different source"
            )
        content = _verified_bytes(
            project_root / acceptance.translation_file,
            acceptance.translation_sha256,
            f"accepted translation for chunk {chunk.chunk_id}",
        )
        resolved.append((chunk, job, acceptance, content))

    validate_chunk_coverage(
        tuple(chunk.chunk_id for chunk in chapter.chunks),
        tuple(chunk.chunk_id for chunk, _, _, _ in resolved),
    )
    try:
        texts = tuple(
            (chunk.chunk_id, content.decode("utf-8")) for chunk, _, _, content in resolved
        )
    except UnicodeDecodeError as exc:
        raise ReleaseAssemblyError("an accepted translation is not valid UTF-8") from exc
    markdown, contributions = assemble_chapter(
        chapter_id=chapter.chapter_id, heading=heading, chunk_texts=texts
    )

    lineage: list[ReleaseChunkLineage] = []
    offset = 2  # paragraph 1 of every chapter file is its heading.
    for (chunk, job, acceptance, _), (_, paragraphs) in zip(resolved, contributions, strict=True):
        provider, model, resolution, source_run, source_job = _provider_lineage(
            job, acceptance, project_root=project_root
        )
        lineage.append(
            ReleaseChunkLineage(
                chapter_id=chapter.chapter_id,
                chapter_sequence=chapter_sequence,
                chunk_id=chunk.chunk_id,
                chunk_sequence=chunk.sequence,
                paragraph_offset=offset,
                job_id=job.job_id,
                source_file=chunk.source_file,
                source_sha256=chunk.source_sha256,
                source_paragraph_count=chunk.source_paragraph_count,
                context_sha256=chunk.context_sha256,
                translation_file=acceptance.translation_file,
                translation_sha256=acceptance.translation_sha256,
                translation_paragraph_count=len(paragraphs),
                acceptance_status=TranslationAcceptanceStatus.ACCEPTED,
                acceptance_origin=acceptance.origin,
                acceptance_evidence_file=acceptance.evidence_file,
                acceptance_evidence_sha256=acceptance.evidence_sha256,
                attempt_id=acceptance.attempt_id,
                attempt_number=acceptance.attempt_number,
                attempt_file=acceptance.attempt_file,
                attempt_sha256=acceptance.attempt_sha256,
                provider=provider,
                model=model,
                provider_resolution=resolution,
                provider_attempts=job.provider_attempt_count,
                terminology_qa_version=acceptance.terminology_qa_version,
                structural_qa_version=acceptance.structural_qa_version,
                lineage_source_run_id=source_run,
                lineage_source_job_id=source_job,
            )
        )
        offset += len(paragraphs)

    record = ReleaseChapterRecord(
        chapter_id=chapter.chapter_id,
        chapter_sequence=chapter_sequence,
        kind=chapter.kind,
        number=chapter.number,
        title_en=chapter.title_en,
        title_fa=chapter.title_fa,
        heading=heading,
        heading_source=heading_source,
        file=Path(CHAPTERS_DIRECTORY)
        / chapter_filename(kind=chapter.kind, number=chapter.number, chapter_id=chapter.chapter_id),
        sha256=sha256_bytes(markdown.encode("utf-8")),
        chunk_count=len(chapter.chunks),
        paragraph_count=offset - 1,
        word_count=word_count(markdown),
        source_cleaned_file=chapter.cleaned_file,
        source_cleaned_sha256=chapter.cleaned_sha256,
    )
    return record, markdown, tuple(lineage)


def _resolve_chapter_titles(
    manifest: TranslationManifest,
    run: TranslationRun,
    *,
    project_root: Path,
    chapter_titles: str | Path | None,
    use_chapter_titles: bool,
) -> ChapterTitleArtifact | None:
    """Find the governed chapter-title artifact for this snapshot and prove it still holds.

    Discovery is by snapshot, so a later terminology generation cannot silently make an existing
    artifact invisible and drop a release back to English headings. What keeps that safe is this:
    the artifact is re-judged here by the installed title QA against the terminology *this*
    release froze, and a set that no longer holds is refused rather than quietly ignored.
    """
    if not use_chapter_titles:
        return None
    if chapter_titles is not None:
        candidate = Path(chapter_titles)
        path = (
            candidate
            if candidate.is_absolute() or len(candidate.parts) > 1
            else ProjectPaths(project_root).chapter_title_artifact(str(chapter_titles))
        )
        named: ChapterTitleArtifact | None = load_chapter_title_artifact(path.resolve())
        artifact = named
        if artifact is None or (
            artifact.manifest.snapshot_id != run.snapshot_id
            or artifact.manifest.snapshot_manifest_sha256 != run.snapshot_manifest_sha256
        ):
            raise ReleaseAssemblyError(
                "the named chapter-title artifact was produced for a different snapshot"
            )
    else:
        artifact = discover_chapter_title_artifact(
            project_root=project_root,
            snapshot_id=run.snapshot_id,
            snapshot_manifest_sha256=run.snapshot_manifest_sha256,
        )
        if artifact is None:
            return None
    registry, _, _, _ = _release_terminology(manifest, project_root)
    report = validate_chapter_titles(
        expected=tuple((chapter.chapter_id, chapter.title_en) for chapter in manifest.chapters),
        records=tuple(artifact.document.chapters),
        registry=registry,
    )
    if not report.valid:
        codes = ", ".join(
            sorted({item.code for item in report.findings if item.severity == "error"})
        )
        raise ReleaseAssemblyError(
            f"chapter-title artifact {artifact.artifact_id} no longer passes title QA: {codes}"
        )
    return artifact


def _release_terminology(
    manifest: TranslationManifest, project_root: Path
) -> tuple[TerminologyRegistry, Path | None, str | None, str | None]:
    """Load the terminology the snapshot froze, proving the bytes are the ones it named."""
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
            None,
        )
    relative = manifest.terminology.frozen_file or manifest.terminology.file
    _verified_bytes(
        project_root / relative, manifest.terminology.sha256, "frozen terminology registry"
    )
    return (
        load_registry(project_root / relative),
        relative,
        manifest.terminology.sha256,
        manifest.terminology.version,
    )


def plan_release_assembly(
    run_id_or_path: str | Path,
    *,
    output: str | Path,
    project_root: Path,
    force: bool = False,
    chapter_titles: str | Path | None = None,
    use_chapter_titles: bool = True,
    semantic_waiver: str | Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> ReleaseAssemblyPlan:
    """Render one complete release from a completed run. Reads only; writes nothing.

    ``semantic_waiver`` names an operator-authorized waiver. Without one, a run under a semantic
    gate policy whose jobs are unjudged is refused exactly as it always was.
    """
    project_root = project_root.resolve()
    clock = now or (lambda: datetime.now(UTC))
    output_path, release_id = _resolve_output(output, project_root)

    run_path = _resolve_translation_run_path(run_id_or_path, project_root)
    try:
        state = _load_queue(run_path, project_root)
    except QueueError as exc:
        raise ReleaseAssemblyError(f"translation run cannot be read for release: {exc}") from exc
    waiver_reference = None
    waived: frozenset[str] = frozenset()
    if semantic_waiver is not None:
        path, waiver, file_sha256 = verified_waiver(
            project_root, semantic_waiver, run=state.run, jobs=tuple(state.jobs)
        )
        waived = frozenset(unjudged_jobs(state.jobs))
        waiver_reference = ReleaseSemanticWaiverReference(
            waiver_id=waiver.waiver_id,
            waiver_file=Path(path.relative_to(project_root).as_posix()),
            waiver_sha256=file_sha256,
            semantic_gate_policy_version=waiver.semantic_gate_policy_version,
            judged_pass=waiver.judged_pass,
            not_judged=waiver.not_judged,
            reason=waiver.reason,
        )
    run = _require_releasable_run(state, waived_jobs=waived)
    coverage = semantic_coverage(tuple(state.jobs))

    # The snapshot is validated in full before a single byte is read out of it: a release is only
    # as trustworthy as the frozen inputs whose acceptance it publishes.
    validation = validate_snapshot(project_root / run.snapshot_path, project_root=project_root)
    if (
        not validation.report.valid
        or not validation.lineage_current
        or validation.snapshot is None
        or validation.manifest is None
        or validation.snapshot.snapshot_id != run.snapshot_id
        or validation.snapshot.manifest_sha256 != run.snapshot_manifest_sha256
    ):
        raise ReleaseAssemblyError("the run's frozen translation snapshot failed validation")
    manifest: TranslationManifest = validation.manifest

    jobs_by_chunk = {job.chunk_id: job for job in state.jobs}
    if len(jobs_by_chunk) != len(state.jobs):
        raise ReleaseAssemblyError("the translation run contains duplicate chunk jobs")
    frozen_chunks = tuple(
        chunk.chunk_id for chapter in manifest.chapters for chunk in chapter.chunks
    )
    validate_chunk_coverage(frozen_chunks, tuple(job.chunk_id for job in state.jobs))

    try:
        title_artifact = _resolve_chapter_titles(
            manifest,
            run,
            project_root=project_root,
            chapter_titles=chapter_titles,
            use_chapter_titles=use_chapter_titles,
        )
    except ChapterTitleError as exc:
        raise ReleaseAssemblyError(f"chapter-title artifact is unusable: {exc}") from exc
    title_overlay = title_artifact.overlay() if title_artifact is not None else {}

    chapters: list[ReleaseChapterRecord] = []
    chapter_markdown: list[str] = []
    lineage_chunks: list[ReleaseChunkLineage] = []
    for sequence, chapter in enumerate(manifest.chapters, start=1):
        record, markdown, lineage = _chapter_plan(
            chapter,
            sequence,
            jobs_by_chunk=jobs_by_chunk,
            project_root=project_root,
            title_overlay=title_overlay,
        )
        chapters.append(record)
        chapter_markdown.append(markdown)
        lineage_chunks.extend(lineage)
    filenames = [record.file.as_posix() for record in chapters]
    if len(set(filenames)) != len(filenames):
        raise ReleaseAssemblyError("two chapters would be released under the same filename")

    book = assemble_book(tuple(chapter_markdown))
    metadata_bytes = _frozen_resource_bytes(
        manifest.book.metadata_file,
        manifest.book.frozen_metadata_file,
        manifest.book.metadata_sha256,
        project_root=project_root,
        label="book metadata",
    )
    terminology = manifest.terminology
    terminology_bytes = _frozen_resource_bytes(
        terminology.file if terminology else None,
        terminology.frozen_file if terminology else None,
        terminology.sha256 if terminology else None,
        project_root=project_root,
        label="terminology registry",
    )

    lineage_document = build_lineage_document(
        release_id=release_id,
        run=run,
        manifest=manifest,
        chunks=tuple(lineage_chunks),
    )
    actions = _governed_action_counts(state, project_root)
    report = build_translation_report(
        release_id=release_id,
        run=run,
        snapshot=validation.snapshot,
        manifest=manifest,
        chunks=tuple(lineage_chunks),
        provider_attempts=sum(job.provider_attempt_count for job in state.jobs),
        abandoned_attempts=sum(job.abandoned_attempt_count for job in state.jobs),
        provider_repairs=actions["provider_repairs"],
        provider_recoveries=actions["provider_recoveries"],
        repair_escalations=actions["repair_escalations"],
        structural_repairs=sum(job.structural_repair_count for job in state.jobs),
        qa_reevaluations=sum(job.reevaluation_count for job in state.jobs),
        cross_run_reuses=sum(job.reuse_count for job in state.jobs),
        historical_carry_forwards=sum(job.carry_forward_count for job in state.jobs),
        semantic_judged_pass=coverage.judged_pass,
        semantic_not_judged=coverage.not_judged,
        semantic_waiver=waiver_reference,
    )

    files: list[ReleaseFile] = [
        release_file(BOOK_FILENAME, ReleaseArtifactRole.BOOK, book.encode("utf-8"))
    ]
    files.extend(
        release_file(record.file.as_posix(), ReleaseArtifactRole.CHAPTER, markdown.encode("utf-8"))
        for record, markdown in zip(chapters, chapter_markdown, strict=True)
    )
    if metadata_bytes is not None:
        files.append(release_file(METADATA_FILENAME, ReleaseArtifactRole.METADATA, metadata_bytes))
    if terminology_bytes is not None:
        files.append(
            release_file(TERMINOLOGY_FILENAME, ReleaseArtifactRole.TERMINOLOGY, terminology_bytes)
        )
    files.append(
        release_file(
            TRANSLATION_REPORT_FILENAME,
            ReleaseArtifactRole.TRANSLATION_REPORT,
            serialize_release_yaml(report).encode("utf-8"),
        )
    )
    files.append(
        release_file(
            LINEAGE_FILENAME,
            ReleaseArtifactRole.LINEAGE,
            serialize_release_json(lineage_document).encode("utf-8"),
        )
    )

    release_manifest = build_manifest(
        release_id=release_id,
        run=run,
        manifest=manifest,
        chapters=tuple(chapters),
        files=tuple(files),
        book_metadata_sha256=manifest.book.metadata_sha256 if metadata_bytes else None,
        terminology_sha256=terminology.sha256 if terminology_bytes and terminology else None,
        terminology_version=terminology.version if terminology_bytes and terminology else None,
        chapter_title_artifact_id=(
            title_artifact.artifact_id if title_artifact is not None else None
        ),
        chapter_title_artifact_sha256=(
            title_artifact.content_sha256 if title_artifact is not None else None
        ),
        chunk_count=len(lineage_chunks),
        paragraph_count=sum(record.paragraph_count for record in chapters),
        word_count=word_count(book),
        created_at=clock(),
        semantic_waiver=waiver_reference,
    )
    return ReleaseAssemblyPlan(
        release_id=release_id,
        output_path=output_path,
        run_id=run.run_id,
        snapshot_id=run.snapshot_id,
        manifest=release_manifest,
        report=report,
        lineage=lineage_document,
        files=tuple(files),
        destination_exists=output_path.exists(),
        overwrite=force,
    )


def _publish(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        atomic_write_text(path, content.decode("utf-8"), overwrite=True)
        os.chmod(path, 0o600)
    except (OSError, UnicodeError) as exc:
        raise ReleaseAssemblyError(f"could not publish release artifact {path.name}") from exc


def _assert_replaceable(destination: Path, release_id: str) -> None:
    """Refuse to replace anything that is not already this release.

    ``--force`` re-exports a release; it is not a licence to delete a directory the operator
    merely pointed at. The existing manifest has to name this release before its bytes may go.
    """
    manifest_path = destination / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ReleaseAssemblyError(
            f"{destination} exists and is not a release directory; refusing to replace it"
        )
    existing = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(existing, dict) or existing.get("release_id") != release_id:
        raise ReleaseAssemblyError(
            f"{destination} holds a different release; refusing to replace it"
        )


def execute_release_assembly(
    plan: ReleaseAssemblyPlan, *, project_root: Path
) -> ReleaseAssemblyResult:
    """Publish one planned release atomically, replacing only a release of the same identity."""
    project_root = project_root.resolve()
    destination = plan.output_path
    replaced = destination.exists()
    if replaced:
        if not plan.overwrite:
            raise ReleaseAssemblyError(
                f"release directory already exists: {destination}; pass --force to replace it"
            )
        _assert_replaceable(destination, plan.release_id)

    staging = destination.parent / f".{plan.release_id}.release.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, mode=0o700)
    try:
        # Content first, manifest last: an interrupted export leaves a staging directory with no
        # manifest, never a published release that indexes files it does not contain.
        for file in plan.files:
            _publish(staging / file.relative_path, file.content)
        _publish(
            staging / MANIFEST_FILENAME,
            serialize_release_yaml(plan.manifest).encode("utf-8"),
        )
        if replaced:
            shutil.rmtree(destination)
        os.replace(staging, destination)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ReleaseAssemblyError(f"could not publish release {plan.release_id}: {exc}") from exc
    written = tuple(destination / file.relative_path for file in plan.files) + (
        destination / MANIFEST_FILENAME,
    )
    return ReleaseAssemblyResult(
        release_id=plan.release_id,
        output_path=destination,
        manifest_file=destination / MANIFEST_FILENAME,
        written_files=written,
        replaced=replaced,
        dry_run=False,
    )


def verify_release(release_path: Path) -> tuple[str, ...]:
    """Re-verify a published release against its own manifest, returning every discrepancy."""
    manifest_path = release_path / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ReleaseAssemblyError(f"no release manifest at {manifest_path}")
    manifest = _load_contract(manifest_path, ReleaseManifest)
    problems: list[str] = []
    for artifact in manifest.artifacts:
        path = release_path / artifact.file
        if not path.is_file():
            problems.append(f"missing artifact: {artifact.file.as_posix()}")
            continue
        content = path.read_bytes()
        if sha256_bytes(content) != artifact.sha256:
            problems.append(f"hash mismatch: {artifact.file.as_posix()}")
        elif len(content) != artifact.byte_count:
            problems.append(f"byte count mismatch: {artifact.file.as_posix()}")
    lineage_path = release_path / LINEAGE_FILENAME
    if lineage_path.is_file():
        payload = json.loads(lineage_path.read_text(encoding="utf-8"))
        if len(payload.get("chunks", ())) != manifest.chunk_count:
            problems.append("lineage does not cover every released chunk")
    return tuple(problems)
