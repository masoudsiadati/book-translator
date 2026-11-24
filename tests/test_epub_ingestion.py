from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.config import BookConfig
from book_translator.epub_ingestion import (
    EpubIngestionError,
    execute_epub_import,
    inspect_epub,
    plan_epub_import,
)
from book_translator.hashing import sha256_file
from book_translator.models import ChapterMetadata, TocDocument, TocKind

runner = CliRunner()


CONTAINER = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def make_epub(
    path: Path,
    *,
    rootfile: str = "OPS/content.opf",
    opf: str,
    ncx: str | None = None,
    html: dict[str, str] | None = None,
) -> Path:
    container = CONTAINER.replace("OPS/content.opf", rootfile)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        # Deliberately write content in an order unrelated to the OPF spine.
        for name, content in reversed(list((html or {}).items())):
            archive.writestr(name, content)
        if ncx is not None:
            archive.writestr((PurePosixPath(rootfile).parent / "toc.ncx").as_posix(), ncx)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr(rootfile, opf)
    return path


def package_document(spine: list[str]) -> str:
    manifest = "\n".join(
        [
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>',
            '<item id="chapter-two" href="chapter-two.xhtml" media-type="application/xhtml+xml"/>',
            '<item id="map" href="map.xhtml" media-type="application/xhtml+xml"/>',
            '<item id="prologue-a" href="Prologue_split_000.xhtml" '
            'media-type="application/xhtml+xml"/>',
            '<item id="prologue-b" href="Prologue_split_001.xhtml" '
            'media-type="application/xhtml+xml"/>',
            '<item id="chapter-one" href="chapter-one.xhtml" media-type="application/xhtml+xml"/>',
            '<item id="glossary" href="glossary.xhtml" media-type="application/xhtml+xml"/>',
            '<item id="about" href="about-author.xhtml" media-type="application/xhtml+xml"/>',
        ]
    )
    itemrefs = "\n".join(f'<itemref idref="{item}"/>' for item in spine)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Synthetic Book</dc:title><dc:creator>Test Author</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>{manifest}</manifest>
  <spine toc="ncx">{itemrefs}</spine>
</package>
"""


NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>
    <navPoint id="p1"><navLabel><text>Prologue</text></navLabel>
      <content src="Prologue_split_001.xhtml"/></navPoint>
    <navPoint id="p2"><navLabel><text>Opening Mirrors</text></navLabel>
      <content src="Prologue_split_001.xhtml#title"/></navPoint>
    <navPoint id="c2a"><navLabel><text>Chapter 2</text></navLabel>
      <content src="chapter-two.xhtml"/></navPoint>
    <navPoint id="c2b"><navLabel><text>Second Title</text></navLabel>
      <content src="chapter-two.xhtml#title"/></navPoint>
    <navPoint id="c1a"><navLabel><text>Chapter 1</text></navLabel>
      <content src="chapter-one.xhtml"/></navPoint>
    <navPoint id="c1b"><navLabel><text>First &amp; Foremost</text></navLabel>
      <content src="chapter-one.xhtml#title"/></navPoint>
    <navPoint id="g"><navLabel><text>Glossary</text></navLabel>
      <content src="glossary.xhtml"/></navPoint>
  </navMap>
</ncx>
"""


def synthetic_html() -> dict[str, str]:
    return {
        "OPS/title.xhtml": "<html><body><h1>Title</h1></body></html>",
        "OPS/chapter-two.xhtml": "<html><body><p>two</p></body></html>",
        "OPS/map.xhtml": "<html><body><p>map</p></body></html>",
        "OPS/Prologue_split_000.xhtml": "<html><body><p>opening</p></body></html>",
        "OPS/Prologue_split_001.xhtml": "<html><body><p>continuation</p></body></html>",
        "OPS/chapter-one.xhtml": "<html><body><p>one</p></body></html>",
        "OPS/glossary.xhtml": "<html><body><p>terms</p></body></html>",
        "OPS/about-author.xhtml": "<html><body><p>bio</p></body></html>",
    }


