"""EPUB Builder 1.0.0 — a publication format converter over a verified release.

The builder's only input is a release directory that release verification has just judged
``READY_FOR_PUBLICATION``. It never reads a translation run, job, attempt, provider artifact, QA
record, or snapshot, it has no provider import and no path to one, and it makes no editorial
decision: chapters, their order, their headings, and their paragraphs arrive already settled.

Given the same release it produces the same bytes. The book identifier is derived from the
release's content address, the modification timestamp is the release's own, archive entry times are
fixed, and entry order is the release's order — so an EPUB is a pure function of the release it
publishes.
"""

from book_translator.epub.engine import (
    CHAPTER_DIRECTORY,
    CONTAINER_PATH,
    EPUB_BUILDER_VERSION,
    EPUB_VERSION,
    MIMETYPE,
    MIMETYPE_PATH,
    NAVIGATION_PATH,
    PACKAGE_PATH,
    STYLESHEET_PATH,
    EpubBuildError,
    book_identifier,
    chapter_href,
    chapter_item_id,
    render_chapter,
    render_container,
    render_navigation,
    render_package,
    render_stylesheet,
)
from book_translator.epub.identity import epub_content_address, epub_id
from book_translator.epub.models import (
    EpubBuildPlan,
    EpubBuildResult,
    EpubChapterRecord,
    EpubFile,
)
from book_translator.epub.validation import validate_archive
from book_translator.epub.workflow import (
    execute_epub_build,
    plan_epub_build,
    read_archive,
    render_archive,
    validate_epub_bytes,
    verify_epub_file,
)

__all__ = [
    "CHAPTER_DIRECTORY",
    "CONTAINER_PATH",
    "EPUB_BUILDER_VERSION",
    "EPUB_VERSION",
    "MIMETYPE",
    "MIMETYPE_PATH",
    "NAVIGATION_PATH",
    "PACKAGE_PATH",
    "STYLESHEET_PATH",
    "EpubBuildError",
    "EpubBuildPlan",
    "EpubBuildResult",
    "EpubChapterRecord",
    "EpubFile",
    "book_identifier",
    "chapter_href",
    "chapter_item_id",
    "epub_content_address",
    "epub_id",
    "execute_epub_build",
    "plan_epub_build",
    "read_archive",
    "render_archive",
    "render_chapter",
    "render_container",
    "render_navigation",
    "render_package",
    "render_stylesheet",
    "validate_archive",
    "validate_epub_bytes",
    "verify_epub_file",
]
