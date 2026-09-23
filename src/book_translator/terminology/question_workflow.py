"""File I/O for the Open Question Registry, decision corrections, and impact analysis.

Every function here is the ordered publication half of a stage: pure computation lives in
``questions.py`` and ``registry.py``.  Nothing in this module contacts a network or prints prose.
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.config import load_book_config
from book_translator.models import (
    SCHEMA_VERSION,
    DecisionSource,
    DecisionState,
    ImpactedChunk,
    ImpactedSnapshot,
    ImpactedTranslation,
    OpenQuestionRegistry,
    QuestionCategory,
    TerminologyDecisionCorrection,
    TerminologyImpactReport,
    TerminologyReadinessReport,
    TerminologyRegistry,
    TranslationJob,
    TranslationManifest,
    TranslationSnapshot,
)
from book_translator.paths import ProjectPaths
from book_translator.terminology.questions import (
    QuestionError,
    build_readiness,
    empty_questions,
    load_questions,
    questions_identity,
    save_questions,
)
from book_translator.terminology.registry import (
    TerminologyError,
    candidate_identity,
    correct_decision,
    load_registry,
    registry_identity,
    save_registry,
)

DEFAULT_QUESTIONS_FILE = Path("config/terminology-questions.yaml")


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        + b"\n"
    )


def _stable_id(prefix: str, payload: Any) -> str:
    return f"{prefix}-{hashlib.sha256(_canonical_bytes(payload)).hexdigest()[:24]}"


def _serialize(model: BaseModel) -> str:
    rendered = yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise QuestionError("could not serialize record")
    return rendered


def questions_path(project_root: Path) -> Path:
    config = load_book_config(project_root / "config/book.yaml")
    return project_root / (config.terminology_questions or DEFAULT_QUESTIONS_FILE)


def terminology_path(project_root: Path) -> Path:
    config = load_book_config(project_root / "config/book.yaml")
    return project_root / (config.terminology or Path("config/terminology.yaml"))


def load_or_empty_questions(project_root: Path) -> OpenQuestionRegistry:
    path = questions_path(project_root)
    if path.is_file():
        return load_questions(path)
    config = load_book_config(project_root / "config/book.yaml")
    return empty_questions(config.book_id)


def _metadata_path(resource: Path) -> Path:
    return resource.with_suffix(".metadata.yaml")


def publish_questions(project_root: Path, registry: OpenQuestionRegistry) -> Path:
    """Write the question registry and keep its versioned resource sidecar in step."""

    path = questions_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_questions(path, registry)
    relative = path.relative_to(project_root)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "resource_kind": "open_questions",
        "resource_file": relative.as_posix(),
        "version": registry.version,
    }
    rendered = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
    atomic_write_text(_metadata_path(path), str(rendered), overwrite=_metadata_path(path).exists())
    return path


@dataclass(frozen=True, slots=True)
class DecisionCorrectionPlan:
    """A planned, fully-derived correction; nothing is written until it is executed."""

    project_root: Path
    source_term: str
    target_term: str
    decision_source: DecisionSource
    decision_state: DecisionState
    locked: bool
    reason: str
    question_id: str | None
    registry: TerminologyRegistry
    previous: dict[str, Any]
    registry_sha256_before: str
    registry_sha256_after: str
    correction_id: str


def plan_decision_correction(
    project_root: Path,
    *,
    source_term: str,
    target_term: str,
    decision_source: DecisionSource,
    decision_state: DecisionState,
    locked: bool,
    reason: str,
    question_id: str | None = None,
) -> DecisionCorrectionPlan:
    registry = load_registry(terminology_path(project_root))
    before = registry_identity(registry)
    corrected, previous = correct_decision(
        registry,
        source_term=source_term,
        target_term=target_term,
        decision_source=decision_source,
        decision_state=decision_state,
        locked=locked,
        reason=reason,
    )
    after = registry_identity(corrected)
    correction_id = _stable_id(
        "tdc",
        {
            "candidate_id": previous["candidate_id"],
            "registry_sha256_before": before,
            "registry_sha256_after": after,
        },
    )
    return DecisionCorrectionPlan(
        project_root=project_root,
        source_term=source_term,
        target_term=target_term,
        decision_source=decision_source,
        decision_state=decision_state,
        locked=locked,
        reason=reason,
        question_id=question_id,
        registry=corrected,
        previous=previous,
        registry_sha256_before=before,
        registry_sha256_after=after,
        correction_id=correction_id,
    )


def execute_decision_correction(
    plan: DecisionCorrectionPlan, *, now: Callable[[], datetime] | None = None
) -> TerminologyDecisionCorrection:
    """Publish the correction evidence first, then the registry it explains."""

    moment = (now or (lambda: datetime.now(UTC)))()
    record = TerminologyDecisionCorrection(
        schema_version=SCHEMA_VERSION,
        correction_id=plan.correction_id,
        candidate_id=plan.previous["candidate_id"],
        decision_id=plan.previous["decision_id"],
        source_term=plan.source_term,
        previous_target_term=plan.previous["target_term"],
        previous_decision_source=plan.previous["decision_source"],
        previous_decision_state=plan.previous["decision_state"],
        previous_locked=plan.previous["locked"],
        corrected_target_term=plan.target_term,
        corrected_decision_source=plan.decision_source.value,
        corrected_decision_state=plan.decision_state.value,
        corrected_locked=plan.locked,
        reason=plan.reason,
        question_id=plan.question_id,
        registry_sha256_before=plan.registry_sha256_before,
        registry_sha256_after=plan.registry_sha256_after,
        created_at=moment,
    )
    paths = ProjectPaths(plan.project_root)
    destination = paths.terminology_decision_correction(record.correction_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(destination, _serialize(record), overwrite=destination.exists())
    save_registry(terminology_path(plan.project_root), plan.registry)
    return record


def load_corrections(project_root: Path) -> tuple[TerminologyDecisionCorrection, ...]:
    directory = ProjectPaths(project_root).terminology_decision_corrections
    if not directory.is_dir():
        return ()
    records: list[TerminologyDecisionCorrection] = []
    for path in sorted(directory.glob("tdc-*.yaml")):
        try:
            records.append(
                TerminologyDecisionCorrection.model_validate(
                    yaml.safe_load(path.read_text(encoding="utf-8"))
                )
            )
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise QuestionError(f"invalid decision correction at {path}: {exc}") from exc
    return tuple(records)


def readiness(project_root: Path) -> TerminologyReadinessReport:
    registry = load_registry(terminology_path(project_root))
    return build_readiness(registry, load_or_empty_questions(project_root))


def _load_model[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT | None:
    if not path.is_file():
        return None
    try:
        return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError):
        return None


def analyze_impact(
    project_root: Path,
    *,
    source_term: str,
    proposed_target: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> TerminologyImpactReport:
    """Report, read-only, everything a terminology change would touch.

    This never mutates a snapshot, job, or translation: its whole purpose is to let a future
    resolution be scoped before anything is rewritten.
    """

    moment = (now or (lambda: datetime.now(UTC)))()
    registry = load_registry(terminology_path(project_root))
    questions = load_or_empty_questions(project_root)
    candidate_id = candidate_identity(source_term, registry.source_language)
    candidate = next(
        (item for item in registry.candidates if item.candidate_id == candidate_id), None
    )
    if candidate is None:
        raise TerminologyError(f"no terminology candidate exists for {source_term!r}")
    decision = next(
        (item for item in registry.decisions if item.candidate_id == candidate_id), None
    )
    question = next(
        (item for item in questions.questions if item.candidate_id == candidate_id), None
    )

    pattern = re.compile(r"(?<!\w)" + re.escape(source_term) + r"(?!\w)")
    chunks: list[ImpactedChunk] = []
    chapters_root = ProjectPaths(project_root).workspace / "chapters"
    if chapters_root.is_dir():
        for chapter in sorted(chapters_root.iterdir()):
            chunk_root = chapter / "chunks"
            if not chunk_root.is_dir():
                continue
            for chunk in sorted(chunk_root.iterdir()):
                source = chunk / "source.md"
                if not source.is_file():
                    continue
                count = len(pattern.findall(source.read_text(encoding="utf-8")))
                if count:
                    chunks.append(
                        ImpactedChunk(
                            chunk_id=f"{chapter.name}-{chunk.name}",
                            chapter_id=chapter.name,
                            occurrence_count=count,
                        )
                    )
    impacted_chunk_ids = {item.chunk_id for item in chunks}

    snapshots: list[ImpactedSnapshot] = []
    snapshot_root = ProjectPaths(project_root).translation_snapshots
    if snapshot_root.is_dir():
        for directory in sorted(snapshot_root.iterdir()):
            snapshot = _load_model(directory / "snapshot.yaml", TranslationSnapshot)
            manifest = _load_model(directory / "manifest.yaml", TranslationManifest)
            if snapshot is None or manifest is None:
                continue
            frozen = manifest.terminology.sha256 if manifest.terminology else None
            froze_affected = _frozen_registry_holds(
                project_root, manifest, candidate_id, decision.target_term if decision else None
            )
            manifest_chunks = {
                chunk.chunk_id for chapter in manifest.chapters for chunk in chapter.chunks
            }
            snapshots.append(
                ImpactedSnapshot(
                    snapshot_id=snapshot.snapshot_id,
                    terminology_sha256=frozen,
                    open_questions_sha256=(
                        manifest.open_questions.sha256 if manifest.open_questions else None
                    ),
                    froze_affected_decision=froze_affected,
                    chunk_count=len(manifest_chunks & impacted_chunk_ids),
                )
            )

    translations: list[ImpactedTranslation] = []
    runs_root = ProjectPaths(project_root).translation_runs
    if runs_root.is_dir():
        for run in sorted(runs_root.iterdir()):
            jobs_root = run / "jobs"
            if not jobs_root.is_dir():
                continue
            for job_file in sorted(jobs_root.glob("*.yaml")):
                job = _load_model(job_file, TranslationJob)
                if job is None or job.chunk_id not in impacted_chunk_ids:
                    continue
                status = job.status.value
                translations.append(
                    ImpactedTranslation(
                        run_id=job.run_id,
                        job_id=job.job_id,
                        chunk_id=job.chunk_id,
                        status=status,
                        requires_reevaluation=status in {"succeeded", "accepted"},
                    )
                )

    return TerminologyImpactReport(
        schema_version=SCHEMA_VERSION,
        candidate_id=candidate_id,
        source_term=source_term,
        current_target_term=decision.target_term if decision else None,
        current_decision_state=decision.decision_state.value if decision else None,
        proposed_target_term=proposed_target,
        question_id=question.question_id if question else None,
        impacted_chunks=chunks,
        impacted_snapshots=snapshots,
        impacted_translations=translations,
        accepted_requiring_reevaluation=sum(
            1 for item in translations if item.requires_reevaluation
        ),
        generated_at=moment,
    )


def _frozen_registry_holds(
    project_root: Path,
    manifest: TranslationManifest,
    candidate_id: str,
    target_term: str | None,
) -> bool:
    """Check the snapshot's own frozen bytes rather than the live registry."""

    if manifest.terminology is None or manifest.terminology.frozen_file is None:
        return False
    frozen = project_root / manifest.terminology.frozen_file
    if not frozen.is_file():
        return False
    try:
        registry = load_registry(frozen)
    except TerminologyError:
        return False
    decision = next(
        (item for item in registry.decisions if item.candidate_id == candidate_id), None
    )
    if decision is None:
        return False
    return target_term is None or decision.target_term == target_term


def question_category(value: str) -> QuestionCategory:
    try:
        return QuestionCategory(value)
    except ValueError as exc:
        raise QuestionError(f"unknown question category {value!r}") from exc


def questions_sha256(project_root: Path) -> str:
    return questions_identity(load_or_empty_questions(project_root))