def test_spine_order_wins_and_ncx_fragment_label_supplies_title(tmp_path: Path) -> None:
    epub = make_epub(
        tmp_path / "book.epub",
        opf=package_document(
            [
                "title",
                "chapter-two",
                "map",
                "prologue-a",
                "prologue-b",
                "chapter-one",
                "glossary",
                "about",
            ]
        ),
        ncx=NCX,
        html=synthetic_html(),
    )

    document = inspect_epub(epub)

    assert document.title == "Synthetic Book"
    assert document.author == "Test Author"
    assert document.language == "en"
    assert [(unit.kind, unit.number, unit.title) for unit in document.narrative_units] == [
        (TocKind.CHAPTER, 2, "Second Title"),
        (TocKind.PROLOGUE, 0, "Opening Mirrors"),
        (TocKind.CHAPTER, 1, "First & Foremost"),
    ]


def test_consecutive_split_resources_merge_into_one_logical_unit(tmp_path: Path) -> None:
    epub = make_epub(
        tmp_path / "book.epub",
        opf=package_document(["prologue-a", "prologue-b", "chapter-one"]),
        ncx=NCX,
        html=synthetic_html(),
    )

    document = inspect_epub(epub)

    prologue = document.narrative_units[0]
    assert prologue.kind is TocKind.PROLOGUE
    assert prologue.number == 0
    assert [resource.href.as_posix() for resource in prologue.resources] == [
        "OPS/Prologue_split_000.xhtml",
        "OPS/Prologue_split_001.xhtml",
    ]


def test_non_narrative_spine_items_are_excluded(tmp_path: Path) -> None:
    epub = make_epub(
        tmp_path / "book.epub",
        opf=package_document(["title", "map", "glossary", "about", "chapter-one"]),
        ncx=NCX,
        html=synthetic_html(),
    )

    document = inspect_epub(epub)

    assert [unit.number for unit in document.narrative_units] == [1]
    assert document.spine_item_count == 5


def test_structural_chapter_label_wins_when_title_starts_with_map(tmp_path: Path) -> None:
    ncx = NCX.replace("First &amp; Foremost", "Map of Stars")
    epub = make_epub(
        tmp_path / "book.epub",
        opf=package_document(["map", "chapter-one"]),
        ncx=ncx,
        html=synthetic_html(),
    )

    document = inspect_epub(epub)

    assert [(unit.number, unit.title) for unit in document.narrative_units] == [(1, "Map of Stars")]


def test_manifest_href_may_resolve_parent_segments_without_escaping_archive(
    tmp_path: Path,
) -> None:
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Synthetic Book</dc:title><dc:creator>Test Author</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest><item id="chapter" href="../../Text/Chapter 01.xhtml"
    media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""
    epub = make_epub(
        tmp_path / "book.epub",
        rootfile="OPS/package/content.opf",
        opf=opf,
        html={"Text/Chapter 01.xhtml": "<html><body><p>synthetic</p></body></html>"},
    )

    document = inspect_epub(epub)

    assert document.narrative_units[0].resources[0].href == PurePosixPath("Text/Chapter 01.xhtml")


@pytest.mark.parametrize(
    "filename",
    ["chapter01.xhtml", "chapter-01.xhtml", "chapter_01.xhtml", "chapter 01.xhtml"],
)
def test_chapter_filename_fallback_accepts_conservative_digit_separators(
    tmp_path: Path, filename: str
) -> None:
    opf = f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Synthetic Book</dc:title><dc:creator>Test Author</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest><item id="chapter" href="{filename}"
    media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""
    epub = make_epub(
        tmp_path / "book.epub",
        opf=opf,
        html={f"OPS/{filename}": "<html><body><p>synthetic</p></body></html>"},
    )

    document = inspect_epub(epub)

    assert [(unit.kind, unit.number) for unit in document.narrative_units] == [(TocKind.CHAPTER, 1)]


def test_filename_fallback_does_not_treat_bare_ordinal_as_narrative(tmp_path: Path) -> None:
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Synthetic Book</dc:title><dc:creator>Test Author</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest><item id="unknown" href="1 Introduction.xhtml"
    media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="unknown"/></spine>
