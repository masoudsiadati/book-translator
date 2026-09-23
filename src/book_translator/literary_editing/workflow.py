"""Immutable orchestration for chapter-level editing and post-edit fidelity verification."""

import hashlib
import os
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from book_translator.models import (
    SCHEMA_VERSION,
    BlindComparisonArtifact,
    BookLiteraryProfile,
    LiteraryCallRole,
    LiteraryChapterArtifact,
    LiteraryChapterStatus,
    LiteraryDiffClass,
    LiteraryEvidenceReference,
    LiteraryParagraphFidelity,
    LiteraryParagraphRecord,
    LiteraryProviderCall,
    LiteraryRunArtifact,
    LiteraryRunStatus,
    LiterarySemanticEvidence,
    LiterarySemanticParagraphEvidence,
    LiteraryTextLayerReference,
    PersianLiteraryStyleContract,
    ReleaseManifest,
    SemanticFinding,
    SemanticGateStatus,
    SemanticParagraphOutcome,
    SemanticQAEvidence,
    SemanticVerdict,
    TerminologyRegistry,
    TranslationAcceptanceStatus,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
)
from book_translator.paragraph_markers import source_paragraphs
from book_translator.paths import ProjectPaths
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
from book_translator.semantic_qa.engine import ParagraphTask, build_batches, build_tasks
from book_translator.semantic_qa.policy import EN_FA_JUDGE_V1, apply_policy
from book_translator.semantic_qa.providers import JudgeCall, TransportSemanticJudge
from book_translator.semantic_qa.rendering import (
    JUDGE_PROMPT_VERSION,
    JUDGE_SCHEMA_VERSION,
    render_user_message,
)
from book_translator.terminology import load_registry
from book_translator.translation_queue import load_run_summary

from .blind import (
    BLIND_COMPARISON_CONTRACT_VERSION,
    BlindChapterInput,
    build_blind_comparison,
)
from .engine import (
    ChapterParagraph,
    ChapterSection,
    assess_mechanical_candidate,
    canonical_document,
    marked_paragraphs,
    marked_values,
    measure_diff,
    merge_editor_responses,
    plan_sections,
    terminology_differences,
    terminology_json,
    text_sha256,
)
from .parsing import CONTINUITY_SCHEMA_VERSION, EDITOR_SCHEMA_VERSION
from .policy import (
    DEFAULT_CORRECTION_POLICY,
    DEFAULT_EDIT_POLICY,
    LITERARY_CORRECTION_POLICY_VERSION,
    LITERARY_EDIT_POLICY_VERSION,
    LiteraryCorrectionPolicy,
    LiteraryEditPolicy,
)
from .providers import LiteraryProviderError, TransportLiteraryRoles
from .rendering import (
    CONTINUITY_PROMPT_VERSION,
    CORRECTION_PROMPT_VERSION,
    EDITOR_PROMPT_VERSION,
    render_continuity_message,
    render_correction_message,
    render_editor_message,
)

LITERARY_EDITOR_ENGINE_VERSION = "1.0.0"


class LiteraryWorkflowError(ValueError):
    """Raised when the literary layer cannot prove a safe immutable result."""


@dataclass(frozen=True, slots=True)
class PlannedLiteraryChapter:
    chapter_id: str
    paragraphs: tuple[ChapterParagraph, ...]
    sections: tuple[ChapterSection, ...]
    faithful_files: tuple[Path, ...]
    faithful_chapter_sha256: str
    existing_semantic: dict[str, LiteraryEvidenceReference]
    v1_file: Path | None
    v1_file_sha256: str | None
    v1_blocks: tuple[str, ...] | None

    @property
    def source_text(self) -> str:
        return canonical_document([item.source_text for item in self.paragraphs])

    @property
    def faithful_text(self) -> str:
        return canonical_document([item.faithful_text for item in self.paragraphs])


@dataclass(frozen=True, slots=True)
class ReusableLiteraryChapter:
    source_root: Path
    artifact: LiteraryChapterArtifact
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class LiteraryRunPlan:
    project_root: Path
    literary_run_id: str
    destination: Path
    staging: Path
    input_run_id: str
    input_snapshot_id: str
    historical_run_id: str | None
    historical_release: Path | None
    reuse_source_literary_run_id: str | None
    provider: str
    model_requested: str | None
    style: PersianLiteraryStyleContract
    style_bytes: bytes
    style_sha256: str
    profile: BookLiteraryProfile | None
    profile_bytes: bytes | None
    profile_sha256: str | None
    registry: TerminologyRegistry | None
    chapters: tuple[PlannedLiteraryChapter, ...]
    reusable_chapters: dict[str, ReusableLiteraryChapter]
    created_at: datetime
    already_complete: LiteraryRunArtifact | None = None


@dataclass(frozen=True, slots=True)
class LiteraryRunResult:
    artifact: LiteraryRunArtifact
    destination: Path | None
    blind_artifact: BlindComparisonArtifact | None
    skipped: bool = False


def _verified_text(path: Path, expected_sha256: str, label: str) -> str:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise LiteraryWorkflowError(f"could not read {label}: {path}") from exc
    if sha256_bytes(content) != expected_sha256:
        raise LiteraryWorkflowError(f"{label} hash mismatch: {path}")
    try:
        return content.decode("utf-8")
    except UnicodeError as exc:
        raise LiteraryWorkflowError(f"{label} is not UTF-8: {path}") from exc


def _existing_semantic_references(
    project_root: Path,
    state_path: Path,
    job: TranslationJob,
    paragraph_count: int,
) -> dict[int, LiteraryEvidenceReference]:
    latest: dict[int, LiteraryEvidenceReference] = {}
    evidence_paths = (state_path / "jobs" / job.chunk_id / "semantic-qa").glob("*/evidence.yaml")
    for path in sorted(evidence_paths):
        content = path.read_bytes()
        evidence = SemanticQAEvidence.model_validate(yaml.safe_load(content.decode("utf-8")))
        reference = LiteraryEvidenceReference(
            kind="existing_semantic_gate",
            file=project_relative(path, project_root),
            sha256=sha256_bytes(content),
        )
        for outcome in evidence.outcomes:
            if outcome.policy_outcome is SemanticVerdict.PASS:
                latest[outcome.paragraph_index] = reference
    if set(latest) != set(range(1, paragraph_count + 1)):
        raise LiteraryWorkflowError(
            f"job {job.job_id} lacks reusable PASS evidence for every faithful paragraph"
        )
    return latest


def _load_registry(manifest: TranslationManifest, project_root: Path) -> TerminologyRegistry | None:
    if manifest.terminology is None:
        return None
    reference = manifest.terminology.frozen_file or manifest.terminology.file
    path = project_root / reference
    if sha256_bytes(path.read_bytes()) != manifest.terminology.sha256:
        raise LiteraryWorkflowError("frozen terminology resource hash mismatch")
    return load_registry(path)


def _historical_release(
    release: Path | None,
    *,
    project_root: Path,
    historical_run_id: str | None,
) -> tuple[Path | None, ReleaseManifest | None]:
    if release is None:
        if historical_run_id is not None:
            raise LiteraryWorkflowError("a historical run requires its published release")
        return None, None
    root = (release if release.is_absolute() else project_root / release).resolve()
    try:
        root.relative_to(project_root)
    except ValueError as exc:
        raise LiteraryWorkflowError("historical release must be inside the project") from exc
    manifest = load_yaml(root / "manifest.yaml", ReleaseManifest)
    if historical_run_id is not None and manifest.source_translation_run != historical_run_id:
        raise LiteraryWorkflowError("historical release does not belong to the requested V1 run")
    return root, manifest


