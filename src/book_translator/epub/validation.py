"""Pure post-generation validation of a built EPUB archive.

The builder does not trust its own output. Everything here reads the finished archive back the way
a reading system would — structure first, then XML, then the book's own claims against the release
it came from — and returns every discrepancy rather than the first one.
"""

import xml.etree.ElementTree as ElementTree
from xml.etree.ElementTree import ParseError

from book_translator.epub.engine import (
    CONTAINER_PATH,
    MIMETYPE,
    MIMETYPE_PATH,
    NAVIGATION_PATH,
    PACKAGE_PATH,
    STYLESHEET_PATH,
    chapter_href,
)

OPF_NAMESPACE = "http://www.idpf.org/2007/opf"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
XHTML_NAMESPACE = "http://www.w3.org/1999/xhtml"
CONTAINER_NAMESPACE = "urn:oasis:names:tc:opendocument:xmlns:container"


def validate_archive(
    entries: tuple[str, ...],
    contents: dict[str, bytes],
    *,
    expected_chapters: tuple[tuple[str, str], ...],
    expected_title: str,
    expected_language: str,
) -> tuple[str, ...]:
    """Re-verify a built archive against the release it claims to publish.

    ``expected_chapters`` is ``(chapter_id, title)`` in published order.
    """
    problems: list[str] = []
    problems.extend(_structure_problems(entries, contents, expected_chapters))
    if problems:
        # Later checks parse documents the structure check just said are absent.
        return tuple(problems)
    problems.extend(_container_problems(contents))
    problems.extend(
        _package_problems(contents, expected_chapters, expected_title, expected_language)
    )
    problems.extend(_navigation_problems(contents, expected_chapters))
    problems.extend(_chapter_problems(contents, expected_chapters))
    return tuple(problems)


def _structure_problems(
    entries: tuple[str, ...],
    contents: dict[str, bytes],
    expected_chapters: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    problems: list[str] = []
    if not entries or entries[0] != MIMETYPE_PATH:
        problems.append("mimetype must be the first entry in the archive")
    if contents.get(MIMETYPE_PATH) != MIMETYPE.encode("ascii"):
        problems.append("mimetype does not hold the EPUB media type")
    required = (
        CONTAINER_PATH,
        PACKAGE_PATH,
        NAVIGATION_PATH,
        STYLESHEET_PATH,
        *(f"OEBPS/{chapter_href(chapter_id)}" for chapter_id, _ in expected_chapters),
    )
    problems.extend(
        f"required archive entry is missing: {path}" for path in required if path not in contents
    )
    expected_paths = {MIMETYPE_PATH, *required}
    problems.extend(
        f"archive holds an unexpected entry: {path}"
        for path in sorted(set(entries) - expected_paths)
    )
    if len(entries) != len(set(entries)):
        problems.append("the archive holds a duplicate entry")
    for path, content in sorted(contents.items()):
        if path == MIMETYPE_PATH:
            continue
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"archive entry is not valid UTF-8: {path}")
    return tuple(problems)


def _parse(
    contents: dict[str, bytes], path: str, problems: list[str]
) -> ElementTree.Element | None:
    try:
        return ElementTree.fromstring(contents[path].decode("utf-8"))
    except (ParseError, UnicodeDecodeError):
        problems.append(f"archive entry is not well-formed XML: {path}")
        return None


def _container_problems(contents: dict[str, bytes]) -> tuple[str, ...]:
    problems: list[str] = []
    root = _parse(contents, CONTAINER_PATH, problems)
    if root is None:
        return tuple(problems)
    rootfile = root.find(f"{{{CONTAINER_NAMESPACE}}}rootfiles/{{{CONTAINER_NAMESPACE}}}rootfile")
    if rootfile is None or rootfile.get("full-path") != PACKAGE_PATH:
        problems.append("container.xml does not point at the package document")
    return tuple(problems)


