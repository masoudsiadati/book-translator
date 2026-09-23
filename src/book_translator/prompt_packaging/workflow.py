"""Preflight, publication, stale detection, and validation for prompt packages."""

import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.hashing import sha256_file
from book_translator.models import (
    SCHEMA_VERSION,
    PromptPackage,
    PromptPackageMessageReference,
    PromptPackageResourceReference,
    PromptPackageStatus,
    PromptPackageValidationReport,
    ReferenceContextPackage,
    SeriesTerminologyPolicy,
    SnapshotChapterReference,
    SnapshotChunkReference,
    SnapshotResourceKind,
    SnapshotResourceReference,
    TerminologyOccurrenceExemptionRegistry,
    TerminologyRegistry,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
)
from book_translator.paragraph_markers import ParagraphMarkerError, strip_markers
from book_translator.prompt_packaging.identity import (
    PROMPT_PACKAGE_RENDERER_VERSION,
    package_identity,
    uses_paragraph_markers,
)
from book_translator.prompt_packaging.rendering import (
    PromptRenderingError,
    RenderedMessages,
    estimate_tokens,
    extract_framed_bytes,
    render_messages,
)
from book_translator.prompt_packaging.templates import PromptTemplateError, validate_template
from book_translator.terminology.exemptions import resolve_occurrence_exemptions
from book_translator.terminology.registry import ExemptOccurrences, TerminologyError
from book_translator.translation_queue.locking import queue_lock
from book_translator.translation_queue.workflow import (
    QueueState,
    load_run_summary,
    load_settled_queue,
)

PACKAGE_FILES = frozenset({"package.yaml", "system.md", "user.md", "validation-report.yaml"})


class PromptPackagePurpose(StrEnum):
    """Why a package plan is being derived.

    Rendering writes into a job directory, so it additionally refuses jobs that have moved past the
    rendering stage or that already carry execution evidence. Validation only recomputes what the
    package should contain and compares it against what is stored, which stays meaningful — and
    necessary — for a job that already holds published attempts.
    """

    RENDER = "render"
    VALIDATE = "validate"


