"""The production semantic gate policy: coverage, granularity, routing, and budgets.

Every number here is a policy rather than a fact, and the policy is versioned so a measured
change can replace it without the orchestration moving. The coverage rule is the one that must
not be argued away by cost: the judge exists to find the defects nothing deterministic can see,
and a paragraph with a normal length, a normal count, and no structural warning is exactly where
such a defect hides. Deterministic signals prioritize and route; they never substitute.
"""

from dataclasses import dataclass

from book_translator.semantic_qa.engine import (
    MAX_SECOND_LOOKS_PER_PARAGRAPH,
    SECOND_LOOK_CONTEXT_POLICY_VERSION,
    SECOND_LOOK_CONTEXT_RADIUS,
)
from book_translator.semantic_revision.policy import (
    MAX_SEMANTIC_REVISIONS_PER_WINDOW,
    SEMANTIC_REVISION_POLICY_VERSION,
)

SEMANTIC_GATE_POLICY_VERSION = "1.0.0"

__all__ = ["PRODUCTION_GATE_V1", "SEMANTIC_GATE_POLICY_VERSION", "SemanticGatePolicy"]


@dataclass(frozen=True, slots=True)
class SemanticGatePolicy:
    policy_version: str
    #: Share of accepted paragraphs that receive semantic judgement. 1.0 is the correctness
    #: policy; anything lower needs measured recall evidence that skipped paragraphs hide nothing.
    coverage: float
    batch_size: int
    second_look_radius: int
    max_second_looks: int
    second_look_policy_version: str
    max_revisions_per_window: int
    revision_policy_version: str
    #: Whether a classified displacement is first tried as a whitespace-only re-segmentation.
    prefer_deterministic_resegmentation: bool


PRODUCTION_GATE_V1 = SemanticGatePolicy(
    policy_version=SEMANTIC_GATE_POLICY_VERSION,
    coverage=1.0,
    batch_size=4,
    second_look_radius=SECOND_LOOK_CONTEXT_RADIUS,
    max_second_looks=MAX_SECOND_LOOKS_PER_PARAGRAPH,
    second_look_policy_version=SECOND_LOOK_CONTEXT_POLICY_VERSION,
    max_revisions_per_window=MAX_SEMANTIC_REVISIONS_PER_WINDOW,
    revision_policy_version=SEMANTIC_REVISION_POLICY_VERSION,
    prefer_deterministic_resegmentation=True,
)
