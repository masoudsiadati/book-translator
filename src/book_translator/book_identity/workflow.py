"""Planning and atomic publication of a governed canonical book identity migration.

Only book identity moves: the `book_id` header of each current registry and any book-scoped derived
identifier that is defined as a function of it. Semantic registry content is proved unchanged by
comparing the parsed payload with identity fields removed.
"""

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.book_identity.engine import (
    BOOK_IDENTITY_MIGRATION_VERSION,
    BookIdentityRewriteError,
    ScalarRewrite,
    apply_scalar_rewrites,
    migration_identity,
    semantic_identity,
)
from book_translator.book_metadata import BookMetadataError, load_book_metadata
from book_translator.hashing import sha256_file
from book_translator.models import (
    SCHEMA_VERSION,
    BookIdentityMigration,
    DerivedIdentityChange,
    MigratedRegistry,
    OpenQuestionRegistry,
    SourceAnomalyRegistry,
    TerminologyRegistry,
)
from book_translator.paths import ProjectPaths
from book_translator.source_anomalies import anomaly_identity

BOOK_ID_KEY = "book_id"


class BookIdentityMigrationError(ValueError):
    """Raised when a book identity migration cannot be planned or published safely."""


@dataclass(frozen=True, slots=True)
class RegistryTarget:
    """One current registry whose book identity is governed by canonical book metadata."""

    file: Path
    model: type[Any]
    # Payload paths removed before semantic comparison, because they encode book identity.
    identity_paths: tuple[tuple[str, ...], ...]


REGISTRY_TARGETS: tuple[RegistryTarget, ...] = (
    RegistryTarget(
        Path("config/source-anomalies.yaml"),
        SourceAnomalyRegistry,
        (
            ("anomalies", "*", "book_id"),
            ("anomalies", "*", "anomaly_id"),
        ),
    ),
    RegistryTarget(
        Path("config/terminology-questions.yaml"), OpenQuestionRegistry, (("book_id",),)
    ),
    RegistryTarget(Path("config/terminology.yaml"), TerminologyRegistry, (("book_id",),)),
)


@dataclass(frozen=True, slots=True)
class PlannedRegistry:
    target: RegistryTarget
    before_text: str
    after_text: str
    record: MigratedRegistry

    @property
    def file(self) -> Path:
        return self.target.file


@dataclass(frozen=True, slots=True)
class BookIdentityMigrationPlan:
    project_root: Path
    migration_id: str
    previous_book_id: str
    book_id: str
    registries: tuple[PlannedRegistry, ...]
    record: BookIdentityMigration | None
    record_file: Path | None
    action: Literal["migrate", "skip"]
    warnings: tuple[str, ...]


