"""The filesystem half of a provider-free chapter-title revision.

It reads one published artifact, derives the corrected set purely, re-runs the full title QA over
the result, and publishes a second immutable artifact that names its parent. The parent is opened
read-only and is never rewritten; no provider exists in this module at all, so nothing here can
acquire the ability to ask for a new title.
"""

import os
import shutil
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from book_translator.chapter_titles.artifact import (
    DOCUMENT_FILENAME,
    MANIFEST_FILENAME,
    PROVENANCE_FILENAME,
    ChapterTitleArtifact,
    load_chapter_title_artifact,
    load_chapter_title_provenance,
    sha256_bytes,
)
from book_translator.chapter_titles.engine import (
    CHAPTER_TITLE_ENGINE_VERSION,
    CHAPTER_TITLE_PROMPT_VERSION,
    ChapterTitleError,
)
from book_translator.chapter_titles.qa import CHAPTER_TITLE_QA_VERSION, validate_chapter_titles
from book_translator.chapter_titles.revision import (
    CHAPTER_TITLE_REVISION_VERSION,
    RevisedTitles,
    TitleOverride,
    revised_titles,
)
from book_translator.chapter_titles.workflow import (
    _serialize_json,
    _serialize_yaml,
    _verified_bytes,
)
from book_translator.models import SCHEMA_VERSION, TerminologyRegistry
from book_translator.models.chapter_title import (
    ChapterTitleArtifactReference,
    ChapterTitleDocument,
    ChapterTitleManifest,
    ChapterTitleOrigin,
    ChapterTitleProvenance,
    ChapterTitleQAReport,
)
from book_translator.paths import ProjectPaths
from book_translator.terminology import load_registry

__all__ = [
    "ChapterTitleRevisionPlan",
    "ChapterTitleRevisionResult",
    "execute_chapter_title_revision",
    "plan_chapter_title_revision",
]


class ChapterTitleRevisionPlan:
    """Everything the revision would publish, derived and QA-judged, with nothing written."""

    __slots__ = ("destination", "parent", "project_root", "qa", "revised")

    def __init__(
        self,
        *,
        project_root: Path,
        parent: ChapterTitleArtifact,
        revised: RevisedTitles,
        qa: ChapterTitleQAReport,
        destination: Path,
    ) -> None:
        self.project_root = project_root
        self.parent = parent
        self.revised = revised
        self.qa = qa
        self.destination = destination

    @property
    def artifact_id(self) -> str:
        return self.revised.artifact_id

    @property
    def overrides(self) -> tuple[TitleOverride, ...]:
        return self.revised.overrides

    @property
    def unchanged_count(self) -> int:
        return self.revised.inherited_count


class ChapterTitleRevisionResult:
    """What one published revision is, in the terms a caller reports."""

    __slots__ = ("artifact_id", "content_sha256", "output_path", "plan", "written_files")

    def __init__(
        self,
        *,
        plan: ChapterTitleRevisionPlan,
        artifact_id: str,
        content_sha256: str,
        output_path: Path,
        written_files: tuple[Path, ...],
    ) -> None:
        self.plan = plan
        self.artifact_id = artifact_id
        self.content_sha256 = content_sha256
        self.output_path = output_path
        self.written_files = written_files

    @property
    def provider_calls(self) -> int:
        """Structural: this boundary has no provider code path at all."""
        return 0


def _resolve_artifact(project_root: Path, artifact_id_or_path: str | Path) -> Path:
    candidate = Path(artifact_id_or_path)
    if candidate.is_absolute() or len(candidate.parts) > 1:
        return candidate.resolve()
    try:
        return ProjectPaths(project_root).chapter_title_artifact(str(artifact_id_or_path))
    except ValueError as exc:
        raise ChapterTitleError(str(exc)) from exc


def _snapshot_terminology(parent: ChapterTitleArtifact, project_root: Path) -> TerminologyRegistry:
    """The registry the QA re-run judges against: the one the parent artifact named."""
    document = parent.document
    if document.terminology_file is None or document.terminology_sha256 is None:
        return TerminologyRegistry(
            schema_version=SCHEMA_VERSION,
            version="1.0.0",
            book_id=document.book_id,
            source_language=document.source_language,
            target_language=document.target_language,
        )
    path = project_root / document.terminology_file
    _verified_bytes(path, document.terminology_sha256, "frozen terminology registry")
    return load_registry(path)


def plan_chapter_title_revision(
    parent_artifact: str | Path,
    *,
    project_root: Path,
    overrides: Mapping[str, TitleOverride],
    snapshot_root: Path | None = None,
) -> ChapterTitleRevisionPlan:
    """Derive and judge the revision. Reads only; contacts nobody; writes nothing."""
    project_root = project_root.resolve()
    parent_path = _resolve_artifact(project_root, parent_artifact)
    parent = load_chapter_title_artifact(parent_path)
    provenance = load_chapter_title_provenance(parent_path)
    revised = revised_titles(document=parent.document, provenance=provenance, overrides=overrides)
    registry = _snapshot_terminology(parent, project_root)
    qa = validate_chapter_titles(
        expected=tuple((record.chapter_id, record.title_en) for record in parent.document.chapters),
        records=revised.records,
        registry=registry,
    )
    if not qa.valid:
        codes = ", ".join(sorted({item.code for item in qa.findings if item.severity == "error"}))
        raise ChapterTitleError(f"chapter-title QA refused this revision: {codes}")
    destination = parent_path.parent / revised.artifact_id
    if destination.exists():
        raise ChapterTitleError(f"this exact revision is already published: {destination}")
    if snapshot_root is not None:
        # The revision is only meaningful for the snapshot its parent answered; proving the
        # manifest still hashes as the parent recorded keeps a moved snapshot from being revised
        # against silently.
        _verified_bytes(
            snapshot_root / "manifest.yaml",
            parent.document.snapshot_manifest_sha256,
            "frozen snapshot manifest",
        )
    return ChapterTitleRevisionPlan(
        project_root=project_root,
        parent=parent,
        revised=revised,
        qa=qa,
        destination=destination,
    )


