import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.atomic_write import atomic_write_text
from book_translator.cli import app
from book_translator.document_chunking import (
    CHUNK_ENGINE_VERSION,
    ChunkingConfig,
    plan_document,
    reconstruct_document,
)
from book_translator.document_chunking.context import build_context
from book_translator.document_chunking.paragraphs import DocumentParseError, parse_document
from book_translator.document_chunking.workflow import (
    ChapterChunkingError,
    execute_chapter_chunking,
    plan_chapter_chunking,
)
from book_translator.models import (
    ChapterMetadata,
    ChapterStatus,
    ChunkMetadata,
    ChunkStatus,
    TocKind,
)
from book_translator.status_transitions import can_transition_chapter

runner = CliRunner()
NOW = datetime(2026, 2, 3, tzinfo=UTC)


def words(label: str, count: int) -> str:
    return " ".join([label, *[f"w{index}" for index in range(1, count)]])


def document(*paragraphs: str) -> str:
    return "\n\n".join(paragraphs) + "\n"


def config(target: int = 10, maximum: int = 15, context: int = 4) -> ChunkingConfig:
    return ChunkingConfig(target, maximum, context)


def project_config() -> str:
    return """\
book_id: synthetic-book
title_en: Synthetic Book
title_fa: کتاب ساختگی
author: Synthetic Author
source_language: en
target_language: fa
pdf_path: input/book.pdf
chunk_target_words: 10
chunk_max_words: 15
previous_context_words: 4
output_format: epub3
"""