def _package_problems(
    contents: dict[str, bytes],
    expected_chapters: tuple[tuple[str, str], ...],
    expected_title: str,
    expected_language: str,
) -> tuple[str, ...]:
    problems: list[str] = []
    root = _parse(contents, PACKAGE_PATH, problems)
    if root is None:
        return tuple(problems)
    if root.get("version") != "3.0":
        problems.append("the package document is not EPUB 3.0")
    metadata = root.find(f"{{{OPF_NAMESPACE}}}metadata")
    if metadata is None:
        problems.append("the package document has no metadata")
        return tuple(problems)

    def text(tag: str) -> str | None:
        node = metadata.find(f"{{{DC_NAMESPACE}}}{tag}")
        return None if node is None or node.text is None else node.text

    if text("title") != expected_title:
        problems.append("the package title is not the release's book title")
    if text("language") != expected_language:
        problems.append("the package language is not the release's target language")
    identifier = text("identifier")
    if identifier is None or not identifier.startswith("urn:uuid:"):
        problems.append("the package has no stable book identifier")
    if not text("creator"):
        problems.append("the package records no author")
    modified = [
        node.text
        for node in metadata.findall(f"{{{OPF_NAMESPACE}}}meta")
        if node.get("property") == "dcterms:modified"
    ]
    if len(modified) != 1 or not modified[0]:
        problems.append("the package must record exactly one dcterms:modified")

    manifest = root.find(f"{{{OPF_NAMESPACE}}}manifest")
    spine = root.find(f"{{{OPF_NAMESPACE}}}spine")
    if manifest is None or spine is None:
        problems.append("the package document has no manifest or spine")
        return tuple(problems)
    if spine.get("page-progression-direction") != "rtl":
        problems.append("the spine does not read right to left")
    hrefs = [item.get("href") for item in manifest.findall(f"{{{OPF_NAMESPACE}}}item")]
    if "nav.xhtml" not in hrefs or "styles/rtl.css" not in hrefs:
        problems.append("the manifest does not carry the navigation document and stylesheet")
    nav_items = [
        item
        for item in manifest.findall(f"{{{OPF_NAMESPACE}}}item")
        if (item.get("properties") or "").split().count("nav")
    ]
    if len(nav_items) != 1:
        problems.append("the manifest must declare exactly one navigation document")
    chapter_hrefs = [href for href in hrefs if href and href.startswith("chapters/")]
    expected_hrefs = [chapter_href(chapter_id) for chapter_id, _ in expected_chapters]
    if chapter_hrefs != expected_hrefs:
        problems.append("the manifest chapters are not the release's chapters in order")
    spine_ids = [item.get("idref") for item in spine.findall(f"{{{OPF_NAMESPACE}}}itemref")]
    manifest_ids = {
        item.get("id"): item.get("href") for item in manifest.findall(f"{{{OPF_NAMESPACE}}}item")
    }
    if [manifest_ids.get(identity) for identity in spine_ids] != expected_hrefs:
        problems.append("the spine does not read the release's chapters in order")
    return tuple(problems)


def _navigation_problems(
    contents: dict[str, bytes], expected_chapters: tuple[tuple[str, str], ...]
) -> tuple[str, ...]:
    problems: list[str] = []
    root = _parse(contents, NAVIGATION_PATH, problems)
    if root is None:
        return tuple(problems)
    links = [
        (anchor.get("href"), (anchor.text or "").strip())
        for anchor in root.iter(f"{{{XHTML_NAMESPACE}}}a")
    ]
    expected = [(chapter_href(chapter_id), title) for chapter_id, title in expected_chapters]
    if links != expected:
        problems.append("the navigation document does not list every chapter title in order")
    return tuple(problems)


def _chapter_problems(
    contents: dict[str, bytes], expected_chapters: tuple[tuple[str, str], ...]
) -> tuple[str, ...]:
    problems: list[str] = []
    for chapter_id, title in expected_chapters:
        path = f"OEBPS/{chapter_href(chapter_id)}"
        root = _parse(contents, path, problems)
        if root is None:
            continue
        headings = [(node.text or "").strip() for node in root.iter(f"{{{XHTML_NAMESPACE}}}h1")]
        if headings != [title]:
            problems.append(f"chapter does not carry its release title once: {chapter_id}")
        body = root.find(f"{{{XHTML_NAMESPACE}}}body")
        if body is None or body.get("dir") != "rtl":
            problems.append(f"chapter body is not right-to-left: {chapter_id}")
    return tuple(problems)
