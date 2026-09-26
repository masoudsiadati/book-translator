"""Pure projection of the series policy onto one book's terminology registry.

The derived registry is the book's *own* occurrence layer: its candidates carry only this book's
chapter and chunk identities, and its decisions restate the policy in the enforcement vocabulary
every downstream consumer already reads. Nothing from the reference corpora is copied into it.
"""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from book_translator.models import (
    SCHEMA_VERSION,
    CandidateStatus,
    DecisionSource,
    DecisionState,
    ReferenceEvidenceQuality,
    ReferenceEvidenceSource,
    SeriesProvenanceKind,
    SeriesTerm,
    SeriesTerminologyPolicy,
    SeriesTermStatus,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyEnforcement,
    TerminologyOccurrence,
    TerminologyRegistry,
)
from book_translator.series_terminology.identity import typographic_variants
from book_translator.series_terminology.promotion import describe_support
from book_translator.terminology.concept_precedence import concept_key, resolve_precedence
from book_translator.terminology.lexical import valid_competing_variants
from book_translator.terminology.registry import candidate_identity, decision_identity


@dataclass(frozen=True, slots=True)
class ChunkText:
    chunk_id: str
    chapter_id: str
    source_text: str
    context_text: str = ""


@dataclass(frozen=True, slots=True)
class DerivationSummary:
    policy_terms: int
    terms_with_occurrences: int
    occurrences: int
    chapters: int
    chunks: int


@lru_cache(maxsize=8192)
def _pattern(term: str) -> re.Pattern[str]:
    # A policy holds more terms than the standard library's regex cache keeps, so without this
    # cache every scan of every chunk would recompile every pattern.
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)")


def count_occurrences(term: str, text: str) -> int:
    """Byte-exact whole-word counting, the same boundary rule the QA validator uses."""
    return len(_pattern(term).findall(text))


def _book_forms(term: SeriesTerm, chunks: Iterable[ChunkText]) -> dict[str, int]:
    """Every typographic form of the term and its aliases that this book actually contains."""
    forms: dict[str, int] = {}
    corpus = list(chunks)
    for base in (term.source_term, *term.aliases):
        for variant in typographic_variants(base):
            total = sum(
                count_occurrences(variant, chunk.source_text)
                + count_occurrences(variant, chunk.context_text)
                for chunk in corpus
            )
            if total:
                forms[variant] = total
    return forms


def _occurrences(forms: Iterable[str], chunks: Iterable[ChunkText]) -> list[TerminologyOccurrence]:
    found: list[TerminologyOccurrence] = []
    for chunk in chunks:
        for location, text in (("source", chunk.source_text), ("context", chunk.context_text)):
            count = sum(count_occurrences(form, text) for form in forms)
            if count:
                found.append(
                    TerminologyOccurrence(
                        chapter_id=chunk.chapter_id,
                        chunk_id=chunk.chunk_id,
                        location=location,  # type: ignore[arg-type]
                        count=count,
                    )
                )
    return found


def _primary_form(term: SeriesTerm, forms: dict[str, int]) -> str:
    """The book's own spelling of the term itself; an alias only stands in when it never occurs.

    Preferring the term's own typographic variants keeps "Amyrlin Seat" from being identified
    by its short alias "Amyrlin" merely because the alias is more frequent, which would collide
    with the separate concept that alias names.
    """
    own = [variant for variant in typographic_variants(term.source_term) if variant in forms]
    pool = own or list(forms)
    return max(pool, key=lambda form: (forms[form], form == term.source_term, form))


def _governing_terms(policy: SeriesTerminologyPolicy) -> list[SeriesTerm]:
    """Collapse each group of article/case variants onto the decision that governs it.

    The displaced spelling becomes an alias of the governing concept, so every occurrence it used
    to match is still matched and is now enforced to the one canonical target. Its corpus evidence
    is carried onto the governing concept's notes rather than dropped: the registry keeps the
    record of what the corpora attested, it simply no longer states it as a rival decision.
    """
    resolution = resolve_precedence(policy.terms)
    if not resolution.superseded:
        return list(policy.terms)
    by_id = {term.concept_id: term for term in policy.terms}
    displaced: dict[str, list[SeriesTerm]] = {}
    for superseded_id, dominant_id in resolution.superseded:
        displaced.setdefault(dominant_id, []).append(by_id[superseded_id])
    governing: list[SeriesTerm] = []
    for term in policy.terms:
        if resolution.is_superseded(term.concept_id):
            continue
        absorbed = displaced.get(term.concept_id)
        if absorbed is None:
            governing.append(term)
            continue
        aliases = {*term.aliases}
        notes = list(term.notes)
        variants = list(term.variants)
        seen = {variant.target_form for variant in variants}
        for item in sorted(absorbed, key=lambda entry: entry.concept_id):
            aliases.update({item.source_term, *item.aliases})
            notes.append(
                f"supersedes {item.status.value} ({item.provenance.kind.value}, "
                f"{item.concept_id}) {item.source_term!r} -> {item.target_term!r}: "
                "the same concept under an article or case variant"
            )
            # The displaced evidence moves onto the governing concept rather than disappearing.
            # It stays evidence: a clean narrative rival of a locked target becomes an enforceable
            # competing variant, which is what it always was -- never an authorized form.
            for variant in item.variants:
                if variant.target_form not in seen:
                    seen.add(variant.target_form)
                    variants.append(variant)
        governing.append(
            term.model_copy(
                update={
                    "aliases": sorted(aliases - {term.source_term}),
                    "notes": notes,
                    "variants": variants,
                }
            )
        )
    return governing


