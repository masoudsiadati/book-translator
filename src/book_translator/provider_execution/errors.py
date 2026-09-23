"""Sanitized provider and executor error classifications."""

from dataclasses import dataclass

from book_translator.models import ProviderErrorCategory
from book_translator.paragraph_markers.models import MarkerFinding


@dataclass(frozen=True, slots=True)
class ErrorClassification:
    category: ProviderErrorCategory
    retryable: bool
    ambiguous: bool
    safe_automatic_retry: bool
    message: str
    provider_status: int | None = None
    provider_code: str | None = None


@dataclass(frozen=True, slots=True)
class MarkerContractDiagnostics:
    """Prose-free evidence about why an answer broke the numbered paragraph contract.

    Every field is structural: the contract version the answer was judged against, how many
    markers the source defined, how many marker lines the answer carried, and the findings with
    their codes, expected and observed marker tokens, and paragraph indexes.  ``observed_marker``
    is always a bare marker-like token or ``None``; the line it was found on is never quoted.
    """

    contract_version: str
    expected_marker_count: int
    observed_marker_count: int
    findings: tuple[MarkerFinding, ...]


class ProviderExecutionError(RuntimeError):
    """Provider failure with prose- and secret-free classification.

    ``marker_diagnostics`` is set only by post-response structural validation of a candidate that
    arrived whole, so the executor can record *which* marker failed beside the classification.
    The candidate text itself is never carried here: it is the executor's to retain or drop.
    """

    def __init__(
        self,
        classification: ErrorClassification,
        *,
        marker_diagnostics: MarkerContractDiagnostics | None = None,
    ) -> None:
        super().__init__(classification.message)
        self.classification = classification
        self.marker_diagnostics = marker_diagnostics


class ExecutorError(ValueError):
    """Raised when execution cannot proceed safely."""
