"""Governed provider-free carry-forward and re-evaluation of historically rejected outputs.

``reevaluate-translation-job`` re-runs current QA against an immutable attempt that already lives
inside the run being assessed, and ``reuse-accepted-translations`` carries an *accepted* output
into a new run. Neither covers the case here: a provider-successful output that its own run
rejected, whose destination job in a newer run has no attempt at all, and whose rejection may or
may not survive the QA rules the newer snapshot froze.

Engine 1.1.0 widens three things and narrows none. A historically *accepted* output whose rendered
request moved (so accepted-translation reuse must refuse it) may be re-assessed here too, under
its own provenance. A series-policy book is governed through published policy amendments and a
term-level drift classification rather than registry corrections. And current QA is the full
acceptance battery a fresh attempt faces — terminology, structural with scoped source anomalies,
and paragraph alignment — so a carried-forward acceptance means exactly what a worker acceptance
means. Its acceptance evidence is published inside the carry-forward record rather than in the
job's ``acceptance/NNN`` slot, which belongs to provider attempts: a later repair attempt 1 of this
job must never find, and adopt, a verdict about a different draft in its own slot.

The carry-forward is evidence, never a provider attempt. The destination job gains a
``carry-forward`` record and its own acceptance evidence with origin ``historical_reevaluation``;
the historical run, its attempts, its acceptance evidence, and every frozen resource are read-only
throughout. The historical rejection is copied into the new record rather than replaced by it, so
a later reader can still see what the output was rejected for and under which snapshot.
"""

import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from book_translator.historical_reevaluation_policy import (
    HISTORICAL_REEVALUATION_POLICY_VERSION,
    HistoricalCompatibility,
    TerminologyDriftFacts,
    evaluate_exemption_compatibility,
    evaluate_historical_compatibility,
    evaluate_series_term_drift,
)
from book_translator.models import (
    SCHEMA_VERSION,
    GovernedRegistryTransitionRecord,
    GovernedTerminologyDrift,
    HistoricalCompatibilityTier,
    HistoricalRequestCompatibility,
    PromptPackage,
    SeriesTerminologyPolicy,
    SourceAnomalyRegistry,
    TerminologyDecision,
    TerminologyRegistry,
    TerminologyValidationReport,
    TranslationAcceptanceEvidence,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationHistoricalReevaluationEvidence,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
    TranslationQAReevaluationEvidence,
    TranslationQAReport,
)
from book_translator.paragraph_alignment import ALIGNMENT_QA_VERSION
from book_translator.paths import ProjectPaths
from book_translator.prompt_packaging.rendering import framed_section_digests
from book_translator.prompt_packaging.workflow import counterfactual_user_message_sha256
from book_translator.provider_execution import validate_attempt
from book_translator.provider_execution.artifacts import (
    load_yaml,
    private_write,
    project_relative,
    serialize_yaml,
    sha256_bytes,
)
from book_translator.series_terminology.policy import SeriesTerminologyError, load_policy
from book_translator.series_terminology.workflow import (
    explain_policy_move,
    load_policy_amendments,
)
from book_translator.terminology import load_registry
from book_translator.terminology.exemption_workflow import frozen_occurrence_exemptions
from book_translator.terminology.lineage import (
    RegistryLineageError,
    explain_registry_drift,
    registry_transitions,
)
from book_translator.terminology.question_workflow import load_corrections
from book_translator.terminology.registry import (
    registry_identity,
    relevant_decisions,
)
from book_translator.terminology.remediation_engine import load_applied_remediations
from book_translator.terminology.validation import STRUCTURAL_QA_VERSION, TERMINOLOGY_QA_VERSION
from book_translator.translation_acceptance_qa import assess_translation
from book_translator.translation_queue.locking import queue_lock
from book_translator.translation_queue.workflow import (
    LOCK_TIMEOUT_SECONDS,
    QueueState,
    _append_event,
    _event,
    _load_queue,
    _persist_job_and_run,
    _resolve_run_path,
)
from book_translator.translation_snapshot import validate_snapshot

HISTORICAL_REEVALUATION_ENGINE_VERSION = "1.1.0"
SERIES_POLICY_AMENDMENT_EVENT = "series_policy_amendment"

ELIGIBLE = "eligible"
BLOCKED = "blocked"
ALREADY_APPLIED = "already_applied"


class HistoricalReevaluationError(ValueError):
    """Raised when a historical rejected output cannot be re-evaluated safely."""


@dataclass(frozen=True, slots=True)
class HistoricalSource:
    """The immutable historical facts a carry-forward is allowed to rest on."""

    attempt: TranslationAttempt
    attempt_path: Path
    acceptance: TranslationAcceptanceEvidence
    acceptance_path: Path
    reevaluation_path: Path | None
    reevaluation_sha256: str | None
    status: TranslationAcceptanceStatus
    terminology_report: TerminologyValidationReport
    structural_report: TranslationQAReport


@dataclass(frozen=True, slots=True)
class HistoricalReevaluationPlan:
    """A fully derived decision for one destination job; nothing is written until executed."""

    run_id: str
    job_id: str
    chunk_id: str
    source_run_id: str
    source_job_id: str | None
    source_attempt_number: int
    outcome: str
    blocking_reasons: tuple[str, ...] = ()
    tier: HistoricalCompatibilityTier = HistoricalCompatibilityTier.UNSAFE_DRIFT
    verified_identity_checks: tuple[str, ...] = ()
    changed_framed_sections: tuple[str, ...] = ()
    observed_resource_drift: tuple[str, ...] = ()
    governed_transition_ids: tuple[str, ...] = ()
    locked_decision_drift: tuple[str, ...] = ()
    historical_status: TranslationAcceptanceStatus | None = None
    historical_finding_codes: tuple[str, ...] = ()
    resolved_historical_findings: tuple[str, ...] = ()
    persisting_historical_findings: tuple[str, ...] = ()
    projected_status: TranslationAcceptanceStatus | None = None
    terminology_finding_codes: tuple[str, ...] = ()
    structural_finding_codes: tuple[str, ...] = ()
    evidence: TranslationHistoricalReevaluationEvidence | None = None
    existing_evidence_file: Path | None = None

    @property
    def eligible(self) -> bool:
        return self.outcome == ELIGIBLE


