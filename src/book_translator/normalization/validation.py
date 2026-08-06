"""Post-normalization structural and character-integrity validation."""

import re

from book_translator.normalization.models import ParsedDocument

PASS_NAME = "validation"
PASS_VERSION = "1.0"
_MARKER = re.compile(r"^<!-- source-page: [1-9]\d* -->$", re.MULTILINE)


class NormalizationValidationError(ValueError):
    """Raised when deterministic normalization violates an invariant."""


def _non_whitespace(text: str) -> str:
    return "".join(character for character in text if not character.isspace())


def validate_output(source: str, document: ParsedDocument, cleaned: str) -> None:
    if not cleaned.strip():
        raise NormalizationValidationError("cleaned output is empty")
    if "\r" in cleaned:
        raise NormalizationValidationError("cleaned output contains CR characters")
    if _MARKER.search(cleaned):
        raise NormalizationValidationError("canonical page marker remains in cleaned output")
    if not cleaned.endswith("\n") or cleaned.endswith("\n\n"):
        raise NormalizationValidationError("cleaned output must end with exactly one newline")
    if len([part for part in cleaned.rstrip("\n").split("\n\n") if part]) != len(document.blocks):
        raise NormalizationValidationError("source block order or correspondence was not preserved")
    source_without_markers = "\n".join(
        line for line in source.splitlines() if not _MARKER.fullmatch(line)
    )
    if _non_whitespace(source_without_markers) != _non_whitespace(cleaned):
        raise NormalizationValidationError("unexpected non-whitespace character loss or change")
    for character, label in (
        ('"', "straight quotes"),
        ("“", "left curly quotes"),
        ("”", "right curly quotes"),
        ("—", "em dashes"),
        ("–", "en dashes"),
        ("'", "apostrophes"),
        ("’", "curly apostrophes"),
    ):
        if source_without_markers.count(character) != cleaned.count(character):
            raise NormalizationValidationError(f"unexpected change to {label}")