</package>
"""
    epub = make_epub(
        tmp_path / "book.epub",
        opf=opf,
        html={"OPS/1 Introduction.xhtml": "<html><body><p>synthetic</p></body></html>"},
    )

    with pytest.raises(EpubIngestionError, match="no discoverable narrative units"):
        inspect_epub(epub)


def test_nested_opf_discovers_combined_prologue_and_ordinal_chapters_in_spine_order(
    tmp_path: Path,
) -> None:
    narrative_items = [
        ("prologue", "frontmatter02.xhtml", "Prologue: Snow", "Snow"),
        *[
            (
                f"chapter-{number}",
                f"chapter{number:02d}.xhtml",
                (
                    f"{number} Synthetic Title {number:02d}"
                    if number % 3 == 1
                    else f"{number}. Synthetic Title {number:02d}"
                    if number % 3 == 2
                    else f"{number}: Synthetic Title {number:02d}"
                ),
                f"Synthetic Title {number:02d}",
            )
            for number in range(1, 36)
        ],
    ]
    manifest_items = [
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="cover" href="xhtml/cover.xhtml" media-type="application/xhtml+xml"/>',
        *[
            f'<item id="{item_id}" href="xhtml/{filename}" media-type="application/xhtml+xml"/>'
            for item_id, filename, _, _ in narrative_items
        ],
        '<item id="about" href="xhtml/about.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine_ids = ["cover", *(item_id for item_id, _, _, _ in narrative_items), "about"]
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Synthetic Nested Book</dc:title><dc:creator>Test Author</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>{"".join(manifest_items)}</manifest>
  <spine toc="ncx">{"".join(f'<itemref idref="{item_id}"/>' for item_id in spine_ids)}</spine>
</package>
"""
    ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>{
        "".join(
            f'<navPoint id="nav-{item_id}"><navLabel><text>{label}</text></navLabel>'
            f'<content src="xhtml/{filename}"/></navPoint>'
            for item_id, filename, label, _ in narrative_items
        )
    }</navMap>
</ncx>
"""
    html = {
        "OPS/xhtml/cover.xhtml": "<html><body><p>synthetic cover</p></body></html>",
        **{
            f"OPS/xhtml/{filename}": "<html><body><p>synthetic narrative</p></body></html>"
            for _, filename, _, _ in narrative_items
        },
        "OPS/xhtml/about.xhtml": "<html><body><p>synthetic about page</p></body></html>",
    }
    epub = make_epub(
        tmp_path / "book.epub",
        rootfile="OPS/package.opf",
        opf=opf,
        ncx=ncx,
        html=html,
    )

    document = inspect_epub(epub)

    assert len(document.narrative_units) == 36
    assert [(unit.kind, unit.number) for unit in document.narrative_units] == [
        (TocKind.PROLOGUE, 0),
        *((TocKind.CHAPTER, number) for number in range(1, 36)),
    ]
    assert [unit.title for unit in document.narrative_units] == [
        "Snow",
        *(f"Synthetic Title {number:02d}" for number in range(1, 36)),
    ]
    assert all(
        resource.href.parent == PurePosixPath("OPS/xhtml")
        for unit in document.narrative_units
        for resource in unit.resources
    )
    positions = [unit.resources[0].spine_position for unit in document.narrative_units]
    assert positions == list(range(2, 38))


def test_combined_chapter_and_epilogue_labels_recover_title_suffixes(tmp_path: Path) -> None:
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Synthetic Book</dc:title><dc:creator>Test Author</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="chapter" href="chapter01.xhtml" media-type="application/xhtml+xml"/>
    <item id="epilogue" href="backmatter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx"><itemref idref="chapter"/><itemref idref="epilogue"/></spine>
</package>
"""
    ncx = """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap>
  <navPoint><navLabel><text>Chapter 1: First Light</text></navLabel>
    <content src="chapter01.xhtml"/></navPoint>
  <navPoint><navLabel><text>Epilogue: Last Light</text></navLabel>
    <content src="backmatter.xhtml"/></navPoint>
</navMap></ncx>
"""
    epub = make_epub(
        tmp_path / "book.epub",
        opf=opf,
        ncx=ncx,
        html={
            "OPS/chapter01.xhtml": "<html><body><p>synthetic chapter</p></body></html>",
            "OPS/backmatter.xhtml": "<html><body><p>synthetic epilogue</p></body></html>",
        },
    )

    document = inspect_epub(epub)

    assert [(unit.kind, unit.number, unit.title) for unit in document.narrative_units] == [
        (TocKind.CHAPTER, 1, "First Light"),
        (TocKind.EPILOGUE, None, "Last Light"),
    ]