class PromptPackageError(ValueError):
    """Raised when prompt package rendering cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class PlannedPromptPackage:
    job: TranslationJob
    chapter: SnapshotChapterReference
    chunk: SnapshotChunkReference
    package_directory: Path
    action: str
    messages: RenderedMessages
    package: PromptPackage
    report: PromptPackageValidationReport


@dataclass(frozen=True, slots=True)
class PromptPackagePlan:
    project_root: Path
    run_path: Path
    run_id: str
    items: tuple[PlannedPromptPackage, ...]
    force: bool


@dataclass(frozen=True, slots=True)
class PromptPackageExecution:
    rendered_job_ids: tuple[str, ...]
    skipped_job_ids: tuple[str, ...]
    output_bytes: int


@dataclass(frozen=True, slots=True)
class ListedPromptPackage:
    job_id: str
    package: PromptPackage | None
    validation_result: str


def _load[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise PromptPackageError(f"invalid prompt package artifact {path}: {exc}") from exc


def _serialize(model: Any) -> str:
    rendered = yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise PromptPackageError("could not serialize prompt package contract")
    return rendered


def _project_relative(path: Path, project_root: Path) -> Path:
    try:
        return Path(path.resolve().relative_to(project_root.resolve()).as_posix())
    except ValueError as exc:
        raise PromptPackageError(f"prompt package path must be inside the project: {path}") from exc


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _resource_reference(
    reference: SnapshotResourceReference,
) -> PromptPackageResourceReference:
    return PromptPackageResourceReference(
        resource_kind=reference.resource_kind,
        file=reference.file,
        sha256=reference.sha256,
        version=reference.version,
    )


def _identity_payload(
    *,
    state: QueueState,
    job: TranslationJob,
    manifest: TranslationManifest,
    system_hash: str,
    user_hash: str,
    output_contract_version: str,
    reference_package: PromptPackageResourceReference | None = None,
) -> dict[str, Any]:
    def resource(reference: SnapshotResourceReference) -> dict[str, str]:
        return {
            "kind": reference.resource_kind.value,
            "sha256": reference.sha256,
            "version": reference.version,
        }

    payload: dict[str, Any] = {
        "renderer_version": PROMPT_PACKAGE_RENDERER_VERSION,
        "output_contract_version": output_contract_version,
        "snapshot_id": state.run.snapshot_id,
        "snapshot_manifest_sha256": state.run.snapshot_manifest_sha256,
        "run_id": state.run.run_id,
        "job_id": job.job_id,
        "chunk_id": job.chunk_id,
        "chunk_source_sha256": job.chunk_source_sha256,
        "context_sha256": job.context_sha256,
        "prompt": resource(manifest.prompt),
        "glossary": resource(manifest.glossary),
        "style_guide": resource(manifest.style_guide),
        "source_language": job.source_language,
        "target_language": job.target_language,
        "system_message_sha256": system_hash,
        "user_message_sha256": user_hash,
    }
    if manifest.terminology is not None:
        payload["terminology"] = resource(manifest.terminology)
    if manifest.source_anomalies is not None:
        payload["source_anomalies"] = resource(manifest.source_anomalies)
    if manifest.series_terminology is not None:
        payload["series_terminology"] = resource(manifest.series_terminology)
    if reference_package is not None:
        payload["reference_context"] = {
            "kind": reference_package.resource_kind.value,
            "sha256": reference_package.sha256,
            "version": reference_package.version,
        }
    if manifest.terminology_context_contract_version is not None:
        payload["terminology_context_contract_version"] = (
            manifest.terminology_context_contract_version
        )
    return payload


def _manifest_from_state(state: QueueState, project_root: Path) -> TranslationManifest:
    snapshot_directory = (project_root / state.run.snapshot_path).resolve()
    return _load(snapshot_directory / "manifest.yaml", TranslationManifest)


def _has_downstream_evidence(job_directory: Path) -> bool:
    """Report whether anything downstream of the package exists, which only blocks *writing* it.

    A published attempt is immutable evidence about a past provider call, not a statement about the
    package that produced it. It therefore blocks replacing the package, and never makes the stored
    package stale: staleness is decided by the package's own inputs, hashes, and frozen resources.
    """
    reserved = {"job.yaml", "prompt-package"}
    for candidate in job_directory.iterdir():
        if candidate.name in reserved or candidate.name.startswith(".prompt-package-"):
            continue
        # An attempt slot an operator explicitly retired leaves only its preserved pre-send
        # reservation marker and the immutable resolution that retired it. Neither is provider
        # evidence, so neither may block the package this job still needs to execute.
        if candidate.name == "resolutions" and candidate.is_dir():
            continue
        if candidate.name == "attempts" and candidate.is_dir():
            if not any(entry.is_dir() for entry in candidate.iterdir()):
                continue
            return True
        if candidate.is_file() or candidate.is_dir():
            return True
    return False


def _find_lineage(
    manifest: TranslationManifest, job: TranslationJob
) -> tuple[SnapshotChapterReference, SnapshotChunkReference]:
    chapter = next((item for item in manifest.chapters if item.chapter_id == job.chapter_id), None)
    if chapter is None:
        raise PromptPackageError(f"job chapter is absent from snapshot: {job.job_id}")
    chunk = next((item for item in chapter.chunks if item.chunk_id == job.chunk_id), None)
    if chunk is None:
        raise PromptPackageError(f"job chunk is absent from snapshot: {job.job_id}")
    return chapter, chunk


def _read_resource(reference: SnapshotResourceReference, project_root: Path) -> bytes:
    resource_file = reference.frozen_file or reference.file
    if resource_file.is_absolute():
        raise PromptPackageError("snapshot resource path must be project-relative")
    path = (project_root / resource_file).resolve()
    _project_relative(path, project_root)
    if not path.is_file() or sha256_file(path) != reference.sha256:
        raise PromptPackageError(
            f"snapshot resource is missing or stale: {reference.resource_kind.value}"
        )
    return path.read_bytes()


def _validate_package_directory(
    directory: Path,
    expected: PromptPackage,
    source_bytes: bytes,
    context_bytes: bytes,
) -> PromptPackageValidationReport:
    errors: list[str] = []
    try:
        actual_files = {path.name for path in directory.iterdir() if path.is_file()}
        if actual_files != PACKAGE_FILES:
            raise PromptPackageError("prompt package artifact set is incomplete or contains extras")
        package = _load(directory / "package.yaml", PromptPackage)
        persisted_report = _load(
            directory / "validation-report.yaml", PromptPackageValidationReport
        )
        system_bytes = (directory / "system.md").read_bytes()
        user_bytes = (directory / "user.md").read_bytes()
        comparable = {"created_at", "validated_at"}
        if package.model_dump(exclude=comparable) != expected.model_dump(exclude=comparable):
            errors.append("package metadata or identity is stale")
        if _sha256_bytes(system_bytes) != package.system_message.sha256:
            errors.append("system message hash mismatch")
        if _sha256_bytes(user_bytes) != package.user_message.sha256:
            errors.append("user message hash mismatch")
        user_text = user_bytes.decode("utf-8")
        framed_source = extract_framed_bytes(user_text, "source-text")
        if uses_paragraph_markers(package.output_contract_version):
            # Under the marked contract the framed section is the *marked* source, so what has to
            # be conserved is the frozen source it reduces to, not the framed bytes themselves.
            framed_source = strip_markers(framed_source.decode("utf-8")).encode("utf-8")
        if framed_source != source_bytes:
            errors.append("source conservation failed")
        if extract_framed_bytes(user_text, "previous-context") != context_bytes:
            errors.append("context conservation failed")
        if not persisted_report.valid or persisted_report.package_id != package.package_id:
            errors.append("persisted validation report mismatch")
    except (
        OSError,
        UnicodeError,
        ParagraphMarkerError,
        PromptPackageError,
        PromptRenderingError,
    ) as exc:
        errors.append(str(exc))
    return PromptPackageValidationReport(
        schema_version=SCHEMA_VERSION,
        package_id=expected.package_id,
        valid=not errors,
        checks_performed=[
            "snapshot_and_queue_lineage",
            "resource_hashes_and_versions",
            "message_hashes",
            "source_and_context_conservation",
            "template_placeholders",
            "length_framing",
            "project_relative_paths",
            "package_identity",
        ],
        snapshot_valid=True,
        queue_job_valid=True,
        resources_valid=True,
        system_hash_valid=not any("system" in error for error in errors),
        user_hash_valid=not any("user" in error for error in errors),
        source_conserved=not any("source conservation" in error for error in errors),
        context_conserved=not any("context conservation" in error for error in errors),
        placeholders_valid=True,
        delimiters_valid=not any("framed" in error or "boundary" in error for error in errors),
        paths_valid=True,
        warnings=[],
        errors=errors,
        created_at=expected.validated_at or expected.created_at,
    )


@dataclass(frozen=True, slots=True)
class _SeriesInputs:
    """The frozen series policy and the job's own frozen reference package."""

    policy: SeriesTerminologyPolicy
    package: ReferenceContextPackage
    package_reference: PromptPackageResourceReference