@dataclass(frozen=True, slots=True)
class HistoricalReevaluationResult:
    plan: HistoricalReevaluationPlan
    published: int
    resulting_status: TranslationAcceptanceStatus | None
    evidence_file: Path | None
    acceptance_file: Path | None
    reused: bool
    dry_run: bool

    @property
    def provider_calls(self) -> int:
        """This workflow has no provider code path at all; the count is structural, not observed."""
        return 0


def _job(state: QueueState, job_id: str) -> TranslationJob:
    selected = next(
        (item for item in state.jobs if job_id in {item.job_id, item.chunk_id}),
        None,
    )
    if selected is None:
        raise HistoricalReevaluationError(f"translation job not found: {job_id}")
    return selected


def _replace(
    jobs: tuple[TranslationJob, ...], changed: TranslationJob
) -> tuple[TranslationJob, ...]:
    return tuple(changed if job.job_id == changed.job_id else job for job in jobs)


def _package(state: QueueState, job: TranslationJob) -> PromptPackage:
    return load_yaml(
        state.path / "jobs" / job.chunk_id / "prompt-package/package.yaml", PromptPackage
    )


def _verified_bytes(path: Path, expected_sha256: str, label: str) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise HistoricalReevaluationError(f"{label} is unreadable: {path}") from exc
    if sha256_bytes(content) != expected_sha256:
        raise HistoricalReevaluationError(f"{label} no longer matches its recorded hash: {path}")
    return content


def _intact(path: Path, expected_sha256: str) -> bool:
    return path.is_file() and sha256_bytes(path.read_bytes()) == expected_sha256


def _rendered_request_intact(package: PromptPackage, project_root: Path) -> bool:
    """Prove the recorded message digests still describe present, unchanged rendered bytes."""
    return all(
        _intact(project_root / reference.file, reference.sha256)
        for reference in (package.system_message, package.user_message)
    )


def _sections(package: PromptPackage, project_root: Path) -> Mapping[str, str]:
    return framed_section_digests(
        (project_root / package.user_message.file).read_text(encoding="utf-8")
    )


def _finding_keys(
    terminology: TerminologyValidationReport, structural: TranslationQAReport
) -> tuple[str, ...]:
    """Identify blocking findings by code and opaque candidate ID, never by source prose."""
    keys = [
        f"{finding.code}:{finding.candidate_id or 'unattributed'}"
        for finding in terminology.findings
        if finding.severity == "error"
    ]
    keys.extend(finding.code for finding in structural.findings if finding.severity == "error")
    return tuple(sorted(set(keys)))


def _historical_source(
    state: QueueState, job: TranslationJob, attempt_number: int, project_root: Path, now: datetime
) -> HistoricalSource | tuple[str, ...]:
    """Resolve the historical attempt and its effective verdict, or the reasons it cannot be."""
    validation = validate_attempt(
        state.path,
        job.job_id,
        attempt_number,
        project_root=project_root,
        now=lambda: now,
        snapshot_validation=state.snapshot_validation,
    )
    if not validation.valid:
        return ("historical_attempt_invalid",)
    attempt_path = (
        state.path / "jobs" / job.chunk_id / "attempts" / f"{attempt_number:03d}" / "attempt.yaml"
    )
    attempt = load_yaml(attempt_path, TranslationAttempt)
    if attempt.status is TranslationAttemptStatus.AMBIGUOUS or attempt.ambiguous:
        return ("historical_attempt_ambiguous",)
    if (
        attempt.status is not TranslationAttemptStatus.SUCCEEDED
        or attempt.translation_file is None
        or attempt.translation_sha256 is None
    ):
        return ("historical_attempt_not_successful",)
    if (
        job.acceptance_status is None
        or job.acceptance_file is None
        or job.acceptance_sha256 is None
    ):
        return ("historical_acceptance_lineage_missing",)
    if job.accepted_attempt != attempt_number:
        return ("historical_attempt_not_effective",)
    acceptance_path = project_root / job.acceptance_file
    if not _intact(acceptance_path, job.acceptance_sha256):
        return ("historical_acceptance_invalid",)
    acceptance = load_yaml(acceptance_path, TranslationAcceptanceEvidence)
    if (
        acceptance.job_id != job.job_id
        or acceptance.run_id != job.run_id
        or acceptance.attempt_number != attempt_number
        or acceptance.translation_sha256 != attempt.translation_sha256
    ):
        return ("historical_acceptance_lineage_invalid",)
    origin = job.acceptance_origin or TranslationAcceptanceOrigin.INITIAL_QA
    status = acceptance.status
    terminology_report = acceptance.terminology_report
    structural_report = acceptance.structural_report
    reevaluation_path: Path | None = None
    reevaluation_sha256: str | None = None
    if origin is TranslationAcceptanceOrigin.QA_REEVALUATION:
        if job.latest_reevaluation_file is None or job.latest_reevaluation_sha256 is None:
            return ("historical_reevaluation_missing",)
        reevaluation_path = project_root / job.latest_reevaluation_file
        if not _intact(reevaluation_path, job.latest_reevaluation_sha256):
            return ("historical_reevaluation_invalid",)
        reevaluation = load_yaml(reevaluation_path, TranslationQAReevaluationEvidence)
        if (
            reevaluation.attempt_number != attempt_number
            or reevaluation.translation_sha256 != attempt.translation_sha256
            or reevaluation.resulting_status is not job.acceptance_status
        ):
            return ("historical_reevaluation_lineage_invalid",)
        reevaluation_sha256 = job.latest_reevaluation_sha256
        status = reevaluation.resulting_status
        terminology_report = reevaluation.terminology_report
        structural_report = reevaluation.structural_report
    if status is not job.acceptance_status:
        return ("historical_effective_status_inconsistent",)
    if not _intact(project_root / attempt.translation_file, attempt.translation_sha256):
        return ("historical_translation_tampered",)
    return HistoricalSource(
        attempt=attempt,
        attempt_path=attempt_path,
        acceptance=acceptance,
        acceptance_path=acceptance_path,
        reevaluation_path=reevaluation_path,
        reevaluation_sha256=reevaluation_sha256,
        status=status,
        terminology_report=terminology_report,
        structural_report=structural_report,
    )


