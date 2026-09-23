"""EPUB Builder: a verified release becomes a readable RTL book, or nothing is written.

Every fixture is synthetic. The Persian below is real Persian script written for these tests; no
book prose appears in this repository.
"""

import hashlib
import zipfile
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import unescape

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.epub import (
    CONTAINER_PATH,
    EPUB_BUILDER_VERSION,
    MIMETYPE,
    MIMETYPE_PATH,
    NAVIGATION_PATH,
    PACKAGE_PATH,
    STYLESHEET_PATH,
    EpubBuildError,
    book_identifier,
    chapter_href,
    epub_content_address,
    execute_epub_build,
    plan_epub_build,
    read_archive,
    render_archive,
    render_chapter,
    render_stylesheet,
    verify_epub_file,
)
from book_translator.release_assembly import (
    BOOK_FILENAME,
    MANIFEST_FILENAME,
    METADATA_FILENAME,
    execute_release_assembly,
    plan_release_assembly,
)
from tests.test_chapter_titles import TitleProvider, _translate_titles
from tests.test_release_assembly import TRANSLATION, _accepted_run, _workspace_fingerprint
from tests.test_release_verification import _reindex, _rewrite

runner = CliRunner()


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _release(root: Path, *, chapters: int = 2, with_titles: bool = True) -> Path:
    run_id = _accepted_run(root, chapters=chapters)
    if with_titles:
        _translate_titles(root, run_id, TitleProvider())
    plan = plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)
    execute_release_assembly(plan, project_root=root)
    return plan.output_path


def _build(root: Path, release: Path, **updates: object) -> tuple[Path, bytes]:
    plan = plan_epub_build(release, project_root=root, **updates)  # type: ignore[arg-type]
    result = execute_epub_build(plan, project_root=root)
    return result.output_path, result.output_path.read_bytes()


def _entries(content: bytes) -> tuple[tuple[str, ...], dict[str, bytes]]:
    return read_archive(content)


# --------------------------------------------------------------------------------------------
# Pure engine
# --------------------------------------------------------------------------------------------


def test_a_chapter_becomes_xhtml_that_keeps_its_paragraphs_in_order() -> None:
    markdown = "# عنوان\n\nپاراگراف یک.\n\nپاراگراف دو.\n"

    document, title = render_chapter(
        chapter_id="001", markdown=markdown, language="fa", expected_heading="# عنوان"
    )

    assert title == "عنوان"
    assert "<h1>عنوان</h1>" in document
    assert document.index("پاراگراف یک.") < document.index("پاراگراف دو.")
    assert document.count("<p") == 2
    assert 'dir="rtl"' in document and 'xml:lang="fa"' in document


def test_a_multiline_paragraph_keeps_its_line_structure() -> None:
    markdown = "# عنوان\n\nسطر یک\nسطر دو\n"

    document, _ = render_chapter(
        chapter_id="001", markdown=markdown, language="fa", expected_heading="# عنوان"
    )

    assert "سطر یک<br/>\nسطر دو" in document


def test_a_scene_separator_stays_a_separator() -> None:
    markdown = "# عنوان\n\nیک.\n\n***\n\nدو.\n"

    document, _ = render_chapter(
        chapter_id="001", markdown=markdown, language="fa", expected_heading="# عنوان"
    )

    assert '<p class="scene-separator">***</p>' in document


def test_markup_characters_are_escaped_without_changing_the_text() -> None:
    markdown = "# عنوان\n\nیک & دو <سه>.\n"

    document, _ = render_chapter(
        chapter_id="001", markdown=markdown, language="fa", expected_heading="# عنوان"
    )

    assert "&amp;" in document and "&lt;سه&gt;" in document
    assert "یک & دو <سه>." in unescape(document)


def test_a_chapter_whose_heading_is_not_the_recorded_one_is_refused() -> None:
    with pytest.raises(EpubBuildError, match="heading the release recorded"):
        render_chapter(
            chapter_id="001",
            markdown="# دیگر\n\nمتن.\n",
            language="fa",
            expected_heading="# عنوان",
        )
    with pytest.raises(EpubBuildError, match="canonical Markdown"):
        render_chapter(chapter_id="001", markdown="", language="fa", expected_heading="# عنوان")


