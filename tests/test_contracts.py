import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from book_translator.hashing import sha256_file
from book_translator.models import (
    SCHEMA_VERSION,
    BookWorkspaceMetadata,
    ChapterMetadata,
    ChapterStatus,
    ChunkMetadata,
    ChunkStatus,
    ExtractionSnapshot,
    PipelineRunStatus,
    TocDocument,
    TocEntry,
    TocKind,
)
from book_translator.models.snapshot import ChapterSnapshotHashes
from book_translator.paths import ProjectPaths, chapter_directory_name, chunk_directory_name
from book_translator.status_transitions import can_transition_chapter, can_transition_chunk

NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
HASH = "a" * 64


def test_canonical_directory_names() -> None:
    assert chapter_directory_name(prologue=True) == "000-prologue"
    assert chapter_directory_name(1) == "001"
    assert chapter_directory_name(56) == "056"
    assert chunk_directory_name(1) == "001"
    assert chunk_directory_name(42) == "042"


@pytest.mark.parametrize("number", [None, 0, -1, 1000])
def test_invalid_chapter_numbers(number: int | None) -> None:
    with pytest.raises(ValueError, match="chapter number"):
        chapter_directory_name(number)


@pytest.mark.parametrize("number", [0, -1, 1000])
def test_invalid_chunk_numbers(number: int) -> None:
    with pytest.raises(ValueError, match="chunk sequence"):
        chunk_directory_name(number)


def test_paths_are_deterministic_and_have_no_creation_side_effect(tmp_path: Path) -> None:
    paths = ProjectPaths(tmp_path)
    expected = tmp_path / "workspace/chapters/000-prologue/chunks/002/translation.md"

    assert paths.chunk_translation(None, 2, prologue=True) == expected
    assert paths.chunk_translation(None, 2, prologue=True) == expected
    assert paths.chapter_metadata(42) == tmp_path / "workspace/chapters/042/metadata.yaml"
    assert paths.book_metadata == tmp_path / "workspace/metadata.yaml"
    assert paths.toc == tmp_path / "workspace/toc.yaml"
    assert paths.snapshot == tmp_path / "workspace/snapshot.yaml"
    assert paths.chapter_chunks(42) == tmp_path / "workspace/chapters/042/chunks"
    assert (
        paths.chapter_chunking_report(42)
        == tmp_path / "workspace/chapters/042/chunking-report.yaml"
    )
    assert not paths.workspace.exists()


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ChapterStatus.DISCOVERED, ChapterStatus.EXTRACTED),
        (ChapterStatus.NORMALIZED, ChapterStatus.VALIDATED),
        (ChapterStatus.TRANSLATED, ChapterStatus.REVIEW_REQUIRED),
        (ChapterStatus.REVIEWED, ChapterStatus.APPROVED),
    ],
)
def test_valid_chapter_transitions(current: ChapterStatus, target: ChapterStatus) -> None:
    assert can_transition_chapter(current, target)


def test_chapter_retry_requires_explicit_request() -> None:
    assert not can_transition_chapter(ChapterStatus.FAILED, ChapterStatus.NORMALIZED)
    assert can_transition_chapter(
        ChapterStatus.FAILED, ChapterStatus.NORMALIZED, explicit_retry=True
    )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ChapterStatus.TRANSLATED, ChapterStatus.APPROVED),
        (ChapterStatus.EXTRACTED, ChapterStatus.APPROVED),
        (ChapterStatus.APPROVED, ChapterStatus.TRANSLATING),
    ],
)
def test_forbidden_chapter_transitions(current: ChapterStatus, target: ChapterStatus) -> None:
    assert not can_transition_chapter(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ChunkStatus.PENDING, ChunkStatus.TRANSLATING),
        (ChunkStatus.TRANSLATING, ChunkStatus.TRANSLATED),
        (ChunkStatus.TRANSLATED, ChunkStatus.REVIEW_REQUIRED),
        (ChunkStatus.REVIEWED, ChunkStatus.APPROVED),
    ],
)
def test_valid_chunk_transitions(current: ChunkStatus, target: ChunkStatus) -> None:
    assert can_transition_chunk(current, target)


def test_chunk_retry_requires_explicit_request() -> None:
    assert not can_transition_chunk(ChunkStatus.FAILED, ChunkStatus.PENDING)
    assert can_transition_chunk(ChunkStatus.FAILED, ChunkStatus.PENDING, explicit_retry=True)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ChunkStatus.PENDING, ChunkStatus.APPROVED),
        (ChunkStatus.TRANSLATED, ChunkStatus.APPROVED),
        (ChunkStatus.APPROVED, ChunkStatus.TRANSLATING),
    ],
)
def test_forbidden_chunk_transitions(current: ChunkStatus, target: ChunkStatus) -> None:
    assert not can_transition_chunk(current, target)


