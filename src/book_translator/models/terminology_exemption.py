"""Strict contracts for user-adjudicated, occurrence-specific terminology exemptions.

A terminology decision governs a spelling everywhere it occurs. Some spellings are both a story
term and an ordinary word ("stilled" is the severing of a channeler and also what a woman does to
her fidgeting hands), and widening or narrowing the decision would be wrong in one sense or the
other. An exemption names exactly one source occurrence the user has adjudicated as the ordinary
sense, so that one span stops selecting the decision while every other occurrence stays governed.

A record carries no source prose. It binds its occurrence to the exact chunk bytes by hash, to
the exact span by offsets and ordinal, and to the exact decision by identity, so any change to
the source, the registry, or the policy makes it fail closed instead of drifting onto some other
occurrence.
"""

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel

_CHUNK_ID = r"^(?:000-prologue|[0-9]{3}|999-epilogue)-[0-9]{3}$"


class OccurrenceSense(StrEnum):
    """Why the occurrence is not the governed term."""

    ORDINARY_ENGLISH = "ordinary_english"


class TerminologyOccurrenceExemption(ContractModel):
    exemption_id: str = Field(pattern=r"^toe-[0-9a-f]{24}$")
    chunk_id: str = Field(pattern=_CHUNK_ID)
    chunk_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: The registry candidate and decision the occurrence would otherwise select.
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    decision_id: str = Field(pattern=r"^td-[0-9a-f]{24}$")
    #: Canonical digest of the whole decision record; any change to it retires the exemption.
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: The series concept the prompt's policy tier would otherwise select; absent for a book
    #: without a series policy.
    concept_id: str | None = Field(
        default=None, pattern=r"^sc-[0-9a-f]{24}$", exclude_if=lambda value: value is None
    )
    #: The byte-exact spelling matched at the span, which is a term or alias spelling, not prose.
    spelling: str = Field(min_length=1, max_length=120)
    #: 1-based ordinal of this spelling's boundary-safe matches in the chunk source.
    occurrence_index: int = Field(ge=1)
    #: Character offsets into the decoded chunk source.
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    #: 1-based source paragraph that contains the span, and that paragraph's SHA-256.
    paragraph_number: int = Field(ge=1)
    paragraph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sense: OccurrenceSense
    decided_by: Literal["user"] = "user"
    rationale: str = Field(min_length=1)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def span_is_coherent(self) -> "TerminologyOccurrenceExemption":
        if self.end - self.start != len(self.spelling):
            raise ValueError("an exemption span must be exactly as long as its spelling")
        return self


class TerminologyOccurrenceExemptionRegistry(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    registry_kind: Literal["terminology_occurrence_exemptions"] = (
        "terminology_occurrence_exemptions"
    )
    book_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    exemptions: list[TerminologyOccurrenceExemption] = Field(default_factory=list)

    @model_validator(mode="after")
    def identities_are_unique(self) -> "TerminologyOccurrenceExemptionRegistry":
        ids = [item.exemption_id for item in self.exemptions]
        if len(ids) != len(set(ids)):
            raise ValueError("exemption IDs must be unique")
        spans = [(item.chunk_id, item.start, item.end) for item in self.exemptions]
        if len(spans) != len(set(spans)):
            raise ValueError("one source occurrence may carry at most one exemption")
        if ids != sorted(ids):
            raise ValueError("exemptions must be stored in exemption-ID order")
        return self