def test_the_book_identifier_is_derived_from_the_release_and_is_stable() -> None:
    first = book_identifier("a" * 64)
    again = book_identifier("a" * 64)
    other = book_identifier("b" * 64)

    assert first == again
    assert first != other
    assert first.startswith("urn:uuid:")
    with pytest.raises(EpubBuildError):
        book_identifier("not-a-digest")


def test_the_stylesheet_sets_right_to_left() -> None:
    css = render_stylesheet()

    assert "direction: rtl;" in css
    assert "text-align: right;" in css


def test_the_content_address_ignores_entry_order() -> None:
    forward = epub_content_address((("a", "1" * 64), ("b", "2" * 64)))
    reversed_order = epub_content_address((("b", "2" * 64), ("a", "1" * 64)))

    assert forward == reversed_order


# --------------------------------------------------------------------------------------------
# 1. A verified release builds
# --------------------------------------------------------------------------------------------


def test_a_verified_release_builds_a_complete_epub(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=3)

    path, content = _build(root, release)

    names, contents = _entries(content)
    assert path == root / "output/book-fa-v1.epub"
    assert names[0] == MIMETYPE_PATH
    assert contents[MIMETYPE_PATH] == MIMETYPE.encode("ascii")
    for required in (CONTAINER_PATH, PACKAGE_PATH, NAVIGATION_PATH, STYLESHEET_PATH):
        assert required in contents
    for chapter_id in ("001", "002", "003"):
        assert f"OEBPS/{chapter_href(chapter_id)}" in contents
    assert verify_epub_file(path) == ()


def test_a_dry_run_renders_everything_and_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)

    plan = plan_epub_build(release, project_root=root)

    assert plan.chapter_count == 2
    assert plan.content_sha256 and plan.epub_id.startswith("epub-")
    assert {item.path for item in plan.files} >= {MIMETYPE_PATH, PACKAGE_PATH, NAVIGATION_PATH}
    assert not (root / "output").exists()


# --------------------------------------------------------------------------------------------
# 2, 3, 4. A release that does not verify is never built
# --------------------------------------------------------------------------------------------


def test_an_unverified_release_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)
    _rewrite(release / METADATA_FILENAME, "tampered: true\n")

    with pytest.raises(EpubBuildError, match="NOT_READY_FOR_PUBLICATION"):
        plan_epub_build(release, project_root=root)
    assert not (root / "output").exists()


def test_a_release_with_a_missing_chapter_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)
    manifest = yaml.safe_load((release / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    (release / manifest["chapters"][1]["file"]).unlink()

    with pytest.raises(EpubBuildError, match="NOT_READY_FOR_PUBLICATION"):
        plan_epub_build(release, project_root=root)


def test_a_release_with_a_broken_manifest_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    _rewrite(release / MANIFEST_FILENAME, "not: a manifest\n")

    with pytest.raises(EpubBuildError, match="NOT_READY_FOR_PUBLICATION"):
        plan_epub_build(release, project_root=root)


def test_a_release_with_a_damaged_book_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)
    book = release / BOOK_FILENAME
    _rewrite(book, book.read_text(encoding="utf-8") + "\nپاراگراف اضافه\n")
    _reindex(release, BOOK_FILENAME)

    with pytest.raises(EpubBuildError, match="NOT_READY_FOR_PUBLICATION"):
        plan_epub_build(release, project_root=root)


def test_a_release_without_provenance_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    (release / "provenance/translation-lineage.json").unlink()

    with pytest.raises(EpubBuildError, match="NOT_READY_FOR_PUBLICATION"):
        plan_epub_build(release, project_root=root)


def test_a_release_whose_book_metadata_is_not_book_metadata_is_refused(tmp_path: Path) -> None:
    """A book with no title and no author is not a publication; it is a pile of chapters."""
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    # Replace the metadata and re-index it so the *release* still verifies: the point is that the
    # builder refuses on its own evidence rather than leaning on verification to catch everything.
    replacement = "schema_version: '1.0'\nnot: book metadata\n"
    _rewrite(release / METADATA_FILENAME, replacement)
    _reindex(release, METADATA_FILENAME)
    manifest_path = release / MANIFEST_FILENAME
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["book_metadata_sha256"] = _hash(replacement.encode("utf-8"))
    _rewrite(manifest_path, yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))

    from book_translator.release_verification import verify_release_artifact

    assert verify_release_artifact(release, project_root=root).ready
    with pytest.raises(EpubBuildError, match="book metadata"):
        plan_epub_build(release, project_root=root)
    assert not (root / "output").exists()


