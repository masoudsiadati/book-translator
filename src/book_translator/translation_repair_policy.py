"""Pure policy deciding whether a mechanical QA rejection may be repaired without an operator.

The repair *mechanism* already exists and is deliberately unopinionated: given a QA-rejected
provider-successful attempt it will send the source, the rejected translation, and the exact
findings back through the provider boundary. Nothing in it decides whether doing so is a good
idea, which is exactly the decision an unattended worker has to make on its own.

That decision is separated here for three reasons. It must be reviewable as a policy rather than
read out of orchestration control flow; it must be versioned independently, because widening what
may be repaired automatically changes what the product does to a book without any code near the
queue changing; and it must be pure, so the interesting cases — a finding class nobody has seen
before, a mixture of a repairable and an unsafe finding, an exhausted budget — are testable
without a queue, a workspace, or a provider.

The rule is a conjunction, and every part of it fails closed:

* Every *blocking* finding must be individually auto-repairable. A rejection carrying one unsafe
  finding beside a repairable one is refused whole, because repairing the repairable half would
  publish an attempt that still cannot be accepted while spending budget that the unsafe half
  will need.
* An unrecognized finding code is ``operator_required``. A QA engine that grew a new blocking
  code must not silently inherit permission to spend provider attempts on it.
* Findings whose QA judgement may simply have moved are ``reevaluation_candidate``: when the
  evidence records which QA engine versions produced it and those versions are no longer the
  installed ones, the correct next step is the provider-free re-evaluation boundary, not a paid
  repair of an output that current QA might already accept.
* The budget is the smaller of the repair-attempt allowance and the job's remaining provider
  attempts, so this policy can only ever be more conservative than the queue's own accounting.

One narrow escape hatch exists. A finding's code says what QA saw, not always what it *means*: the
same ``competing_variant`` covers a contested canonical, a plural of the canonical, and a provider
that simply used the wrong rendering. A caller holding strictly stronger, separately versioned
evidence about one specific finding may say so through ``terminology_overrides``, keyed by
``code:candidate_id``. The default is empty and the classification is unchanged without it, the
override is recorded in the persisted repair authorization alongside every other classification,
and it can only ever be applied to a finding the caller has actually analyzed.

Nothing here performs I/O or reads the workspace; the caller supplies the effective QA reports,
the recorded and installed QA engine versions, and the two budgets.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from book_translator.models import (
    AutoRepairVerdict,
    TerminologyEnforcement,
    TerminologyValidationReport,
    TranslationAcceptanceStatus,
    TranslationAlignmentReport,
    TranslationJob,
    TranslationJobStatus,
    TranslationQAReport,
)
from book_translator.models import (
    RepairFindingClass as RepairFindingClass,
)

AUTO_REPAIR_POLICY_VERSION = "1.2.0"

#: Default repair allowance: one initial translation attempt plus at most this many repairs.
DEFAULT_MAX_REPAIR_ATTEMPTS = 2

# A structural defect is mechanically specified by the frozen source, so a repair request can
# state exactly what has to change. `known_source_anomaly` is the exception: it describes the
# source edition, and no rewrite of the translation can resolve something the translation did
# not cause.
_STRUCTURAL_CLASSES: dict[str, RepairFindingClass] = {
    "paragraph_count_mismatch": RepairFindingClass.AUTO_REPAIRABLE,
    "trailing_double_space": RepairFindingClass.AUTO_REPAIRABLE,
    "tab": RepairFindingClass.AUTO_REPAIRABLE,
    "replacement_character": RepairFindingClass.AUTO_REPAIRABLE,
    "doubled_zwnj": RepairFindingClass.AUTO_REPAIRABLE,
    "arabic_yeh": RepairFindingClass.AUTO_REPAIRABLE,
    "arabic_kaf": RepairFindingClass.AUTO_REPAIRABLE,
    "suspicious_persian_conjunction_spacing": RepairFindingClass.AUTO_REPAIRABLE,
    "known_source_anomaly": RepairFindingClass.NON_REPAIRABLE,
}

# Alignment findings are never auto-repairable.  A suspected boundary drift is a claim that
# content sits under the wrong paragraph identity, and the only honest remedies are a wider
# repair a person authorizes or the semantic judgement this phase deliberately does not attempt.
# Handing it to a provider as "fix this" would ask the provider to guess which sentence belongs
# where, which is the guess the whole numbered contract exists to eliminate.
_ALIGNMENT_CLASSES: dict[str, RepairFindingClass] = {
    "adjacent_boundary_drift_suspected": RepairFindingClass.OPERATOR_REQUIRED,
    "paragraph_word_ratio_out_of_band": RepairFindingClass.NON_REPAIRABLE,
    "numeral_not_preserved": RepairFindingClass.NON_REPAIRABLE,
    "dialogue_marks_absent": RepairFindingClass.NON_REPAIRABLE,
    "alignment_not_evaluable": RepairFindingClass.NON_REPAIRABLE,
}

# Terminology is repairable only where the governed registry supplies the answer the provider is
# missing. `competing_variant` is not that case: the translation used a *known* rival rendering,
# which is evidence that the canonical decision itself is contested, and resolving that is the
# terminology governance boundary's job rather than a translation edit.
_TERMINOLOGY_CLASSES: dict[str, RepairFindingClass] = {
    "canonical_term_missing": RepairFindingClass.AUTO_REPAIRABLE,
    "competing_variant": RepairFindingClass.OPERATOR_REQUIRED,
    "canonical_term_unverified": RepairFindingClass.REEVALUATION_CANDIDATE,
    "provisional_decision_enforced": RepairFindingClass.REEVALUATION_CANDIDATE,
}

# Enforcement answers what proof a canonical decision demands. Only the two mechanically provable
# kinds can be handed to a provider as an instruction; a contextual or absent enforcement is a
# judgement, and this policy never automates a judgement.
_REPAIRABLE_ENFORCEMENT = frozenset(
    {TerminologyEnforcement.EXACT, TerminologyEnforcement.MORPHOLOGICAL}
)


@dataclass(frozen=True, slots=True)
class ClassifiedFinding:
    """One QA finding placed in a remedy class, carrying no translated content."""

    origin: str
    code: str
    severity: str
    classification: RepairFindingClass

    @property
    def blocking(self) -> bool:
        return self.severity == "error"


@dataclass(frozen=True, slots=True)
class RepairBudget:
    """How many further repair attempts the policy and the queue jointly permit."""

    max_repair_attempts: int
    repair_attempts_used: int
    max_provider_attempts: int
    provider_attempts_used: int

    @property
    def policy_remaining(self) -> int:
        return max(0, self.max_repair_attempts - self.repair_attempts_used)

    @property
    def provider_remaining(self) -> int:
        return max(0, self.max_provider_attempts - self.provider_attempts_used)

    @property
    def remaining(self) -> int:
        return min(self.policy_remaining, self.provider_remaining)

    @property
    def limiting_bound(self) -> str:
        return "repair_attempts" if self.policy_remaining <= self.provider_remaining else "attempts"


@dataclass(frozen=True, slots=True)
class AutoRepairAssessment:
    """The policy's complete, explainable answer for one QA-rejected job."""

    policy_version: str
    verdict: AutoRepairVerdict
    findings: tuple[ClassifiedFinding, ...]
    triggering_codes: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    budget: RepairBudget

    @property
    def repairable(self) -> bool:
        return self.verdict is AutoRepairVerdict.REPAIRABLE


