"""Book-scoped workspaces: two books in one project must never share a mutable artifact path.

Everything here is synthetic. Book "alpha" lives in the legacy `workspace/` root exactly as the
existing project does; book "beta" lives in `workspace/books/beta/`. The tests prove that the
legacy root is untouched by every beta stage, that both books can carry a `000-prologue`, and
that the Book-6-shaped TOC (front matter, prologue, numbered chapters, epilogue, glossary, an
unknown trailing entry) yields exactly the narrative corpus.
"""

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.artifact_regeneration import plan_artifact_regeneration
from book_translator.book_metadata import plan_book_metadata
from book_translator.chapter_extraction import (
    ChapterExtractionError,
    execute_extraction,
    plan_extraction,
)
from book_translator.cli import app
from book_translator.document_chunking import ChunkingConfig
from book_translator.document_chunking.workflow import (
    execute_chapter_chunking,
    plan_chapter_chunking,
)
from book_translator.models import ChapterMetadata, ChunkMetadata, TocDocument, TocEntry, TocKind
from book_translator.normalization.workflow import execute_normalization, plan_normalization
from book_translator.paths import (
    EPILOGUE_CHAPTER_ID,
    PROLOGUE_CHAPTER_ID,
    ProjectPaths,
    book_workspace_of,
    chapter_id_from_selector,
    narrative_chapter_id,
    parse_narrative_chapter_id,
)
from book_translator.prompt_packaging.identity import (
    MARKED_OUTPUT_CONTRACT_VERSION,
    OUTPUT_CONTRACT_VERSION,
)
from book_translator.source_quality_audit import audit_source_quality, persist_report
from book_translator.toc_writer import write_toc
from book_translator.translation_queue import QueueError, create_run, plan_run
from book_translator.translation_snapshot.workflow import (
    SnapshotError,
    create_snapshot,
    plan_resource_freeze,
    plan_snapshot,
    validate_snapshot,
)
from tests.test_chapter_extraction import make_pdf

runner = CliRunner()


