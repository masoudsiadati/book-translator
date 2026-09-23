"""Deterministic terminology identities, persistence, precedence, and relevance."""

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    DecisionState,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyEnforcement,
    TerminologyOccurrence,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.terminology.concept_precedence import concept_key
from book_translator.terminology.lexical import valid_competing_variants


class TerminologyError(ValueError):
    """Raised when terminology state cannot be changed safely."""


_SOURCE_PRIORITY = {
    DecisionSource.INFERRED: 0,
    DecisionSource.IMPORTED: 1,
    DecisionSource.AI: 2,
    DecisionSource.USER: 3,
}


def decision_priority(source: DecisionSource) -> int:
    return _SOURCE_PRIORITY[source]


#: The one sentence every provider-facing rendering attaches to a decision's authorized forms, so
#: a translator, a reviser, and a judge all read the same rule: the preferred target stays the
#: default, and a listed form is a permitted grammatical shape of it rather than a rival.
AUTHORIZED_FORMS_RULE = (
    "the target is the default rendering; use an allowed target form only where the grammar of "
    "the sentence requires it"
)


def authorized_form_lines(forms: list[str] | tuple[str, ...], *, indent: str) -> list[str]:
    """Render a decision's explicitly authorized target forms, or nothing when it has none.

    Emitting nothing for a decision without forms is what keeps every prompt, request, and
    package rendered from a registry or policy that predates authorized forms byte-identical —
    and therefore every frozen package identity and every cross-run request proof intact.
    Competing variants are never passed here: they are rival renderings, the opposite claim.
    """
    if not forms:
        return []
    return [
        f"{indent}allowed_target_forms: {json.dumps(list(forms), ensure_ascii=False)}",
        f"{indent}form_rule: {json.dumps(AUTHORIZED_FORMS_RULE, ensure_ascii=False)}",
    ]


def authorized_forms_payload(forms: list[str] | tuple[str, ...]) -> dict[str, object]:
    """The JSON-payload counterpart of :func:`authorized_form_lines`: empty when there are none."""
    if not forms:
        return {}
    return {"allowed_target_forms": list(forms), "form_rule": AUTHORIZED_FORMS_RULE}


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        + b"\n"
    )


def candidate_identity(source_term: str, source_language: str) -> str:
    payload = {"source_language": source_language, "source_term": source_term}
    return f"tc-{hashlib.sha256(_canonical_bytes(payload)).hexdigest()[:24]}"


def decision_identity(candidate_id: str, target_language: str) -> str:
    payload = {"candidate_id": candidate_id, "target_language": target_language}
    return f"td-{hashlib.sha256(_canonical_bytes(payload)).hexdigest()[:24]}"


def canonical_registry_bytes(registry: TerminologyRegistry) -> bytes:
    payload = registry.model_dump(mode="json", exclude_none=False)
    payload["candidates"] = sorted(payload["candidates"], key=lambda item: item["candidate_id"])
    payload["decisions"] = sorted(payload["decisions"], key=lambda item: item["decision_id"])
    return _canonical_bytes(payload)


def registry_identity(registry: TerminologyRegistry) -> str:
    return hashlib.sha256(canonical_registry_bytes(registry)).hexdigest()


def load_registry(path: Path) -> TerminologyRegistry:
    try:
        return TerminologyRegistry.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise TerminologyError(f"invalid terminology registry at {path}: {exc}") from exc


