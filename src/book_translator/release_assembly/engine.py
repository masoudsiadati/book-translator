"""Pure release assembly: chapter and book rendering with no I/O and no rewriting.

Assembly is not a translation step. Nothing here reads a registry, judges a translation, or
changes a character of accepted text; the only decisions it makes are where one accepted chunk
ends and the next begins, and what a chapter file is called.

The join rule is the canonical Markdown paragraph form the rest of the pipeline already uses:
:func:`parse_document` splits an accepted translation into exact blank-line-separated paragraphs
and :func:`render_paragraph_texts` writes them back with one blank line between them and one
final newline. Concatenating chunks that way is the same conservation rule the Document Chunk
Engine proves in the other direction, so a chapter is the reconstruction of its chunks and
nothing else — paragraph order, scene separators, and every Persian byte inside a paragraph
survive untouched. A translation that is not in canonical form is refused rather than tidied.
"""

import re

from book_translator.document_chunking.paragraphs import (
    DocumentParseError,
    parse_document,
    render_paragraph_texts,
)
from book_translator.models.book import TocKind
from book_translator.models.release import ReleaseHeadingSource

RELEASE_ASSEMBLY_ENGINE_VERSION = "1.0.0"

#: The name of one release directory, and therefore of the release itself.
RELEASE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ReleaseAssemblyError(ValueError):
    """Raised when a release cannot be assembled from immutable accepted evidence alone."""


def validate_release_id(release_id: str) -> str:
    """Return *release_id* when it is a filesystem-safe lowercase ASCII name."""
    if not RELEASE_ID_PATTERN.fullmatch(release_id) or len(release_id) > 64:
        raise ReleaseAssemblyError(
            "release ID must be filesystem-safe lowercase ASCII of at most 64 characters"
        )
    if release_id in {".", ".."} or release_id.endswith(".md"):
        raise ReleaseAssemblyError("release ID must not name a relative path or a Markdown file")
    return release_id


def chapter_filename(*, kind: TocKind, number: int | None, chapter_id: str) -> str:
    """Derive one chapter's release filename from its recorded identity.

    A numbered chapter is named by its number and kind, which is what makes a release directory
    sort into reading order in any file browser. Everything else keeps its canonical chapter ID,
    because inventing an ordinal for it would be a claim the workspace never made.
    """
    if number is None:
        return f"{chapter_id}.md"
    if not 0 <= number <= 999:
        raise ReleaseAssemblyError(f"chapter number is out of range for a release: {number}")
    return f"{number:03d}-{kind.value.replace('_', '-')}.md"


def chapter_heading(
    *, title_target: str, title_source: str, title_artifact: str | None = None
) -> tuple[str, ReleaseHeadingSource]:
    """Return the heading paragraph for one chapter and the title it was taken from.

    Precedence is workspace first, artifact second, source last. A title a person recorded in the
    workspace outranks a translated one because it is a decision, not a derivation; a governed
    chapter-title artifact outranks nothing but the fallback. A blank everywhere falls back to the
    source-language title verbatim — never a transliteration, never a guess — and says so.
    """
    target = title_target.strip()
    if target:
        return f"# {target}", ReleaseHeadingSource.TITLE_TARGET
    supplied = (title_artifact or "").strip()
    if supplied:
        return f"# {supplied}", ReleaseHeadingSource.TITLE_TARGET_ARTIFACT
    source = title_source.strip()
    if not source:
        raise ReleaseAssemblyError("a chapter must carry at least one recorded title")
    return f"# {source}", ReleaseHeadingSource.TITLE_SOURCE


def chunk_paragraphs(chunk_id: str, translation_text: str) -> tuple[str, ...]:
    """Split one accepted translation into its canonical paragraphs, or refuse it."""
    try:
        parsed = parse_document(translation_text)
    except DocumentParseError as exc:
        raise ReleaseAssemblyError(
            f"accepted translation for chunk {chunk_id} is not canonical Markdown: {exc}"
        ) from exc
    return tuple(paragraph.text for paragraph in parsed)


def assemble_chapter(
    *, chapter_id: str, heading: str, chunk_texts: tuple[tuple[str, str], ...]
) -> tuple[str, tuple[tuple[str, tuple[str, ...]], ...]]:
    """Assemble one chapter from its ordered accepted chunks.

    Returns the chapter Markdown and, per chunk, the paragraphs it contributed — the caller needs
    the second half to record where each chunk starts without re-deriving it.
    """
    if not chunk_texts:
        raise ReleaseAssemblyError(f"chapter {chapter_id} has no accepted chunks to assemble")
    contributions = tuple(
        (chunk_id, chunk_paragraphs(chunk_id, text)) for chunk_id, text in chunk_texts
    )
    body = tuple(paragraph for _, texts in contributions for paragraph in texts)
    paragraphs = (heading,) + body
    return render_paragraph_texts(paragraphs), contributions


def assemble_book(chapter_markdown: tuple[str, ...]) -> str:
    """Assemble the canonical book from its ordered assembled chapters.

    Each chapter is already canonical, so the book is their concatenation with exactly one blank
    line between them: ``book.md`` is byte-identical to joining the chapter files with ``\\n``.
    """
    if not chapter_markdown:
        raise ReleaseAssemblyError("a release must contain at least one chapter")
    return "\n".join(chapter_markdown)


def word_count(markdown: str) -> int:
    """Count whitespace-separated words, which is a structural measure and never prose."""
    return len(markdown.split())


def validate_chunk_coverage(expected: tuple[str, ...], actual: tuple[str, ...]) -> None:
    """Prove the assembled chunks are exactly the frozen ones, once each and in order.

    Missing and duplicate chunks are told apart deliberately. A missing chunk is a hole in the
    book; a duplicate is the same accepted text published twice, which reads as a doubled scene
    rather than as an error. Both are refused, and each is named for what it is.
    """
    seen: set[str] = set()
    repeated: set[str] = set()
    for chunk_id in actual:
        if chunk_id in seen:
            repeated.add(chunk_id)
        seen.add(chunk_id)
    if repeated:
        listed = ", ".join(sorted(repeated))
        raise ReleaseAssemblyError(f"release assembly repeats chunks: {listed}")
    missing = [chunk_id for chunk_id in expected if chunk_id not in seen]
    if missing:
        raise ReleaseAssemblyError(f"release assembly is missing chunks: {', '.join(missing)}")
    unexpected = sorted(seen.difference(expected))
    if unexpected:
        raise ReleaseAssemblyError(
            f"release assembly contains chunks the snapshot never froze: {', '.join(unexpected)}"
        )
    if actual != expected:
        raise ReleaseAssemblyError("release assembly does not follow frozen chunk order")
