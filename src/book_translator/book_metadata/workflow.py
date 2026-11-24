"""Planning and atomic publication of the canonical resolved book metadata record."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.book_metadata.engine import (
    BookMetadataResolution,
    ConfiguredBookEvidence,
    OperatorAssertion,
    resolve_book_metadata,
)
from book_translator.book_metadata.evidence import collect_pdf_evidence
from book_translator.book_metadata.identity import BOOK_METADATA_ENGINE_VERSION, is_placeholder
from book_translator.config import BookConfig, ProjectDataError, load_book_config, require_pdf_path
from book_translator.hashing import sha256_file
from book_translator.models import (
    BOOK_METADATA_FIELDS,
    SCHEMA_VERSION,
    BookMetadataFieldProvenance,
    BookWorkspaceMetadata,
)
from book_translator.paths import ProjectPaths
from book_translator.pdf_inspector import PdfError

# Identity-bearing metadata whose placeholder value must never reach authoritative lineage.
IDENTITY_FIELDS: tuple[str, ...] = ("book_id", "title_en", "title_fa", "author", "series_name")


class BookMetadataError(ValueError):
    """Raised when book metadata cannot be resolved or published safely."""


@dataclass(frozen=True, slots=True)
class BookMetadataPlan:
    project_root: Path
    metadata_file: Path
    book: str | None
    metadata: BookWorkspaceMetadata
    resolution: BookMetadataResolution
    action: Literal["create", "update", "skip"]
    placeholder_fields: tuple[str, ...]
    unresolved_fields: tuple[str, ...]
    warnings: tuple[str, ...]


def serialize_book_metadata(metadata: BookWorkspaceMetadata) -> str:
    """Render the canonical record deterministically in declared field order."""
    rendered = yaml.safe_dump(
        metadata.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise BookMetadataError("could not serialize the book metadata record")
    return rendered


def load_book_metadata(
    project_root: Path, *, book: str | None = None
) -> BookWorkspaceMetadata | None:
    """Load the selected book's canonical record, or None when it has not been resolved yet."""
    try:
        path = ProjectPaths(project_root, book).book_metadata
    except ValueError as exc:
        raise BookMetadataError(str(exc)) from exc
    if not path.is_file():
        return None
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        return BookWorkspaceMetadata.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise BookMetadataError(f"invalid book metadata at {path}: {exc}") from exc


def placeholder_fields(metadata: BookWorkspaceMetadata) -> tuple[str, ...]:
    """Report identity fields that still hold unfilled project scaffolding."""
    return tuple(name for name in IDENTITY_FIELDS if is_placeholder(getattr(metadata, name)))


def _configured_evidence(config: BookConfig) -> ConfiguredBookEvidence:
    return ConfiguredBookEvidence(
        book_id=config.book_id,
        title_en=config.title_en,
        title_fa=config.title_fa,
        author=config.author,
        source_language=config.source_language,
        target_language=config.target_language,
        series_name=config.series_name,
        series_index=config.series_index,
    )