def _serialized_registry(registry: TerminologyRegistry) -> str:
    normalized = TerminologyRegistry.model_validate_json(canonical_registry_bytes(registry))
    rendered = yaml.safe_dump(
        normalized.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise TerminologyError("could not serialize terminology registry")
    return rendered


def save_registry(path: Path, registry: TerminologyRegistry) -> None:
    atomic_write_text(path, _serialized_registry(registry), overwrite=path.exists())


def add_candidate(
    registry: TerminologyRegistry,
    *,
    source_term: str,
    classification: TerminologyType,
    confidence: float,
    provenance: DecisionSource,
    aliases: tuple[str, ...] = (),
    context_sensitive: bool = False,
    occurrences: tuple[TerminologyOccurrence, ...] = (),
) -> TerminologyRegistry:
    source_term = source_term.strip()
    if not source_term:
        raise TerminologyError("source term must not be empty")
    candidate_id = candidate_identity(source_term, registry.source_language)
    if any(item.candidate_id == candidate_id for item in registry.candidates):
        raise TerminologyError("terminology candidate already exists")
    candidate = TerminologyCandidate(
        candidate_id=candidate_id,
        source_term=source_term,
        classification=classification,
        confidence=confidence,
        provenance=provenance,
        aliases=sorted(set(aliases)),
        context_sensitive=context_sensitive,
        occurrences=list(occurrences),
    )
    return registry.model_copy(update={"candidates": [*registry.candidates, candidate]})


def set_decision(
    registry: TerminologyRegistry,
    *,
    source_term: str,
    target_term: str,
    classification: TerminologyType,
    decision_source: DecisionSource,
    locked: bool,
    decision_state: DecisionState = DecisionState.CONFIRMED,
    aliases: tuple[str, ...] = (),
    competing_variants: tuple[str, ...] = (),
    context_sensitive: bool = False,
    replace_locked: bool = False,
    enforcement: TerminologyEnforcement | None = None,
    allowed_target_forms: tuple[str, ...] = (),
) -> TerminologyRegistry:
    source_term = source_term.strip()
    target_term = target_term.strip()
    if not source_term or not target_term:
        raise TerminologyError("source and target terms must not be empty")
    forms = sorted({form.strip() for form in allowed_target_forms})
    if "" in forms or target_term in forms:
        raise TerminologyError(
            "an authorized target form must be non-empty and differ from the canonical target"
        )
    if decision_state is DecisionState.PROVISIONAL and locked:
        raise TerminologyError("a provisional decision must not be locked")
    if locked and enforcement not in {None, TerminologyEnforcement.EXACT}:
        raise TerminologyError("a locked user decision must be enforced exactly")
    invalid_variants = sorted(
        set(competing_variants) - set(valid_competing_variants(target_term, competing_variants))
    )
    if invalid_variants:
        raise TerminologyError(
            "competing variants must not occur lexically inside the canonical target"
        )
    if any(
        valid_competing_variants(form, competing_variants) != tuple(competing_variants)
        for form in forms
    ):
        raise TerminologyError(
            "competing variants must not occur lexically inside an authorized target form"
        )
    candidate_id = candidate_identity(source_term, registry.source_language)
    candidates = list(registry.candidates)
    existing_candidate = next(
        (item for item in candidates if item.candidate_id == candidate_id), None
    )
    candidate = TerminologyCandidate(
        candidate_id=candidate_id,
        source_term=source_term,
        classification=classification,
        confidence=(
            1.0
            if decision_source is DecisionSource.USER
            else existing_candidate.confidence
            if existing_candidate
            else 0.8
        ),
        status=CandidateStatus.APPROVED,
        provenance=decision_source,
        aliases=sorted(set(aliases)),
        occurrences=existing_candidate.occurrences if existing_candidate else [],
        context_sensitive=context_sensitive,
        classification_conflicts=(
            existing_candidate.classification_conflicts if existing_candidate else []
        ),
        evidence_notes=existing_candidate.evidence_notes if existing_candidate else [],
    )
    if existing_candidate:
        candidates[candidates.index(existing_candidate)] = candidate
    else:
        candidates.append(candidate)

    decisions = list(registry.decisions)
    existing = next((item for item in decisions if item.candidate_id == candidate_id), None)
    if existing is not None:
        if existing.locked and not replace_locked:
            if (
                existing.target_term == target_term
                and existing.decision_source is decision_source
                and existing.allowed_target_forms == forms
            ):
                return registry
            raise TerminologyError(
                "locked user decision cannot be overwritten; explicitly unlock it"
            )
        if _SOURCE_PRIORITY[decision_source] < _SOURCE_PRIORITY[existing.decision_source]:
            raise TerminologyError(
                "lower-priority decision provenance cannot replace canonical state"
            )
    decision = TerminologyDecision(
        decision_id=decision_identity(candidate_id, registry.target_language),
        candidate_id=candidate_id,
        target_term=target_term,
        decision_source=decision_source,
        decision_state=decision_state,
        locked=locked,
        required=not context_sensitive,
        enforcement=enforcement,
        competing_variants=sorted(set(competing_variants)),
        allowed_target_forms=forms,
        contextual_notes=(
            ["Context-sensitive term; do not enforce as a global exact replacement."]
            if context_sensitive
            else []
        ),
    )
    if existing:
        decisions[decisions.index(existing)] = decision
    else:
        decisions.append(decision)
    return registry.model_copy(update={"candidates": candidates, "decisions": decisions})


def reject_candidate(
    registry: TerminologyRegistry, *, source_term: str, reason: str
) -> TerminologyRegistry:
    """Record that a candidate is deliberately not terminology.

    Without this, rejection is indistinguishable from an untouched backlog entry: both are simply
    "no decision".  Making the state explicit is what lets escalation accounting derive closure
    from the registry instead of from a hand-maintained table.
    """

    if not reason.strip():
        raise TerminologyError("a rejection must record why the candidate is not terminology")
    candidate_id = candidate_identity(source_term, registry.source_language)
    candidates = list(registry.candidates)
    existing = next((item for item in candidates if item.candidate_id == candidate_id), None)
    if existing is None:
        raise TerminologyError(f"no terminology candidate exists for {source_term!r}")
    if any(item.candidate_id == candidate_id for item in registry.decisions):
        raise TerminologyError(
            "a candidate with a canonical decision cannot be rejected; remove the decision first"
        )
    notes = list(existing.evidence_notes)
    if reason not in notes:
        notes.append(reason)
    candidates[candidates.index(existing)] = existing.model_copy(
        update={"status": CandidateStatus.REJECTED, "evidence_notes": notes}
    )
    return registry.model_copy(update={"candidates": candidates})


def correct_decision(
    registry: TerminologyRegistry,
    *,
    source_term: str,
    target_term: str,
    decision_source: DecisionSource,
    decision_state: DecisionState,
    locked: bool,
    reason: str,
) -> tuple[TerminologyRegistry, dict[str, Any]]:
    """Rewrite one decision's ownership and state, returning the superseded facts.

    ``set_decision`` refuses to lower provenance or overwrite a lock, which is correct for ordinary
    edits: it stops an AI pass from quietly demoting a human choice.  A correction is the opposite
    situation — the stored provenance is itself wrong — so it needs an explicit, separately named
    operation that hands back the previous record for durable evidence rather than discarding it.
    """

    if not reason.strip():
        raise TerminologyError("a correction must record why the stored record was wrong")
    if decision_state is DecisionState.PROVISIONAL and locked:
        raise TerminologyError("a provisional decision must not be locked")
    candidate_id = candidate_identity(source_term, registry.source_language)
    decisions = list(registry.decisions)
    existing = next((item for item in decisions if item.candidate_id == candidate_id), None)
    if existing is None:
        raise TerminologyError(f"no canonical decision exists for {source_term!r}")
    previous = {
        "decision_id": existing.decision_id,
        "candidate_id": existing.candidate_id,
        "source_term": source_term,
        "target_term": existing.target_term,
        "decision_source": existing.decision_source.value,
        "decision_state": existing.decision_state.value,
        "locked": existing.locked,
    }
    corrected = existing.model_copy(
        update={
            "target_term": target_term.strip(),
            "decision_source": decision_source,
            "decision_state": decision_state,
            "locked": locked,
        }
    )
    if corrected == existing:
        raise TerminologyError("a correction must change at least one recorded decision fact")
    decisions[decisions.index(existing)] = corrected
    return registry.model_copy(update={"decisions": decisions}), previous


@lru_cache(maxsize=8192)
def _boundary_pattern(term: str) -> re.Pattern[str]:
    # A registry holds more terms than the standard library's regex cache keeps, so without this
    # every selection over every chunk would recompile every pattern.
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)")