def _destination_reasons(job: TranslationJob) -> tuple[str, ...]:
    reasons: list[str] = []
    if job.status is not TranslationJobStatus.PENDING:
        reasons.append("destination_not_pending")
    if job.attempt_count:
        reasons.append("destination_already_attempted")
    if job.acceptance_status is not None:
        reasons.append("destination_already_assessed")
    if job.reuse_count:
        reasons.append("destination_already_reused")
    return tuple(reasons)


def _user_owned(decision: TerminologyDecision | None) -> bool:
    return decision is not None and (decision.locked or decision.decision_source.value == "user")


def _locked_decision_drift(
    old: TerminologyRegistry, new: TerminologyRegistry, source_text: str, context_text: str
) -> tuple[str, ...]:
    """Name every locked or user-owned canonical decision reaching this job that moved.

    Every decision the rendered projection or the QA matcher selects is a boundary-safe match of
    a source term or alias in the source or context, so ``relevant_decisions`` covers both; a
    locked decision that only one of them saw can therefore never be waived by the other.
    """

    def selected(registry: TerminologyRegistry) -> dict[str, TerminologyDecision]:
        pairs = relevant_decisions(registry, source_text, context_text)
        return {candidate.candidate_id: decision for candidate, decision in pairs}

    before, after = selected(old), selected(new)
    return tuple(
        sorted(
            candidate_id
            for candidate_id in set(before) | set(after)
            if (before.get(candidate_id) != after.get(candidate_id))
            and (_user_owned(before.get(candidate_id)) or _user_owned(after.get(candidate_id)))
        )
    )


def _terminology_drift(
    project_root: Path,
    old_registry: TerminologyRegistry,
    new_registry: TerminologyRegistry,
    *,
    source_text: str,
    context_text: str,
) -> TerminologyDriftFacts:
    """Explain the move between two frozen registries from the project's governed records."""
    before, after = registry_identity(old_registry), registry_identity(new_registry)
    try:
        transitions = registry_transitions(
            corrections=load_corrections(project_root),
            remediations=load_applied_remediations(project_root),
        )
    except (RegistryLineageError, ValueError, OSError):
        return TerminologyDriftFacts(before, after, governed=False)
    drift = explain_registry_drift(
        transitions, from_semantic_sha256=before, to_semantic_sha256=after
    )
    return TerminologyDriftFacts(
        from_semantic_sha256=before,
        to_semantic_sha256=after,
        governed=drift.explained,
        transition_ids=tuple(item.event_id for item in drift.path),
        locked_decision_drift=_locked_decision_drift(
            old_registry, new_registry, source_text, context_text
        ),
    )


def _frozen_series_policy(
    manifest: TranslationManifest, project_root: Path
) -> SeriesTerminologyPolicy | None:
    reference = manifest.series_terminology
    if reference is None:
        return None
    path = project_root / (reference.frozen_file or reference.file)
    if not _intact(path, reference.sha256):
        return None
    try:
        return load_policy(path)
    except SeriesTerminologyError:
        return None


def _decision_spellings(
    registry: TerminologyRegistry, source_text: str, context_text: str
) -> dict[str, tuple[frozenset[str], TerminologyDecision]]:
    """Every decision reaching the job, keyed by candidate, with the spellings that name it."""
    candidates = {item.candidate_id: item for item in registry.candidates}
    pairs = relevant_decisions(registry, source_text, context_text)
    return {
        candidate.candidate_id: (
            frozenset((candidates[candidate.candidate_id].source_term, *candidate.aliases)),
            decision,
        )
        for candidate, decision in pairs
    }


