"""Pure structural correspondence between a source chunk and its translation.

Terminology QA has to answer a question about *one source occurrence*: was this occurrence of this
source term realized with the canonical target?  Nothing in the pipeline persists a span-for-span
alignment between a chunk and its translation, so the question has no exact answer available.  What
*is* available is the block structure both sides already contract on: `translate-chunk` renders one
target block per source block, and `paragraph_count_mismatch` is a blocking structural finding
precisely because that correspondence is required.

This module makes that contracted correspondence explicit and addressable, and nothing more.  It
deliberately offers no fuzzy matching, no length heuristic, and no partial alignment: either the
two sides have the same number of blocks — in which case block *i* on one side corresponds to block
*i* on the other, by the same contract the structural validator enforces — or there is no
correspondence here at all and a caller must fail closed rather than guess.  A wrong alignment
would silently move evidence from one occurrence to another, which is the exact failure this
module exists to prevent.
"""

import re
from dataclasses import dataclass

# A paragraph is a block of text, so the separator is one *or more* blank lines and a run of them
# is not an empty paragraph.  Both sides are segmented identically, so the comparison stays a
# comparison.
PARAGRAPH_SEPARATOR = re.compile(r"\n[^\S\n]*\n\s*")


@dataclass(frozen=True, slots=True)
class Paragraph:
    """One text block together with its offsets into the text it was segmented from."""

    index: int
    text: str
    start: int
    end: int


def paragraphs(text: str) -> tuple[Paragraph, ...]:
    """Return the text blocks of *text*, each carrying its offsets into *text*.

    Offsets are kept because the caller's evidence — an authoritative source-term match — is a
    character span, and mapping a span to a block is the whole point of segmenting here rather
    than counting.
    """

    stripped = text.strip()
    if not stripped:
        return ()
    base = text.index(stripped)
    found: list[Paragraph] = []
    cursor = 0
    for block in PARAGRAPH_SEPARATOR.split(stripped):
        start = stripped.index(block, cursor)
        cursor = start + len(block)
        if not block.strip():
            continue
        found.append(
            Paragraph(
                index=len(found), text=block, start=base + start, end=base + start + len(block)
            )
        )
    return tuple(found)


def paragraph_count(text: str) -> int:
    """Count the text blocks in *text*, ignoring blank-line runs and trailing whitespace."""
    return len(paragraphs(text))


@dataclass(frozen=True, slots=True)
class ParagraphAlignment:
    """A block-for-block correspondence between a source text and its translation."""

    source: tuple[Paragraph, ...]
    translation: tuple[Paragraph, ...]

    def source_index(self, offset: int) -> int | None:
        """Return the source block containing *offset*, or ``None`` when it falls between blocks."""
        for block in self.source:
            if block.start <= offset < block.end:
                return block.index
        return None

    def translated(self, index: int) -> str:
        """Return the translation block corresponding to source block *index*."""
        return self.translation[index].text


def paragraph_alignment(source_text: str, translation_text: str) -> ParagraphAlignment | None:
    """Return the block-for-block correspondence, or ``None`` when the two sides do not have one.

    ``None`` is not a soft failure: it means the structural contract this alignment rests on is
    already broken, which the structural validator reports separately as a blocking finding.
    """

    source = paragraphs(source_text)
    translation = paragraphs(translation_text)
    if not source or len(source) != len(translation):
        return None
    return ParagraphAlignment(source=source, translation=translation)
