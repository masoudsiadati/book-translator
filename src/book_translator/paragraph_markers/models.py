"""Typed results of parsing a marked paragraph document.

Nothing here is persisted directly. The findings are converted into the persisted alignment
finding contract by the caller that needs a durable record, which keeps this layer free of the
schema-versioning concerns that belong to `book_translator.models`.
"""

from dataclasses import dataclass

__all__ = ["MarkerFinding", "MarkerParseResult"]


@dataclass(frozen=True, slots=True)
class MarkerFinding:
    """One structural violation of the numbered paragraph contract.

    ``source_paragraph_index`` is the 1-based index of the source paragraph the finding is about
    whenever the violation names one; it is ``None`` for a violation about the document as a whole,
    such as text standing before the first marker.
    """

    code: str
    severity: str
    expected_marker: str | None
    observed_marker: str | None
    source_paragraph_index: int | None
    message: str


@dataclass(frozen=True, slots=True)
class MarkerParseResult:
    """The complete answer for one marked document: is it conformant, and what does it contain?

    ``blocks`` holds the paragraph texts exactly as they were written under their markers, with
    the marker lines removed and nothing else changed.  It is populated only when ``valid`` is
    true: a document that violated the contract has no trustworthy paragraph identities, and
    handing a caller a partial reconstruction is precisely the guessing this contract forbids.
    """

    valid: bool
    expected_count: int
    observed_markers: tuple[str, ...]
    blocks: tuple[str, ...]
    findings: tuple[MarkerFinding, ...]

    @property
    def text(self) -> str:
        """Return the marker-free document: the blocks, in order, in canonical block form."""
        if not self.valid:
            raise ValueError("a nonconformant marked document has no marker-free text")
        return "\n\n".join(self.blocks) + "\n"
