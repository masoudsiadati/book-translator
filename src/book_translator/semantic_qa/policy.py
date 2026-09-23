"""The versioned policy that decides what a judge's answer is allowed to mean.

The judge reports what it saw. The policy decides which of those reports are material enough to
count, and the two are deliberately not the same act. A judge that flags a lost shade of emphasis
is not wrong, and a pipeline that treated every such report as a defect would spend a person's
attention on prose that is fine — the false-positive failure this stage is most exposed to.

Materiality is a function of severity and confidence together, because the two carry different
risks. A HIGH claim asserts a plot-level change and is worth acting on even when the judge is only
fairly sure; a MEDIUM claim needs more conviction before it is worth a provider call; a LOW claim
is recorded as advisory and never blocks, because nuance is exactly where an automated fidelity
judge is least reliable and where a Persian literary editor, not a repair, is the right answer.

INCONCLUSIVE is never quietly converted. It is not a pass, it is not a failure, and it is not an
automatic escalation to a person: it is persisted as itself so a later orchestration stage can
decide with evidence rather than inheriting a decision made here.
"""

from dataclasses import dataclass

from book_translator.models.semantic_qa import (
    SemanticFinding,
    SemanticParagraphVerdict,
    SemanticSeverity,
    SemanticVerdict,
)

#: Version of the policy itself: the materiality thresholds and the aggregation rules.
SEMANTIC_COVERAGE_POLICY_VERSION = "1.0.0"

__all__ = [
    "SEMANTIC_COVERAGE_POLICY_VERSION",
    "EN_FA_JUDGE_V1",
    "SemanticPolicy",
    "apply_policy",
]


@dataclass(frozen=True, slots=True)
class SemanticPolicy:
    """Minimum confidence a finding needs, per severity, before it counts as material."""

    policy_id: str
    policy_version: str
    high_confidence: float
    medium_confidence: float
    #: ``None`` means a LOW finding is never material at any confidence.
    low_confidence: float | None

    def material(self, finding: SemanticFinding) -> bool:
        if finding.severity is SemanticSeverity.HIGH:
            return finding.confidence >= self.high_confidence
        if finding.severity is SemanticSeverity.MEDIUM:
            return finding.confidence >= self.medium_confidence
        return self.low_confidence is not None and finding.confidence >= self.low_confidence


#: The initial English-to-Persian policy. The thresholds are conservative on purpose: this phase
#: is measuring whether the judge is trustworthy, and a policy that acted on weak claims would
#: make that measurement impossible to interpret.
EN_FA_JUDGE_V1 = SemanticPolicy(
    policy_id="en-fa-semantic",
    policy_version=SEMANTIC_COVERAGE_POLICY_VERSION,
    high_confidence=0.60,
    medium_confidence=0.70,
    low_confidence=None,
)


def apply_policy(
    verdict: SemanticParagraphVerdict, *, policy: SemanticPolicy = EN_FA_JUDGE_V1
) -> tuple[SemanticVerdict, tuple[SemanticFinding, ...], tuple[SemanticFinding, ...]]:
    """Return the policy outcome plus the material and advisory findings behind it.

    A judge REVISE whose findings are all immaterial becomes PASS with advisories, which is the
    single most important lever this policy owns: it is where a judge that is merely fussy stops
    costing anything. A judge PASS is never overridden into REVISE, because the policy has no
    evidence the judge did not have.
    """
    material = tuple(item for item in verdict.findings if policy.material(item))
    advisory = tuple(item for item in verdict.findings if item not in material)
    if verdict.verdict is SemanticVerdict.INCONCLUSIVE:
        return SemanticVerdict.INCONCLUSIVE, material, advisory
    if material:
        return SemanticVerdict.REVISE, material, advisory
    return SemanticVerdict.PASS, (), advisory
