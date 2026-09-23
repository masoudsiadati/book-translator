"""Pure EPUB rendering. No I/O, no clock, no randomness.

The builder is a *format converter* and nothing more. It receives canonical Markdown chapters that
a verified release already proved complete, and turns them into XHTML, an OPF package document, a
navigation document, and a stylesheet. It makes no editorial decision: it does not reflow, retitle,
renumber, merge, or drop anything, and the only transformation applied to a paragraph's text is the
XML escaping the format requires.

Determinism is a property of every function here. Given the same release bytes each one returns the
same string — no timestamps of its own, no generated identifiers, no dependence on iteration order
that is not already fixed by the release manifest.
"""

import re
import uuid
from xml.sax.saxutils import escape

from book_translator.document_chunking.paragraphs import (
    DocumentParseError,
    parse_document,
)

EPUB_BUILDER_VERSION = "1.0.0"
EPUB_VERSION = "3.0"

CONTAINER_PATH = "META-INF/container.xml"
PACKAGE_PATH = "OEBPS/content.opf"
NAVIGATION_PATH = "OEBPS/nav.xhtml"
STYLESHEET_PATH = "OEBPS/styles/rtl.css"
CHAPTER_DIRECTORY = "OEBPS/chapters"
MIMETYPE_PATH = "mimetype"
MIMETYPE = "application/epub+zip"

#: The UUID namespace the book identifier is derived in. A release's content address is hashed into
#: a v5 UUID, so the same release always yields the same ``dc:identifier`` and a different release
#: never accidentally reuses one.
IDENTIFIER_NAMESPACE = uuid.NAMESPACE_URL
IDENTIFIER_PREFIX = "https://book-translator.invalid/release/"

_NCNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*$")


class EpubBuildError(ValueError):
    """Raised when an EPUB cannot be rendered from the release it was given."""


def chapter_href(chapter_id: str) -> str:
    """Derive one chapter's EPUB-relative path from its canonical identity."""
    if not _NCNAME.fullmatch(f"ch-{chapter_id}"):
        raise EpubBuildError(f"chapter identity is not usable as an EPUB name: {chapter_id}")
    return f"chapters/{chapter_id}.xhtml"


def chapter_item_id(chapter_id: str) -> str:
    """Derive one chapter's OPF manifest item ID, which must be an XML name."""
    identifier = f"ch-{chapter_id}"
    if not _NCNAME.fullmatch(identifier):
        raise EpubBuildError(f"chapter identity is not a valid XML name: {chapter_id}")
    return identifier


def book_identifier(release_content_sha256: str) -> str:
    """Derive the book's stable URN from the release's own content address."""
    if not re.fullmatch(r"[0-9a-f]{64}", release_content_sha256):
        raise EpubBuildError("a book identifier needs the release content address")
    derived = uuid.uuid5(IDENTIFIER_NAMESPACE, IDENTIFIER_PREFIX + release_content_sha256)
    return f"urn:uuid:{derived}"


def render_stylesheet() -> str:
    """Render the right-to-left stylesheet. It sets direction and nothing typographic."""
    return (
        "html {\n"
        "  direction: rtl;\n"
        "}\n"
        "\n"
        "body {\n"
        "  direction: rtl;\n"
        "  text-align: right;\n"
        "  margin: 0 5%;\n"
        "  line-height: 1.7;\n"
        "}\n"
        "\n"
        "h1 {\n"
        "  direction: rtl;\n"
        "  text-align: center;\n"
        "  page-break-before: always;\n"
        "  margin: 2em 0 1.5em;\n"
        "}\n"
        "\n"
        "p {\n"
        "  direction: rtl;\n"
        "  text-align: justify;\n"
        "  text-indent: 1.5em;\n"
        "  margin: 0;\n"
        "}\n"
        "\n"
        "p.first {\n"
        "  text-indent: 0;\n"
        "}\n"
        "\n"
        "p.scene-separator {\n"
        "  text-align: center;\n"
        "  text-indent: 0;\n"
        "  margin: 1.5em 0;\n"
        "}\n"
        "\n"
        "nav ol {\n"
        "  direction: rtl;\n"
        "  text-align: right;\n"
        "  list-style-type: none;\n"
        "  padding-inline-start: 0;\n"
        "}\n"
    )


