"""The only chapter-title layer that touches the filesystem or a provider.

``plan_chapter_titles`` reads one run's frozen snapshot and renders every request without sending
anything; ``execute_chapter_titles`` asks the provider and publishes the artifact. The split is the
ordinary stage convention and it matters here because ``--dry-run`` is the honest way to see
exactly what would leave the machine before any of it does.

Four rules bound what this stage may do.

**It never touches translation state.** The run is opened read-only to learn which snapshot it
froze, and that is all. No job, attempt, acceptance, QA verdict, queue counter, run status, or
snapshot byte is read for mutation or written, ever. A chapter title is metadata; the translated
prose is not this stage's business.

**Only a title and its own terminology are sent.** One provider call carries one English chapter
title plus the canonical decisions that title actually contains. No chapter prose, no context, no
other chapter's title, and nothing from a translation attempt.

**Identical titles are asked once.** Distinct source titles are translated and the answers are
reused, so two chapters with the same English title cannot receive different Persian ones. That
makes consistency a property of the artifact rather than a hope about the provider.

**Nothing is published that QA rejects.** The full provider-free QA runs over the assembled set
before a byte is written, and an artifact with any error finding is refused outright.
"""

import json
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.chapter_titles.artifact import (
    DOCUMENT_FILENAME,
    MANIFEST_FILENAME,
    PROVENANCE_FILENAME,
    sha256_bytes,
)
from book_translator.chapter_titles.engine import (
    CHAPTER_TITLE_ENGINE_VERSION,
    CHAPTER_TITLE_OUTPUT_CONTRACT,
    CHAPTER_TITLE_PROMPT_VERSION,
    ChapterTitleError,
    distinct_source_titles,
    normalize_provider_title,
    require_source_title,
    title_sha256,
)
from book_translator.chapter_titles.identity import (
    chapter_title_identity,
    title_content_address,
)
from book_translator.chapter_titles.models import (
    ChapterTitlePlan,
    ChapterTitleResult,
    PlannedTitle,
    TranslatedTitle,
)
from book_translator.chapter_titles.qa import CHAPTER_TITLE_QA_VERSION, validate_chapter_titles
from book_translator.chapter_titles.rendering import (
    SYSTEM_MESSAGE,
    enforced_decision_ids,
    render_title_request,
)
from book_translator.models import (
    SCHEMA_VERSION,
    TerminologyRegistry,
    TranslationManifest,
    TranslationRun,
)
from book_translator.models.chapter_title import (
    ChapterTitleArtifactReference,
    ChapterTitleDocument,
    ChapterTitleManifest,
    ChapterTitleOrigin,
    ChapterTitleProvenance,
    ChapterTitleProvenanceRecord,
    ChapterTitleRecord,
)
from book_translator.paths import ProjectPaths
from book_translator.provider_execution import ProviderRequest, TranslationProvider
from book_translator.provider_execution.errors import ProviderExecutionError
from book_translator.terminology import load_registry
from book_translator.translation_snapshot import validate_snapshot

#: Workspace roots a title artifact may never be written into.
PROTECTED_WORKSPACE_ROOTS = (
    "chapters",
    "translation-runs",
    "snapshots",
    "audits",
    "migrations",
    "terminology-runs",
    "terminology-corrections",
    "terminology-remediation",
    "releases",
)

DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_OUTPUT_TOKENS = 400


