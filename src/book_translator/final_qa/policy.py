"""Pure versioned policy deciding what may be done about one final QA rejection.

This is the decision layer the whole finalization stage turns on, and it is separated from the
orchestration for the same reasons the auto-repair policy is: it must be reviewable as a policy
rather than read out of control flow, versioned independently because widening it changes what the
product does to a book, and pure, so the cases that matter — a contested canonical, a variant that
turns out to be the canonical's own plural, an exhausted budget behind a repairable defect — are
testable without a queue, a workspace, or a provider.

Three rules run through all of it.

**Provider-free first.** A rejection is only a translation defect if current QA still says so. The
policy therefore reads the *current* QA reports, not the historical ones, and a rejection that no
longer reproduces is settled by re-evaluation — never by a repair invented to justify a finding
that has stopped existing.

**The free remedy first.** A ``paragraph_count_mismatch`` whose whole cause is where the blank
lines are can be repaired from the frozen source alone, and a policy that sent that to a provider
would be spending an attempt on a question it already had the answer to. So when the structural
engine has *proven* a deterministic repair — rendered it, and preserved every character — that
verdict wins over everything else, whatever else the job also carries. It cannot make anything
worse: the repaired text is judged by the same validators, adopted only if it removes findings,
and everything still standing afterwards is diagnosed again from scratch.

**Fail closed on meaning.** ``competing_variant`` is the dominant blocker at the end of a book and
it is not one defect but eight different ones wearing the same code. Only the classes whose
meaning is decidable from governed evidence are automated; a genuine disagreement between two
canonical decisions, or an occurrence nothing in the registry explains, stops and asks. The
product target is an unattended book, and the way to get there is to make the decidable cases
decidable — not to widen the threshold until the undecidable ones slip through.
"""

from dataclasses import dataclass

from book_translator.final_qa.analysis import CompetingVariantAnalysis
from book_translator.models import (
    GOVERNED_RESOLUTION_VARIANT_CLASSES,
    OPERATOR_VARIANT_CLASSES,
    PROVIDER_FREE_VARIANT_CLASSES,
    CompetingVariantClass,
    FinalQAVerdict,
    RepairFindingClass,
    TerminologyValidationReport,
    TranslationAlignmentReport,
    TranslationQAReport,
)
from book_translator.translation_repair_policy import (
    RepairBudget,
    classify_structural_finding,
    classify_terminology_finding,
)

FINAL_QA_RESOLUTION_POLICY_VERSION = "1.2.0"

#: How the verdicts rank when a job carries several blockers at once. The job takes the *most*
#: conservative verdict any one of its findings implies, because clearing the easy half of a
#: rejection publishes an attempt that still cannot be accepted.
_SEVERITY: tuple[FinalQAVerdict, ...] = (
    FinalQAVerdict.ACCEPTED_NO_CHANGE,
    FinalQAVerdict.REEVALUATE,
    FinalQAVerdict.REPAIR_STRUCTURE,
    FinalQAVerdict.REPAIR_TRANSLATION,
    FinalQAVerdict.RESOLVE_TERMINOLOGY_THEN_REEVALUATE,
    FinalQAVerdict.RESOLVE_TERMINOLOGY_THEN_REPAIR,
    FinalQAVerdict.EXHAUSTED,
    FinalQAVerdict.OPERATOR_REQUIRED,
)


def _worst(verdicts: tuple[FinalQAVerdict, ...]) -> FinalQAVerdict:
    return max(verdicts, key=_SEVERITY.index)


@dataclass(frozen=True, slots=True)
class FinalQAAssessment:
    """The policy's complete, explainable answer for one QA-rejected job."""

    policy_version: str
    verdict: FinalQAVerdict
    reasons: tuple[str, ...]
    blocking_codes: tuple[str, ...]
    locked_terminology_involved: bool
    budget: RepairBudget

    @property
    def accepts_without_provider(self) -> bool:
        from book_translator.models import ACCEPTS_WITHOUT_PROVIDER_VERDICTS

        return self.verdict in ACCEPTS_WITHOUT_PROVIDER_VERDICTS

    @property
    def actionable(self) -> bool:
        from book_translator.models import ACTIONABLE_VERDICTS

        return self.verdict in ACTIONABLE_VERDICTS


def classify_competing_variant(analysis: CompetingVariantAnalysis) -> tuple[FinalQAVerdict, str]:
    """Map one classified competing variant to the verdict its class permits.

    The mapping is the policy's whole opinion about ``competing_variant`` and is deliberately
    small enough to read in one sitting.
    """
    variant_class = analysis.variant_class
    label = f"competing_variant:{analysis.candidate.candidate_id}:{variant_class.value}"
    if variant_class in PROVIDER_FREE_VARIANT_CLASSES | GOVERNED_RESOLUTION_VARIANT_CLASSES:
        # The finding rests on registry data that no longer supports it. Re-running current QA
        # against the frozen resources is the entire remedy; nothing is sent anywhere.
        return FinalQAVerdict.RESOLVE_TERMINOLOGY_THEN_REEVALUATE, label
    if variant_class is CompetingVariantClass.CANONICAL_ABSENT:
        # The canonical is nowhere in the translation and the rival is: the translation, not the
        # registry, is what is wrong.
        return FinalQAVerdict.REPAIR_TRANSLATION, label
    if variant_class is CompetingVariantClass.LOCKED_CANONICAL_CONFLICT:
        # A locked or user-owned canonical is never the thing that gives way. The decision stands
        # exactly as it is and the translation is brought into line with it.
        return FinalQAVerdict.RESOLVE_TERMINOLOGY_THEN_REPAIR, label
    if variant_class in OPERATOR_VARIANT_CLASSES:
        return FinalQAVerdict.OPERATOR_REQUIRED, label
    return FinalQAVerdict.OPERATOR_REQUIRED, f"{label}:unclassified"


