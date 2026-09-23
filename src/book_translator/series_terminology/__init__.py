"""Series-aware terminology: governed series policy, book occurrences, and reference retrieval."""

from book_translator.series_terminology.identity import (
    PROMOTION_RULE_VERSION,
    REFERENCE_CONTEXT_CONTRACT_VERSION,
    SERIES_TERMINOLOGY_ENGINE_VERSION,
    TERMINOLOGY_CONTEXT_CONTRACT_VERSION,
)
from book_translator.series_terminology.policy import SeriesTerminologyError

__all__ = [
    "PROMOTION_RULE_VERSION",
    "REFERENCE_CONTEXT_CONTRACT_VERSION",
    "SERIES_TERMINOLOGY_ENGINE_VERSION",
    "TERMINOLOGY_CONTEXT_CONTRACT_VERSION",
    "SeriesTerminologyError",
]