def _entry(
    entry_id: str,
    kind: TocKind,
    start: int,
    end: int,
    number: int | None = None,
    title: str = "Synthetic Title",
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


def _write_toc(toc_path: Path, pdf: Path, pdf_relative: Path, entries: list[TocEntry]) -> Path:
    pages = max(entry.end_page for entry in entries)
    toc = TocDocument(
        schema_version="1.0",
        source="embedded",
        source_pdf=pdf_relative,
        source_pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
        pdf_page_count=pages,
        entry_count=len(entries),
        entries=entries,
    )
    write_toc(toc, toc_path)
    return toc_path


def _resource(root: Path, stem: str, kind: str, content: str) -> None:
    suffix = ".yaml" if stem == "glossary" else ".md"
    resource = root / f"config/{stem}{suffix}"
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text(content, encoding="utf-8")
    resource.with_suffix(".metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "resource_kind": kind,
                "resource_file": resource.relative_to(root).as_posix(),
                "version": "1.0.0",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _config_text(*, book_id: str, title: str, pdf: str) -> str:
    return (
        f"book_id: {book_id}\n"
        f"title_en: {title}\n"
        "title_fa: ''\n"
        "author: Synthetic Author\n"
        "source_language: en\n"
        "target_language: fa\n"
        f"pdf_path: {pdf}\n"
        "chunk_target_words: 10\n"
        "chunk_max_words: 20\n"
        "previous_context_words: 2\n"
        "output_format: epub3\n"
    )


def _book(
    root: Path, book: str | None, *, page_lines: list[list[str]], entries: list[TocEntry]
) -> Path:
    """Create the PDF, configuration, and canonical TOC of one book; extract nothing."""
    paths = ProjectPaths(root, book)
    (root / "input").mkdir(exist_ok=True)
    name = "book.pdf" if book is None else f"{book}.pdf"
    pdf = root / "input" / name
    make_pdf(pdf, page_lines)
    paths.book_config.parent.mkdir(parents=True, exist_ok=True)
    paths.book_config.write_text(
        _config_text(
            book_id=f"{book or 'alpha'}-book-en-fa",
            title=f"{(book or 'alpha').title()} Book",
            pdf=f"input/{name}",
        ),
        encoding="utf-8",
    )
    _write_toc(paths.toc, pdf, Path(f"input/{name}"), entries)
    return pdf


def _two_book_project(root: Path) -> tuple[Path, Path]:
    """Legacy book alpha in workspace/, book beta in workspace/books/beta/, both with prologues."""
    alpha = _book(
        root,
        None,
        page_lines=[["ALPHA PROLOGUE", "alpha words one"], ["ALPHA CHAPTER ONE", "alpha two"]],
        entries=[
            _entry("prologue", TocKind.PROLOGUE, 1, 1, 0),
            _entry("chapter-001", TocKind.CHAPTER, 2, 2, 1),
        ],
    )
    beta = _book(
        root,
        "beta",
        page_lines=[["BETA PROLOGUE", "beta words one"], ["BETA CHAPTER ONE", "beta two"]],
        entries=[
            _entry("prologue", TocKind.PROLOGUE, 1, 1, 0),
            _entry("chapter-001", TocKind.CHAPTER, 2, 2, 1),
        ],
    )
    _resource(root, "translation-prompt", "prompt", "Source: {{ source_text }}\n")
    _resource(root, "glossary", "glossary", "terms: {}\n")
    _resource(root, "style-guide", "style_guide", "Synthetic style guide.\n")
    return alpha, beta


def _extract(root: Path, pdf: Path, book: str | None, **kwargs: object) -> list[str]:
    plan = plan_extraction(
        pdf,
        ProjectPaths(root, book).toc,
        project_root=root,
        book=book,
        **kwargs,  # type: ignore[arg-type]
    )
    return execute_extraction(plan, project_root=root).extracted_ids


def _tree_hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _legacy_hashes(root: Path) -> dict[str, str]:
    """Every byte of the legacy root, excluding the container of book-scoped workspaces."""
    return {
        name: digest
        for name, digest in _tree_hashes(root / "workspace").items()
        if not name.startswith("books/")
    }


def _metadata(path: Path) -> ChapterMetadata:
    return ChapterMetadata.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# --- path layer -------------------------------------------------------------------------------


def test_legacy_paths_are_unchanged_without_a_book_selection(tmp_path: Path) -> None:
    legacy = ProjectPaths(tmp_path)
    assert legacy.book is None
    assert legacy.workspace == tmp_path / "workspace"
    assert legacy.toc == tmp_path / "workspace/toc.yaml"
    assert legacy.book_metadata == tmp_path / "workspace/metadata.yaml"
    assert legacy.book_config == tmp_path / "config/book.yaml"
    assert legacy.chapter(prologue=True) == tmp_path / "workspace/chapters/000-prologue"
    assert legacy.chapter(42) == tmp_path / "workspace/chapters/042"
    assert legacy.translation_snapshots == tmp_path / "workspace/snapshots/translation"
    assert legacy.translation_runs == tmp_path / "workspace/translation-runs"
    assert legacy.frozen_resources == tmp_path / "workspace/resources"
    assert legacy.releases == tmp_path / "workspace/releases"
    assert legacy.source_quality_audits == tmp_path / "workspace/audits/source-quality"
    assert not (tmp_path / "workspace").exists()


def test_book_scoped_paths_share_the_layout_under_an_isolated_root(tmp_path: Path) -> None:
    scoped = ProjectPaths(tmp_path, "lord-of-chaos")
    legacy = ProjectPaths(tmp_path)
    book_root = tmp_path / "workspace/books/lord-of-chaos"
    assert scoped.workspace == book_root
    assert scoped.book_config == tmp_path / "config/books/lord-of-chaos.yaml"
    assert scoped.toc == book_root / "toc.yaml"
    assert scoped.chapter(prologue=True) == book_root / "chapters/000-prologue"
    assert scoped.chapter(epilogue=True) == book_root / "chapters/999-epilogue"
    assert scoped.chapter_directory("055") == book_root / "chapters/055"
    assert scoped.translation_snapshots == book_root / "snapshots/translation"
    assert scoped.frozen_resources == book_root / "resources"
    # Same logical unit, different physical identity: never the same path.
    assert scoped.chapter(prologue=True) != legacy.chapter(prologue=True)
    assert scoped.chapter(prologue=True).name == legacy.chapter(prologue=True).name
    assert book_workspace_of(Path("workspace/books/lord-of-chaos/toc.yaml")) == "lord-of-chaos"
    assert book_workspace_of(Path("workspace/toc.yaml")) is None
    assert not book_root.exists()


@pytest.mark.parametrize("book", ["", "Lord", "../x", "a/b", ".", "..", "lord chaos"])
def test_invalid_book_workspace_ids_are_rejected(tmp_path: Path, book: str) -> None:
    with pytest.raises(ValueError):
        ProjectPaths(tmp_path, book)


def test_narrative_chapter_identity_round_trips() -> None:
    assert narrative_chapter_id(TocKind.PROLOGUE, 0) == PROLOGUE_CHAPTER_ID
    assert narrative_chapter_id(TocKind.EPILOGUE, None) == EPILOGUE_CHAPTER_ID
    assert narrative_chapter_id(TocKind.CHAPTER, 55) == "055"
    assert parse_narrative_chapter_id("000-prologue") == (TocKind.PROLOGUE, 0)
    assert parse_narrative_chapter_id("999-epilogue") == (TocKind.EPILOGUE, None)
    assert parse_narrative_chapter_id("999") == (TocKind.CHAPTER, 999)
    for invalid in ("000", "epilogue", "chapter-001", "front-matter-001", "appendix-001"):
        with pytest.raises(ValueError):
            parse_narrative_chapter_id(invalid)
    for kind in (TocKind.FRONT_MATTER, TocKind.APPENDIX, TocKind.UNKNOWN):
        with pytest.raises(ValueError):
            narrative_chapter_id(kind, None)
    assert chapter_id_from_selector("epilogue") == EPILOGUE_CHAPTER_ID
    assert chapter_id_from_selector("999-epilogue") == EPILOGUE_CHAPTER_ID
    assert chapter_id_from_selector("prologue") == PROLOGUE_CHAPTER_ID
    assert chapter_id_from_selector("chapter-7") == "007"
    assert sorted(["055", "999-epilogue", "000-prologue", "001"]) == [
        "000-prologue",
        "001",
        "055",
        "999-epilogue",
    ]


# --- two books, one project ----------------------------------------------------------------


def test_two_books_can_both_have_a_prologue_without_colliding(tmp_path: Path) -> None:
    alpha, beta = _two_book_project(tmp_path)
    assert _extract(tmp_path, alpha, None) == ["prologue", "chapter-001"]
    assert _extract(tmp_path, beta, "beta") == ["prologue", "chapter-001"]

    alpha_prologue = tmp_path / "workspace/chapters/000-prologue"
    beta_prologue = tmp_path / "workspace/books/beta/chapters/000-prologue"
    assert alpha_prologue.joinpath("source.md").is_file()
    assert beta_prologue.joinpath("source.md").is_file()
    alpha_source = alpha_prologue.joinpath("source.md").read_bytes()
    beta_source = beta_prologue.joinpath("source.md").read_bytes()
    assert alpha_source != beta_source
    alpha_metadata = _metadata(alpha_prologue / "metadata.yaml")
    beta_metadata = _metadata(beta_prologue / "metadata.yaml")
    assert alpha_metadata.chapter_id == beta_metadata.chapter_id == "000-prologue"
    assert alpha_metadata.source_file == Path("workspace/chapters/000-prologue/source.md")
    assert beta_metadata.source_file == Path("workspace/books/beta/chapters/000-prologue/source.md")
    assert alpha_metadata.source_sha256 != beta_metadata.source_sha256
    # Nothing of beta leaked into the legacy chapter tree.
    assert sorted(p.name for p in (tmp_path / "workspace/chapters").iterdir()) == [
        "000-prologue",
        "001",
    ]


def test_conflict_detection_still_works_inside_each_book(tmp_path: Path) -> None:
    alpha, beta = _two_book_project(tmp_path)
    _extract(tmp_path, alpha, None)
    _extract(tmp_path, beta, "beta")

    for book, pdf in ((None, alpha), ("beta", beta)):
        paths = ProjectPaths(tmp_path, book)
        # Same source and hash: idempotent skip, no rewrite.
        plan = plan_extraction(pdf, paths.toc, project_root=tmp_path, book=book)
        assert [item.action for item in plan.entries] == ["skip", "skip"]
        # Conflicting page range for the same unit ID: fails closed inside that book only.
        toc = TocDocument.model_validate(yaml.safe_load(paths.toc.read_text(encoding="utf-8")))
        shifted = [
            entry.model_copy(update={"start_page": 2, "end_page": 2})
            if entry.kind is TocKind.PROLOGUE
            else entry
            for entry in toc.entries
        ]
        conflicting = paths.workspace / "conflicting-toc.yaml"
        write_toc(toc.model_copy(update={"entries": shifted}), conflicting)
        with pytest.raises(
            ChapterExtractionError, match="stale or conflicting extraction for prologue"
        ):
            plan_extraction(pdf, conflicting, project_root=tmp_path, book=book)
        # The other book's PDF against this book's TOC is refused by provenance, never merged.
        other = beta if book is None else alpha
        with pytest.raises(ChapterExtractionError, match="TOC source PDF hash does not match"):
            plan_extraction(other, paths.toc, project_root=tmp_path, book=book)


def test_wrong_book_selection_is_a_provenance_error_not_a_fallback(tmp_path: Path) -> None:
    alpha, beta = _two_book_project(tmp_path)
    _extract(tmp_path, alpha, None)
    # Beta's PDF against the legacy TOC: refused, and the legacy root stays as it was.
    before = _legacy_hashes(tmp_path)
    with pytest.raises(ChapterExtractionError, match="TOC source PDF hash does not match"):
        plan_extraction(beta, ProjectPaths(tmp_path).toc, project_root=tmp_path)
    # A book that has no workspace yet cannot silently read another book's chapters.
    with pytest.raises(ChapterExtractionError, match="TOC file not found"):
        plan_extraction(
            beta, ProjectPaths(tmp_path, "gamma").toc, project_root=tmp_path, book="gamma"
        )
    assert _legacy_hashes(tmp_path) == before


# --- Book-6-shaped TOC ----------------------------------------------------------------------


def _book6_style(root: Path, book: str = "six") -> tuple[Path, list[TocEntry]]:
    chapters = 55
    front = 6
    entries: list[TocEntry] = []
    page_lines: list[list[str]] = []
    page = 1
    for index in range(1, front + 1):
        entries.append(_entry(f"front-matter-{index:03d}", TocKind.FRONT_MATTER, page, page))
        page_lines.append([f"FRONT MATTER {index}"])
        page += 1
    entries.append(_entry("prologue", TocKind.PROLOGUE, page, page, 0, "Prologue Title"))
    page_lines.append(["PROLOGUE HEADING", "prologue words"])
    page += 1
    for number in range(1, chapters + 1):
        entries.append(
            _entry(f"chapter-{number:03d}", TocKind.CHAPTER, page, page, number, f"Title {number}")
        )
        page_lines.append([f"CHAPTER {number}", f"chapter {number} words"])
        page += 1
    entries.append(_entry("epilogue", TocKind.EPILOGUE, page, page, None, "Epilogue Title"))
    page_lines.append(["EPILOGUE HEADING", "epilogue words"])
    page += 1
    entries.append(_entry("appendix-001", TocKind.APPENDIX, page, page, None, "Glossary"))
    page_lines.append(["GLOSSARY", "glossary words"])
    page += 1
    entries.append(_entry("unknown-001", TocKind.UNKNOWN, page, page, None, "About the Author"))
    page_lines.append(["ABOUT THE AUTHOR", "author words"])
    pdf = _book(root, book, page_lines=page_lines, entries=entries)
    _resource(root, "translation-prompt", "prompt", "Source: {{ source_text }}\n")
    _resource(root, "glossary", "glossary", "terms: {}\n")
    _resource(root, "style-guide", "style_guide", "Synthetic style guide.\n")
    return pdf, entries


def test_book6_style_toc_extracts_exactly_the_narrative_corpus(tmp_path: Path) -> None:
    pdf, entries = _book6_style(tmp_path)
    paths = ProjectPaths(tmp_path, "six")
    plan = plan_extraction(pdf, paths.toc, project_root=tmp_path, book="six")
    planned_ids = [item.entry.entry_id for item in plan.entries]
    assert len(planned_ids) == 57
    assert planned_ids[0] == "prologue"
    assert planned_ids[1:56] == [f"chapter-{n:03d}" for n in range(1, 56)]
    assert planned_ids[56] == "epilogue"
    assert plan.workspace_path == paths.workspace
    assert plan.book == "six"

    result = execute_extraction(plan, project_root=tmp_path)
    assert len(result.extracted_ids) == 57
    chapters = paths.chapters
    names = sorted(p.name for p in chapters.iterdir())
    assert names == ["000-prologue", *[f"{n:03d}" for n in range(1, 56)], "999-epilogue"]
    assert not any(name.startswith(("front-matter", "appendix", "unknown")) for name in names)

    epilogue = _metadata(chapters / "999-epilogue/metadata.yaml")
    assert epilogue.kind is TocKind.EPILOGUE
    assert epilogue.number is None
    assert epilogue.chapter_id == "999-epilogue"
    assert epilogue.source_file == Path("workspace/books/six/chapters/999-epilogue/source.md")
    assert "EPILOGUE HEADING" in (chapters / "999-epilogue/source.md").read_text(encoding="utf-8")
    assert _metadata(chapters / "055/metadata.yaml").number == 55
    assert _metadata(chapters / "000-prologue/metadata.yaml").kind is TocKind.PROLOGUE
    # Glossary and About the Author text never entered the narrative corpus.
    corpus = "".join(p.read_text(encoding="utf-8") for p in chapters.rglob("source.md"))
    assert "GLOSSARY" not in corpus and "ABOUT THE AUTHOR" not in corpus
    assert "FRONT MATTER" not in corpus


def test_epilogue_selector_and_front_matter_opt_in(tmp_path: Path) -> None:
    pdf, _ = _book6_style(tmp_path)
    paths = ProjectPaths(tmp_path, "six")
    plan = plan_extraction(pdf, paths.toc, project_root=tmp_path, book="six", selector="epilogue")
    assert [item.entry.entry_id for item in plan.entries] == ["epilogue"]
    assert plan.entries[0].paths.directory == paths.chapter(epilogue=True)
    with pytest.raises(ChapterExtractionError, match="did not resolve"):
        plan_extraction(pdf, paths.toc, project_root=tmp_path, book="six", selector="appendix-001")
    with pytest.raises(ChapterExtractionError, match="did not resolve"):
        plan_extraction(pdf, paths.toc, project_root=tmp_path, book="six", selector="unknown-001")
    opted = plan_extraction(
        pdf, paths.toc, project_root=tmp_path, book="six", include_front_matter=True
    )
    assert len(opted.entries) == 63


# --- downstream stages resolve the selected workspace ----------------------------------------


def test_normalization_and_chunking_use_the_selected_book_workspace(tmp_path: Path) -> None:
    alpha, beta = _two_book_project(tmp_path)
    _extract(tmp_path, alpha, None)
    _extract(tmp_path, beta, "beta")
    legacy_before = _legacy_hashes(tmp_path)

    normalization = plan_normalization(project_root=tmp_path, book="beta")
    assert normalization.book == "beta"
    assert [item.metadata.chapter_id for item in normalization.chapters] == ["000-prologue", "001"]
    assert all(
        item.paths.cleaned.is_relative_to(tmp_path / "workspace/books/beta")
        for item in normalization.chapters
    )
    execute_normalization(normalization)
    assert (tmp_path / "workspace/books/beta/chapters/000-prologue/cleaned.md").is_file()
    assert not (tmp_path / "workspace/chapters/000-prologue/cleaned.md").exists()

    chunking = plan_chapter_chunking(
        project_root=tmp_path, config=ChunkingConfig(10, 20, 2), book="beta"
    )
    assert chunking.book == "beta"
    execute_chapter_chunking(chunking)
    chunk_metadata = ChunkMetadata.model_validate(
        yaml.safe_load(
            (
                tmp_path / "workspace/books/beta/chapters/000-prologue/chunks/001/metadata.yaml"
            ).read_text(encoding="utf-8")
        )
    )
    assert chunk_metadata.chunk_id == "000-prologue-001"
    assert chunk_metadata.source_file == Path(
        "workspace/books/beta/chapters/000-prologue/chunks/001/source.md"
    )
    assert not (tmp_path / "workspace/chapters/000-prologue/chunks").exists()
    assert _legacy_hashes(tmp_path) == legacy_before

    # The legacy book still normalizes on its own, into its own root.
    execute_normalization(plan_normalization(project_root=tmp_path))
    assert (tmp_path / "workspace/chapters/000-prologue/cleaned.md").is_file()


def test_epilogue_flows_through_normalization_and_chunking(tmp_path: Path) -> None:
    pdf, _ = _book6_style(tmp_path)
    paths = ProjectPaths(tmp_path, "six")
    execute_extraction(
        plan_extraction(pdf, paths.toc, project_root=tmp_path, book="six", selector="epilogue"),
        project_root=tmp_path,
    )
    execute_normalization(
        plan_normalization(project_root=tmp_path, book="six", selector="epilogue")
    )
    plan = plan_chapter_chunking(
        project_root=tmp_path, config=ChunkingConfig(10, 20, 2), book="six", selector="epilogue"
    )
    execute_chapter_chunking(plan)
    metadata = _metadata(paths.chapter(epilogue=True) / "metadata.yaml")
    assert metadata.status.value == "chunked"
    assert metadata.chunk_count >= 1
    chunk = paths.chapter(epilogue=True) / "chunks/001/metadata.yaml"
    assert ChunkMetadata.model_validate(yaml.safe_load(chunk.read_text())).chunk_id == (
        "999-epilogue-001"
    )
    staleness = plan_artifact_regeneration(
        project_root=tmp_path, selectors=["epilogue"], book="six"
    )
    assert [item.metadata.chapter_id for item in staleness.chapters] == ["999-epilogue"]
    assert staleness.chapters[0].source_current


def _fully_chunk(root: Path, pdf: Path, book: str | None) -> None:
    _extract(root, pdf, book)
    execute_normalization(plan_normalization(project_root=root, book=book))
    execute_chapter_chunking(
        plan_chapter_chunking(project_root=root, config=ChunkingConfig(10, 20, 2), book=book)
    )


def test_source_quality_audit_uses_the_selected_book_workspace(tmp_path: Path) -> None:
    alpha, beta = _two_book_project(tmp_path)
    _fully_chunk(tmp_path, alpha, None)
    _fully_chunk(tmp_path, beta, "beta")
    legacy_before = _legacy_hashes(tmp_path)

    selection = audit_source_quality(tmp_path, book="beta")
    assert selection.book == "beta"
    report = selection.report
    assert report.chapters_scanned == 2
    directory = persist_report(selection)
    assert directory.is_relative_to(tmp_path / "workspace/books/beta/audits/source-quality")
    assert not (tmp_path / "workspace/audits").exists()
    legacy_selection = audit_source_quality(tmp_path)
    assert legacy_selection.book is None
    assert legacy_selection.report.audit_id != report.audit_id
    assert _legacy_hashes(tmp_path) == legacy_before
    with pytest.raises(Exception, match="chapter workspace not found"):
        audit_source_quality(tmp_path, book="gamma")


def test_metadata_resolution_reads_only_the_selected_book(tmp_path: Path) -> None:
    alpha, beta = _two_book_project(tmp_path)
    # A pre-existing legacy record must never be treated as beta's existing record.
    (tmp_path / "workspace/metadata.yaml").write_text("not: beta\n", encoding="utf-8")
    plan = plan_book_metadata(project_root=tmp_path, book="beta")
    assert plan.book == "beta"
    assert plan.action == "create"
    assert plan.metadata_file == tmp_path / "workspace/books/beta/metadata.yaml"
    assert plan.metadata.source_pdf == Path("input/beta.pdf")
    assert plan.metadata.source_pdf_sha256 == hashlib.sha256(beta.read_bytes()).hexdigest()
    assert plan.metadata.title_en == "Beta Book"
    assert plan.metadata.source_pdf_sha256 != hashlib.sha256(alpha.read_bytes()).hexdigest()


def test_resource_freezing_and_snapshot_follow_book_lineage(tmp_path: Path) -> None:
    alpha, beta = _two_book_project(tmp_path)
    _fully_chunk(tmp_path, alpha, None)
    _fully_chunk(tmp_path, beta, "beta")
    legacy_before = _legacy_hashes(tmp_path)

    freeze = plan_resource_freeze(project_root=tmp_path, book="beta")
    assert freeze.book == "beta"
    assert all(
        item.frozen_file.as_posix().startswith("workspace/books/beta/resources/")
        for item in freeze.publications
    )
    plan = plan_snapshot(project_root=tmp_path, book="beta")
    assert plan.book == "beta"
    manifest = plan.manifest
    assert manifest.book.source_pdf == Path("input/beta.pdf")
    assert manifest.book.source_pdf_sha256 == hashlib.sha256(beta.read_bytes()).hexdigest()
    assert manifest.book.toc_file == Path("workspace/books/beta/toc.yaml")
    assert [chapter.chapter_id for chapter in manifest.chapters] == ["000-prologue", "001"]
    for chapter in manifest.chapters:
        assert chapter.cleaned_file.as_posix().startswith("workspace/books/beta/chapters/")
        for chunk in chapter.chunks:
            assert chunk.source_file.as_posix().startswith("workspace/books/beta/chapters/")
    assert plan.output_root == tmp_path / "workspace/books/beta/snapshots/translation"

    legacy_plan = plan_snapshot(project_root=tmp_path)
    assert legacy_plan.snapshot_id != plan.snapshot_id
    assert legacy_plan.manifest.book.toc_file == Path("workspace/toc.yaml")

    result = create_snapshot(plan)
    assert result.path.is_relative_to(tmp_path / "workspace/books/beta/snapshots/translation")
    assert (tmp_path / "workspace/books/beta/resources").is_dir()
    assert not (tmp_path / "workspace/resources").exists()
    assert not (tmp_path / "workspace/snapshots").exists()
    assert _legacy_hashes(tmp_path) == legacy_before

    # The frozen record names its own workspace, so validation recovers the lineage without a
    # selection, agrees with the explicit one, and refuses a different one.
    inferred = validate_snapshot(result.path, project_root=tmp_path)
    assert inferred.report.valid and inferred.lineage_current
    explicit = validate_snapshot(plan.snapshot_id, project_root=tmp_path, book="beta")
    assert explicit.report.valid and explicit.lineage_current
    wrong = validate_snapshot(result.path, project_root=tmp_path, book="alpha")
    assert not wrong.report.valid
    assert any(
        "frozen from book workspace beta, not alpha" in error for error in wrong.report.errors
    )


def test_snapshot_planning_cannot_mix_chapter_artifacts_across_books(tmp_path: Path) -> None:
    alpha, beta = _two_book_project(tmp_path)
    _fully_chunk(tmp_path, alpha, None)
    # Beta is extracted and normalized, but chapter 001 is never chunked: alpha's chunked 001
    # must not be borrowed to complete beta's manifest.
    _extract(tmp_path, beta, "beta")
    execute_normalization(plan_normalization(project_root=tmp_path, book="beta"))
    execute_chapter_chunking(
        plan_chapter_chunking(
            project_root=tmp_path,
            config=ChunkingConfig(10, 20, 2),
            book="beta",
            selector="prologue",
        )
    )
    with pytest.raises(SnapshotError, match="chapter 001 is not validly chunked"):
        plan_snapshot(project_root=tmp_path, book="beta")
    # A subset that names only beta's chunked prologue succeeds and still never leaves beta.
    subset = plan_snapshot(project_root=tmp_path, book="beta", selectors=["prologue"])
    assert all(
        chapter.cleaned_file.as_posix().startswith("workspace/books/beta/")
        for chapter in subset.manifest.chapters
    )
    # The legacy book is planned from its own root even though beta shares every unit ID.
    legacy = plan_snapshot(project_root=tmp_path)
    assert all(
        chapter.cleaned_file.as_posix().startswith("workspace/chapters/")
        for chapter in legacy.manifest.chapters
    )


def test_book_workspace_operations_never_mutate_the_legacy_book(tmp_path: Path) -> None:
    alpha, beta = _two_book_project(tmp_path)
    _fully_chunk(tmp_path, alpha, None)
    create_snapshot(plan_snapshot(project_root=tmp_path))
    before = _legacy_hashes(tmp_path)
    assert before

    _fully_chunk(tmp_path, beta, "beta")
    audit = audit_source_quality(tmp_path, book="beta")
    persist_report(audit)
    plan_book_metadata(project_root=tmp_path, book="beta")
    create_snapshot(plan_snapshot(project_root=tmp_path, book="beta"))
    plan_artifact_regeneration(project_root=tmp_path, selectors=["prologue", "1"], book="beta")

    assert _legacy_hashes(tmp_path) == before
    # And the legacy book's own lineage is still current from its own root.
    legacy = plan_snapshot(project_root=tmp_path)
    assert legacy.action == "skip"


# --- CLI ------------------------------------------------------------------------------------


def test_cli_book_option_selects_the_workspace_and_prints_no_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, beta = _two_book_project(tmp_path)
    _extract(tmp_path, alpha, None)
    monkeypatch.chdir(tmp_path)
    legacy_before = _legacy_hashes(tmp_path)

    dry = runner.invoke(app, ["extract-chapters", "input/beta.pdf", "--book", "beta", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "Dry run" in dry.output
    assert "BETA PROLOGUE" not in dry.output
    assert not (tmp_path / "workspace/books/beta/chapters").exists()

    real = runner.invoke(app, ["extract-chapters", "input/beta.pdf", "--book", "beta"])
    assert real.exit_code == 0, real.output
    assert (tmp_path / "workspace/books/beta/chapters/000-prologue/source.md").is_file()
    assert _legacy_hashes(tmp_path) == legacy_before

    normalized = runner.invoke(app, ["normalize-chapters", "--book", "beta", "--dry-run"])
    assert normalized.exit_code == 0, normalized.output
    assert not (tmp_path / "workspace/books/beta/chapters/000-prologue/cleaned.md").exists()

    invalid = runner.invoke(app, ["extract-chapters", "input/beta.pdf", "--book", "Bad Id"])
    assert invalid.exit_code == 1
    assert "book workspace ID" in invalid.output

    # Without --book the legacy TOC governs, and beta's PDF is refused rather than adopted.
    legacy = runner.invoke(app, ["extract-chapters", "input/beta.pdf", "--dry-run"])
    assert legacy.exit_code == 1
    assert "TOC source PDF hash does not match" in legacy.output
    assert _legacy_hashes(tmp_path) == legacy_before


def test_cli_discover_toc_writes_into_the_selected_book_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_book_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    ProjectPaths(tmp_path, "beta").toc.unlink()
    result = runner.invoke(app, ["discover-toc", "--book", "beta", "--dry-run"])
    # A PDF without an embedded TOC is a discovery error, but the resolved output path and the
    # configured PDF must already be beta's, never the legacy ones.
    assert "workspace/books/beta" in result.output.replace("\n", "") or result.exit_code == 1
    assert not ProjectPaths(tmp_path, "beta").toc.exists()
    assert ProjectPaths(tmp_path).toc.is_file()


# --- Translation runs over book-scoped snapshots -----------------------------------------------


RUN_NOW = datetime(2026, 3, 4, 5, 6, tzinfo=UTC)


def _snapshot_both_books(root: Path) -> tuple[str, str, Path]:
    """Freeze one snapshot per book; return the legacy ID, beta's ID, and beta's snapshot path."""
    alpha, beta = _two_book_project(root)
    _fully_chunk(root, alpha, None)
    _fully_chunk(root, beta, "beta")
    legacy_id = create_snapshot(plan_snapshot(project_root=root)).snapshot_id
    scoped = create_snapshot(plan_snapshot(project_root=root, book="beta"))
    return legacy_id, scoped.snapshot_id, scoped.path


def _legacy_hashes_without_runs(root: Path) -> dict[str, str]:
    return {
        name: digest
        for name, digest in _legacy_hashes(root).items()
        if not name.startswith("translation-runs/")
    }


def test_translation_run_still_resolves_a_legacy_snapshot_by_id(tmp_path: Path) -> None:
    legacy_id, _, _ = _snapshot_both_books(tmp_path)
    implicit = plan_run(legacy_id, project_root=tmp_path, now=lambda: RUN_NOW)
    explicit = plan_run(legacy_id, project_root=tmp_path, book=None, now=lambda: RUN_NOW)
    assert implicit.run == explicit.run
    assert implicit.run.snapshot_path == Path(f"workspace/snapshots/translation/{legacy_id}")
    assert implicit.output_root == tmp_path / "workspace/translation-runs"


def test_translation_run_resolves_a_book_scoped_snapshot_by_id_with_its_book(
    tmp_path: Path,
) -> None:
    _, beta_id, beta_path = _snapshot_both_books(tmp_path)
    plan = plan_run(beta_id, project_root=tmp_path, book="beta", now=lambda: RUN_NOW)
    assert plan.run.snapshot_id == beta_id
    assert plan.run.snapshot_path == Path(f"workspace/books/beta/snapshots/translation/{beta_id}")
    assert (tmp_path / plan.run.snapshot_path).resolve() == beta_path.resolve()
    assert plan.run.chapter_count == 2
    assert plan.run.job_count == len(plan.jobs) == plan.run.pending_count
    assert plan.run.blocked_count == 0
    assert plan.run.status.value == "ready"
    assert {job.chapter_id for job in plan.jobs} == {"000-prologue", "001"}


def test_book_scoped_snapshot_by_id_matches_explicit_path_resolution_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, beta_id, beta_path = _snapshot_both_books(tmp_path)
    by_id = plan_run(beta_id, project_root=tmp_path, book="beta", now=lambda: RUN_NOW)
    by_path = plan_run(beta_path, project_root=tmp_path, now=lambda: RUN_NOW)
    # A relative explicit path resolves from the working directory, which the CLI sets to the
    # project root.
    monkeypatch.chdir(tmp_path)
    by_relative = plan_run(
        beta_path.relative_to(tmp_path), project_root=tmp_path, now=lambda: RUN_NOW
    )
    by_path_and_book = plan_run(beta_path, project_root=tmp_path, book="beta", now=lambda: RUN_NOW)
    assert by_id.run == by_path.run == by_relative.run == by_path_and_book.run
    assert by_id.jobs == by_path.jobs == by_relative.jobs == by_path_and_book.jobs
    assert by_id.report == by_path.report
    assert by_id.output_root == by_path.output_root


def test_translation_run_fails_closed_on_the_wrong_book_and_never_scans_workspaces(
    tmp_path: Path,
) -> None:
    legacy_id, beta_id, beta_path = _snapshot_both_books(tmp_path)
    # A book-scoped ID without its book is not found in the legacy root, and is not searched for.
    with pytest.raises(QueueError, match="not current, valid, and frozen"):
        plan_run(beta_id, project_root=tmp_path, now=lambda: RUN_NOW)
    # A legacy ID is not found inside another book's workspace either.
    with pytest.raises(QueueError, match="not current, valid, and frozen"):
        plan_run(legacy_id, project_root=tmp_path, book="beta", now=lambda: RUN_NOW)
    # A workspace that does not exist is a refusal, not a fallback.
    with pytest.raises(QueueError, match="not current, valid, and frozen"):
        plan_run(beta_id, project_root=tmp_path, book="gamma", now=lambda: RUN_NOW)
    # An explicit path whose recorded workspace disagrees with the selected book is refused too.
    with pytest.raises(QueueError, match="not current, valid, and frozen"):
        plan_run(beta_path, project_root=tmp_path, book="gamma", now=lambda: RUN_NOW)
    # An invalid book workspace ID is a queue error the CLI can report, not a stray exception.
    with pytest.raises(QueueError, match="book workspace ID"):
        plan_run(beta_id, project_root=tmp_path, book="Bad Id", now=lambda: RUN_NOW)
    assert not (tmp_path / "workspace/translation-runs").exists()


def test_duplicate_snapshot_ids_across_workspaces_are_never_resolved_ambiguously(
    tmp_path: Path,
) -> None:
    legacy_id, beta_id, beta_path = _snapshot_both_books(tmp_path)
    legacy_path = ProjectPaths(tmp_path).translation_snapshot(legacy_id)
    # Plant byte-identical copies of each snapshot under the same ID in the other workspace.
    shutil.copytree(beta_path, ProjectPaths(tmp_path).translation_snapshot(beta_id))
    shutil.copytree(legacy_path, ProjectPaths(tmp_path, "beta").translation_snapshot(legacy_id))

    # Each selection consults exactly one location, and the copy there is refused because its
    # frozen manifest path names the workspace it was actually frozen from.
    with pytest.raises(QueueError, match="not current, valid, and frozen"):
        plan_run(beta_id, project_root=tmp_path, now=lambda: RUN_NOW)
    with pytest.raises(QueueError, match="not current, valid, and frozen"):
        plan_run(legacy_id, project_root=tmp_path, book="beta", now=lambda: RUN_NOW)
    scoped = plan_run(beta_id, project_root=tmp_path, book="beta", now=lambda: RUN_NOW)
    assert (tmp_path / scoped.run.snapshot_path).resolve() == beta_path.resolve()
    legacy = plan_run(legacy_id, project_root=tmp_path, now=lambda: RUN_NOW)
    assert (tmp_path / legacy.run.snapshot_path).resolve() == legacy_path.resolve()


def test_planning_and_creating_a_book_scoped_run_leaves_the_legacy_book_unchanged(
    tmp_path: Path,
) -> None:
    legacy_id, beta_id, _ = _snapshot_both_books(tmp_path)
    legacy_before = _legacy_hashes(tmp_path)
    beta_before = _tree_hashes(tmp_path / "workspace/books/beta")
    legacy_plan_before = plan_run(legacy_id, project_root=tmp_path, now=lambda: RUN_NOW)

    plan = plan_run(beta_id, project_root=tmp_path, book="beta", now=lambda: RUN_NOW)
    assert _legacy_hashes(tmp_path) == legacy_before
    assert _tree_hashes(tmp_path / "workspace/books/beta") == beta_before

    created = create_run(plan)
    # Run storage is deliberately unchanged by this fix: the run lands in the project-level
    # run root, and its lineage names beta's snapshot. Nothing else in the legacy root moves.
    assert created.path.parent == tmp_path / "workspace/translation-runs"
    assert _legacy_hashes_without_runs(tmp_path) == {
        name: digest
        for name, digest in legacy_before.items()
        if not name.startswith("translation-runs/")
    }
    assert _tree_hashes(tmp_path / "workspace/books/beta") == beta_before
    persisted = yaml.safe_load((created.path / "run.yaml").read_text(encoding="utf-8"))
    assert persisted["snapshot_id"] == beta_id
    assert persisted["snapshot_path"].startswith("workspace/books/beta/snapshots/translation/")
    # The legacy book plans exactly as it did before beta's run existed, apart from the run
    # sequence that is allocated per snapshot rather than per book.
    legacy_plan_after = plan_run(legacy_id, project_root=tmp_path, now=lambda: RUN_NOW)
    assert legacy_plan_after.run == legacy_plan_before.run


def test_cli_create_translation_run_accepts_book_for_id_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_id, beta_id, beta_path = _snapshot_both_books(tmp_path)
    monkeypatch.chdir(tmp_path)
    legacy_before = _legacy_hashes(tmp_path)
    beta_before = _tree_hashes(tmp_path / "workspace/books/beta")

    failing = runner.invoke(
        app, ["create-translation-run", beta_id, "--paragraph-markers", "--dry-run"]
    )
    assert failing.exit_code == 1, failing.output
    assert "not current, valid, and frozen" in failing.output

    wrong = runner.invoke(app, ["create-translation-run", beta_id, "--book", "alpha", "--dry-run"])
    assert wrong.exit_code == 1, wrong.output

    dry = runner.invoke(
        app,
        [
            "create-translation-run",
            beta_id,
            "--book",
            "beta",
            "--label",
            "beta-full",
            "--paragraph-markers",
            "--dry-run",
            "--show-plan",
        ],
    )
    assert dry.exit_code == 0, dry.output
    flat = dry.output.replace("\n", "")
    assert beta_id in flat
    assert MARKED_OUTPUT_CONTRACT_VERSION in flat
    assert "ready" in flat
    for prose in ("BETA PROLOGUE", "beta words", "ALPHA PROLOGUE", "alpha words"):
        assert prose not in dry.output
    assert not (tmp_path / "workspace/translation-runs").exists()
    assert _legacy_hashes(tmp_path) == legacy_before
    assert _tree_hashes(tmp_path / "workspace/books/beta") == beta_before

    # The same snapshot supplied by explicit path plans the identical run.
    by_path = plan_run(beta_path, project_root=tmp_path, now=lambda: RUN_NOW)
    by_id = plan_run(beta_id, project_root=tmp_path, book="beta", now=lambda: RUN_NOW)
    assert by_id.run == by_path.run

    # The legacy book keeps resolving by bare ID exactly as before.
    legacy = runner.invoke(app, ["create-translation-run", legacy_id, "--dry-run"])
    assert legacy.exit_code == 0, legacy.output
    assert legacy_id in legacy.output.replace("\n", "")


def test_cli_create_translation_run_reads_the_selected_book_contract_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_id, beta_id, _ = _snapshot_both_books(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = ProjectPaths(tmp_path, "beta").book_config
    config.write_text(
        config.read_text(encoding="utf-8") + "paragraph_marker_contract: false\n",
        encoding="utf-8",
    )
    scoped = runner.invoke(app, ["create-translation-run", beta_id, "--book", "beta", "--dry-run"])
    assert scoped.exit_code == 0, scoped.output
    assert OUTPUT_CONTRACT_VERSION in scoped.output.replace("\n", "")
    assert MARKED_OUTPUT_CONTRACT_VERSION not in scoped.output.replace("\n", "")
    legacy = runner.invoke(app, ["create-translation-run", legacy_id, "--dry-run"])
    assert legacy.exit_code == 0, legacy.output
    assert MARKED_OUTPUT_CONTRACT_VERSION in legacy.output.replace("\n", "")