def _release_chapter(
    chapter_id: str,
    paragraph_count: int,
    *,
    release_root: Path | None,
    manifest: ReleaseManifest | None,
    project_root: Path,
) -> tuple[Path | None, str | None, tuple[str, ...] | None]:
    if release_root is None or manifest is None:
        return None, None, None
    record = next((item for item in manifest.chapters if item.chapter_id == chapter_id), None)
    if record is None:
        raise LiteraryWorkflowError(f"historical release lacks chapter {chapter_id}")
    path = release_root / record.file
    text = _verified_text(path, record.sha256, "historical published chapter")
    blocks = source_paragraphs(text)
    # Release paragraph 1 is its heading by contract; it is metadata, not a translated body block.
    if len(blocks) != paragraph_count + 1:
        raise LiteraryWorkflowError(
            f"historical chapter {chapter_id} does not align positionally with the V2 chapter"
        )
    return project_relative(path, project_root), record.sha256, blocks[1:]


def _literary_identity(
    *,
    input_run_id: str,
    input_snapshot_id: str,
    provider: str,
    model: str | None,
    style_sha256: str,
    profile_sha256: str | None,
    historical_run_id: str | None,
    release_manifest_sha256: str | None,
    reuse_source_literary_run_id: str | None,
    reused_chapters: list[tuple[str, str]],
    chapters: list[tuple[str, str, str]],
) -> str:
    material = "\n".join(
        (
            LITERARY_EDITOR_ENGINE_VERSION,
            EDITOR_PROMPT_VERSION,
            EDITOR_SCHEMA_VERSION,
            LITERARY_EDIT_POLICY_VERSION,
            CORRECTION_PROMPT_VERSION,
            LITERARY_CORRECTION_POLICY_VERSION,
            CONTINUITY_PROMPT_VERSION,
            CONTINUITY_SCHEMA_VERSION,
            BLIND_COMPARISON_CONTRACT_VERSION,
            input_run_id,
            input_snapshot_id,
            provider,
            model or "<selected-provider-model>",
            style_sha256,
            profile_sha256 or "<no-profile>",
            historical_run_id or "<no-historical-run>",
            release_manifest_sha256 or "<no-historical-release>",
            reuse_source_literary_run_id or "<no-literary-reuse>",
            *(f"reuse:{chapter}:{digest}" for chapter, digest in reused_chapters),
            *(f"{chapter}:{source}:{faithful}" for chapter, source, faithful in chapters),
        )
    )
    return f"lr-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _validate_complete_run(path: Path, literary_run_id: str) -> LiteraryRunArtifact:
    artifact = load_yaml(path / "run.yaml", LiteraryRunArtifact)
    if artifact.literary_run_id != literary_run_id:
        raise LiteraryWorkflowError("existing literary run identity mismatch")
    for chapter in artifact.chapters:
        _verified_text(
            path.parents[2] / chapter.output_file,
            chapter.output_sha256,
            "literary output",
        )
    return artifact


def _validate_reusable_chapter(
    artifact: LiteraryChapterArtifact,
    chapter: PlannedLiteraryChapter,
    *,
    source_root: Path,
    provider: str,
    model: str | None,
    style_version: str,
) -> None:
    """Prove an atomically published chapter can be carried into a resumed run."""
    if artifact.chapter_id != chapter.chapter_id:
        raise LiteraryWorkflowError("literary reuse chapter identity mismatch")
    if artifact.literary_engine_version != LITERARY_EDITOR_ENGINE_VERSION:
        raise LiteraryWorkflowError("literary reuse chapter uses an incompatible engine")
    if artifact.style_contract_version != style_version:
        raise LiteraryWorkflowError("literary reuse chapter uses a different style contract")
    if artifact.edit_policy_version != LITERARY_EDIT_POLICY_VERSION:
        raise LiteraryWorkflowError("literary reuse chapter uses a different edit policy")
    if (
        artifact.correction_prompt_version != CORRECTION_PROMPT_VERSION
        or artifact.correction_policy_version != LITERARY_CORRECTION_POLICY_VERSION
    ):
        raise LiteraryWorkflowError(
            "literary reuse chapter uses incompatible correction governance"
        )
    # Prompt/schema 1.0.1/1.0.0 produced the completed chapter before the current
    # metadata-normalization clarification. Its prose and semantic evidence remain valid.
    supported_editor_contracts = {
        (EDITOR_PROMPT_VERSION, EDITOR_SCHEMA_VERSION),
        ("1.0.1", "1.0.0"),
    }
    if (artifact.editor_prompt_version, artifact.editor_schema_version) not in (
        supported_editor_contracts
    ):
        raise LiteraryWorkflowError("literary reuse chapter uses an incompatible editor contract")
    supported_continuity_contracts = {
        (CONTINUITY_PROMPT_VERSION, CONTINUITY_SCHEMA_VERSION),
        ("1.0.1", "1.0.0"),
    }
    if (artifact.continuity_prompt_version, artifact.continuity_schema_version) not in (
        supported_continuity_contracts
    ):
        raise LiteraryWorkflowError("literary reuse chapter uses incompatible continuity QA")
    expected_ids = [item.paragraph_id for item in chapter.paragraphs]
    if [item.paragraph_id for item in artifact.paragraphs] != expected_ids:
        raise LiteraryWorkflowError("literary reuse paragraph identities do not match")
    for planned, recorded in zip(chapter.paragraphs, artifact.paragraphs, strict=True):
        if recorded.source_sha256 != text_sha256(
            planned.source_text
        ) or recorded.faithful_sha256 != text_sha256(planned.faithful_text):
            raise LiteraryWorkflowError("literary reuse source or faithful input hash changed")
    chapter_root = source_root / "chapters" / chapter.chapter_id
    final_text = _verified_text(
        chapter_root / "chapter.md", artifact.output_sha256, "reusable literary chapter"
    )
    if len(source_paragraphs(final_text)) != len(chapter.paragraphs):
        raise LiteraryWorkflowError("literary reuse chapter paragraph count changed")
    project_root = source_root.parents[2]
    for record in artifact.paragraphs:
        for reference in record.semantic_evidence:
            if reference.kind == "existing_semantic_gate":
                evidence_path = project_root / reference.file
            else:
                evidence_path = chapter_root / "semantic" / reference.file.name
            if (
                not evidence_path.is_file()
                or sha256_bytes(evidence_path.read_bytes()) != reference.sha256
            ):
                raise LiteraryWorkflowError(
                    "literary reuse semantic evidence is missing or changed"
                )
    for call in artifact.provider_calls:
        if call.provider != provider or call.model_requested != model:
            raise LiteraryWorkflowError("literary reuse provider or requested model differs")


