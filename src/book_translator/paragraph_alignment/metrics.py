"""Deterministic per-paragraph measurements over one aligned source/target pair.

Every measurement here is a count over characters the two texts actually contain.  Nothing
consults a dictionary, a language model, a locale, or the clock, so the same two paragraphs
always produce the same numbers on any machine — which is what makes a threshold policy over
them auditable rather than anecdotal.

Words are counted by whitespace separation rather than by a Unicode word regular expression.
That is not a simplification: Persian binds compounds and clitics with a zero-width non-joiner,
and a `\\w+` scan splits those into several tokens, inflating the target count against an English
source that writes the same idea as one word.  The pilot band this project's policy uses was
measured with whitespace separation, and reproducing the band requires reproducing the counting
rule it was measured with.
"""

import re
import unicodedata
from dataclasses import dataclass

#: Sentence-final punctuation shared by the English source and the Persian target, plus the
#: Persian question mark.  Runs collapse, so an ellipsis followed by a full stop counts once.
_SENTENCE_END = re.compile(r"[.!?…؟]+")

#: Double-quotation forms either side may use for direct speech.  Persian literary typesetting
#: uses guillemets where English uses quotation marks, so both families are one class.
_DIALOGUE_MARK = re.compile(r"[\"“”«»„‟]")

_DIGIT_RUN = re.compile(r"\d+")

__all__ = [
    "ParagraphMetrics",
    "dialogue_marks",
    "measure_pair",
    "numerals",
    "sentence_count",
    "word_count",
]


def word_count(text: str) -> int:
    """Count whitespace-separated tokens."""
    return len(text.split())


def character_count(text: str) -> int:
    """Count non-whitespace characters, so reflowed line breaks cannot change the measurement."""
    return sum(1 for character in text if not character.isspace())


def sentence_count(text: str) -> int:
    """Count sentence-final punctuation runs."""
    return len(_SENTENCE_END.findall(text))


def dialogue_marks(text: str) -> int:
    """Count the quotation characters that mark direct speech on either side."""
    return len(_DIALOGUE_MARK.findall(text))


def numerals(text: str) -> tuple[str, ...]:
    """Return the numeric runs of *text* with every decimal digit folded to its ASCII value.

    Persian text writes the same number with Extended Arabic-Indic digits, so comparing raw
    characters would report a difference where the translation is correct.  Folding through the
    Unicode decimal value compares the *number* instead, which is the property that has to be
    preserved.
    """
    folded = "".join(
        str(unicodedata.decimal(character)) if character.isdigit() else character
        for character in text
    )
    return tuple(sorted(_DIGIT_RUN.findall(folded)))


@dataclass(frozen=True, slots=True)
class ParagraphMetrics:
    """Every measurement the alignment policy is allowed to reason about, for one pair."""

    index: int
    source_words: int
    target_words: int
    source_characters: int
    target_characters: int
    source_sentences: int
    target_sentences: int
    source_numerals: tuple[str, ...]
    target_numerals: tuple[str, ...]
    source_dialogue_marks: int
    target_dialogue_marks: int

    @property
    def word_ratio(self) -> float:
        """Target words per source word, or ``0.0`` for an empty source paragraph."""
        return self.target_words / self.source_words if self.source_words else 0.0

    @property
    def character_ratio(self) -> float:
        return self.target_characters / self.source_characters if self.source_characters else 0.0

    @property
    def missing_numerals(self) -> tuple[str, ...]:
        """Return the source numerals the target does not account for, counting duplicates."""
        remaining = list(self.target_numerals)
        missing: list[str] = []
        for value in self.source_numerals:
            if value in remaining:
                remaining.remove(value)
            else:
                missing.append(value)
        return tuple(missing)


def measure_pair(index: int, source: str, target: str) -> ParagraphMetrics:
    """Measure one aligned source/target paragraph pair."""
    return ParagraphMetrics(
        index=index,
        source_words=word_count(source),
        target_words=word_count(target),
        source_characters=character_count(source),
        target_characters=character_count(target),
        source_sentences=sentence_count(source),
        target_sentences=sentence_count(target),
        source_numerals=numerals(source),
        target_numerals=numerals(target),
        source_dialogue_marks=dialogue_marks(source),
        target_dialogue_marks=dialogue_marks(target),
    )