def is_auto_repair_candidate(job: TranslationJob) -> bool:
    """Return whether a job is in the only state an automatic repair may act on.

    Provider success plus an effective mechanical rejection is the whole precondition. A job that
    is claimed, running, failed, blocked, accepted, or unassessed is deliberately outside it.
    So is a rejection resting on a carried-forward historical draft: repairing a draft this run
    never produced is an explicit operator decision, never an unattended one.
    """
    return (
        job.status is TranslationJobStatus.SUCCEEDED
        and job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
        and job.carry_forward_count == 0
    )


def is_failed_repair(job: TranslationJob) -> bool:
    """Return whether a provider failure ended a *repair* of this job rather than a translation.

    The two are told apart by what the job still points at rather than by reading an attempt. A
    failed repair leaves the QA rejection it was answering fully intact — the acceptance sidecar,
    the accepted attempt number, the origin — because the executor only clears those on a repair
    that *succeeded*. So a failed job that still carries a standing rejection whose accepted
    attempt is not the attempt that just failed is a job whose repair the provider never
    delivered, and nothing else produces that shape.

    A job in this state is deliberately outside :func:`is_auto_repair_candidate`, which requires
    a live provider success: repairing from here would have to restore the draft first, and that
    restoration is a governed recovery decision rather than a repair decision.
    """
    if (
        job.status is TranslationJobStatus.FAILED
        and job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
        and job.carry_forward_count > 0
    ):
        # A carried-forward draft is numbered by carry-forward, not by attempt, so its first
        # repair attempt shares its number; the standing carry-forward is what identifies it.
        return job.last_attempt is not None
    return (
        job.status is TranslationJobStatus.FAILED
        and job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
        and job.accepted_attempt is not None
        and job.last_attempt is not None
        and job.last_attempt != job.accepted_attempt
    )