@pytest.mark.parametrize(
    ("rootfile", "opf", "message"),
    [
        ("../content.opf", package_document(["chapter-one"]), "unsafe archive path"),
        (
            "OPS/content.opf",
            package_document(["chapter-one"]).replace('idref="chapter-one"', 'idref="missing"'),
            "missing manifest item",
        ),
    ],
)
def test_invalid_package_references_fail_closed(
    tmp_path: Path, rootfile: str, opf: str, message: str
) -> None:
    epub = make_epub(
        tmp_path / "book.epub", rootfile=rootfile, opf=opf, ncx=NCX, html=synthetic_html()
    )

    with pytest.raises(EpubIngestionError, match=message):
        inspect_epub(epub)


def extraction_html() -> dict[str, str]:
    return {
        "OPS/Prologue_split_000.xhtml": """<html><head><style>noise</style></head><body>
          <nav><p>navigation noise</p></nav><h2>Prologue</h2>
          <p>First <em>emphasized</em> paragraph.</p><p>Second—dialogue “stays.”</p>
        </body></html>""",
        "OPS/Prologue_split_001.xhtml": """<html><body><h4>Opening Mirrors</h4>
          <p>Third <i>continued</i> paragraph.</p><img src="ornament.png" alt=""/>
        </body></html>""",
        "OPS/chapter-one.xhtml": """<html><body><h3>Chapter 1</h3>
          <h4>First &amp; Foremost</h4><p>Chapter paragraph.</p>
        </body></html>""",
    }


def test_import_preserves_paragraphs_emphasis_and_split_resource_order(tmp_path: Path) -> None:
    epub = make_epub(
        tmp_path / "book.epub",
        opf=package_document(["prologue-a", "prologue-b", "chapter-one"]),
        ncx=NCX,
        html=extraction_html(),
    )

    plan = plan_epub_import(epub, project_root=tmp_path, book="synthetic")
    result = execute_epub_import(plan, project_root=tmp_path)

    assert result.imported_ids == ("000-prologue", "001")
    prologue = tmp_path / "workspace/books/synthetic/chapters/000-prologue"
    assert prologue.joinpath("source.md").read_text(encoding="utf-8") == (
        "<!-- source-page: 1 -->\n\n"
        "First *emphasized* paragraph.\n\n"
        "Second—dialogue “stays.”\n\n"
        "<!-- source-page: 2 -->\n\n"
        "Third *continued* paragraph.\n"
    )
    metadata = ChapterMetadata.model_validate(
        yaml.safe_load(prologue.joinpath("metadata.yaml").read_text(encoding="utf-8"))
    )
    assert metadata.title_en == "Opening Mirrors"
    assert metadata.source_paragraph_count == 3
    assert metadata.source_sha256 == sha256_file(prologue / "source.md")
    assert metadata.extraction_version == "epub/1.0.0"


def test_import_preserves_direct_container_text_as_a_paragraph(tmp_path: Path) -> None:
    html = extraction_html()
    html["OPS/chapter-one.xhtml"] = (
        "<html><body><h3>Chapter 1</h3><h4>First &amp; Foremost</h4>"
        "<div>Direct <em>container</em> text.</div><p>Next paragraph.</p></body></html>"
    )
    epub = make_epub(
        tmp_path / "book.epub",
        opf=package_document(["chapter-one"]),
        ncx=NCX,
        html=html,
    )

    plan = plan_epub_import(epub, project_root=tmp_path, book="synthetic")

    assert plan.units[0].source_text == (
        "<!-- source-page: 1 -->\n\nDirect *container* text.\n\nNext paragraph.\n"
    )


