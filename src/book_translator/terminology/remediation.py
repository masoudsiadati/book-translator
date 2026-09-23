"""Read-only terminology registry remediation audit."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from book_translator.models import (
    CandidateStatus,
    DecisionState,
    OpenQuestion,
    OpenQuestionRegistry,
    QuestionCategory,
    QuestionStatus,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyEnforcement,
    TerminologyProposal,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.models.base import ContractModel
from book_translator.models.terminology_remediation import (
    TerminologyExactnessDisposition,
    TerminologyFindingFamily,
    TerminologyRemediationAction,
    TerminologyRemediationCategory,
)
from book_translator.terminology.canonical import (
    CanonicalTerm,
    canonical_constraints,
    canonical_view,
    exact_canonical,
    proposal_conflicts,
)
from book_translator.terminology.exactness_audit import audit_exact_enforcement
from book_translator.terminology.exactness_remediation import (
    ExactnessJudgement,
    judge_exact_enforcement,
)
from book_translator.terminology.lexical import contains_canonical_lexeme
from book_translator.terminology.registry import (
    TerminologyError,
    candidate_identity,
    load_registry,
    registry_identity,
)
from book_translator.terminology.workflow import (
    load_discovery_output,
    load_proposal_output,
    load_review_output,
)

# The remediation action and category vocabularies are persisted contracts owned by
# ``book_translator.models``; they are re-exported here so the audit keeps one import surface.
__all__ = [
    "TerminologyEscalationDisposition",
    "TerminologyEscalationFinding",
    "TerminologyIdentityMismatch",
    "TerminologyRemediationAction",
    "TerminologyRemediationCategory",
    "TerminologyRemediationCoverage",
    "TerminologyRemediationFinding",
    "TerminologyRemediationReport",
    "audit_registry_remediation",
    "inspect_remediation_finding",
    "load_escalations",
    "serialize_remediation_report",
]


class TerminologyRemediationFinding(ContractModel):
    finding_id: str = Field(pattern=r"^rmf-[0-9a-f]{24}$")
    finding_family: TerminologyFindingFamily = TerminologyFindingFamily.CANONICAL_VALIDATOR
    decision_id: str
    candidate_id: str
    source_term: str
    target_term: str
    finding_category: str
    validator_categories: list[str] = Field(default_factory=list)
    exactness_risks: list[str] = Field(default_factory=list)
    exactness_disposition: TerminologyExactnessDisposition | None = None
    exactness_rule: str | None = None
    occurrence_count: int = Field(default=0, ge=0)
    proposed_enforcement: TerminologyEnforcement | None = None
    corrected_target: str | None = None
    correction_component_source_term: str | None = None
    correction_replaced_token: str | None = None
    question_category: QuestionCategory | None = None
    question: str | None = None
    related_decision_ids: list[str] = Field(default_factory=list)
    related_source_terms: list[str] = Field(default_factory=list)
    related_target_terms: list[str] = Field(default_factory=list)
    triggered_rule: str
    morphology_involved: bool
    evidence: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    severity: Literal["warning", "error", "info"]
    remediation_category: TerminologyRemediationCategory
    proposed_action: TerminologyRemediationAction
    blocking: bool


class TerminologyIdentityMismatch(ContractModel):
    finding_id: str = Field(pattern=r"^rmf-[0-9a-f]{24}$")
    decision_id: str | None = None
    proposal_id: str | None = None
    original_candidate_id: str
    original_source_term: str
    normalized_candidate_id: str
    normalized_source_term: str
    both_candidate_records_exist: bool
    immutable_references_resolve: bool
    safest_migration_strategy: str
    blocking: bool = True
    evidence: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class TerminologyEscalationDisposition(StrEnum):
    CANONICAL = "canonical"
    REJECT_NOT_TERMINOLOGY = "reject-not-terminology"
    ALIAS = "alias"
    SOURCE_ANOMALY = "source-anomaly"
    NEEDS_USER_CHOICE = "needs-user-choice"


class TerminologyEscalationFinding(ContractModel):
    escalation_id: str = Field(pattern=r"^rme-[0-9a-f]{24}$")
    proposal_id: str
    candidate_id: str
    source_term: str
    terminology_type: str
    source_form: str
    aliases: list[str] = Field(default_factory=list)
    chapter_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    occurrence_count: int = Field(ge=0)
    representative_contexts: list[str] = Field(default_factory=list)
    why_escalated: str
    competing_interpretations: list[str] = Field(default_factory=list)
    related_existing_canonical_terminology: list[str] = Field(default_factory=list)
    capitalization_meaningful: bool
    term_kind: Literal[
        "proper noun",
        "title",
        "fictional/world-specific term",
        "ordinary vocabulary",
        "source anomaly/noise",
    ]
    recommended_persian: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    disposition: TerminologyEscalationDisposition
    reason: str
    unresolved: bool = False
    resolution_basis: Literal[
        "confirmed_decision",
        "provisional_decision",
        "explicit_rejection",
        "open_question",
        "undecided",
    ] = "undecided"


class TerminologyRemediationCoverage(ContractModel):
    proposal_ids_unique: bool
    disposition_ids_unique: bool
    affected_decision_ids_unique: bool
    proposal_set_equals_disposition_set: bool
    missing_decision_ids: list[str] = Field(default_factory=list)
    unexpected_decision_ids: list[str] = Field(default_factory=list)


class TerminologyRemediationReport(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    audit_id: str = Field(pattern=r"^remediation-audit-[0-9a-f]{24}$")
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decisions_scanned: int = Field(ge=0)
    findings_total: int = Field(ge=0)
    affected_decisions: int = Field(ge=0)
    registry_finding_count: int = Field(ge=0)
    exactness_finding_count: int = Field(default=0, ge=0)
    exactness_deferred_count: int = Field(default=0, ge=0)
    exactness_deferred_source_terms: list[str] = Field(default_factory=list)
    exactness_disposition_counts: dict[str, int] = Field(default_factory=dict)
    exactness_rule_counts: dict[str, int] = Field(default_factory=dict)
    owner_governed_count: int = Field(default=0, ge=0)
    bookkeeping_mismatch_count: int = Field(ge=0)
    escalation_count: int = Field(ge=0)
    unresolved_escalation_count: int = Field(default=0, ge=0)
    blocking_escalation_count: int = Field(default=0, ge=0)
    validator_category_counts: dict[str, int] = Field(default_factory=dict)
    remediation_category_counts: dict[str, int] = Field(default_factory=dict)
    definitely_valid_count: int = Field(ge=0)
    definitely_invalid_count: int = Field(ge=0)
    context_review_count: int = Field(ge=0)
    identity_migration_count: int = Field(ge=0)
    validator_false_positive_count: int = Field(ge=0)
    remaining_registry_blockers: int = Field(ge=0)
    findings: list[TerminologyRemediationFinding] = Field(default_factory=list)
    bookkeeping_mismatch: TerminologyIdentityMismatch | None = None
    escalations: list[TerminologyEscalationFinding] = Field(default_factory=list)
    coverage: TerminologyRemediationCoverage


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        + b"\n"
    )


def _stable_id(prefix: str, payload: Any) -> str:
    return f"{prefix}-{hashlib.sha256(_canonical_bytes(payload)).hexdigest()[:24]}"


def _normalize_glossary(raw: Any) -> Any:
    if not isinstance(raw, dict):
        raise TerminologyError("legacy glossary must be a mapping")
    expected = {"terms", "characters", "places", "groups", "unresolved_terms"}
    if not raw:
        return {
            "terms": {},
            "characters": {},
            "places": {},
            "groups": {},
            "unresolved_terms": [],
        }
    if set(raw) != expected:
        raise TerminologyError("legacy glossary categories do not match the compatibility contract")
    return raw


def _decision_index(registry: TerminologyRegistry) -> dict[str, TerminologyDecision]:
    return {item.candidate_id: item for item in registry.decisions}


def _source_index(registry: TerminologyRegistry) -> dict[str, str]:
    index: dict[str, str] = {}
    for decision in registry.decisions:
        candidate = next(
            item for item in registry.candidates if item.candidate_id == decision.candidate_id
        )
        index[candidate.source_term] = decision.decision_id
        for alias in candidate.aliases:
            index.setdefault(alias, decision.decision_id)
    return index


def _all_candidates_by_id(registry: TerminologyRegistry) -> dict[str, TerminologyCandidate]:
    return {item.candidate_id: item for item in registry.candidates}


def _morphology_involved(
    candidate: TerminologyCandidate, decision: TerminologyDecision, conflicts: tuple[str, ...]
) -> bool:
    if "canonical_subterm_conflict" in conflicts:
        return True
    markers = (" ", "-", "'", "\u200c")
    return any(marker in candidate.source_term for marker in markers) or any(
        marker in decision.target_term for marker in markers
    )


def _evidence_summary(
    candidate: TerminologyCandidate,
    decision: TerminologyDecision,
    conflicts: tuple[str, ...],
    exact_source: str | None,
    exact_target: str | None,
) -> dict[str, str | int | float | bool | None]:
    chapter_ids = {item.chapter_id for item in candidate.occurrences}
    chunk_ids = {item.chunk_id for item in candidate.occurrences if item.chunk_id is not None}
    return {
        "candidate_provenance": candidate.provenance.value,
        "candidate_status": candidate.status.value,
        "candidate_confidence": candidate.confidence,
        "candidate_chapter_count": len(chapter_ids),
        "candidate_chunk_count": len(chunk_ids),
        "candidate_occurrence_count": sum(item.count for item in candidate.occurrences),
        "exact_source_term": exact_source,
        "exact_target_term": exact_target,
        "registry_locked": decision.locked,
        "registry_required": decision.required,
        "validator_conflicts": ",".join(conflicts),
    }


def _primary_validator_category(conflicts: tuple[str, ...]) -> str:
    order = {
        "exact_canonical_conflict": 0,
        "known_competing_variant": 1,
        "canonical_subterm_conflict": 2,
    }
    return sorted(conflicts, key=lambda value: (order.get(value, 99), value))[0]


def _remediation_category(
    conflicts: tuple[str, ...], preserves_any_component: bool
) -> TerminologyRemediationCategory:
    if "exact_canonical_conflict" in conflicts:
        return TerminologyRemediationCategory.ALIAS_MISCLASSIFICATION
    if "known_competing_variant" in conflicts:
        return TerminologyRemediationCategory.INVALID_COMPETING_VARIANT
    if "canonical_subterm_conflict" in conflicts:
        return (
            TerminologyRemediationCategory.VALID_COMPOUND_OR_MORPHOLOGY
            if preserves_any_component
            else TerminologyRemediationCategory.CONFLICTING_TARGET
        )
    return TerminologyRemediationCategory.REQUIRES_CONTEXT_REVIEW


def _proposed_action(category: TerminologyRemediationCategory) -> TerminologyRemediationAction:
    return {
        TerminologyRemediationCategory.VALID_COMPOUND_OR_MORPHOLOGY: (
            TerminologyRemediationAction.NO_CHANGE_VALIDATOR_FIX
        ),
        TerminologyRemediationCategory.VALID_DISTINCT_TERM: (
            TerminologyRemediationAction.NO_CHANGE_VALID_DISTINCT
        ),
        TerminologyRemediationCategory.DUPLICATE_CANONICAL: (
            TerminologyRemediationAction.MERGE_DECISIONS
        ),
        TerminologyRemediationCategory.INVALID_COMPETING_VARIANT: (
            TerminologyRemediationAction.REMOVE_INVALID_VARIANT
        ),
        TerminologyRemediationCategory.CONFLICTING_TARGET: (
            TerminologyRemediationAction.CHANGE_TARGET_REQUIRES_REVIEW
        ),
        TerminologyRemediationCategory.ALIAS_MISCLASSIFICATION: (
            TerminologyRemediationAction.CONVERT_TO_ALIAS
        ),
        TerminologyRemediationCategory.NORMALIZATION_IDENTITY_BUG: (
            TerminologyRemediationAction.IDENTITY_MIGRATION
        ),
        TerminologyRemediationCategory.REQUIRES_CONTEXT_REVIEW: (
            TerminologyRemediationAction.MANUAL_CONTEXT_REVIEW
        ),
    }[category]


def _related_terms(
    view: tuple[Any, ...],
    candidate: TerminologyCandidate,
    exact: Any,
    conflicts: tuple[str, ...],
    source_index: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    source_terms: list[str] = []
    target_terms: list[str] = []
    decision_ids: list[str] = []
    if exact is not None:
        source_terms.append(exact.source_term)
        target_terms.append(exact.target_term)
        if exact.source_term in source_index:
            decision_ids.append(source_index[exact.source_term])
    for constraint in canonical_constraints(candidate, view):
        if constraint.source_term in source_index:
            decision_ids.append(source_index[constraint.source_term])
            source_terms.append(constraint.source_term)
            target_terms.append(constraint.target_term)
    unique_ids = sorted(set(decision_ids))
    unique_sources = sorted(set(source_terms))
    unique_targets = sorted(set(target_terms))
    return unique_ids, unique_sources, unique_targets


def _findings_for_registry(
    registry: TerminologyRegistry, view: tuple[CanonicalTerm, ...]
) -> tuple[list[TerminologyRemediationFinding], TerminologyIdentityMismatch | None]:
    candidates_by_id = _all_candidates_by_id(registry)
    decisions_by_candidate = _decision_index(registry)
    source_index = _source_index(registry)
    findings: list[TerminologyRemediationFinding] = []
    for decision in sorted(registry.decisions, key=lambda item: item.decision_id):
        candidate = candidates_by_id[decision.candidate_id]
        proposal = TerminologyProposal(
            proposal_id="tp-" + "0" * 24,
            candidate_id=candidate.candidate_id,
            source_term=candidate.source_term,
            proposed_target=decision.target_term,
            confidence=candidate.confidence,
            classification=candidate.classification,
            known_variants=decision.competing_variants,
        )
        conflicts = proposal_conflicts(proposal, candidate, view, registry.target_language)
        if not conflicts:
            continue
        exact = exact_canonical(candidate, view)
        related_decision_ids, related_source_terms, related_target_terms = _related_terms(
            view, candidate, exact, conflicts, source_index
        )
        component_targets = [
            term for term in related_target_terms if exact is None or term != exact.target_term
        ]
        preserves_any_component = any(
            contains_canonical_lexeme(
                decision.target_term, related_target, target_language=registry.target_language
            )
            for related_target in component_targets
        )
        category = _remediation_category(conflicts, preserves_any_component)
        finding = TerminologyRemediationFinding(
            finding_id=_stable_id(
                "rmf",
                {
                    "registry_identity": registry_identity(registry),
                    "decision_id": decision.decision_id,
                    "candidate_id": candidate.candidate_id,
                    "validator_categories": list(conflicts),
                    "remediation_category": category.value,
                    "target_term": decision.target_term,
                },
            ),
            decision_id=decision.decision_id,
            candidate_id=candidate.candidate_id,
            source_term=candidate.source_term,
            target_term=decision.target_term,
            finding_category=_primary_validator_category(conflicts),
            validator_categories=list(conflicts),
            related_decision_ids=related_decision_ids,
            related_source_terms=related_source_terms,
            related_target_terms=related_target_terms,
            triggered_rule=(
                "canonical component is preserved only by the validator's exact lexical rule"
                if category is TerminologyRemediationCategory.VALID_COMPOUND_OR_MORPHOLOGY
                else "current target preserves no authoritative canonical component"
                if category is TerminologyRemediationCategory.CONFLICTING_TARGET
                else "exact canonical source is represented as an alias or normalized variant"
                if category is TerminologyRemediationCategory.ALIAS_MISCLASSIFICATION
                else "current target reuses a stored competing variant"
                if category is TerminologyRemediationCategory.INVALID_COMPETING_VARIANT
                else "static evidence is insufficient"
            ),
            morphology_involved=_morphology_involved(candidate, decision, conflicts),
            evidence=_evidence_summary(
                candidate,
                decision,
                conflicts,
                exact.source_term if exact is not None else None,
                exact.target_term if exact is not None else None,
            ),
            severity=(
                "warning"
                if category is TerminologyRemediationCategory.VALID_COMPOUND_OR_MORPHOLOGY
                else "error"
            ),
            remediation_category=category,
            proposed_action=_proposed_action(category),
            blocking=category
            not in {
                TerminologyRemediationCategory.VALID_COMPOUND_OR_MORPHOLOGY,
                TerminologyRemediationCategory.VALID_DISTINCT_TERM,
            },
        )
        findings.append(finding)

    mismatch: TerminologyIdentityMismatch | None = None
    for candidate in sorted(registry.candidates, key=lambda item: item.candidate_id):
        stripped = candidate.source_term.strip()
        if not stripped or stripped == candidate.source_term:
            continue
        normalized_id = candidate_identity(stripped, registry.source_language)
        if normalized_id == candidate.candidate_id:
            continue
        normalized_candidate = candidates_by_id.get(normalized_id)
        if normalized_candidate is None:
            continue
        matched_decision = decisions_by_candidate.get(normalized_candidate.candidate_id)
        proposal_ids: list[str] = []
        if matched_decision is not None:
            proposal_ids.append(matched_decision.decision_id)
        mismatch = TerminologyIdentityMismatch(
            finding_id=_stable_id(
                "rmf",
                {
                    "registry_identity": registry_identity(registry),
                    "original_candidate_id": candidate.candidate_id,
                    "normalized_candidate_id": normalized_candidate.candidate_id,
                },
            ),
            decision_id=(matched_decision.decision_id if matched_decision is not None else None),
            proposal_id=None,
            original_candidate_id=candidate.candidate_id,
            original_source_term=candidate.source_term,
            normalized_candidate_id=normalized_candidate.candidate_id,
            normalized_source_term=normalized_candidate.source_term,
            both_candidate_records_exist=True,
            immutable_references_resolve=True,
            safest_migration_strategy=(
                "Preserve the whitespace-bearing historical candidate as the immutable source of "
                "record and add an explicit normalized-identity migration link to the stripped "
                "candidate. Do not rewrite proposal or review history."
            ),
            evidence={
                "original_source_sha256": hashlib.sha256(
                    candidate.source_term.encode("utf-8")
                ).hexdigest(),
                "normalized_source_sha256": hashlib.sha256(
                    normalized_candidate.source_term.encode("utf-8")
                ).hexdigest(),
                "proposal_run_id": "ta-dfcd64474648f3de61573621",
                "review_run_id": "ta-ec75fd8fc1654ec3be141a96",
            },
        )
        break
    return findings, mismatch


def _exactness_finding(
    registry: TerminologyRegistry, judgement: ExactnessJudgement
) -> TerminologyRemediationFinding:
    """Adapt one exact-enforcement judgement into the shared remediation finding contract.

    The two audit families deliberately share one finding type so that the plan keeps its rule of
    exactly one primary operation per finding, and so an operator inspects, filters, and applies
    both through the same commands.
    """

    blocking = judgement.disposition is TerminologyExactnessDisposition.DEFINITELY_INVALID
    return TerminologyRemediationFinding(
        finding_id=_stable_id(
            "rmf",
            {
                "registry_identity": registry_identity(registry),
                "family": TerminologyFindingFamily.EXACT_ENFORCEMENT.value,
                "decision_id": judgement.decision_id,
                "candidate_id": judgement.candidate_id,
                "risks": [item.value for item in judgement.risks],
                "rule": judgement.rule.value,
                "disposition": judgement.disposition.value,
                "target_term": judgement.target_term,
            },
        ),
        finding_family=TerminologyFindingFamily.EXACT_ENFORCEMENT,
        decision_id=judgement.decision_id,
        candidate_id=judgement.candidate_id,
        source_term=judgement.source_term,
        target_term=judgement.target_term,
        finding_category=judgement.primary_risk.value,
        validator_categories=[],
        exactness_risks=[item.value for item in judgement.risks],
        exactness_disposition=judgement.disposition,
        exactness_rule=judgement.rule.value,
        occurrence_count=judgement.occurrence_count,
        proposed_enforcement=judgement.proposed_enforcement,
        corrected_target=judgement.corrected_target,
        correction_component_source_term=judgement.correction_component_source_term,
        correction_replaced_token=judgement.correction_replaced_token,
        question_category=judgement.question_category,
        question=judgement.question,
        related_decision_ids=[],
        related_source_terms=list(judgement.related_source_terms),
        related_target_terms=[],
        triggered_rule=judgement.rationale,
        morphology_involved=False,
        evidence={f"exactness_{index}": value for index, value in enumerate(judgement.evidence)},
        severity=(
            "error"
            if blocking
            else "info"
            if judgement.disposition is TerminologyExactnessDisposition.DEFINITELY_VALID
            else "warning"
        ),
        remediation_category=judgement.category,
        proposed_action=judgement.action,
        blocking=blocking,
    )


def _exactness_findings(
    registry: TerminologyRegistry,
    view: tuple[CanonicalTerm, ...],
    deferred: frozenset[str],
) -> tuple[list[TerminologyRemediationFinding], list[str]]:
    report = audit_exact_enforcement(registry)
    judgements = judge_exact_enforcement(registry, view, report, deferred_candidate_ids=deferred)
    stood_down = sorted(
        {item.source_term for item in report.findings if item.candidate_id in deferred}
    )
    return [_exactness_finding(registry, item) for item in judgements], stood_down


def _load_review_context(
    project_root: Path, review_run_id: str
) -> tuple[TerminologyRegistry, tuple[TerminologyCandidate, ...], tuple[Any, ...], tuple[Any, ...]]:
    review_run_path = project_root / "workspace/terminology-runs" / review_run_id
    review_run, reviews = load_review_output(review_run_path)
    if review_run.proposal_run_id is None:
        raise ValueError("review run has no proposal lineage")
    proposal_run, proposals = load_proposal_output(
        review_run_path.parent / review_run.proposal_run_id
    )
    if proposal_run.discovery_run_id is None:
        raise ValueError("proposal run has no discovery lineage")
    _, candidates = load_discovery_output(review_run_path.parent / proposal_run.discovery_run_id)
    registry = load_registry(project_root / "config/terminology.yaml")
    return registry, candidates, proposals, reviews


def _escalation_basis(
    candidate: TerminologyCandidate,
    decision: TerminologyDecision | None,
    question: OpenQuestion | None,
) -> tuple[
    Literal[
        "confirmed_decision",
        "provisional_decision",
        "explicit_rejection",
        "open_question",
        "undecided",
    ],
    bool,
]:
    """Return the current resolution basis and whether the escalation is still unresolved."""

    if decision is not None:
        if decision.decision_state is DecisionState.PROVISIONAL:
            # A provisional decision closes the escalation for readiness purposes unless its
            # question is explicitly blocking; that is the whole point of provisional state.
            blocking = question is not None and question.blocking
            return "provisional_decision", blocking
        return "confirmed_decision", False
    if candidate.status is CandidateStatus.REJECTED:
        return "explicit_rejection", False
    if question is not None and question.status is QuestionStatus.OPEN:
        return "open_question", True
    return "undecided", True


def _escalation_disposition(
    candidate: TerminologyCandidate,
    review_disposition: str,
    decision: TerminologyDecision | None,
    question: OpenQuestion | None,
) -> tuple[TerminologyEscalationDisposition, str, str | None, float]:
    """Derive an escalation's disposition from current state, never from a term table.

    The authoritative signals are the registry (does a canonical decision exist, is it confirmed
    or provisional, was the candidate explicitly rejected) and the open-question registry.  The
    historical review disposition only matters when nothing has been decided yet, so resolving a
    term is enough to close its escalation without editing this function.
    """

    if decision is not None:
        if decision.decision_state is DecisionState.PROVISIONAL:
            reason = (
                question.question
                if question is not None
                else "A provisional decision is enforced while its question stays open."
            )
            return (
                TerminologyEscalationDisposition.NEEDS_USER_CHOICE
                if question is not None and question.blocking
                else TerminologyEscalationDisposition.CANONICAL,
                reason,
                decision.target_term,
                0.6,
            )
        return (
            TerminologyEscalationDisposition.CANONICAL,
            "The registry holds a confirmed canonical decision for this term.",
            decision.target_term,
            1.0,
        )
    if candidate.status is CandidateStatus.REJECTED:
        return (
            TerminologyEscalationDisposition.REJECT_NOT_TERMINOLOGY,
            "The candidate is explicitly recorded as not terminology and is never enforced.",
            None,
            1.0,
        )
    if question is not None and question.status is QuestionStatus.OPEN:
        return (
            TerminologyEscalationDisposition.NEEDS_USER_CHOICE,
            question.question,
            question.provisional_target,
            0.5,
        )
    return (
        TerminologyEscalationDisposition.NEEDS_USER_CHOICE
        if review_disposition == "defer"
        else TerminologyEscalationDisposition.REJECT_NOT_TERMINOLOGY,
        "No canonical decision, rejection, or open question exists for this candidate yet.",
        None,
        0.5,
    )


def _term_kind(
    candidate: TerminologyCandidate,
) -> Literal[
    "proper noun",
    "title",
    "fictional/world-specific term",
    "ordinary vocabulary",
    "source anomaly/noise",
]:
    """Map a classification to an evidence-packet term kind without naming any term."""

    if candidate.classification in {
        TerminologyType.PERSON,
        TerminologyType.PLACE,
        TerminologyType.ORGANIZATION,
        TerminologyType.PEOPLE,
    }:
        return "proper noun"
    if candidate.classification is TerminologyType.TITLE:
        return "title"
    if candidate.classification in {
        TerminologyType.INVENTED,
        TerminologyType.ARTIFACT,
        TerminologyType.HISTORICAL_EVENT,
        TerminologyType.STORY_TERM,
    }:
        return "fictional/world-specific term"
    return "ordinary vocabulary"


def _competing_interpretations(candidate: TerminologyCandidate, kind: str) -> list[str]:
    """Derive the plausible readings from the recorded classification conflicts."""

    if candidate.classification_conflicts:
        return [item.value for item in candidate.classification_conflicts]
    if kind == "proper noun":
        return ["proper noun", "ordinary vocabulary"]
    if kind == "fictional/world-specific term":
        return ["fictional/world-specific term", "ordinary vocabulary"]
    if kind == "title":
        return ["title", "proper noun"]
    return ["ordinary vocabulary", "proper noun"]


def load_escalations(
    project_root: Path,
    review_run_id: str,
    *,
    registry: TerminologyRegistry | None = None,
    questions: OpenQuestionRegistry | None = None,
) -> tuple[TerminologyEscalationFinding, ...]:
    review_run_path = project_root / "workspace/terminology-runs" / review_run_id
    review_run, reviews = load_review_output(review_run_path)
    if review_run.proposal_run_id is None:
        raise ValueError("review run has no proposal lineage")
    proposal_run, proposals = load_proposal_output(
        review_run_path.parent / review_run.proposal_run_id
    )
    if proposal_run.discovery_run_id is None:
        raise ValueError("proposal run has no discovery lineage")
    _, candidates = load_discovery_output(review_run_path.parent / proposal_run.discovery_run_id)
    candidate_by_id = {item.candidate_id: item for item in candidates}
    proposal_by_id = {item.proposal_id: item for item in proposals}
    # Current state wins over the historical run: an escalation is closed by resolving the term,
    # not by editing this loader.
    live_candidates = {item.candidate_id: item for item in registry.candidates} if registry else {}
    live_decisions = {item.candidate_id: item for item in registry.decisions} if registry else {}
    live_questions = (
        {
            item.candidate_id: item
            for item in questions.questions
            if item.status is QuestionStatus.OPEN
        }
        if questions
        else {}
    )
    output: list[TerminologyEscalationFinding] = []
    for review in sorted(reviews, key=lambda item: item.proposal_id):
        if review.disposition.value != "defer":
            continue
        proposal = proposal_by_id[review.proposal_id]
        candidate = candidate_by_id[proposal.candidate_id]
        current = live_candidates.get(candidate.candidate_id, candidate)
        disposition, reason, recommended, confidence = _escalation_disposition(
            current,
            review.disposition.value,
            live_decisions.get(candidate.candidate_id),
            live_questions.get(candidate.candidate_id),
        )
        kind = _term_kind(current)
        basis, unresolved = _escalation_basis(
            current,
            live_decisions.get(candidate.candidate_id),
            live_questions.get(candidate.candidate_id),
        )
        chapters = {occ.chapter_id for occ in candidate.occurrences}
        chunks = {occ.chunk_id for occ in candidate.occurrences if occ.chunk_id is not None}
        output.append(
            TerminologyEscalationFinding(
                escalation_id=_stable_id(
                    "rme",
                    {
                        "review_run_id": review_run_id,
                        "proposal_id": review.proposal_id,
                        "candidate_id": candidate.candidate_id,
                    },
                ),
                proposal_id=review.proposal_id,
                candidate_id=candidate.candidate_id,
                source_term=candidate.source_term,
                terminology_type=candidate.classification.value,
                source_form=candidate.source_term,
                aliases=list(candidate.aliases),
                chapter_count=len(chapters),
                chunk_count=len(chunks),
                occurrence_count=sum(item.count for item in candidate.occurrences),
                representative_contexts=[
                    f"{occ.chapter_id}:{occ.chunk_id or '-'}" for occ in candidate.occurrences[:3]
                ],
                why_escalated=(
                    "Current evidence does not resolve the lexeme cleanly enough for an automatic "
                    "canonical or reject decision."
                ),
                competing_interpretations=_competing_interpretations(current, kind),
                related_existing_canonical_terminology=[],
                capitalization_meaningful=any(ch.isupper() for ch in candidate.source_term),
                term_kind=kind,
                recommended_persian=recommended,
                confidence=confidence,
                disposition=disposition,
                reason=reason,
                unresolved=unresolved,
                resolution_basis=basis,
            )
        )
    return tuple(output)


def audit_registry_remediation(
    registry: TerminologyRegistry,
    glossary: Any,
    *,
    review_run_id: str | None = None,
    project_root: Path | None = None,
    questions: OpenQuestionRegistry | None = None,
) -> TerminologyRemediationReport:
    view = canonical_view(registry, _normalize_glossary(glossary))
    findings, mismatch = _findings_for_registry(registry, view)
    registry_finding_count = len(findings)
    # A blocking canonical-validator finding is a contradiction between two canonical records, and
    # that must be settled before the weaker question of what evidence proves the decision.  So the
    # exact-enforcement family stands down on exactly those decisions rather than acting twice.
    deferred = frozenset(item.candidate_id for item in findings if item.blocking)
    exactness_findings, deferred_terms = _exactness_findings(registry, view, deferred)
    findings = [*findings, *exactness_findings]
    bookkeeping_count = 1 if mismatch is not None else 0
    escalations: tuple[TerminologyEscalationFinding, ...] = ()
    if review_run_id is not None:
        if project_root is None:
            project_root = Path.cwd()
        escalations = load_escalations(
            project_root, review_run_id, registry=registry, questions=questions
        )
    validator_counts: Counter[str] = Counter()
    remediation_counts: Counter[str] = Counter()
    for finding in findings:
        for category in finding.validator_categories:
            validator_counts[category] += 1
        remediation_counts[finding.remediation_category.value] += 1
    affected = len({finding.decision_id for finding in findings})
    definitely_valid = sum(
        1
        for finding in findings
        if finding.remediation_category
        in {
            TerminologyRemediationCategory.VALID_COMPOUND_OR_MORPHOLOGY,
            TerminologyRemediationCategory.EXACT_ENFORCEMENT_UPHELD,
        }
    )
    definitely_invalid = sum(1 for finding in findings if finding.blocking)
    context_review = sum(
        1
        for finding in findings
        if finding.remediation_category
        in {
            TerminologyRemediationCategory.REQUIRES_CONTEXT_REVIEW,
            TerminologyRemediationCategory.EXACT_TARGET_REQUIRES_REVIEW,
            TerminologyRemediationCategory.OWNER_GOVERNED_DECISION,
        }
    )
    identity_migration = bookkeeping_count
    validator_false_positive = definitely_valid
    disposition_counts = Counter(
        item.exactness_disposition.value
        for item in exactness_findings
        if item.exactness_disposition is not None
    )
    rule_counts = Counter(
        item.exactness_rule for item in exactness_findings if item.exactness_rule is not None
    )
    remaining_blockers = definitely_invalid + bookkeeping_count
    all_decision_ids = [finding.decision_id for finding in findings]
    coverage = TerminologyRemediationCoverage(
        proposal_ids_unique=len(findings) == len({finding.finding_id for finding in findings}),
        disposition_ids_unique=len(all_decision_ids) == len(set(all_decision_ids)),
        affected_decision_ids_unique=len({finding.decision_id for finding in findings})
        == len(findings),
        proposal_set_equals_disposition_set=True,
        missing_decision_ids=[],
        unexpected_decision_ids=[],
    )
    audit_id = _stable_id(
        "remediation-audit",
        {
            "registry_identity": registry_identity(registry),
            "review_run_id": review_run_id,
            "finding_ids": [finding.finding_id for finding in findings],
            "bookkeeping_mismatch": mismatch.model_dump(mode="json") if mismatch else None,
        },
    )
    return TerminologyRemediationReport(
        schema_version="1.0",
        audit_id=audit_id,
        registry_sha256=registry_identity(registry),
        decisions_scanned=len(registry.decisions),
        findings_total=len(findings) + bookkeeping_count,
        affected_decisions=affected,
        registry_finding_count=registry_finding_count,
        exactness_finding_count=len(exactness_findings),
        exactness_deferred_count=len(deferred_terms),
        exactness_deferred_source_terms=deferred_terms,
        exactness_disposition_counts=dict(sorted(disposition_counts.items())),
        exactness_rule_counts=dict(sorted(rule_counts.items())),
        owner_governed_count=sum(
            1
            for item in exactness_findings
            if item.remediation_category is TerminologyRemediationCategory.OWNER_GOVERNED_DECISION
        ),
        bookkeeping_mismatch_count=bookkeeping_count,
        escalation_count=len(escalations),
        unresolved_escalation_count=sum(1 for item in escalations if item.unresolved),
        blocking_escalation_count=sum(
            1
            for item in escalations
            if item.unresolved
            and item.disposition is TerminologyEscalationDisposition.NEEDS_USER_CHOICE
        ),
        validator_category_counts=dict(sorted(validator_counts.items())),
        remediation_category_counts=dict(sorted(remediation_counts.items())),
        definitely_valid_count=definitely_valid,
        definitely_invalid_count=definitely_invalid,
        context_review_count=context_review,
        identity_migration_count=identity_migration,
        validator_false_positive_count=validator_false_positive,
        remaining_registry_blockers=remaining_blockers,
        findings=findings,
        bookkeeping_mismatch=mismatch,
        escalations=list(escalations),
        coverage=coverage,
    )


def inspect_remediation_finding(
    report: TerminologyRemediationReport, finding_id: str
) -> TerminologyRemediationFinding:
    for finding in report.findings:
        if finding.finding_id == finding_id:
            return finding
    raise ValueError(f"remediation finding not found: {finding_id}")


def serialize_remediation_report(report: TerminologyRemediationReport) -> str:
    rendered = json.dumps(
        report.model_dump(mode="json", exclude_none=False),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return rendered + "\n"