def test_a_book_with_no_recorded_title_at_all_is_refused() -> None:
    from book_translator.epub.workflow import _release_book_title
    from book_translator.models import BookWorkspaceMetadata
    from tests.test_contracts import _provenance

    titleless = BookWorkspaceMetadata.model_construct(title_en="  ", title_fa="  ")

    with pytest.raises(EpubBuildError, match="records no title"):
        _release_book_title(titleless)
    assert _provenance is not None


def test_a_directory_that_is_not_a_release_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _release(root, chapters=1)
    stray = root / "workspace/releases/not-a-release"
    stray.mkdir()

    with pytest.raises(EpubBuildError, match="could not be verified"):
        plan_epub_build(stray, project_root=root)


# --------------------------------------------------------------------------------------------
# 5 and 6. Determinism
# --------------------------------------------------------------------------------------------


def test_building_one_release_twice_produces_identical_bytes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=3)

    first_path, first = _build(root, release)
    second_path, second = _build(root, release, force=True)

    assert first == second
    assert _hash(first) == _hash(second)
    assert first_path == second_path


def test_the_archive_encodes_no_timestamp_and_no_filesystem_order(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=3)
    _, content = _build(root, release)

    with zipfile.ZipFile(BytesIO(content)) as archive:
        infos = archive.infolist()
    assert {info.date_time for info in infos} == {(1980, 1, 1, 0, 0, 0)}
    assert {info.create_system for info in infos} == {3}
    assert infos[0].filename == MIMETYPE_PATH
    assert infos[0].compress_type == zipfile.ZIP_STORED
    names = [info.filename for info in infos[1:]]
    assert names == sorted(names)


def test_the_planned_content_address_is_the_archive_that_gets_written(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)
    plan = plan_epub_build(release, project_root=root)

    _, content = _build(root, release)
    names, contents = _entries(content)

    assert (
        epub_content_address(tuple((name, _hash(contents[name])) for name in names))
        == plan.content_sha256
    )
    assert render_archive(plan) == content


def test_two_different_releases_do_not_share_a_book_identifier(tmp_path: Path) -> None:
    first = plan_epub_build(_release(tmp_path / "a", chapters=1), project_root=tmp_path / "a")
    second = plan_epub_build(_release(tmp_path / "b", chapters=2), project_root=tmp_path / "b")

    assert first.book_identifier != second.book_identifier


# --------------------------------------------------------------------------------------------
# 7 to 10. Content, RTL, navigation, metadata
# --------------------------------------------------------------------------------------------


def test_persian_text_reaches_the_epub_intact(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)
    _, content = _build(root, release)
    _, contents = _entries(content)

    for chapter_id in ("001", "002"):
        document = contents[f"OEBPS/{chapter_href(chapter_id)}"].decode("utf-8")
        for paragraph in TRANSLATION.strip("\n").split("\n\n"):
            assert paragraph in unescape(document)