def test_import_writes_epub_lineage_toc_and_exact_rerun_skips(tmp_path: Path) -> None:
    epub = make_epub(
        tmp_path / "book.epub",
        opf=package_document(["prologue-a", "prologue-b", "chapter-one"]),
        ncx=NCX,
        html=extraction_html(),
    )
    first = plan_epub_import(epub, project_root=tmp_path, book="synthetic")
    execute_epub_import(first, project_root=tmp_path)

    toc = TocDocument.model_validate(
        yaml.safe_load(
            tmp_path.joinpath("workspace/books/synthetic/toc.yaml").read_text(encoding="utf-8")
        )
    )
    assert toc.source == "epub_spine"
    assert toc.source_document == Path("book.epub")
    assert toc.source_document_sha256 == sha256_file(epub)
    assert toc.source_item_count == 3
    assert [(entry.kind, entry.number, entry.title_en) for entry in toc.entries] == [
        (TocKind.PROLOGUE, 0, "Opening Mirrors"),
        (TocKind.CHAPTER, 1, "First & Foremost"),
    ]

    rerun = plan_epub_import(epub, project_root=tmp_path, book="synthetic")
    assert [item.action for item in rerun.units] == ["skip", "skip"]
    assert execute_epub_import(rerun, project_root=tmp_path).imported_ids == ()


def test_force_refuses_to_replace_import_with_downstream_artifacts(tmp_path: Path) -> None:
    epub = make_epub(
        tmp_path / "book.epub",
        opf=package_document(["chapter-one"]),
        ncx=NCX,
        html=extraction_html(),
    )
    plan = plan_epub_import(epub, project_root=tmp_path, book="synthetic")
    execute_epub_import(plan, project_root=tmp_path)
    chapter = tmp_path / "workspace/books/synthetic/chapters/001"
    chapter.joinpath("cleaned.md").write_text("derived\n", encoding="utf-8")

    with pytest.raises(EpubIngestionError, match="downstream artifacts exist"):
        plan_epub_import(epub, project_root=tmp_path, book="synthetic", force=True)


def config_data(**source: str) -> dict[str, object]:
    return {
        "book_id": "synthetic-en-fa",
        "title_en": "Synthetic Book",
        "author": "Test Author",
        "source_language": "en",
        "target_language": "fa",
        "chunk_target_words": 10,
        "chunk_max_words": 20,
        "previous_context_words": 5,
        "output_format": "epub3",
        **source,
    }


def test_config_accepts_one_pdf_or_epub_source_and_rejects_ambiguous_source() -> None:
    legacy = BookConfig.model_validate(config_data(pdf_path="input/book.pdf"))
    epub = BookConfig.model_validate(config_data(epub_path="input/book.epub"))

    assert legacy.source_format == "pdf"
    assert legacy.source_path == Path("input/book.pdf")
    assert epub.source_format == "epub"
    assert epub.source_path == Path("input/book.epub")
    with pytest.raises(ValidationError, match="exactly one"):
        BookConfig.model_validate(config_data())
    with pytest.raises(ValidationError, match="exactly one"):
        BookConfig.model_validate(
            config_data(pdf_path="input/book.pdf", epub_path="input/book.epub")
        )


def test_import_epub_cli_dry_run_then_imports_configured_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.joinpath("input").mkdir()
    epub = make_epub(
        tmp_path / "input/book.epub",
        opf=package_document(["chapter-one"]),
        ncx=NCX,
        html=extraction_html(),
    )
    config_path = tmp_path / "config/books/synthetic.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump(config_data(epub_path="input/book.epub"), sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    dry_run = runner.invoke(app, ["import-epub", "--book", "synthetic", "--dry-run"])

    assert dry_run.exit_code == 0, dry_run.output
    assert "Narrative units" in dry_run.stdout
    assert "not written" in dry_run.stdout
    assert not Path("workspace/books/synthetic").exists()

    imported = runner.invoke(app, ["import-epub", "--book", "synthetic"])

    assert imported.exit_code == 0, imported.output
    assert "Units imported" in imported.stdout
    assert Path("workspace/books/synthetic/chapters/001/source.md").is_file()
    assert epub.is_file()