def execute_chapter_title_revision(
    plan: ChapterTitleRevisionPlan, *, now: Callable[[], datetime] | None = None
) -> ChapterTitleRevisionResult:
    """Publish the revision as its own immutable artifact, leaving the parent untouched."""
    created_at = (now or (lambda: datetime.now(UTC)))()
    parent = plan.parent
    revised = plan.revised
    document = ChapterTitleDocument(
        schema_version=SCHEMA_VERSION,
        chapter_title_engine_version=CHAPTER_TITLE_ENGINE_VERSION,
        artifact_id=revised.artifact_id,
        book_id=parent.document.book_id,
        source_language=parent.document.source_language,
        target_language=parent.document.target_language,
        snapshot_id=parent.document.snapshot_id,
        snapshot_manifest_sha256=parent.document.snapshot_manifest_sha256,
        terminology_file=parent.document.terminology_file,
        terminology_sha256=parent.document.terminology_sha256,
        terminology_version=parent.document.terminology_version,
        chapter_count=len(revised.records),
        chapters=list(revised.records),
        content_sha256=revised.content_sha256,
        parent_artifact_id=parent.artifact_id,
    )
    provenance = ChapterTitleProvenance(
        schema_version=SCHEMA_VERSION,
        chapter_title_engine_version=CHAPTER_TITLE_ENGINE_VERSION,
        chapter_title_prompt_version=CHAPTER_TITLE_PROMPT_VERSION,
        artifact_id=revised.artifact_id,
        book_id=parent.document.book_id,
        source_translation_run=parent.manifest.source_translation_run,
        snapshot_id=parent.document.snapshot_id,
        snapshot_manifest_sha256=parent.document.snapshot_manifest_sha256,
        chapter_count=len(revised.provenance),
        titles=list(revised.provenance),
        qa=plan.qa,
        created_at=created_at,
    )
    document_bytes = _serialize_yaml(document)
    provenance_bytes = _serialize_json(provenance)
    payload = ((DOCUMENT_FILENAME, document_bytes), (PROVENANCE_FILENAME, provenance_bytes))
    origins = [record.origin for record in revised.records]
    manifest = ChapterTitleManifest(
        schema_version=SCHEMA_VERSION,
        chapter_title_engine_version=CHAPTER_TITLE_ENGINE_VERSION,
        chapter_title_prompt_version=CHAPTER_TITLE_PROMPT_VERSION,
        chapter_title_qa_version=CHAPTER_TITLE_QA_VERSION,
        artifact_id=revised.artifact_id,
        book_id=parent.document.book_id,
        source_language=parent.document.source_language,
        target_language=parent.document.target_language,
        source_translation_run=parent.manifest.source_translation_run,
        snapshot_id=parent.document.snapshot_id,
        snapshot_manifest_sha256=parent.document.snapshot_manifest_sha256,
        terminology_sha256=parent.document.terminology_sha256,
        terminology_version=parent.document.terminology_version,
        chapter_count=len(revised.records),
        translated_title_count=0,
        deduplicated_title_count=0,
        recorded_title_count=0,
        inherited_title_count=origins.count(ChapterTitleOrigin.INHERITED),
        # Counted over the resulting set, so a chain's earlier corrections stay visible here.
        override_title_count=origins.count(ChapterTitleOrigin.OPERATOR_OVERRIDE),
        parent_artifact_id=parent.artifact_id,
        chapter_title_revision_version=CHAPTER_TITLE_REVISION_VERSION,
        provider_calls=0,
        providers={},
        models={},
        content_sha256=revised.content_sha256,
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
    destination = plan.destination
    if destination.exists():
        raise ChapterTitleError(f"this exact revision is already published: {destination}")
    staging = destination.parent / f".{revised.artifact_id}.titles.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, mode=0o700)
    try:
        # Content first, manifest last, exactly as a translated artifact publishes: an
        # interruption leaves a staging directory, never an artifact indexing files it lacks.
        for name, content in payload:
            path = staging / name
            path.write_bytes(content)
            os.chmod(path, 0o600)
        manifest_path = staging / MANIFEST_FILENAME
        manifest_path.write_bytes(_serialize_yaml(manifest))
        os.chmod(manifest_path, 0o600)
        os.replace(staging, destination)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ChapterTitleError(
            f"could not publish chapter-title revision {revised.artifact_id}: {exc}"
        ) from exc
    written = tuple(destination / name for name, _ in payload) + (destination / MANIFEST_FILENAME,)
    return ChapterTitleRevisionResult(
        plan=plan,
        artifact_id=revised.artifact_id,
        content_sha256=revised.content_sha256,
        output_path=destination,
        written_files=written,
    )