def _text(resolution: BookMetadataResolution, name: str) -> str | None:
    value = resolution.value(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BookMetadataError(f"{name} must resolve to text")
    return value


def _number(resolution: BookMetadataResolution, name: str) -> int | None:
    value = resolution.value(name)
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise BookMetadataError(f"{name} must resolve to a whole number")


def _required(resolution: BookMetadataResolution, name: str) -> str:
    value = _text(resolution, name)
    if value is None:
        raise BookMetadataError(
            f"{name} could not be resolved from local source evidence or configuration; "
            "supply it explicitly rather than guessing"
        )
    return value


def plan_book_metadata(
    *,
    project_root: Path,
    assertions: Mapping[str, OperatorAssertion] | None = None,
    now: Callable[[], datetime] | None = None,
    book: str | None = None,
) -> BookMetadataPlan:
    """Resolve book metadata from PDF evidence and configuration without writing anything.

    `book` selects the book workspace whose configuration and canonical record are used; the
    legacy root never contributes evidence to a book-scoped resolution.
    """
    project_root = project_root.resolve()
    try:
        paths = ProjectPaths(project_root, book)
    except ValueError as exc:
        raise BookMetadataError(str(exc)) from exc
    try:
        config = load_book_config(paths.book_config)
    except ProjectDataError as exc:
        raise BookMetadataError(str(exc)) from exc
    try:
        pdf_relative = Path(require_pdf_path(config).as_posix())
    except ProjectDataError as exc:
        raise BookMetadataError(str(exc)) from exc
    if pdf_relative.is_absolute():
        raise BookMetadataError("configured pdf_path must be project-relative")
    pdf_path = project_root / pdf_relative
    if not pdf_path.is_file():
        raise BookMetadataError(f"configured PDF not found: {pdf_relative.as_posix()}")
    try:
        evidence = collect_pdf_evidence(pdf_path)
    except PdfError as exc:
        raise BookMetadataError(str(exc)) from exc
    try:
        resolution = resolve_book_metadata(
            pdf=evidence, config=_configured_evidence(config), assertions=assertions
        )
    except ValueError as exc:
        raise BookMetadataError(str(exc)) from exc

    existing = load_book_metadata(project_root, book=book)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    created_at = existing.created_at if existing is not None else timestamp
    # Report the underlying gap first: book_id is derived from the title when it is not configured.
    required = {
        name: _required(resolution, name)
        for name in ("title_en", "author", "source_language", "target_language", "book_id")
    }
    try:
        metadata = BookWorkspaceMetadata(
            schema_version=SCHEMA_VERSION,
            metadata_version=BOOK_METADATA_ENGINE_VERSION,
            book_id=required["book_id"],
            source_pdf=pdf_relative,
            source_pdf_name=pdf_relative.name,
            source_pdf_sha256=sha256_file(pdf_path),
            source_pdf_page_count=evidence.page_count,
            title_en=required["title_en"],
            title_fa=_text(resolution, "title_fa") or "",
            author=required["author"],
            source_language=required["source_language"],
            target_language=required["target_language"],
            series_name=_text(resolution, "series_name"),
            series_index=_number(resolution, "series_index"),
            edition=_text(resolution, "edition"),
            publisher=_text(resolution, "publisher"),
            publication_year=_number(resolution, "publication_year"),
            isbn=_text(resolution, "isbn"),
            expected_output_format=config.output_format,
            provenance=[
                BookMetadataFieldProvenance(
                    field=name,
                    provenance=resolution.fields[name].provenance,
                    evidence=resolution.fields[name].evidence,
                )
                for name in BOOK_METADATA_FIELDS
            ],
            created_at=created_at,
            updated_at=existing.updated_at if existing is not None else timestamp,
        )
    except ValidationError as exc:
        raise BookMetadataError(f"resolved book metadata is not a valid contract: {exc}") from exc

    action: Literal["create", "update", "skip"] = "create"
    if existing is not None:
        action = "skip" if metadata == existing else "update"
        if action == "update":
            metadata = metadata.model_copy(update={"updated_at": timestamp})
    placeholders = placeholder_fields(metadata)
    warnings = list(resolution.warnings)
    warnings.extend(f"{name} is still placeholder scaffolding" for name in placeholders)
    return BookMetadataPlan(
        project_root=project_root,
        metadata_file=paths.book_metadata,
        book=book,
        metadata=metadata,
        resolution=resolution,
        action=action,
        placeholder_fields=placeholders,
        unresolved_fields=tuple(
            name
            for name in BOOK_METADATA_FIELDS
            if resolution.fields[name].provenance.value == "unresolved"
        ),
        warnings=tuple(warnings),
    )


def execute_book_metadata(plan: BookMetadataPlan) -> Path:
    """Publish the canonical record atomically; an unchanged record is never rewritten."""
    if plan.action == "skip":
        return plan.metadata_file
    serialized = serialize_book_metadata(plan.metadata)
    atomic_write_text(plan.metadata_file, serialized, overwrite=True)
    published = load_book_metadata(plan.project_root, book=plan.book)
    if published != plan.metadata:
        raise BookMetadataError("published book metadata does not match the planned record")
    return plan.metadata_file
