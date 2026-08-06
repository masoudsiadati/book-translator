import hashlib
import json
from pathlib import Path

import pymupdf
import pytest
import yaml
from typer.testing import CliRunner

import book_translator.chapter_extraction as extraction_module
from book_translator.chapter_extraction import (
    ChapterExtractionError,
    execute_extraction,
    plan_extraction,
)
from book_translator.cli import app
from book_translator.models import ChapterMetadata, TocDocument, TocEntry, TocKind
from book_translator.toc_writer import write_toc

runner = CliRunner()


def make_pdf(path: Path, page_lines: list[list[str]]) -> None:
    document = pymupdf.open()  # type: ignore[no-untyped-call]
    for lines in page_lines:
        page = document.new_page()
        for index, line in enumerate(lines):
            page.insert_text((72, 72 + index * 100), line)
    document.save(path)  # type: ignore[no-untyped-call]
    document.close()  # type: ignore[no-untyped-call]


def make_toc(
    root: Path,
    pdf: Path,
    entries: list[TocEntry],
    *,
    pdf_hash: str | None = None,
    page_count: int | None = None,
) -> Path:
    toc_path = root / "workspace/toc.yaml"
    toc = TocDocument(
        schema_version="1.0",
        source="embedded",
        source_pdf=pdf,
        source_pdf_sha256=pdf_hash or hashlib.sha256(pdf.read_bytes()).hexdigest(),
        pdf_page_count=page_count or len(pymupdf.open(pdf)),  # type: ignore[no-untyped-call]
        entry_count=len(entries),
        entries=entries,
    )
    write_toc(toc, toc_path)
    return toc_path


def entry(
    entry_id: str,
    kind: TocKind,
    start: int,
    end: int,
    number: int | None = None,
    title: str = "Metadata Title",
) -> TocEntry:
    return TocEntry(
        entry_id=entry_id,
        level=1,
        kind=kind,
        number=number,
        title_en=title,
        start_page=start,
        end_page=end,
        source_toc_title=title,
    )


def basic_fixture(tmp_path: Path) -> tuple[Path, Path]:
    pdf = tmp_path / "book.pdf"
    make_pdf(
        pdf,
        [
            ["FRONT MATTER"],
            ["PROLOGUE HEADING", "Header remains", "hyphen-"],
            ["continued", "Footer 3"],
            ["CHAPTER ONE", "Quoted words stay"],
        ],
    )
    toc = make_toc(
        tmp_path,
        pdf,
        [
            entry("front-matter-001", TocKind.FRONT_MATTER, 1, 1),
            entry("prologue", TocKind.PROLOGUE, 2, 3, 0),
            entry("chapter-001", TocKind.CHAPTER, 4, 4, 1),
        ],
    )
    return pdf, toc


def test_prologue_and_chapter_paths_content_counts_hash_and_metadata(tmp_path: Path) -> None:
    pdf, toc = basic_fixture(tmp_path)
    plan = plan_extraction(pdf, toc, project_root=tmp_path)
    result = execute_extraction(plan, project_root=tmp_path)

    prologue_dir = tmp_path / "workspace/chapters/000-prologue"
    chapter_dir = tmp_path / "workspace/chapters/001"
    assert result.extracted_ids == ["prologue", "chapter-001"]
    assert not (tmp_path / "workspace/chapters/front-matter-001").exists()
    assert {path.name for path in prologue_dir.iterdir()} == {"source.md", "metadata.yaml"}
    assert chapter_dir.joinpath("source.md").is_file()
    source = prologue_dir.joinpath("source.md").read_text(encoding="utf-8")
    assert source.index("source-page: 2") < source.index("source-page: 3")
    assert "<!-- source-page: 2 -->\n\n" in source
    assert "PROLOGUE HEADING" in source
    assert "Header remains" in source and "Footer 3" in source
    assert "hyphen-" in source and "continued" in source
    assert "hyphen-continued" not in source
    assert source.endswith("\n") and not source.endswith("\n\n")

    metadata = ChapterMetadata.model_validate(
        yaml.safe_load(prologue_dir.joinpath("metadata.yaml").read_text(encoding="utf-8"))
    )
    assert metadata.chapter_id == "000-prologue"
    assert metadata.status.value == "extracted"
    assert metadata.source_start_page == 2 and metadata.source_end_page == 3
    assert metadata.source_file == Path("workspace/chapters/000-prologue/source.md")
    assert metadata.source_sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert metadata.source_word_count == 8
    assert metadata.source_paragraph_count == 5
    assert metadata.chunk_count == 0
    assert metadata.cleaned_sha256 is None


