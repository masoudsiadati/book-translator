"""Deterministic identity, persistence, and readiness for the Open Question Registry.

This module is pure apart from the explicit load/save boundary: question identity is derived from
content, never from wall-clock time or insertion order, so the same question asked twice is the
same record.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.models import (
    SCHEMA_VERSION,
    DecisionState,
    EvidenceProvenance,
    EvidenceStatus,
    OpenQuestion,
    OpenQuestionRegistry,
    QuestionCategory,
    QuestionResearch,
    QuestionResolution,
    QuestionStatus,
    TerminologyReadinessReport,
    TerminologyRegistry,
)

QUESTION_ENGINE_VERSION = "1.0.0"


class QuestionError(ValueError):
    """Raised when the open-question registry cannot be changed safely."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        + b"\n"
    )


def question_identity(candidate_id: str, category: QuestionCategory) -> str:
    """Derive the stable identity of one question.

    Identity is ``(candidate, category)`` and deliberately excludes the question text and the
    provisional target: rewording a question or revising its working translation must not mint a
    new record, otherwise the same uncertainty accumulates duplicates across runs.
    """

    payload = {"candidate_id": candidate_id, "category": category.value}
    return f"tq-{hashlib.sha256(_canonical_bytes(payload)).hexdigest()[:24]}"


def canonical_questions_bytes(registry: OpenQuestionRegistry) -> bytes:
    payload = registry.model_dump(mode="json", exclude_none=False)
    payload["questions"] = sorted(payload["questions"], key=lambda item: item["question_id"])
    return _canonical_bytes(payload)


def questions_identity(registry: OpenQuestionRegistry) -> str:
    return hashlib.sha256(canonical_questions_bytes(registry)).hexdigest()


def empty_questions(book_id: str, *, version: str = "1.0.0") -> OpenQuestionRegistry:
    return OpenQuestionRegistry(
        schema_version=SCHEMA_VERSION, version=version, book_id=book_id, questions=[]
    )


def load_questions(path: Path) -> OpenQuestionRegistry:
    try:
        return OpenQuestionRegistry.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise QuestionError(f"invalid open question registry at {path}: {exc}") from exc


def _serialized_questions(registry: OpenQuestionRegistry) -> str:
    normalized = OpenQuestionRegistry.model_validate_json(canonical_questions_bytes(registry))
    rendered = yaml.safe_dump(
        normalized.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise QuestionError("could not serialize open question registry")
    return rendered


def save_questions(path: Path, registry: OpenQuestionRegistry) -> None:
    atomic_write_text(path, _serialized_questions(registry), overwrite=path.exists())


def upsert_question(
    registry: OpenQuestionRegistry,
    *,
    candidate_id: str,
    source_term: str,
    category: QuestionCategory,
    question: str,
    created_at: datetime,
    decision_id: str | None = None,
    provisional_target: str | None = None,
    blocking: bool = False,
    evidence_status: EvidenceStatus = EvidenceStatus.NOT_GATHERED,
    chapters: tuple[str, ...] = (),
    chunks: tuple[str, ...] = (),
    research: QuestionResearch | None = None,
    provenance: EvidenceProvenance = EvidenceProvenance.CURRENT_BOOK,
    evidence_sha256: str | None = None,
) -> OpenQuestionRegistry:
    """Create or update the single question a candidate holds for one category."""

    question_id = question_identity(candidate_id, category)
    existing = next((item for item in registry.questions if item.question_id == question_id), None)
    if existing is not None and existing.status is QuestionStatus.RESOLVED:
        raise QuestionError("a resolved question must be reopened explicitly, not overwritten")
    record = OpenQuestion(
        question_id=question_id,
        candidate_id=candidate_id,
        decision_id=decision_id,
        source_term=source_term,
        provisional_target=provisional_target,
        category=category,
        question=question,
        status=QuestionStatus.OPEN,
        blocking=blocking,
        evidence_status=evidence_status,
        chapters=sorted(set(chapters)),
        chunks=sorted(set(chunks)),
        research=research or QuestionResearch(),
        provenance=provenance,
        evidence_sha256=evidence_sha256,
        resolution=None,
        created_at=existing.created_at if existing else created_at,
        superseded_by=None,
    )
    questions = list(registry.questions)
    if existing is not None:
        questions[questions.index(existing)] = record
    else:
        questions.append(record)
    return registry.model_copy(update={"questions": questions})


def resolve_question(
    registry: OpenQuestionRegistry,
    *,
    question_id: str,
    resolution: QuestionResolution,
) -> OpenQuestionRegistry:
    """Close one open question, preserving the target it supersedes."""

    questions = list(registry.questions)
    existing = next((item for item in questions if item.question_id == question_id), None)
    if existing is None:
        raise QuestionError(f"unknown question {question_id}")
    if existing.status is not QuestionStatus.OPEN:
        raise QuestionError("only an open question may be resolved")
    record = existing.model_copy(
        update={"status": QuestionStatus.RESOLVED, "resolution": resolution}
    )
    questions[questions.index(existing)] = record
    return registry.model_copy(update={"questions": questions})


def open_questions(registry: OpenQuestionRegistry) -> tuple[OpenQuestion, ...]:
    return tuple(
        sorted(
            (item for item in registry.questions if item.status is QuestionStatus.OPEN),
            key=lambda item: item.question_id,
        )
    )


def questions_for_candidate(
    registry: OpenQuestionRegistry, candidate_id: str
) -> tuple[OpenQuestion, ...]:
    return tuple(
        sorted(
            (item for item in registry.questions if item.candidate_id == candidate_id),
            key=lambda item: item.question_id,
        )
    )


def build_readiness(
    registry: TerminologyRegistry, questions: OpenQuestionRegistry
) -> TerminologyReadinessReport:
    """Report translation readiness without ever letting a non-blocking question block it."""

    from book_translator.terminology.registry import registry_identity

    provisional = [
        item for item in registry.decisions if item.decision_state is DecisionState.PROVISIONAL
    ]
    confirmed = len(registry.decisions) - len(provisional)
    opened = open_questions(questions)
    blocking = [item for item in opened if item.blocking]
    candidates_by_id = {item.candidate_id: item for item in registry.candidates}
    questioned = {
        item.candidate_id for item in questions.questions if item.status is QuestionStatus.OPEN
    }
    orphaned = sorted(
        candidates_by_id[item.candidate_id].source_term
        for item in provisional
        if item.candidate_id not in questioned and item.candidate_id in candidates_by_id
    )
    reasons: list[str] = []
    for item in blocking:
        reasons.append(f"blocking open question {item.question_id} for {item.source_term!r}")
    for term in orphaned:
        reasons.append(f"provisional decision for {term!r} has no open question")
    return TerminologyReadinessReport(
        schema_version=SCHEMA_VERSION,
        registry_sha256=registry_identity(registry),
        questions_sha256=questions_identity(questions),
        confirmed_decisions=confirmed,
        provisional_decisions=len(provisional),
        total_decisions=len(registry.decisions),
        open_questions=len(opened),
        open_blocking_questions=len(blocking),
        open_non_blocking_questions=len(opened) - len(blocking),
        resolved_questions=sum(
            1 for item in questions.questions if item.status is QuestionStatus.RESOLVED
        ),
        superseded_questions=sum(
            1 for item in questions.questions if item.status is QuestionStatus.SUPERSEDED
        ),
        provisional_without_question=orphaned,
        ready=not reasons,
        blocking_reasons=reasons,
    )
