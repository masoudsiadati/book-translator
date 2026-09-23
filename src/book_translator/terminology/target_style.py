"""Deterministic target-style normalization for AI-generated canonical terms."""

from dataclasses import dataclass

DEFAULT_TARGET_STYLE_VERSION = "identity/1.0.0"
PERSIAN_TARGET_STYLE_VERSION = "fa-canonical-target/1.0.0"

# The Persian style contract disallows these optional short-vowel presentation marks.
# Other combining marks, letters, join controls, spacing, and morphology remain significant.
_PERSIAN_OPTIONAL_SHORT_VOWELS: dict[int, int | None] = str.maketrans("", "", "\u064e\u064f\u0650")


@dataclass(frozen=True, slots=True)
class TargetStyleAdapter:
    """A versioned exact transformation for AI-generated canonical targets."""

    version: str
    removable_characters: dict[int, int | None]

    def normalize_ai_target(self, value: str) -> str:
        """Normalize presentation style without changing lexical spelling."""
        return value.translate(self.removable_characters)

    def canonical_key(self, value: str) -> str:
        """Return the documented exact canonical-target comparison key."""
        return self.normalize_ai_target(value)


def target_style_adapter(target_language: str) -> TargetStyleAdapter:
    """Select target-language behavior behind a language-specific abstraction."""
    language = target_language.lower().split("-", 1)[0]
    if language == "fa":
        return TargetStyleAdapter(PERSIAN_TARGET_STYLE_VERSION, _PERSIAN_OPTIONAL_SHORT_VOWELS)
    return TargetStyleAdapter(DEFAULT_TARGET_STYLE_VERSION, {})


def canonical_targets_equal(left: str, right: str, target_language: str) -> bool:
    """Compare canonical targets without fuzzy, morphological, or semantic equivalence."""
    adapter = target_style_adapter(target_language)
    return adapter.canonical_key(left) == adapter.canonical_key(right)
