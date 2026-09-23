"""Governed chapter-title translation.

A chapter title is book metadata that the translation run never produced: chunks carry paragraphs,
headings do not appear in them. This stage translates titles under the same frozen terminology the
prose was translated with, publishes one immutable content-addressed artifact, and touches no
translation run, job, attempt, acceptance, or QA state at any point.

Consumers should import :mod:`book_translator.chapter_titles.artifact`, which reads a published
artifact and has no provider or writer of its own.
"""

from book_translator.chapter_titles.artifact import (
    CHAPTER_TITLES_CATEGORY,
    DOCUMENT_FILENAME,
    MANIFEST_FILENAME,
    PROVENANCE_FILENAME,
    ChapterTitleArtifact,
    discover_chapter_title_artifact,
    load_chapter_title_artifact,
    load_chapter_title_provenance,
)
from book_translator.chapter_titles.engine import (
    CHAPTER_TITLE_ENGINE_VERSION,
    CHAPTER_TITLE_OUTPUT_CONTRACT,
    CHAPTER_TITLE_PROMPT_VERSION,
    MAX_TITLE_CHARACTERS,
    ChapterTitleError,
    distinct_source_titles,
    is_target_script,
    normalize_provider_title,
    require_source_title,
    title_sha256,
)
from book_translator.chapter_titles.identity import (
    chapter_title_identity,
    title_content_address,
)
from book_translator.chapter_titles.models import (
    ChapterTitlePlan,
    ChapterTitleResult,
    PlannedTitle,
    TranslatedTitle,
)
from book_translator.chapter_titles.qa import (
    CHAPTER_TITLE_QA_VERSION,
    validate_chapter_titles,
)
from book_translator.chapter_titles.rendering import (
    SYSTEM_MESSAGE,
    enforced_decision_ids,
    render_title_request,
    render_title_terminology,
)
from book_translator.chapter_titles.workflow import (
    execute_chapter_titles,
    plan_chapter_titles,
)

__all__ = [
    "CHAPTER_TITLES_CATEGORY",
    "CHAPTER_TITLE_ENGINE_VERSION",
    "CHAPTER_TITLE_OUTPUT_CONTRACT",
    "CHAPTER_TITLE_PROMPT_VERSION",
    "CHAPTER_TITLE_QA_VERSION",
    "DOCUMENT_FILENAME",
    "MANIFEST_FILENAME",
    "MAX_TITLE_CHARACTERS",
    "PROVENANCE_FILENAME",
    "SYSTEM_MESSAGE",
    "ChapterTitleArtifact",
    "ChapterTitleError",
    "ChapterTitlePlan",
    "ChapterTitleResult",
    "PlannedTitle",
    "TranslatedTitle",
    "chapter_title_identity",
    "discover_chapter_title_artifact",
    "distinct_source_titles",
    "enforced_decision_ids",
    "execute_chapter_titles",
    "is_target_script",
    "load_chapter_title_artifact",
    "load_chapter_title_provenance",
    "normalize_provider_title",
    "plan_chapter_titles",
    "render_title_request",
    "render_title_terminology",
    "require_source_title",
    "title_content_address",
    "title_sha256",
    "validate_chapter_titles",
]
