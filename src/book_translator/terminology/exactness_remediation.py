"""Pure classification of exact-enforcement audit risks into governed remediation dispositions.

:mod:`book_translator.terminology.exactness_audit` answers "which decisions should never have been
exact".  That is a detection question and it stays read-only.  This module answers the separate
governance question — "what, if anything, may an automatic plan do about it" — and it is the single
owner of that mapping, so neither the remediation audit nor the planner classifies risk itself.

The split matters because the two questions have different error costs.  A false positive in
detection costs a line in a report; a false positive here retires a canonical decision.  So every
rule below fires only on evidence the frozen registry already holds — a finite verb, an elliptical
shorthand the canonical view already recognizes, a target that provably survives inside a longer
canonical target — and everything else becomes a question for a human rather than a mutation.
"""

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    QuestionCategory,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyEnforcement,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.models.terminology_remediation import (
    TerminologyExactnessDisposition,
    TerminologyRemediationAction,
    TerminologyRemediationCategory,
)
from book_translator.terminology.canonical import (
    CanonicalTerm,
    elliptical_shorthand_sources,
    target_identity_present,
)
from book_translator.terminology.enforcement import resolve_enforcement
from book_translator.terminology.exactness_audit import (
    ExactnessAuditReport,
    ExactnessFinding,
    ExactnessRisk,
    descriptive_words,
    is_subsequence,
    occurrence_count,
    source_tokens,
)
from book_translator.terminology.lexical import contains_canonical_lexeme

EXACTNESS_REMEDIATION_VERSION = "1.0.0"

# Ordered exactly as the rules are evaluated; the first match owns the decision.
_TRANSLITERATED_CLASSIFICATIONS = frozenset(
    {
        TerminologyType.PERSON,
        TerminologyType.PLACE,
        TerminologyType.ORGANIZATION,
        TerminologyType.TITLE,
        TerminologyType.PEOPLE,
    }
)


class ExactnessRemediationRule(StrEnum):
    """The named deterministic rule that decided one exact-enforcement finding.

    A rule name is durable provenance: an operation records which rule produced it, so a later
    reader can re-derive the conclusion from the frozen registry instead of trusting the plan.
    """

    OWNER_GOVERNED_DECISION = "owner_governed_decision"
    FINITE_CLAUSE_SOURCE_FORM = "finite_clause_source_form"
    ELLIPTICAL_FRAGMENT_OF_COMPOUND = "elliptical_fragment_of_compound"
    GENERIC_LEXEME_CONTRADICTED_BY_COMPOUNDS = "generic_lexeme_contradicted_by_compounds"
    NESTED_DESCRIPTIVE_PHRASINGS = "nested_descriptive_phrasings"
    DIVERGENT_COMPONENT_SPELLING = "divergent_component_spelling"
    ESTABLISHED_LEXEME_UPHELD = "established_lexeme_upheld"
    DESCRIPTIVE_COMPOUND_OF_CANONICAL_TERM = "descriptive_compound_of_canonical_term"
    CANONICAL_COMPONENT_CONTRADICTED = "canonical_component_contradicted"
    STRUCTURAL_REDUNDANCY_ONLY = "structural_redundancy_only"
    OPEN_DESCRIPTION_REQUIRES_REVIEW = "open_description_requires_review"


@dataclass(frozen=True, slots=True)
class ExactnessJudgement:
    """One decided candidate's exact-enforcement risk resolved into a governed disposition."""

    candidate_id: str
    decision_id: str
    source_term: str
    target_term: str
    classification: TerminologyType
    occurrence_count: int
    primary_risk: ExactnessRisk
    risks: tuple[ExactnessRisk, ...]
    related_source_terms: tuple[str, ...]
    rule: ExactnessRemediationRule
    disposition: TerminologyExactnessDisposition
    category: TerminologyRemediationCategory
    action: TerminologyRemediationAction
    proposed_enforcement: TerminologyEnforcement | None
    corrected_target: str | None
    correction_component_source_term: str | None
    correction_replaced_token: str | None
    question_category: QuestionCategory | None
    question: str | None
    rationale: str
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Decided:
    candidate: TerminologyCandidate
    decision: TerminologyDecision
    tokens: tuple[str, ...]


