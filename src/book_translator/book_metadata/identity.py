"""Pure, deterministic book identity derivation and placeholder detection."""

import re
import unicodedata

BOOK_METADATA_ENGINE_VERSION = "1.0.0"

_SEPARATOR = re.compile(r"[^a-z0-9]+")
_BOOK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# Generic scaffolding vocabulary, never a book title. "synthetic" is deliberately absent: the
# testing policy uses synthetic fixtures as legitimate content rather than as unfilled scaffolding.
PLACEHOLDER_TOKENS = frozenset(
    {
        "changeme",
        "example",
        "fixme",
        "ipsum",
        "lorem",
        "placeholder",
        "sample",
        "tbd",
        "todo",
        "untitled",
        "نمونه",
    }
)
_TOKEN_SEPARATOR = re.compile(r"[^0-9a-z؀-ۿ]+")


class BookIdentityError(ValueError):
    """Raised when a stable filesystem-safe book identity cannot be derived."""


def slugify(value: str) -> str:
    """Fold a title to lowercase ASCII words joined by hyphens."""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_folded = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return _SEPARATOR.sub("-", ascii_folded.casefold()).strip("-")


def derive_book_id(*, title_en: str, source_language: str, target_language: str) -> str:
    """Derive a stable filesystem-safe book ID from the resolved English title."""
    parts = [slugify(title_en), slugify(source_language), slugify(target_language)]
    if not all(parts):
        raise BookIdentityError("book ID needs a title and both language codes")
    candidate = "-".join(parts)
    if not _BOOK_ID.fullmatch(candidate):
        raise BookIdentityError(f"derived book ID is not filesystem-safe: {candidate!r}")
    return candidate


def is_placeholder(value: object) -> bool:
    """Report whether a metadata value is unfilled project scaffolding rather than a fact."""
    if not isinstance(value, str) or not value.strip():
        return False
    tokens = {token for token in _TOKEN_SEPARATOR.split(value.casefold()) if token}
    return bool(tokens & PLACEHOLDER_TOKENS)