def _series_terminology_drift(
    project_root: Path,
    old_manifest: TranslationManifest,
    new_manifest: TranslationManifest,
    old_registry: TerminologyRegistry,
    new_registry: TerminologyRegistry,
    *,
    source_text: str,
    context_text: str,
) -> TerminologyDriftFacts:
    """Explain a series-policy move: amendment chain, then every reaching term, then the registry.

    The registry is a projection of the policy, so a registry decision reaching the job may differ
    only where the spelling that selects it belongs to a term the chain changed; a difference the
    policy move cannot account for is reported rather than waived.
    """
    assert old_manifest.series_terminology is not None
    assert new_manifest.series_terminology is not None
    before = old_manifest.series_terminology.sha256
    after = new_manifest.series_terminology.sha256
    old_policy = _frozen_series_policy(old_manifest, project_root)
    new_policy = _frozen_series_policy(new_manifest, project_root)
    if old_policy is None or new_policy is None or old_policy.series_id != new_policy.series_id:
        return TerminologyDriftFacts(before, after, governed=False, lineage_kind="series_policy")
    try:
        chain = explain_policy_move(
            load_policy_amendments(project_root),
            from_sha256=before,
            to_sha256=after,
            series_id=new_policy.series_id,
        )
    except (SeriesTerminologyError, OSError):
        chain = None
    terms = evaluate_series_term_drift(
        old_policy, new_policy, source_text=source_text, context_text=context_text
    )
    old_terms = {term.concept_id: term for term in old_policy.terms}
    new_terms = {term.concept_id: term for term in new_policy.terms}
    changed_spellings = {
        spelling
        for concept in set(old_terms) | set(new_terms)
        if old_terms.get(concept) != new_terms.get(concept)
        for term in (old_terms.get(concept), new_terms.get(concept))
        if term is not None
        for spelling in (term.source_term, *term.aliases)
    }
    old_decisions = _decision_spellings(old_registry, source_text, context_text)
    new_decisions = _decision_spellings(new_registry, source_text, context_text)
    unexplained = tuple(
        f"ungoverned_registry_drift:{candidate_id}"
        for candidate_id in sorted(set(old_decisions) | set(new_decisions))
        if old_decisions.get(candidate_id) != new_decisions.get(candidate_id)
        and not any(
            entry[0] & changed_spellings
            for entry in (old_decisions.get(candidate_id), new_decisions.get(candidate_id))
            if entry is not None
        )
    )
    return TerminologyDriftFacts(
        from_semantic_sha256=before,
        to_semantic_sha256=after,
        governed=chain is not None,
        transition_ids=tuple(record.amendment_id for record in chain or ()),
        locked_decision_drift=(*terms.blocking_reasons, *unexplained),
        lineage_kind="series_policy",
        waived_term_changes=terms.waived_concepts,
    )


def _governed_drift_record(
    project_root: Path, facts: TerminologyDriftFacts
) -> GovernedTerminologyDrift:
    if facts.lineage_kind == "series_policy":
        amendments = {
            record.amendment_id: record for record in load_policy_amendments(project_root)
        }
        return GovernedTerminologyDrift(
            from_semantic_sha256=facts.from_semantic_sha256,
            to_semantic_sha256=facts.to_semantic_sha256,
            explained=facts.governed,
            transitions=[
                GovernedRegistryTransitionRecord(
                    event_id=amendment_id,
                    event_kind=SERIES_POLICY_AMENDMENT_EVENT,
                    semantic_sha256_before=amendments[amendment_id].policy_sha256_before,
                    semantic_sha256_after=amendments[amendment_id].policy_sha256_after,
                )
                for amendment_id in facts.transition_ids
            ],
            locked_decision_drift=list(facts.locked_decision_drift),
            lineage_kind="series_policy",
            waived_term_changes=list(facts.waived_term_changes),
        )
    transitions = registry_transitions(
        corrections=load_corrections(project_root),
        remediations=load_applied_remediations(project_root),
    )
    by_id = {item.event_id: item for item in transitions}
    return GovernedTerminologyDrift(
        from_semantic_sha256=facts.from_semantic_sha256,
        to_semantic_sha256=facts.to_semantic_sha256,
        explained=facts.governed,
        transitions=[
            GovernedRegistryTransitionRecord(
                event_id=by_id[event_id].event_id,
                event_kind=by_id[event_id].event_kind.value,
                semantic_sha256_before=by_id[event_id].semantic_sha256_before,
                semantic_sha256_after=by_id[event_id].semantic_sha256_after,
            )
            for event_id in facts.transition_ids
            if event_id in by_id
        ],
        locked_decision_drift=list(facts.locked_decision_drift),
    )


def _compatibility_record(
    verdict: HistoricalCompatibility,
    old_package: PromptPackage,
    new_package: PromptPackage,
    historical_output_contract: str,
    drift: GovernedTerminologyDrift | None,
    old_manifest: TranslationManifest,
    new_manifest: TranslationManifest,
) -> HistoricalRequestCompatibility:
    return HistoricalRequestCompatibility(
        policy_version=HISTORICAL_REEVALUATION_POLICY_VERSION,
        tier=verdict.tier,
        source_snapshot_id=old_package.snapshot_id,
        target_snapshot_id=new_package.snapshot_id,
        source_prompt_package_id=old_package.package_id,
        target_prompt_package_id=new_package.package_id,
        source_system_message_sha256=old_package.system_message.sha256,
        target_system_message_sha256=new_package.system_message.sha256,
        source_user_message_sha256=old_package.user_message.sha256,
        target_user_message_sha256=new_package.user_message.sha256,
        source_output_contract=historical_output_contract,
        target_output_contract=new_package.output_contract_version,
        source_terminology_sha256=(
            old_package.terminology.sha256 if old_package.terminology is not None else None
        ),
        target_terminology_sha256=(
            new_package.terminology.sha256 if new_package.terminology is not None else None
        ),
        source_manifest_sha256=old_package.manifest_sha256,
        target_manifest_sha256=new_package.manifest_sha256,
        source_series_policy_sha256=(
            old_manifest.series_terminology.sha256
            if old_manifest.series_terminology is not None
            else None
        ),
        target_series_policy_sha256=(
            new_manifest.series_terminology.sha256
            if new_manifest.series_terminology is not None
            else None
        ),
        verified_identity_checks=list(verdict.verified_identity_checks),
        changed_framed_sections=list(verdict.changed_framed_sections),
        observed_resource_drift=list(verdict.observed_resource_drift),
        terminology_drift=drift,
        rationale=verdict.rationale,
        occurrence_exemption_ids=list(verdict.occurrence_exemption_ids),
        counterfactual_user_message_sha256=verdict.counterfactual_user_message_sha256,
    )


