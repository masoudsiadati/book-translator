"""Read-only audit of which canonical decisions are safe to enforce as exact target strings.

Exact enforcement is the registry's strongest claim: it asserts that a source form is a name whose
target rendering is a fixed lexeme.  That claim holds for a proper noun and fails for a clause, an
open descriptive phrase, or a source fragment whose canonical spelling contradicts the longer term
it belongs to.  When it fails, QA reports a defect the translator did not commit, and the pipeline
learns to distrust its own terminology gate.

This module answers "which decisions should never have been exact" without changing what QA does.
The separation is the point: a false positive traced to bad registry data is fixed by remediating
the registry and freezing a new resource, not by teaching the matcher to accept looser evidence.
Every check below reads only the frozen registry, so an audit is reproducible against any resource.
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from book_translator.models import (
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyEnforcement,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.terminology.enforcement import resolve_enforcement


class ExactnessRisk(StrEnum):
    """Why one exactly enforced decision is unlikely to survive normal target grammar."""

    CONTAINMENT_INCONSISTENCY = "containment_inconsistency"
    VERB_PHRASE = "verb_phrase"
    DESCRIPTIVE_PHRASE = "descriptive_phrase"
    COORDINATED_SOURCE = "coordinated_source"
    SOURCE_FRAGMENT = "source_fragment"
    COMPOSITE_OF_DECIDED_TERMS = "composite_of_decided_terms"


# Ordered most to least likely to be a registry defect rather than a debatable modelling choice.
_RISK_ORDER = {risk: index for index, risk in enumerate(ExactnessRisk)}

_FUNCTION_WORDS = frozenset(
    {"a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with"}
)
_COORDINATORS = frozenset({"and", "or"})
_DETERMINERS = frozenset({"a", "an", "the", "his", "her", "its", "their"})
# Name particles are lowercase by convention and carry no descriptive content, so they must not make
# a personal name look like a description.
_NAME_PARTICLES = frozenset({"a", "ap", "ben", "bin", "da", "de", "den", "do", "du", "la", "van"})
_FINITE_VERB_FORMS = frozenset(
    {
        "bore",
        "bound",
        "broke",
        "came",
        "died",
        "fell",
        "gave",
        "held",
        "lost",
        "made",
        "rode",
        "rose",
        "slew",
        "stood",
        "struck",
        "swore",
        "took",
        "went",
        "won",
    }
)
NAMED_CLASSIFICATIONS = frozenset(
    {
        TerminologyType.PERSON,
        TerminologyType.PLACE,
        TerminologyType.ORGANIZATION,
        TerminologyType.TITLE,
        TerminologyType.ARTIFACT,
        TerminologyType.INVENTED,
        TerminologyType.HISTORICAL_EVENT,
    }
)
# Ezafe, the ZWNJ, and a plural ``ی`` are grammar the target writes onto an unchanged lexeme, so two
# spellings that differ only by them are the same canonical decision rather than a contradiction.
_TARGET_GRAMMAR = re.compile(r"[ٔ‌]")


@dataclass(frozen=True, slots=True)
class ExactnessFinding:
    """One exactly enforced decision whose source form does not support exact target evidence."""

    risk: ExactnessRisk
    candidate_id: str
    source_term: str
    classification: TerminologyType
    occurrence_count: int
    related_source_terms: tuple[str, ...]
    rationale: str


@dataclass(frozen=True, slots=True)
class ExactnessAuditReport:
    """Deterministic census of exact enforcement across a whole registry."""

    total_decisions: int
    exact_decisions: int
    multiword_exact_decisions: int
    locked_exact_decisions: int
    risk_counts: tuple[tuple[ExactnessRisk, int], ...]
    findings: tuple[ExactnessFinding, ...]

    @property
    def suspicious_decisions(self) -> int:
        return len({finding.candidate_id for finding in self.findings})


def target_tokens(term: str) -> tuple[str, ...]:
    normalized = _TARGET_GRAMMAR.sub("", term.replace("-", " "))
    return tuple(token.removesuffix("ی") or token for token in normalized.split())


def source_tokens(term: str) -> tuple[str, ...]:
    return tuple(term.replace("-", " ").split())


def is_subsequence(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    remaining = iter(longer)
    return all(token in remaining for token in shorter)


def finite_verb(source_term: str) -> str | None:
    """Return the finite verb making *source_term* a clause rather than a name.

    A past participle used attributively ("twisted redstone") is not finite, so a form is only
    accepted as a verb when a determiner follows it or it closes a subject-predicate pair.
    """

    tokens = source_tokens(source_term)
    for index, token in enumerate(tokens):
        if not token[:1].islower():
            continue
        following = tokens[index + 1].lower() if index + 1 < len(tokens) else None
        if token.lower() in _FINITE_VERB_FORMS and (
            following in _DETERMINERS or (following is None and index > 0)
        ):
            return token
        if token.lower().endswith("ed") and following in _DETERMINERS:
            return token
    return None


def descriptive_words(source_term: str) -> tuple[str, ...]:
    """Return the lowercase content words that make *source_term* a description, not a name."""

    return tuple(
        token
        for token in source_tokens(source_term)
        if token[:1].islower()
        and "'" not in token
        and "’" not in token
        and token.lower() not in _FUNCTION_WORDS
        and token.lower() not in _NAME_PARTICLES
    )


def _decided_pairs(
    registry: TerminologyRegistry,
) -> tuple[tuple[TerminologyCandidate, TerminologyDecision], ...]:
    candidates = {item.candidate_id: item for item in registry.candidates}
    return tuple(
        (candidates[decision.candidate_id], decision)
        for decision in sorted(registry.decisions, key=lambda item: item.candidate_id)
        if decision.candidate_id in candidates
    )


def occurrence_count(candidate: TerminologyCandidate) -> int:
    return sum(item.count for item in candidate.occurrences)


def _containment_findings(
    pairs: Iterable[tuple[TerminologyCandidate, TerminologyDecision]],
    exact_ids: frozenset[str],
) -> tuple[ExactnessFinding, ...]:
    """Flag a decision whose canonical target contradicts the longer term it is contained in.

    Containment on the source side is a claim about the same name, so the shorter target must
    survive inside the longer one.  When it does not, one of the two spellings is wrong and no
    translation can satisfy both — which is a registry contradiction, never a translation defect.
    """

    named = [
        (candidate, decision)
        for candidate, decision in pairs
        if candidate.classification in NAMED_CLASSIFICATIONS
    ]
    findings: list[ExactnessFinding] = []
    for candidate, decision in named:
        if candidate.candidate_id not in exact_ids:
            continue
        source = source_tokens(candidate.source_term)
        target = target_tokens(decision.target_term)
        conflicting = tuple(
            sorted(
                other.source_term
                for other, longer in named
                if other.candidate_id != candidate.candidate_id
                and len(source) < len(source_tokens(other.source_term))
                and is_subsequence(source, source_tokens(other.source_term))
                and not is_subsequence(target, target_tokens(longer.target_term))
            )
        )
        if conflicting:
            findings.append(
                ExactnessFinding(
                    risk=ExactnessRisk.CONTAINMENT_INCONSISTENCY,
                    candidate_id=candidate.candidate_id,
                    source_term=candidate.source_term,
                    classification=candidate.classification,
                    occurrence_count=occurrence_count(candidate),
                    related_source_terms=conflicting,
                    rationale=(
                        "canonical target is absent from the longer canonical term this source"
                        " form is contained in; the two spellings cannot both be honored"
                    ),
                )
            )
    return tuple(findings)


def audit_exact_enforcement(registry: TerminologyRegistry) -> ExactnessAuditReport:
    """Return every exactly enforced decision whose source form cannot support exact evidence."""

    pairs = _decided_pairs(registry)
    exact = tuple(
        (candidate, decision)
        for candidate, decision in pairs
        if resolve_enforcement(candidate, decision) is TerminologyEnforcement.EXACT
    )
    exact_ids = frozenset(candidate.candidate_id for candidate, _ in exact)
    decided_sources = {candidate.source_term: candidate for candidate, _ in pairs}

    findings = list(_containment_findings(pairs, exact_ids))
    for candidate, _ in exact:
        tokens = source_tokens(candidate.source_term)
        if len(tokens) < 2:
            continue

        def finding(
            risk: ExactnessRisk,
            rationale: str,
            related: tuple[str, ...] = (),
            subject: TerminologyCandidate = candidate,
        ) -> ExactnessFinding:
            return ExactnessFinding(
                risk=risk,
                candidate_id=subject.candidate_id,
                source_term=subject.source_term,
                classification=subject.classification,
                occurrence_count=occurrence_count(subject),
                related_source_terms=related,
                rationale=rationale,
            )

        verb = finite_verb(candidate.source_term)
        if verb is not None:
            findings.append(
                finding(
                    ExactnessRisk.VERB_PHRASE,
                    f"source form is a clause headed by the finite verb {verb!r}; a target"
                    " language renders an event as a noun phrase, so no fixed lexeme exists",
                )
            )
        elif descriptive := descriptive_words(candidate.source_term):
            findings.append(
                finding(
                    ExactnessRisk.DESCRIPTIVE_PHRASE,
                    "source form is an open description rather than a name; its target"
                    " realization admits ezafe, reordering, and alternative modifiers",
                    tuple(sorted(descriptive)),
                )
            )
        if _COORDINATORS.intersection(token.lower() for token in tokens):
            findings.append(
                finding(
                    ExactnessRisk.COORDINATED_SOURCE,
                    "source form coordinates two heads, which the target may write with a"
                    " single shared modifier rather than as the recorded target string",
                )
            )
        contained_in = tuple(
            sorted(
                other
                for other in decided_sources
                if other != candidate.source_term
                and len(tokens) < len(source_tokens(other))
                and is_subsequence(tokens, source_tokens(other))
            )
        )
        if contained_in:
            findings.append(
                finding(
                    ExactnessRisk.SOURCE_FRAGMENT,
                    "source form is a fragment of a longer canonical term and may be better"
                    " modelled as its alias than as an independent canonical decision",
                    contained_in,
                )
            )
        contains = tuple(
            sorted(
                other
                for other in decided_sources
                if other != candidate.source_term
                and len(source_tokens(other)) < len(tokens)
                and is_subsequence(source_tokens(other), tokens)
            )
        )
        if len(contains) > 1:
            findings.append(
                finding(
                    ExactnessRisk.COMPOSITE_OF_DECIDED_TERMS,
                    "source form composes canonical terms that are already decided"
                    " independently, duplicating their canonical identity",
                    contains,
                )
            )

    ordered = tuple(
        sorted(
            findings,
            key=lambda item: (
                _RISK_ORDER[item.risk],
                -item.occurrence_count,
                item.source_term,
                item.candidate_id,
            ),
        )
    )
    counts = Counter(item.risk for item in ordered)
    return ExactnessAuditReport(
        total_decisions=len(pairs),
        exact_decisions=len(exact),
        multiword_exact_decisions=sum(
            1 for candidate, _ in exact if len(source_tokens(candidate.source_term)) > 1
        ),
        locked_exact_decisions=sum(1 for _, decision in exact if decision.locked),
        risk_counts=tuple((risk, counts[risk]) for risk in ExactnessRisk if counts[risk]),
        findings=ordered,
    )