def is_active_repair(job: TranslationJob) -> bool:
    """Return whether a held claim or a running execution is a repair rather than a translation.

    The distinction is structural, not a guess from the shape of the evidence. A ``pending`` job
    may not carry an acceptance status at all — ``TranslationJob`` refuses that combination — and
    an ordinary claim only ever starts from ``pending``, so every ordinary translation execution
    runs with no acceptance on record. The one edge that claims an already settled job is
    ``succeeded -> claimed``, which the frozen policy opens solely for a repair and which is taken
    only after the rejection's whole lineage has been hash-verified.

    So an active job that still carries an acceptance status is a repair of that acceptance, and
    nothing else produces that shape. Readers that must tell a repair execution from a translation
    one — deciding what a half-finished execution was, above all — ask this rather than inspecting
    an attempt that may not exist.
    """
    return (
        job.status in {TranslationJobStatus.CLAIMED, TranslationJobStatus.RUNNING}
        and job.acceptance_status is not None
    )


def classify_terminology_finding(
    code: str, enforcement: TerminologyEnforcement | None
) -> RepairFindingClass:
    """Classify one terminology finding, failing closed on an unknown code."""
    classification = _TERMINOLOGY_CLASSES.get(code, RepairFindingClass.OPERATOR_REQUIRED)
    if (
        classification is RepairFindingClass.AUTO_REPAIRABLE
        and enforcement not in _REPAIRABLE_ENFORCEMENT
    ):
        return RepairFindingClass.OPERATOR_REQUIRED
    return classification


def classify_structural_finding(code: str) -> RepairFindingClass:
    """Classify one structural finding, failing closed on an unknown code."""
    return _STRUCTURAL_CLASSES.get(code, RepairFindingClass.OPERATOR_REQUIRED)


def classify_alignment_finding(code: str) -> RepairFindingClass:
    """Classify one alignment finding, failing closed on an unknown code."""
    return _ALIGNMENT_CLASSES.get(code, RepairFindingClass.OPERATOR_REQUIRED)


def override_key(code: str, candidate_id: str | None) -> str:
    """Name one terminology finding precisely enough to be reclassified on its own."""
    return f"{code}:{candidate_id or ''}"


