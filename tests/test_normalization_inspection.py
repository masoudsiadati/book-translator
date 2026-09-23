from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.artifact_regeneration import (
    execute_artifact_regeneration,
    plan_artifact_regeneration,
)
from book_translator.cli import app
from book_translator.models import ChapterMetadata, ChapterStatus, TocKind
from book_translator.normalization import NORMALIZATION_VERSION, normalize_text
from book_translator.normalization.inspection import inspect_normalization_changes

runner = CliRunner()
ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _workspace_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_yaml(path: Path, value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _synthetic_project(root: Path, *, byte_stale: bool) -> None:
    (root / "config").mkdir()
    (root / "input").mkdir()
    pdf = root / "input/book.pdf"
    pdf.write_bytes(b"synthetic-pdf")
    pdf_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (root / "config/book.yaml").write_text(
        """book_id: synthetic-book
title_en: Synthetic Book
title_fa: کتاب ساختگی
author: Synthetic Author
source_language: en
target_language: fa
pdf_path: input/book.pdf
chunk_target_words: 10
chunk_max_words: 20
previous_context_words: 4
output_format: epub3
""",
        encoding="utf-8",
    )
    resources = (
        ("translation-prompt.md", "prompt", "Synthetic prompt.\n"),
        ("glossary.yaml", "glossary", "terms: {}\n"),
        ("style-guide.md", "style_guide", "Synthetic style.\n"),
    )
    for filename, kind, content in resources:
        path = root / "config" / filename
        path.write_text(content, encoding="utf-8")
        _write_yaml(
            path.with_suffix(".metadata.yaml"),
            {
                "schema_version": "1.0",
                "resource_kind": kind,
                "resource_file": path.relative_to(root).as_posix(),
                "version": "1.0.0",
            },
        )
    chapter = root / "workspace/chapters/001"
    chapter.mkdir(parents=True)
    source = (
        "<!-- source-page: 1 -->\n\n"
        "A synthetic lead line\nrequires the\n\n"
        "<!-- source-page: 2 -->\n\nCapitalized continuation is synthetic.\n\n"
        "An unchanged synthetic paragraph.\n"
    )
    normalized = normalize_text(source).cleaned_text
    cleaned = (
        normalized.replace("the Capitalized continuation", "the\n\nCapitalized continuation", 1)
        if byte_stale
        else normalized
    )
    assert (cleaned != normalized) is byte_stale
    source_path = chapter / "source.md"
    cleaned_path = chapter / "cleaned.md"
    source_path.write_text(source, encoding="utf-8")
    cleaned_path.write_text(cleaned, encoding="utf-8")
    source_sha256 = hashlib.sha256(source.encode()).hexdigest()
    cleaned_sha256 = hashlib.sha256(cleaned.encode()).hexdigest()
    metadata = ChapterMetadata(
        schema_version="1.0",
        chapter_id="001",
        kind=TocKind.CHAPTER,
        number=1,
        title_en="Synthetic Chapter",
        source_start_page=1,
        source_end_page=2,
        source_file=Path("workspace/chapters/001/source.md"),
        source_sha256=source_sha256,
        cleaned_file=Path("workspace/chapters/001/cleaned.md"),
        cleaned_sha256=cleaned_sha256,
        source_word_count=len(source.split()),
        cleaned_word_count=len(cleaned.split()),
        source_paragraph_count=3,
        cleaned_paragraph_count=len(cleaned.rstrip("\n").split("\n\n")),
        status=ChapterStatus.NORMALIZED,
        extraction_version="1.0",
        normalization_version="1.0",
        created_at=NOW,
        updated_at=NOW,
    )
    _write_yaml(chapter / "metadata.yaml", metadata)
    _write_yaml(
        chapter / "normalization-report.yaml",
        {
            "schema_version": "1.0",
            "chapter_id": "001",
            "source_sha256": source_sha256,
            "cleaned_sha256": cleaned_sha256,
            "normalization_version": "1.0",
            "cleaned_paragraph_count": metadata.cleaned_paragraph_count,
            "warnings": [],
        },
    )
    _write_yaml(
        root / "workspace/toc.yaml",
        {
            "schema_version": "1.0",
            "source": "embedded",
            "source_pdf": "input/book.pdf",
            "source_pdf_sha256": pdf_sha256,
            "pdf_page_count": 2,
            "entry_count": 1,
            "entries": [
                {
                    "entry_id": "chapter-001",
                    "level": 1,
                    "kind": "chapter",
                    "number": 1,
                    "title_en": "Synthetic Chapter",
                    "title_fa": "",
                    "symbol": None,
                    "start_page": 1,
                    "end_page": 2,
                    "source_toc_title": "Chapter 1",
                }
            ],
        },
    )


def test_real_prologue_inspection_uses_exact_planner_bytes() -> None:
    plan = plan_artifact_regeneration(project_root=ROOT, selectors=["000-prologue"])

    inspection = inspect_normalization_changes(plan.chapters[0])

    assert inspection.planned_bytes == plan.chapters[0].normalization_result.cleaned_text.encode()
    assert hashlib.sha256(inspection.planned_bytes).hexdigest() == (
        plan.chapters[0].planned_cleaned_sha256
    )
    assert (
        inspection.current_sha256
        == hashlib.sha256(plan.chapters[0].paths.cleaned.read_bytes()).hexdigest()
    )
    assert inspection.planned_sha256 == plan.chapters[0].planned_cleaned_sha256


def test_real_inspection_is_stable_and_read_only(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    before = _workspace_hash(ROOT / "workspace")
    monkeypatch.chdir(ROOT)

    first = runner.invoke(app, ["inspect-normalization-changes", "--chapter", "000-prologue"])
    second = runner.invoke(app, ["inspect-normalization-changes", "--chapter", "000-prologue"])

    assert first.exit_code == 0, first.stdout
    assert second.exit_code == 0, second.stdout
    assert first.stdout == second.stdout
    assert _workspace_hash(ROOT / "workspace") == before


def test_version_stale_chapter_has_no_cleaned_byte_changes(tmp_path: Path) -> None:
    _synthetic_project(tmp_path, byte_stale=False)
    plan = plan_artifact_regeneration(project_root=tmp_path, selectors=["001"], now=lambda: NOW)

    inspections = tuple(inspect_normalization_changes(chapter) for chapter in plan.chapters)

    assert tuple(item.chapter_id for item in inspections) == ("001",)
    assert all(item.current_version != item.planned_version for item in inspections)
    assert all(not item.bytes_change for item in inspections)
    assert all(not item.regions for item in inspections)


def test_json_output_is_deterministic_and_contains_no_candidate_byte_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _synthetic_project(tmp_path, byte_stale=True)
    before = _workspace_hash(tmp_path / "workspace")
    monkeypatch.chdir(tmp_path)

    first = runner.invoke(app, ["inspect-normalization-changes", "--chapter", "001", "--json"])
    second = runner.invoke(app, ["inspect-normalization-changes", "--chapter", "001", "--json"])

    assert first.exit_code == 0, first.stdout
    assert second.exit_code == 0, second.stdout
    assert first.stdout == second.stdout
    assert '"kind": "paragraph_join"' in second.stdout
    assert '"pass_name": "page-boundary-continuations"' in second.stdout
    assert '"rule": "article-capitalized-complement"' in second.stdout
    assert '"planned_bytes"' not in second.stdout
    assert _workspace_hash(tmp_path / "workspace") == before


def test_inspection_lifecycle_becomes_current_after_regeneration(tmp_path: Path) -> None:
    _synthetic_project(tmp_path, byte_stale=True)
    stale_plan = plan_artifact_regeneration(
        project_root=tmp_path, selectors=["001"], now=lambda: NOW
    )
    stale = inspect_normalization_changes(stale_plan.chapters[0])

    assert stale.bytes_change
    assert len(stale.regions) == 1
    assert stale.regions[0].kind == "paragraph_join"
    assert stale.regions[0].pages == (1, 2)
    assert stale.regions[0].pass_name == "page-boundary-continuations"
    assert stale.regions[0].rule == "article-capitalized-complement"
    assert stale.planned_bytes == stale_plan.chapters[0].normalization_result.cleaned_text.encode()
    assert stale.planned_sha256 == stale_plan.chapters[0].planned_cleaned_sha256

    execute_artifact_regeneration(stale_plan)
    current_plan = plan_artifact_regeneration(
        project_root=tmp_path, selectors=["001"], now=lambda: NOW
    )
    current = inspect_normalization_changes(current_plan.chapters[0])

    assert current.current_version == NORMALIZATION_VERSION
    assert current.current_version == current.planned_version
    assert not current.bytes_change
    assert not current.regions


def test_multiple_semantic_regions_are_reported_in_source_order(tmp_path: Path) -> None:
    base = plan_artifact_regeneration(project_root=ROOT, selectors=["000-prologue"]).chapters[0]
    source = (
        "<!-- source-page: 1 -->\n\n"
        "A synthetic lead line\nrequires the\n\n"
        "<!-- source-page: 2 -->\n\nFirst continuation is synthetic.\n\n"
        "An unchanged anchor paragraph.\n\n"
        "A different synthetic lead line\nrequires a\n\n"
        "<!-- source-page: 3 -->\n\nSecond continuation is synthetic.\n"
    )
    result = normalize_text(source)
    current = result.cleaned_text.replace(
        "the First continuation", "the\n\nFirst continuation", 1
    ).replace("a Second continuation", "a\n\nSecond continuation", 1)
    chapter_dir = tmp_path / "chapter"
    chapter_dir.mkdir()
    source_path = chapter_dir / "source.md"
    cleaned_path = chapter_dir / "cleaned.md"
    source_path.write_text(source, encoding="utf-8")
    cleaned_path.write_text(current, encoding="utf-8")
    paths = replace(base.paths, chapter=chapter_dir, source=source_path, cleaned=cleaned_path)
    chapter = replace(base, paths=paths, normalization_result=result, chunk_artifacts=())

    inspection = inspect_normalization_changes(chapter)

    assert tuple(region.kind for region in inspection.regions) == (
        "paragraph_join",
        "paragraph_join",
    )
    assert tuple(region.pages for region in inspection.regions) == ((1, 2), (2, 3))