def test_every_archive_entry_is_valid_utf8(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _, content = _build(root, _release(root, chapters=2))
    names, contents = _entries(content)

    for name in names:
        if name == MIMETYPE_PATH:
            continue
        assert contents[name].decode("utf-8")


def test_the_stylesheet_is_shipped_and_linked(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _, content = _build(root, _release(root, chapters=1))
    _, contents = _entries(content)

    css = contents[STYLESHEET_PATH].decode("utf-8")
    assert "direction: rtl;" in css and "text-align: right;" in css
    assert 'href="styles/rtl.css"' in contents[NAVIGATION_PATH].decode("utf-8")
    assert 'href="../styles/rtl.css"' in contents[f"OEBPS/{chapter_href('001')}"].decode("utf-8")
    assert 'href="styles/rtl.css"' in contents[PACKAGE_PATH].decode("utf-8")


def test_navigation_lists_every_chapter_with_its_persian_title(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=3)
    plan = plan_epub_build(release, project_root=root)
    _, content = _build(root, release)
    _, contents = _entries(content)

    navigation = contents[NAVIGATION_PATH].decode("utf-8")
    assert 'epub:type="toc"' in navigation
    for record in plan.chapters:
        assert f'href="{chapter_href(record.chapter_id)}"' in navigation
        assert record.title in navigation
    order = [navigation.index(record.title) for record in plan.chapters]
    assert order == sorted(order)


def test_the_package_describes_the_book_the_release_names(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)
    plan = plan_epub_build(release, project_root=root)
    _, content = _build(root, release)
    _, contents = _entries(content)

    package = contents[PACKAGE_PATH].decode("utf-8")
    # The synthetic project records a Persian book title, and the target title wins.
    assert "<dc:title>کتاب ساختگی</dc:title>" in package
    assert "<dc:creator>Author</dc:creator>" in package
    assert "<dc:language>fa</dc:language>" in package
    assert 'page-progression-direction="rtl"' in package
    assert plan.book_identifier in package
    assert plan.release_content_sha256 in package
    assert f'<meta property="dcterms:modified">{plan.modified}</meta>' in package
    assert plan.modified.endswith("Z")


def test_the_spine_reads_the_release_chapters_in_order(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=3)
    _, content = _build(root, release)
    _, contents = _entries(content)

    package = contents[PACKAGE_PATH].decode("utf-8")
    spine = package.split("<spine")[1]
    positions = [spine.index(f'idref="ch-{chapter}"') for chapter in ("001", "002", "003")]
    assert positions == sorted(positions)


# --------------------------------------------------------------------------------------------
# 11, 12, 13. No provider, no mutation
# --------------------------------------------------------------------------------------------


def test_the_epub_stage_holds_no_path_to_a_provider_or_to_translation_state() -> None:
    package = Path(__file__).parents[1] / "src/book_translator/epub"
    sources = {path.name: path.read_text(encoding="utf-8") for path in package.glob("*.py")}

    assert sources
    forbidden = (
        "provider_execution",
        "TranslationProvider",
        "translation_queue",
        "translation_snapshot",
        "translation_worker",
        "chapter_titles",
    )
    for name, source in sources.items():
        for symbol in forbidden:
            assert symbol not in source, f"{name} reaches {symbol}"


def test_a_build_succeeds_while_every_provider_adapter_is_sabotaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)

    from book_translator.provider_execution import providers

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the EPUB builder contacted a provider")

    for adapter in (
        "ClaudeCodeTranslationProvider",
        "CodexCliTranslationProvider",
        "FakeTranslationProvider",
        "OpenAIResponsesProvider",
    ):
        monkeypatch.setattr(providers, adapter, refuse)

    path, _ = _build(root, release)

    assert path.is_file()


def test_building_mutates_no_byte_of_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)
    before = _workspace_fingerprint(root)
    release_before = {
        item.relative_to(release).as_posix(): _hash(item.read_bytes())
        for item in sorted(release.rglob("*"))
        if item.is_file()
    }

    _build(root, release)
    _build(root, release, force=True)

    assert _workspace_fingerprint(root) == before
    assert {
        item.relative_to(release).as_posix(): _hash(item.read_bytes())
        for item in sorted(release.rglob("*"))
        if item.is_file()
    } == release_before


