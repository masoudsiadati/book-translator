"""Pure rules for a targeted semantic revision: one operator-named paragraph, nothing else.

The gate revises what its own judge asked it to revise. Some defects no mechanical check and no
judge sampling will ever raise — a paragraph that is well-formed, terminologically clean, and
simply says the wrong thing, identified by a person reading the book. This module is the pure
half of the boundary that answers exactly that case: it turns one operator statement into the
bounded window the existing reviser already understands, and it decides, from measured facts
alone, whether the answer may be adopted.

It owns no I/O and no provider. What makes the revision safe lives next door and is reused
unchanged: :func:`~book_translator.semantic_revision.bounds.splice_window` copies every other
paragraph from the standing translation, and :func:`~book_translator.semantic_revision.bounds.
measure_bounds` re-derives that nothing outside the window moved.
"""

import hashlib
from dataclasses import dataclass

from book_translator.models import (
    OperatorDefectStatement,
    RevisedParagraphIdentity,
    SemanticFinding,
    SemanticParagraphOutcome,
    SemanticRevisionWindow,
    SemanticVerdict,
    TerminologyValidationReport,
    TranslationAlignmentReport,
    TranslationQAReport,
)
from book_translator.paragraph_markers import source_paragraphs
from book_translator.semantic_qa.engine import paragraph_identity as paragraph_marker_identity
from book_translator.semantic_revision.policy import (
    MAX_SEMANTIC_REVISIONS_PER_WINDOW,
    terminology_regressed,
)

#: The operator-initiated revision boundary. Its own version, because the gate's engine, the
#: reviser prompt, and the revision policy are unchanged by it and must keep verifying their
#: own historical records.
TARGETED_SEMANTIC_REVISION_VERSION = "1.0.0"

__all__ = [
    "TARGETED_SEMANTIC_REVISION_VERSION",
    "TargetedRevisionError",
    "adoption_refusals",
    "operator_findings",
    "paragraph_identity",
    "remaining_revision_budget",
    "single_paragraph_window",
]


class TargetedRevisionError(ValueError):
    """Raised when a targeted revision cannot be proven safe to plan or to adopt."""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def single_paragraph_window(
    paragraph_index: int, *, paragraph_count: int
) -> SemanticRevisionWindow:
    """The one writable paragraph. Every other paragraph of the chunk is outside it."""
    if paragraph_index < 1 or paragraph_index > paragraph_count:
        raise TargetedRevisionError(
            f"paragraph {paragraph_index} is outside the chunk's {paragraph_count} paragraphs"
        )
    return SemanticRevisionWindow(
        first_index=paragraph_index, last_index=paragraph_index, reason="single_paragraph"
    )


def operator_findings(
    statement: OperatorDefectStatement, paragraph_index: int
) -> dict[int, tuple[SemanticFinding, ...]]:
    """Render the operator's statement in the vocabulary the reviser prompt already takes.

    Confidence is 1.0 because a person asserted it; the record says the claim is the operator's,
    so nothing downstream can mistake this for a judge's sampled verdict.
    """
    finding = SemanticFinding(
        code=statement.code,
        severity=statement.severity,
        source_evidence=statement.source_evidence,
        explanation=statement.reason,
        confidence=1.0,
    )
    return {paragraph_index: (finding,)}


def paragraph_identity(
    *, source_text: str, translation_text: str, paragraph_index: int, revised_text: str | None
) -> RevisedParagraphIdentity:
    """Bind the named paragraph: its identity, its source bytes, and its Persian before/after."""
    source_blocks = source_paragraphs(source_text)
    target_blocks = source_paragraphs(translation_text)
    if len(source_blocks) != len(target_blocks):
        raise TargetedRevisionError(
            "the standing translation is not paragraph-aligned with its source"
        )
    if paragraph_index < 1 or paragraph_index > len(source_blocks):
        raise TargetedRevisionError("the named paragraph is outside the chunk")
    after: str | None = None
    if revised_text is not None:
        revised_blocks = source_paragraphs(revised_text)
        if len(revised_blocks) != len(target_blocks):
            raise TargetedRevisionError("a revision must not change the number of paragraphs")
        after = _digest(revised_blocks[paragraph_index - 1])
    return RevisedParagraphIdentity(
        paragraph_id=paragraph_marker_identity(paragraph_index),
        paragraph_index=paragraph_index,
        source_paragraph_sha256=_digest(source_blocks[paragraph_index - 1]),
        target_paragraph_sha256_before=_digest(target_blocks[paragraph_index - 1]),
        target_paragraph_sha256_after=after,
    )


def remaining_revision_budget(provider_revisions_on_paragraph: int) -> int:
    """How many provider-backed revisions this paragraph may still consume."""
    return max(0, MAX_SEMANTIC_REVISIONS_PER_WINDOW - provider_revisions_on_paragraph)


@dataclass(frozen=True, slots=True)
class AdoptionInputs:
    """Everything measured about one candidate, and nothing inferred from anything else."""

    standing_terminology: TerminologyValidationReport
    terminology: TerminologyValidationReport
    structural: TranslationQAReport
    alignment: TranslationAlignmentReport
    verification: SemanticParagraphOutcome | None
    scope_error: str | None = None
    marker_error: str | None = None
    provider_error: str | None = None
    verification_error: str | None = None


def adoption_refusals(inputs: AdoptionInputs) -> tuple[str, ...]:
    """Every reason this candidate may not be adopted, in the order they were established.

    A revision is adopted only when it changed nothing outside its paragraph, kept every marker,
    introduced no blocking terminology code the standing text did not already carry, passed the
    structural and alignment batteries, and was independently judged faithful. Each of those is
    a measured fact; none of them is assumed from another.
    """
    refusals: list[str] = []
    if inputs.provider_error is not None:
        refusals.append(f"reviser:{inputs.provider_error}")
    if inputs.marker_error is not None:
        refusals.append(f"markers:{inputs.marker_error}")
    if inputs.scope_error is not None:
        refusals.append(f"scope:{inputs.scope_error}")
    regressed = terminology_regressed(inputs.standing_terminology, inputs.terminology)
    if regressed:
        refusals.append("terminology_regression:" + ",".join(regressed))
    if not inputs.structural.valid:
        refusals.append(
            "structural:"
            + ",".join(item.code for item in inputs.structural.findings if item.severity == "error")
        )
    if not inputs.alignment.valid:
        refusals.append(
            "alignment:"
            + ",".join(item.code for item in inputs.alignment.findings if item.severity == "error")
        )
    if inputs.verification_error is not None:
        refusals.append(f"verification:{inputs.verification_error}")
    elif inputs.verification is None:
        refusals.append("verification:not_judged")
    elif inputs.verification.policy_outcome is not SemanticVerdict.PASS:
        refusals.append(f"verification:{inputs.verification.policy_outcome.value}")
    return tuple(refusals)