def plan_literary_run(
    input_run_id_or_path: str | Path,
    *,
    project_root: Path,
    chapter_ids: tuple[str, ...],
    provider: str,
    model: str | None,
    style_file: Path,
    profile_file: Path | None = None,
    historical_run_id: str | None = None,
    historical_release: Path | None = None,
    reuse_literary_run_id: str | None = None,
    edit_policy: LiteraryEditPolicy = DEFAULT_EDIT_POLICY,
    now: Callable[[], datetime] | None = None,
) -> LiteraryRunPlan:
    """Read and hash every input without writing or contacting a provider."""
    project_root = project_root.resolve()
    if not chapter_ids or len(chapter_ids) != len(set(chapter_ids)):
        raise LiteraryWorkflowError("literary editing requires unique explicit chapter selections")
    state = load_run_summary(input_run_id_or_path, project_root=project_root)
    if not uses_paragraph_markers(state.run.output_contract_version):
        raise LiteraryWorkflowError("literary editing requires a marked-contract translation run")
    manifest = load_yaml(
        project_root / state.run.snapshot_path / "manifest.yaml", TranslationManifest
    )
    if manifest.book.source_language != "en" or manifest.book.target_language != "fa":
        raise LiteraryWorkflowError("the Persian literary style contract supports en -> fa only")
    style_path = (style_file if style_file.is_absolute() else project_root / style_file).resolve()
    style_bytes = style_path.read_bytes()
    style = PersianLiteraryStyleContract.model_validate(yaml.safe_load(style_bytes.decode("utf-8")))
    style_sha = sha256_bytes(style_bytes)
    profile: BookLiteraryProfile | None = None
    profile_bytes: bytes | None = None
    profile_sha: str | None = None
    if profile_file is not None:
        profile_path = (
            profile_file if profile_file.is_absolute() else project_root / profile_file
        ).resolve()
        profile_bytes = profile_path.read_bytes()
        profile = BookLiteraryProfile.model_validate(yaml.safe_load(profile_bytes.decode("utf-8")))
        if profile.book_id != manifest.book.book_id:
            raise LiteraryWorkflowError("book literary profile belongs to another book")
        profile_sha = sha256_bytes(profile_bytes)
    registry = _load_registry(manifest, project_root)
    release_root, release_manifest = _historical_release(
        historical_release,
        project_root=project_root,
        historical_run_id=historical_run_id,
    )
    release_manifest_sha = (
        sha256_bytes((release_root / "manifest.yaml").read_bytes())
        if release_root is not None
        else None
    )
    planned: list[PlannedLiteraryChapter] = []
    identity_chapters: list[tuple[str, str, str]] = []
    for chapter_id in chapter_ids:
        jobs = sorted(
            (job for job in state.jobs if job.chapter_id == chapter_id),
            key=lambda job: job.chunk_sequence,
        )
        if not jobs:
            raise LiteraryWorkflowError(f"translation run lacks selected chapter {chapter_id}")
        paragraphs: list[ChapterParagraph] = []
        faithful_files: list[Path] = []
        existing: dict[str, LiteraryEvidenceReference] = {}
        global_index = 0
        for job in jobs:
            if (
                job.status is not TranslationJobStatus.SUCCEEDED
                or job.acceptance_status is not TranslationAcceptanceStatus.ACCEPTED
                or job.semantic_status is not SemanticGateStatus.PASS
            ):
                raise LiteraryWorkflowError(
                    f"job {job.job_id} is not mechanically and semantically accepted"
                )
            try:
                acceptance = load_effective_acceptance(job, project_root=project_root)
            except ReleaseAssemblyError as exc:
                raise LiteraryWorkflowError(f"cannot resolve {job.job_id}: {exc}") from exc
            source = _verified_text(
                project_root / acceptance.source_file,
                acceptance.source_sha256,
                "frozen chunk source",
            )
            faithful = _verified_text(
                project_root / acceptance.translation_file,
                acceptance.translation_sha256,
                "semantically accepted translation",
            )
            source_blocks = source_paragraphs(source)
            faithful_blocks = source_paragraphs(faithful)
            if len(source_blocks) != len(faithful_blocks):
                raise LiteraryWorkflowError(f"job {job.job_id} is not paragraph-aligned")
            refs = _existing_semantic_references(project_root, state.path, job, len(source_blocks))
            faithful_files.append(acceptance.translation_file)
            for local_index, (source_block, faithful_block) in enumerate(
                zip(source_blocks, faithful_blocks, strict=True), 1
            ):
                global_index += 1
                paragraph_id = f"P{global_index:04d}"
                paragraphs.append(
                    ChapterParagraph(
                        paragraph_id=paragraph_id,
                        paragraph_index=global_index,
                        chunk_id=job.chunk_id,
                        chunk_paragraph_index=local_index,
                        source_text=source_block,
                        faithful_text=faithful_block,
                    )
                )
                existing[paragraph_id] = refs[local_index]
        paragraph_tuple = tuple(paragraphs)
        source_text = canonical_document([item.source_text for item in paragraph_tuple])
        faithful_text = canonical_document([item.faithful_text for item in paragraph_tuple])
        v1_file, v1_sha, v1_blocks = _release_chapter(
            chapter_id,
            len(paragraph_tuple),
            release_root=release_root,
            manifest=release_manifest,
            project_root=project_root,
        )
        planned.append(
            PlannedLiteraryChapter(
                chapter_id=chapter_id,
                paragraphs=paragraph_tuple,
                sections=plan_sections(paragraph_tuple, policy=edit_policy),
                faithful_files=tuple(faithful_files),
                faithful_chapter_sha256=text_sha256(faithful_text),
                existing_semantic=existing,
                v1_file=v1_file,
                v1_file_sha256=v1_sha,
                v1_blocks=v1_blocks,
            )
        )
        identity_chapters.append((chapter_id, text_sha256(source_text), text_sha256(faithful_text)))
    reusable: dict[str, ReusableLiteraryChapter] = {}
    reused_identities: list[tuple[str, str]] = []
    if reuse_literary_run_id is not None:
        paths = ProjectPaths(project_root)
        final_source = paths.literary_run(reuse_literary_run_id)
        staging_source = paths.literary_runs / f".{reuse_literary_run_id}.staging"
        source_root = final_source if final_source.is_dir() else staging_source
        if not source_root.is_dir():
            raise LiteraryWorkflowError("literary reuse source does not exist")
        if (source_root / "style-contract.yaml").read_bytes() != style_bytes:
            raise LiteraryWorkflowError("literary reuse source has a different style contract")
        source_profile = source_root / "book-profile.yaml"
        if profile_bytes is None and source_profile.exists():
            raise LiteraryWorkflowError("literary reuse source unexpectedly has a book profile")
        if profile_bytes is not None and (
            not source_profile.is_file() or source_profile.read_bytes() != profile_bytes
        ):
            raise LiteraryWorkflowError("literary reuse source has a different book profile")
        for chapter in planned:
            artifact_path = source_root / "chapters" / chapter.chapter_id / "chapter.yaml"
            if not artifact_path.is_file():
                continue
            artifact_bytes = artifact_path.read_bytes()
            artifact = LiteraryChapterArtifact.model_validate(
                yaml.safe_load(artifact_bytes.decode("utf-8"))
            )
            _validate_reusable_chapter(
                artifact,
                chapter,
                source_root=source_root,
                provider=provider,
                model=model,
                style_version=style.contract_version,
            )
            digest = sha256_bytes(artifact_bytes)
            reusable[chapter.chapter_id] = ReusableLiteraryChapter(
                source_root=source_root,
                artifact=artifact,
                artifact_sha256=digest,
            )
            reused_identities.append((chapter.chapter_id, digest))
        if not reusable:
            raise LiteraryWorkflowError("literary reuse source has no compatible selected chapter")
    literary_id = _literary_identity(
        input_run_id=state.run.run_id,
        input_snapshot_id=state.run.snapshot_id,
        provider=provider,
        model=model,
        style_sha256=style_sha,
        profile_sha256=profile_sha,
        historical_run_id=historical_run_id,
        release_manifest_sha256=release_manifest_sha,
        reuse_source_literary_run_id=reuse_literary_run_id,
        reused_chapters=reused_identities,
        chapters=identity_chapters,
    )
    paths = ProjectPaths(project_root)
    destination = paths.literary_run(literary_id)
    complete = _validate_complete_run(destination, literary_id) if destination.is_dir() else None
    return LiteraryRunPlan(
        project_root=project_root,
        literary_run_id=literary_id,
        destination=destination,
        staging=paths.literary_runs / f".{literary_id}.staging",
        input_run_id=state.run.run_id,
        input_snapshot_id=state.run.snapshot_id,
        historical_run_id=historical_run_id,
        historical_release=(project_relative(release_root, project_root) if release_root else None),
        reuse_source_literary_run_id=reuse_literary_run_id,
        provider=provider,
        model_requested=model,
        style=style,
        style_bytes=style_bytes,
        style_sha256=style_sha,
        profile=profile,
        profile_bytes=profile_bytes,
        profile_sha256=profile_sha,
        registry=registry,
        chapters=tuple(planned),
        reusable_chapters=reusable,
        created_at=(now or (lambda: datetime.now(UTC)))(),
        already_complete=complete,
    )


