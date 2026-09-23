"""The bounded revision budget and the rules that decide when a revision may be adopted."""

from book_translator.models.terminology import TerminologyValidationReport

#: Version of the revision policy: the budget, the scope rule, and the adoption conjunction.
SEMANTIC_REVISION_POLICY_VERSION = "1.0.0"

#: Hard limit on provider-backed revision attempts one paragraph or window may consume. Two is
#: one honest correction plus one retry; a third would be a translator-judge loop.
MAX_SEMANTIC_REVISIONS_PER_WINDOW = 2

__all__ = [
    "MAX_SEMANTIC_REVISIONS_PER_WINDOW",
    "SEMANTIC_REVISION_POLICY_VERSION",
    "terminology_regressed",
]


def terminology_regressed(
    before: TerminologyValidationReport, after: TerminologyValidationReport
) -> tuple[str, ...]:
    """Return the blocking terminology codes a revision introduced.

    A revision that fixes a fidelity defect by dropping a canonical term has traded one defect
    for another, and the trade is refused. A blocking code the standing translation already
    carried is not a regression; it is the terminology stage's business, not this one's.
    """
    standing = {item.code for item in before.findings if item.severity == "error"}
    resulting = {item.code for item in after.findings if item.severity == "error"}
    return tuple(sorted(resulting - standing))