def _load_contract[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ChapterTitleError(f"unreadable artifact {path}: {exc}") from exc


def _verified_bytes(path: Path, expected_sha256: str, label: str) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ChapterTitleError(f"{label} is missing: {path}") from exc
    if sha256_bytes(content) != expected_sha256:
        raise ChapterTitleError(f"{label} hash mismatch: {path}")
    return content


def _resolve_run(run_id_or_path: str | Path, project_root: Path) -> tuple[TranslationRun, Path]:
    """Open one translation run read-only, purely to learn which snapshot it froze."""
    candidate = Path(run_id_or_path)
    run_path = (
        candidate.resolve()
        if candidate.is_absolute() or len(candidate.parts) > 1
        else ProjectPaths(project_root).translation_runs / candidate
    )
    run = _load_contract(run_path / "run.yaml", TranslationRun)
    if run.run_id != run_path.name:
        raise ChapterTitleError("translation run identity does not match its directory")
    return run, run_path


def _frozen_terminology(
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
    path = project_root / relative
    _verified_bytes(path, manifest.terminology.sha256, "frozen terminology registry")
    return (
        load_registry(path),
        relative,
        manifest.terminology.sha256,
        manifest.terminology.version,
    )


def _resolve_output(output: str | Path | None, artifact_id: str, project_root: Path) -> Path:
    if output is None:
        return ProjectPaths(project_root).chapter_title_artifact(artifact_id)
    candidate = Path(output)
    resolved = (candidate if candidate.is_absolute() else project_root / candidate).resolve()
    try:
        relative = Path(resolved.relative_to(project_root).as_posix())
    except ValueError as exc:
        raise ChapterTitleError(
            f"chapter-title output must live inside the project: {resolved}"
        ) from exc
    parts = relative.parts
    if len(parts) < 2:
        raise ChapterTitleError("a chapter-title artifact must not be the project root")
    if parts[0] == "workspace" and parts[1] in PROTECTED_WORKSPACE_ROOTS:
        raise ChapterTitleError(
            f"a chapter-title artifact may not be written into canonical workspace state: "
            f"{relative.as_posix()}"
        )
    return resolved


def plan_chapter_titles(
    run_id_or_path: str | Path,
    *,
    project_root: Path,
    output: str | Path | None = None,
    force: bool = False,
) -> ChapterTitlePlan:
    """Render every chapter-title request for one run's snapshot. Reads only; sends nothing."""
    project_root = project_root.resolve()
    run, _ = _resolve_run(run_id_or_path, project_root)

    validation = validate_snapshot(project_root / run.snapshot_path, project_root=project_root)
    if (
        not validation.report.valid
        or validation.snapshot is None
        or validation.manifest is None
        or validation.snapshot.snapshot_id != run.snapshot_id
        or validation.snapshot.manifest_sha256 != run.snapshot_manifest_sha256
    ):
        raise ChapterTitleError("the run's frozen translation snapshot failed validation")
    manifest = validation.manifest
    registry, terminology_file, terminology_sha256, terminology_version = _frozen_terminology(
        manifest, project_root
    )

    titles: list[PlannedTitle] = []
    for sequence, chapter in enumerate(manifest.chapters, start=1):
        title_en = require_source_title(chapter.chapter_id, chapter.title_en)
        user_message = render_title_request(
            title_en=title_en,
            registry=registry,
            target_language=manifest.book.target_language,
        )
        titles.append(
            PlannedTitle(
                chapter_id=chapter.chapter_id,
                chapter_sequence=sequence,
                kind=chapter.kind,
                number=chapter.number,
                title_en=title_en,
                recorded_title_fa=chapter.title_fa.strip(),
                enforced_decision_ids=enforced_decision_ids(registry, title_en),
                system_message=SYSTEM_MESSAGE,
                user_message=user_message,
                request_sha256=sha256_bytes((SYSTEM_MESSAGE + "\n" + user_message).encode("utf-8")),
            )
        )
    if not titles:
        raise ChapterTitleError("the frozen snapshot contains no chapters to title")

    artifact_id, digest = chapter_title_identity(
        book_id=manifest.book.book_id,
        source_language=manifest.book.source_language,
        target_language=manifest.book.target_language,
        snapshot_id=run.snapshot_id,
        snapshot_manifest_sha256=run.snapshot_manifest_sha256,
        terminology_sha256=terminology_sha256,
        chapters=tuple((item.chapter_id, item.kind, item.number, item.title_en) for item in titles),
    )
    output_path = _resolve_output(output, artifact_id, project_root)
    pending = distinct_source_titles(
        tuple(item.title_en for item in titles if not item.already_recorded)
    )
    return ChapterTitlePlan(
        artifact_id=artifact_id,
        artifact_digest=digest,
        output_path=output_path,
        run_id=run.run_id,
        snapshot_id=run.snapshot_id,
        snapshot_manifest_sha256=run.snapshot_manifest_sha256,
        book_id=manifest.book.book_id,
        source_language=manifest.book.source_language,
        target_language=manifest.book.target_language,
        terminology_file=terminology_file,
        terminology_sha256=terminology_sha256,
        terminology_version=terminology_version,
        titles=tuple(titles),
        distinct_source_titles=pending,
        planned_provider_calls=len(pending),
        destination_exists=output_path.exists(),
        overwrite=force,
    )


def _ask_provider(
    plan: ChapterTitlePlan,
    planned: PlannedTitle,
    *,
    provider: TranslationProvider,
    model: str | None,
    timeout_seconds: int,
    max_output_tokens: int,
    index: int,
) -> tuple[str, str | None]:
    """Ask one provider for one chapter title and reduce the answer to a heading."""
    request = ProviderRequest(
        request_id=f"{plan.artifact_id}-{index:03d}",
        idempotency_key=f"{plan.artifact_digest}:{title_sha256(planned.title_en)}",
        run_id=plan.run_id or plan.snapshot_id,
        job_id=f"{plan.artifact_id}__chapter-title",
        attempt_number=1,
        system_message=planned.system_message,
        user_message=planned.user_message,
        source_language=plan.source_language,
        target_language=plan.target_language,
        output_contract=CHAPTER_TITLE_OUTPUT_CONTRACT,
        model=model,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
        metadata={"artifact_id": plan.artifact_id, "stage": "chapter-titles"},
    )
    try:
        result = provider.execute(request)
    except ProviderExecutionError as exc:
        raise ChapterTitleError(
            f"provider failed on a chapter title: {exc.classification.message}"
        ) from exc
    if result.status != "completed" or result.incomplete_reason:
        raise ChapterTitleError("provider response for a chapter title was incomplete")
    outputs = tuple(text for text in result.text_outputs if text.strip())
    if len(outputs) != 1:
        raise ChapterTitleError("provider returned no single chapter title")
    return normalize_provider_title(outputs[0]), result.model


def _translate_titles(
    plan: ChapterTitlePlan,
    *,
    provider: TranslationProvider | None,
    model: str | None,
    timeout_seconds: int,
    max_output_tokens: int,
    now: Callable[[], datetime],
) -> tuple[dict[str, TranslatedTitle], int]:
    """Produce one Persian title per chapter, asking each distinct English title exactly once."""
    answers: dict[str, tuple[str, str, str | None]] = {}
    first_chapter: dict[str, str] = {}
    produced: dict[str, TranslatedTitle] = {}
    calls = 0
    for index, planned in enumerate(plan.titles, start=1):
        if planned.already_recorded:
            # The workspace already decided this one. A provider is not asked to second-guess a
            # title a person recorded.
            produced[planned.chapter_id] = TranslatedTitle(
                chapter_id=planned.chapter_id,
                title_fa=planned.recorded_title_fa,
                origin=ChapterTitleOrigin.RECORDED_TITLE,
                provider=None,
                provider_adapter_version=None,
                model=None,
                provider_response_id=None,
                translated_at=None,
                deduplicated_from_chapter_id=None,
            )
            continue
        known = answers.get(planned.title_en)
        if known is not None:
            produced[planned.chapter_id] = TranslatedTitle(
                chapter_id=planned.chapter_id,
                title_fa=known[0],
                origin=ChapterTitleOrigin.DEDUPLICATED,
                provider=known[1],
                provider_adapter_version=None,
                model=known[2],
                provider_response_id=None,
                translated_at=None,
                deduplicated_from_chapter_id=first_chapter[planned.title_en],
            )
            continue
        if provider is None:
            raise ChapterTitleError("a provider is required to translate chapter titles")
        title_fa, returned_model = _ask_provider(
            plan,
            planned,
            provider=provider,
            model=model,
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            index=index,
        )
        calls += 1
        answers[planned.title_en] = (title_fa, provider.provider_name, returned_model or model)
        first_chapter[planned.title_en] = planned.chapter_id
        produced[planned.chapter_id] = TranslatedTitle(
            chapter_id=planned.chapter_id,
            title_fa=title_fa,
            origin=ChapterTitleOrigin.PROVIDER_TRANSLATION,
            provider=provider.provider_name,
            provider_adapter_version=provider.adapter_version,
            model=returned_model or model,
            provider_response_id=None,
            translated_at=now(),
            deduplicated_from_chapter_id=None,
        )
    return produced, calls


def _serialize_yaml(model: BaseModel) -> bytes:
    rendered = yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise ChapterTitleError("could not serialize a chapter-title record")
    return rendered.encode("utf-8")


def _serialize_json(model: BaseModel) -> bytes:
    payload = model.model_dump(mode="json", exclude_none=False)
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode(
        "utf-8"
    )


def _publish(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        atomic_write_text(path, content.decode("utf-8"), overwrite=True)
        os.chmod(path, 0o600)
    except (OSError, UnicodeError) as exc:
        raise ChapterTitleError(f"could not publish chapter-title artifact {path.name}") from exc


def _assert_replaceable(destination: Path, artifact_id: str) -> None:
    manifest_path = destination / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ChapterTitleError(
            f"{destination} exists and is not a chapter-title artifact; refusing to replace it"
        )
    existing = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(existing, dict) or existing.get("artifact_id") != artifact_id:
        raise ChapterTitleError(
            f"{destination} holds a different chapter-title artifact; refusing to replace it"
        )


def execute_chapter_titles(
    plan: ChapterTitlePlan,
    *,
    project_root: Path,
    provider: TranslationProvider | None,
    model: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    now: Callable[[], datetime] | None = None,
) -> ChapterTitleResult:
    """Translate every chapter title and publish one immutable artifact."""
    project_root = project_root.resolve()
    clock = now or (lambda: datetime.now(UTC))
    destination = plan.output_path
    replaced = destination.exists()
    if replaced:
        if not plan.overwrite:
            raise ChapterTitleError(
                f"chapter-title artifact already exists: {destination}; pass --force to replace it"
            )
        _assert_replaceable(destination, plan.artifact_id)

    produced, calls = _translate_titles(
        plan,
        provider=provider,
        model=model,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
        now=clock,
    )

    records: list[ChapterTitleRecord] = []
    for planned in plan.titles:
        answer = produced.get(planned.chapter_id)
        if answer is None:  # pragma: no cover - every planned chapter is produced above
            raise ChapterTitleError(f"chapter {planned.chapter_id} received no title")
        records.append(
            ChapterTitleRecord(
                chapter_id=planned.chapter_id,
                chapter_sequence=planned.chapter_sequence,
                kind=planned.kind,
                number=planned.number,
                title_en=planned.title_en,
                title_fa=answer.title_fa,
                source_sha256=title_sha256(planned.title_en),
                translation_sha256=title_sha256(answer.title_fa),
                origin=answer.origin,
            )
        )

    registry_path = project_root / plan.terminology_file if plan.terminology_file else None
    registry = (
        load_registry(registry_path)
        if registry_path is not None
        else TerminologyRegistry(
            schema_version=SCHEMA_VERSION,
            version="1.0.0",
            book_id=plan.book_id,
            source_language=plan.source_language,
            target_language=plan.target_language,
        )
    )
    qa = validate_chapter_titles(
        expected=tuple((item.chapter_id, item.title_en) for item in plan.titles),
        records=tuple(records),
        registry=registry,
    )
    if not qa.valid:
        codes = ", ".join(sorted({item.code for item in qa.findings if item.severity == "error"}))
        raise ChapterTitleError(f"chapter-title QA refused this artifact: {codes}")

    content_sha256 = title_content_address(
        tuple((item.chapter_id, item.title_en, item.title_fa) for item in records)
    )
    document = ChapterTitleDocument(
        schema_version=SCHEMA_VERSION,
        chapter_title_engine_version=CHAPTER_TITLE_ENGINE_VERSION,
        artifact_id=plan.artifact_id,
        book_id=plan.book_id,
        source_language=plan.source_language,
        target_language=plan.target_language,
        snapshot_id=plan.snapshot_id,
        snapshot_manifest_sha256=plan.snapshot_manifest_sha256,
        terminology_file=plan.terminology_file,
        terminology_sha256=plan.terminology_sha256,
        terminology_version=plan.terminology_version,
        chapter_count=len(records),
        chapters=records,
        content_sha256=content_sha256,
    )
    created_at = clock()
    provenance = ChapterTitleProvenance(
        schema_version=SCHEMA_VERSION,
        chapter_title_engine_version=CHAPTER_TITLE_ENGINE_VERSION,
        chapter_title_prompt_version=CHAPTER_TITLE_PROMPT_VERSION,
        artifact_id=plan.artifact_id,
        book_id=plan.book_id,
        source_translation_run=plan.run_id,
        snapshot_id=plan.snapshot_id,
        snapshot_manifest_sha256=plan.snapshot_manifest_sha256,
        chapter_count=len(records),
        titles=[
            ChapterTitleProvenanceRecord(
                chapter_id=record.chapter_id,
                chapter_sequence=record.chapter_sequence,
                title_en=record.title_en,
                title_fa=record.title_fa,
                source_sha256=record.source_sha256,
                translation_sha256=record.translation_sha256,
                origin=record.origin,
                provider=produced[record.chapter_id].provider,
                provider_adapter_version=produced[record.chapter_id].provider_adapter_version,
                model=produced[record.chapter_id].model,
                provider_response_id=produced[record.chapter_id].provider_response_id,
                request_sha256=planned.request_sha256,
                snapshot_id=plan.snapshot_id,
                terminology_sha256=plan.terminology_sha256,
                terminology_version=plan.terminology_version,
                enforced_terminology_decisions=sorted(planned.enforced_decision_ids),
                qa_valid=not any(
                    finding.chapter_id == record.chapter_id and finding.severity == "error"
                    for finding in qa.findings
                ),
                qa_finding_codes=sorted(
                    {
                        finding.code
                        for finding in qa.findings
                        if finding.chapter_id == record.chapter_id
                    }
                ),
                translated_at=produced[record.chapter_id].translated_at,
                deduplicated_from_chapter_id=(
                    produced[record.chapter_id].deduplicated_from_chapter_id
                ),
            )
            for record, planned in zip(records, plan.titles, strict=True)
        ],
        qa=qa,
        created_at=created_at,
    )

    origins = [record.origin for record in records]
    document_bytes = _serialize_yaml(document)
    provenance_bytes = _serialize_json(provenance)
    payload = (
        (DOCUMENT_FILENAME, document_bytes),
        (PROVENANCE_FILENAME, provenance_bytes),
    )
    providers: dict[str, int] = {}
    models: dict[str, int] = {}
    for record in records:
        answer = produced[record.chapter_id]
        providers[answer.provider or "none"] = providers.get(answer.provider or "none", 0) + 1
        models[answer.model or "unrecorded"] = models.get(answer.model or "unrecorded", 0) + 1
    manifest = ChapterTitleManifest(
        schema_version=SCHEMA_VERSION,
        chapter_title_engine_version=CHAPTER_TITLE_ENGINE_VERSION,
        chapter_title_prompt_version=CHAPTER_TITLE_PROMPT_VERSION,
        chapter_title_qa_version=CHAPTER_TITLE_QA_VERSION,
        artifact_id=plan.artifact_id,
        book_id=plan.book_id,
        source_language=plan.source_language,
        target_language=plan.target_language,
        source_translation_run=plan.run_id,
        snapshot_id=plan.snapshot_id,
        snapshot_manifest_sha256=plan.snapshot_manifest_sha256,
        terminology_sha256=plan.terminology_sha256,
        terminology_version=plan.terminology_version,
        chapter_count=len(records),
        translated_title_count=origins.count(ChapterTitleOrigin.PROVIDER_TRANSLATION),
        deduplicated_title_count=origins.count(ChapterTitleOrigin.DEDUPLICATED),
        recorded_title_count=origins.count(ChapterTitleOrigin.RECORDED_TITLE),
        provider_calls=calls,
        providers={key: providers[key] for key in sorted(providers)},
        models={key: models[key] for key in sorted(models)},
        content_sha256=content_sha256,
        artifacts=sorted(
            (
                ChapterTitleArtifactReference(
                    file=Path(name), sha256=sha256_bytes(content), byte_count=len(content)
                )
                for name, content in payload
            ),
            key=lambda item: item.file.as_posix(),
        ),
        created_at=created_at,
    )

    staging = destination.parent / f".{plan.artifact_id}.titles.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, mode=0o700)
    try:
        # Content first, manifest last: an interruption leaves a staging directory, never a
        # published artifact indexing files it does not contain.
        for name, content in payload:
            _publish(staging / name, content)
        _publish(staging / MANIFEST_FILENAME, _serialize_yaml(manifest))
        if replaced:
            shutil.rmtree(destination)
        os.replace(staging, destination)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ChapterTitleError(
            f"could not publish chapter-title artifact {plan.artifact_id}: {exc}"
        ) from exc

    written = tuple(destination / name for name, _ in payload) + (destination / MANIFEST_FILENAME,)
    return ChapterTitleResult(
        artifact_id=plan.artifact_id,
        output_path=destination,
        records=tuple(records),
        qa=qa,
        provider_calls=calls,
        written_files=written,
        replaced=replaced,
        dry_run=False,
    )