def _decided(registry: TerminologyRegistry) -> tuple[_Decided, ...]:
    candidates = {item.candidate_id: item for item in registry.candidates}
    return tuple(
        _Decided(
            candidate=candidates[decision.candidate_id],
            decision=decision,
            tokens=source_tokens(candidates[decision.candidate_id].source_term),
        )
        for decision in sorted(registry.decisions, key=lambda item: item.candidate_id)
        if decision.candidate_id in candidates
    )


def _is_name_token(token: str) -> bool:
    """Whether one source token carries proper-name evidence rather than description.

    Capitalization and an internal apostrophe are the only two signals used, because both are
    orthographic facts of the recorded source form rather than an interpretation of it.
    """

    return token[:1].isupper() or "'" in token or "’" in token


def _is_fully_descriptive(source_term: str) -> bool:
    """Whether every content word of *source_term* is an ordinary lowercase description."""

    tokens = source_tokens(source_term)
    return bool(descriptive_words(source_term)) and not any(_is_name_token(item) for item in tokens)


def _components(subject: _Decided, decided: tuple[_Decided, ...]) -> tuple[_Decided, ...]:
    """Return the decided terms whose source form is a proper part of *subject*'s source form."""

    return tuple(
        other
        for other in decided
        if other.candidate.candidate_id != subject.candidate.candidate_id
        and len(other.tokens) < len(subject.tokens)
        and is_subsequence(other.tokens, subject.tokens)
    )


def _named_components(
    subject: _Decided, decided: tuple[_Decided, ...], shorthands: frozenset[str] = frozenset()
) -> tuple[_Decided, ...]:
    """Return the components that are themselves names rather than further descriptions.

    Only a name earns a compound the benefit of the doubt.  A compound wrapping a lowercase
    description wraps nothing canonical, so it cannot be excused as "a description of a decided
    term" — that is the difference between ``doorframe ter'angreal`` and ``redstone doorframe``.

    An elliptical shorthand is excluded for the same reason the canonical validator excludes it
    from its constraints: its target spells out a longer name it elides, so it is a claim about
    that name and not about the ordinary word a compound happens to reuse.
    """

    return tuple(
        item
        for item in _components(subject, decided)
        if any(_is_name_token(token) for token in item.tokens)
        and item.candidate.source_term not in shorthands
    )


@dataclass(frozen=True, slots=True)
class ComponentSpellingCorrection:
    """The one-token substitution that makes a compound spell its component canonically."""

    component: "_Decided"
    replaced_token: str
    corrected_target: str


def _component_spelling_correction(
    subject: "_Decided",
    decided: tuple["_Decided", ...],
    target_language: str,
    shorthands: frozenset[str],
) -> ComponentSpellingCorrection | None:
    """Return the mechanical correction that spells one component canonically, if there is one.

    The correction is only derivable when the compound is *already* right about everything except
    the spelling: exactly one decided component is missing from the target, exactly one token of
    that target is not accounted for by any other component, and the substitution puts the
    component's own canonical target where that token stood.  Any looser condition would be the
    engine composing target text, which it must never do.
    """

    components = _components(subject, decided)
    target = subject.decision.target_term
    absent = [
        item
        for item in _named_components(subject, decided, shorthands)
        if not contains_canonical_lexeme(
            target, item.decision.target_term, target_language=target_language
        )
    ]
    if len(absent) != 1:
        return None
    component = absent[0]
    others = [
        item
        for item in components
        if item.candidate.candidate_id != component.candidate.candidate_id
    ]
    tokens = target.split()
    unexplained = [
        token
        for token in tokens
        if not any(
            contains_canonical_lexeme(
                token, item.decision.target_term, target_language=target_language
            )
            for item in others
        )
    ]
    # Substituting a token for a target that already contains it would nest the divergent spelling
    # rather than replace it, which is never a correction.
    if len(unexplained) != 1 or unexplained[0] in component.decision.target_term.split():
        return None
    corrected = " ".join(
        component.decision.target_term if token == unexplained[0] else token for token in tokens
    )
    if corrected == target or not contains_canonical_lexeme(
        corrected, component.decision.target_term, target_language=target_language
    ):
        return None
    # A correction must never collapse two canonical identities into one shared target.
    if any(
        item.decision.target_term == corrected
        for item in decided
        if item.candidate.candidate_id != subject.candidate.candidate_id
    ):
        return None
    return ComponentSpellingCorrection(component, unexplained[0], corrected)


