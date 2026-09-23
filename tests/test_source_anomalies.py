from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.config import load_book_config
from book_translator.models import (
    SourceAnomaly,
    SourceAnomalyCategory,
    SourceAnomalyEvidence,
    SourceAnomalyRegistry,
    SourceAnomalySeverity,
    SourceAnomalyStatus,
)
from book_translator.prompt_packaging.rendering import render_source_anomalies
from book_translator.source_anomalies import (
    anomaly_identity,
    load_source_anomalies,
    register_anomaly,
)
from book_translator.source_quality_audit import audit_source_quality
from book_translator.terminology.validation import validate_translation_qa


def _register(
    text: str = 'A token fra —"',
) -> tuple[SourceAnomalyRegistry, SourceAnomaly, str]:
    registry = SourceAnomalyRegistry(schema_version="1.0", registry_version="1.0.0")
    digest = hashlib.sha256(text.encode()).hexdigest()
    updated, anomaly = register_anomaly(
        registry,
        book_id="synthetic-book",
        chapter_id="008",
        page_from=10,
        page_to=11,
        source_location="page-boundary:10-11",
        category=SourceAnomalyCategory.TRUNCATED_TOKEN,
        status=SourceAnomalyStatus.CONFIRMED_SOURCE_DEFECT,
        severity=SourceAnomalySeverity.LOCAL,
        blocking=False,
        original_text=text,
        source_file=Path("workspace/chapters/008/source.md"),
        source_file_sha256="0" * 64,
        evidence=SourceAnomalyEvidence(
            rendered_pdf_checked=True,
            pdf_text_layer_matches=True,
            immutable_source_matches=True,
            extraction_at_fault=False,
            replacement_known=False,
        ),
        operator_note="synthetic evidence",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return updated, anomaly, digest


def test_anomaly_identity_ignores_mutable_operator_fields() -> None:
    registry, anomaly, digest = _register()
    assert anomaly.anomaly_id == anomaly_identity(
        book_id="synthetic-book",
        chapter_id="008",
        page_from=10,
        page_to=11,
        source_location="page-boundary:10-11",
        category=SourceAnomalyCategory.TRUNCATED_TOKEN,
        original_sha256=digest,
    )
    changed = registry.anomalies[0].model_copy(update={"operator_note": "changed"})
    assert changed.anomaly_id == anomaly.anomaly_id


def test_prompt_and_qa_receive_only_relevant_anomaly() -> None:
    registry, anomaly, _ = _register()
    rendered = render_source_anomalies(registry, "008", f"Before {anomaly.original_text} After")
    assert anomaly.anomaly_id in rendered
    assert "invent missing source text" in rendered
    assert render_source_anomalies(registry, "009", "unrelated synthetic source") == ""
    report = validate_translation_qa(
        anomaly.original_text,
        "یک بند مصنوعی",
        target_language="fa",
        source_anomaly_registry=registry,
        chapter_id="008",
    )
    assert "known_source_anomaly" in {item.code for item in report.findings}


def test_chapter_008_registry_and_audit_regression() -> None:
    root = Path(__file__).parents[1]
    before = (root / "workspace/chapters/008/source.md").read_bytes()
    registry = load_source_anomalies(root / "config/source-anomalies.yaml")
    anomaly = registry.anomalies[0]
    # The identity is book-scoped by construction, so assert the rule rather than a frozen literal.
    assert anomaly.anomaly_id == anomaly_identity(
        book_id=anomaly.book_id,
        chapter_id=anomaly.chapter_id,
        page_from=anomaly.page_from,
        page_to=anomaly.page_to,
        source_location=anomaly.source_location,
        category=anomaly.category,
        original_sha256=anomaly.original_sha256,
    )
    assert anomaly.book_id == load_book_config(root / "config/book.yaml").book_id
    assert anomaly.original_sha256 == hashlib.sha256(anomaly.original_text.encode()).hexdigest()
    finding = next(
        item
        for item in audit_source_quality(root, chapter="008").report.findings
        if item.anomaly_id == anomaly.anomaly_id
    )
    assert finding.resolution_status.value == "known_source_anomaly"
    assert not finding.blocking
    assert (root / "workspace/chapters/008/source.md").read_bytes() == before


def test_list_and_show_are_read_only() -> None:
    root = Path(__file__).parents[1]
    path = root / "config/source-anomalies.yaml"
    before = path.read_bytes()
    anomaly_id = load_source_anomalies(path).anomalies[0].anomaly_id
    runner = CliRunner()
    assert runner.invoke(app, ["list-source-anomalies"], env={"PWD": str(root)}).exit_code == 0
    assert (
        runner.invoke(app, ["show-source-anomaly", anomaly_id], env={"PWD": str(root)}).exit_code
        == 0
    )
    assert path.read_bytes() == before