def test_conservative_mechanical_normalization() -> None:
    normalized = extraction_module._normalize_block("a\r\nline\u00a0x  \r\n\r\n\r\nb\t \n")
    assert normalized == "a\nline x\n\n\nb"


def test_front_matter_requires_option(tmp_path: Path) -> None:
    pdf, toc = basic_fixture(tmp_path)
    plan = plan_extraction(pdf, toc, project_root=tmp_path, include_front_matter=True)
    execute_extraction(plan, project_root=tmp_path)
    assert (tmp_path / "workspace/chapters/front-matter-001/source.md").is_file()


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("prologue", "prologue"),
        ("0", "prologue"),
        ("1", "chapter-001"),
        ("001", "chapter-001"),
        ("chapter-001", "chapter-001"),
    ],
)
def test_selector_forms(tmp_path: Path, selector: str, expected: str) -> None:
    pdf, toc = basic_fixture(tmp_path)
    plan = plan_extraction(pdf, toc, project_root=tmp_path, selector=selector)
    assert [item.entry.entry_id for item in plan.entries] == [expected]


def test_front_matter_selector_and_nonexistent_selector(tmp_path: Path) -> None:
    pdf, toc = basic_fixture(tmp_path)
    with pytest.raises(ChapterExtractionError, match="did not resolve"):
        plan_extraction(pdf, toc, project_root=tmp_path, selector="front-matter-001")
    plan = plan_extraction(
        pdf,
        toc,
        project_root=tmp_path,
        selector="front-matter-001",
        include_front_matter=True,
    )
    assert plan.entries[0].entry.entry_id == "front-matter-001"
    with pytest.raises(ChapterExtractionError, match="did not resolve"):
        plan_extraction(pdf, toc, project_root=tmp_path, selector="99")


@pytest.mark.parametrize("failure", ["missing_pdf", "invalid_pdf", "missing_toc", "malformed_toc"])
def test_invalid_inputs(tmp_path: Path, failure: str) -> None:
    pdf, toc = basic_fixture(tmp_path)
    if failure == "missing_pdf":
        pdf.unlink()
    elif failure == "invalid_pdf":
        pdf.write_text("invalid", encoding="utf-8")
    elif failure == "missing_toc":
        toc.unlink()
    else:
        toc.write_text("entries: [", encoding="utf-8")
    with pytest.raises(ChapterExtractionError):
        plan_extraction(pdf, toc, project_root=tmp_path)


@pytest.mark.parametrize("mismatch", ["hash", "pages"])
def test_toc_provenance_mismatch(tmp_path: Path, mismatch: str) -> None:
    pdf = tmp_path / "book.pdf"
    make_pdf(pdf, [["one"]])
    toc = make_toc(
        tmp_path,
        pdf,
        [entry("chapter-001", TocKind.CHAPTER, 1, 1, 1)],
        pdf_hash="a" * 64 if mismatch == "hash" else None,
        page_count=2 if mismatch == "pages" else None,
    )
    with pytest.raises(ChapterExtractionError, match="does not match"):
        plan_extraction(pdf, toc, project_root=tmp_path)


def test_invalid_selected_page_range(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    make_pdf(pdf, [["one"]])
    toc = make_toc(tmp_path, pdf, [entry("chapter-001", TocKind.CHAPTER, 1, 2, 1)])
    with pytest.raises(ChapterExtractionError, match="invalid page range"):
        plan_extraction(pdf, toc, project_root=tmp_path)


def test_skip_valid_and_refuse_inconsistent_or_bad_hash(tmp_path: Path) -> None:
    pdf, toc = basic_fixture(tmp_path)
    execute_extraction(plan_extraction(pdf, toc, project_root=tmp_path), project_root=tmp_path)
    rerun = plan_extraction(pdf, toc, project_root=tmp_path)
    assert all(item.action == "skip" for item in rerun.entries)
    chapter_source = tmp_path / "workspace/chapters/001/source.md"
    chapter_source.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ChapterExtractionError, match="integrity"):
        plan_extraction(pdf, toc, project_root=tmp_path)
    chapter_source.unlink()
    with pytest.raises(ChapterExtractionError, match="inconsistent workspace"):
        plan_extraction(pdf, toc, project_root=tmp_path)


