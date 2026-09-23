"""Pure planning and fail-closed resolution of occurrence-specific terminology exemptions.

An exemption is a user's adjudication that one source occurrence of a governed spelling is not
the governed term (see :mod:`book_translator.models.terminology_exemption`). This module turns a
request into a record and a record back into the exact spans it withholds, and refuses whenever
the two no longer describe the same occurrence: a changed chunk, a moved span, an ordinal that
now points elsewhere, a decision that was re-identified, or a span another term now claims.

Only the source text of the chunk the record names is ever affected. The same words appearing in
another chunk's context window are not the adjudicated occurrence and stay governed there.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from book_translator.models import (
    OccurrenceSense,
    SeriesTerminologyPolicy,
    SeriesTermStatus,
    TerminologyDecision,
    TerminologyOccurrenceExemption,
    TerminologyOccurrenceExemptionRegistry,
    TerminologyRegistry,
)
from book_translator.paragraph_markers import source_paragraphs
from book_translator.terminology.registry import (
    ExemptOccurrences,
    TerminologyError,
    boundary_spans,
    source_term_matches,
)

TERMINOLOGY_EXEMPTION_ENGINE_VERSION = "1.0.0"
EXEMPTION_REGISTRY_SCHEMA_VERSION = "1.0"


class TerminologyExemptionError(TerminologyError):
    """Raised when an exemption cannot be proven against the exact occurrence it names."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def exemption_identity(
    *, chunk_id: str, chunk_source_sha256: str, candidate_id: str, start: int, end: int
) -> str:
    """Address an exemption by the exact occurrence it adjudicates."""
    payload = {
        "candidate_id": candidate_id,
        "chunk_id": chunk_id,
        "chunk_source_sha256": chunk_source_sha256,
        "end": end,
        "engine_version": TERMINOLOGY_EXEMPTION_ENGINE_VERSION,
        "start": start,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"toe-{hashlib.sha256(encoded).hexdigest()[:24]}"


def decision_digest(decision: TerminologyDecision) -> str:
    """Canonical digest of a decision record, so a re-decided term retires its exemptions."""
    encoded = json.dumps(
        decision.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _spelling_spans(text: str, spelling: str) -> tuple[tuple[int, int], ...]:
    # The same boundary rule every terminology matcher uses; nothing here folds case.
    return boundary_spans(spelling, text)


def paragraph_offsets(text: str) -> tuple[tuple[int, str], ...]:
    """Each canonical source paragraph with its character offset into *text*."""
    located: list[tuple[int, str]] = []
    cursor = 0
    for block in source_paragraphs(text):
        position = text.find(block, cursor)
        if position < 0:
            raise TerminologyExemptionError("source paragraphs cannot be located in their text")
        located.append((position, block))
        cursor = position + len(block)
    return tuple(located)


def _paragraph_of(text: str, start: int, end: int) -> tuple[int, str]:
    """The 1-based canonical paragraph holding the span, and that paragraph's text."""
    for number, (position, block) in enumerate(paragraph_offsets(text), start=1):
        if position <= start and end <= position + len(block):
            return number, block
    raise TerminologyExemptionError("the exempt span does not lie inside one source paragraph")


def paragraph_exemptions(
    exemptions: ExemptOccurrences | None, *, source_text: str, block_start: int, block: str
) -> ExemptOccurrences | None:
    """Rebase a chunk's proven exemptions onto one of its paragraphs, for paragraph-level QA."""
    if exemptions is None:
        return None
    exemptions.require_text(source_text)
    block_end = block_start + len(block)
    if source_text[block_start:block_end] != block:
        raise TerminologyExemptionError("the paragraph does not lie at its recorded offset")

    def inside(
        spans: frozenset[tuple[str, int, int]],
    ) -> frozenset[tuple[str, int, int]]:
        return frozenset(
            (owner, start - block_start, end - block_start)
            for owner, start, end in spans
            if block_start <= start and end <= block_end
        )

    spans, concepts = inside(exemptions.spans), inside(exemptions.concept_spans)
    if not spans and not concepts:
        return None
    return ExemptOccurrences(
        source_sha256=_sha256(block),
        spans=spans,
        exemption_ids=exemptions.exemption_ids,
        concept_spans=concepts,
    )


def _decided_concepts(policy: SeriesTerminologyPolicy, spelling: str) -> list[str]:
    return [
        term.concept_id
        for term in policy.terms
        if term.status in {SeriesTermStatus.LOCKED, SeriesTermStatus.STRONG_REFERENCE}
        and spelling in (term.source_term, *term.aliases)
    ]


def _proven_span(
    record: TerminologyOccurrenceExemption,
    registry: TerminologyRegistry,
    policy: SeriesTerminologyPolicy | None,
    text: str,
) -> None:
    """Re-derive every fact the record binds from the text and resources, or refuse."""
    label = f"exemption {record.exemption_id}"
    if _sha256(text) != record.chunk_source_sha256:
        raise TerminologyExemptionError(f"{label}: chunk {record.chunk_id} source changed")
    spans = _spelling_spans(text, record.spelling)
    if record.occurrence_index > len(spans) or spans[record.occurrence_index - 1] != (
        record.start,
        record.end,
    ):
        raise TerminologyExemptionError(f"{label}: the occurrence ordinal no longer names its span")
    number, block = _paragraph_of(text, record.start, record.end)
    if number != record.paragraph_number or _sha256(block) != record.paragraph_sha256:
        raise TerminologyExemptionError(f"{label}: the containing paragraph changed")
    matches = [
        match
        for match in source_term_matches(registry, text)
        if (match.start, match.end) == (record.start, record.end)
    ]
    if len(matches) != 1 or matches[0].candidate_id != record.candidate_id:
        raise TerminologyExemptionError(
            f"{label}: the span is not an authoritative match of its candidate"
        )
    decision = next(
        (item for item in registry.decisions if item.candidate_id == record.candidate_id), None
    )
    if (
        decision is None
        or decision.decision_id != record.decision_id
        or decision_digest(decision) != record.decision_sha256
    ):
        raise TerminologyExemptionError(f"{label}: the exempted decision changed")
    if policy is None:
        if record.concept_id is not None:
            raise TerminologyExemptionError(f"{label}: names a series concept but no policy exists")
        return
    concepts = _decided_concepts(policy, record.spelling)
    if record.concept_id is None or concepts != [record.concept_id]:
        raise TerminologyExemptionError(
            f"{label}: the spelling is not owned by exactly its recorded series concept"
        )


def resolve_occurrence_exemptions(
    exemptions: TerminologyOccurrenceExemptionRegistry | None,
    *,
    registry: TerminologyRegistry,
    policy: SeriesTerminologyPolicy | None,
    chunk_id: str,
    source_text: str,
) -> ExemptOccurrences | None:
    """The proven exempt spans of one chunk's source, or None when none name it."""
    if exemptions is None:
        return None
    records = [item for item in exemptions.exemptions if item.chunk_id == chunk_id]
    if not records:
        return None
    for record in records:
        _proven_span(record, registry, policy, source_text)
    return ExemptOccurrences(
        source_sha256=_sha256(source_text),
        spans=frozenset((item.candidate_id, item.start, item.end) for item in records),
        exemption_ids=tuple(sorted(item.exemption_id for item in records)),
        concept_spans=frozenset(
            (item.concept_id, item.start, item.end)
            for item in records
            if item.concept_id is not None
        ),
    )


@dataclass(frozen=True, slots=True)
class PlannedExemption:
    record: TerminologyOccurrenceExemption
    #: Occurrences of the same spelling in the same chunk that stay governed.
    remaining_occurrences: int


def plan_occurrence_exemption(
    *,
    registry: TerminologyRegistry,
    policy: SeriesTerminologyPolicy | None,
    chunk_id: str,
    source_text: str,
    spelling: str,
    occurrence_index: int,
    sense: OccurrenceSense,
    rationale: str,
    created_at: datetime,
) -> PlannedExemption:
    """Derive the record for the *occurrence_index*-th boundary match of *spelling*."""
    if not rationale.strip():
        raise TerminologyExemptionError("an exemption must record the user's rationale")
    spans = _spelling_spans(source_text, spelling)
    if not 1 <= occurrence_index <= len(spans):
        raise TerminologyExemptionError(
            f"chunk {chunk_id} has {len(spans)} occurrence(s) of {spelling!r}, "
            f"not an occurrence {occurrence_index}"
        )
    start, end = spans[occurrence_index - 1]
    matches = [
        match
        for match in source_term_matches(registry, source_text)
        if (match.start, match.end) == (start, end)
    ]
    if len(matches) != 1:
        raise TerminologyExemptionError(
            f"occurrence {occurrence_index} of {spelling!r} selects no terminology decision"
        )
    candidate_id = matches[0].candidate_id
    decision = next(
        (item for item in registry.decisions if item.candidate_id == candidate_id), None
    )
    if decision is None:
        raise TerminologyExemptionError(f"candidate {candidate_id} carries no decision")
    concept_id: str | None = None
    if policy is not None:
        concepts = _decided_concepts(policy, spelling)
        if len(concepts) != 1:
            raise TerminologyExemptionError(
                f"{spelling!r} is owned by {len(concepts)} decided series concepts, not one"
            )
        concept_id = concepts[0]
    source_sha256 = _sha256(source_text)
    number, block = _paragraph_of(source_text, start, end)
    record = TerminologyOccurrenceExemption(
        exemption_id=exemption_identity(
            chunk_id=chunk_id,
            chunk_source_sha256=source_sha256,
            candidate_id=candidate_id,
            start=start,
            end=end,
        ),
        chunk_id=chunk_id,
        chunk_source_sha256=source_sha256,
        candidate_id=candidate_id,
        decision_id=decision.decision_id,
        decision_sha256=decision_digest(decision),
        concept_id=concept_id,
        spelling=spelling,
        occurrence_index=occurrence_index,
        start=start,
        end=end,
        paragraph_number=number,
        paragraph_sha256=_sha256(block),
        sense=sense,
        rationale=rationale.strip(),
        created_at=created_at,
    )
    _proven_span(record, registry, policy, source_text)
    return PlannedExemption(record, len(spans) - 1)


def with_exemption(
    current: TerminologyOccurrenceExemptionRegistry | None,
    record: TerminologyOccurrenceExemption,
    *,
    book_id: str,
) -> tuple[TerminologyOccurrenceExemptionRegistry, bool]:
    """The registry with *record* added; ``False`` when the same occurrence is already exempt.

    An existing exemption for the occurrence is never replaced: re-adjudicating it is a new
    decision about a record some snapshot may already have frozen.
    """
    existing: Iterable[TerminologyOccurrenceExemption] = current.exemptions if current else ()
    for item in existing:
        if (item.chunk_id, item.start, item.end) == (record.chunk_id, record.start, record.end):
            if item.model_dump(exclude={"created_at", "rationale"}) != record.model_dump(
                exclude={"created_at", "rationale"}
            ):
                raise TerminologyExemptionError(
                    f"occurrence already carries a different exemption {item.exemption_id}"
                )
            assert current is not None
            return current, False
    if current is not None and current.book_id != book_id:
        raise TerminologyExemptionError("the exemption registry belongs to another book")
    exemptions = sorted([*existing, record], key=lambda item: item.exemption_id)
    return (
        TerminologyOccurrenceExemptionRegistry(
            schema_version=EXEMPTION_REGISTRY_SCHEMA_VERSION,
            book_id=book_id,
            exemptions=exemptions,
        ),
        True,
    )