def assess_final_qa(
    *,
    terminology_report: TerminologyValidationReport,
    structural_report: TranslationQAReport,
    competing_variants: tuple[CompetingVariantAnalysis, ...],
    alignment_report: TranslationAlignmentReport | None = None,
    budget: RepairBudget,
    escalation_available: bool = False,
    deterministic_structural_repair: bool = False,
) -> FinalQAAssessment:
    """Decide what one QA-rejected job needs, from its *current* QA reports.

    ``escalation_available`` says only whether a governed budget escalation could be granted; it
    never changes the verdict, because whether the budget may be widened is an authorization
    question and not a diagnosis. It is carried so the caller can distinguish "exhausted, and
    that is the end of it" from "exhausted, and an operator could authorize one more attempt".

    ``deterministic_structural_repair`` is a different kind of input: it is the assertion that a
    provider-free repair of this job's block structure has already been derived and preserves
    the translation's content. That is a fact about a rendered artifact rather than a permission,
    and it decides the verdict, because no other remedy should be reached for while it holds.
    """
    alignment_codes = tuple(
        one.code
        for one in (alignment_report.findings if alignment_report is not None else ())
        if one.severity == "error"
    )
    blocking_codes = (
        *(one.code for one in terminology_report.findings if one.severity == "error"),
        *(one.code for one in structural_report.findings if one.severity == "error"),
        *alignment_codes,
    )
    if alignment_codes:
        # A blocking alignment finding says content may sit under the wrong paragraph identity.
        # Every automatic remedy this policy can reach either rewrites prose or moves separators,
        # and both would be acting on a guess about which paragraph the content belongs to. That
        # judgement is a person's, or a later semantic stage's, and never this policy's.
        return FinalQAAssessment(
            FINAL_QA_RESOLUTION_POLICY_VERSION,
            FinalQAVerdict.OPERATOR_REQUIRED,
            ("paragraph_alignment_requires_judgement",),
            blocking_codes,
            False,
            budget,
        )
    if blocking_codes and deterministic_structural_repair:
        return FinalQAAssessment(
            FINAL_QA_RESOLUTION_POLICY_VERSION,
            FinalQAVerdict.REPAIR_STRUCTURE,
            ("deterministic_structural_repair_available",),
            blocking_codes,
            False,
            budget,
        )
    if not blocking_codes:
        return FinalQAAssessment(
            FINAL_QA_RESOLUTION_POLICY_VERSION,
            FinalQAVerdict.REEVALUATE,
            ("current_qa_clears_the_standing_translation",),
            (),
            False,
            budget,
        )

    verdicts: list[FinalQAVerdict] = []
    reasons: list[str] = []
    locked = False
    # Only the variants the installed validator still enforces can explain a live finding, and a
    # candidate may carry several of them; the job takes the worst answer any of them implies.
    triggered: dict[str, list[CompetingVariantAnalysis]] = {}
    for analysis in competing_variants:
        if analysis.enforceable:
            triggered.setdefault(analysis.candidate.candidate_id, []).append(analysis)

    for finding in terminology_report.findings:
        if finding.severity != "error":
            continue
        if finding.code == "competing_variant":
            analyses = triggered.get(finding.candidate_id or "")
            if not analyses:
                # QA fired on a variant the analyzer could not account for. That disagreement is
                # itself a reason to stop: the two must see the same registry.
                verdicts.append(FinalQAVerdict.OPERATOR_REQUIRED)
                reasons.append(f"competing_variant:{finding.candidate_id}:unanalyzable")
                continue
            classified = [classify_competing_variant(analysis) for analysis in analyses]
            verdict = _worst(tuple(item for item, _ in classified))
            locked = locked or any(
                analysis.variant_class is CompetingVariantClass.LOCKED_CANONICAL_CONFLICT
                for analysis in analyses
            )
            verdicts.append(verdict)
            reasons.extend(reason for _, reason in classified)
            continue
        classification = classify_terminology_finding(finding.code, finding.enforcement)
        verdicts.append(
            FinalQAVerdict.REPAIR_TRANSLATION
            if classification is RepairFindingClass.AUTO_REPAIRABLE
            else FinalQAVerdict.OPERATOR_REQUIRED
        )
        reasons.append(f"terminology:{finding.code}:{classification.value}")

    for structural in structural_report.findings:
        if structural.severity != "error":
            continue
        classification = classify_structural_finding(structural.code)
        verdicts.append(
            FinalQAVerdict.REPAIR_TRANSLATION
            if classification is RepairFindingClass.AUTO_REPAIRABLE
            else FinalQAVerdict.OPERATOR_REQUIRED
        )
        reasons.append(f"structural:{structural.code}:{classification.value}")

    verdict = _worst(tuple(verdicts))
    if (
        verdict
        in {FinalQAVerdict.REPAIR_TRANSLATION, FinalQAVerdict.RESOLVE_TERMINOLOGY_THEN_REPAIR}
        and budget.remaining < 1
    ):
        verdict = FinalQAVerdict.EXHAUSTED
        reasons.append(
            f"{budget.limiting_bound}_exhausted"
            + (":escalation_available" if escalation_available else "")
        )
    return FinalQAAssessment(
        FINAL_QA_RESOLUTION_POLICY_VERSION,
        verdict,
        tuple(reasons),
        blocking_codes,
        locked,
        budget,
    )