def _blocked(
    new_job: TranslationJob,
    source_run_id: str,
    source_job_id: str | None,
    attempt_number: int,
    reasons: tuple[str, ...],
    **extra: object,
) -> HistoricalReevaluationPlan:
    return HistoricalReevaluationPlan(
        run_id=new_job.run_id,
        job_id=new_job.job_id,
        chunk_id=new_job.chunk_id,
        source_run_id=source_run_id,
        source_job_id=source_job_id,
        source_attempt_number=attempt_number,
        outcome=BLOCKED,
        blocking_reasons=reasons,
        **extra,  # type: ignore[arg-type]
    )


def _manifest(state: QueueState, project_root: Path) -> TranslationManifest:
    return load_yaml(project_root / state.run.snapshot_path / "manifest.yaml", TranslationManifest)


def _frozen_terminology(manifest: TranslationManifest, project_root: Path) -> Path | None:
    reference = manifest.terminology
    if reference is None:
        return None
    path = project_root / (reference.frozen_file or reference.file)
    return path if _intact(path, reference.sha256) else None


def plan_historical_output_reevaluation(
    run_id_or_path: str | Path,
    *,
    from_run: str | Path,
    job_id: str,
    attempt_number: int,
    project_root: Path,
    now: Callable[[], datetime] | None = None,
    new_state: QueueState | None = None,
    old_state: QueueState | None = None,
) -> HistoricalReevaluationPlan:
    """Derive, without writing anything, whether one historical output may be re-evaluated.

    ``new_state`` and ``old_state`` are queue loads a batch caller already holds; their snapshot
    validations are reused, and the destination is still re-read before any publication.
    """
    if attempt_number < 1:
        raise HistoricalReevaluationError("attempt number must be positive")
    project_root = project_root.resolve()
    timestamp = (now or (lambda: datetime.now(UTC)))()
    if new_state is None:
        new_state = _load_queue(_resolve_run_path(run_id_or_path, project_root), project_root)
    if old_state is None:
        old_state = _load_queue(_resolve_run_path(from_run, project_root), project_root)
    new_job = _job(new_state, job_id)
    old_run_id = old_state.run.run_id
    old_job = next(
        (item for item in old_state.jobs if item.chunk_id == new_job.chunk_id),
        None,
    )
    if old_job is None:
        return _blocked(new_job, old_run_id, None, attempt_number, ("historical_job_missing",))

    applied = _applied_plan(new_job, old_job, old_run_id, attempt_number, project_root)
    if applied is not None:
        return applied
    destination_reasons = _destination_reasons(new_job)
    if destination_reasons:
        return _blocked(new_job, old_run_id, old_job.job_id, attempt_number, destination_reasons)

    historical = _historical_source(old_state, old_job, attempt_number, project_root, timestamp)
    if isinstance(historical, tuple):
        return _blocked(new_job, old_run_id, old_job.job_id, attempt_number, historical)

    reasons: tuple[str, ...] = ()
    new_manifest = _manifest(new_state, project_root)
    old_manifest = _manifest(old_state, project_root)
    if old_manifest.workflow != new_manifest.workflow:
        reasons += ("snapshot_workflow_mismatch",)
    snapshot = new_state.snapshot_validation or validate_snapshot(
        project_root / new_state.run.snapshot_path, project_root=project_root, now=lambda: timestamp
    )
    if (
        not snapshot.report.valid
        or snapshot.snapshot is None
        or snapshot.snapshot.snapshot_id != new_job.snapshot_id
        or snapshot.snapshot.manifest_sha256 != new_job.manifest_sha256
    ):
        reasons += ("destination_snapshot_invalid",)
    new_terminology = _frozen_terminology(new_manifest, project_root)
    old_terminology = _frozen_terminology(old_manifest, project_root)
    if new_terminology is None:
        reasons += ("frozen_terminology_unavailable",)
    if old_terminology is None:
        reasons += ("historical_terminology_unavailable",)
    old_package, new_package = _package(old_state, old_job), _package(new_state, new_job)
    if not _rendered_request_intact(old_package, project_root):
        reasons += ("historical_rendered_request_unavailable",)
    if not _rendered_request_intact(new_package, project_root):
        reasons += ("rendered_request_unavailable",)
    if reasons:
        return _blocked(new_job, old_run_id, old_job.job_id, attempt_number, reasons)
    assert new_terminology is not None and old_terminology is not None

    source_bytes = _verified_bytes(
        project_root / new_job.chunk_source_file, new_job.chunk_source_sha256, "frozen chunk source"
    )
    context_bytes = _verified_bytes(
        project_root / new_job.context_file, new_job.context_sha256, "frozen chunk context"
    )
    translation_bytes = _verified_bytes(
        project_root / historical.attempt.translation_file,  # type: ignore[operator]
        historical.attempt.translation_sha256,  # type: ignore[arg-type]
        "historical translation",
    )
    try:
        source_text = source_bytes.decode("utf-8")
        context_text = context_bytes.decode("utf-8")
        translation_text = translation_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise HistoricalReevaluationError("QA inputs are not valid UTF-8") from exc

    old_registry = load_registry(old_terminology)
    new_registry = load_registry(new_terminology)
    series_lineage = (
        old_manifest.series_terminology is not None and new_manifest.series_terminology is not None
    )
    if (old_manifest.series_terminology is None) != (new_manifest.series_terminology is None):
        return _blocked(
            new_job,
            old_run_id,
            old_job.job_id,
            attempt_number,
            ("terminology_lineage_kind_changed",),
        )
    drift = (
        _series_terminology_drift(
            project_root,
            old_manifest,
            new_manifest,
            old_registry,
            new_registry,
            source_text=source_text,
            context_text=context_text,
        )
        if series_lineage
        else _terminology_drift(
            project_root,
            old_registry,
            new_registry,
            source_text=source_text,
            context_text=context_text,
        )
    )
    exemptions = frozen_occurrence_exemptions(
        new_manifest,
        project_root,
        registry=new_registry,
        chunk_id=new_job.chunk_id,
        source_text=source_text,
    )
    if (
        exemptions is not None
        and old_package.user_message.sha256 != new_package.user_message.sha256
    ):
        # Exemptions reach this job and its request moved. The only explanation admitted is the
        # exemptions themselves, proven by rendering this run's request without them.
        verdict = evaluate_exemption_compatibility(
            old_package,
            new_package,
            old_sections=_sections(old_package, project_root),
            new_sections=_sections(new_package, project_root),
            historical_output_contract=historical.attempt.output_contract,
            counterfactual_user_message_sha256=counterfactual_user_message_sha256(
                new_state, new_job, project_root=project_root
            ),
            exemption_ids=exemptions.exemption_ids,
        )
    else:
        verdict = evaluate_historical_compatibility(
            old_package,
            new_package,
            old_sections=_sections(old_package, project_root),
            new_sections=_sections(new_package, project_root),
            historical_output_contract=historical.attempt.output_contract,
            terminology_drift=drift,
        )
    historical_keys = _finding_keys(historical.terminology_report, historical.structural_report)
    common = {
        "tier": verdict.tier,
        "verified_identity_checks": verdict.verified_identity_checks,
        "changed_framed_sections": verdict.changed_framed_sections,
        "observed_resource_drift": verdict.observed_resource_drift,
        "governed_transition_ids": drift.transition_ids,
        "locked_decision_drift": drift.locked_decision_drift,
        "historical_status": historical.status,
        "historical_finding_codes": historical_keys,
    }
    if not verdict.eligible:
        return _blocked(
            new_job,
            old_run_id,
            old_job.job_id,
            attempt_number,
            verdict.blocking_reasons,
            **common,
        )
    if (
        historical.status is TranslationAcceptanceStatus.ACCEPTED
        and verdict.tier is HistoricalCompatibilityTier.EXACT_RENDERED_REQUEST
    ):
        # The identical request proves more than this workflow does; accepted-translation reuse
        # is the boundary for it, and filing it here would record the weaker provenance.
        return _blocked(
            new_job,
            old_run_id,
            old_job.job_id,
            attempt_number,
            ("historical_output_reusable_exactly",),
            **common,
        )

    anomaly_registry = None
    if new_manifest.source_anomalies is not None:
        anomaly_path = project_root / (
            new_manifest.source_anomalies.frozen_file or new_manifest.source_anomalies.file
        )
        _verified_bytes(
            anomaly_path, new_manifest.source_anomalies.sha256, "frozen source anomalies"
        )
        anomaly_registry = load_yaml(anomaly_path, SourceAnomalyRegistry)
    assessment = assess_translation(
        source_text,
        translation_text,
        registry=new_registry,
        target_language=new_job.target_language,
        output_contract=historical.attempt.output_contract,
        anomaly_registry=anomaly_registry,
        chapter_id=new_job.chapter_id,
        occurrence_exemptions=exemptions,
    )
    terminology_report = assessment.terminology_report
    structural_report = assessment.structural_report
    alignment_report = assessment.alignment_report
    accepted = assessment.accepted
    status = (
        TranslationAcceptanceStatus.ACCEPTED
        if accepted
        else TranslationAcceptanceStatus.QA_REJECTED
    )
    current_keys = _finding_keys(terminology_report, structural_report) + tuple(
        sorted({item.code for item in alignment_report.findings if item.severity == "error"})
    )
    destination = ProjectPaths(project_root).translation_carry_forward(
        new_job.run_id, new_job.chunk_id, 1
    )
    evidence = TranslationHistoricalReevaluationEvidence(
        schema_version=SCHEMA_VERSION,
        carry_forward_engine_version=HISTORICAL_REEVALUATION_ENGINE_VERSION,
        run_id=new_job.run_id,
        job_id=new_job.job_id,
        carry_forward_number=1,
        source_run_id=old_run_id,
        source_job_id=old_job.job_id,
        source_attempt_id=historical.attempt.attempt_id,
        source_attempt_number=attempt_number,
        source_attempt_file=project_relative(historical.attempt_path, project_root),
        source_attempt_sha256=sha256_bytes(historical.attempt_path.read_bytes()),
        source_provider=historical.attempt.provider.value,
        source_provider_adapter_version=historical.attempt.provider_adapter_version,
        source_provider_response_id=historical.attempt.provider_response_id,
        source_model_returned=historical.attempt.model_returned,
        source_translation_file=historical.attempt.translation_file,  # type: ignore[arg-type]
        source_translation_sha256=historical.attempt.translation_sha256,  # type: ignore[arg-type]
        historical_acceptance_file=project_relative(historical.acceptance_path, project_root),
        historical_acceptance_sha256=sha256_bytes(historical.acceptance_path.read_bytes()),
        historical_reevaluation_file=(
            project_relative(historical.reevaluation_path, project_root)
            if historical.reevaluation_path is not None
            else None
        ),
        historical_reevaluation_sha256=historical.reevaluation_sha256,
        historical_status=historical.status,
        historical_terminology_report=historical.terminology_report,
        historical_structural_report=historical.structural_report,
        compatibility=_compatibility_record(
            verdict,
            old_package,
            new_package,
            historical.attempt.output_contract,
            (
                _governed_drift_record(project_root, drift)
                if verdict.tier is HistoricalCompatibilityTier.GOVERNED_RESOURCE_DRIFT
                else None
            ),
            old_manifest,
            new_manifest,
        ),
        source_file=new_job.chunk_source_file,
        source_sha256=new_job.chunk_source_sha256,
        context_file=new_job.context_file,
        context_sha256=new_job.context_sha256,
        terminology_file=project_relative(new_terminology, project_root),
        terminology_sha256=sha256_bytes(new_terminology.read_bytes()),
        prompt_file=new_package.prompt.file,
        prompt_sha256=new_package.prompt.sha256,
        style_file=new_package.style_guide.file,
        style_sha256=new_package.style_guide.sha256,
        terminology_qa_version=TERMINOLOGY_QA_VERSION,
        structural_qa_version=STRUCTURAL_QA_VERSION,
        terminology_report=terminology_report,
        structural_report=structural_report,
        alignment_qa_version=ALIGNMENT_QA_VERSION,
        alignment_report=alignment_report,
        resulting_status=status,
        accepted=accepted,
        translation_file=project_relative(destination / "translation.md", project_root),
        translation_sha256=historical.attempt.translation_sha256,  # type: ignore[arg-type]
        created_at=timestamp,
    )
    return HistoricalReevaluationPlan(
        run_id=new_job.run_id,
        job_id=new_job.job_id,
        chunk_id=new_job.chunk_id,
        source_run_id=old_run_id,
        source_job_id=old_job.job_id,
        source_attempt_number=attempt_number,
        outcome=ELIGIBLE,
        resolved_historical_findings=tuple(
            key for key in historical_keys if key not in current_keys
        ),
        persisting_historical_findings=tuple(key for key in historical_keys if key in current_keys),
        projected_status=status,
        terminology_finding_codes=tuple(item.code for item in terminology_report.findings),
        structural_finding_codes=tuple(item.code for item in structural_report.findings),
        evidence=evidence,
        **common,  # type: ignore[arg-type]
    )