def _occurs(term: str, text: str) -> bool:
    return _boundary_pattern(term).search(text) is not None


def boundary_spans(term: str, text: str) -> tuple[tuple[int, int], ...]:
    """Every boundary-safe, byte-exact occurrence of *term*, in source order."""
    return tuple((match.start(), match.end()) for match in _boundary_pattern(term).finditer(text))


@dataclass(frozen=True)
class SourceTermMatch:
    """One authoritative boundary-safe source occurrence."""

    candidate_id: str
    term: str
    start: int
    end: int
    is_alias: bool


@dataclass(frozen=True, slots=True)
class ExemptOccurrences:
    """User-adjudicated source occurrences withheld from terminology selection for one text.

    Built only by :func:`book_translator.terminology.exemptions.resolve_occurrence_exemptions`,
    which proves every span against the exact chunk bytes. The spans are character offsets into
    that one text, so they are refused against any other text rather than applied to it.
    """

    source_sha256: str
    #: ``(candidate_id, start, end)`` for each exempt authoritative registry match.
    spans: frozenset[tuple[str, int, int]]
    exemption_ids: tuple[str, ...]
    #: ``(concept_id, start, end)`` for the series policy tier of the same occurrences.
    concept_spans: frozenset[tuple[str, int, int]] = frozenset()

    def require_text(self, text: str) -> None:
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != self.source_sha256:
            raise TerminologyError(
                "occurrence exemptions were resolved for a different source text"
            )