def _descriptive_family(
    subject: _Decided, decided: tuple[_Decided, ...], target_language: str
) -> tuple[str, ...]:
    """Return the wholly descriptive decided phrases nested with *subject* on both sides.

    A canonical name has exactly one form.  When the registry decides two overlapping multi-word
    descriptions of one thing, and the shorter target survives verbatim inside the longer one, the
    evidence is that a passing description was recorded at two lengths — the longer phrase adds
    modifiers rather than naming something else, and neither phrase is a name.  Both are single
    words away from being a name, which is why a one-word head never counts: a bare noun is the
    head a real name would build on, not a competing phrasing of it.
    """

    if not _is_fully_descriptive(subject.candidate.source_term) or len(subject.tokens) < 2:
        return ()
    family: set[str] = set()
    for other in decided:
        if other.candidate.candidate_id == subject.candidate.candidate_id:
            continue
        if not _is_fully_descriptive(other.candidate.source_term) or len(other.tokens) < 2:
            continue
        shorter, longer = sorted((subject, other), key=lambda item: len(item.tokens))
        if len(shorter.tokens) == len(longer.tokens):
            continue
        if is_subsequence(shorter.tokens, longer.tokens) and target_identity_present(
            shorter.decision.target_term, longer.decision.target_term, target_language
        ):
            family.add(other.candidate.source_term)
    return tuple(sorted(family))


def _question_category(subject: _Decided, contradicted: tuple[_Decided, ...]) -> QuestionCategory:
    involved = (subject, *contradicted)
    if any(
        item.candidate.classification in _TRANSLITERATED_CLASSIFICATIONS
        and any(_is_name_token(token) for token in item.tokens)
        for item in involved
    ):
        return QuestionCategory.TRANSLITERATION
    return QuestionCategory.WORLD_TERM_SEMANTICS


def _uphold(
    rule: ExactnessRemediationRule, rationale: str
) -> tuple[
    ExactnessRemediationRule,
    TerminologyExactnessDisposition,
    TerminologyRemediationCategory,
    TerminologyRemediationAction,
    str,
]:
    return (
        rule,
        TerminologyExactnessDisposition.DEFINITELY_VALID,
        TerminologyRemediationCategory.EXACT_ENFORCEMENT_UPHELD,
        TerminologyRemediationAction.NO_CHANGE_EXACT_UPHELD,
        rationale,
    )


def _retire(
    rule: ExactnessRemediationRule,
    category: TerminologyRemediationCategory,
    rationale: str,
    *,
    reject: bool,
) -> tuple[
    ExactnessRemediationRule,
    TerminologyExactnessDisposition,
    TerminologyRemediationCategory,
    TerminologyRemediationAction,
    str,
]:
    return (
        rule,
        TerminologyExactnessDisposition.DEFINITELY_INVALID,
        category,
        TerminologyRemediationAction.RETIRE_AND_REJECT_CANDIDATE
        if reject
        else TerminologyRemediationAction.RETIRE_DECISION,
        rationale,
    )


def _may_reject(subject: _Decided, decided: tuple[_Decided, ...]) -> bool:
    """Whether withdrawing this decision may also declare the candidate non-terminology.

    A source form another decided candidate lists as an alias stays terminology evidence for that
    candidate, so its own record is withdrawn without being contradicted.
    """

    if subject.candidate.status is CandidateStatus.REJECTED:
        return False
    return not any(
        subject.candidate.source_term in other.candidate.aliases
        for other in decided
        if other.candidate.candidate_id != subject.candidate.candidate_id
    )


