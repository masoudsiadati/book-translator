"""Deterministic terminology registry remediation planning, simulation, and application."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyOccurrence,
    TerminologyRegistry,
)
from book_translator.models.terminology_reconciliation import RegistryEdit, RegistryEditKind
from book_translator.models.terminology_remediation import (
    RETIREMENT_ACTIONS,
    TerminologyCandidateMigration,
    TerminologyDecisionRetirement,
    TerminologyEnforcementChange,
    TerminologyFindingFamily,
    TerminologyLookupChangeKind,
    TerminologyLookupComparison,
    TerminologyQuestionRequest,
    TerminologyRemediationAction,
    TerminologyRemediationCategory,
    TerminologyRemediationOperation,
    TerminologyRemediationPlan,
    TerminologyRemediationResolution,
    TerminologyRemediationSimulation,
    TerminologyTargetCorrection,
    TerminologyVariantRemoval,
)
from book_translator.paths import ProjectPaths
from book_translator.terminology.canonical import (
    CanonicalTerm,
    canonical_constraints,
    canonical_view,
    exact_canonical,
    target_identity_present,
)
from book_translator.terminology.enforcement import resolve_enforcement
from book_translator.terminology.questions import question_identity
from book_translator.terminology.registry import (
    TerminologyError,
    canonical_registry_bytes,
    decision_identity,
    load_registry,
    registry_identity,
    save_registry,
)
from book_translator.terminology.remediation import (
    TerminologyRemediationFinding,
    TerminologyRemediationReport,
    audit_registry_remediation,
)
from book_translator.terminology.target_style import canonical_targets_equal

REMEDIATION_ENGINE_VERSION = "1.1.0"
PLAN_SCHEMA_VERSION = "1.0"

# Publication order: variant relations are removed before the identity migration so that a
# migrated candidate is never referenced by a variant relation that is about to disappear, and
# every edit that reads a decision runs before the retirement that withdraws it.
_ACTION_ORDER: dict[TerminologyRemediationAction, int] = {
    TerminologyRemediationAction.REMOVE_INVALID_VARIANT: 0,
    TerminologyRemediationAction.APPLY_SEMANTIC_RESOLUTION: 1,
    TerminologyRemediationAction.CORRECT_TARGET: 2,
    TerminologyRemediationAction.CHANGE_ENFORCEMENT: 3,
    TerminologyRemediationAction.RETIRE_DECISION: 4,
    TerminologyRemediationAction.RETIRE_AND_REJECT_CANDIDATE: 5,
    TerminologyRemediationAction.IDENTITY_MIGRATION: 6,
    TerminologyRemediationAction.MERGE_DECISIONS: 7,
    TerminologyRemediationAction.NO_CHANGE_VALIDATOR_FIX: 8,
    TerminologyRemediationAction.NO_CHANGE_VALID_DISTINCT: 9,
    TerminologyRemediationAction.NO_CHANGE_EXACT_UPHELD: 10,
    TerminologyRemediationAction.NO_CHANGE_OWNER_AUTHORITY: 11,
    TerminologyRemediationAction.OPEN_TERMINOLOGY_QUESTION: 12,
    TerminologyRemediationAction.CONVERT_TO_ALIAS: 13,
    TerminologyRemediationAction.CHANGE_TARGET_REQUIRES_REVIEW: 14,
    TerminologyRemediationAction.MANUAL_CONTEXT_REVIEW: 15,
}

PROTECTED_TERMS: tuple[tuple[str, str], ...] = (
    ("Ajah", "آجای"),
    # The Ajah colour shorthands are protected precisely because an over-broad component rule once
    # pushed 'آجای سرخ' onto every unrelated name containing the word "Red".
    ("Red Ajah", "آجای سرخ"),
    ("Rand", "رند"),
    ("Whitecloak", "فرزند نور"),
    ("Aes Sedai", "آیزسدای"),
    ("Forsaken", "مطرودین"),
    ("The One Power", "قدرت یگانه"),
    ("Saidar", "سایدار"),
    ("Saidin", "ساییدین"),
    ("Amyrlin Seat", "مقام امیرلین"),
    ("Rand al'Thor", "رند ال ثور"),
    ("Nynaeve", "ناینیو"),
)


class TerminologyRemediationPlanError(TerminologyError):
    """Raised when a remediation plan cannot be built or applied safely."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        + b"\n"
    )


def _stable_id(prefix: str, payload: Any) -> str:
    return f"{prefix}-{hashlib.sha256(_canonical_bytes(payload)).hexdigest()[:24]}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bump_patch(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def canonical_lookup(view: tuple[CanonicalTerm, ...], source_form: str) -> CanonicalTerm | None:
    """Resolve one source form exactly as the canonical validator resolves it."""
    matches = [
        term
        for term in view
        if source_form == term.source_term or source_form in term.source_variants
    ]
    return max(matches, key=lambda item: item.priority, default=None)


def _decisions_by_candidate(registry: TerminologyRegistry) -> dict[str, TerminologyDecision]:
    return {item.candidate_id: item for item in registry.decisions}


def _candidates_by_id(registry: TerminologyRegistry) -> dict[str, TerminologyCandidate]:
    return {item.candidate_id: item for item in registry.candidates}


def _source_decision_index(
    registry: TerminologyRegistry,
) -> dict[str, TerminologyDecision]:
    """Map each primary source term to its canonical decision exactly once per plan."""
    decisions = _decisions_by_candidate(registry)
    index: dict[str, TerminologyDecision] = {}
    for candidate in registry.candidates:
        decision = decisions.get(candidate.candidate_id)
        if decision is not None:
            index.setdefault(candidate.source_term, decision)
    return index