def classify_findings(
    terminology_report: TerminologyValidationReport,
    structural_report: TranslationQAReport,
    *,
    alignment_report: TranslationAlignmentReport | None = None,
    terminology_overrides: Mapping[str, RepairFindingClass] | None = None,
) -> tuple[ClassifiedFinding, ...]:
    """Classify every finding of both QA reports in a stable, report-ordered sequence."""
    overrides = terminology_overrides or {}
    classified = [
        ClassifiedFinding(
            "terminology",
            finding.code,
            finding.severity,
            overrides.get(
                override_key(finding.code, finding.candidate_id),
                classify_terminology_finding(finding.code, finding.enforcement),
            ),
        )
        for finding in terminology_report.findings
    ]
    classified.extend(
        ClassifiedFinding(
            "structural", finding.code, finding.severity, classify_structural_finding(finding.code)
        )
        for finding in structural_report.findings
    )
    if alignment_report is not None:
        classified.extend(
            ClassifiedFinding(
                "alignment",
                finding.code,
                finding.severity,
                classify_alignment_finding(finding.code),
            )
            for finding in alignment_report.findings
        )
    return tuple(classified)


def _stale_qa_engines(
    recorded: tuple[str | None, str | None], installed: tuple[str, str]
) -> tuple[str, ...]:
    """Name the QA engines whose recorded version is not the installed one.

    An unrecorded version proves nothing and is not treated as drift: older acceptance evidence
    predates the fields, and inventing staleness there would refuse every legacy rejection.
    """
    labels = ("terminology_qa", "structural_qa")
    return tuple(
        f"{label}_version_moved"
        for label, was, now in zip(labels, recorded, installed, strict=True)
        if was is not None and was != now
    )


def assess_auto_repair(
    *,
    terminology_report: TerminologyValidationReport,
    structural_report: TranslationQAReport,
    alignment_report: TranslationAlignmentReport | None = None,
    budget: RepairBudget,
    recorded_terminology_qa_version: str | None = None,
    recorded_structural_qa_version: str | None = None,
    installed_terminology_qa_version: str,
    installed_structural_qa_version: str,
    terminology_overrides: Mapping[str, RepairFindingClass] | None = None,
) -> AutoRepairAssessment:
    """Decide whether one effective QA rejection may be repaired automatically."""
    findings = classify_findings(
        terminology_report,
        structural_report,
        alignment_report=alignment_report,
        terminology_overrides=terminology_overrides,
    )
    blocking = tuple(finding for finding in findings if finding.blocking)
    triggering = tuple(finding.code for finding in blocking)
    if not blocking:
        return AutoRepairAssessment(
            AUTO_REPAIR_POLICY_VERSION,
            AutoRepairVerdict.NOT_REJECTED,
            findings,
            (),
            (),
            budget,
        )
    stale = _stale_qa_engines(
        (recorded_terminology_qa_version, recorded_structural_qa_version),
        (installed_terminology_qa_version, installed_structural_qa_version),
    )
    if stale:
        return AutoRepairAssessment(
            AUTO_REPAIR_POLICY_VERSION,
            AutoRepairVerdict.REEVALUATION_REQUIRED,
            findings,
            triggering,
            stale,
            budget,
        )
    refusals = tuple(
        f"{finding.origin}:{finding.code}:{finding.classification.value}"
        for finding in blocking
        if finding.classification is not RepairFindingClass.AUTO_REPAIRABLE
    )
    if refusals:
        return AutoRepairAssessment(
            AUTO_REPAIR_POLICY_VERSION,
            AutoRepairVerdict.REFUSED,
            findings,
            triggering,
            refusals,
            budget,
        )
    if budget.remaining < 1:
        return AutoRepairAssessment(
            AUTO_REPAIR_POLICY_VERSION,
            AutoRepairVerdict.BUDGET_EXHAUSTED,
            findings,
            triggering,
            (f"{budget.limiting_bound}_exhausted",),
            budget,
        )
    return AutoRepairAssessment(
        AUTO_REPAIR_POLICY_VERSION,
        AutoRepairVerdict.REPAIRABLE,
        findings,
        triggering,
        (),
        budget,
    )