def _judge(  # noqa: PLR0911 - one return per named rule keeps the rule order readable
    subject: _Decided,
    risks: frozenset[ExactnessRisk],
    conflicting_containers: tuple[str, ...],
    decided: tuple[_Decided, ...],
    shorthands: frozenset[str],
    target_language: str,
) -> tuple[
    ExactnessRemediationRule,
    TerminologyExactnessDisposition,
    TerminologyRemediationCategory,
    TerminologyRemediationAction,
    str,
]:
    """Return the first rule that resolves *subject*'s exact-enforcement risk."""

    candidate, decision = subject.candidate, subject.decision
    named = _named_components(subject, decided, shorthands)
    # The same containment rule the canonical validator uses for compounds, so the two audit
    # families can never contradict each other about ordinary target morphology.
    preserved = tuple(
        item
        for item in named
        if contains_canonical_lexeme(
            decision.target_term, item.decision.target_term, target_language=target_language
        )
    )
    contradicted = tuple(item for item in named if item not in preserved)
    reject = _may_reject(subject, decided)

    if decision.locked or decision.decision_source is DecisionSource.USER:
        invalid = ExactnessRisk.VERB_PHRASE in risks or candidate.source_term in shorthands
        return (
            ExactnessRemediationRule.OWNER_GOVERNED_DECISION,
            TerminologyExactnessDisposition.UNRESOLVED
            if invalid
            else TerminologyExactnessDisposition.CONTEXT_REVIEW_REQUIRED,
            TerminologyRemediationCategory.OWNER_GOVERNED_DECISION,
            TerminologyRemediationAction.NO_CHANGE_OWNER_AUTHORITY,
            "A human owns this canonical decision, so remediation changes nothing here; only the"
            " explicit decision-correction workflow may revise a locked or user-owned record.",
        )
    if ExactnessRisk.VERB_PHRASE in risks:
        return _retire(
            ExactnessRemediationRule.FINITE_CLAUSE_SOURCE_FORM,
            TerminologyRemediationCategory.NON_TERMINOLOGY_SOURCE_FORM,
            "The source form is a finite clause, so it has no fixed target lexeme to enforce and"
            " never was terminology; the event it describes needs a named term to be canonical.",
            reject=reject,
        )
    if candidate.source_term in shorthands and conflicting_containers:
        return _retire(
            ExactnessRemediationRule.ELLIPTICAL_FRAGMENT_OF_COMPOUND,
            TerminologyRemediationCategory.FRAGMENT_OF_CANONICAL_TERM,
            "The canonical view already recognizes this source form as an elliptical shorthand"
            " whose target spells out a longer canonical name, so enforcing it exactly imposes"
            " that longer name on every unrelated term that reuses the ordinary word.",
            reject=reject,
        )
    if len(conflicting_containers) > 1 and not any(
        _is_name_token(token) for token in subject.tokens
    ):
        return _retire(
            ExactnessRemediationRule.GENERIC_LEXEME_CONTRADICTED_BY_COMPOUNDS,
            TerminologyRemediationCategory.NON_TERMINOLOGY_SOURCE_FORM,
            "Several independent canonical terms contain this lowercase source form and none"
            " renders it the same way, which is how an ordinary word behaves rather than a name.",
            reject=reject,
        )
    if ExactnessRisk.CONTAINMENT_INCONSISTENCY not in risks and _descriptive_family(
        subject, decided, target_language
    ):
        return _retire(
            ExactnessRemediationRule.NESTED_DESCRIPTIVE_PHRASINGS,
            TerminologyRemediationCategory.NON_TERMINOLOGY_SOURCE_FORM,
            "The registry decides two nested wholly descriptive phrasings of one thing whose"
            " targets contain one another, so this is a description recorded at two lengths rather"
            " than a name with a fixed rendering.",
            reject=reject,
        )
    if (
        ExactnessRisk.CONTAINMENT_INCONSISTENCY in risks
        and len(subject.tokens) == 1
        and _is_name_token(subject.tokens[0])
        and occurrence_count(candidate)
        > sum(
            occurrence_count(item.candidate)
            for item in decided
            if item.candidate.source_term in conflicting_containers
        )
    ):
        return _uphold(
            ExactnessRemediationRule.ESTABLISHED_LEXEME_UPHELD,
            "This single established lexeme is attested far more often than every compound that"
            " contradicts it, so the compounds carry the defect and this exact target stands.",
        )
    if contradicted and _component_spelling_correction(
        subject, decided, target_language, shorthands
    ):
        return (
            ExactnessRemediationRule.DIVERGENT_COMPONENT_SPELLING,
            TerminologyExactnessDisposition.DEFINITELY_INVALID,
            TerminologyRemediationCategory.COMPONENT_SPELLING_DIVERGENCE,
            TerminologyRemediationAction.CORRECT_TARGET,
            "This target already renders its canonical component in the right place and order but"
            " spells it a way the registry does not recognize, so the component's own canonical"
            " spelling is substituted for that single token and nothing else changes.",
        )
    if contradicted:
        return (
            ExactnessRemediationRule.CANONICAL_COMPONENT_CONTRADICTED,
            TerminologyExactnessDisposition.CONTEXT_REVIEW_REQUIRED,
            TerminologyRemediationCategory.EXACT_TARGET_REQUIRES_REVIEW,
            TerminologyRemediationAction.OPEN_TERMINOLOGY_QUESTION,
            "This target does not contain the canonical target of a component term it decides"
            " independently, and choosing between the two spellings composes new target text,"
            " which is a translation act no plan may perform.",
        )
    if ExactnessRisk.DESCRIPTIVE_PHRASE in risks and preserved:
        return (
            ExactnessRemediationRule.DESCRIPTIVE_COMPOUND_OF_CANONICAL_TERM,
            TerminologyExactnessDisposition.DEFINITELY_INVALID,
            TerminologyRemediationCategory.OVERBROAD_EXACT_ENFORCEMENT,
            TerminologyRemediationAction.CHANGE_ENFORCEMENT,
            "The compound already honors the canonical component it contains, and that component"
            " is enforced exactly by its own decision; only the claim that the whole open"
            " description has one fixed rendering is wrong, so enforcement is relaxed and no"
            " target text changes.",
        )
    if not risks - {
        ExactnessRisk.SOURCE_FRAGMENT,
        ExactnessRisk.COMPOSITE_OF_DECIDED_TERMS,
        ExactnessRisk.COORDINATED_SOURCE,
    } and any(_is_name_token(token) for token in subject.tokens):
        return _uphold(
            ExactnessRemediationRule.STRUCTURAL_REDUNDANCY_ONLY,
            "The only risks reported are structural relations to other canonical terms, and this"
            " target contradicts none of them, so a named form keeps its exact target.",
        )
    return (
        ExactnessRemediationRule.OPEN_DESCRIPTION_REQUIRES_REVIEW,
        TerminologyExactnessDisposition.CONTEXT_REVIEW_REQUIRED,
        TerminologyRemediationCategory.EXACT_TARGET_REQUIRES_REVIEW,
        TerminologyRemediationAction.OPEN_TERMINOLOGY_QUESTION,
        "The recorded evidence shows an open description rather than a settled name, but nothing"
        " in the registry proves whether the term should be withdrawn, relaxed, or retargeted.",
    )


