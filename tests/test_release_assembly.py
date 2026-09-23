"""Release assembly: a completed run becomes a canonical book, or it does not become one.

Every fixture here is synthetic. The "Persian" used below is real Persian script written for the
test; no book prose appears in this repository.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.document_chunking.context import build_context
from book_translator.document_chunking.paragraphs import parse_document
from book_translator.models import (
    BOOK_METADATA_FIELDS,
    BookMetadataFieldProvenance,
    BookMetadataProvenance,
    BookWorkspaceMetadata,
    ChunkMetadata,
    ChunkStatus,
    ReleaseManifest,
    ReleaseProviderResolution,
    TranslationAcceptanceStatus,
    TranslationJob,
    TranslationJobStatus,
    TranslationRunStatus,
)
from book_translator.paths import ProjectPaths
from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
from book_translator.release_assembly import (
    BOOK_FILENAME,
    LINEAGE_FILENAME,
    MANIFEST_FILENAME,
    METADATA_FILENAME,
    TERMINOLOGY_FILENAME,
    TRANSLATION_REPORT_FILENAME,
    ReleaseAssemblyError,
    assemble_book,
    assemble_chapter,
    chapter_filename,
    chapter_heading,
    chunk_paragraphs,
    execute_release_assembly,
    plan_release_assembly,
    validate_chunk_coverage,
    validate_release_id,
    verify_release,
)
from book_translator.translation_queue import create_run, load_run_summary, plan_run
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import run_translation_worker
from tests.test_translation_auto_repair import ScriptedProvider
from tests.test_translation_queue import NOW, SNAPSHOT_NOW
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import _options, _run_path

runner = CliRunner()

#: Two Persian paragraphs matching the synthetic source's two-paragraph shape.
TRANSLATION = "الف و بتا.\n\nگاما و دلتا.\n"
#: The same content split the way a two-chunk chapter needs it.
FIRST_HALF = "الف و بتا.\n"
SECOND_HALF = "گاما و دلتا.\n"
RELEASE_CLOCK = datetime(2026, 3, 4, 5, 6, tzinfo=UTC)


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _split_chapter_into_two_chunks(root: Path, chapter_id: str) -> None:
    """Rewrite one synthetic chapter as two chunks without changing its cleaned bytes.

    Chunk ordering can only be tested where more than one chunk exists, and the snapshot engine
    verifies conservation, so the split has to reconstruct ``cleaned.md`` exactly.
    """
    directory = root / "workspace/chapters" / chapter_id
    cleaned = (directory / "cleaned.md").read_text(encoding="utf-8")
    paragraphs = parse_document(cleaned)
    assert len(paragraphs) == 2
    chapter_metadata = yaml.safe_load((directory / "metadata.yaml").read_text(encoding="utf-8"))
    cleaned_hash = chapter_metadata["cleaned_sha256"]

    for sequence, paragraph in enumerate(paragraphs, start=1):
        chunk_directory = directory / "chunks" / f"{sequence:03d}"
        chunk_directory.mkdir(parents=True, exist_ok=True)
        source = f"{paragraph.text}\n"
        context = build_context(paragraphs, current_start=sequence, limit_words=2)
        (chunk_directory / "source.md").write_text(source, encoding="utf-8")
        (chunk_directory / "context.md").write_text(context.text, encoding="utf-8")
        metadata = ChunkMetadata(
            schema_version="1.0",
            chunk_id=f"{chapter_id}-{sequence:03d}",
            chapter_id=chapter_id,
            sequence=sequence,
            source_file=Path(f"workspace/chapters/{chapter_id}/chunks/{sequence:03d}/source.md"),
            source_sha256=_hash(source.encode("utf-8")),
            context_file=Path(f"workspace/chapters/{chapter_id}/chunks/{sequence:03d}/context.md"),
            context_sha256=_hash(context.text.encode("utf-8")),
            context_word_count=context.word_count,
            translation_file=Path(
                f"workspace/chapters/{chapter_id}/chunks/{sequence:03d}/translation.md"
            ),
            source_word_count=paragraph.word_count,
            source_paragraph_count=1,
            status=ChunkStatus.PENDING,
            chunk_engine_version="1.0.0",
            cleaned_source_sha256=cleaned_hash,
            created_at=NOW,
            updated_at=NOW,
        )
        (chunk_directory / "metadata.yaml").write_text(
            yaml.safe_dump(
                metadata.model_dump(mode="json", exclude_none=False),
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    chapter_metadata["chunk_count"] = 2
    (directory / "metadata.yaml").write_text(
        yaml.safe_dump(chapter_metadata, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    report = yaml.safe_load((directory / "chunking-report.yaml").read_text(encoding="utf-8"))
    report["chunk_count"] = 2
    (directory / "chunking-report.yaml").write_text(
        yaml.safe_dump(report, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _write_book_metadata(root: Path) -> None:
    """Give the synthetic project a resolved canonical book record and its frozen copy.

    A release publishes the *frozen* metadata the snapshot named, so the fixture has to freeze
    one; without it the synthetic project falls back to configuration and there is nothing to
    prove was copied.
    """
    resolved = {
        "author": BookMetadataProvenance.CONFIGURATION,
        "book_id": BookMetadataProvenance.CONFIGURATION,
        "source_language": BookMetadataProvenance.CONFIGURATION,
        "target_language": BookMetadataProvenance.CONFIGURATION,
        "title_en": BookMetadataProvenance.CONFIGURATION,
        "title_fa": BookMetadataProvenance.CONFIGURATION,
    }
    metadata = BookWorkspaceMetadata(
        schema_version="1.0",
        metadata_version="1.0.0",
        book_id="synthetic-book",
        source_pdf=Path("input/book.pdf"),
        source_pdf_name="book.pdf",
        source_pdf_sha256=_hash((root / "input/book.pdf").read_bytes()),
        title_en="Synthetic Book",
        title_fa="کتاب ساختگی",
        author="Author",
        source_language="en",
        target_language="fa",
        expected_output_format="epub3",
        provenance=[
            BookMetadataFieldProvenance(
                field=field,
                provenance=resolved.get(field, BookMetadataProvenance.UNRESOLVED),
                evidence="config/book.yaml" if field in resolved else None,
            )
            for field in BOOK_METADATA_FIELDS
        ],
        created_at=NOW,
        updated_at=NOW,
    )
    serialized = yaml.safe_dump(
        metadata.model_dump(mode="json", exclude_none=False), allow_unicode=True, sort_keys=False
    )
    paths = ProjectPaths(root)
    paths.book_metadata.parent.mkdir(parents=True, exist_ok=True)
    paths.book_metadata.write_text(serialized, encoding="utf-8")
    frozen = paths.book_metadata_resource(_hash(serialized.encode("utf-8")))
    frozen.parent.mkdir(parents=True, exist_ok=True)
    frozen.write_text(serialized, encoding="utf-8")


def _prepared_run(root: Path, *, chapters: int = 1, split_first_chapter: bool = False) -> str:
    root.mkdir(parents=True, exist_ok=True)
    synthetic_project(root, chapters=chapters)
    _write_book_metadata(root)
    if split_first_chapter:
        _split_chapter_into_two_chunks(root, "001")
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    created = create_run(plan_run(snapshot.snapshot_id, project_root=root, now=lambda: NOW))
    execute_prompt_packages(
        plan_prompt_packages(created.run_id, project_root=root, now=lambda: NOW)
    )
    return created.run_id


def _translate(root: Path, run_id: str, texts: tuple[str, ...], *, jobs: int = 1) -> None:
    provider = ScriptedProvider(texts)
    run_translation_worker(
        run_id,
        project_root=root,
        options=_options(max_jobs=jobs),
        provider_factory=lambda _job: provider,
        now=lambda: NOW + timedelta(seconds=10),
    )


def _accepted_run(root: Path, *, chapters: int = 1, split_first_chapter: bool = False) -> str:
    """A completed run whose every job is an accepted translation."""
    run_id = _prepared_run(root, chapters=chapters, split_first_chapter=split_first_chapter)
    if split_first_chapter:
        # Each half-chapter chunk holds exactly one source paragraph.
        _translate(root, run_id, (FIRST_HALF, SECOND_HALF), jobs=chapters + 1)
    else:
        _translate(root, run_id, (TRANSLATION,), jobs=chapters)
    return run_id


def _export(root: Path, run_id: str, name: str = "book-fa-v1", **updates: object) -> Path:
    options: dict[str, object] = {
        "output": f"workspace/releases/{name}",
        "project_root": root,
        "now": lambda: RELEASE_CLOCK,
    }
    options.update(updates)
    plan = plan_release_assembly(run_id, **options)  # type: ignore[arg-type]
    execute_release_assembly(plan, project_root=root)
    return plan.output_path


def _workspace_fingerprint(root: Path) -> dict[str, str]:
    """Hash every workspace byte except the release directory itself."""
    releases = (root / "workspace/releases").resolve()
    fingerprint: dict[str, str] = {}
    for path in sorted((root / "workspace").rglob("*")):
        if path.is_file() and releases not in path.resolve().parents:
            fingerprint[path.relative_to(root).as_posix()] = _hash(path.read_bytes())
    return fingerprint


def _manifest(release: Path) -> ReleaseManifest:
    return ReleaseManifest.model_validate(
        yaml.safe_load((release / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    )


# --------------------------------------------------------------------------------------------
# Pure engine
# --------------------------------------------------------------------------------------------


def test_chapter_filenames_sort_into_reading_order_and_name_their_kind() -> None:
    from book_translator.models.book import TocKind

    assert chapter_filename(kind=TocKind.PROLOGUE, number=0, chapter_id="000-prologue") == (
        "000-prologue.md"
    )
    assert chapter_filename(kind=TocKind.CHAPTER, number=1, chapter_id="001") == "001-chapter.md"
    assert chapter_filename(kind=TocKind.CHAPTER, number=57, chapter_id="057") == "057-chapter.md"
    assert (
        chapter_filename(kind=TocKind.FRONT_MATTER, number=None, chapter_id="front-matter-1")
        == "front-matter-1.md"
    )


def test_a_blank_target_title_falls_back_to_the_source_title_and_says_so() -> None:
    from book_translator.models.release import ReleaseHeadingSource

    assert chapter_heading(title_target="یک", title_source="One") == (
        "# یک",
        ReleaseHeadingSource.TITLE_TARGET,
    )
    assert chapter_heading(title_target="  ", title_source="One") == (
        "# One",
        ReleaseHeadingSource.TITLE_SOURCE,
    )
    with pytest.raises(ReleaseAssemblyError):
        chapter_heading(title_target="", title_source="")


def test_release_identifiers_must_be_filesystem_safe() -> None:
    assert validate_release_id("book5-fa-v1") == "book5-fa-v1"
    for rejected in ("Book5", "../escape", "book5/fa", "", "release.md", "x" * 65):
        with pytest.raises(ReleaseAssemblyError):
            validate_release_id(rejected)


def test_the_book_is_byte_identical_to_its_chapters_joined() -> None:
    first, _ = assemble_chapter(
        chapter_id="001", heading="# One", chunk_texts=(("001-001", TRANSLATION),)
    )
    second, _ = assemble_chapter(
        chapter_id="002", heading="# Two", chunk_texts=(("002-001", TRANSLATION),)
    )

    assert assemble_book((first, second)) == "\n".join((first, second))


# --------------------------------------------------------------------------------------------
# 1. A completed run exports successfully
# --------------------------------------------------------------------------------------------


def test_a_completed_run_exports_a_canonical_release(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root, chapters=2)

    release = _export(root, run_id)

    assert (release / BOOK_FILENAME).is_file()
    assert (release / MANIFEST_FILENAME).is_file()
    assert (release / TRANSLATION_REPORT_FILENAME).is_file()
    assert (release / LINEAGE_FILENAME).is_file()
    manifest = _manifest(release)
    assert manifest.release_id == "book-fa-v1"
    assert manifest.source_translation_run == run_id
    assert manifest.chapter_count == 2
    assert manifest.chunk_count == manifest.accepted_translation_count == 2
    chapter_files = [record.file for record in manifest.chapters]
    assert chapter_files == [Path("chapters/001-chapter.md"), Path("chapters/002-chapter.md")]
    assert all((release / file).is_file() for file in chapter_files)
    book = (release / BOOK_FILENAME).read_text(encoding="utf-8")
    assert book == "\n".join((release / file).read_text(encoding="utf-8") for file in chapter_files)
    report = yaml.safe_load((release / TRANSLATION_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert report["final_status"] == "released"
    assert report["accepted_chunks"] == report["total_chunks"] == 2
    assert report["qa_rejected_chunks"] == 0


def test_a_dry_run_renders_the_whole_release_and_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root)

    plan = plan_release_assembly(
        run_id,
        output="workspace/releases/book-fa-v1",
        project_root=root,
        now=lambda: RELEASE_CLOCK,
    )

    assert plan.chunk_count == 1
    assert {file.relative_path for file in plan.files} >= {BOOK_FILENAME, METADATA_FILENAME}
    assert not (root / "workspace/releases").exists()


# --------------------------------------------------------------------------------------------
# 2. An incomplete run fails
# --------------------------------------------------------------------------------------------


def test_an_incomplete_run_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _prepared_run(root, chapters=2)
    _translate(root, run_id, (TRANSLATION,), jobs=1)
    state = load_run_summary(run_id, project_root=root)

    assert state.run.status is not TranslationRunStatus.COMPLETED
    assert state.run.pending_count == 1
    with pytest.raises(ReleaseAssemblyError, match="completed"):
        plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)
    assert not (root / "workspace/releases").exists()


# --------------------------------------------------------------------------------------------
# 3. A QA-rejected run fails
# --------------------------------------------------------------------------------------------


def test_a_qa_rejected_run_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _prepared_run(root)
    # One paragraph against a two-paragraph source: structural QA rejects it.
    _translate(root, run_id, ("الف و بتا و گاما و دلتا.\n",))
    state = load_run_summary(run_id, project_root=root)

    assert state.run.qa_rejected_count == 1
    assert state.jobs[0].acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    with pytest.raises(ReleaseAssemblyError):
        plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)
    assert not (root / "workspace/releases").exists()


def test_a_run_forced_to_look_complete_while_a_job_is_rejected_is_still_refused(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _prepared_run(root)
    _translate(root, run_id, ("الف و بتا و گاما و دلتا.\n",))
    run_path = _run_path(root, run_id) / "run.yaml"
    run = yaml.safe_load(run_path.read_text(encoding="utf-8"))
    run["status"] = TranslationRunStatus.COMPLETED.value
    run_path.write_text(yaml.safe_dump(run, allow_unicode=True, sort_keys=False), encoding="utf-8")

    with pytest.raises(ReleaseAssemblyError):
        plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)


# --------------------------------------------------------------------------------------------
# 4. A missing translation artifact fails
# --------------------------------------------------------------------------------------------


def test_a_missing_translation_artifact_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root)
    job = load_run_summary(run_id, project_root=root).jobs[0]
    evidence = yaml.safe_load((root / str(job.acceptance_file)).read_text(encoding="utf-8"))
    (root / evidence["translation_file"]).unlink()

    with pytest.raises(ReleaseAssemblyError, match="missing"):
        plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)


def test_a_tampered_translation_artifact_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root)
    job = load_run_summary(run_id, project_root=root).jobs[0]
    evidence = yaml.safe_load((root / str(job.acceptance_file)).read_text(encoding="utf-8"))
    (root / evidence["translation_file"]).write_text("دیگری.\n\nمتفاوت.\n", encoding="utf-8")

    with pytest.raises(ReleaseAssemblyError, match="hash mismatch"):
        plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)


# --------------------------------------------------------------------------------------------
# 5. Chunk ordering is deterministic
# --------------------------------------------------------------------------------------------


def test_chunk_ordering_is_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root, chapters=1, split_first_chapter=True)

    release = _export(root, run_id)

    manifest = _manifest(release)
    assert manifest.chunk_count == 2
    chapter = (release / manifest.chapters[0].file).read_text(encoding="utf-8")
    assert chapter == f"# Synthetic\n\n{FIRST_HALF.strip()}\n\n{SECOND_HALF.strip()}\n"
    lineage = json.loads((release / LINEAGE_FILENAME).read_text(encoding="utf-8"))
    assert [chunk["chunk_id"] for chunk in lineage["chunks"]] == ["001-001", "001-002"]
    assert [chunk["chunk_sequence"] for chunk in lineage["chunks"]] == [1, 2]
    assert [chunk["paragraph_offset"] for chunk in lineage["chunks"]] == [2, 3]


def test_chunk_order_is_proven_and_not_assumed() -> None:
    validate_chunk_coverage(("a-001", "a-002"), ("a-001", "a-002"))
    with pytest.raises(ReleaseAssemblyError, match="frozen chunk order"):
        validate_chunk_coverage(("a-001", "a-002"), ("a-002", "a-001"))


# --------------------------------------------------------------------------------------------
# 6. Chapter ordering is deterministic
# --------------------------------------------------------------------------------------------


def test_chapter_ordering_is_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root, chapters=3)

    manifest = _manifest(_export(root, run_id))

    assert [record.chapter_id for record in manifest.chapters] == ["001", "002", "003"]
    assert [record.chapter_sequence for record in manifest.chapters] == [1, 2, 3]
    assert [record.file.name for record in manifest.chapters] == [
        "001-chapter.md",
        "002-chapter.md",
        "003-chapter.md",
    ]


# --------------------------------------------------------------------------------------------
# 7. Repeated export produces identical hashes
# --------------------------------------------------------------------------------------------


def test_repeating_an_export_reproduces_every_content_hash(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root, chapters=2)

    first = _manifest(_export(root, run_id))
    second = _manifest(
        _export(root, run_id, force=True, now=lambda: RELEASE_CLOCK + timedelta(days=1))
    )

    assert first.content_sha256 == second.content_sha256
    assert [artifact.model_dump() for artifact in first.artifacts] == [
        artifact.model_dump() for artifact in second.artifacts
    ]
    assert first.created_at != second.created_at


def test_an_existing_release_is_never_replaced_without_force(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root)
    release = _export(root, run_id)

    with pytest.raises(ReleaseAssemblyError, match="--force"):
        execute_release_assembly(
            plan_release_assembly(
                run_id, output="workspace/releases/book-fa-v1", project_root=root
            ),
            project_root=root,
        )
    assert (release / MANIFEST_FILENAME).is_file()


def test_force_refuses_a_directory_that_is_not_this_release(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root)
    foreign = root / "workspace/releases/book-fa-v1"
    foreign.mkdir(parents=True)
    (foreign / "notes.txt").write_text("not a release\n", encoding="utf-8")

    with pytest.raises(ReleaseAssemblyError, match="not a release directory"):
        execute_release_assembly(
            plan_release_assembly(
                run_id,
                output="workspace/releases/book-fa-v1",
                project_root=root,
                force=True,
            ),
            project_root=root,
        )
    assert (foreign / "notes.txt").read_text(encoding="utf-8") == "not a release\n"


def test_a_release_may_never_be_written_into_canonical_workspace_state(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root)

    for destination in (
        "workspace/chapters/001",
        "workspace/translation-runs/anything",
        "workspace/snapshots/translation",
        "workspace/resources/terminology",
    ):
        with pytest.raises(ReleaseAssemblyError, match="canonical workspace state"):
            plan_release_assembly(run_id, output=destination, project_root=root)


# --------------------------------------------------------------------------------------------
# 8. Release manifest validation
# --------------------------------------------------------------------------------------------


def test_the_published_manifest_indexes_exactly_what_was_published(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _export(root, _accepted_run(root, chapters=2))

    assert verify_release(release) == ()
    manifest = _manifest(release)
    indexed = {artifact.file.as_posix() for artifact in manifest.artifacts}
    published = {
        path.relative_to(release).as_posix()
        for path in release.rglob("*")
        if path.is_file() and path.name != MANIFEST_FILENAME
    }
    assert indexed == published
    for artifact in manifest.artifacts:
        content = (release / artifact.file).read_bytes()
        assert _hash(content) == artifact.sha256
        assert len(content) == artifact.byte_count


def test_a_manifest_detects_a_release_file_that_changed_after_publication(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _export(root, _accepted_run(root))
    (release / BOOK_FILENAME).chmod(0o600)
    (release / BOOK_FILENAME).write_text("دستکاری شده.\n", encoding="utf-8")

    assert verify_release(release) == ("hash mismatch: book.md",)


# --------------------------------------------------------------------------------------------
# 9. Provenance completeness
# --------------------------------------------------------------------------------------------


def test_provenance_accounts_for_every_released_chunk(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root, chapters=2)
    release = _export(root, run_id)

    lineage = json.loads((release / LINEAGE_FILENAME).read_text(encoding="utf-8"))
    manifest = _manifest(release)
    assert lineage["chunk_count"] == manifest.chunk_count == len(lineage["chunks"])
    assert lineage["source_translation_run"] == run_id
    assert lineage["snapshot_id"] == manifest.snapshot_id
    for chunk in lineage["chunks"]:
        for field in (
            "chapter_id",
            "chunk_id",
            "source_sha256",
            "translation_sha256",
            "acceptance_origin",
            "acceptance_evidence_sha256",
            "attempt_id",
            "attempt_sha256",
            "provider",
        ):
            assert chunk[field], f"{field} is missing from chunk provenance"
        # QA versions are recorded when the evidence carries them and are never invented.
        assert "terminology_qa_version" in chunk
        assert "structural_qa_version" in chunk
        assert chunk["acceptance_status"] == "accepted"
        assert chunk["provider_resolution"] == ReleaseProviderResolution.ATTEMPT.value


def test_provenance_stores_no_prompt_or_response_material(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _export(root, _accepted_run(root))

    lineage = (release / LINEAGE_FILENAME).read_text(encoding="utf-8")
    assert TRANSLATION.split("\n")[0] not in lineage
    assert "Alpha beta." not in lineage
    assert "request" not in lineage and "response" not in lineage


# --------------------------------------------------------------------------------------------
# 10. No mutation of the source workspace
# --------------------------------------------------------------------------------------------


def test_export_mutates_no_byte_outside_the_release_directory(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root, chapters=2)
    before = _workspace_fingerprint(root)

    _export(root, run_id)
    _export(root, run_id, force=True)

    assert _workspace_fingerprint(root) == before


def test_a_dry_run_creates_nothing_at_all(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root)
    before = _workspace_fingerprint(root)

    plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)

    assert _workspace_fingerprint(root) == before
    assert not (root / "workspace/releases").exists()


# --------------------------------------------------------------------------------------------
# 11. No provider invocation
# --------------------------------------------------------------------------------------------


def test_the_release_stage_holds_no_path_to_a_provider() -> None:
    package = Path(__file__).parents[1] / "src/book_translator/release_assembly"
    sources = {path.name: path.read_text(encoding="utf-8") for path in package.glob("*.py")}

    assert sources
    for name, source in sources.items():
        assert "provider_execution" not in source, f"{name} reaches the provider boundary"
        assert "TranslationProvider" not in source, f"{name} names a provider adapter"


def test_an_export_succeeds_while_every_provider_adapter_is_sabotaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root)

    from book_translator.provider_execution import providers

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("release assembly contacted a provider")

    for adapter in (
        "ClaudeCodeTranslationProvider",
        "CodexCliTranslationProvider",
        "FakeTranslationProvider",
        "OpenAIResponsesProvider",
    ):
        monkeypatch.setattr(providers, adapter, refuse)

    release = _export(root, run_id)

    assert (release / MANIFEST_FILENAME).is_file()


# --------------------------------------------------------------------------------------------
# 12. Terminology and metadata are copied immutably
# --------------------------------------------------------------------------------------------


def test_frozen_resources_are_copied_byte_for_byte(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root)
    release = _export(root, run_id)
    manifest = _manifest(release)
    snapshot_manifest = yaml.safe_load(
        (ProjectPaths(root).translation_snapshot(manifest.snapshot_id) / "manifest.yaml").read_text(
            encoding="utf-8"
        )
    )

    frozen_metadata = snapshot_manifest["book"]["frozen_metadata_file"]
    published = (release / METADATA_FILENAME).read_bytes()
    assert published == (root / frozen_metadata).read_bytes()
    assert manifest.book_metadata_sha256 == snapshot_manifest["book"]["metadata_sha256"]
    assert _hash(published) == manifest.book_metadata_sha256

    terminology = snapshot_manifest.get("terminology")
    if terminology is not None:
        copied = (release / TERMINOLOGY_FILENAME).read_bytes()
        assert copied == (root / terminology["frozen_file"]).read_bytes()
        assert _hash(copied) == manifest.terminology_sha256 == terminology["sha256"]
        assert manifest.terminology_version == terminology["version"]


# --------------------------------------------------------------------------------------------
# 13. Persian UTF-8 is preserved
# --------------------------------------------------------------------------------------------


def test_persian_text_reaches_the_release_byte_for_byte(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root)
    job = load_run_summary(run_id, project_root=root).jobs[0]
    evidence = yaml.safe_load((root / str(job.acceptance_file)).read_text(encoding="utf-8"))
    accepted = (root / evidence["translation_file"]).read_bytes()

    release = _export(root, run_id)

    chapter = (release / _manifest(release).chapters[0].file).read_bytes()
    for paragraph in parse_document(accepted.decode("utf-8")):
        assert paragraph.text.encode("utf-8") in chapter
    assert accepted.decode("utf-8") == TRANSLATION
    assert chapter.decode("utf-8") == f"# Synthetic\n\n{TRANSLATION}"


# --------------------------------------------------------------------------------------------
# 14. Empty chapter handling
# --------------------------------------------------------------------------------------------


def test_an_empty_chapter_is_refused_rather_than_published_blank() -> None:
    with pytest.raises(ReleaseAssemblyError, match="no accepted chunks"):
        assemble_chapter(chapter_id="001", heading="# One", chunk_texts=())
    with pytest.raises(ReleaseAssemblyError, match="canonical Markdown"):
        chunk_paragraphs("001-001", "")
    with pytest.raises(ReleaseAssemblyError, match="canonical Markdown"):
        chunk_paragraphs("001-001", "   \n")
    with pytest.raises(ReleaseAssemblyError, match="at least one chapter"):
        assemble_book(())


def test_an_empty_accepted_translation_stops_the_export(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root)
    job = load_run_summary(run_id, project_root=root).jobs[0]
    evidence_path = root / str(job.acceptance_file)
    evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8"))
    translation = root / evidence["translation_file"]
    translation.chmod(0o600)
    translation.write_text("", encoding="utf-8")
    # Keep the acceptance sidecar honest about the bytes so the *emptiness* is what is refused.
    evidence["translation_sha256"] = _hash(b"")
    evidence["terminology_report"]["translation_sha256"] = _hash(b"")
    evidence["structural_report"]["translation_sha256"] = _hash(b"")
    evidence_path.chmod(0o600)
    serialized = yaml.safe_dump(evidence, allow_unicode=True, sort_keys=False)
    evidence_path.write_text(serialized, encoding="utf-8")
    job_path = _run_path(root, run_id) / "jobs" / job.chunk_id / "job.yaml"
    stored = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    stored["acceptance_sha256"] = _hash(serialized.encode("utf-8"))
    job_path.write_text(
        yaml.safe_dump(stored, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ReleaseAssemblyError, match="canonical Markdown"):
        plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)


# --------------------------------------------------------------------------------------------
# 15 and 16. Missing and duplicate chunk detection
# --------------------------------------------------------------------------------------------


def test_a_missing_chunk_is_named_as_missing() -> None:
    with pytest.raises(ReleaseAssemblyError, match="missing chunks: a-002"):
        validate_chunk_coverage(("a-001", "a-002"), ("a-001",))


def test_a_missing_job_stops_the_export(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root, chapters=2)
    import shutil

    shutil.rmtree(_run_path(root, run_id) / "jobs/002-001")

    with pytest.raises(ReleaseAssemblyError):
        plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)


def test_a_duplicate_chunk_is_told_apart_from_a_missing_one() -> None:
    with pytest.raises(ReleaseAssemblyError, match="repeats chunks: a-001"):
        validate_chunk_coverage(("a-001", "a-002"), ("a-001", "a-001", "a-002"))
    with pytest.raises(ReleaseAssemblyError, match="never froze"):
        validate_chunk_coverage(("a-001",), ("a-001", "a-002"))


def test_a_duplicated_job_stops_the_export(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root, chapters=2)
    jobs = _run_path(root, run_id) / "jobs"
    duplicate = yaml.safe_load((jobs / "001-001/job.yaml").read_text(encoding="utf-8"))
    (jobs / "002-001/job.yaml").write_text(
        yaml.safe_dump(duplicate, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ReleaseAssemblyError):
        plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def test_the_cli_exports_a_release_and_reports_zero_provider_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root, chapters=2)
    monkeypatch.chdir(root)

    result = runner.invoke(
        app,
        [
            "export-translation-run",
            run_id,
            "--output",
            "workspace/releases/book5-fa-v1",
            "--show-manifest",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "Provider contacted: no" in result.stdout
    assert "mutations: 0" in result.stdout
    assert (root / "workspace/releases/book5-fa-v1" / MANIFEST_FILENAME).is_file()


def test_the_cli_dry_run_writes_nothing_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    run_id = _accepted_run(root)
    monkeypatch.chdir(root)

    result = runner.invoke(
        app,
        [
            "export-translation-run",
            run_id,
            "--output",
            "workspace/releases/book5-fa-v1",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "Dry run: yes" in result.stdout
    assert "Files written: 0" in result.stdout
    assert not (root / "workspace/releases").exists()


def test_the_cli_refuses_an_unreleasable_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    run_id = _prepared_run(root, chapters=2)
    _translate(root, run_id, (TRANSLATION,), jobs=1)
    monkeypatch.chdir(root)

    result = runner.invoke(
        app,
        ["export-translation-run", run_id, "--output", "workspace/releases/book5-fa-v1"],
    )

    assert result.exit_code == 1
    assert not (root / "workspace/releases").exists()


def test_a_released_job_carries_its_accepted_attempt_forward(tmp_path: Path) -> None:
    """The lineage names the attempt the queue accepted, not merely the latest one."""
    root = tmp_path / "project"
    run_id = _accepted_run(root)
    job: TranslationJob = load_run_summary(run_id, project_root=root).jobs[0]
    release = _export(root, run_id)

    lineage = json.loads((release / LINEAGE_FILENAME).read_text(encoding="utf-8"))
    record = lineage["chunks"][0]
    assert job.status is TranslationJobStatus.SUCCEEDED
    assert record["job_id"] == job.job_id
    assert record["attempt_number"] == job.accepted_attempt
    assert record["acceptance_evidence_file"] == str(job.acceptance_file)
    assert record["acceptance_evidence_sha256"] == job.acceptance_sha256
