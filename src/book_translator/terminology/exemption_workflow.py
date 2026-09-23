"""File I/O for occurrence-specific terminology exemptions.

Two jobs live here. Recording an exemption plans one record against the book's *current* chunk,
registry, and series policy and publishes it atomically with its versioned metadata; nothing is
frozen or re-judged by that, because only a new snapshot carries an exemption to any translation.
Loading an exemption reads the *frozen* copy a snapshot pinned, verifies every hash on the way,
and resolves it for exactly one chunk's source, so each QA boundary judges a job under the
exemptions its own snapshot froze and never under a later edit.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.config import load_book_config
from book_translator.models import (
    SCHEMA_VERSION,
    OccurrenceSense,
    SeriesTerminologyPolicy,
    SnapshotResourceKind,
    TerminologyOccurrenceExemptionRegistry,
    TerminologyRegistry,
    TranslationManifest,
    VersionedResourceMetadata,
)
from book_translator.paths import ProjectPaths
from book_translator.terminology.exemptions import (
    PlannedExemption,
    TerminologyExemptionError,
    plan_occurrence_exemption,
    resolve_occurrence_exemptions,
    with_exemption,
)
from book_translator.terminology.registry import ExemptOccurrences, load_registry

INITIAL_EXEMPTION_RESOURCE_VERSION = "1.0.0"


def _verified(path: Path, expected_sha256: str, label: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TerminologyExemptionError(f"frozen {label} is unreadable: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise TerminologyExemptionError(f"frozen {label} hash mismatch")
    return data


def _load_exemption_registry(data: bytes) -> TerminologyOccurrenceExemptionRegistry:
    try:
        return TerminologyOccurrenceExemptionRegistry.model_validate(
            yaml.safe_load(data.decode("utf-8"))
        )
    except (UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise TerminologyExemptionError(f"occurrence exemptions are invalid: {exc}") from exc


def _load_policy(data: bytes) -> SeriesTerminologyPolicy:
    try:
        return SeriesTerminologyPolicy.model_validate(yaml.safe_load(data.decode("utf-8")))
    except (UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise TerminologyExemptionError(f"frozen series policy is invalid: {exc}") from exc


def frozen_occurrence_exemptions(
    manifest: TranslationManifest,
    project_root: Path,
    *,
    registry: TerminologyRegistry,
    chunk_id: str,
    source_text: str,
) -> ExemptOccurrences | None:
    """The proven exemptions a snapshot froze for one chunk, or None when it froze none there."""
    reference = manifest.terminology_exemptions
    if reference is None:
        return None
    exemptions = _load_exemption_registry(
        _verified(
            project_root / (reference.frozen_file or reference.file),
            reference.sha256,
            "occurrence exemptions",
        )
    )
    if not any(item.chunk_id == chunk_id for item in exemptions.exemptions):
        return None
    policy = None
    if manifest.series_terminology is not None:
        series = manifest.series_terminology
        policy = _load_policy(
            _verified(
                project_root / (series.frozen_file or series.file),
                series.sha256,
                "series terminology policy",
            )
        )
    return resolve_occurrence_exemptions(
        exemptions, registry=registry, policy=policy, chunk_id=chunk_id, source_text=source_text
    )


@dataclass(frozen=True, slots=True)
class ExemptionRecordPlan:
    project_root: Path
    registry_path: Path
    planned: PlannedExemption
    registry_after: TerminologyOccurrenceExemptionRegistry
    version_before: str | None
    version_after: str
    action: str

    @property
    def metadata_path(self) -> Path:
        return self.registry_path.with_suffix(".metadata.yaml")


def _serialize(model: object) -> str:
    rendered = yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=False),  # type: ignore[attr-defined]
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise TerminologyExemptionError("could not serialize occurrence exemptions")
    return rendered


def _next_minor(version: str) -> str:
    major, minor, _ = (int(part) for part in version.split("."))
    return f"{major}.{minor + 1}.0"


def plan_terminology_exemption(
    *,
    project_root: Path,
    book: str | None,
    chunk_id: str,
    spelling: str,
    occurrence_index: int,
    sense: OccurrenceSense,
    rationale: str,
    now: Callable[[], datetime] | None = None,
) -> ExemptionRecordPlan:
    """Plan one exemption against the book's current chunk, registry, and series policy."""
    project_root = project_root.resolve()
    paths = ProjectPaths(project_root, book)
    config = load_book_config(paths.book_config)
    registry_file = (
        paths.book_terminology_registry
        if config.series_terminology_policy is not None
        else (project_root / config.terminology if config.terminology is not None else None)
    )
    if registry_file is None or not registry_file.is_file():
        raise TerminologyExemptionError("the book has no terminology registry to exempt from")
    registry = load_registry(registry_file)
    policy = None
    if config.series_terminology_policy is not None:
        policy = _load_policy((project_root / config.series_terminology_policy).read_bytes())
    chapter_id, _, sequence = chunk_id.rpartition("-")
    source_path = paths.chapter_directory(chapter_id) / "chunks" / sequence / "source.md"
    if not source_path.is_file():
        raise TerminologyExemptionError(f"chunk {chunk_id} has no source in this workspace")
    planned = plan_occurrence_exemption(
        registry=registry,
        policy=policy,
        chunk_id=chunk_id,
        source_text=source_path.read_text(encoding="utf-8"),
        spelling=spelling,
        occurrence_index=occurrence_index,
        sense=sense,
        rationale=rationale,
        created_at=(now or (lambda: datetime.now(UTC)))(),
    )
    target = paths.book_terminology_exemptions
    current = _load_exemption_registry(target.read_bytes()) if target.is_file() else None
    version_before = None
    if current is not None:
        metadata = VersionedResourceMetadata.model_validate(
            yaml.safe_load(target.with_suffix(".metadata.yaml").read_text(encoding="utf-8"))
        )
        if metadata.resource_kind is not SnapshotResourceKind.TERMINOLOGY_EXEMPTIONS:
            raise TerminologyExemptionError("occurrence exemption metadata names another kind")
        version_before = metadata.version
    registry_after, added = with_exemption(current, planned.record, book_id=registry.book_id)
    if not added:
        assert version_before is not None
        return ExemptionRecordPlan(
            project_root, target, planned, registry_after, version_before, version_before, "skip"
        )
    version_after = (
        INITIAL_EXEMPTION_RESOURCE_VERSION
        if version_before is None
        else _next_minor(version_before)
    )
    return ExemptionRecordPlan(
        project_root, target, planned, registry_after, version_before, version_after, "add"
    )


def execute_terminology_exemption(plan: ExemptionRecordPlan) -> ExemptionRecordPlan:
    """Publish the registry, then its metadata, re-checking what the plan was derived from."""
    if plan.action == "skip":
        return plan
    target = plan.registry_path
    if plan.version_before is None:
        if target.exists():
            raise TerminologyExemptionError("occurrence exemptions appeared after planning")
    else:
        current = _load_exemption_registry(target.read_bytes())
        expected = [
            item
            for item in plan.registry_after.exemptions
            if item.exemption_id != plan.planned.record.exemption_id
        ]
        if current.exemptions != expected:
            raise TerminologyExemptionError("occurrence exemptions changed after planning")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, _serialize(plan.registry_after), overwrite=True)
    metadata = VersionedResourceMetadata(
        schema_version=SCHEMA_VERSION,
        resource_kind=SnapshotResourceKind.TERMINOLOGY_EXEMPTIONS,
        resource_file=Path(target.relative_to(plan.project_root).as_posix()),
        version=plan.version_after,
    )
    atomic_write_text(plan.metadata_path, _serialize(metadata), overwrite=True)
    return plan