def _without_exempt(
    matches: tuple[SourceTermMatch, ...], text: str, exemptions: ExemptOccurrences | None
) -> tuple[SourceTermMatch, ...]:
    """Drop exactly the exempt spans, failing closed if one no longer names a match."""
    if exemptions is None:
        return matches
    exemptions.require_text(text)
    present = {(match.candidate_id, match.start, match.end) for match in matches}
    if exemptions.spans - present:
        raise TerminologyError("an occurrence exemption no longer names an authoritative match")
    return tuple(
        match
        for match in matches
        if (match.candidate_id, match.start, match.end) not in exemptions.spans
    )


def source_term_matches(
    registry: TerminologyRegistry, text: str, exemptions: ExemptOccurrences | None = None
) -> tuple[SourceTermMatch, ...]:
    """Return non-overlapping matches, preferring the longest and canonical source term.

    Overlap is resolved before *exemptions* are applied, so an exempt span is removed outright:
    it never falls through to a shorter or alias match that lost the span to it.
    """
    matches: list[SourceTermMatch] = []
    approved = [
        candidate
        for candidate in registry.candidates
        if candidate.status is CandidateStatus.APPROVED and not candidate.context_sensitive
    ]
    for candidate in approved:
        for term, is_alias in (
            (candidate.source_term, False),
            *((alias, True) for alias in candidate.aliases),
        ):
            pattern = _boundary_pattern(term)
            matches.extend(
                SourceTermMatch(
                    candidate_id=candidate.candidate_id,
                    term=term,
                    start=match.start(),
                    end=match.end(),
                    is_alias=is_alias,
                )
                for match in pattern.finditer(text)
            )

    authoritative: list[SourceTermMatch] = []
    for match in sorted(
        matches,
        key=lambda item: (
            -(item.end - item.start),
            item.is_alias,
            item.start,
            item.candidate_id,
            item.term,
        ),
    ):
        if any(match.start < kept.end and kept.start < match.end for kept in authoritative):
            continue
        authoritative.append(match)
    ordered = tuple(
        sorted(authoritative, key=lambda item: (item.start, item.end, item.candidate_id))
    )
    return _without_exempt(ordered, text, exemptions)


def applicable_decisions(
    registry: TerminologyRegistry,
    source_text: str,
    context_text: str = "",
    exemptions: ExemptOccurrences | None = None,
) -> tuple[tuple[TerminologyCandidate, TerminologyDecision], ...]:
    """The decisions that govern this text, resolved the same way enforcement resolves them.

    This is the one selector every prompt path uses. It shares ``source_term_matches`` with the QA
    validator, so where two governed spellings overlap the longest span wins in both places, and a
    shorter term that also occurs on its own span is still selected, because there it governs.
    Every decision the validator enforces is selected here (see
    :func:`canonically_matched_decisions`); the converse holds for every alias that is an
    inflection of the source term, and deliberately not for an alias that is a different source
    expression, which is prompted as guidance but never enforced.

    The context window is matched separately from the source. A term is in scope when it occurs in
    either, and overlap is resolved within each text rather than across them. Occurrence
    *exemptions* are proven against the source text and apply to it alone.
    """
    matched = {
        match.candidate_id
        for text, exempt in ((source_text, exemptions), (context_text, None))
        if text
        for match in source_term_matches(registry, text, exempt)
    }
    decisions = {item.candidate_id: item for item in registry.decisions}
    return tuple(
        (candidate, decisions[candidate.candidate_id])
        for candidate in sorted(registry.candidates, key=lambda item: item.candidate_id)
        if candidate.candidate_id in matched and candidate.candidate_id in decisions
    )