def make_normalized_chapter(
    root: Path,
    number: int | None,
    cleaned: str,
    *,
    title: str = "Synthetic Metadata Title",
) -> Path:
    name = "000-prologue" if number is None else f"{number:03d}"
    directory = root / "workspace/chapters" / name
    directory.mkdir(parents=True)
    cleaned_path = directory / "cleaned.md"
    cleaned_path.write_text(cleaned, encoding="utf-8")
    source_path = directory / "source.md"
    source_path.write_text("synthetic immutable source\n", encoding="utf-8")
    metadata = ChapterMetadata(
        schema_version="1.0",
        chapter_id=name,
        kind=TocKind.PROLOGUE if number is None else TocKind.CHAPTER,
        number=0 if number is None else number,
        title_en=title,
        source_start_page=1,
        source_end_page=1,
        source_file=Path(f"workspace/chapters/{name}/source.md"),
        source_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
        cleaned_file=Path(f"workspace/chapters/{name}/cleaned.md"),
        cleaned_sha256=hashlib.sha256(cleaned_path.read_bytes()).hexdigest(),
        source_word_count=3,
        cleaned_word_count=len(cleaned.split()),
        source_paragraph_count=1,
        cleaned_paragraph_count=len(cleaned.rstrip("\n").split("\n\n")),
        status=ChapterStatus.NORMALIZED,
        extraction_version="1.0",
        normalization_version="1.0",
        created_at=NOW,
        updated_at=NOW,
    )
    directory.joinpath("metadata.yaml").write_text(
        yaml.safe_dump(metadata.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    return directory


def test_paragraph_parsing_preserves_exact_text_order_and_counts() -> None:
    cleaned = document("Heading", '"Dialogue remains."', "Verse line one\nVerse line two")

    paragraphs = parse_document(cleaned)

    assert [paragraph.sequence for paragraph in paragraphs] == [1, 2, 3]
    assert [paragraph.text for paragraph in paragraphs] == [
        "Heading",
        '"Dialogue remains."',
        "Verse line one\nVerse line two",
    ]
    assert paragraphs[2].is_multiline
    assert paragraphs[0].byte_count == len(b"Heading")


@pytest.mark.parametrize("cleaned", ["", "text", "text\n\n", "\ntext\n", "text\r\n"])
def test_empty_or_noncanonical_input_is_rejected(cleaned: str) -> None:
    with pytest.raises(DocumentParseError):
        parse_document(cleaned)


def test_target_planning_obeys_maximum_and_never_splits_paragraphs() -> None:
    cleaned = document(*(words(f"p{index}", 6) for index in range(1, 6)))

    plan = plan_document(cleaned, config())

    assert all(chunk.word_count <= 15 for chunk in plan.chunks)
    assert [paragraph.sequence for chunk in plan.chunks for paragraph in chunk.paragraphs] == list(
        range(1, 6)
    )
    assert all(chunk.paragraph_start <= chunk.paragraph_end for chunk in plan.chunks)


def test_explicit_scene_boundary_is_preferred_and_separator_is_preserved() -> None:
    cleaned = document(words("before", 6), "***", words("nearer", 5), words("later", 6))

    plan = plan_document(cleaned, config())

    assert plan.chunks[0].paragraph_end == 2
    assert plan.chunks[0].ended_at_scene_boundary
    assert plan.scene_separators_found == 1
    assert "***" in plan.chunks[0].source_text
    assert reconstruct_document(tuple(chunk.source_text for chunk in plan.chunks)) == cleaned


def test_planning_is_deterministic_for_single_and_multiple_chunks() -> None:
    single = document(words("only", 3))
    multiple = document(*(words(f"p{index}", 5) for index in range(5)))

    assert plan_document(single, config()) == plan_document(single, config())
    assert len(plan_document(single, config()).chunks) == 1
    assert plan_document(multiple, config()) == plan_document(multiple, config())


def test_oversized_paragraph_is_kept_intact_and_warned() -> None:
    cleaned = document(words("oversize", 20), words("normal", 4))

    plan = plan_document(cleaned, config())

    assert plan.chunks[0].word_count == 20
    assert plan.chunks[0].oversize
    assert plan.oversize_chunk_count == 1
    assert plan.warnings
    assert len(plan.chunks[0].paragraphs) == 1


def test_tiny_tail_is_rebalanced_using_complete_paragraphs() -> None:
    cleaned = document(words("one", 6), words("two", 6), words("three", 2), words("four", 2))

    plan = plan_document(cleaned, config())

    assert plan.tail_rebalanced
    assert [chunk.word_count for chunk in plan.chunks] == [6, 10]
    assert [chunk.paragraph_end for chunk in plan.chunks] == [1, 4]


def test_tail_is_not_rebalanced_when_no_safe_improvement_exists() -> None:
    cleaned = document(words("large", 14), words("tiny", 2))

    plan = plan_document(cleaned, config())

    assert not plan.tail_rebalanced
    assert [chunk.word_count for chunk in plan.chunks] == [14, 2]


def test_exact_reconstruction_has_no_loss_duplication_or_reordering() -> None:
    cleaned = document("Heading", words("alpha", 8), "* * *", words("omega", 9))

    plan = plan_document(cleaned, config())
    reconstructed = reconstruct_document(tuple(chunk.source_text for chunk in plan.chunks))

    assert reconstructed.encode() == cleaned.encode()
    assert (
        hashlib.sha256(reconstructed.encode()).hexdigest()
        == hashlib.sha256(cleaned.encode()).hexdigest()
    )
    assert [p.text for chunk in plan.chunks for p in chunk.paragraphs] == [
        p.text for p in plan.paragraphs
    ]


def test_context_is_empty_for_first_chunk_and_uses_only_preceding_content() -> None:
    cleaned = document(*(words(f"paragraph{index}", 6) for index in range(4)))

    plan = plan_document(cleaned, config(context=4))

    assert plan.chunks[0].context.text == ""
    assert plan.chunks[0].context.word_count == 0
    for chunk in plan.chunks[1:]:
        current_text = {paragraph.text for paragraph in chunk.paragraphs}
        assert all(text not in chunk.context.text for text in current_text)


def test_context_prefers_complete_paragraphs_then_word_tail_fallback() -> None:
    paragraphs = parse_document(
        document(words("older", 5), words("nearest", 2), words("current", 3))
    )

    context = build_context(paragraphs, current_start=3, limit_words=4)

    assert context.word_count == 4
    assert context.source_start_paragraph == 1
    assert context.source_end_paragraph == 2
    assert context.text.endswith(words("nearest", 2) + "\n")
    assert context == build_context(paragraphs, current_start=3, limit_words=4)


def test_configuration_validation() -> None:
    with pytest.raises(ValueError):
        ChunkingConfig(0, 10, 1)
    with pytest.raises(ValueError):
        ChunkingConfig(11, 10, 1)
    with pytest.raises(ValueError):
        ChunkingConfig(5, 10, -1)
    with pytest.raises(ValueError):
        ChunkingConfig(5, 10, 10)


def test_publication_hashes_metadata_report_paths_and_status(tmp_path: Path) -> None:
    directory = make_normalized_chapter(
        tmp_path, 1, document(*(words(f"p{index}", 6) for index in range(4)))
    )
    source_before = directory.joinpath("source.md").read_bytes()
    cleaned_before = directory.joinpath("cleaned.md").read_bytes()

    plan = plan_chapter_chunking(
        project_root=tmp_path, config=config(), selector="1", now=lambda: NOW
    )
    result = execute_chapter_chunking(plan)

    assert result.chunked_ids == ("001",)
    assert directory.joinpath("source.md").read_bytes() == source_before
    assert directory.joinpath("cleaned.md").read_bytes() == cleaned_before
    chapter = ChapterMetadata.model_validate(
        yaml.safe_load(directory.joinpath("metadata.yaml").read_text(encoding="utf-8"))
    )
    report = yaml.safe_load(directory.joinpath("chunking-report.yaml").read_text(encoding="utf-8"))
    assert chapter.status is ChapterStatus.CHUNKED
    assert chapter.chunk_count == len(plan.chapters[0].document_plan.chunks)
    assert chapter.chunk_engine_version == CHUNK_ENGINE_VERSION
    assert chapter.created_at == NOW
    assert report["reconstructed_sha256"] == chapter.cleaned_sha256
    assert report["chunk_count"] == chapter.chunk_count
    assert report["normal_chunk_count"] + report["oversize_chunk_count"] == chapter.chunk_count
    for sequence in range(1, chapter.chunk_count + 1):
        chunk_dir = directory / f"chunks/{sequence:03d}"
        assert {path.name for path in chunk_dir.iterdir()} == {
            "metadata.yaml",
            "source.md",
            "context.md",
        }
        metadata = ChunkMetadata.model_validate(
            yaml.safe_load(chunk_dir.joinpath("metadata.yaml").read_text(encoding="utf-8"))
        )
        assert metadata.chunk_id == f"001-{sequence:03d}"
        assert metadata.status is ChunkStatus.PENDING
        assert metadata.source_file == Path(
            f"workspace/chapters/001/chunks/{sequence:03d}/source.md"
        )
        assert (
            metadata.source_sha256
            == hashlib.sha256(chunk_dir.joinpath("source.md").read_bytes()).hexdigest()
        )
        assert (
            metadata.context_sha256
            == hashlib.sha256(chunk_dir.joinpath("context.md").read_bytes()).hexdigest()
        )
        assert metadata.translation_attempts == metadata.review_attempts == 0
        assert not chunk_dir.joinpath("translation.md").exists()


def test_canonical_transition_path_and_reconstruction_after_publication(tmp_path: Path) -> None:
    directory = make_normalized_chapter(
        tmp_path, None, document(*(words(f"p{index}", 5) for index in range(5)))
    )
    assert can_transition_chapter(ChapterStatus.NORMALIZED, ChapterStatus.VALIDATED)
    assert can_transition_chapter(ChapterStatus.VALIDATED, ChapterStatus.CHUNKED)
    plan = plan_chapter_chunking(project_root=tmp_path, config=config(), selector="prologue")
    execute_chapter_chunking(plan)
    sources = tuple(
        path.read_text(encoding="utf-8") for path in sorted(directory.glob("chunks/*/source.md"))
    )
    assert reconstruct_document(sources) == directory.joinpath("cleaned.md").read_text(
        encoding="utf-8"
    )


def test_skip_stale_configuration_engine_and_cleaned_lineage(tmp_path: Path) -> None:
    directory = make_normalized_chapter(tmp_path, 1, document(words("one", 6), words("two", 6)))
    execute_chapter_chunking(plan_chapter_chunking(project_root=tmp_path, config=config()))
    assert (
        plan_chapter_chunking(project_root=tmp_path, config=config()).chapters[0].action == "skip"
    )
    assert (
        plan_chapter_chunking(project_root=tmp_path, config=ChunkingConfig(9, 15, 4))
        .chapters[0]
        .action
        == "stale"
    )
    report_path = directory / "chunking-report.yaml"
    report = yaml.safe_load(report_path.read_text(encoding="utf-8"))
    report["chunk_engine_version"] = "0.9.0"
    report_path.write_text(yaml.safe_dump(report, sort_keys=False), encoding="utf-8")
    assert (
        plan_chapter_chunking(project_root=tmp_path, config=config()).chapters[0].action == "stale"
    )
    report["chunk_engine_version"] = CHUNK_ENGINE_VERSION
    report_path.write_text(yaml.safe_dump(report, sort_keys=False), encoding="utf-8")
    cleaned_path = directory / "cleaned.md"
    cleaned_path.write_text(document(words("changed", 7), words("two", 6)), encoding="utf-8")
    metadata_path = directory / "metadata.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    metadata["cleaned_sha256"] = hashlib.sha256(cleaned_path.read_bytes()).hexdigest()
    metadata["cleaned_word_count"] = 13
    metadata_path.write_text(yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8")
    assert (
        plan_chapter_chunking(project_root=tmp_path, config=config()).chapters[0].action == "stale"
    )


def test_cleaned_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    directory = make_normalized_chapter(tmp_path, 1, document(words("one", 5)))
    directory.joinpath("cleaned.md").write_text(document(words("tampered", 5)), encoding="utf-8")
    with pytest.raises(ChapterChunkingError, match="cleaned hash integrity"):
        plan_chapter_chunking(project_root=tmp_path, config=config())


def test_partial_artifacts_block_and_force_can_replace_safely(tmp_path: Path) -> None:
    directory = make_normalized_chapter(tmp_path, 1, document(words("one", 6), words("two", 6)))
    directory.joinpath("chunks").mkdir()
    plan = plan_chapter_chunking(project_root=tmp_path, config=config())
    assert plan.chapters[0].action == "conflict"
    with pytest.raises(ChapterChunkingError, match="preflight blocked"):
        execute_chapter_chunking(plan)
    forced = plan_chapter_chunking(project_root=tmp_path, config=config(), force=True)
    assert forced.chapters[0].action == "replace"
    execute_chapter_chunking(forced)
    assert directory.joinpath("chunking-report.yaml").is_file()


def test_force_refuses_translation_review_or_final_artifacts(tmp_path: Path) -> None:
    directory = make_normalized_chapter(tmp_path, 1, document(words("one", 6), words("two", 6)))
    execute_chapter_chunking(plan_chapter_chunking(project_root=tmp_path, config=config()))
    directory.joinpath("chunks/001/translation.md").write_text(
        "synthetic downstream", encoding="utf-8"
    )
    forced = plan_chapter_chunking(project_root=tmp_path, config=config(), force=True)
    assert forced.chapters[0].action == "conflict"
    with pytest.raises(ChapterChunkingError, match="preflight blocked"):
        execute_chapter_chunking(forced)


def test_dry_run_cli_writes_nothing_and_show_plan_contains_no_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = make_normalized_chapter(
        tmp_path, None, document("SECRET_SYNTHETIC_PROSE", words("other", 8))
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config/book.yaml").write_text(project_config(), encoding="utf-8")
    metadata_before = directory.joinpath("metadata.yaml").read_bytes()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app, ["chunk-chapters", "--chapter", "prologue", "--dry-run", "--show-plan"]
    )

    assert result.exit_code == 0
    assert "SECRET_SYNTHETIC_PROSE" not in result.stdout + result.stderr
    assert "Paragraphs" in result.stdout
    assert not directory.joinpath("chunks").exists()
    assert not directory.joinpath("chunking-report.yaml").exists()
    assert directory.joinpath("metadata.yaml").read_bytes() == metadata_before


@pytest.mark.parametrize("selector", ["prologue", "0"])
def test_prologue_selector_compatibility(tmp_path: Path, selector: str) -> None:
    make_normalized_chapter(tmp_path, None, document(words("one", 5)))
    assert (
        plan_chapter_chunking(project_root=tmp_path, config=config(), selector=selector)
        .chapters[0]
        .metadata.chapter_id
        == "000-prologue"
    )


@pytest.mark.parametrize("selector", ["1", "001", "chapter-001"])
def test_numbered_selector_compatibility(tmp_path: Path, selector: str) -> None:
    make_normalized_chapter(tmp_path, 1, document(words("one", 5)))
    assert (
        plan_chapter_chunking(project_root=tmp_path, config=config(), selector=selector)
        .chapters[0]
        .metadata.chapter_id
        == "001"
    )


def test_batch_preflight_prevents_known_partial_writes(tmp_path: Path) -> None:
    first = make_normalized_chapter(tmp_path, 1, document(words("first", 5)))
    second = make_normalized_chapter(tmp_path, 2, document(words("second", 5)))
    second.joinpath("chunks").mkdir()
    plan = plan_chapter_chunking(project_root=tmp_path, config=config())

    with pytest.raises(ChapterChunkingError, match="preflight blocked"):
        execute_chapter_chunking(plan)

    assert not first.joinpath("chunks").exists()


def test_runtime_failure_preserves_completed_and_failed_chapter_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = make_normalized_chapter(tmp_path, 1, document(words("first", 5)))
    second = make_normalized_chapter(tmp_path, 2, document(words("second", 5)))
    plan = plan_chapter_chunking(project_root=tmp_path, config=config())
    real_write = atomic_write_text

    def failing_write(destination: Path, content: str, *, overwrite: bool = False) -> None:
        if destination == second / "chunking-report.yaml":
            raise OSError("injected report failure")
        real_write(destination, content, overwrite=overwrite)

    monkeypatch.setattr(
        "book_translator.document_chunking.workflow.atomic_write_text", failing_write
    )
    with pytest.raises(ChapterChunkingError, match="completed: 001"):
        execute_chapter_chunking(plan)
    first_metadata = ChapterMetadata.model_validate(
        yaml.safe_load(first.joinpath("metadata.yaml").read_text())
    )
    second_metadata = ChapterMetadata.model_validate(
        yaml.safe_load(second.joinpath("metadata.yaml").read_text())
    )
    assert first_metadata.status is ChapterStatus.CHUNKED
    assert second_metadata.status is ChapterStatus.NORMALIZED
    monkeypatch.undo()
    recovery = plan_chapter_chunking(
        project_root=tmp_path, config=config(), selector="2", force=True
    )
    execute_chapter_chunking(recovery)
    recovered = ChapterMetadata.model_validate(
        yaml.safe_load(second.joinpath("metadata.yaml").read_text())
    )
    assert recovered.status is ChapterStatus.CHUNKED


def test_forced_replacement_failure_rolls_back_valid_chunk_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = make_normalized_chapter(
        tmp_path, 1, document(*(words(f"p{index}", 6) for index in range(4)))
    )
    execute_chapter_chunking(plan_chapter_chunking(project_root=tmp_path, config=config()))
    metadata_before = directory.joinpath("metadata.yaml").read_bytes()
    report_before = directory.joinpath("chunking-report.yaml").read_bytes()
    source_hash_before = hashlib.sha256(
        directory.joinpath("chunks/001/source.md").read_bytes()
    ).hexdigest()
    forced = plan_chapter_chunking(project_root=tmp_path, config=config(), force=True)
    real_write = atomic_write_text

    def failing_write(destination: Path, content: str, *, overwrite: bool = False) -> None:
        if destination == directory / "chunking-report.yaml":
            raise OSError("injected forced report failure")
        real_write(destination, content, overwrite=overwrite)

    monkeypatch.setattr(
        "book_translator.document_chunking.workflow.atomic_write_text", failing_write
    )
    with pytest.raises(ChapterChunkingError, match="failed artifacts remain recoverable"):
        execute_chapter_chunking(forced)

    assert directory.joinpath("metadata.yaml").read_bytes() == metadata_before
    assert directory.joinpath("chunking-report.yaml").read_bytes() == report_before
    assert (
        hashlib.sha256(directory.joinpath("chunks/001/source.md").read_bytes()).hexdigest()
        == source_hash_before
    )
    assert directory.joinpath(".chunking-failed-chunks").is_dir()
    monkeypatch.undo()
    retry = plan_chapter_chunking(project_root=tmp_path, config=config(), force=True)
    execute_chapter_chunking(retry)
    assert not directory.joinpath(".chunking-failed-chunks").exists()


def test_status_reports_chunked_chapters_and_total_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_normalized_chapter(tmp_path, 1, document(words("one", 6), words("two", 6)))
    execute_chapter_chunking(plan_chapter_chunking(project_root=tmp_path, config=config()))
    (tmp_path / "config").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "config/book.yaml").write_text(project_config(), encoding="utf-8")
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
    assert "Chunked chapters" in result.stdout
    assert "Total chunks" in result.stdout