def _assign_forms(
    terms: list[SeriesTerm], corpus: tuple[ChunkText, ...]
) -> dict[str, tuple[str, dict[str, int]]]:
    """Give every occurring term a unique exact source term plus its remaining book forms.

    Two terms can attest the same spelling (a glossary alias that is also another concept's
    headword). The spelling belongs to the term it names; the other term keeps it neither as its
    identity nor as an alias, so the derived registry has one candidate per exact source form.

    One case overrides that, and only one: when a spelling is an article or case variant of a
    *decided* term's own spelling and the term naming it holds no decision, the decision takes it.
    That is the consolidation case -- an evidence-only record of "Dark One" cannot hold the
    spelling hostage and leave every occurrence of the locked "the Dark One" unenforced.

    The narrowness matters. A decision must never absorb an arbitrary alias this way: "Amyrlin" is
    its own concept that "Amyrlin Seat" merely lists as an alias, and taking it would enforce the
    office's rendering on every bare mention of the title.
    """
    by_id = {term.concept_id: term for term in terms}
    forms_by_term = {
        term.concept_id: forms for term in terms if (forms := _book_forms(term, corpus))
    }
    claimed: dict[str, str] = {}
    for concept_id in sorted(forms_by_term):
        term = by_id[concept_id]
        for variant in typographic_variants(term.source_term):
            if variant in forms_by_term[concept_id]:
                claimed.setdefault(variant, concept_id)
    for concept_id in sorted(forms_by_term):
        term = by_id[concept_id]
        if term.status is SeriesTermStatus.OBSERVED_VARIANTS:
            continue
        own = concept_key(term.source_term)
        for alias in term.aliases:
            if concept_key(alias) != own:
                continue
            for variant in typographic_variants(alias):
                holder = claimed.get(variant)
                if variant not in forms_by_term[concept_id]:
                    continue
                if holder is None or by_id[holder].status is SeriesTermStatus.OBSERVED_VARIANTS:
                    claimed[variant] = concept_id
    assignments: dict[str, tuple[str, dict[str, int]]] = {}
    for concept_id in sorted(forms_by_term):
        forms = {
            form: count
            for form, count in forms_by_term[concept_id].items()
            if claimed.get(form, concept_id) == concept_id
        }
        if not forms:
            continue
        primary = _primary_form(by_id[concept_id], forms)
        if claimed.setdefault(primary, concept_id) != concept_id:
            continue
        assignments[concept_id] = (primary, forms)
    return assignments


def _series_note(term: SeriesTerm) -> str:
    return (
        f"series {term.status.value} ({term.provenance.kind.value}, {term.concept_id}): "
        f"{term.provenance.rationale}"
    )


def _corpus_rivals(term: SeriesTerm) -> tuple[str, ...]:
    """Clean narrative forms that rival an explicit decision become enforceable competing variants.

    Printed-glossary headwords are excluded: a dictionary-order headword is not a rendering a
    translation could contain, so enforcing it would only add noise to every finding list. A form
    the decision explicitly authorized is agreement with it, not a rival, and neither is a form
    occurring inside one.
    """
    rivals = [
        variant.target_form
        for variant in term.variants
        if variant.target_form != term.target_term
        and variant.target_form not in term.allowed_target_forms
        and any(
            item.source is ReferenceEvidenceSource.NARRATIVE
            and item.quality is ReferenceEvidenceQuality.CLEAN
            for item in variant.support
        )
    ]
    kept = valid_competing_variants(term.target_term or "", tuple(sorted(set(rivals))))
    for form in term.allowed_target_forms:
        kept = valid_competing_variants(form, kept)
    return kept


# Snapshot validation re-derives the registry to prove the frozen one still matches, and one
# worker process validates many times per job. The derivation is a pure function of the policy,
# the book ID and the chunk texts, so a result is memoized under a digest of exactly those inputs;
# callers receive a deep copy, so a hit is indistinguishable from recomputation.
_DERIVATION_MEMO: dict[str, tuple[TerminologyRegistry, DerivationSummary]] = {}
_DERIVATION_MEMO_LIMIT = 4