def _semantic_call_record(record: object) -> LiteraryProviderCall:
    from book_translator.models import SemanticJudgeRequestRecord

    item = SemanticJudgeRequestRecord.model_validate(record)
    return LiteraryProviderCall(
        role=LiteraryCallRole.SEMANTIC_REJUDGE,
        request_id=item.request_id,
        provider=item.provider,
        adapter_version=item.adapter_version,
        model_requested=item.model_requested,
        model_returned=item.model_returned,
        system_message_sha256=item.system_message_sha256,
        user_message_sha256=item.user_message_sha256,
        input_tokens=item.input_tokens,
        output_tokens=item.output_tokens,
        total_tokens=item.total_tokens,
        latency_milliseconds=item.latency_milliseconds,
        prompt_version=JUDGE_PROMPT_VERSION,
        schema_version_used=JUDGE_SCHEMA_VERSION,
        policy_version=EN_FA_JUDGE_V1.policy_version,
    )


def _judge_outcomes(
    call: JudgeCall,
    tasks_by_id: dict[str, ParagraphTask],
) -> list[SemanticParagraphOutcome]:
    outcomes: list[SemanticParagraphOutcome] = []
    for verdict in call.response.verdicts:
        task = tasks_by_id[verdict.paragraph_id]
        outcome, material, advisory = apply_policy(verdict, policy=EN_FA_JUDGE_V1)
        outcomes.append(
            SemanticParagraphOutcome(
                paragraph_id=verdict.paragraph_id,
                paragraph_index=task.paragraph_index,
                judge_verdict=verdict.verdict,
                policy_outcome=outcome,
                judge_confidence=verdict.confidence,
                source_word_count=task.source_word_count,
                target_word_count=task.target_word_count,
                material_findings=list(material),
                advisory_findings=list(advisory),
            )
        )
    return outcomes


@dataclass(slots=True)
class _ChapterExecution:
    provider_calls: list[LiteraryProviderCall]
    semantic_bytes: list[tuple[Path, bytes]]
    editor_bytes: list[tuple[Path, bytes]]
    correction_bytes: list[tuple[Path, bytes]]
    continuity_bytes: bytes | None
    fresh_evidence: dict[str, LiteraryEvidenceReference]
    semantic_results: dict[str, SemanticVerdict]
    semantic_findings: dict[str, list[SemanticFinding]]
    evidence_number: int = 0


def _judge_values(
    plan: LiteraryRunPlan,
    chapter: PlannedLiteraryChapter,
    values: list[str],
    indexes: tuple[int, ...],
    *,
    correction_round: int,
    look: str,
    context_radius: int,
    batch_size: int,
    judge: TransportSemanticJudge,
    timestamp: datetime,
    execution: _ChapterExecution,
) -> None:
    if not indexes:
        return
    tasks = build_tasks(
        chapter.source_text,
        canonical_document(values),
        registry=plan.registry,
        paragraph_indexes=indexes,
        context_radius=context_radius,
    )
    tasks_by_id = {task.paragraph_id: task for task in tasks}
    for batch in build_batches(tasks, batch_size=batch_size):
        message = render_user_message(
            batch, chapter_id=chapter.chapter_id, chunk_id=f"chapter-{chapter.chapter_id}"
        )
        call = judge.judge(
            run_id=plan.input_run_id,
            job_id=f"{plan.input_run_id}__literary-{chapter.chapter_id}",
            chunk_id=f"chapter-{chapter.chapter_id}",
            user_message=message,
            paragraph_ids=batch.paragraph_ids,
            source_language="en",
            target_language="fa",
            repetition=correction_round + 1,
            batch_size=batch_size,
        )
        call_record = _semantic_call_record(call.record)
        execution.provider_calls.append(call_record)
        outcomes = _judge_outcomes(call, tasks_by_id)
        execution.evidence_number += 1
        evidence = LiterarySemanticEvidence(
            schema_version=SCHEMA_VERSION,
            literary_run_id=plan.literary_run_id,
            input_translation_run_id=plan.input_run_id,
            chapter_id=chapter.chapter_id,
            evidence_number=execution.evidence_number,
            correction_round=correction_round,
            look=look,  # type: ignore[arg-type]
            candidate_chapter_sha256=text_sha256(canonical_document(values)),
            call=call_record,
            paragraphs=[
                LiterarySemanticParagraphEvidence(
                    paragraph_id=outcome.paragraph_id,
                    source_sha256=text_sha256(
                        chapter.paragraphs[outcome.paragraph_index - 1].source_text
                    ),
                    candidate_sha256=text_sha256(values[outcome.paragraph_index - 1]),
                    outcome=outcome,
                )
                for outcome in outcomes
            ],
            created_at=timestamp,
        )
        content = serialize_yaml(evidence).encode("utf-8")
        relative = Path(
            f"workspace/literary-runs/{plan.literary_run_id}/chapters/"
            f"{chapter.chapter_id}/semantic/{execution.evidence_number:03d}.yaml"
        )
        execution.semantic_bytes.append((relative, content))
        reference = LiteraryEvidenceReference(
            kind=(
                "literary_semantic_second_look" if look == "second" else "literary_semantic_rejudge"
            ),
            file=relative,
            sha256=sha256_bytes(content),
        )
        for outcome in outcomes:
            execution.semantic_results[outcome.paragraph_id] = outcome.policy_outcome
            execution.semantic_findings[outcome.paragraph_id] = list(outcome.material_findings)
            if outcome.policy_outcome is SemanticVerdict.PASS:
                execution.fresh_evidence[outcome.paragraph_id] = reference


