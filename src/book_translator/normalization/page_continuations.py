"""Conservative prose continuation joining across canonical page boundaries."""

import re

from book_translator.normalization.models import (
    ChangeRecord,
    ParsedDocument,
    PassResult,
    PhysicalLine,
    TextBlock,
)

PASS_NAME = "page-boundary-continuations"
PASS_VERSION = "1.5.0"

_SCENE_SEPARATORS = frozenset({"***", "* * *", "# # #", "— — —"})
_DIALOGUE_OPENERS = frozenset({'"', "“", "‘", "«"})
_DIALOGUE_CLOSERS = ('"', "”", "’", "»")
_TERMINAL_ENDINGS = (".", "!", "?", '"', "”", "’", "»")
_COMPLETE_SENTENCE_BEFORE_MORE = re.compile(r'[.!?](?:["”’»])?\s')
_ORDERED_LIST_PREFIX = re.compile(r"^\d+[.)]\s")
_DIALOGUE_PUNCTUATION_TAIL = re.compile(r'^[—–]\s*["”’»](?:\s|$)')
_NARRATIVE_DASH_CONTINUATION = re.compile(r"^[—–]\s+[a-z]")
_REQUIRED_CAPITALIZED_COMPLEMENT = frozenset({"a", "an", "the"})


def _physical_line_count(block: TextBlock) -> int:
    return len(block.original_text.splitlines())


def _is_structural(block: TextBlock) -> bool:
    stripped = block.original_text.strip()
    return (
        _physical_line_count(block) == 1
        or stripped in _SCENE_SEPARATORS
        or stripped.startswith("#")
        or stripped.startswith(("- ", "* ", "+ "))
        or _ORDERED_LIST_PREFIX.match(stripped) is not None
    )


def _has_unclosed_dialogue(block: TextBlock) -> bool:
    """Recognize only explicit unmatched dialogue delimiters, without parsing prose."""
    text = block.original_text.strip()
    if text.startswith('"') and text.count('"') % 2 == 1:
        return True
    return any(
        text.startswith(opener) and closer not in text
        for opener, closer in (("“", "”"), ("‘", "’"), ("«", "»"))
    )


def _has_any_unclosed_dialogue(block: TextBlock) -> bool:
    """Recognize an unmatched quotation even when narration precedes it."""
    text = block.original_text
    if text.count('"') % 2 == 1:
        return True
    return any(
        text.count(opener) > text.count(closer)
        for opener, closer in (("“", "”"), ("‘", "’"), ("«", "»"))
    )


def _has_prior_complete_sentence(block: TextBlock) -> bool:
    """Identify dialogue containing a completed sentence before its incomplete final clause."""
    text = block.original_text.strip()
    return _COMPLETE_SENTENCE_BEFORE_MORE.search(text[:-1]) is not None


def _edge_word(text: str, *, first: bool) -> str:
    words = text.split()
    if not words:
        return ""
    word = words[0] if first else words[-1]
    return word.strip('"“”‘’«»()[]{}.,!?;:')


def _is_capitalized_name_word(word: str) -> bool:
    """Recognize a conservative title-case word, including an attached possessive."""
    stem = word.removesuffix("'s").removesuffix("’s")
    return len(stem) > 1 and stem[0].isupper() and stem[1:].islower() and stem.isalpha()


def _continues_capitalized_name(previous_text: str, following_text: str) -> bool:
    """Allow a proper name split before a capitalized possessive name word."""
    previous_word = _edge_word(previous_text, first=False)
    following_word = _edge_word(following_text, first=True)
    return (
        _is_capitalized_name_word(previous_word)
        and following_word.endswith(("'s", "’s"))
        and _is_capitalized_name_word(following_word)
    )


def _continues_lowercase_into_possessive_name(previous_text: str, following_text: str) -> bool:
    """Allow an unfinished lowercase word to continue into a capitalized possessive name.

    A prose paragraph never ends on a bare lowercase word with no punctuation at
    all, so the left edge proves the clause is incomplete; the capitalized
    possessive on the right is the noun phrase that completes it rather than a new
    sentence. Structural, verse, dialogue-led, and same-page blocks are excluded
    by the caller before this predicate is consulted.
    """
    previous_words = previous_text.split()
    following_word = _edge_word(following_text, first=True)
    return (
        bool(previous_words)
        and previous_words[-1].isalpha()
        and previous_words[-1].islower()
        and following_word.endswith(("'s", "’s"))
        and _is_capitalized_name_word(following_word)
    )


def _is_guarded_one_line_dialogue_tail(previous: TextBlock, following: TextBlock) -> bool:
    """Allow a short extracted tail only for an established unfinished dialogue block."""
    text = following.original_text.strip()
    return (
        _physical_line_count(following) == 1
        and bool(text)
        and text[0].islower()
        and text.endswith(_DIALOGUE_CLOSERS)
        and _has_unclosed_dialogue(previous)
        and _has_prior_complete_sentence(previous)
    )


