"""Deterministic, provider-free paragraph alignment QA."""

from book_translator.paragraph_alignment.engine import (
    LEGACY_POSITIONAL,
    MARKED,
    AlignmentAssessment,
    AlignmentFinding,
    assess_alignment,
)
from book_translator.paragraph_alignment.metrics import ParagraphMetrics, measure_pair
from book_translator.paragraph_alignment.policy import (
    ADJACENT_BOUNDARY_DRIFT_SUSPECTED,
    ALIGNMENT_NOT_EVALUABLE,
    ALIGNMENT_QA_VERSION,
    DIALOGUE_MARKS_ABSENT,
    EN_FA_V1,
    GENERIC_V1,
    NUMERAL_NOT_PRESERVED,
    PARAGRAPH_WORD_RATIO_OUT_OF_BAND,
    AlignmentPolicy,
    policy_for,
)
from book_translator.paragraph_alignment.reporting import build_alignment_report

__all__ = [
    "ADJACENT_BOUNDARY_DRIFT_SUSPECTED",
    "ALIGNMENT_NOT_EVALUABLE",
    "ALIGNMENT_QA_VERSION",
    "DIALOGUE_MARKS_ABSENT",
    "EN_FA_V1",
    "GENERIC_V1",
    "LEGACY_POSITIONAL",
    "MARKED",
    "NUMERAL_NOT_PRESERVED",
    "PARAGRAPH_WORD_RATIO_OUT_OF_BAND",
    "AlignmentAssessment",
    "AlignmentFinding",
    "AlignmentPolicy",
    "ParagraphMetrics",
    "assess_alignment",
    "build_alignment_report",
    "measure_pair",
    "policy_for",
]