def _before_state_hash(
    registry: TerminologyRegistry, decision_ids: list[str], candidate_ids: list[str]
) -> str:
    decisions = {item.decision_id: item for item in registry.decisions}
    candidates = _candidates_by_id(registry)
    payload = {
        "decisions": [
            decisions[item].model_dump(mode="json")
            for item in sorted(set(decision_ids))
            if item in decisions
        ],
        "candidates": [
            candidates[item].model_dump(mode="json")
            for item in sorted(set(candidate_ids))
            if item in candidates
        ],
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _related_terms_for(
    candidates: dict[str, TerminologyCandidate],
    finding: TerminologyRemediationFinding,
    view: tuple[CanonicalTerm, ...],
) -> tuple[CanonicalTerm | None, tuple[CanonicalTerm, ...]]:
    candidate = candidates[finding.candidate_id]
    return exact_canonical(candidate, view), canonical_constraints(candidate, view)


def _priority_evidence(
    flagged: TerminologyDecision,
    exact: CanonicalTerm | None,
    constraints: tuple[CanonicalTerm, ...],
) -> dict[str, str | int | float | bool | None]:
    involved = [term for term in (exact, *constraints) if term is not None]
    priorities = sorted({term.priority for term in involved})
    return {
        "flagged_decision_source": flagged.decision_source.value,
        "flagged_decision_locked": flagged.locked,
        "involved_term_count": len(involved),
        "involved_priorities": ",".join(str(value) for value in priorities),
        "distinct_priority_count": len(priorities),
        "has_locked_user_authority": any(term.priority == 5 for term in involved),
        "has_legacy_glossary_authority": any(term.origin == "legacy_glossary" for term in involved),
    }


def _plan_variant_removals(
    registry: TerminologyRegistry,
    finding: TerminologyRemediationFinding,
    view: tuple[CanonicalTerm, ...],
    candidates: dict[str, TerminologyCandidate],
    sources: dict[str, TerminologyDecision],
) -> list[TerminologyVariantRemoval]:
    """Find every stored competing-variant relation that duplicates this canonical target."""
    exact, constraints = _related_terms_for(candidates, finding, view)
    owner = sources.get(finding.source_term)
    if owner is None:
        return []
    owner_target = owner.target_term
    removals: list[TerminologyVariantRemoval] = []
    seen: set[tuple[str, str]] = set()
    for term in (item for item in (exact, *constraints) if item is not None):
        if term.origin != "registry":
            continue
        holder = sources.get(term.source_term)
        if holder is None or holder.decision_id == owner.decision_id:
            continue
        for variant in holder.competing_variants:
            if not target_identity_present(variant, owner_target, registry.target_language):
                continue
            key = (holder.decision_id, variant)
            if key in seen:
                continue
            seen.add(key)
            removals.append(
                TerminologyVariantRemoval(
                    holder_decision_id=holder.decision_id,
                    holder_candidate_id=holder.candidate_id,
                    holder_source_term=term.source_term,
                    holder_target_term=holder.target_term,
                    removed_variant=variant,
                    canonical_owner_decision_id=owner.decision_id,
                    canonical_owner_source_term=finding.source_term,
                    canonical_owner_target_term=owner_target,
                )
            )
    return sorted(removals, key=lambda item: (item.holder_decision_id, item.removed_variant))


def _record_hash(record: TerminologyCandidate | TerminologyDecision) -> str:
    """Content-address one registry record so a plan proves which state it was built against."""
    return hashlib.sha256(_canonical_bytes(record.model_dump(mode="json"))).hexdigest()


def _owner_governed(decision: TerminologyDecision) -> bool:
    return decision.locked or decision.decision_source is DecisionSource.USER


def _retirement(
    finding: TerminologyRemediationFinding,
    candidate: TerminologyCandidate,
    decision: TerminologyDecision,
    *,
    reject: bool,
) -> TerminologyDecisionRetirement:
    reason = (
        f"Exact enforcement audit rule {finding.exactness_rule}: {finding.triggered_rule}"
        if finding.exactness_rule
        else finding.triggered_rule
    )
    return TerminologyDecisionRetirement(
        decision_id=decision.decision_id,
        candidate_id=candidate.candidate_id,
        source_term=candidate.source_term,
        retired_target_term=decision.target_term,
        retired_enforcement=decision.enforcement,
        resolved_enforcement=resolve_enforcement(candidate, decision),
        decision_source=decision.decision_source.value,
        decision_state=decision.decision_state.value,
        retired_aliases=list(candidate.aliases),
        retired_competing_variants=list(decision.competing_variants),
        occurrence_count=sum(item.count for item in candidate.occurrences),
        reject_candidate=reject,
        candidate_status_before=candidate.status.value,
        candidate_status_after=(
            CandidateStatus.REJECTED.value if reject else candidate.status.value
        ),
        rejection_reason=reason if reject else None,
        decision_record_sha256=_record_hash(decision),
        candidate_record_sha256=_record_hash(candidate),
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class _ExactnessStep:
    """The governed change one exact-enforcement finding authorizes, or the refusal to act."""

    action: TerminologyRemediationAction
    resolution: TerminologyRemediationResolution
    mutates: bool
    retirement: TerminologyDecisionRetirement | None = None
    enforcement_change: TerminologyEnforcementChange | None = None
    target_correction: TerminologyTargetCorrection | None = None
    question_request: TerminologyQuestionRequest | None = None
    blocked_reason: str | None = None
    blocking_evidence: tuple[str, ...] = ()


_REGISTRY_WRITING_ACTIONS = frozenset(
    {
        *RETIREMENT_ACTIONS,
        TerminologyRemediationAction.CHANGE_ENFORCEMENT,
        TerminologyRemediationAction.CORRECT_TARGET,
    }
)


def _target_correction(
    finding: TerminologyRemediationFinding,
    candidate: TerminologyCandidate,
    decision: TerminologyDecision,
    sources: dict[str, TerminologyDecision],
) -> TerminologyTargetCorrection:
    component_term = finding.correction_component_source_term
    if (
        finding.corrected_target is None
        or component_term is None
        or (finding.correction_replaced_token is None)
    ):
        raise TerminologyRemediationPlanError(
            "a target correction finding must carry the component and token it rewrites"
        )
    component = sources.get(component_term)
    if component is None:
        raise TerminologyRemediationPlanError(
            f"the canonical component this correction spells is not decided: {component_term!r}"
        )
    return TerminologyTargetCorrection(
        decision_id=decision.decision_id,
        candidate_id=candidate.candidate_id,
        source_term=candidate.source_term,
        target_before=decision.target_term,
        target_after=finding.corrected_target,
        component_source_term=component_term,
        component_decision_id=component.decision_id,
        component_target_term=component.target_term,
        replaced_token=finding.correction_replaced_token,
        decision_record_sha256=_record_hash(decision),
        reason=finding.triggered_rule,
    )


def _exactness_step(
    finding: TerminologyRemediationFinding,
    candidate: TerminologyCandidate,
    decision: TerminologyDecision,
    sources: dict[str, TerminologyDecision],
) -> _ExactnessStep:
    """Derive the governed change one exact-enforcement finding authorizes, or refuse to act.

    The ownership guard is repeated here on purpose.  The classifier already stands down on a
    locked or user-owned decision, but a plan that could ever emit a retirement for one would be a
    single rule regression away from deleting a human's terminology, so the planner proves the
    property itself instead of trusting its input.
    """

    action = finding.proposed_action
    if _owner_governed(decision) and action in _REGISTRY_WRITING_ACTIONS:
        return _ExactnessStep(
            action=TerminologyRemediationAction.NO_CHANGE_OWNER_AUTHORITY,
            resolution=TerminologyRemediationResolution.UNRESOLVED,
            mutates=False,
            blocked_reason=(
                "A locked or user-owned canonical decision is governed by the explicit correction"
                " workflow, so no remediation plan may withdraw, relax, or rewrite it."
            ),
            blocking_evidence=("owner_governed_decision",),
        )
    if action in RETIREMENT_ACTIONS:
        return _ExactnessStep(
            action=action,
            resolution=TerminologyRemediationResolution.DETERMINISTIC,
            mutates=True,
            retirement=_retirement(
                finding,
                candidate,
                decision,
                reject=action is TerminologyRemediationAction.RETIRE_AND_REJECT_CANDIDATE,
            ),
        )
    if action is TerminologyRemediationAction.CHANGE_ENFORCEMENT:
        if finding.proposed_enforcement is None:
            raise TerminologyRemediationPlanError(
                "an enforcement change finding must name the enforcement mode it proposes"
            )
        return _ExactnessStep(
            action=action,
            resolution=TerminologyRemediationResolution.DETERMINISTIC,
            mutates=True,
            enforcement_change=TerminologyEnforcementChange(
                decision_id=decision.decision_id,
                candidate_id=candidate.candidate_id,
                source_term=candidate.source_term,
                target_term=decision.target_term,
                enforcement_before=decision.enforcement,
                resolved_enforcement_before=resolve_enforcement(candidate, decision),
                enforcement_after=finding.proposed_enforcement,
                decision_record_sha256=_record_hash(decision),
                reason=finding.triggered_rule,
            ),
        )
    if action is TerminologyRemediationAction.CORRECT_TARGET:
        correction = _target_correction(finding, candidate, decision, sources)
        # The relaxation, when the audit asks for one, is preconditioned on the decision *as this
        # same operation has just corrected it*, which is the order the apply half uses.
        corrected = decision.model_copy(update={"target_term": correction.target_after})
        return _ExactnessStep(
            action=action,
            resolution=TerminologyRemediationResolution.DETERMINISTIC,
            mutates=True,
            target_correction=correction,
            enforcement_change=(
                TerminologyEnforcementChange(
                    decision_id=decision.decision_id,
                    candidate_id=candidate.candidate_id,
                    source_term=candidate.source_term,
                    target_term=correction.target_after,
                    enforcement_before=decision.enforcement,
                    resolved_enforcement_before=resolve_enforcement(candidate, corrected),
                    enforcement_after=finding.proposed_enforcement,
                    decision_record_sha256=_record_hash(corrected),
                    reason=(
                        "Correcting the component spelling makes this compound honor its canonical"
                        " component, which leaves only the over-broad claim that the whole"
                        " description has one fixed rendering."
                    ),
                )
                if finding.proposed_enforcement is not None
                else None
            ),
        )
    if action is TerminologyRemediationAction.OPEN_TERMINOLOGY_QUESTION:
        if finding.question_category is None or finding.question is None:
            raise TerminologyRemediationPlanError(
                "a question finding must carry the question it requires"
            )
        return _ExactnessStep(
            action=action,
            resolution=TerminologyRemediationResolution.DETERMINISTIC,
            mutates=False,
            question_request=TerminologyQuestionRequest(
                question_id=question_identity(candidate.candidate_id, finding.question_category),
                candidate_id=candidate.candidate_id,
                decision_id=decision.decision_id,
                source_term=candidate.source_term,
                provisional_target=decision.target_term,
                category=finding.question_category,
                question=finding.question,
                related_source_terms=list(finding.related_source_terms),
                evidence=[
                    str(value) for _, value in sorted(finding.evidence.items()) if value is not None
                ],
            ),
        )
    return _ExactnessStep(
        action=action,
        resolution=TerminologyRemediationResolution.DETERMINISTIC,
        mutates=False,
    )


def _exactness_operation_for_finding(
    registry: TerminologyRegistry,
    finding: TerminologyRemediationFinding,
    candidates: dict[str, TerminologyCandidate],
    decisions: dict[str, TerminologyDecision],
    sources: dict[str, TerminologyDecision],
) -> TerminologyRemediationOperation:
    candidate = candidates[finding.candidate_id]
    decision = decisions[finding.decision_id]
    step = _exactness_step(finding, candidate, decision, sources)
    affected_decisions = [finding.decision_id]
    affected_candidates = [finding.candidate_id]
    provenance: dict[str, str | int | float | bool | None] = {
        "exactness_rule": finding.exactness_rule,
        "exactness_risks": ",".join(finding.exactness_risks),
        "exactness_disposition": (
            finding.exactness_disposition.value
            if finding.exactness_disposition is not None
            else None
        ),
        "occurrence_count": finding.occurrence_count,
        "resolved_enforcement": resolve_enforcement(candidate, decision).value,
        "flagged_decision_source": decision.decision_source.value,
        "flagged_decision_locked": decision.locked,
        "related_source_term_count": len(finding.related_source_terms),
    }
    operation_id = _stable_id(
        "rmo",
        {
            "finding_id": finding.finding_id,
            "action": step.action.value,
            "resolution": step.resolution.value,
            "retirement": (step.retirement.model_dump(mode="json") if step.retirement else None),
            "enforcement_change": (
                step.enforcement_change.model_dump(mode="json") if step.enforcement_change else None
            ),
            "target_correction": (
                step.target_correction.model_dump(mode="json") if step.target_correction else None
            ),
            "question_request": (
                step.question_request.model_dump(mode="json") if step.question_request else None
            ),
        },
    )
    return TerminologyRemediationOperation(
        operation_id=operation_id,
        sequence=1,
        finding_id=finding.finding_id,
        finding_family=TerminologyFindingFamily.EXACT_ENFORCEMENT,
        remediation_category=finding.remediation_category,
        action=step.action,
        resolution=step.resolution,
        exactness_disposition=finding.exactness_disposition,
        exactness_risks=list(finding.exactness_risks),
        exactness_rule=finding.exactness_rule,
        mutates_registry=step.mutates,
        decision_id=finding.decision_id,
        candidate_id=finding.candidate_id,
        source_term=finding.source_term,
        target_term=finding.target_term,
        affected_decision_ids=affected_decisions,
        affected_candidate_ids=affected_candidates,
        before_state_sha256=_before_state_hash(registry, affected_decisions, affected_candidates),
        retirement=step.retirement,
        enforcement_change=step.enforcement_change,
        target_correction=step.target_correction,
        question_request=step.question_request,
        reason=finding.triggered_rule,
        provenance=provenance,
        blocked_reason=step.blocked_reason,
        blocking_evidence=list(step.blocking_evidence),
    )


def _operation_for_finding(
    registry: TerminologyRegistry,
    finding: TerminologyRemediationFinding,
    view: tuple[CanonicalTerm, ...],
    candidates: dict[str, TerminologyCandidate],
    decisions: dict[str, TerminologyDecision],
    sources: dict[str, TerminologyDecision],
) -> TerminologyRemediationOperation:
    flagged = decisions[finding.decision_id]
    exact, constraints = _related_terms_for(candidates, finding, view)
    category = finding.remediation_category
    affected_decisions = sorted({finding.decision_id, *finding.related_decision_ids})
    affected_candidates = [finding.candidate_id]
    provenance = _priority_evidence(flagged, exact, constraints)
    provenance["audit_finding_category"] = finding.finding_category
    provenance["validator_conflicts"] = ",".join(finding.validator_categories)

    action = TerminologyRemediationAction.MANUAL_CONTEXT_REVIEW
    resolution = TerminologyRemediationResolution.UNRESOLVED
    mutates = False
    removals: list[TerminologyVariantRemoval] = []
    blocked: str | None = None
    evidence: list[str] = []
    reason = finding.triggered_rule

    if category in {
        TerminologyRemediationCategory.VALID_COMPOUND_OR_MORPHOLOGY,
        TerminologyRemediationCategory.VALID_DISTINCT_TERM,
    }:
        action = (
            TerminologyRemediationAction.NO_CHANGE_VALIDATOR_FIX
            if category is TerminologyRemediationCategory.VALID_COMPOUND_OR_MORPHOLOGY
            else TerminologyRemediationAction.NO_CHANGE_VALID_DISTINCT
        )
        resolution = TerminologyRemediationResolution.DETERMINISTIC
        reason = (
            "The corrected validator recognizes legitimate compound or morphological structure; "
            "the canonical target is already correct and is left byte-identical."
        )
    elif category is TerminologyRemediationCategory.INVALID_COMPETING_VARIANT:
        action = TerminologyRemediationAction.REMOVE_INVALID_VARIANT
        removals = _plan_variant_removals(registry, finding, view, candidates, sources)
        if removals:
            resolution = TerminologyRemediationResolution.DETERMINISTIC
            mutates = True
            affected_decisions = sorted(
                {*affected_decisions, *(item.holder_decision_id for item in removals)}
            )
            reason = (
                "A canonical target is stored as a competing Persian variant of another "
                "decision; only that malformed variant relation is removed."
            )
        else:
            blocked = (
                "No stored competing-variant relation duplicates this canonical target, so the "
                "invalid relation cannot be identified without interpretation."
            )
            evidence.append("no_matching_competing_variant_relation")
    elif category is TerminologyRemediationCategory.ALIAS_MISCLASSIFICATION:
        action = TerminologyRemediationAction.CONVERT_TO_ALIAS
        exact_target = exact.target_term if exact is not None else None
        # A finding exists only when the authoritative target is absent from this target, so a
        # lossless duplicate can never reach this branch: any conversion changes the lookup.
        blocked = (
            "Converting this decision into an alias would change the resolved canonical target "
            f"from {flagged.target_term!r} to {exact_target!r}. The audit selects the "
            "authoritative side by equal-priority ordering rather than by governance authority, "
            "so neither collapsing this decision nor dropping the rival alias is provable here."
        )
        evidence.append("alias_conversion_changes_resolved_target")
        if exact is not None and exact.priority == flagged_priority(flagged):
            evidence.append("both_sides_share_equal_governance_priority")

    elif category is TerminologyRemediationCategory.CONFLICTING_TARGET:
        action = TerminologyRemediationAction.CHANGE_TARGET_REQUIRES_REVIEW
        blocked = (
            "Resolving this conflict requires composing a corrected Persian target, which is a "
            "translation act; no stored governance evidence selects a winner deterministically."
        )
        evidence.append("requires_new_target_text")
        if (
            not provenance["has_locked_user_authority"]
            and not provenance["has_legacy_glossary_authority"]
        ):
            evidence.append("all_involved_decisions_share_equal_ai_priority")
    elif category is TerminologyRemediationCategory.DUPLICATE_CANONICAL:
        action = TerminologyRemediationAction.MERGE_DECISIONS
        blocked = "Duplicate canonical merging is not implemented by this engine version."
        evidence.append("unsupported_category")
    else:
        blocked = "The audit could not classify this finding from static evidence."
        evidence.append("requires_context_review")

    operation_id = _stable_id(
        "rmo",
        {
            "finding_id": finding.finding_id,
            "action": action.value,
            "resolution": resolution.value,
            "affected_decision_ids": affected_decisions,
            "variant_removals": [item.model_dump(mode="json") for item in removals],
        },
    )
    return TerminologyRemediationOperation(
        operation_id=operation_id,
        sequence=1,
        finding_id=finding.finding_id,
        remediation_category=category,
        action=action,
        resolution=resolution,
        mutates_registry=mutates,
        decision_id=finding.decision_id,
        candidate_id=finding.candidate_id,
        source_term=finding.source_term,
        target_term=finding.target_term,
        affected_decision_ids=affected_decisions,
        affected_candidate_ids=affected_candidates,
        before_state_sha256=_before_state_hash(registry, affected_decisions, affected_candidates),
        variant_removals=removals,
        migration=None,
        reason=reason,
        provenance=provenance,
        blocked_reason=blocked,
        blocking_evidence=evidence,
    )


def flagged_priority(decision: TerminologyDecision) -> int:
    if decision.decision_source.value == "user":
        return 5 if decision.locked else 4
    return 3


def _migration_operation(
    registry: TerminologyRegistry, report: TerminologyRemediationReport
) -> TerminologyRemediationOperation | None:
    mismatch = report.bookkeeping_mismatch
    if mismatch is None:
        return None
    candidates = _candidates_by_id(registry)
    original = candidates.get(mismatch.original_candidate_id)
    normalized = candidates.get(mismatch.normalized_candidate_id)
    decisions = _decisions_by_candidate(registry)
    blocked: str | None = None
    evidence: list[str] = []
    if original is None or normalized is None:
        blocked = "Both candidate records must exist to migrate a normalized identity."
        evidence.append("missing_candidate_record")
    elif original.candidate_id in decisions:
        blocked = (
            "The historical candidate owns a canonical decision, so collapsing it would change "
            "canonical state rather than bookkeeping identity."
        )
        evidence.append("historical_candidate_owns_decision")

    migration: TerminologyCandidateMigration | None = None
    affected_candidates = sorted({mismatch.original_candidate_id, mismatch.normalized_candidate_id})
    affected_decisions = [mismatch.decision_id] if mismatch.decision_id else []
    if blocked is None and original is not None and normalized is not None:
        merged_aliases = sorted(set(original.aliases) - set(normalized.aliases))
        merged_notes = sorted(set(original.evidence_notes) - set(normalized.evidence_notes))
        migration = TerminologyCandidateMigration(
            original_candidate_id=original.candidate_id,
            original_source_term=original.source_term,
            normalized_candidate_id=normalized.candidate_id,
            normalized_source_term=normalized.source_term,
            merged_occurrence_count=len(original.occurrences),
            merged_alias_count=len(merged_aliases),
            merged_evidence_note_count=len(merged_notes),
            historical_run_ids=sorted(
                str(value)
                for key, value in mismatch.evidence.items()
                if key.endswith("run_id") and value is not None
            ),
            reason=mismatch.safest_migration_strategy,
        )

    operation_id = _stable_id(
        "rmo",
        {
            "finding_id": mismatch.finding_id,
            "action": TerminologyRemediationAction.IDENTITY_MIGRATION.value,
            "resolution": (
                TerminologyRemediationResolution.UNRESOLVED.value
                if blocked
                else TerminologyRemediationResolution.DETERMINISTIC.value
            ),
            "migration": migration.model_dump(mode="json") if migration else None,
        },
    )
    return TerminologyRemediationOperation(
        operation_id=operation_id,
        sequence=1,
        finding_id=mismatch.finding_id,
        remediation_category=TerminologyRemediationCategory.NORMALIZATION_IDENTITY_BUG,
        action=TerminologyRemediationAction.IDENTITY_MIGRATION,
        resolution=(
            TerminologyRemediationResolution.UNRESOLVED
            if blocked
            else TerminologyRemediationResolution.DETERMINISTIC
        ),
        mutates_registry=blocked is None,
        decision_id=mismatch.decision_id,
        candidate_id=mismatch.original_candidate_id,
        source_term=mismatch.original_source_term,
        target_term=None,
        affected_decision_ids=affected_decisions,
        affected_candidate_ids=affected_candidates,
        before_state_sha256=_before_state_hash(registry, affected_decisions, affected_candidates),
        variant_removals=[],
        migration=migration,
        reason=(
            "The whitespace-bearing historical candidate is superseded by its normalized "
            "identity; occurrences and aliases merge forward and immutable run history is kept."
        ),
        provenance={
            "original_source_sha256": _sha256_text(mismatch.original_source_term),
            "normalized_source_sha256": _sha256_text(mismatch.normalized_source_term),
            "both_candidate_records_exist": mismatch.both_candidate_records_exist,
            "immutable_references_resolve": mismatch.immutable_references_resolve,
        },
        blocked_reason=blocked,
        blocking_evidence=evidence,
    )


def plan_registry_remediation(
    registry: TerminologyRegistry,
    glossary: Any,
    report: TerminologyRemediationReport,
    *,
    registry_raw_sha256: str,
    glossary_sha256: str,
    registry_file: str = "config/terminology.yaml",
    review_run_id: str | None = None,
    proposal_run_id: str | None = None,
    analysis_run_id: str | None = None,
    created_at: datetime | None = None,
) -> TerminologyRemediationPlan:
    """Build the immutable remediation plan implied by one audited registry state."""
    semantic = registry_identity(registry)
    if report.registry_sha256 != semantic:
        raise TerminologyRemediationPlanError(
            "the remediation audit was produced against a different registry state"
        )
    view = canonical_view(registry, glossary)
    candidates = _candidates_by_id(registry)
    decisions = {item.decision_id: item for item in registry.decisions}
    sources = _source_decision_index(registry)
    operations = [
        _exactness_operation_for_finding(registry, finding, candidates, decisions, sources)
        if finding.finding_family is TerminologyFindingFamily.EXACT_ENFORCEMENT
        else _operation_for_finding(registry, finding, view, candidates, decisions, sources)
        for finding in sorted(report.findings, key=lambda item: item.finding_id)
    ]
    migration_operation = _migration_operation(registry, report)
    if migration_operation is not None:
        operations.append(migration_operation)

    ordered = sorted(operations, key=lambda item: (_ACTION_ORDER[item.action], item.finding_id))
    sequenced = [
        item.model_copy(update={"sequence": index}) for index, item in enumerate(ordered, start=1)
    ]
    unresolved = [
        item for item in sequenced if item.resolution is TerminologyRemediationResolution.UNRESOLVED
    ]
    mutating = [item for item in sequenced if item.mutates_registry]
    version_after = _bump_patch(registry.version) if mutating else registry.version
    expected = registry_identity(
        _apply_operations(registry, sequenced, target_version=version_after)
    )
    action_counts = Counter(item.action.value for item in sequenced)
    category_counts = Counter(item.remediation_category.value for item in sequenced)
    disposition_counts = Counter(
        item.exactness_disposition.value
        for item in sequenced
        if item.exactness_disposition is not None
    )
    retirements = [item for item in sequenced if item.retirement is not None]
    affected_decision_ids = sorted(
        {value for item in sequenced for value in item.affected_decision_ids}
    )
    affected_candidate_ids = sorted(
        {value for item in sequenced for value in item.affected_candidate_ids}
    )
    plan_id = _stable_id(
        "trp",
        {
            "engine_version": REMEDIATION_ENGINE_VERSION,
            "audit_id": report.audit_id,
            "source_registry_semantic_sha256": semantic,
            "source_registry_raw_sha256": registry_raw_sha256,
            "glossary_sha256": glossary_sha256,
            "review_run_id": review_run_id,
            "operation_ids": [item.operation_id for item in sequenced],
        },
    )
    return TerminologyRemediationPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        plan_id=plan_id,
        engine_version=REMEDIATION_ENGINE_VERSION,
        audit_id=report.audit_id,
        audit_schema_version=report.schema_version,
        review_run_id=review_run_id,
        proposal_run_id=proposal_run_id,
        analysis_run_id=analysis_run_id,
        registry_file=registry_file,
        registry_version_before=registry.version,
        registry_version_after=version_after,
        source_registry_semantic_sha256=semantic,
        source_registry_raw_sha256=registry_raw_sha256,
        glossary_sha256=glossary_sha256,
        decisions_before=len(registry.decisions),
        candidates_before=len(registry.candidates),
        operations=sequenced,
        operation_count=len(sequenced),
        deterministic_operation_count=len(sequenced) - len(unresolved),
        unresolved_operation_count=len(unresolved),
        mutating_operation_count=len(mutating),
        exactness_operation_count=sum(
            1
            for item in sequenced
            if item.finding_family is TerminologyFindingFamily.EXACT_ENFORCEMENT
        ),
        retirement_count=len(retirements),
        candidate_rejection_count=sum(
            1
            for item in retirements
            if item.retirement is not None and item.retirement.reject_candidate
        ),
        enforcement_change_count=sum(
            1 for item in sequenced if item.enforcement_change is not None
        ),
        target_correction_count=sum(1 for item in sequenced if item.target_correction is not None),
        question_request_count=sum(1 for item in sequenced if item.question_request is not None),
        action_counts=dict(sorted(action_counts.items())),
        category_counts=dict(sorted(category_counts.items())),
        disposition_counts=dict(sorted(disposition_counts.items())),
        affected_decision_ids=affected_decision_ids,
        affected_candidate_ids=affected_candidate_ids,
        expected_semantic_sha256=expected,
        applicable=not unresolved,
        created_at=created_at or datetime.now(UTC),
    )


def _apply_edit(
    candidates: dict[str, TerminologyCandidate],
    decisions: dict[str, TerminologyDecision],
    edit: RegistryEdit,
    registry: TerminologyRegistry,
) -> None:
    """Apply one typed semantic edit to the isolated registry maps."""
    candidate = candidates.get(edit.candidate_id)
    if candidate is None:
        raise TerminologyRemediationPlanError(
            f"semantic edit references a missing candidate: {edit.candidate_id}"
        )
    decision = next(
        (item for item in decisions.values() if item.candidate_id == edit.candidate_id), None
    )
    if edit.kind is RegistryEditKind.CREATE_DECISION:
        if decision is not None:
            raise TerminologyRemediationPlanError(
                f"cannot create a decision for {edit.source_term!r}; one already exists"
            )
        created = TerminologyDecision(
            decision_id=decision_identity(edit.candidate_id, registry.target_language),
            candidate_id=edit.candidate_id,
            target_term=edit.value,
            decision_source=DecisionSource.AI,
            locked=False,
            required=True,
        )
        decisions[created.decision_id] = created
        candidates[edit.candidate_id] = candidate.model_copy(
            update={"status": CandidateStatus.APPROVED}
        )
        return
    if decision is None:
        raise TerminologyRemediationPlanError(
            f"semantic edit references a missing decision for {edit.source_term!r}"
        )
    if decision.locked:
        raise TerminologyRemediationPlanError(
            f"a locked user decision must never be edited: {decision.decision_id}"
        )
    if edit.kind is RegistryEditKind.SET_TARGET:
        decisions[decision.decision_id] = decision.model_copy(
            update={
                "target_term": edit.value,
                "competing_variants": [
                    item for item in decision.competing_variants if item != edit.value
                ],
            }
        )
    elif edit.kind is RegistryEditKind.REMOVE_COMPETING_VARIANT:
        decisions[decision.decision_id] = decision.model_copy(
            update={
                "competing_variants": [
                    item for item in decision.competing_variants if item != edit.value
                ]
            }
        )
    elif edit.kind is RegistryEditKind.ADD_ALIAS:
        if edit.value != candidate.source_term and edit.value not in candidate.aliases:
            candidates[edit.candidate_id] = candidate.model_copy(
                update={"aliases": sorted([*candidate.aliases, edit.value])}
            )
    elif edit.kind is RegistryEditKind.REMOVE_ALIAS:
        candidates[edit.candidate_id] = candidate.model_copy(
            update={"aliases": [item for item in candidate.aliases if item != edit.value]}
        )


def _apply_retirement(
    candidates: dict[str, TerminologyCandidate],
    decisions: dict[str, TerminologyDecision],
    retirement: TerminologyDecisionRetirement,
) -> None:
    """Withdraw one canonical decision, rejecting its candidate in the same publication.

    Both halves happen against the same in-memory registry that is written once and atomically, so
    a candidate can never be left rejected while its decision survives, or decided while its
    candidate says the term is not terminology.
    """

    decision = decisions.get(retirement.decision_id)
    candidate = candidates.get(retirement.candidate_id)
    if decision is None or candidate is None:
        raise TerminologyRemediationPlanError(
            f"the decision this plan retires no longer exists: {retirement.decision_id}"
        )
    if decision.locked or decision.decision_source is DecisionSource.USER:
        raise TerminologyRemediationPlanError(
            f"a locked or user-owned decision must never be retired: {decision.decision_id}"
        )
    if _record_hash(decision) != retirement.decision_record_sha256 or (
        _record_hash(candidate) != retirement.candidate_record_sha256
    ):
        raise TerminologyRemediationPlanError(
            f"the record this plan retires drifted since the plan was created:"
            f" {retirement.decision_id}"
        )
    del decisions[retirement.decision_id]
    if not retirement.reject_candidate:
        return
    notes = list(candidate.evidence_notes)
    if retirement.rejection_reason and retirement.rejection_reason not in notes:
        notes.append(retirement.rejection_reason)
    candidates[candidate.candidate_id] = candidate.model_copy(
        update={"status": CandidateStatus.REJECTED, "evidence_notes": notes}
    )


def _apply_enforcement_change(
    decisions: dict[str, TerminologyDecision], change: TerminologyEnforcementChange
) -> None:
    decision = decisions.get(change.decision_id)
    if decision is None:
        raise TerminologyRemediationPlanError(
            f"the decision this plan re-enforces no longer exists: {change.decision_id}"
        )
    if decision.locked or decision.decision_source is DecisionSource.USER:
        raise TerminologyRemediationPlanError(
            f"a locked or user-owned decision must never be relaxed: {decision.decision_id}"
        )
    if _record_hash(decision) != change.decision_record_sha256:
        raise TerminologyRemediationPlanError(
            f"the record this plan re-enforces drifted since the plan was created:"
            f" {change.decision_id}"
        )
    decisions[decision.decision_id] = decision.model_copy(
        update={"enforcement": change.enforcement_after}
    )


def _apply_target_correction(
    decisions: dict[str, TerminologyDecision], correction: TerminologyTargetCorrection
) -> None:
    decision = decisions.get(correction.decision_id)
    if decision is None:
        raise TerminologyRemediationPlanError(
            f"the decision this plan respells no longer exists: {correction.decision_id}"
        )
    if decision.locked or decision.decision_source is DecisionSource.USER:
        raise TerminologyRemediationPlanError(
            f"a locked or user-owned decision must never be respelled: {decision.decision_id}"
        )
    if _record_hash(decision) != correction.decision_record_sha256:
        raise TerminologyRemediationPlanError(
            f"the record this plan respells drifted since the plan was created:"
            f" {correction.decision_id}"
        )
    decisions[decision.decision_id] = decision.model_copy(
        update={
            "target_term": correction.target_after,
            "competing_variants": [
                item for item in decision.competing_variants if item != correction.target_after
            ],
        }
    )


def _apply_operations(
    registry: TerminologyRegistry,
    operations: list[TerminologyRemediationOperation],
    *,
    target_version: str,
) -> TerminologyRegistry:
    """Apply only deterministic, mutating operations to an isolated registry value."""
    decisions = {item.decision_id: item for item in registry.decisions}
    candidates = {item.candidate_id: item for item in registry.candidates}
    dropped: set[str] = set()
    for operation in operations:
        if not operation.mutates_registry:
            continue
        if operation.target_correction is not None:
            _apply_target_correction(decisions, operation.target_correction)
        if operation.enforcement_change is not None:
            _apply_enforcement_change(decisions, operation.enforcement_change)
        if operation.retirement is not None:
            _apply_retirement(candidates, decisions, operation.retirement)
        for removal in operation.variant_removals:
            holder = decisions.get(removal.holder_decision_id)
            if holder is None:
                raise TerminologyRemediationPlanError(
                    f"remediation target decision is missing: {removal.holder_decision_id}"
                )
            remaining = [
                variant
                for variant in holder.competing_variants
                if variant != removal.removed_variant
            ]
            decisions[holder.decision_id] = holder.model_copy(
                update={"competing_variants": remaining}
            )
        for edit in operation.edits:
            _apply_edit(candidates, decisions, edit, registry)
        migration = operation.migration
        if migration is not None:
            original = candidates.get(migration.original_candidate_id)
            normalized = candidates.get(migration.normalized_candidate_id)
            if original is None or normalized is None:
                raise TerminologyRemediationPlanError(
                    "remediation migration references a missing candidate record"
                )
            merged_aliases = sorted(
                {*normalized.aliases, *original.aliases} - {normalized.source_term}
            )
            merged_notes = sorted({*normalized.evidence_notes, *original.evidence_notes})
            merged_occurrences = sorted(
                {
                    (item.chapter_id, item.chunk_id, item.location, item.count)
                    for item in (*normalized.occurrences, *original.occurrences)
                },
                key=lambda item: (item[0], item[1] or "", item[2], item[3]),
            )
            candidates[normalized.candidate_id] = normalized.model_copy(
                update={
                    "aliases": merged_aliases,
                    "evidence_notes": merged_notes,
                    "occurrences": [
                        TerminologyOccurrence(
                            chapter_id=chapter_id,
                            chunk_id=chunk_id,
                            location=location,
                            count=count,
                        )
                        for chapter_id, chunk_id, location, count in merged_occurrences
                    ],
                }
            )
            dropped.add(original.candidate_id)
    return registry.model_copy(
        update={
            "version": target_version,
            "candidates": [candidates[key] for key in sorted(candidates) if key not in dropped],
            "decisions": [decisions[key] for key in sorted(decisions)],
        }
    )


def apply_remediation_plan(
    registry: TerminologyRegistry, plan: TerminologyRemediationPlan
) -> TerminologyRegistry:
    """Apply a plan to an in-memory registry after verifying its exact source lineage."""
    if registry_identity(registry) != plan.source_registry_semantic_sha256:
        raise TerminologyRemediationPlanError(
            "the terminology registry drifted since the remediation plan was created"
        )
    if registry.version != plan.registry_version_before:
        raise TerminologyRemediationPlanError(
            "the terminology registry version drifted since the remediation plan was created"
        )
    return _apply_operations(
        registry, list(plan.operations), target_version=plan.registry_version_after
    )


def _registry_invariants(registry: TerminologyRegistry) -> list[str]:
    """Check the structural terminology invariants remediation must preserve."""
    violations: list[str] = []
    candidates = _candidates_by_id(registry)
    targets = {
        decision.target_term: candidates[decision.candidate_id].source_term
        for decision in registry.decisions
    }
    for decision in sorted(registry.decisions, key=lambda item: item.decision_id):
        for variant in decision.competing_variants:
            owner = targets.get(variant)
            if owner is not None and owner != candidates[decision.candidate_id].source_term:
                violations.append(
                    f"{decision.decision_id} stores canonical target of {owner!r} as a variant"
                )
    for decision in registry.decisions:
        if decision.candidate_id not in candidates:
            violations.append(f"{decision.decision_id} references a missing candidate")
    for candidate in registry.candidates:
        stripped = candidate.source_term.strip()
        if stripped != candidate.source_term:
            violations.append(f"{candidate.candidate_id} retains an unnormalized source term")
    return sorted(set(violations))


def _intended_source_forms(plan: TerminologyRemediationPlan) -> dict[str, str]:
    """Map every source form a mutating operation deliberately changes to that operation."""
    intended: dict[str, str] = {}
    for operation in plan.operations:
        if not operation.mutates_registry:
            continue
        for edit in operation.edits:
            intended.setdefault(edit.source_term, operation.operation_id)
            if edit.kind in {RegistryEditKind.ADD_ALIAS, RegistryEditKind.REMOVE_ALIAS}:
                # Changing an alias relation deliberately moves the alias form's own lookup.
                intended.setdefault(edit.value, operation.operation_id)
        for removal in operation.variant_removals:
            intended.setdefault(removal.holder_source_term, operation.operation_id)
            intended.setdefault(removal.canonical_owner_source_term, operation.operation_id)
        if operation.migration is not None:
            intended.setdefault(operation.migration.original_source_term, operation.operation_id)
            intended.setdefault(operation.migration.normalized_source_term, operation.operation_id)
        if operation.retirement is not None:
            # Withdrawing a decision withdraws every source form it resolved, its aliases
            # included, so each of those lookups is an intended correction rather than a loss.
            intended.setdefault(operation.retirement.source_term, operation.operation_id)
            for alias in operation.retirement.retired_aliases:
                intended.setdefault(alias, operation.operation_id)
        if operation.enforcement_change is not None:
            intended.setdefault(operation.enforcement_change.source_term, operation.operation_id)
        if operation.target_correction is not None:
            intended.setdefault(operation.target_correction.source_term, operation.operation_id)
    return intended


def _lookup_comparisons(
    before: TerminologyRegistry,
    after: TerminologyRegistry,
    glossary: Any,
    plan: TerminologyRemediationPlan,
) -> list[TerminologyLookupComparison]:
    before_view = canonical_view(before, glossary)
    after_view = canonical_view(after, glossary)
    intended_forms = _intended_source_forms(plan)
    forms: set[str] = {item.source_term for item in plan.operations}
    forms.update(intended_forms)
    forms.update(source for source, _ in PROTECTED_TERMS)

    comparisons: list[TerminologyLookupComparison] = []
    for form in sorted(forms):
        before_term = canonical_lookup(before_view, form)
        after_term = canonical_lookup(after_view, form)
        before_target = before_term.target_term if before_term else None
        after_target = after_term.target_term if after_term else None
        operation_id = intended_forms.get(form)
        if before_target == after_target:
            kind = TerminologyLookupChangeKind.EQUIVALENT_PRESERVED
            detail = "The canonical lookup is byte-identical before and after remediation."
        elif operation_id is not None:
            kind = TerminologyLookupChangeKind.INTENDED_CORRECTION
            detail = (
                f"Canonical lookup corrected from {before_target!r} to {after_target!r} by an "
                "evidence-backed remediation operation."
            )
        else:
            kind = TerminologyLookupChangeKind.UNINTENDED_REGRESSION
            detail = (
                f"Canonical lookup changed from {before_target!r} to {after_target!r} without a "
                "remediation operation that targets this source form."
            )
        comparisons.append(
            TerminologyLookupComparison(
                source_form=form,
                before_target=before_target,
                after_target=after_target,
                change_kind=kind,
                operation_id=operation_id,
                detail=detail,
            )
        )
    return comparisons


_REFERRING_ACTIONS = frozenset(
    {
        TerminologyRemediationAction.OPEN_TERMINOLOGY_QUESTION,
        TerminologyRemediationAction.NO_CHANGE_OWNER_AUTHORITY,
        TerminologyRemediationAction.MANUAL_CONTEXT_REVIEW,
        TerminologyRemediationAction.CHANGE_TARGET_REQUIRES_REVIEW,
        TerminologyRemediationAction.CONVERT_TO_ALIAS,
    }
)


def _blocking_accounting(
    plan: TerminologyRemediationPlan, after: TerminologyRemediationReport
) -> tuple[int, list[str]]:
    """Split the surviving blocking findings into those this plan referred to a human and the rest.

    Withdrawing a decision can uncover a contradiction that a now-retired component was masking.
    That is the audit becoming more honest, not the plan causing damage — but only if the plan
    already named that decision as one a human must settle.
    """

    referred = {
        item.decision_id
        for item in plan.operations
        if item.decision_id is not None
        and (
            item.action in _REFERRING_ACTIONS
            or item.resolution is TerminologyRemediationResolution.UNRESOLVED
        )
    }
    surviving = [item for item in after.findings if item.blocking]
    accounted = sorted({item.source_term for item in surviving if item.decision_id in referred})
    unaccounted = sum(1 for item in surviving if item.decision_id not in referred)
    return unaccounted, accounted


def simulate_remediation_plan(
    registry_path: Path,
    glossary: Any,
    plan: TerminologyRemediationPlan,
    *,
    review_run_id: str | None = None,
    project_root: Path | None = None,
    created_at: datetime | None = None,
) -> TerminologyRemediationSimulation:
    """Apply a plan to an isolated copy and prove the real registry is untouched."""
    raw_before = registry_path.read_bytes()
    registry = load_registry(registry_path)
    semantic_before = registry_identity(registry)
    if semantic_before != plan.source_registry_semantic_sha256:
        raise TerminologyRemediationPlanError(
            "the terminology registry drifted since the remediation plan was created"
        )
    if hashlib.sha256(raw_before).hexdigest() != plan.source_registry_raw_sha256:
        raise TerminologyRemediationPlanError(
            "the terminology registry file bytes drifted since the plan was created"
        )

    simulated = apply_remediation_plan(registry, plan)

    validation_errors: list[str] = []
    try:
        simulated = TerminologyRegistry.model_validate_json(canonical_registry_bytes(simulated))
    except ValidationError as exc:
        validation_errors = [str(exc)]

    invariants_before = _registry_invariants(registry)
    invariants = _registry_invariants(simulated)
    introduced = sorted(set(invariants) - set(invariants_before))
    after_report = audit_registry_remediation(
        simulated,
        glossary,
        review_run_id=review_run_id,
        project_root=project_root,
    )
    before_report = audit_registry_remediation(registry, glossary)
    comparisons = _lookup_comparisons(registry, simulated, glossary, plan)
    regressions = [
        item
        for item in comparisons
        if item.change_kind is TerminologyLookupChangeKind.UNINTENDED_REGRESSION
    ]
    before_view = canonical_view(registry, glossary)
    after_view = canonical_view(simulated, glossary)
    after_registry_language = simulated.target_language
    protected_before: dict[str, str | None] = {}
    protected_after: dict[str, str | None] = {}
    protected: dict[str, bool] = {}
    protected_correct: dict[str, bool] = {}
    for source, expected in PROTECTED_TERMS:
        before_term = canonical_lookup(before_view, source)
        after_term = canonical_lookup(after_view, source)
        protected_before[source] = before_term.target_term if before_term else None
        protected_after[source] = after_term.target_term if after_term else None
        protected[source] = protected_before[source] == protected_after[source]
        # A protected term may move, but only towards its governed authoritative target.
        # A term this registry never defines is out of scope rather than incorrect.
        if protected_before[source] is None and protected_after[source] is None:
            protected_correct[source] = True
        else:
            protected_correct[source] = protected_after[source] is not None and (
                canonical_targets_equal(
                    str(protected_after[source]), expected, after_registry_language
                )
            )
    locked_before = {
        item.decision_id: item.target_term for item in registry.decisions if item.locked
    }
    locked_after = {
        item.decision_id: item.target_term for item in simulated.decisions if item.locked
    }

    raw_after = registry_path.read_bytes()
    unchanged = raw_after == raw_before
    skipped = plan.unresolved_operation_count
    blockers: list[str] = []
    if skipped:
        blockers.append(
            f"{skipped} audited findings have no deterministic remediation and were not applied"
        )
    unaccounted, accounted = _blocking_accounting(plan, after_report)
    if unaccounted:
        blockers.append(f"{unaccounted} blocking registry findings remain unaccounted for")
    if after_report.bookkeeping_mismatch is not None:
        blockers.append("a normalized-identity mismatch remains")
    if regressions:
        blockers.append(f"{len(regressions)} unintended canonical lookup regressions")
    if validation_errors:
        blockers.append("the simulated registry failed contract validation")
    if introduced:
        blockers.append(f"{len(introduced)} terminology invariant violations were introduced")
    if not unchanged:
        blockers.append("the real registry file changed during the dry run")
    if locked_before != locked_after:
        blockers.append("a locked user decision changed")
    if not all(protected_correct.values()):
        blockers.append(
            f"{sum(1 for value in protected_correct.values() if not value)} protected "
            "authoritative lookups are incorrect"
        )

    return TerminologyRemediationSimulation(
        schema_version=PLAN_SCHEMA_VERSION,
        plan_id=plan.plan_id,
        engine_version=REMEDIATION_ENGINE_VERSION,
        source_registry_semantic_sha256=semantic_before,
        source_registry_raw_sha256=hashlib.sha256(raw_before).hexdigest(),
        simulated_registry_semantic_sha256=registry_identity(simulated),
        simulated_registry_raw_sha256=_sha256_text(_simulated_yaml(simulated)),
        real_registry_raw_sha256_after=hashlib.sha256(raw_after).hexdigest(),
        real_registry_unchanged=unchanged,
        decisions_before=len(registry.decisions),
        decisions_after=len(simulated.decisions),
        candidates_before=len(registry.candidates),
        candidates_after=len(simulated.candidates),
        applied_operation_count=plan.mutating_operation_count,
        skipped_unresolved_count=skipped,
        decisions_retired=plan.retirement_count,
        candidates_rejected=plan.candidate_rejection_count,
        enforcement_changes_applied=plan.enforcement_change_count,
        target_corrections_applied=plan.target_correction_count,
        question_requests_recorded=plan.question_request_count,
        exact_enforcement_findings_before=before_report.exactness_finding_count,
        exact_enforcement_findings_after=after_report.exactness_finding_count,
        retired_source_forms_resolved_after={
            item.retirement.source_term: (
                after_term.target_term
                if (after_term := canonical_lookup(after_view, item.retirement.source_term))
                else None
            )
            for item in plan.operations
            if item.retirement is not None
        },
        registry_valid_after=not validation_errors,
        registry_validation_errors=validation_errors,
        invariant_violations_before=len(invariants_before),
        invariant_violations=invariants,
        invariant_violations_introduced=introduced,
        audit_findings_before=before_report.findings_total,
        audit_findings_after=after_report.findings_total,
        blocking_findings_before=before_report.remaining_registry_blockers,
        blocking_findings_after=after_report.remaining_registry_blockers,
        unaccounted_blocking_findings_after=unaccounted,
        accounted_blocking_source_terms=accounted,
        category_counts_after=after_report.remediation_category_counts,
        identity_mismatches_after=after_report.bookkeeping_mismatch_count,
        lookup_comparisons=comparisons,
        lookup_regression_count=len(regressions),
        protected_terms_unchanged=protected,
        protected_terms_correct=protected_correct,
        protected_terms_before=protected_before,
        protected_terms_after=protected_after,
        locked_decisions_preserved=locked_before == locked_after,
        safe_to_apply=not blockers,
        blockers=blockers,
        created_at=created_at or datetime.now(UTC),
    )


def _simulated_yaml(registry: TerminologyRegistry) -> str:
    normalized = TerminologyRegistry.model_validate_json(canonical_registry_bytes(registry))
    rendered = yaml.safe_dump(
        normalized.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise TerminologyRemediationPlanError("could not serialize the simulated registry")
    return rendered


def execute_remediation_plan(
    registry_path: Path,
    plan: TerminologyRemediationPlan,
    simulation: TerminologyRemediationSimulation,
    *,
    evidence_directory: Path,
) -> TerminologyRegistry:
    """Atomically apply a proven-safe plan. Never called by the dry-run path."""
    if not plan.applicable:
        raise TerminologyRemediationPlanError(
            "a plan with unresolved operations must never be applied"
        )
    if not simulation.safe_to_apply or simulation.plan_id != plan.plan_id:
        raise TerminologyRemediationPlanError(
            "a plan may only be applied after its own dry run proved it safe"
        )
    raw = registry_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != plan.source_registry_raw_sha256:
        raise TerminologyRemediationPlanError(
            "the terminology registry drifted since the remediation plan was created"
        )
    registry = load_registry(registry_path)
    updated = apply_remediation_plan(registry, plan)
    if registry_identity(updated) != plan.expected_semantic_sha256:
        raise TerminologyRemediationPlanError(
            "the applied registry does not match the plan's expected semantic identity"
        )
    atomic_write_text(evidence_directory / "plan.json", serialize_plan(plan), overwrite=True)
    atomic_write_text(
        evidence_directory / "dry-run.json", serialize_simulation(simulation), overwrite=True
    )
    save_registry(registry_path, updated)
    return updated


def load_applied_remediations(project_root: Path) -> tuple[TerminologyRemediationPlan, ...]:
    """Load every remediation plan this project actually applied, oldest first.

    ``execute_remediation_plan`` is the only writer of a plan directory, and it publishes the plan
    beside the dry run that proved it safe only after the applied registry matched the plan's
    expected identity. Requiring both files is therefore what separates an applied plan from a plan
    that was merely written out for review.
    """
    directory = ProjectPaths(project_root).terminology_remediation_plans
    if not directory.is_dir():
        return ()
    applied: list[TerminologyRemediationPlan] = []
    for path in sorted(directory.glob("trp-*/plan.json")):
        if not (path.parent / "dry-run.json").is_file():
            continue
        try:
            plan = TerminologyRemediationPlan.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            raise TerminologyRemediationPlanError(
                f"invalid remediation plan at {path}: {exc}"
            ) from exc
        if plan.plan_id != path.parent.name:
            raise TerminologyRemediationPlanError(
                f"remediation plan {plan.plan_id} is published under {path.parent.name}"
            )
        applied.append(plan)
    return tuple(sorted(applied, key=lambda item: (item.created_at, item.plan_id)))


def serialize_plan(plan: TerminologyRemediationPlan) -> str:
    return (
        json.dumps(
            plan.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def serialize_simulation(simulation: TerminologyRemediationSimulation) -> str:
    return (
        json.dumps(
            simulation.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