def _load_payload(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BookIdentityMigrationError(f"invalid registry at {path}: {exc}") from exc


def _validate(target: RegistryTarget, payload: Any, path: Path) -> Any:
    try:
        return target.model.model_validate(payload)
    except ValidationError as exc:
        raise BookIdentityMigrationError(f"invalid registry contract at {path}: {exc}") from exc


def _book_ids(payload: Any, target: RegistryTarget) -> list[str]:
    if isinstance(payload, dict) and BOOK_ID_KEY in payload:
        return [str(payload[BOOK_ID_KEY])]
    if target.model is SourceAnomalyRegistry and isinstance(payload, dict):
        return [str(item[BOOK_ID_KEY]) for item in payload.get("anomalies", [])]
    raise BookIdentityMigrationError(f"no book identity found in {target.file.as_posix()}")


def _anomaly_rewrites(payload: Any, previous: str, book_id: str) -> list[DerivedIdentityChange]:
    """Re-derive each anomaly ID, which is defined as a hash that includes the book identity."""
    changes: list[DerivedIdentityChange] = []
    for index, item in enumerate(payload.get("anomalies", [])):
        if str(item[BOOK_ID_KEY]) != previous:
            continue
        anomaly = SourceAnomalyRegistry.model_validate({**payload, "anomalies": [item]}).anomalies[
            0
        ]
        derived = anomaly_identity(
            book_id=book_id,
            chapter_id=anomaly.chapter_id,
            page_from=anomaly.page_from,
            page_to=anomaly.page_to,
            source_location=anomaly.source_location,
            category=anomaly.category,
            original_sha256=anomaly.original_sha256,
        )
        if derived == anomaly.anomaly_id:
            continue
        changes.append(
            DerivedIdentityChange(
                field="anomaly_id",
                record=f"anomalies[{index}]",
                before=anomaly.anomaly_id,
                after=derived,
            )
        )
    return changes


def _plan_registry(
    target: RegistryTarget, project_root: Path, previous: str, book_id: str
) -> PlannedRegistry:
    path = project_root / target.file
    before_text = path.read_text(encoding="utf-8")
    payload = _load_payload(path)
    before_model = _validate(target, payload, path)
    occurrences = sum(1 for value in _book_ids(payload, target) if value == previous)
    if occurrences == 0:
        raise BookIdentityMigrationError(
            f"{target.file.as_posix()} does not carry the previous book identity"
        )
    derived_changes = (
        _anomaly_rewrites(payload, previous, book_id)
        if target.model is SourceAnomalyRegistry
        else []
    )
    rewrites: list[ScalarRewrite] = [ScalarRewrite(BOOK_ID_KEY, previous, book_id)]
    rewrites.extend(
        ScalarRewrite(change.field, change.before, change.after) for change in derived_changes
    )
    try:
        result = apply_scalar_rewrites(before_text, rewrites)
    except BookIdentityRewriteError as exc:
        raise BookIdentityMigrationError(f"{target.file.as_posix()}: {exc}") from exc
    if result.counts[0] != occurrences:
        raise BookIdentityMigrationError(
            f"{target.file.as_posix()}: book identity rewrite count does not match the registry"
        )
    after_payload = yaml.safe_load(result.text)
    after_model = _validate(target, after_payload, path)
    before_semantic = semantic_identity(before_model.model_dump(mode="json"), target.identity_paths)
    after_semantic = semantic_identity(after_model.model_dump(mode="json"), target.identity_paths)
    if before_semantic != after_semantic:
        raise BookIdentityMigrationError(
            f"{target.file.as_posix()}: semantic registry content would change"
        )
    if sorted(set(_book_ids(after_payload, target))) != [book_id]:
        raise BookIdentityMigrationError(
            f"{target.file.as_posix()}: book identity is inconsistent after rewriting"
        )
    return PlannedRegistry(
        target=target,
        before_text=before_text,
        after_text=result.text,
        record=MigratedRegistry(
            file=target.file,
            registry_kind=str(payload.get("registry_kind", target.model.__name__)),
            before_sha256=hashlib.sha256(before_text.encode("utf-8")).hexdigest(),
            after_sha256=hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
            semantic_sha256=before_semantic,
            book_id_occurrences=occurrences,
            changed_line_count=len(result.changed_lines),
            derived_identity_changes=derived_changes,
        ),
    )


def plan_book_identity_migration(
    *,
    project_root: Path,
    targets: Sequence[RegistryTarget] | None = None,
    now: Callable[[], datetime] | None = None,
) -> BookIdentityMigrationPlan:
    """Plan the migration of every current registry onto the canonical book identity."""
    project_root = project_root.resolve()
    paths = ProjectPaths(project_root)
    try:
        metadata = load_book_metadata(project_root)
    except BookMetadataError as exc:
        raise BookIdentityMigrationError(str(exc)) from exc
    if metadata is None:
        raise BookIdentityMigrationError(
            "canonical book metadata is required; run resolve-book-metadata --apply first"
        )
    book_id = metadata.book_id
    present = [
        target for target in (targets or REGISTRY_TARGETS) if (project_root / target.file).is_file()
    ]
    if not present:
        raise BookIdentityMigrationError("no current book-scoped registry is present")
    stale: list[RegistryTarget] = []
    previous_ids: set[str] = set()
    for target in present:
        values = set(_book_ids(_load_payload(project_root / target.file), target))
        others = values - {book_id}
        if others:
            stale.append(target)
            previous_ids |= others
    if not stale:
        return BookIdentityMigrationPlan(
            project_root=project_root,
            migration_id="",
            previous_book_id=book_id,
            book_id=book_id,
            registries=(),
            record=None,
            record_file=None,
            action="skip",
            warnings=(),
        )
    if len(previous_ids) != 1:
        raise BookIdentityMigrationError(
            "current registries disagree on the previous book identity: "
            + ", ".join(sorted(previous_ids))
        )
    previous_book_id = previous_ids.pop()
    planned = tuple(
        sorted(
            (_plan_registry(target, project_root, previous_book_id, book_id) for target in stale),
            key=lambda item: item.file,
        )
    )
    migration_id = migration_identity(
        previous_book_id=previous_book_id,
        book_id=book_id,
        registries=[(item.file.as_posix(), item.record.before_sha256) for item in planned],
    )
    timestamp = (now or (lambda: datetime.now(UTC)))()
    record = BookIdentityMigration(
        schema_version=SCHEMA_VERSION,
        migration_id=migration_id,
        engine_version=BOOK_IDENTITY_MIGRATION_VERSION,
        book_metadata_file=Path(paths.book_metadata.relative_to(project_root).as_posix()),
        book_metadata_sha256=sha256_file(paths.book_metadata),
        previous_book_id=previous_book_id,
        book_id=book_id,
        registries=[item.record for item in planned],
        semantic_content_unchanged=True,
        created_at=timestamp,
    )
    warnings = [
        f"{item.file.as_posix()}: {len(item.record.derived_identity_changes)} book-scoped derived "
        "identifier(s) are re-derived under the new book identity"
        for item in planned
        if item.record.derived_identity_changes
    ]
    return BookIdentityMigrationPlan(
        project_root=project_root,
        migration_id=migration_id,
        previous_book_id=previous_book_id,
        book_id=book_id,
        registries=planned,
        record=record,
        record_file=paths.book_identity_migration(migration_id),
        action="migrate",
        warnings=tuple(warnings),
    )


def serialize_migration(record: BookIdentityMigration) -> str:
    rendered = yaml.safe_dump(
        record.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise BookIdentityMigrationError("could not serialize the migration record")
    return rendered


def execute_book_identity_migration(plan: BookIdentityMigrationPlan) -> Path | None:
    """Publish the migration: refuse on drift, rewrite registries, then record immutable lineage."""
    if plan.action == "skip":
        return None
    if plan.record is None or plan.record_file is None:
        raise BookIdentityMigrationError("an executable migration plan requires its lineage record")
    for item in plan.registries:
        path = plan.project_root / item.file
        if sha256_file(path) != item.record.before_sha256:
            raise BookIdentityMigrationError(
                f"{item.file.as_posix()} changed after planning; replan before migrating"
            )
    serialized = serialize_migration(plan.record)
    if plan.record_file.exists() and plan.record_file.read_text(encoding="utf-8") != serialized:
        raise BookIdentityMigrationError(
            f"a different migration is already recorded at {plan.record_file}"
        )
    for item in plan.registries:
        path = plan.project_root / item.file
        atomic_write_text(path, item.after_text, overwrite=True)
        if sha256_file(path) != item.record.after_sha256:
            raise BookIdentityMigrationError(f"{item.file.as_posix()} publication failed")
    atomic_write_text(plan.record_file, serialized, overwrite=True)
    return plan.record_file


def load_migrations(project_root: Path) -> tuple[BookIdentityMigration, ...]:
    """Load every published migration record in stable identity order."""
    directory = ProjectPaths(project_root.resolve()).book_identity_migrations
    if not directory.is_dir():
        return ()
    records: list[BookIdentityMigration] = []
    for path in sorted(directory.glob("bim-*.yaml")):
        try:
            records.append(
                BookIdentityMigration.model_validate(
                    yaml.safe_load(path.read_text(encoding="utf-8"))
                )
            )
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise BookIdentityMigrationError(f"invalid migration record at {path}: {exc}") from exc
    return tuple(records)