def test_an_epub_may_never_be_written_inside_the_release(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)

    for destination in (
        "workspace/releases/book-fa-v1",
        "workspace/releases/book-fa-v1/book.epub",
    ):
        with pytest.raises(EpubBuildError, match="inside the release"):
            plan_epub_build(release, project_root=root, output=destination)


def test_an_existing_epub_is_never_replaced_without_force(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    path, original = _build(root, release)

    with pytest.raises(EpubBuildError, match="--force"):
        execute_epub_build(plan_epub_build(release, project_root=root), project_root=root)

    assert path.read_bytes() == original


def test_an_output_path_must_name_an_epub(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)

    with pytest.raises(EpubBuildError, match="must end in .epub"):
        plan_epub_build(release, project_root=root, output="output/book.zip")


# --------------------------------------------------------------------------------------------
# 14. Corrupted EPUB detection
# --------------------------------------------------------------------------------------------


def test_a_corrupted_archive_is_detected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path, content = _build(root, _release(root, chapters=1))
    path.write_bytes(content[: len(content) // 2])

    with pytest.raises(EpubBuildError, match="could not be read back"):
        verify_epub_file(path)


def test_an_archive_missing_its_package_is_detected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path, content = _build(root, _release(root, chapters=1))
    names, contents = _entries(content)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            if name != PACKAGE_PATH:
                archive.writestr(name, contents[name])
    path.write_bytes(buffer.getvalue())

    assert verify_epub_file(path) == ("required archive entry is missing: OEBPS/content.opf",)


def test_an_archive_whose_chapter_was_swapped_is_detected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path, content = _build(root, _release(root, chapters=2))
    names, contents = _entries(content)
    swapped = contents[f"OEBPS/{chapter_href('001')}"].replace(b"<h1>", b"<h1>x")
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(
                name, swapped if name == f"OEBPS/{chapter_href('001')}" else contents[name]
            )
    path.write_bytes(buffer.getvalue())

    problems = verify_epub_file(path)
    assert any("navigation document" in item or "chapter" in item for item in problems)


def test_a_stray_archive_entry_is_detected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path, content = _build(root, _release(root, chapters=1))
    names, contents = _entries(content)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, contents[name])
        archive.writestr("OEBPS/stray.xhtml", "<html/>")
    path.write_bytes(buffer.getvalue())

    assert "archive holds an unexpected entry: OEBPS/stray.xhtml" in verify_epub_file(path)


# --------------------------------------------------------------------------------------------
# 15. CLI
# --------------------------------------------------------------------------------------------


def test_the_cli_builds_and_reports_zero_provider_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _release(root, chapters=2)
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["build-epub", "workspace/releases/book-fa-v1", "--show-manifest"])

    assert result.exit_code == 0, result.stdout
    assert "READY_FOR_PUBLICATION" in result.stdout
    assert "Provider contacted: no" in result.stdout
    assert "mutations: 0" in result.stdout
    assert EPUB_BUILDER_VERSION in result.stdout
    assert (root / "output/book-fa-v1.epub").is_file()


def test_the_cli_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    _release(root, chapters=1)
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["build-epub", "workspace/releases/book-fa-v1", "--dry-run"])

    assert result.exit_code == 0, result.stdout
    assert "Dry run: yes" in result.stdout
    assert "EPUB written: no" in result.stdout
    assert not (root / "output").exists()


def test_the_cli_refuses_an_unverified_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    _rewrite(release / METADATA_FILENAME, "tampered: true\n")
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["build-epub", "workspace/releases/book-fa-v1"])

    assert result.exit_code == 1
    assert not (root / "output").exists()


def test_the_cli_honours_an_explicit_output_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _release(root, chapters=1)
    monkeypatch.chdir(root)

    result = runner.invoke(
        app,
        ["build-epub", "workspace/releases/book-fa-v1", "--output", "dist/mybook.epub"],
    )

    assert result.exit_code == 0, result.stdout
    assert (root / "dist/mybook.epub").is_file()
