"""Deterministic source-anomaly identity, persistence, matching, and relevance."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.models.source_anomaly import (
    SourceAnomaly,
    SourceAnomalyCategory,
    SourceAnomalyEvidence,
    SourceAnomalyRegistry,
    SourceAnomalySeverity,
    SourceAnomalyStatus,
)


class SourceAnomalyError(ValueError):
    """Raised when an explicit registry operation is unsafe or invalid."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def anomaly_identity(
    *,
    book_id: str,
    chapter_id: str,
    page_from: int,
    page_to: int,
    source_location: str,
    category: SourceAnomalyCategory,
    original_sha256: str,
) -> str:
    payload = {
        "book_id": book_id,
        "chapter_id": chapter_id,
        "page_from": page_from,
        "page_to": page_to,
        "source_location": source_location,
        "category": category.value,
        "original_sha256": original_sha256,
    }
    return "sa-" + hashlib.sha256(_canonical(payload)).hexdigest()[:24]


def load_source_anomalies(path: Path) -> SourceAnomalyRegistry:
    try:
        return SourceAnomalyRegistry.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise SourceAnomalyError(f"invalid source anomaly registry at {path}: {exc}") from exc


def serialize_source_anomalies(registry: SourceAnomalyRegistry) -> str:
    normalized = registry.model_copy(
        update={"anomalies": sorted(registry.anomalies, key=lambda x: x.anomaly_id)}
    )
    rendered = yaml.safe_dump(
        normalized.model_dump(mode="json", exclude_none=False), allow_unicode=True, sort_keys=False
    )
    if not isinstance(rendered, str):
        raise SourceAnomalyError("could not serialize source anomaly registry")
    return rendered


def save_source_anomalies(path: Path, registry: SourceAnomalyRegistry) -> None:
    atomic_write_text(path, serialize_source_anomalies(registry), overwrite=path.exists())


def register_anomaly(
    registry: SourceAnomalyRegistry,
    *,
    book_id: str,
    chapter_id: str,
    page_from: int,
    page_to: int,
    source_location: str,
    category: SourceAnomalyCategory,
    status: SourceAnomalyStatus,
    severity: SourceAnomalySeverity,
    blocking: bool,
    original_text: str,
    source_file: Path,
    source_file_sha256: str,
    evidence: SourceAnomalyEvidence,
    operator_note: str,
    created_at: datetime,
) -> tuple[SourceAnomalyRegistry, SourceAnomaly]:
    original_sha256 = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
    anomaly_id = anomaly_identity(
        book_id=book_id,
        chapter_id=chapter_id,
        page_from=page_from,
        page_to=page_to,
        source_location=source_location,
        category=category,
        original_sha256=original_sha256,
    )
    if any(item.anomaly_id == anomaly_id for item in registry.anomalies):
        raise SourceAnomalyError("source anomaly is already registered")
    anomaly = SourceAnomaly(
        anomaly_id=anomaly_id,
        book_id=book_id,
        chapter_id=chapter_id,
        page_from=page_from,
        page_to=page_to,
        source_location=source_location,
        category=category,
        status=status,
        severity=severity,
        blocking=blocking,
        original_text=original_text,
        original_sha256=original_sha256,
        source_file=source_file,
        source_file_sha256=source_file_sha256,
        evidence=evidence,
        operator_note=operator_note,
        created_at=created_at,
    )
    return registry.model_copy(update={"anomalies": [*registry.anomalies, anomaly]}), anomaly


def relevant_anomalies(
    registry: SourceAnomalyRegistry, chapter_id: str, text: str
) -> list[SourceAnomaly]:
    return [
        item
        for item in registry.anomalies
        if item.chapter_id == chapter_id and item.original_text in text
    ]