def _derivation_key(
    policy: SeriesTerminologyPolicy, book_id: str, corpus: tuple[ChunkText, ...]
) -> str:
    digest = hashlib.sha256()
    for part in (policy.model_dump_json(), book_id):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    for chunk in corpus:
        for part in (chunk.chunk_id, chunk.chapter_id, chunk.source_text, chunk.context_text):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def derive_book_registry(
    policy: SeriesTerminologyPolicy,
    *,
    book_id: str,
    chunks: Iterable[ChunkText],
) -> tuple[TerminologyRegistry, DerivationSummary]:
    corpus = tuple(chunks)
    key = _derivation_key(policy, book_id, corpus)
    cached = _DERIVATION_MEMO.get(key)
    if cached is None:
        cached = _derive_book_registry(policy, book_id=book_id, corpus=corpus)
        if len(_DERIVATION_MEMO) >= _DERIVATION_MEMO_LIMIT:
            _DERIVATION_MEMO.pop(next(iter(_DERIVATION_MEMO)))
        _DERIVATION_MEMO[key] = cached
    registry, summary = cached
    return registry.model_copy(deep=True), summary


def _derive_book_registry(
    policy: SeriesTerminologyPolicy,
    *,
    book_id: str,
    corpus: tuple[ChunkText, ...],
) -> tuple[TerminologyRegistry, DerivationSummary]:
    candidates: list[TerminologyCandidate] = []
    decisions: list[TerminologyDecision] = []
    occurrences_total = 0
    chapters: set[str] = set()
    chunk_ids: set[str] = set()
    governing = _governing_terms(policy)
    assignments = _assign_forms(governing, corpus)
    for term in sorted(governing, key=lambda item: item.concept_id):
        assignment = assignments.get(term.concept_id)
        if assignment is None:
            continue
        primary, forms = assignment
        aliases = sorted(form for form in forms if form != primary)
        occurrences = _occurrences(forms, corpus)
        occurrences_total += sum(item.count for item in occurrences)
        chapters.update(item.chapter_id for item in occurrences)
        chunk_ids.update(item.chunk_id for item in occurrences if item.chunk_id)
        candidate_id = candidate_identity(primary, policy.source_language)
        decided = term.status is not SeriesTermStatus.OBSERVED_VARIANTS
        explicit_user = term.provenance.kind is SeriesProvenanceKind.EXPLICIT_USER
        candidates.append(
            TerminologyCandidate(
                candidate_id=candidate_id,
                source_term=primary,
                classification=term.classification,
                confidence=1.0 if decided else 0.5,
                status=CandidateStatus.APPROVED if decided else CandidateStatus.CANDIDATE,
                provenance=DecisionSource.USER if explicit_user else DecisionSource.IMPORTED,
                aliases=aliases,
                occurrences=occurrences,
                evidence_notes=[
                    _series_note(term),
                    *(describe_support(variant) for variant in term.variants),
                ],
            )
        )
        if not decided:
            continue
        if term.status is SeriesTermStatus.LOCKED:
            decisions.append(
                TerminologyDecision(
                    decision_id=decision_identity(candidate_id, policy.target_language),
                    candidate_id=candidate_id,
                    target_term=term.target_term or "",
                    decision_source=DecisionSource.USER
                    if explicit_user
                    else DecisionSource.IMPORTED,
                    decision_state=DecisionState.CONFIRMED,
                    locked=explicit_user,
                    required=True,
                    enforcement=TerminologyEnforcement.EXACT,
                    competing_variants=list(_corpus_rivals(term)),
                    allowed_target_forms=list(term.allowed_target_forms),
                    contextual_notes=[_series_note(term)],
                )
            )
        else:
            decisions.append(
                TerminologyDecision(
                    decision_id=decision_identity(candidate_id, policy.target_language),
                    candidate_id=candidate_id,
                    target_term=term.target_term or "",
                    decision_source=DecisionSource.IMPORTED,
                    decision_state=DecisionState.CONFIRMED,
                    locked=False,
                    required=True,
                    # Contextual: absence is a review signal, never a blocking finding, and no
                    # rival form is enforced because the evidence did not settle it.
                    enforcement=TerminologyEnforcement.CONTEXTUAL,
                    allowed_target_forms=list(term.allowed_target_forms),
                    contextual_notes=[_series_note(term), term.provenance.evidence_summary],
                )
            )
    registry = TerminologyRegistry(
        schema_version=SCHEMA_VERSION,
        version=policy.version,
        book_id=book_id,
        source_language=policy.source_language,
        target_language=policy.target_language,
        candidates=sorted(candidates, key=lambda item: item.candidate_id),
        decisions=sorted(decisions, key=lambda item: item.decision_id),
    )
    return registry, DerivationSummary(
        policy_terms=len(policy.terms),
        terms_with_occurrences=len(candidates),
        occurrences=occurrences_total,
        chapters=len(chapters),
        chunks=len(chunk_ids),
    )
