import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.config import ProjectDataError, load_book_config
from book_translator.models import ChapterMetadata, ChapterStatus, TocKind

runner = CliRunner()


def valid_config() -> str:
    return """\
book_id: test-book
title_en: Test Book
title_fa: کتاب آزمایشی
author: Test Author
source_language: en
target_language: fa
pdf_path: input/book.pdf
chunk_target_words: 1800
chunk_max_words: 2400
previous_context_words: 250
output_format: epub3
"""


def empty_progress() -> dict[str, list[object]]:
    return {
        "detected_chapters": [],
        "translated_chapters": [],
        "approved_chapters": [],
        "failed_chunks": [],
    }


def test_load_valid_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "book.yaml"
    config_path.write_text(valid_config(), encoding="utf-8")

    config = load_book_config(config_path)

    assert config.title_en == "Test Book"
    assert config.title_fa == "کتاب آزمایشی"
    assert config.chunk_target_words == 1800
    assert config.output_format == "epub3"


def test_status_reports_absent_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    Path("state").mkdir()
    Path("config/book.yaml").write_text(valid_config(), encoding="utf-8")
    Path("state/progress.json").write_text(json.dumps(empty_progress()), encoding="utf-8")

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Test Book" in result.stdout
    assert "en → fa" in result.stdout
    assert "Source PDF exists" in result.stdout
    assert "no" in result.stdout
    assert "Detected chapters" in result.stdout


def test_load_malformed_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "book.yaml"
    config_path.write_text("title_en: [unterminated", encoding="utf-8")

    with pytest.raises(ProjectDataError, match="could not read configuration"):
        load_book_config(config_path)


def test_status_exits_nonzero_for_malformed_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    Path("state").mkdir()
    Path("config/book.yaml").write_text("title_en: [unterminated", encoding="utf-8")
    Path("state/progress.json").write_text(json.dumps(empty_progress()), encoding="utf-8")

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 1
    assert "Error:" in result.stderr
    assert "configuration" in result.stderr


def test_status_reports_workspace_normalized_count_separately_from_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    Path("state").mkdir()
    chapter = Path("workspace/chapters/001")
    chapter.mkdir(parents=True)
    Path("config/book.yaml").write_text(valid_config(), encoding="utf-8")
    progress = empty_progress()
    progress["translated_chapters"] = [1]
    Path("state/progress.json").write_text(json.dumps(progress), encoding="utf-8")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    metadata = ChapterMetadata(
        schema_version="1.0",
        chapter_id="001",
        kind=TocKind.CHAPTER,
        number=1,
        source_start_page=1,
        source_end_page=1,
        source_file=Path("workspace/chapters/001/source.md"),
        source_sha256="a" * 64,
        cleaned_file=Path("workspace/chapters/001/cleaned.md"),
        cleaned_sha256="b" * 64,
        status=ChapterStatus.NORMALIZED,
        extraction_version="1.0",
        normalization_version="1.0",
        created_at=now,
        updated_at=now,
    )
    chapter.joinpath("metadata.yaml").write_text(
        yaml.safe_dump(metadata.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Normalized chapters" in result.stdout
    assert "Legacy translated progress" in result.stdout