def render_container() -> str:
    """Render the OCF container document that points at the package."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        f'    <rootfile full-path="{PACKAGE_PATH}" '
        'media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    )


def _paragraph_markup(text: str, *, first: bool) -> str:
    """Render one canonical paragraph as one XHTML block, preserving its text exactly.

    A paragraph may span several lines — verse, an address, a broken-up inscription. The line
    structure is meaning, so it is kept with ``<br/>`` rather than flattened into a space.
    """
    lines = text.split("\n")
    body = "<br/>\n".join(escape(line) for line in lines)
    classes = " ".join(part for part in ("first" if first else "",) if part)
    attribute = f' class="{classes}"' if classes else ""
    return f"<p{attribute}>{body}</p>"


def _scene_separator_markup(text: str) -> str:
    return f'<p class="scene-separator">{escape(text)}</p>'


def render_chapter(
    *, chapter_id: str, markdown: str, language: str, expected_heading: str
) -> tuple[str, str]:
    """Convert one canonical Markdown chapter to XHTML, returning it and its title.

    The chapter's first paragraph is its heading and becomes the ``<h1>``; every other paragraph
    becomes a ``<p>`` in the order it was published. Nothing is reordered, merged, or dropped, and
    the heading is checked against the one the release recorded rather than trusted.
    """
    try:
        paragraphs = parse_document(markdown)
    except DocumentParseError as exc:
        raise EpubBuildError(f"chapter {chapter_id} is not canonical Markdown: {exc}") from exc
    heading_paragraph = paragraphs[0]
    if heading_paragraph.text != expected_heading:
        raise EpubBuildError(
            f"chapter {chapter_id} does not open with the heading the release recorded"
        )
    if not heading_paragraph.text.startswith("# "):
        raise EpubBuildError(f"chapter {chapter_id} does not open with a Markdown heading")
    title = heading_paragraph.text[2:].strip()
    if not title:
        raise EpubBuildError(f"chapter {chapter_id} has an empty heading")

    blocks = [f"<h1>{escape(title)}</h1>"]
    for index, paragraph in enumerate(paragraphs[1:]):
        if paragraph.is_scene_separator:
            blocks.append(_scene_separator_markup(paragraph.text))
        else:
            blocks.append(_paragraph_markup(paragraph.text, first=index == 0))
    body = "\n".join(blocks)
    document = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{escape(language)}" lang="{escape(language)}" dir="rtl">\n'
        "<head>\n"
        '  <meta charset="utf-8"/>\n'
        f"  <title>{escape(title)}</title>\n"
        '  <link rel="stylesheet" type="text/css" href="../styles/rtl.css"/>\n'
        "</head>\n"
        '<body dir="rtl">\n'
        f'<section epub:type="chapter" id="{escape(chapter_item_id(chapter_id))}">\n'
        f"{body}\n"
        "</section>\n"
        "</body>\n"
        "</html>\n"
    )
    return document, title


def render_navigation(
    *, book_title: str, language: str, chapters: tuple[tuple[str, str], ...]
) -> str:
    """Render the EPUB 3 navigation document: ``(chapter_id, title)`` in published order."""
    if not chapters:
        raise EpubBuildError("a navigation document needs at least one chapter")
    entries = "\n".join(
        f'      <li><a href="{escape(chapter_href(chapter_id))}">{escape(title)}</a></li>'
        for chapter_id, title in chapters
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{escape(language)}" lang="{escape(language)}" dir="rtl">\n'
        "<head>\n"
        '  <meta charset="utf-8"/>\n'
        f"  <title>{escape(book_title)}</title>\n"
        '  <link rel="stylesheet" type="text/css" href="styles/rtl.css"/>\n'
        "</head>\n"
        '<body dir="rtl">\n'
        '  <nav epub:type="toc" id="toc" dir="rtl">\n'
        f"    <h1>{escape(book_title)}</h1>\n"
        "    <ol>\n"
        f"{entries}\n"
        "    </ol>\n"
        "  </nav>\n"
        "</body>\n"
        "</html>\n"
    )


def render_package(
    *,
    identifier: str,
    title: str,
    author: str,
    language: str,
    modified: str,
    source: str,
    release_id: str,
    series_name: str | None,
    series_index: int | None,
    chapters: tuple[str, ...],
) -> str:
    """Render the OPF package document.

    ``modified`` is taken from the release rather than from the clock, which is what makes two
    builds of one release byte-identical. The release's own content address travels in ``dc:source``
    so a published book names the artifact it was made from.
    """
    if not chapters:
        raise EpubBuildError("a package document needs at least one chapter")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", modified):
        raise EpubBuildError("dcterms:modified must be a whole-second UTC timestamp")
    collection = ""
    if series_name:
        collection = (
            f'    <meta property="belongs-to-collection" id="series">{escape(series_name)}'
            "</meta>\n"
            '    <meta refines="#series" property="collection-type">series</meta>\n'
        )
        if series_index is not None:
            collection += (
                f'    <meta refines="#series" property="group-position">{series_index}</meta>\n'
            )
    manifest_items = "\n".join(
        f'    <item id="{escape(chapter_item_id(chapter_id))}" '
        f'href="{escape(chapter_href(chapter_id))}" media-type="application/xhtml+xml"/>'
        for chapter_id in chapters
    )
    spine_items = "\n".join(
        f'    <itemref idref="{escape(chapter_item_id(chapter_id))}"/>' for chapter_id in chapters
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<package xmlns="http://www.idpf.org/2007/opf" version="{EPUB_VERSION}" '
        f'unique-identifier="book-id" xml:lang="{escape(language)}" dir="rtl">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="book-id">{escape(identifier)}</dc:identifier>\n'
        f"    <dc:title>{escape(title)}</dc:title>\n"
        f"    <dc:creator>{escape(author)}</dc:creator>\n"
        f"    <dc:language>{escape(language)}</dc:language>\n"
        f"    <dc:source>{escape(source)}</dc:source>\n"
        f'    <meta property="dcterms:modified">{modified}</meta>\n'
        f'    <meta property="book-translator:release">{escape(release_id)}</meta>\n'
        f"{collection}"
        "  </metadata>\n"
        "  <manifest>\n"
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/>\n'
        '    <item id="rtl-css" href="styles/rtl.css" media-type="text/css"/>\n'
        f"{manifest_items}\n"
        "  </manifest>\n"
        '  <spine page-progression-direction="rtl">\n'
        f"{spine_items}\n"
        "  </spine>\n"
        "</package>\n"
    )