def _write_private_if_same(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise LiteraryWorkflowError(f"conflicting literary staging artifact: {path.name}")
        return
    private_write(path, content)


def _publish_chapter(
    plan: LiteraryRunPlan,
    chapter: PlannedLiteraryChapter,
    artifact: LiteraryChapterArtifact,
    final_text: str,
    execution: _ChapterExecution,
) -> Path:
    chapters_root = plan.staging / "chapters"
    chapters_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = chapters_root / chapter.chapter_id
    if destination.exists():
        existing = load_yaml(destination / "chapter.yaml", LiteraryChapterArtifact)
        if existing.output_sha256 != artifact.output_sha256:
            raise LiteraryWorkflowError(
                f"completed literary chapter {chapter.chapter_id} conflicts"
            )
        return destination
    with tempfile.TemporaryDirectory(
        prefix=f".{chapter.chapter_id}.", suffix=".staging", dir=chapters_root
    ) as temporary:
        root = Path(temporary)
        os.chmod(root, 0o700)
        (root / "editor").mkdir(mode=0o700)
        (root / "semantic").mkdir(mode=0o700)
        (root / "corrections").mkdir(mode=0o700)
        for relative, content in execution.editor_bytes:
            _write_private_if_same(root / "editor" / relative.name, content)
        for relative, content in execution.semantic_bytes:
            _write_private_if_same(root / "semantic" / relative.name, content)
        for relative, content in execution.correction_bytes:
            _write_private_if_same(root / "corrections" / relative.name, content)
        if execution.continuity_bytes is None:
            raise LiteraryWorkflowError("continuity review evidence is missing")
        _write_private_if_same(root / "continuity.yaml", execution.continuity_bytes)
        _write_private_if_same(root / "chapter.md", final_text.encode("utf-8"))
        _write_private_if_same(root / "chapter.yaml", serialize_yaml(artifact).encode("utf-8"))
        os.replace(root, destination)
    return destination


def _carry_forward_chapter(
    plan: LiteraryRunPlan,
    chapter: PlannedLiteraryChapter,
    reusable: ReusableLiteraryChapter,
    *,
    timestamp: datetime,
) -> LiteraryChapterArtifact:
    """Copy one verified completed chapter without repeating any provider operation."""
    old = reusable.artifact
    source_chapter = reusable.source_root / "chapters" / chapter.chapter_id
    final_text = _verified_text(
        source_chapter / "chapter.md", old.output_sha256, "reusable literary chapter"
    )
    calls = [
        call.model_copy(update={"reused_from_literary_run_id": plan.reuse_source_literary_run_id})
        for call in old.provider_calls
    ]
    calls_by_request = {call.request_id: call for call in calls}
    semantic_bytes: list[tuple[Path, bytes]] = []
    semantic_references: dict[tuple[str, str], LiteraryEvidenceReference] = {}
    for source_path in sorted((source_chapter / "semantic").glob("*.yaml")):
        evidence = LiterarySemanticEvidence.model_validate(
            yaml.safe_load(source_path.read_text(encoding="utf-8"))
        )
        copied_call = calls_by_request.get(evidence.call.request_id)
        if copied_call is None:
            copied_call = evidence.call.model_copy(
                update={"reused_from_literary_run_id": plan.reuse_source_literary_run_id}
            )
        copied = evidence.model_copy(
            update={
                "literary_run_id": plan.literary_run_id,
                "call": copied_call,
            }
        )
        content = serialize_yaml(copied).encode("utf-8")
        relative = Path(
            f"workspace/literary-runs/{plan.literary_run_id}/chapters/"
            f"{chapter.chapter_id}/semantic/{source_path.name}"
        )
        semantic_bytes.append((relative, content))
        semantic_references[(source_path.name, sha256_bytes(source_path.read_bytes()))] = (
            LiteraryEvidenceReference(
                kind=(
                    "literary_semantic_second_look"
                    if copied.look == "second"
                    else "literary_semantic_rejudge"
                ),
                file=relative,
                sha256=sha256_bytes(content),
            )
        )
    records: list[LiteraryParagraphRecord] = []
    for record in old.paragraphs:
        references: list[LiteraryEvidenceReference] = []
        for reference in record.semantic_evidence:
            if reference.kind == "existing_semantic_gate":
                references.append(reference)
                continue
            replacement = semantic_references.get((reference.file.name, reference.sha256))
            if replacement is None:
                raise LiteraryWorkflowError("could not remap reused literary semantic evidence")
            references.append(replacement)
        records.append(record.model_copy(update={"semantic_evidence": references}))
    output_relative = Path(
        f"workspace/literary-runs/{plan.literary_run_id}/chapters/{chapter.chapter_id}/chapter.md"
    )
    layers = [
        (
            layer.model_copy(update={"files": [output_relative]})
            if layer.layer == "v2_literary_edited"
            else layer
        )
        for layer in old.text_layers
    ]
    artifact = old.model_copy(
        update={
            "paragraphs": records,
            "provider_calls": calls,
            "text_layers": layers,
            "output_file": output_relative,
            "reused_from_literary_run_id": plan.reuse_source_literary_run_id,
            "reused_chapter_artifact_sha256": reusable.artifact_sha256,
            "created_at": timestamp,
        }
    )
    # Revalidate model-copy updates so cross-field fidelity invariants remain fail-closed.
    artifact = LiteraryChapterArtifact.model_validate(artifact.model_dump(mode="python"))
    execution = _ChapterExecution(
        provider_calls=calls,
        semantic_bytes=semantic_bytes,
        editor_bytes=[
            (path, path.read_bytes()) for path in sorted((source_chapter / "editor").glob("*.yaml"))
        ],
        correction_bytes=[
            (path, path.read_bytes())
            for path in sorted((source_chapter / "corrections").glob("*.yaml"))
        ],
        continuity_bytes=(source_chapter / "continuity.yaml").read_bytes(),
        fresh_evidence={},
        semantic_results={},
        semantic_findings={},
    )
    _publish_chapter(plan, chapter, artifact, final_text, execution)
    return artifact


def _execute_chapter(
    plan: LiteraryRunPlan,
    chapter: PlannedLiteraryChapter,
    *,
    literary: TransportLiteraryRoles,
    judge: TransportSemanticJudge,
    edit_policy: LiteraryEditPolicy,
    correction_policy: LiteraryCorrectionPolicy,
    timestamp: datetime,
) -> LiteraryChapterArtifact:
    execution = _ChapterExecution([], [], [], [], None, {}, {}, {})
    editor_responses = []
    for section in chapter.sections:
        visible = tuple(chapter.paragraphs[index] for index in section.visible_indexes)
        editable = tuple(chapter.paragraphs[index] for index in section.editable_indexes)
        message = render_editor_message(
            chapter_id=chapter.chapter_id,
            paragraph_ids=tuple(item.paragraph_id for item in visible),
            editable_ids=tuple(item.paragraph_id for item in editable),
            marked_source=marked_paragraphs(visible, source=True),
            marked_faithful=marked_paragraphs(visible, source=False),
            terminology_json=terminology_json(plan.registry, chapter.source_text),
            style=plan.style,
            profile=plan.profile,
            section_label=section.label,
        )
        attempts = 0
        while True:
            attempts += 1
            try:
                call = literary.edit(
                    run_id=plan.input_run_id,
                    chapter_id=chapter.chapter_id,
                    sequence=section.section_number * 10 + attempts,
                    user_message=message,
                    expected_ids=tuple(item.paragraph_id for item in editable),
                    faithful_texts=tuple(item.faithful_text for item in editable),
                )
                break
            except LiteraryProviderError as exc:
                if exc.record is not None:
                    execution.provider_calls.append(exc.record)
                if not exc.retryable or attempts > edit_policy.max_editor_contract_retries:
                    raise
        editor_responses.append(call.response)
        execution.provider_calls.append(call.record)
        execution.editor_bytes.append(
            (
                Path(f"{section.section_number:03d}-response.yaml"),
                serialize_yaml(call.response).encode("utf-8"),
            )
        )
    values, reason_values, governed = merge_editor_responses(
        chapter.paragraphs, chapter.sections, tuple(editor_responses)
    )
    attempted_values = list(values)
    attempted = {
        item.paragraph_id
        for item, value in zip(chapter.paragraphs, attempted_values, strict=True)
        if value != item.faithful_text
    }
    attempt_diffs = {
        item.paragraph_id: measure_diff(item.faithful_text, value, policy=edit_policy)
        for item, value in zip(chapter.paragraphs, attempted_values, strict=True)
        if value != item.faithful_text
    }
    fallback_reasons: dict[str, str] = {}
    mechanics = assess_mechanical_candidate(chapter.paragraphs, values, registry=plan.registry)
    mechanically_refused = set()
    if not mechanics.marker_identity_preserved:
        mechanically_refused.update(attempted)
    for index, codes in mechanics.paragraph_refusals.items():
        paragraph_id = chapter.paragraphs[index - 1].paragraph_id
        if paragraph_id in attempted:
            mechanically_refused.add(paragraph_id)
            fallback_reasons[paragraph_id] = "mechanical_qa:" + ",".join(codes)
    for paragraph_id in mechanically_refused:
        index = int(paragraph_id[1:]) - 1
        values[index] = chapter.paragraphs[index].faithful_text
        fallback_reasons.setdefault(paragraph_id, "marker_identity_failed")
    changed_indexes = tuple(
        item.paragraph_index
        for item, value in zip(chapter.paragraphs, values, strict=True)
        if value != item.faithful_text
    )
    normal = tuple(
        index
        for index in changed_indexes
        if attempt_diffs[chapter.paragraphs[index - 1].paragraph_id].diff_class
        is not LiteraryDiffClass.SUSPICIOUS
    )
    suspicious = tuple(index for index in changed_indexes if index not in normal)
    _judge_values(
        plan,
        chapter,
        values,
        normal,
        correction_round=0,
        look="first",
        context_radius=1,
        batch_size=edit_policy.semantic_batch_size,
        judge=judge,
        timestamp=timestamp,
        execution=execution,
    )
    _judge_values(
        plan,
        chapter,
        values,
        suspicious,
        correction_round=0,
        look="suspicious",
        context_radius=edit_policy.suspicious_context_radius,
        batch_size=1,
        judge=judge,
        timestamp=timestamp,
        execution=execution,
    )
    second_looks: Counter[str] = Counter()
    inconclusive = tuple(
        int(paragraph_id[1:])
        for paragraph_id, verdict in execution.semantic_results.items()
        if verdict is SemanticVerdict.INCONCLUSIVE
    )
    if inconclusive:
        for index in inconclusive:
            second_looks[f"P{index:04d}"] += 1
        _judge_values(
            plan,
            chapter,
            values,
            inconclusive,
            correction_round=0,
            look="second",
            context_radius=edit_policy.suspicious_context_radius,
            batch_size=1,
            judge=judge,
            timestamp=timestamp,
            execution=execution,
        )
    initial_regressions = {
        paragraph_id
        for paragraph_id, verdict in execution.semantic_results.items()
        if verdict is SemanticVerdict.REVISE
    }
    correction_counts: Counter[str] = Counter()
    for round_number in range(1, correction_policy.max_correction_rounds + 1):
        failing = tuple(
            paragraph_id
            for paragraph_id in sorted(execution.semantic_results)
            if execution.semantic_results[paragraph_id] is SemanticVerdict.REVISE
        )
        if not failing:
            break
        findings = {
            paragraph_id: execution.semantic_findings[paragraph_id] for paragraph_id in failing
        }
        message = render_correction_message(
            chapter_id=chapter.chapter_id,
            round_number=round_number,
            editable_ids=failing,
            marked_source=marked_paragraphs(chapter.paragraphs, source=True),
            marked_faithful=marked_paragraphs(chapter.paragraphs, source=False),
            marked_candidate=marked_values(chapter.paragraphs, values),
            findings=findings,
            terminology_json=terminology_json(plan.registry, chapter.source_text),
            style=plan.style,
            profile=plan.profile,
        )
        correction = literary.correct(
            run_id=plan.input_run_id,
            chapter_id=chapter.chapter_id,
            sequence=round_number,
            user_message=message,
            expected_ids=failing,
        )
        execution.provider_calls.append(correction.record)
        execution.correction_bytes.append(
            (
                Path(f"{round_number:03d}-response.yaml"),
                serialize_yaml(correction.response).encode("utf-8"),
            )
        )
        for item in correction.response.paragraphs:
            index = int(item.paragraph_id[1:]) - 1
            values[index] = item.edited_text
            reason_values[item.paragraph_id] = tuple(item.reasons)
            correction_counts[item.paragraph_id] += 1
            attempt_diffs[item.paragraph_id] = measure_diff(
                chapter.paragraphs[index].faithful_text, item.edited_text, policy=edit_policy
            )
        mechanics = assess_mechanical_candidate(chapter.paragraphs, values, registry=plan.registry)
        refused: set[str] = set()
        if not mechanics.marker_identity_preserved:
            refused.update(failing)
        for index, codes in mechanics.paragraph_refusals.items():
            paragraph_id = chapter.paragraphs[index - 1].paragraph_id
            if paragraph_id in failing:
                refused.add(paragraph_id)
                fallback_reasons[paragraph_id] = "correction_mechanical_qa:" + ",".join(codes)
        for paragraph_id in refused:
            index = int(paragraph_id[1:]) - 1
            values[index] = chapter.paragraphs[index].faithful_text
            execution.semantic_results[paragraph_id] = SemanticVerdict.PASS
        to_judge = tuple(int(item[1:]) for item in failing if item not in refused)
        ordinary = tuple(
            index
            for index in to_judge
            if attempt_diffs[f"P{index:04d}"].diff_class is not LiteraryDiffClass.SUSPICIOUS
        )
        strong = tuple(index for index in to_judge if index not in ordinary)
        _judge_values(
            plan,
            chapter,
            values,
            ordinary,
            correction_round=round_number,
            look="first",
            context_radius=1,
            batch_size=edit_policy.semantic_batch_size,
            judge=judge,
            timestamp=timestamp,
            execution=execution,
        )
        _judge_values(
            plan,
            chapter,
            values,
            strong,
            correction_round=round_number,
            look="suspicious",
            context_radius=edit_policy.suspicious_context_radius,
            batch_size=1,
            judge=judge,
            timestamp=timestamp,
            execution=execution,
        )
        retry_inconclusive = tuple(
            index
            for index in to_judge
            if execution.semantic_results[f"P{index:04d}"] is SemanticVerdict.INCONCLUSIVE
            and second_looks[f"P{index:04d}"] < correction_policy.max_second_looks
        )
        for index in retry_inconclusive:
            second_looks[f"P{index:04d}"] += 1
        _judge_values(
            plan,
            chapter,
            values,
            retry_inconclusive,
            correction_round=round_number,
            look="second",
            context_radius=edit_policy.suspicious_context_radius,
            batch_size=1,
            judge=judge,
            timestamp=timestamp,
            execution=execution,
        )
    for paragraph_id, verdict in tuple(execution.semantic_results.items()):
        if verdict is SemanticVerdict.PASS:
            continue
        index = int(paragraph_id[1:]) - 1
        values[index] = chapter.paragraphs[index].faithful_text
        fallback_reasons[paragraph_id] = (
            "semantic_inconclusive"
            if verdict is SemanticVerdict.INCONCLUSIVE
            else "retry_budget_exhausted"
        )
    final_text = canonical_document(values)
    final_mechanics = assess_mechanical_candidate(
        chapter.paragraphs, values, registry=plan.registry
    )
    if (
        not final_mechanics.marker_identity_preserved
        or not final_mechanics.structural_report.valid
        or not final_mechanics.alignment_report.valid
        or (
            final_mechanics.terminology_report is not None
            and not final_mechanics.terminology_report.valid
        )
    ):
        raise LiteraryWorkflowError("final literary chapter failed mandatory mechanical QA")
    continuity_message = render_continuity_message(
        chapter_id=chapter.chapter_id,
        marked_chapter=marked_values(chapter.paragraphs, values),
        terminology_json=terminology_json(plan.registry, chapter.source_text),
        style=plan.style,
        profile=plan.profile,
    )
    continuity_attempts = 0
    while True:
        continuity_attempts += 1
        try:
            continuity = literary.review_continuity(
                run_id=plan.input_run_id,
                chapter_id=chapter.chapter_id,
                sequence=continuity_attempts,
                user_message=continuity_message,
                known_ids=frozenset(item.paragraph_id for item in chapter.paragraphs),
            )
            break
        except LiteraryProviderError as exc:
            if exc.record is not None:
                execution.provider_calls.append(exc.record)
            if (
                not exc.retryable
                or continuity_attempts > edit_policy.max_continuity_contract_retries
            ):
                raise
    execution.provider_calls.append(continuity.record)
    execution.continuity_bytes = serialize_yaml(continuity.response).encode("utf-8")
    records: list[LiteraryParagraphRecord] = []
    unchanged_hashes: dict[str, str] = {}
    category_counts: Counter[str] = Counter()
    changed_ids: list[str] = []
    for paragraph, value in zip(chapter.paragraphs, values, strict=True):
        paragraph_id = paragraph.paragraph_id
        changed = value != paragraph.faithful_text
        if changed:
            changed_ids.append(paragraph_id)
            reference = execution.fresh_evidence.get(paragraph_id)
            if reference is None:
                raise LiteraryWorkflowError(
                    f"changed paragraph {paragraph_id} has no fresh semantic PASS evidence"
                )
            evidence = [reference]
            fidelity = LiteraryParagraphFidelity.FRESH_PASS
            category_counts.update(reason.value for reason in reason_values.get(paragraph_id, ()))
        else:
            evidence = [chapter.existing_semantic[paragraph_id]]
            fidelity = (
                LiteraryParagraphFidelity.FALLBACK
                if paragraph_id in fallback_reasons
                else LiteraryParagraphFidelity.REUSED
            )
            unchanged_hashes[paragraph_id] = text_sha256(value)
        diff = measure_diff(paragraph.faithful_text, value, policy=edit_policy)
        attempted_diff = attempt_diffs.get(paragraph_id)
        records.append(
            LiteraryParagraphRecord(
                paragraph_id=paragraph_id,
                paragraph_index=paragraph.paragraph_index,
                chunk_id=paragraph.chunk_id,
                chunk_paragraph_id=paragraph.chunk_paragraph_id,
                chunk_paragraph_index=paragraph.chunk_paragraph_index,
                source_sha256=text_sha256(paragraph.source_text),
                faithful_sha256=text_sha256(paragraph.faithful_text),
                final_sha256=text_sha256(value),
                changed=changed,
                attempted_change=paragraph_id in attempted,
                reasons=list(reason_values.get(paragraph_id, ())),
                diff_class=(
                    diff.diff_class
                    if changed
                    else (attempted_diff.diff_class if attempted_diff else None)
                ),
                character_delta=diff.character_delta,
                word_delta=diff.word_delta,
                character_change_ratio=diff.character_change_ratio,
                fidelity=fidelity,
                correction_rounds=correction_counts[paragraph_id],
                fallback_reason=fallback_reasons.get(paragraph_id),
                semantic_evidence=evidence,
            )
        )
    output_relative = Path(
        f"workspace/literary-runs/{plan.literary_run_id}/chapters/{chapter.chapter_id}/chapter.md"
    )
    layers = []
    if chapter.v1_file is not None and chapter.v1_file_sha256 is not None:
        layers.append(
            LiteraryTextLayerReference(
                layer="v1_published", files=[chapter.v1_file], sha256=chapter.v1_file_sha256
            )
        )
    layers.extend(
        (
            LiteraryTextLayerReference(
                layer="v2_fidelity_only",
                files=list(chapter.faithful_files),
                sha256=chapter.faithful_chapter_sha256,
            ),
            LiteraryTextLayerReference(
                layer="v2_literary_edited", files=[output_relative], sha256=text_sha256(final_text)
            ),
        )
    )
    regressions_corrected = sum(
        paragraph_id in initial_regressions
        and execution.semantic_results.get(paragraph_id) is SemanticVerdict.PASS
        and paragraph_id not in fallback_reasons
        for paragraph_id in initial_regressions
    )
    final_diffs = [
        measure_diff(item.faithful_text, value, policy=edit_policy)
        for item, value in zip(chapter.paragraphs, values, strict=True)
        if value != item.faithful_text
    ]
    artifact = LiteraryChapterArtifact(
        schema_version=SCHEMA_VERSION,
        literary_engine_version=LITERARY_EDITOR_ENGINE_VERSION,
        editor_prompt_version=EDITOR_PROMPT_VERSION,
        editor_schema_version=EDITOR_SCHEMA_VERSION,
        style_contract_version=plan.style.contract_version,
        edit_policy_version=edit_policy.policy_version,
        correction_prompt_version=CORRECTION_PROMPT_VERSION,
        correction_policy_version=correction_policy.policy_version,
        continuity_prompt_version=CONTINUITY_PROMPT_VERSION,
        continuity_schema_version=CONTINUITY_SCHEMA_VERSION,
        chapter_id=chapter.chapter_id,
        status=(
            LiteraryChapterStatus.COMPLETE_WITH_FALLBACKS
            if fallback_reasons
            else LiteraryChapterStatus.COMPLETE
        ),
        paragraph_count=len(chapter.paragraphs),
        changed_paragraph_count=len(changed_ids),
        changed_paragraph_ids=changed_ids,
        attempted_changed_paragraph_count=len(attempted),
        character_delta=len(final_text) - len(chapter.faithful_text),
        word_delta=len(final_text.split()) - len(chapter.faithful_text.split()),
        changed_percentage=round(100 * len(changed_ids) / len(chapter.paragraphs), 4),
        broad_edit_count=sum(item.diff_class is LiteraryDiffClass.BROAD for item in final_diffs),
        suspicious_rewrite_count=sum(
            item.diff_class is LiteraryDiffClass.SUSPICIOUS for item in final_diffs
        ),
        marker_identity_preserved=final_mechanics.marker_identity_preserved,
        unchanged_paragraph_hashes=unchanged_hashes,
        terminology_differences=terminology_differences(
            chapter.source_text, chapter.faithful_text, final_text, plan.registry
        ),
        edit_category_counts=dict(sorted(category_counts.items())),
        semantic_regressions_caught=len(initial_regressions),
        semantic_regressions_corrected=regressions_corrected,
        inconclusive_count=sum(
            verdict is SemanticVerdict.INCONCLUSIVE
            for verdict in execution.semantic_results.values()
        ),
        fallback_count=len(fallback_reasons),
        paragraphs=records,
        structural_report=final_mechanics.structural_report,
        alignment_report=final_mechanics.alignment_report,
        terminology_report=final_mechanics.terminology_report,
        continuity_findings=continuity.response.findings,
        provider_calls=execution.provider_calls,
        text_layers=layers,
        output_file=output_relative,
        output_sha256=text_sha256(final_text),
        created_at=timestamp,
    )
    _publish_chapter(plan, chapter, artifact, final_text, execution)
    return artifact


def _aggregate_run(
    plan: LiteraryRunPlan,
    chapters: list[LiteraryChapterArtifact],
    *,
    completed_at: datetime,
) -> LiteraryRunArtifact:
    calls = [call for chapter in chapters for call in chapter.provider_calls]
    counts = Counter(call.role.value for call in calls)
    inputs: Counter[str] = Counter()
    outputs: Counter[str] = Counter()
    latencies: Counter[str] = Counter()
    models: dict[str, set[str]] = {}
    for call in calls:
        role = call.role.value
        inputs[role] += call.input_tokens or 0
        outputs[role] += call.output_tokens or 0
        latencies[role] += call.latency_milliseconds
        if call.model_returned:
            models.setdefault(role, set()).add(call.model_returned)
    paragraph_count = sum(item.paragraph_count for item in chapters)
    changed = sum(item.changed_paragraph_count for item in chapters)
    regressions = sum(item.semantic_regressions_caught for item in chapters)
    fallbacks = sum(item.fallback_count for item in chapters)
    status = LiteraryRunStatus.COMPLETE_WITH_FALLBACKS if fallbacks else LiteraryRunStatus.COMPLETE
    return LiteraryRunArtifact(
        schema_version=SCHEMA_VERSION,
        literary_run_id=plan.literary_run_id,
        literary_engine_version=LITERARY_EDITOR_ENGINE_VERSION,
        editor_prompt_version=EDITOR_PROMPT_VERSION,
        editor_schema_version=EDITOR_SCHEMA_VERSION,
        style_contract_version=plan.style.contract_version,
        edit_policy_version=LITERARY_EDIT_POLICY_VERSION,
        correction_prompt_version=CORRECTION_PROMPT_VERSION,
        correction_policy_version=LITERARY_CORRECTION_POLICY_VERSION,
        continuity_prompt_version=CONTINUITY_PROMPT_VERSION,
        continuity_schema_version=CONTINUITY_SCHEMA_VERSION,
        blind_comparison_contract_version=BLIND_COMPARISON_CONTRACT_VERSION,
        input_translation_run_id=plan.input_run_id,
        input_snapshot_id=plan.input_snapshot_id,
        historical_translation_run_id=plan.historical_run_id,
        historical_release=plan.historical_release,
        reuse_source_literary_run_id=plan.reuse_source_literary_run_id,
        style_contract_file=Path(
            f"workspace/literary-runs/{plan.literary_run_id}/style-contract.yaml"
        ),
        style_contract_sha256=plan.style_sha256,
        book_profile_file=(
            Path(f"workspace/literary-runs/{plan.literary_run_id}/book-profile.yaml")
            if plan.profile_bytes is not None
            else None
        ),
        book_profile_sha256=plan.profile_sha256,
        provider=plan.provider,
        model_requested=plan.model_requested,
        status=status,
        chapter_ids=[item.chapter_id for item in chapters],
        chapters=chapters,
        literary_editor_calls=counts[LiteraryCallRole.EDITOR.value],
        semantic_rejudge_calls=counts[LiteraryCallRole.SEMANTIC_REJUDGE.value],
        literary_correction_calls=counts[LiteraryCallRole.CORRECTION.value],
        continuity_reviewer_calls=counts[LiteraryCallRole.CONTINUITY.value],
        input_tokens_by_role=dict(sorted(inputs.items())),
        output_tokens_by_role=dict(sorted(outputs.items())),
        latency_milliseconds_by_role=dict(sorted(latencies.items())),
        models_returned_by_role={key: sorted(value) for key, value in sorted(models.items())},
        paragraph_count=paragraph_count,
        changed_paragraph_count=changed,
        changed_paragraph_percentage=(
            round(100 * changed / paragraph_count, 4) if paragraph_count else 0.0
        ),
        semantic_regression_count=regressions,
        semantic_regression_rate=round(regressions / changed, 6) if changed else 0.0,
        fallback_count=fallbacks,
        fallback_rate=round(fallbacks / paragraph_count, 6) if paragraph_count else 0.0,
        created_at=plan.created_at,
        completed_at=completed_at,
    )


def execute_literary_run(
    plan: LiteraryRunPlan,
    *,
    literary: TransportLiteraryRoles,
    judge: TransportSemanticJudge,
    edit_policy: LiteraryEditPolicy = DEFAULT_EDIT_POLICY,
    correction_policy: LiteraryCorrectionPolicy = DEFAULT_CORRECTION_POLICY,
    now: Callable[[], datetime] | None = None,
    dry_run: bool = False,
) -> LiteraryRunResult:
    """Edit selected chapters and publish the new layer only after every safety gate passes."""
    if plan.already_complete is not None:
        blind_path = plan.destination / "blind" / "manifest.yaml"
        blind = load_yaml(blind_path, BlindComparisonArtifact) if blind_path.is_file() else None
        return LiteraryRunResult(plan.already_complete, plan.destination, blind, skipped=True)
    if literary.provider_name != plan.provider or judge.provider_name != plan.provider:
        raise LiteraryWorkflowError("all Phase 4 roles must use the selected audited provider")
    if dry_run:
        processing = LiteraryRunArtifact(
            schema_version=SCHEMA_VERSION,
            literary_run_id=plan.literary_run_id,
            literary_engine_version=LITERARY_EDITOR_ENGINE_VERSION,
            editor_prompt_version=EDITOR_PROMPT_VERSION,
            editor_schema_version=EDITOR_SCHEMA_VERSION,
            style_contract_version=plan.style.contract_version,
            edit_policy_version=edit_policy.policy_version,
            correction_prompt_version=CORRECTION_PROMPT_VERSION,
            correction_policy_version=correction_policy.policy_version,
            continuity_prompt_version=CONTINUITY_PROMPT_VERSION,
            continuity_schema_version=CONTINUITY_SCHEMA_VERSION,
            blind_comparison_contract_version=BLIND_COMPARISON_CONTRACT_VERSION,
            input_translation_run_id=plan.input_run_id,
            input_snapshot_id=plan.input_snapshot_id,
            historical_translation_run_id=plan.historical_run_id,
            historical_release=plan.historical_release,
            reuse_source_literary_run_id=plan.reuse_source_literary_run_id,
            style_contract_file=Path("style-contract.yaml"),
            style_contract_sha256=plan.style_sha256,
            book_profile_file=Path("book-profile.yaml") if plan.profile else None,
            book_profile_sha256=plan.profile_sha256,
            provider=plan.provider,
            model_requested=plan.model_requested,
            status=LiteraryRunStatus.PROCESSING,
            chapter_ids=[item.chapter_id for item in plan.chapters],
            literary_editor_calls=0,
            semantic_rejudge_calls=0,
            literary_correction_calls=0,
            continuity_reviewer_calls=0,
            paragraph_count=sum(len(item.paragraphs) for item in plan.chapters),
            changed_paragraph_count=0,
            changed_paragraph_percentage=0,
            semantic_regression_count=0,
            semantic_regression_rate=0,
            fallback_count=0,
            fallback_rate=0,
            created_at=plan.created_at,
        )
        return LiteraryRunResult(processing, None, None)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    plan.staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(plan.staging, 0o700)
    _write_private_if_same(plan.staging / "style-contract.yaml", plan.style_bytes)
    if plan.profile_bytes is not None:
        _write_private_if_same(plan.staging / "book-profile.yaml", plan.profile_bytes)
    chapter_artifacts: list[LiteraryChapterArtifact] = []
    for chapter in plan.chapters:
        complete_path = plan.staging / "chapters" / chapter.chapter_id
        if complete_path.is_dir():
            artifact = load_yaml(complete_path / "chapter.yaml", LiteraryChapterArtifact)
            _verified_text(
                complete_path / "chapter.md", artifact.output_sha256, "resumed literary chapter"
            )
        elif chapter.chapter_id in plan.reusable_chapters:
            artifact = _carry_forward_chapter(
                plan,
                chapter,
                plan.reusable_chapters[chapter.chapter_id],
                timestamp=timestamp,
            )
        else:
            artifact = _execute_chapter(
                plan,
                chapter,
                literary=literary,
                judge=judge,
                edit_policy=edit_policy,
                correction_policy=correction_policy,
                timestamp=timestamp,
            )
        chapter_artifacts.append(artifact)
    if any(item.v1_blocks is None for item in plan.chapters):
        raise LiteraryWorkflowError("blind comparison requires the historical published chapters")
    blind_inputs = []
    for planned_chapter, artifact in zip(plan.chapters, chapter_artifacts, strict=True):
        final_text = _verified_text(
            plan.staging / "chapters" / planned_chapter.chapter_id / "chapter.md",
            artifact.output_sha256,
            "literary chapter",
        )
        blind_inputs.append(
            BlindChapterInput(
                chapter_id=planned_chapter.chapter_id,
                paragraph_ids=tuple(item.paragraph_id for item in planned_chapter.paragraphs),
                v1_blocks=planned_chapter.v1_blocks or (),
                literary_blocks=source_paragraphs(final_text),
                changed_indexes=tuple(
                    item.paragraph_index - 1 for item in artifact.paragraphs if item.changed
                ),
            )
        )
    blind_root = plan.staging / "blind"
    blind_root.mkdir(exist_ok=True, mode=0o700)
    blind_build = build_blind_comparison(
        plan.literary_run_id,
        tuple(blind_inputs),
        document_file=Path(f"workspace/literary-runs/{plan.literary_run_id}/blind/comparison.md"),
        answer_key_file=Path(
            f"workspace/literary-runs/{plan.literary_run_id}/blind/answer-key.yaml"
        ),
        created_at=timestamp,
    )
    _write_private_if_same(blind_root / "comparison.md", blind_build.document.encode("utf-8"))
    _write_private_if_same(
        blind_root / "answer-key.yaml", serialize_yaml(blind_build.key).encode("utf-8")
    )
    _write_private_if_same(
        blind_root / "manifest.yaml", serialize_yaml(blind_build.artifact).encode("utf-8")
    )
    run = _aggregate_run(plan, chapter_artifacts, completed_at=timestamp)
    _write_private_if_same(plan.staging / "run.yaml", serialize_yaml(run).encode("utf-8"))
    if plan.destination.exists():
        raise LiteraryWorkflowError("literary run destination appeared during publication")
    os.replace(plan.staging, plan.destination)
    return LiteraryRunResult(run, plan.destination, blind_build.artifact)