def _is_dialogue_punctuation_tail(previous: TextBlock, following_text: str) -> bool:
    """Attach delayed interruption punctuation to the quote it closes."""
    return (
        _has_any_unclosed_dialogue(previous)
        and not previous.original_text.rstrip().endswith(_DIALOGUE_CLOSERS)
        and _DIALOGUE_PUNCTUATION_TAIL.match(following_text) is not None
    )


def _is_narrative_dash_continuation(previous: TextBlock, following_text: str) -> bool:
    """Recognize a lowercase dash clause continuing non-dialogue narration."""
    return not _has_unclosed_dialogue(previous) and (
        _NARRATIVE_DASH_CONTINUATION.match(following_text) is not None
    )


def _requires_capitalized_complement(previous_text: str, following_text: str) -> bool:
    """Allow capitalization after an article that cannot grammatically end a clause."""
    return (
        _edge_word(previous_text, first=False).casefold() in _REQUIRED_CAPITALIZED_COMPLEMENT
        and bool(following_text)
        and following_text[0].isupper()
    )


def _is_clear_continuation(previous: TextBlock, following: TextBlock) -> bool:
    if previous.source_page == following.source_page:
        return False
    if previous.preserve_lines or following.preserve_lines:
        return False
    if _is_structural(previous):
        return False
    previous_text = previous.lines[-1].text.rstrip()
    following_text = following.lines[0].text.lstrip()
    special_continuation = (
        _is_dialogue_punctuation_tail(previous, following_text)
        or _is_narrative_dash_continuation(previous, following_text)
        or _requires_capitalized_complement(previous_text, following_text)
    )
    if (
        _is_structural(following)
        and not _is_guarded_one_line_dialogue_tail(previous, following)
        and not special_continuation
    ):
        return False
    if following.original_text.lstrip()[:1] in _DIALOGUE_OPENERS:
        return False
    if (
        _has_unclosed_dialogue(previous)
        and not _has_prior_complete_sentence(previous)
        and not _is_dialogue_punctuation_tail(previous, following_text)
    ):
        return False
    return (
        bool(previous_text)
        and bool(following_text)
        and (special_continuation or not previous_text.endswith(_TERMINAL_ENDINGS))
        and not previous_text.endswith("-")
        and (
            following_text[0].islower()
            or _continues_capitalized_name(previous_text, following_text)
            or _continues_lowercase_into_possessive_name(previous_text, following_text)
            or special_continuation
        )
    )


def _matched_rule(previous: TextBlock, following: TextBlock) -> str:
    """Name the exact successful predicate for downstream audit evidence."""
    previous_text = previous.lines[-1].text.rstrip()
    following_text = following.lines[0].text.lstrip()
    if _is_dialogue_punctuation_tail(previous, following_text):
        return "dialogue-punctuation-tail"
    if _is_narrative_dash_continuation(previous, following_text):
        return "narrative-dash-continuation"
    if _requires_capitalized_complement(previous_text, following_text):
        return "article-capitalized-complement"
    if _continues_capitalized_name(previous_text, following_text):
        return "capitalized-possessive-name"
    if _continues_lowercase_into_possessive_name(previous_text, following_text):
        return "lowercase-possessive-name-continuation"
    if _is_guarded_one_line_dialogue_tail(previous, following):
        return "guarded-one-line-dialogue-tail"
    return "lowercase-page-continuation"


def _merge(previous: TextBlock, following: TextBlock) -> TextBlock:
    first = previous.lines[0]
    joined_text = f"{previous.lines[-1].text.rstrip()} {following.lines[0].text.lstrip()}"
    return TextBlock(
        source_page=previous.source_page,
        block_index=previous.block_index,
        lines=(
            PhysicalLine(
                joined_text,
                first.source_page,
                first.block_index,
                first.line_index,
            ),
        ),
        original_text=f"{previous.original_text}\n{following.original_text}",
    )


def join_page_continuations(document: ParsedDocument) -> PassResult:
    """Join only structurally clear prose continuations split by a page marker."""
    blocks: list[TextBlock] = []
    changes: list[ChangeRecord] = []
    for block in document.blocks:
        if blocks and _is_clear_continuation(blocks[-1], block):
            previous = blocks.pop()
            blocks.append(_merge(previous, block))
            changes.append(
                ChangeRecord(
                    PASS_NAME,
                    "page_boundary_continuation_join",
                    block.source_page,
                    block.block_index,
                    block.lines[0].line_index,
                    _matched_rule(previous, block),
                )
            )
        else:
            blocks.append(block)
    return PassResult(
        ParsedDocument(
            tuple(blocks), document.page_transitions, document.source_physical_line_count
        ),
        tuple(changes),
    )