def test_model_serialization_round_trip_preserves_persian() -> None:
    model = BookWorkspaceMetadata(
        schema_version=SCHEMA_VERSION,
        book_id="test-book",
        source_pdf=Path("input/book.pdf"),
        source_pdf_sha256=HASH,
        title_en="Test Book",
        title_fa="کتاب آزمایشی",
        author="Author",
        source_language="en",
        target_language="fa",
        expected_output_format="epub3",
        created_at=NOW,
        updated_at=NOW,
    )
    serialized = model.model_dump_json()

    restored = BookWorkspaceMetadata.model_validate_json(serialized)

    assert restored == model
    assert "کتاب آزمایشی" in serialized


def test_representative_contract_models_validate() -> None:
    toc = TocDocument(
        schema_version=SCHEMA_VERSION,
        entries=[
            TocEntry(
                entry_id="001",
                level=1,
                kind=TocKind.CHAPTER,
                number=1,
                title_en="One",
                start_page=10,
                end_page=20,
                source_toc_title="Chapter One",
            )
        ],
    )
    chapter = ChapterMetadata(
        schema_version=SCHEMA_VERSION,
        chapter_id="001",
        kind=TocKind.CHAPTER,
        number=1,
        source_start_page=10,
        source_end_page=20,
        source_file=Path("workspace/chapters/001/source.md"),
        cleaned_file=Path("workspace/chapters/001/cleaned.md"),
        status=ChapterStatus.DISCOVERED,
        extraction_version="1",
        normalization_version="1",
        created_at=NOW,
        updated_at=NOW,
    )
    chunk = ChunkMetadata(
        schema_version=SCHEMA_VERSION,
        chunk_id="001-001",
        chapter_id="001",
        sequence=1,
        source_file=Path("workspace/chapters/001/chunks/001/source.md"),
        source_sha256=HASH,
        context_file=Path("workspace/chapters/001/chunks/001/context.md"),
        translation_file=Path("workspace/chapters/001/chunks/001/translation.md"),
        source_word_count=10,
        source_paragraph_count=1,
        status=ChunkStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
    )
    snapshot = ExtractionSnapshot(
        schema_version=SCHEMA_VERSION,
        snapshot_id="snapshot-001",
        book_id="test-book",
        source_pdf_sha256=HASH,
        toc_sha256=HASH,
        extraction_version="1",
        normalization_version="1",
        chapter_count=1,
        chapter_hashes={"001": ChapterSnapshotHashes(source_sha256=HASH, cleaned_sha256=HASH)},
        created_at=NOW,
        validated=True,
    )

    assert toc.entries[0].start_page == 10
    assert chapter.status is ChapterStatus.DISCOVERED
    assert chunk.status is ChunkStatus.PENDING
    assert snapshot.chapter_hashes["001"].cleaned_sha256 == HASH


def test_enums_have_frozen_values() -> None:
    assert {status.value for status in ChapterStatus} == {
        "discovered",
        "extracted",
        "normalized",
        "validated",
        "chunked",
        "translating",
        "translated",
        "review_required",
        "reviewed",
        "approved",
        "failed",
    }
    assert {status.value for status in ChunkStatus} == {
        "pending",
        "translating",
        "translated",
        "validation_failed",
        "review_required",
        "reviewed",
        "approved",
        "failed",
    }
    assert {status.value for status in PipelineRunStatus} == {
        "not_started",
        "running",
        "completed",
        "completed_with_warnings",
        "failed",
    }


def test_committed_json_schemas_match_models() -> None:
    schemas: dict[str, type[BaseModel]] = {
        "book-metadata.schema.json": BookWorkspaceMetadata,
        "toc.schema.json": TocDocument,
        "chapter-metadata.schema.json": ChapterMetadata,
        "chunk-metadata.schema.json": ChunkMetadata,
        "snapshot.schema.json": ExtractionSnapshot,
    }
    root = Path(__file__).parents[1]
    for filename, model in schemas.items():
        committed = json.loads((root / "schemas" / filename).read_text(encoding="utf-8"))
        assert committed == model.model_json_schema()
        assert "schema_version" in committed["properties"]
        assert "schema_version" in committed["required"]


def test_sha256_file_hashes_bytes_without_normalizing(tmp_path: Path) -> None:
    content = "فارسی\r\nEnglish\n".encode()
    path = tmp_path / "artifact.md"
    path.write_bytes(content)

    assert sha256_file(path, block_size=3) == hashlib.sha256(content).hexdigest()


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError):
        BookWorkspaceMetadata(
            schema_version=SCHEMA_VERSION,
            book_id="test-book",
            source_pdf=Path("input/book.pdf"),
            source_pdf_sha256=HASH,
            title_en="Test",
            title_fa="آزمایش",
            author="Author",
            source_language="en",
            target_language="fa",
            expected_output_format="epub3",
            created_at=datetime(2026, 1, 1),
            updated_at=NOW,
        )