def _applied_plan(
    new_job: TranslationJob,
    old_job: TranslationJob,
    source_run_id: str,
    attempt_number: int,
    project_root: Path,
) -> HistoricalReevaluationPlan | None:
    """Recognize an identical carry-forward that was already published for this job."""
    if not new_job.carry_forward_count:
        return None
    if new_job.latest_carry_forward_file is None or new_job.latest_carry_forward_sha256 is None:
        return None
    path = project_root / new_job.latest_carry_forward_file
    if not _intact(path, new_job.latest_carry_forward_sha256):
        return _blocked(
            new_job,
            source_run_id,
            old_job.job_id,
            attempt_number,
            ("existing_carry_forward_invalid",),
        )
    evidence = load_yaml(path, TranslationHistoricalReevaluationEvidence)
    if (
        evidence.source_job_id != old_job.job_id
        or evidence.source_attempt_number != attempt_number
        or evidence.job_id != new_job.job_id
    ):
        return _blocked(
            new_job,
            evidence.source_run_id,
            old_job.job_id,
            attempt_number,
            ("existing_carry_forward_lineage_mismatch",),
        )
    return HistoricalReevaluationPlan(
        run_id=new_job.run_id,
        job_id=new_job.job_id,
        chunk_id=new_job.chunk_id,
        source_run_id=evidence.source_run_id,
        source_job_id=evidence.source_job_id,
        source_attempt_number=evidence.source_attempt_number,
        outcome=ALREADY_APPLIED,
        tier=evidence.compatibility.tier,
        verified_identity_checks=tuple(evidence.compatibility.verified_identity_checks),
        changed_framed_sections=tuple(evidence.compatibility.changed_framed_sections),
        observed_resource_drift=tuple(evidence.compatibility.observed_resource_drift),
        historical_status=evidence.historical_status,
        historical_finding_codes=_finding_keys(
            evidence.historical_terminology_report, evidence.historical_structural_report
        ),
        projected_status=evidence.resulting_status,
        terminology_finding_codes=tuple(item.code for item in evidence.terminology_report.findings),
        structural_finding_codes=tuple(item.code for item in evidence.structural_report.findings),
        evidence=evidence,
        existing_evidence_file=path,
    )


