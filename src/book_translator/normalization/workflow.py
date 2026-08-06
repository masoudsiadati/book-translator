"""Chapter normalization preflight and recoverable transactional execution."""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.hashing import sha256_file
from book_translator.models import ChapterMetadata, ChapterStatus, TocKind
from book_translator.normalization.engine import NORMALIZATION_VERSION, normalize_text
from book_translator.normalization.models import NormalizationResult
from book_translator.normalization.reporting import build_report, serialize_yaml
from book_translator.status_transitions import can_transition_chapter


class ChapterNormalizationError(ValueError):
    """Raised when chapter normalization cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class NormalizationPaths:
    directory: Path
    source: Path
    metadata: Path
    cleaned: Path
    report: Path


@dataclass(frozen=True, slots=True)
class PlannedChapter:
    paths: NormalizationPaths
    metadata: ChapterMetadata
    action: str
    result: NormalizationResult
    source_sha256: str
    cleaned_sha256: str
    cleaned_word_count: int
    report_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NormalizationPlan:
    project_root: Path
    chapters: tuple[PlannedChapter, ...]
    normalization_version: str
    force: bool


@dataclass(frozen=True, slots=True)
class NormalizationExecution:
    normalized_ids: tuple[str, ...]
    skipped_ids: tuple[str, ...]
    total_bytes: int


def _load_metadata(path: Path) -> ChapterMetadata:
    try:
        return ChapterMetadata.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ChapterNormalizationError(f"invalid chapter metadata {path}: {exc}") from exc


def _paths(directory: Path) -> NormalizationPaths:
    return NormalizationPaths(
        directory,
        directory / "source.md",
        directory / "metadata.yaml",
        directory / "cleaned.md",
        directory / "normalization-report.yaml",
    )


def _chapter_number(directory: Path) -> int | None:
    return None if directory.name == "000-prologue" else int(directory.name)


def _selected_directories(project_root: Path, selector: str | None) -> list[Path]:
    root = project_root / "workspace/chapters"
    if selector is None:
        if not root.is_dir():
            raise ChapterNormalizationError(f"chapter workspace not found: {root}")
        return sorted(
            (
                path
                for path in root.iterdir()
                if path.is_dir()
                and (
                    path.name == "000-prologue"
                    or re.fullmatch(r"\d{3}", path.name)
                    and path.name != "000"
                )
            ),
            key=lambda path: (path.name != "000-prologue", path.name),
        )
    normalized = selector.strip().lower()
    if normalized in {"prologue", "0"}:
        destination = root / "000-prologue"
    elif normalized.isdigit():
        number = int(normalized)
        if not 1 <= number <= 999:
            raise ChapterNormalizationError(f"invalid chapter selector {selector!r}")
        destination = root / f"{number:03d}"
    elif re.fullmatch(r"chapter-\d+", normalized):
        number = int(normalized.removeprefix("chapter-"))
        if not 1 <= number <= 999:
            raise ChapterNormalizationError(f"invalid chapter selector {selector!r}")
        destination = root / f"{number:03d}"
    else:
        raise ChapterNormalizationError(f"invalid chapter selector {selector!r}")
    if not destination.is_dir():
        raise ChapterNormalizationError(f"selected chapter workspace not found: {destination}")
    return [destination]


def _has_downstream(paths: NormalizationPaths) -> bool:
    if not paths.directory.is_dir():
        return False
    ignored = {paths.source, paths.metadata, paths.cleaned, paths.report}
    for candidate in paths.directory.rglob("*"):
        if not candidate.is_file() or candidate in ignored:
            continue
        relative = candidate.relative_to(paths.directory)
        if (
            relative == Path("final.md")
            or "chunks" in relative.parts
            or "review" in relative.parts
            or "translations" in relative.parts
            or "translated" in relative.parts
            or candidate.name == "translation.md"
        ):
            return True
    return False


def _validate_complete_existing(
    paths: NormalizationPaths,
    metadata: ChapterMetadata,
    source_hash: str,
    expected_cleaned_hash: str,
    requested_version: str,
    expected_result: NormalizationResult,
) -> None:
    try:
        report = yaml.safe_load(paths.report.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ChapterNormalizationError(
            f"invalid normalization report {paths.report}: {exc}"
        ) from exc
    cleaned_hash = sha256_file(paths.cleaned)
    if metadata.status is not ChapterStatus.NORMALIZED:
        raise ChapterNormalizationError(f"inconsistent normalized status for {metadata.chapter_id}")
    if metadata.cleaned_sha256 != cleaned_hash:
        raise ChapterNormalizationError(f"cleaned hash integrity error for {metadata.chapter_id}")
    if not isinstance(report, dict) or report.get("cleaned_sha256") != cleaned_hash:
        raise ChapterNormalizationError(f"report hash integrity error for {metadata.chapter_id}")
    if report.get("source_sha256") != source_hash:
        raise ChapterNormalizationError(
            f"report source hash integrity error for {metadata.chapter_id}"
        )
    if report.get("chapter_id") != metadata.chapter_id:
        raise ChapterNormalizationError(f"report chapter identity error for {metadata.chapter_id}")
    if report.get("normalization_version") != metadata.normalization_version:
        raise ChapterNormalizationError(f"report version integrity error for {metadata.chapter_id}")
    if report.get("cleaned_paragraph_count") != metadata.cleaned_paragraph_count:
        raise ChapterNormalizationError(
            f"report paragraph-count integrity error for {metadata.chapter_id}"
        )
    if report.get("warnings") != metadata.warnings:
        raise ChapterNormalizationError(f"report warning integrity error for {metadata.chapter_id}")
    if (
        metadata.normalization_version == requested_version
        and cleaned_hash != expected_cleaned_hash
    ):
        raise ChapterNormalizationError(
            f"cleaned output is not deterministic for {metadata.chapter_id}"
        )
    if metadata.normalization_version == requested_version:
        expected_words = len(expected_result.cleaned_text.split())
        expected_paragraphs = len(expected_result.document.blocks)
        if (
            metadata.cleaned_word_count != expected_words
            or metadata.cleaned_paragraph_count != expected_paragraphs
        ):
            raise ChapterNormalizationError(
                f"normalized metadata count integrity error for {metadata.chapter_id}"
            )
        expected_report_fields = {
            "schema_version": "1.0",
            "passes_run": list(expected_result.passes_run),
            "source_physical_line_count": expected_result.document.source_physical_line_count,
            "cleaned_paragraph_count": expected_paragraphs,
            "page_markers_removed": expected_result.page_markers_removed,
            "page_transitions": list(expected_result.document.page_transitions),
            "wrapped_line_joins": expected_result.wrapped_line_joins,
            "hyphen_repairs": expected_result.hyphen_repairs,
            "ambiguous_blocks": expected_result.ambiguous_blocks,
            "warnings": list(expected_result.warnings),
        }
        if any(report.get(key) != value for key, value in expected_report_fields.items()):
            raise ChapterNormalizationError(
                f"normalization report counter integrity error for {metadata.chapter_id}"
            )


def _action(
    paths: NormalizationPaths,
    metadata: ChapterMetadata,
    source_hash: str,
    expected_cleaned_hash: str,
    version: str,
    result: NormalizationResult,
    *,
    force: bool,
) -> str:
    cleaned_exists, report_exists = paths.cleaned.exists(), paths.report.exists()
    if force:
        if _has_downstream(paths):
            return "conflict"
        return "replace" if cleaned_exists or report_exists else "normalize"
    if cleaned_exists != report_exists:
        return "conflict"
    if not cleaned_exists:
        return "conflict" if metadata.status is ChapterStatus.NORMALIZED else "normalize"
    _validate_complete_existing(
        paths, metadata, source_hash, expected_cleaned_hash, version, result
    )
    if metadata.normalization_version != version:
        return "stale"
    return "skip"


def plan_normalization(
    *,
    project_root: Path,
    selector: str | None = None,
    force: bool = False,
    normalization_version: str = NORMALIZATION_VERSION,
    now: Callable[[], datetime] | None = None,
) -> NormalizationPlan:
    if not normalization_version.strip():
        raise ChapterNormalizationError("normalization version must not be empty")
    timestamp = (now or (lambda: datetime.now(UTC)))()
    planned: list[PlannedChapter] = []
    for directory in _selected_directories(project_root, selector):
        paths = _paths(directory)
        if not paths.source.is_file() or not paths.metadata.is_file():
            raise ChapterNormalizationError(f"incomplete extracted chapter workspace: {directory}")
        metadata = _load_metadata(paths.metadata)
        if metadata.kind not in {TocKind.PROLOGUE, TocKind.CHAPTER}:
            raise ChapterNormalizationError(f"unsupported chapter kind for {metadata.chapter_id}")
        expected_number = _chapter_number(directory)
        if metadata.number != (0 if expected_number is None else expected_number):
            raise ChapterNormalizationError(
                f"chapter metadata path mismatch for {metadata.chapter_id}"
            )
        expected_source_file = Path(paths.source.relative_to(project_root).as_posix())
        expected_cleaned_file = Path(paths.cleaned.relative_to(project_root).as_posix())
        if (
            metadata.source_file != expected_source_file
            or metadata.cleaned_file != expected_cleaned_file
        ):
            raise ChapterNormalizationError(f"noncanonical artifact path for {metadata.chapter_id}")
        source_hash = sha256_file(paths.source)
        if metadata.source_sha256 != source_hash:
            raise ChapterNormalizationError(
                f"source hash integrity error for {metadata.chapter_id}"
            )
        source = paths.source.read_text(encoding="utf-8")
        result = normalize_text(source)
        cleaned_hash = hashlib.sha256(result.cleaned_text.encode("utf-8")).hexdigest()
        action = _action(
            paths,
            metadata,
            source_hash,
            cleaned_hash,
            normalization_version,
            result,
            force=force,
        )
        if not can_transition_chapter(
            metadata.status, ChapterStatus.NORMALIZED, explicit_retry=force
        ):
            action = "conflict"
        report = build_report(
            chapter_id=metadata.chapter_id,
            source_sha256=source_hash,
            cleaned_sha256=cleaned_hash,
            normalization_version=normalization_version,
            result=result,
            created_at=timestamp,
        )
        planned.append(
            PlannedChapter(
                paths,
                metadata,
                action,
                result,
                source_hash,
                cleaned_hash,
                len(result.cleaned_text.split()),
                report,
            )
        )
    if not planned:
        raise ChapterNormalizationError("no extracted prologue or numbered chapters found")
    return NormalizationPlan(project_root, tuple(planned), normalization_version, force)


def _serialize_metadata(metadata: ChapterMetadata) -> str:
    rendered = yaml.safe_dump(
        metadata.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise ChapterNormalizationError("could not serialize chapter metadata")
    return rendered


def execute_normalization(
    plan: NormalizationPlan, *, now: Callable[[], datetime] | None = None
) -> NormalizationExecution:
    blockers = [item for item in plan.chapters if item.action in {"conflict", "stale"}]
    if blockers:
        descriptions = ", ".join(f"{item.metadata.chapter_id} ({item.action})" for item in blockers)
        raise ChapterNormalizationError(f"normalization preflight blocked: {descriptions}")
    completed: list[str] = []
    skipped = tuple(item.metadata.chapter_id for item in plan.chapters if item.action == "skip")
    total_bytes = 0
    timestamp = (now or (lambda: datetime.now(UTC)))()
    for item in plan.chapters:
        if item.action == "skip":
            continue
        try:
            if not can_transition_chapter(
                item.metadata.status, ChapterStatus.NORMALIZED, explicit_retry=plan.force
            ):
                raise ChapterNormalizationError(
                    f"status transition {item.metadata.status.value} -> normalized is not allowed"
                )
            atomic_write_text(
                item.paths.cleaned, item.result.cleaned_text, overwrite=item.action == "replace"
            )
            if sha256_file(item.paths.cleaned) != item.cleaned_sha256:
                raise ChapterNormalizationError("persisted cleaned hash verification failed")
            atomic_write_text(
                item.paths.report,
                serialize_yaml(item.report_data),
                overwrite=item.action == "replace",
            )
            updated = item.metadata.model_copy(
                update={
                    "cleaned_file": Path(
                        item.paths.cleaned.relative_to(plan.project_root).as_posix()
                    ),
                    "cleaned_sha256": item.cleaned_sha256,
                    "cleaned_word_count": item.cleaned_word_count,
                    "cleaned_paragraph_count": len(item.result.document.blocks),
                    "normalization_version": plan.normalization_version,
                    "status": ChapterStatus.NORMALIZED,
                    "updated_at": timestamp,
                    "warnings": list(item.result.warnings),
                }
            )
            atomic_write_text(item.paths.metadata, _serialize_metadata(updated), overwrite=True)
            completed.append(item.metadata.chapter_id)
            total_bytes += len(item.result.cleaned_text.encode("utf-8"))
        except Exception as exc:
            raise ChapterNormalizationError(
                f"normalization failed for {item.metadata.chapter_id}; completed: "
                f"{', '.join(completed) or 'none'}; incomplete artifacts are retained "
                f"for diagnosis: {exc}"
            ) from exc
    return NormalizationExecution(tuple(completed), skipped, total_bytes)
