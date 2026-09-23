import hashlib
from datetime import UTC, datetime
from pathlib import Path

import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import (
    ChapterMetadata,
    ChapterStatus,
    ChunkMetadata,
    ChunkStatus,
    TocKind,
)
from book_translator.models.source_audit import (
    AuditCategory,
    AuditConfidence,
    AuditResolutionStatus,
    AuditSeverity,
)
from book_translator.source_anomalies import load_source_anomalies
from book_translator.source_quality_audit import (
    AUDIT_ENGINE_VERSION,
    _cleaned_findings,
    _source_findings,
    audit_source_quality,
    filter_findings,
    inspect_finding,
    serialize_report,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _dump(path: Path, model: ChapterMetadata | ChunkMetadata) -> None:
    path.write_text(
        yaml.safe_dump(model.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )


def _workspace(
    tmp_path: Path,
    *,
    broken_chunk: bool = False,
    boundary: bool = False,
    resolved_boundary: bool = False,
    missing_cleaned: bool = False,
) -> Path:
    chapter = tmp_path / "workspace/chapters/001"
    chunk = chapter / "chunks/001"
    chunk.mkdir(parents=True)
    source = (
        "<!-- source-page: 1 -->\n\nChapter 1\n\nThe Quiet Test\n\n"
        + ("A synthetic token l\n\n" if boundary else "A complete synthetic paragraph.\n\n")
        + "<!-- source-page: 2 -->\n\n"
        + ("ooking onward.\n" if boundary else "Another complete synthetic paragraph.\n")
    )
    cleaned = (
        "Chapter 1\n\nThe Quiet Test\n\nA synthetic token l ooking onward.\n"
        if boundary and resolved_boundary
        else "Chapter 1\n\nThe Quiet Test\n\nA synthetic token l\n\nooking onward.\n"
        if boundary
        else "Chapter 1\n\nThe Quiet Test\n\nA complete synthetic paragraph.\n\n"
        "Another complete synthetic paragraph.\n"
    )
    chunk_source = cleaned if not broken_chunk else "Chapter 1\n"
    (chapter / "source.md").write_text(source, encoding="utf-8")
    (chapter / "cleaned.md").write_text(cleaned, encoding="utf-8")
    (chunk / "source.md").write_text(chunk_source, encoding="utf-8")
    (chunk / "context.md").write_text("", encoding="utf-8")
    metadata = ChapterMetadata(
        schema_version="1.0",
        chapter_id="001",
        kind=TocKind.CHAPTER,
        number=1,
        title_en="The Quiet Test",
        source_start_page=1,
        source_end_page=2,
        source_file=Path("workspace/chapters/001/source.md"),
        source_sha256=_hash(source),
        cleaned_file=Path("workspace/chapters/001/cleaned.md"),
        cleaned_sha256=_hash(cleaned),
        source_word_count=12,
        cleaned_word_count=12,
        source_paragraph_count=4,
        cleaned_paragraph_count=4,
        chunk_count=1,
        status=ChapterStatus.CHUNKED,
        extraction_version="1.0",
        normalization_version="1.4.0",
        chunk_engine_version="1.0.0",
        created_at=NOW,
        updated_at=NOW,
    )
    _dump(chapter / "metadata.yaml", metadata)
    normalization_report = {
        "source_sha256": _hash(source),
        "cleaned_sha256": _hash(cleaned),
        "changes": [
            {
                "source_page": 2,
                "block_index": 3,
                "change_type": "page_boundary_continuation_join",
            }
        ]
        if resolved_boundary
        else [],
    }
    (chapter / "normalization-report.yaml").write_text(
        yaml.safe_dump(normalization_report), encoding="utf-8"
    )
    chunk_metadata = ChunkMetadata(
        schema_version="1.0",
        chunk_id="001-001",
        chapter_id="001",
        sequence=1,
        source_file=Path("workspace/chapters/001/chunks/001/source.md"),
        source_sha256=_hash(chunk_source),
        context_file=Path("workspace/chapters/001/chunks/001/context.md"),
        context_sha256=_hash(""),
        translation_file=Path("workspace/chapters/001/chunks/001/translation.md"),
        source_word_count=len(chunk_source.split()),
        source_paragraph_count=1,
        status=ChunkStatus.PENDING,
        chunk_engine_version="1.0.0",
        cleaned_source_sha256=_hash(cleaned),
        created_at=NOW,
        updated_at=NOW,
    )
    _dump(chunk / "metadata.yaml", chunk_metadata)
    (chapter / "chunking-report.yaml").write_text(
        yaml.safe_dump(
            {
                "cleaned_sha256": _hash(cleaned),
                "chunk_count": 1,
                "conservation_validation": not broken_chunk,
            }
        ),
        encoding="utf-8",
    )
    if missing_cleaned:
        (chapter / "cleaned.md").unlink()
        (chapter / "normalization-report.yaml").unlink()
        for path in (chapter / "chunks").rglob("*"):
            if path.is_file():
                path.unlink()
        (chapter / "chunking-report.yaml").unlink()
    return tmp_path


def test_page_boundary_joined_token_dialogue_and_duplication_detection() -> None:
    repeated = (
        "A deliberately repeated synthetic extraction block with enough characters "
        "to qualify for duplicate detection."
    )
    source = (
        f"<!-- source-page: 1 -->\n\n{repeated}\n\n"
        '"This synthetic dialogue ends with l\n\n'
        f'<!-- source-page: 2 -->\n\nlooking onward"\n\n{repeated}\n\n'
        "couldChannel synthetic material.\n"
    )
    findings, pages, blocks = _source_findings("001", source)
    categories = {finding.category for finding in findings}
    assert pages == 2 and blocks == 5
    assert AuditCategory.PAGE_BOUNDARY_CONTINUATION in categories
    assert AuditCategory.DIALOGUE_BOUNDARY in categories
    assert AuditCategory.JOINED_TOKEN in categories
    assert AuditCategory.DUPLICATION in categories
    assert any(f.confidence is AuditConfidence.HIGH for f in findings)


def test_paragraph_heading_and_false_positive_controls() -> None:
    suspicious = "Chapter 1\n\nA synthetic clause\n\ncontinuing here.\n"
    assert {f.category for f in _cleaned_findings("001", suspicious)} == {
        AuditCategory.PARAGRAPH_BOUNDARY
    }
    controls = (
        'Chapter 1\n\nA Heading\n\n"Stop!"\n\nA valid paragraph.\n\n'
        "Wait…\n\n— Interrupted.\n\n***\n\nA short line.\n"
    )
    assert _cleaned_findings("001", controls) == []


def test_deterministic_identity_serialization_filter_and_dry_run(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    before = sorted(path.relative_to(root) for path in root.rglob("*"))
    first = audit_source_quality(root).report
    second = audit_source_quality(root).report
    after = sorted(path.relative_to(root) for path in root.rglob("*"))
    assert first == second
    assert first.identity.audit_engine_version == AUDIT_ENGINE_VERSION
    assert serialize_report(first) == serialize_report(second)
    assert '"audit_id"' in serialize_report(first, as_json=True)
    assert filter_findings(first, confidence=AuditConfidence.HIGH).findings == []
    assert before == after


def test_conservation_failure_and_stable_finding_id(tmp_path: Path) -> None:
    report = audit_source_quality(_workspace(tmp_path, broken_chunk=True)).report
    failures = [f for f in report.findings if f.category is AuditCategory.CONSERVATION]
    failure = next(item for item in failures if "concatenation" in item.message)
    assert failure.severity is AuditSeverity.ERROR
    assert failure.confidence is AuditConfidence.HIGH
    rerun = audit_source_quality(tmp_path).report
    assert failure.finding_id in {item.finding_id for item in rerun.findings}


def test_upstream_resolution_preserves_finding_and_controls_blocking(tmp_path: Path) -> None:
    resolved = audit_source_quality(
        _workspace(tmp_path / "resolved", boundary=True, resolved_boundary=True)
    ).report
    page = next(
        item
        for item in resolved.findings
        if item.category is AuditCategory.PAGE_BOUNDARY_CONTINUATION
    )
    assert page.resolution_status is AuditResolutionStatus.RESOLVED_BY_NORMALIZATION
    assert not page.blocking
    assert resolved.resolved_high_confidence_findings == 2
    assert resolved.chapters_translation_ready == 1

    unresolved = audit_source_quality(_workspace(tmp_path / "unresolved", boundary=True)).report
    same_page = next(
        item
        for item in unresolved.findings
        if item.category is AuditCategory.PAGE_BOUNDARY_CONTINUATION
    )
    assert same_page.finding_id == page.finding_id
    assert same_page.resolution_status is AuditResolutionStatus.UNRESOLVED
    assert same_page.blocking
    assert unresolved.chapters_translation_ready == 0


def test_missing_downstream_cannot_evaluate_and_readiness_blocked(tmp_path: Path) -> None:
    report = audit_source_quality(_workspace(tmp_path, boundary=True, missing_cleaned=True)).report
    upstream = [item for item in report.findings if item.category is not AuditCategory.CONSERVATION]
    assert upstream
    assert all(
        item.resolution_status is AuditResolutionStatus.CANNOT_EVALUATE and not item.blocking
        for item in upstream
    )
    assert report.blocking_findings == 1
    assert report.chapters_missing_normalization == 1
    assert report.chapters_translation_ready == 0


def test_finding_inspection_is_concise(tmp_path: Path) -> None:
    root = _workspace(tmp_path, boundary=True, resolved_boundary=True)
    finding = next(
        item
        for item in audit_source_quality(root).report.findings
        if item.category is AuditCategory.PAGE_BOUNDARY_CONTINUATION
    )
    inspection = inspect_finding(root, finding.finding_id)
    assert inspection.source_left and len(inspection.source_left) <= 97
    assert inspection.source_right and len(inspection.source_right) <= 97
    assert "joined" in inspection.normalized_representation


def test_real_chapter_020_possessive_boundary_is_resolved() -> None:
    project_root = Path(__file__).resolve().parents[1]
    report = audit_source_quality(project_root, chapter="020").report
    finding = next(
        item for item in report.findings if item.finding_id == "sqf-b4037619b65becb2531b0cbc"
    )
    assert finding.resolution_status is AuditResolutionStatus.RESOLVED_BY_NORMALIZATION
    assert not finding.blocking
    assert report.chapters_translation_ready == 1


def test_reviewed_speaker_interruptions_are_not_high_confidence_continuations() -> None:
    project_root = Path(__file__).resolve().parents[1]
    reviewed = (
        ("002", 86, 87),
        ("015", 266, 267),
        ("039", 576, 577),
        ("039", 577, 578),
        ("043", 649, 650),
    )
    for chapter_id, page_from, page_to in reviewed:
        report = audit_source_quality(project_root, chapter=chapter_id).report
        assert not any(
            finding.page_from == page_from
            and finding.page_to == page_to
            and finding.confidence is AuditConfidence.HIGH
            and finding.category
            in {AuditCategory.PAGE_BOUNDARY_CONTINUATION, AuditCategory.SPLIT_TOKEN}
            for finding in report.findings
        )


def test_real_chapter_008_keeps_speaker_boundary_and_truncation_signal() -> None:
    project_root = Path(__file__).resolve().parents[1]
    report = audit_source_quality(project_root, chapter="008").report
    boundary = [
        finding
        for finding in report.findings
        if finding.page_from == 179 and finding.page_to == 180
    ]
    assert not any(
        finding.category is AuditCategory.PAGE_BOUNDARY_CONTINUATION for finding in boundary
    )
    split = next(finding for finding in boundary if finding.category is AuditCategory.SPLIT_TOKEN)
    assert split.confidence is AuditConfidence.HIGH
    assert split.source_layer.value == "manual_source_review"
    assert split.resolution_status.value == "known_source_anomaly"
    registered = load_source_anomalies(project_root / "config/source-anomalies.yaml").anomalies[0]
    assert split.anomaly_id == registered.anomaly_id
    assert not split.blocking


def test_cli_filters_json_and_fail_on_without_persistence(tmp_path: Path) -> None:
    _workspace(tmp_path, broken_chunk=True)
    runner = CliRunner()
    result = runner.invoke(app, ["audit-source-quality", "--json"], env={"PWD": str(tmp_path)})
    # CliRunner isolation does not change Path.cwd(), so invoke from an isolated current directory.
    old = Path.cwd()
    try:
        import os

        os.chdir(tmp_path)
        result = runner.invoke(
            app, ["audit-source-quality", "--category", "conservation", "--blocking-only"]
        )
        failed = runner.invoke(app, ["audit-source-quality", "--fail-on", "high"])
        finding_id = audit_source_quality(tmp_path).report.findings[0].finding_id
        shown = runner.invoke(app, ["show-source-quality-finding", finding_id])
    finally:
        os.chdir(old)
    assert result.exit_code == 0 and "conservation" in result.stdout
    assert failed.exit_code == 2
    assert shown.exit_code == 0 and "Resolution:" in shown.stdout
    assert not (tmp_path / "workspace/audits").exists()


def test_modal_prefix_words_are_not_joined_tokens_without_attested_remainder() -> None:
    # Agent nouns and their inflections begin with a modal verb's letters; the
    # remainder is never a standalone word in the chapter, so no join is reported.
    source = (
        "<!-- source-page: 256 -->\n\nA synthetic porter was shouldering whole\n"
        "sacks toward the kitchens.\n\n"
        "<!-- source-page: 437 -->\n\nThe officer was shouldering in. Well, not\n"
        "exactly shouldering; he was too slender to shoulder anyone aside.\n"
    )
    findings, _, _ = _source_findings("008", source)
    assert [f for f in findings if f.category is AuditCategory.JOINED_TOKEN] == []


def test_attested_modal_join_is_reported_once_per_occurrence_with_matching_evidence() -> None:
    source = (
        "<!-- source-page: 1 -->\n\nShe never thought it wouldnever end, and again it\n"
        "wouldnever end.\n\nIt shouldalways rain here; always.\n"
    )
    findings, _, _ = _source_findings("001", source)
    joined = [f for f in findings if f.category is AuditCategory.JOINED_TOKEN]
    assert [f.evidence["token"] for f in joined] == ["wouldnever", "wouldnever", "shouldalways"]
    assert all(f.evidence["join_kind"] == "modal" for f in joined)
    assert all("modal verb" in f.message for f in joined)
    assert all(f.confidence is AuditConfidence.HIGH for f in joined)
    # Two occurrences in one block are distinct findings with distinct token positions.
    assert len({f.finding_id for f in joined}) == 3
    assert joined[0].evidence["block"] == joined[1].evidence["block"]
    assert joined[0].evidence["token_index"] != joined[1].evidence["token_index"]


def test_case_boundary_and_article_joins_keep_their_own_messages() -> None:
    source = "<!-- source-page: 1 -->\n\nA synthetic couldChannel token and TheDragon banner.\n"
    findings, _, _ = _source_findings("001", source)
    joined = {f.evidence["token"]: f for f in findings if f.category is AuditCategory.JOINED_TOKEN}
    assert joined["couldChannel"].evidence["join_kind"] == "case_boundary"
    assert "lowercase-to-uppercase" in joined["couldChannel"].message
    assert joined["TheDragon"].evidence["join_kind"] == "article"
    assert "article" in joined["TheDragon"].message
