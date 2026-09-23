"""Versioned pure policies for literary diff governance and bounded correction."""

from dataclasses import dataclass

from book_translator.models import LiteraryDiffClass

LITERARY_EDIT_POLICY_VERSION = "1.1.0"
LITERARY_CORRECTION_POLICY_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class LiteraryEditPolicy:
    policy_version: str = LITERARY_EDIT_POLICY_VERSION
    normal_character_change_ratio: float = 0.35
    broad_character_change_ratio: float = 0.70
    normal_absolute_word_delta_ratio: float = 0.25
    broad_absolute_word_delta_ratio: float = 0.50
    semantic_batch_size: int = 4
    suspicious_context_radius: int = 2
    section_overlap_paragraphs: int = 2
    minimum_section_core_paragraphs: int = 8
    # Real Opus pilot evidence showed that a 48k-character bilingual chapter can return an
    # identity-incomplete structured answer.  This bound keeps chapter-global context through the
    # frozen profile and two-paragraph overlaps while limiting each uniquely owned response core.
    max_request_characters: int = 28_000
    max_editor_contract_retries: int = 1
    max_continuity_contract_retries: int = 1


@dataclass(frozen=True, slots=True)
class LiteraryCorrectionPolicy:
    policy_version: str = LITERARY_CORRECTION_POLICY_VERSION
    max_correction_rounds: int = 2
    max_second_looks: int = 1


DEFAULT_EDIT_POLICY = LiteraryEditPolicy()
DEFAULT_CORRECTION_POLICY = LiteraryCorrectionPolicy()


def classify_diff(
    character_change_ratio: float,
    word_delta: int,
    faithful_word_count: int,
    *,
    policy: LiteraryEditPolicy = DEFAULT_EDIT_POLICY,
) -> LiteraryDiffClass:
    """Classify breadth without rejecting broad literary work."""
    word_ratio = abs(word_delta) / max(1, faithful_word_count)
    if (
        character_change_ratio <= policy.normal_character_change_ratio
        and word_ratio <= policy.normal_absolute_word_delta_ratio
    ):
        return LiteraryDiffClass.NORMAL
    if (
        character_change_ratio <= policy.broad_character_change_ratio
        and word_ratio <= policy.broad_absolute_word_delta_ratio
    ):
        return LiteraryDiffClass.BROAD
    return LiteraryDiffClass.SUSPICIOUS