def _proposed_enforcement(
    subject: _Decided,
    action: TerminologyRemediationAction,
    correction: ComponentSpellingCorrection | None,
    *,
    risks: frozenset[ExactnessRisk],
    containment: tuple[str, ...],
    decided: tuple[_Decided, ...],
    shorthands: frozenset[str],
    target_language: str,
) -> TerminologyEnforcement | None:
    """Return the enforcement mode this decision should end the operation with, if any.

    A correction changes the very evidence the rules read, so the decision is judged again as it
    will exist afterwards.  Without that second look a respelled compound would satisfy its
    canonical component for the first time and only *then* become an obviously over-broad exact
    claim — a defect the plan itself created, left for a later round to find.
    """

    if action is TerminologyRemediationAction.CHANGE_ENFORCEMENT:
        return TerminologyEnforcement.CONTEXTUAL
    if correction is None:
        return None
    corrected = _Decided(
        candidate=subject.candidate,
        decision=subject.decision.model_copy(update={"target_term": correction.corrected_target}),
        tokens=subject.tokens,
    )
    remainder = tuple(
        item for item in decided if item.candidate.candidate_id != subject.candidate.candidate_id
    )
    _, _, _, next_action, _ = _judge(
        corrected, risks, containment, (corrected, *remainder), shorthands, target_language
    )
    return (
        TerminologyEnforcement.CONTEXTUAL
        if next_action is TerminologyRemediationAction.CHANGE_ENFORCEMENT
        else None
    )


