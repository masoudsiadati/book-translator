"""Pure chapter-title transformation and validation. No I/O, no provider, no clock.

The stage exists because a chapter title is the one part of a chapter the translation run never
translated: chunks carry paragraphs, headings are metadata. Everything here decides what a title
*is* — never what a translation run holds — and it fails closed rather than guessing.
"""

import hashlib
import re
import unicodedata

CHAPTER_TITLE_ENGINE_VERSION = "1.0.0"
CHAPTER_TITLE_PROMPT_VERSION = "1.1.0"
CHAPTER_TITLE_OUTPUT_CONTRACT = "persian-chapter-title-text/1.0.0"

#: A heading is one line. Anything longer than this is prose, a refusal, or an explanation.
MAX_TITLE_CHARACTERS = 200

#: Arabic-script ranges covering Persian, including the Arabic Presentation Forms.
_TARGET_SCRIPT = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_LATIN_WORD = re.compile(r"\b[A-Za-z]{3,}\b")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PROMPT_FRAMING = re.compile(
    r"<framed-data|</?chapter-title|<system-instructions|<user-data|```", re.IGNORECASE
)


class ChapterTitleError(ValueError):
    """Raised when a chapter title cannot be produced or trusted."""


def title_sha256(title: str) -> str:
    """Hash one title's exact UTF-8 bytes. The title is hashed as written, never normalized."""
    return hashlib.sha256(title.encode("utf-8")).hexdigest()


def normalize_provider_title(raw: str) -> str:
    """Reduce one provider answer to a single trimmed title line, or refuse it.

    A provider that returns a paragraph, a refusal, a code fence, or the prompt's own framing has
    not answered the question, and silently taking its first line would turn that into a heading
    nobody chose. Only surrounding whitespace and a single pair of wrapping quotes are removed.
    """
    if not isinstance(raw, str):
        raise ChapterTitleError("provider returned a non-text chapter title")
    text = unicodedata.normalize("NFC", raw.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if _PROMPT_FRAMING.search(text):
        raise ChapterTitleError("provider chapter title reproduced prompt framing")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        raise ChapterTitleError("provider returned an empty chapter title")
    if len(lines) != 1:
        raise ChapterTitleError("provider returned more than one line for one chapter title")
    title = lines[0]
    for opening, closing in (('"', '"'), ("'", "'"), ("«", "»"), ("“", "”")):
        if len(title) > 1 and title.startswith(opening) and title.endswith(closing):
            title = title[1:-1].strip()
            break
    if _CONTROL.search(title):
        raise ChapterTitleError("provider chapter title contains control characters")
    if not title:
        raise ChapterTitleError("provider returned an empty chapter title")
    if len(title) > MAX_TITLE_CHARACTERS:
        raise ChapterTitleError(f"provider chapter title exceeds {MAX_TITLE_CHARACTERS} characters")
    return title


def is_target_script(title: str) -> bool:
    """Return whether a title carries any Persian/Arabic-script character."""
    return _TARGET_SCRIPT.search(title) is not None


def latin_word_ratio(title: str) -> float:
    """Return the share of whitespace-separated words that are Latin words of three or more."""
    words = title.split()
    if not words:
        return 0.0
    return len(_LATIN_WORD.findall(title)) / len(words)


def distinct_source_titles(titles: tuple[str, ...]) -> tuple[str, ...]:
    """Return each distinct English title once, in first-appearance order.

    Deduplication is what makes "the same English title always gets the same Persian title" a
    property of the artifact rather than a hope about the provider: an identical title is asked
    once and the answer is reused, so two chapters cannot disagree.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for title in titles:
        if title not in seen:
            seen.add(title)
            ordered.append(title)
    return tuple(ordered)


def require_source_title(chapter_id: str, title_en: str) -> str:
    """Return the trimmed English title, refusing a chapter that has none."""
    title = title_en.strip()
    if not title:
        raise ChapterTitleError(f"chapter {chapter_id} has no source title to translate")
    if "\n" in title or "\r" in title:
        raise ChapterTitleError(f"chapter {chapter_id} has a multi-line source title")
    return title