def relevant_decisions(
    registry: TerminologyRegistry, source_text: str, context_text: str = ""
) -> tuple[tuple[TerminologyCandidate, TerminologyDecision], ...]:
    decisions = {item.candidate_id: item for item in registry.decisions}
    relevant = []
    for candidate in registry.candidates:
        decision = decisions.get(candidate.candidate_id)
        if decision is None or candidate.status is not CandidateStatus.APPROVED:
            continue
        if candidate.context_sensitive:
            continue
        if any(
            _occurs(term, source_text) or _occurs(term, context_text)
            for term in [candidate.source_term, *candidate.aliases]
        ):
            relevant.append((candidate, decision))
    return tuple(sorted(relevant, key=lambda item: item[0].candidate_id))


# English inflection of a source term: the possessive, then the plural. The irregular plural of a
# final ``man`` is the only irregular form the registries use. Nothing verbal is folded: a
# participle and a gerund are separate decisions with separate targets.
_ENGLISH_POSSESSIVE = re.compile(r"(?:['’]s|(?<=s)['’])$")
_ENGLISH_PLURALS = (("men", "man"), ("ies", "y"), ("ches", "ch"), ("shes", "sh"), ("sses", "ss"))


def inflectional_key(term: str) -> str:
    """Fold a source term to the key it shares with its own English inflections.

    This is :func:`concept_key` -- case and one leading article -- plus the possessive and the
    plural of the final word, so ``Warders``, ``the Warder's`` and ``Warder`` share a key while
    ``Gaidin``, ``Red sisters`` or ``her old nurse`` never share one with the term they alias.
    """
    key = _ENGLISH_POSSESSIVE.sub("", concept_key(term))
    for plural, singular in _ENGLISH_PLURALS:
        if key.endswith(plural) and len(key) > len(plural):
            return key[: -len(plural)] + singular
    if key.endswith("s") and not key.endswith("ss") and len(key) > 2:
        return key[:-1]
    return key


def governing_source_matches(
    registry: TerminologyRegistry, text: str, exemptions: ExemptOccurrences | None = None
) -> tuple[SourceTermMatch, ...]:
    """The authoritative source matches that bind a decision to its canonical target.

    A decision answers for its own source term and for every inflection of it: an article, case,
    possessive, or plural spelling is an occurrence of the concept itself, and the book writes
    "Warders" and "the One Power" where the concepts are "Warder" and "The One Power". An alias
    that is a different source expression governs nothing here: "Amyrlin" is its own concept that
    "Amyrlin Seat" happens to name, and "her old nurse" is rendered as the description it is, not
    as the name it refers to. Overlap has already been resolved by ``source_term_matches``.
    """
    canonical = {
        item.candidate_id: inflectional_key(item.source_term) for item in registry.candidates
    }
    return tuple(
        match
        for match in source_term_matches(registry, text, exemptions)
        if not match.is_alias or inflectional_key(match.term) == canonical.get(match.candidate_id)
    )


def canonically_matched_decisions(
    registry: TerminologyRegistry, source_text: str, exemptions: ExemptOccurrences | None = None
) -> tuple[tuple[TerminologyCandidate, TerminologyDecision], ...]:
    """Select the decisions with a governing source occurrence (see ``governing_source_matches``).

    This is the selector every QA path enforces. It is a subset of :func:`applicable_decisions`
    over the same text: the prompt may also name a decision reached only through a
    different-expression alias, as guidance, but the validator never holds a translation to it.
    """
    matched_ids = {
        match.candidate_id for match in governing_source_matches(registry, source_text, exemptions)
    }
    decisions = {item.candidate_id: item for item in registry.decisions}
    return tuple(
        (candidate, decisions[candidate.candidate_id])
        for candidate in sorted(registry.candidates, key=lambda item: item.candidate_id)
        if candidate.candidate_id in matched_ids and candidate.candidate_id in decisions
    )


def legacy_glossary_decisions(raw: Any) -> tuple[tuple[str, str, bool], ...]:
    """Adapt the version-1 glossary without changing its persisted contract."""
    if not isinstance(raw, dict):
        raise TerminologyError("legacy glossary must be a mapping")
    expected = {"terms", "characters", "places", "groups", "unresolved_terms"}
    if set(raw) != expected:
        raise TerminologyError("legacy glossary categories do not match the compatibility contract")
    adapted: list[tuple[str, str, bool]] = []
    for category in ("terms", "characters", "places", "groups"):
        values = raw[category]
        if not isinstance(values, dict):
            raise TerminologyError(f"legacy glossary {category} must be a mapping")
        adapted.extend((str(source), str(target), False) for source, target in values.items())
    return tuple(sorted(adapted))