def test_force_replaces_but_refuses_downstream(tmp_path: Path) -> None:
    pdf, toc = basic_fixture(tmp_path)
    execute_extraction(plan_extraction(pdf, toc, project_root=tmp_path), project_root=tmp_path)
    forced = plan_extraction(pdf, toc, project_root=tmp_path, selector="prologue", force=True)
    assert forced.entries[0].action == "replace"
    execute_extraction(forced, project_root=tmp_path)
    cleaned = tmp_path / "workspace/chapters/000-prologue/cleaned.md"
    cleaned.write_text("downstream", encoding="utf-8")
    with pytest.raises(ChapterExtractionError, match="downstream artifacts"):
        plan_extraction(pdf, toc, project_root=tmp_path, selector="prologue", force=True)


def test_dry_run_cli_creates_no_chapter_directories_and_prints_no_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf, toc = basic_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["extract-chapters", str(pdf), "--toc", str(toc), "--dry-run", "--chapter", "1"]
    )
    assert result.exit_code == 0
    assert "Canonical entry ID" in result.stdout and "chapter-001" in result.stdout
    assert "CHAPTER ONE" not in result.stdout + result.stderr
    assert not (tmp_path / "workspace/chapters").exists()


def test_batch_preflight_prevents_partial_writes(tmp_path: Path) -> None:
    pdf, toc = basic_fixture(tmp_path)
    conflict = tmp_path / "workspace/chapters/001"
    conflict.mkdir(parents=True)
    conflict.joinpath("source.md").write_text("orphan", encoding="utf-8")
    with pytest.raises(ChapterExtractionError, match="inconsistent"):
        plan_extraction(pdf, toc, project_root=tmp_path)
    assert not (tmp_path / "workspace/chapters/000-prologue").exists()


def test_runtime_failure_cleanup_and_rerun_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf, toc = basic_fixture(tmp_path)
    plan = plan_extraction(pdf, toc, project_root=tmp_path)
    original = extraction_module.extract_entry_content

    def fail_second(document: pymupdf.Document, selected: TocEntry) -> tuple[str, int, int]:
        if selected.entry_id == "chapter-001":
            raise RuntimeError("injected")
        return original(document, selected)

    monkeypatch.setattr(extraction_module, "extract_entry_content", fail_second)
    with pytest.raises(ChapterExtractionError, match="completed: prologue"):
        execute_extraction(plan, project_root=tmp_path)
    assert (tmp_path / "workspace/chapters/000-prologue/metadata.yaml").is_file()
    assert not (tmp_path / "workspace/chapters/001/metadata.yaml").exists()
    assert not (tmp_path / "workspace/chapters/001/source.md").exists()

    monkeypatch.setattr(extraction_module, "extract_entry_content", original)
    resumed = plan_extraction(pdf, toc, project_root=tmp_path)
    assert [item.action for item in resumed.entries] == ["skip", "extract"]
    result = execute_extraction(resumed, project_root=tmp_path)
    assert result.skipped_ids == ["prologue"]
    assert result.extracted_ids == ["chapter-001"]


def test_status_uses_canonical_detected_and_extracted_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf, toc = basic_fixture(tmp_path)
    execute_extraction(
        plan_extraction(pdf, toc, project_root=tmp_path, selector="prologue"),
        project_root=tmp_path,
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "config/book.yaml").write_text(
        """book_id: test\n title_en: Test\n""".replace(" ", ""), encoding="utf-8"
    )
    config = {
        "book_id": "test",
        "title_en": "Test",
        "title_fa": "آزمون",
        "author": "A",
        "source_language": "en",
        "target_language": "fa",
        "pdf_path": str(pdf),
        "chunk_target_words": 10,
        "chunk_max_words": 20,
        "previous_context_words": 0,
        "output_format": "epub3",
    }
    (tmp_path / "config/book.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    progress: dict[str, list[object]] = {
        "detected_chapters": [],
        "translated_chapters": [],
        "approved_chapters": [],
        "failed_chunks": [],
    }
    (tmp_path / "state/progress.json").write_text(json.dumps(progress), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Detected chapters" in result.stdout and "1" in result.stdout
    assert "Extracted chapters" in result.stdout