def _publish(
    plan: HistoricalReevaluationPlan,
    state: QueueState,
    project_root: Path,
    now: datetime,
) -> tuple[Path, Path, TranslationAcceptanceStatus]:
    evidence = plan.evidence
    assert evidence is not None
    job = next(item for item in state.jobs if item.job_id == plan.job_id)
    translation_bytes = _verified_bytes(
        project_root / evidence.source_translation_file,
        evidence.source_translation_sha256,
        "historical translation",
    )
    destination = ProjectPaths(project_root).translation_carry_forward(
        job.run_id, job.chunk_id, evidence.carry_forward_number
    )
    evidence_path = destination / "evidence.yaml"
    # The acceptance sidecar lives inside the carry-forward record, never in acceptance/NNN: that
    # slot belongs to the provider attempt of the same number, and a repair attempt 001 of this
    # job must find its own slot empty rather than adopt a verdict about the carried draft.
    acceptance_path = destination / "acceptance.yaml"
    evidence_bytes = serialize_yaml(evidence).encode("utf-8")

    def acceptance_for(evidence_sha256: str, created_at: datetime) -> TranslationAcceptanceEvidence:
        assert evidence is not None
        return TranslationAcceptanceEvidence(
            schema_version=SCHEMA_VERSION,
            acceptance_engine_version=HISTORICAL_REEVALUATION_ENGINE_VERSION,
            run_id=job.run_id,
            job_id=job.job_id,
            attempt_id=f"{job.job_id}__carry-forward-{evidence.carry_forward_number:03d}",
            attempt_number=evidence.carry_forward_number,
            attempt_file=project_relative(evidence_path, project_root),
            attempt_sha256=evidence_sha256,
            source_file=evidence.source_file,
            source_sha256=evidence.source_sha256,
            translation_file=evidence.translation_file,
            translation_sha256=evidence.translation_sha256,
            terminology_file=evidence.terminology_file,
            terminology_sha256=evidence.terminology_sha256,
            terminology_qa_version=evidence.terminology_qa_version,
            structural_qa_version=evidence.structural_qa_version,
            alignment_qa_version=evidence.alignment_qa_version,
            terminology_report=evidence.terminology_report,
            structural_report=evidence.structural_report,
            alignment_report=evidence.alignment_report,
            status=evidence.resulting_status,
            accepted=evidence.accepted,
            created_at=created_at,
        )

    if destination.exists():
        persisted = load_yaml(evidence_path, TranslationHistoricalReevaluationEvidence)
        if persisted.model_dump(exclude={"created_at"}) != evidence.model_dump(
            exclude={"created_at"}
        ):
            raise HistoricalReevaluationError(
                "existing carry-forward evidence conflicts with the current plan"
            )
        persisted_acceptance = load_yaml(acceptance_path, TranslationAcceptanceEvidence)
        expected = acceptance_for(sha256_bytes(evidence_path.read_bytes()), now)
        if persisted_acceptance.model_dump(exclude={"created_at"}) != expected.model_dump(
            exclude={"created_at"}
        ):
            raise HistoricalReevaluationError(
                "existing acceptance evidence conflicts with the current QA"
            )
    else:
        acceptance = acceptance_for(sha256_bytes(evidence_bytes), now)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        staging = Path(tempfile.mkdtemp(prefix=".carry-forward.staging.", dir=destination.parent))
        os.chmod(staging, 0o700)
        try:
            private_write(staging / "translation.md", translation_bytes)
            private_write(staging / "evidence.yaml", evidence_bytes)
            private_write(staging / "acceptance.yaml", serialize_yaml(acceptance).encode("utf-8"))
            os.rename(staging, destination)
        except FileExistsError as exc:
            raise HistoricalReevaluationError("carry-forward destination exists") from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    changed = job.model_copy(
        update={
            "status": TranslationJobStatus.SUCCEEDED,
            "completed_at": now,
            "acceptance_status": evidence.resulting_status,
            "acceptance_origin": TranslationAcceptanceOrigin.HISTORICAL_REEVALUATION,
            "accepted_attempt": evidence.carry_forward_number,
            "acceptance_file": project_relative(acceptance_path, project_root),
            "acceptance_sha256": sha256_bytes(acceptance_path.read_bytes()),
            "carry_forward_count": evidence.carry_forward_number,
            "latest_carry_forward_file": project_relative(evidence_path, project_root),
            "latest_carry_forward_sha256": sha256_bytes(evidence_path.read_bytes()),
            "updated_at": now,
            "recent_events": _append_event(
                job,
                _event(
                    "historical_output_reevaluated",
                    now,
                    reason=evidence.resulting_status.value,
                ),
            ),
        }
    )
    _persist_job_and_run(state, _replace(state.jobs, changed), changed, now)
    return evidence_path, acceptance_path, evidence.resulting_status