def _question(subject: _Decided, risks: tuple[ExactnessRisk, ...], related: tuple[str, ...]) -> str:
    return (
        f"Is {subject.candidate.source_term!r} a canonical name with one fixed target rendering,"
        f" or an open description that must not be enforced exactly? Reported risks:"
        f" {', '.join(item.value for item in risks)}."
        + (f" Related canonical terms: {', '.join(related)}." if related else "")
    )


def judge_exact_enforcement(
    registry: TerminologyRegistry,
    view: tuple[CanonicalTerm, ...],
    report: ExactnessAuditReport,
    *,
    deferred_candidate_ids: frozenset[str] = frozenset(),
) -> tuple[ExactnessJudgement, ...]:
    """Resolve every exact-enforcement finding into exactly one judgement per decided candidate.

    Grouping by candidate is a governance requirement rather than a convenience: a decision can
    carry several risks at once, and a plan that acted on each risk separately could withdraw the
    same decision twice or withdraw and relax it in the same publication.
    """

    grouped: dict[str, list[ExactnessFinding]] = defaultdict(list)
    for finding in report.findings:
        grouped[finding.candidate_id].append(finding)
    decided = _decided(registry)
    by_id = {item.candidate.candidate_id: item for item in decided}
    shorthands = elliptical_shorthand_sources(view)

    judgements: list[ExactnessJudgement] = []
    for candidate_id in sorted(grouped):
        subject = by_id.get(candidate_id)
        if subject is None or candidate_id in deferred_candidate_ids:
            continue
        findings = grouped[candidate_id]
        risks = tuple(sorted({item.risk for item in findings}, key=lambda item: item.value))
        related = tuple(sorted({value for item in findings for value in item.related_source_terms}))
        containment = tuple(
            sorted(
                {
                    value
                    for item in findings
                    if item.risk is ExactnessRisk.CONTAINMENT_INCONSISTENCY
                    for value in item.related_source_terms
                }
            )
        )
        rule, disposition, category, action, rationale = _judge(
            subject,
            frozenset(risks),
            containment,
            decided,
            shorthands,
            registry.target_language,
        )
        asks = action is TerminologyRemediationAction.OPEN_TERMINOLOGY_QUESTION
        correction = (
            _component_spelling_correction(subject, decided, registry.target_language, shorthands)
            if action is TerminologyRemediationAction.CORRECT_TARGET
            else None
        )
        enforcement = _proposed_enforcement(
            subject,
            action,
            correction,
            risks=frozenset(risks),
            containment=containment,
            decided=decided,
            shorthands=shorthands,
            target_language=registry.target_language,
        )
        contradicted = tuple(
            item
            for item in _named_components(subject, decided, shorthands)
            if not contains_canonical_lexeme(
                subject.decision.target_term,
                item.decision.target_term,
                target_language=registry.target_language,
            )
        )
        judgements.append(
            ExactnessJudgement(
                candidate_id=candidate_id,
                decision_id=subject.decision.decision_id,
                source_term=subject.candidate.source_term,
                target_term=subject.decision.target_term,
                classification=subject.candidate.classification,
                occurrence_count=occurrence_count(subject.candidate),
                primary_risk=findings[0].risk,
                risks=risks,
                related_source_terms=related,
                rule=rule,
                disposition=disposition,
                category=category,
                action=action,
                proposed_enforcement=enforcement,
                corrected_target=correction.corrected_target if correction else None,
                correction_component_source_term=(
                    correction.component.candidate.source_term if correction else None
                ),
                correction_replaced_token=correction.replaced_token if correction else None,
                question_category=_question_category(subject, contradicted) if asks else None,
                question=_question(subject, risks, related) if asks else None,
                rationale=rationale,
                evidence=tuple(
                    sorted(
                        {f"risk:{item.value}" for item in risks}
                        | {
                            f"resolved_enforcement:"
                            f"{resolve_enforcement(subject.candidate, subject.decision).value}",
                            f"occurrences:{occurrence_count(subject.candidate)}",
                            f"conflicting_containers:{len(containment)}",
                            f"decision_source:{subject.decision.decision_source.value}",
                        }
                    )
                ),
            )
        )
    return tuple(judgements)