def _series_inputs(
    manifest: TranslationManifest, job: TranslationJob, project_root: Path
) -> _SeriesInputs | None:
    if manifest.series_terminology is None or manifest.reference_context is None:
        return None
    from book_translator.models import ReferenceContextIndex
    from book_translator.series_terminology.policy import SeriesTerminologyError, load_policy

    policy_bytes = _read_resource(manifest.series_terminology, project_root)
    index_bytes = _read_resource(manifest.reference_context, project_root)
    try:
        index = ReferenceContextIndex.model_validate(yaml.safe_load(index_bytes.decode("utf-8")))
    except (UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise PromptPackageError(f"frozen reference context index is invalid: {exc}") from exc
    entry = next((item for item in index.entries if item.chunk_id == job.chunk_id), None)
    if entry is None or entry.source_sha256 != job.chunk_source_sha256:
        raise PromptPackageError(
            f"frozen reference context does not cover job {job.job_id} for its exact source"
        )
    package_path = (project_root / entry.package_file).resolve()
    _project_relative(package_path, project_root)
    if not package_path.is_file() or sha256_file(package_path) != entry.package_sha256:
        raise PromptPackageError(f"reference package is missing or stale for job {job.job_id}")
    try:
        package = ReferenceContextPackage.model_validate(
            yaml.safe_load(package_path.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise PromptPackageError(
            f"reference package is invalid for job {job.job_id}: {exc}"
        ) from exc
    frozen_policy = manifest.series_terminology.frozen_file or manifest.series_terminology.file
    try:
        policy = load_policy(project_root / frozen_policy)
    except SeriesTerminologyError as exc:
        raise PromptPackageError(str(exc)) from exc
    if hashlib.sha256(policy_bytes).hexdigest() != manifest.series_terminology.sha256:
        raise PromptPackageError("frozen series terminology policy hash mismatch")
    return _SeriesInputs(
        policy=policy,
        package=package,
        package_reference=PromptPackageResourceReference(
            resource_kind=SnapshotResourceKind.REFERENCE_CONTEXT,
            file=entry.package_file,
            sha256=entry.package_sha256,
            version=manifest.reference_context.version,
        ),
    )


def _render_terminology_policy(
    inputs: _SeriesInputs,
    source_bytes: bytes,
    context_bytes: bytes,
    exemptions: ExemptOccurrences | None = None,
) -> str:
    from book_translator.series_terminology.context import (
        build_effective_context,
        render_effective_context,
    )

    return render_effective_context(
        build_effective_context(
            inputs.policy,
            source_text=source_bytes.decode("utf-8"),
            context_text=context_bytes.decode("utf-8"),
            reference=inputs.package,
            exemptions=exemptions,
        )
    )


def _plan_item(
    *,
    state: QueueState,
    manifest: TranslationManifest,
    job: TranslationJob,
    project_root: Path,
    timestamp: datetime,
    force: bool,
    purpose: PromptPackagePurpose = PromptPackagePurpose.RENDER,
    withhold_exemptions: bool = False,
) -> PlannedPromptPackage:
    """Derive one job's package; *withhold_exemptions* renders the counterfactual request.

    The counterfactual — the same job rendered as if the snapshot carried no occurrence
    exemptions — is never published. It exists so a boundary can prove that exemptions are the
    whole difference between a historical request and this one.
    """
    rendering = purpose is PromptPackagePurpose.RENDER
    if rendering and job.status not in {
        TranslationJobStatus.PENDING,
        TranslationJobStatus.CLAIMED,
    }:
        raise PromptPackageError(
            f"job status {job.status.value} is not eligible for prompt rendering: {job.job_id}"
        )
    chapter, chunk = _find_lineage(manifest, job)
    job_directory = state.path / "jobs" / job.chunk_id
    package_directory = job_directory / "prompt-package"
    if rendering and _has_downstream_evidence(job_directory):
        raise PromptPackageError(f"downstream execution evidence blocks package work: {job.job_id}")
    source_path = (project_root / job.chunk_source_file).resolve()
    context_path = (project_root / job.context_file).resolve()
    _project_relative(source_path, project_root)
    _project_relative(context_path, project_root)
    source_bytes = source_path.read_bytes()
    context_bytes = context_path.read_bytes()
    if _sha256_bytes(source_bytes) != job.chunk_source_sha256:
        raise PromptPackageError(f"stale chunk source for job {job.job_id}")
    if _sha256_bytes(context_bytes) != job.context_sha256:
        raise PromptPackageError(f"stale context for job {job.job_id}")
    if len(source_bytes.decode("utf-8").split()) != job.source_word_count:
        raise PromptPackageError(f"source word count mismatch for job {job.job_id}")
    if len(context_bytes.decode("utf-8").split()) != job.context_word_count:
        raise PromptPackageError(f"context word count mismatch for job {job.job_id}")

    prompt_bytes = _read_resource(manifest.prompt, project_root)
    glossary_bytes = _read_resource(manifest.glossary, project_root)
    style_bytes = _read_resource(manifest.style_guide, project_root)
    terminology_bytes = (
        _read_resource(manifest.terminology, project_root)
        if manifest.terminology is not None
        else None
    )
    anomaly_bytes = (
        _read_resource(manifest.source_anomalies, project_root)
        if manifest.source_anomalies is not None
        else None
    )
    question_bytes = (
        _read_resource(manifest.open_questions, project_root)
        if manifest.open_questions is not None
        else None
    )
    exemption_bytes = (
        _read_resource(manifest.terminology_exemptions, project_root)
        if manifest.terminology_exemptions is not None and not withhold_exemptions
        else None
    )
    series_inputs = _series_inputs(manifest, job, project_root)
    try:
        prompt_template = prompt_bytes.decode("utf-8")
        validate_template(prompt_template)
        glossary = yaml.safe_load(glossary_bytes.decode("utf-8"))
        terminology_registry = (
            TerminologyRegistry.model_validate(yaml.safe_load(terminology_bytes.decode("utf-8")))
            if terminology_bytes is not None
            else None
        )
        from book_translator.models import OpenQuestionRegistry, SourceAnomalyRegistry

        open_question_registry = (
            OpenQuestionRegistry.model_validate(yaml.safe_load(question_bytes.decode("utf-8")))
            if question_bytes is not None
            else None
        )
        source_anomaly_registry = (
            SourceAnomalyRegistry.model_validate(yaml.safe_load(anomaly_bytes.decode("utf-8")))
            if anomaly_bytes is not None
            else None
        )
        occurrence_exemptions = None
        if exemption_bytes is not None:
            if terminology_registry is None:
                raise PromptPackageError("occurrence exemptions require a frozen terminology")
            occurrence_exemptions = resolve_occurrence_exemptions(
                TerminologyOccurrenceExemptionRegistry.model_validate(
                    yaml.safe_load(exemption_bytes.decode("utf-8"))
                ),
                registry=terminology_registry,
                policy=series_inputs.policy if series_inputs is not None else None,
                chunk_id=job.chunk_id,
                source_text=source_bytes.decode("utf-8"),
            )
        messages = render_messages(
            prompt_template=prompt_template,
            glossary=glossary,
            style_guide_bytes=style_bytes,
            context_bytes=context_bytes,
            source_bytes=source_bytes,
            chapter_id=chapter.chapter_id,
            chapter_number=chapter.number,
            chapter_title=chapter.title_en,
            chunk_sequence=chunk.sequence,
            total_chunks=chapter.chunk_count,
            source_language=job.source_language,
            target_language=job.target_language,
            terminology_registry=terminology_registry,
            source_anomaly_registry=source_anomaly_registry,
            open_question_registry=open_question_registry,
            paragraph_markers=uses_paragraph_markers(state.run.output_contract_version),
            terminology_policy_text=(
                _render_terminology_policy(
                    series_inputs, source_bytes, context_bytes, occurrence_exemptions
                )
                if series_inputs is not None
                else None
            ),
            occurrence_exemptions=occurrence_exemptions,
        )
    except (
        UnicodeError,
        yaml.YAMLError,
        ValidationError,
        PromptTemplateError,
        PromptRenderingError,
        TerminologyError,
    ) as exc:
        raise PromptPackageError(f"could not render package for {job.job_id}: {exc}") from exc
    system_bytes = messages.system_text.encode("utf-8")
    user_bytes = messages.user_text.encode("utf-8")
    system_hash = _sha256_bytes(system_bytes)
    user_hash = _sha256_bytes(user_bytes)
    identity = _identity_payload(
        state=state,
        job=job,
        manifest=manifest,
        system_hash=system_hash,
        user_hash=user_hash,
        output_contract_version=messages.output_contract_version,
        reference_package=series_inputs.package_reference if series_inputs is not None else None,
    )
    if manifest.terminology_exemptions is not None:
        identity["terminology_exemptions"] = {
            "kind": manifest.terminology_exemptions.resource_kind.value,
            "sha256": manifest.terminology_exemptions.sha256,
            "version": manifest.terminology_exemptions.version,
        }
    package_id, package_hash = package_identity(identity)
    system_path = package_directory / "system.md"
    user_path = package_directory / "user.md"
    system_tokens = estimate_tokens(len(messages.system_text))
    user_tokens = estimate_tokens(len(messages.user_text))
    package = PromptPackage(
        schema_version=SCHEMA_VERSION,
        package_id=package_id,
        package_manifest_sha256=package_hash,
        renderer_version=PROMPT_PACKAGE_RENDERER_VERSION,
        run_id=state.run.run_id,
        job_id=job.job_id,
        snapshot_id=state.run.snapshot_id,
        manifest_sha256=state.run.snapshot_manifest_sha256,
        chapter_id=chapter.chapter_id,
        chunk_id=chunk.chunk_id,
        chunk_sequence=chunk.sequence,
        total_chapter_chunks=chapter.chunk_count,
        system_message=PromptPackageMessageReference(
            role="system",
            file=_project_relative(system_path, project_root),
            sha256=system_hash,
            character_count=len(messages.system_text),
            estimated_tokens=system_tokens,
        ),
        user_message=PromptPackageMessageReference(
            role="user",
            file=_project_relative(user_path, project_root),
            sha256=user_hash,
            character_count=len(messages.user_text),
            estimated_tokens=user_tokens,
        ),
        chunk_source_sha256=job.chunk_source_sha256,
        context_sha256=job.context_sha256,
        prompt=_resource_reference(manifest.prompt),
        glossary=_resource_reference(manifest.glossary),
        style_guide=_resource_reference(manifest.style_guide),
        terminology=(
            _resource_reference(manifest.terminology) if manifest.terminology is not None else None
        ),
        source_anomalies=(
            _resource_reference(manifest.source_anomalies)
            if manifest.source_anomalies is not None
            else None
        ),
        series_terminology=(
            _resource_reference(manifest.series_terminology)
            if manifest.series_terminology is not None
            else None
        ),
        reference_context=series_inputs.package_reference if series_inputs is not None else None,
        terminology_context_contract_version=manifest.terminology_context_contract_version,
        terminology_exemptions=(
            _resource_reference(manifest.terminology_exemptions)
            if manifest.terminology_exemptions is not None
            else None
        ),
        source_language=job.source_language,
        target_language=job.target_language,
        source_word_count=job.source_word_count,
        context_word_count=job.context_word_count,
        estimated_input_tokens=system_tokens + user_tokens,
        output_contract_version=messages.output_contract_version,
        status=PromptPackageStatus.VALIDATED,
        created_at=timestamp,
        validated_at=timestamp,
        warnings=["token count is a local character-based estimate, not provider billing data"],
    )
    report = PromptPackageValidationReport(
        schema_version=SCHEMA_VERSION,
        package_id=package_id,
        valid=True,
        checks_performed=[
            "snapshot_and_queue_lineage",
            "resource_hashes_and_versions",
            "message_hashes",
            "source_and_context_conservation",
            "template_placeholders",
            "length_framing",
            "project_relative_paths",
            "package_identity",
        ],
        snapshot_valid=True,
        queue_job_valid=True,
        resources_valid=True,
        system_hash_valid=True,
        user_hash_valid=True,
        source_conserved=True,
        context_conserved=True,
        placeholders_valid=True,
        delimiters_valid=True,
        paths_valid=True,
        warnings=list(package.warnings),
        errors=[],
        created_at=timestamp,
    )
    action = "render"
    safely_replaceable = False
    if package_directory.exists():
        if not package_directory.is_dir():
            action = "conflict"
        else:
            actual_files = {path.name for path in package_directory.iterdir() if path.is_file()}
            if actual_files != PACKAGE_FILES:
                action = "conflict"
                safely_replaceable = actual_files.issubset(PACKAGE_FILES)
            else:
                validation = _validate_package_directory(
                    package_directory, package, source_bytes, context_bytes
                )
                action = "skip" if validation.valid else "stale"
    if force and (action == "stale" or (action == "conflict" and safely_replaceable)):
        action = "replace"
    return PlannedPromptPackage(
        job, chapter, chunk, package_directory, action, messages, package, report
    )


def plan_prompt_packages(
    run_id_or_path: str | Path,
    *,
    project_root: Path,
    job_id: str | None = None,
    chapter_id: str | None = None,
    status: TranslationJobStatus | None = None,
    force: bool = False,
    purpose: PromptPackagePurpose = PromptPackagePurpose.RENDER,
    now: Callable[[], datetime] | None = None,
) -> PromptPackagePlan:
    project_root = project_root.resolve()
    # Settled rather than plain: validity gates every package decision, and a plain read outside
    # the queue lock can catch a concurrent execution slot mid-publication.
    state = load_settled_queue(run_id_or_path, project_root=project_root)
    if not state.validation.valid or not state.snapshot_valid:
        raise PromptPackageError("translation run or frozen snapshot is not valid")
    manifest = _manifest_from_state(state, project_root)
    selected = tuple(
        job
        for job in state.jobs
        if (job_id is None or job.job_id == job_id)
        and (chapter_id is None or job.chapter_id == chapter_id)
        and (status is None or job.status is status)
    )
    if not selected:
        raise PromptPackageError("no translation jobs match the package selection")
    timestamp = (now or (lambda: datetime.now(UTC)))()
    items = tuple(
        _plan_item(
            state=state,
            manifest=manifest,
            job=job,
            project_root=project_root,
            timestamp=timestamp,
            force=force,
            purpose=purpose,
        )
        for job in selected
    )
    return PromptPackagePlan(project_root, state.path, state.run.run_id, items, force)


def counterfactual_user_message_sha256(
    state: QueueState, job: TranslationJob, *, project_root: Path
) -> str:
    """The job's user message rendered with the snapshot's occurrence exemptions withheld."""
    project_root = project_root.resolve()
    item = _plan_item(
        state=state,
        manifest=_manifest_from_state(state, project_root),
        job=job,
        project_root=project_root,
        timestamp=datetime.now(UTC),
        force=False,
        purpose=PromptPackagePurpose.VALIDATE,
        withhold_exemptions=True,
    )
    return item.package.user_message.sha256


def _write_staging(item: PlannedPromptPackage) -> Path:
    parent = item.package_directory.parent
    staging = Path(tempfile.mkdtemp(prefix=".prompt-package-staging.", dir=parent))
    os.chmod(staging, 0o700)
    try:
        contents = {
            "system.md": item.messages.system_text,
            "user.md": item.messages.user_text,
            "validation-report.yaml": _serialize(item.report),
            "package.yaml": _serialize(item.package),
        }
        for name in ("system.md", "user.md", "validation-report.yaml", "package.yaml"):
            path = staging / name
            atomic_write_text(path, contents[name])
            os.chmod(path, 0o600)
        return staging
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _publish_replacement(item: PlannedPromptPackage, staging: Path) -> None:
    backup = item.package_directory.parent / ".prompt-package-backup"
    if backup.exists():
        raise PromptPackageError(f"unresolved prompt package backup blocks replacement: {backup}")
    os.rename(item.package_directory, backup)
    try:
        os.rename(staging, item.package_directory)
    except Exception:
        if not item.package_directory.exists():
            os.rename(backup, item.package_directory)
        raise
    shutil.rmtree(backup)


def execute_prompt_packages(plan: PromptPackagePlan) -> PromptPackageExecution:
    blockers = [item for item in plan.items if item.action in {"stale", "conflict"}]
    if blockers:
        detail = ", ".join(f"{item.job.job_id} ({item.action})" for item in blockers)
        raise PromptPackageError(f"prompt package preflight blocked: {detail}")
    completed: list[str] = []
    skipped = tuple(item.job.job_id for item in plan.items if item.action == "skip")
    output_bytes = 0
    with queue_lock(plan.run_path / ".queue.lock"):
        current = load_run_summary(plan.run_path, project_root=plan.project_root)
        current_by_id = {job.job_id: job for job in current.jobs}
        for item in plan.items:
            current_job = current_by_id.get(item.job.job_id)
            if current_job is None or current_job.status not in {
                TranslationJobStatus.PENDING,
                TranslationJobStatus.CLAIMED,
            }:
                raise PromptPackageError(
                    f"job eligibility changed after preflight: {item.job.job_id}"
                )
        for item in plan.items:
            if item.action == "skip":
                continue
            staging: Path | None = None
            try:
                staging = _write_staging(item)
                source_bytes = (plan.project_root / item.job.chunk_source_file).read_bytes()
                context_bytes = (plan.project_root / item.job.context_file).read_bytes()
                validation = _validate_package_directory(
                    staging, item.package, source_bytes, context_bytes
                )
                if not validation.valid:
                    raise PromptPackageError(
                        f"staged prompt package validation failed: {item.job.job_id}"
                    )
                if item.action == "replace":
                    _publish_replacement(item, staging)
                else:
                    os.rename(staging, item.package_directory)
                staging = None
                completed.append(item.job.job_id)
                output_bytes += sum(
                    (item.package_directory / name).stat().st_size for name in PACKAGE_FILES
                )
            except Exception as exc:
                raise PromptPackageError(
                    f"prompt package publication failed for {item.job.job_id}; completed: "
                    f"{', '.join(completed) or 'none'}; {exc}"
                ) from exc
            finally:
                if staging is not None and staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
    return PromptPackageExecution(tuple(completed), skipped, output_bytes)


def validate_prompt_package(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
) -> PromptPackageValidationReport:
    """Recompute what the job's package must contain and compare it with what is stored.

    Validation is independent of how far the job has travelled: an immutable package stays valid for
    an explicit retry as long as its snapshot lineage, frozen resources, source, context, and
    rendered messages are byte-identical to what it recorded.
    """
    try:
        plan = plan_prompt_packages(
            run_id_or_path,
            project_root=project_root,
            job_id=job_id,
            purpose=PromptPackagePurpose.VALIDATE,
        )
        item = plan.items[0]
        source_bytes = (plan.project_root / item.job.chunk_source_file).read_bytes()
        context_bytes = (plan.project_root / item.job.context_file).read_bytes()
        return _validate_package_directory(
            item.package_directory, item.package, source_bytes, context_bytes
        )
    except (PromptPackageError, OSError, ValueError) as exc:
        return PromptPackageValidationReport(
            schema_version=SCHEMA_VERSION,
            package_id="unknown",
            valid=False,
            checks_performed=[],
            snapshot_valid=False,
            queue_job_valid=False,
            resources_valid=False,
            system_hash_valid=False,
            user_hash_valid=False,
            source_conserved=False,
            context_conserved=False,
            placeholders_valid=False,
            delimiters_valid=False,
            paths_valid=False,
            warnings=[],
            errors=[str(exc)],
            created_at=datetime.now(UTC),
        )


def list_prompt_packages(
    run_id_or_path: str | Path, *, project_root: Path
) -> tuple[ListedPromptPackage, ...]:
    project_root = project_root.resolve()
    state = load_run_summary(run_id_or_path, project_root=project_root)
    manifest = _manifest_from_state(state, project_root)
    listed: list[ListedPromptPackage] = []
    for job in state.jobs:
        directory = state.path / "jobs" / job.chunk_id / "prompt-package"
        if not directory.exists():
            listed.append(ListedPromptPackage(job.job_id, None, "missing"))
            continue
        try:
            package = _load(directory / "package.yaml", PromptPackage)
            item = _plan_item(
                state=state,
                manifest=manifest,
                job=job,
                project_root=project_root,
                timestamp=package.created_at,
                force=False,
                purpose=PromptPackagePurpose.VALIDATE,
            )
            source_bytes = (project_root / job.chunk_source_file).read_bytes()
            context_bytes = (project_root / job.context_file).read_bytes()
            validation = _validate_package_directory(
                directory, item.package, source_bytes, context_bytes
            )
            listed.append(
                ListedPromptPackage(
                    job.job_id,
                    package,
                    "valid" if validation.valid else "invalid",
                )
            )
        except (OSError, UnicodeError, PromptPackageError, PromptRenderingError):
            listed.append(ListedPromptPackage(job.job_id, None, "invalid"))
    return tuple(listed)