def reevaluate_historical_output(
    run_id_or_path: str | Path,
    *,
    from_run: str | Path,
    job_id: str,
    attempt_number: int,
    project_root: Path,
    dry_run: bool = False,
    now: Callable[[], datetime] | None = None,
) -> HistoricalReevaluationResult:
    """Carry one historical rejected output forward and assess it under current frozen QA."""
    timestamp = (now or (lambda: datetime.now(UTC)))()
    plan = plan_historical_output_reevaluation(
        run_id_or_path,
        from_run=from_run,
        job_id=job_id,
        attempt_number=attempt_number,
        project_root=project_root,
        now=lambda: timestamp,
    )
    if dry_run:
        return HistoricalReevaluationResult(plan, 0, plan.projected_status, None, None, False, True)
    if plan.outcome == ALREADY_APPLIED:
        return HistoricalReevaluationResult(
            plan, 0, plan.projected_status, plan.existing_evidence_file, None, True, False
        )
    if plan.outcome != ELIGIBLE:
        raise HistoricalReevaluationError(
            "historical output carry-forward refused: " + ", ".join(plan.blocking_reasons)
        )
    project_root = project_root.resolve()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    with queue_lock(run_path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        confirmed = plan_historical_output_reevaluation(
            run_path,
            from_run=from_run,
            job_id=job_id,
            attempt_number=attempt_number,
            project_root=project_root,
            now=lambda: timestamp,
        )
        if confirmed.outcome != ELIGIBLE or confirmed.evidence is None:
            raise HistoricalReevaluationError(
                "historical output carry-forward changed after planning: "
                + ", ".join(confirmed.blocking_reasons or (confirmed.outcome,))
            )
        state = _load_queue(run_path, project_root)
        evidence_path, acceptance_path, status = _publish(confirmed, state, project_root, timestamp)
        return HistoricalReevaluationResult(
            confirmed,
            1,
            status,
            project_relative(evidence_path, project_root),
            project_relative(acceptance_path, project_root),
            False,
            False,
        )
