"""Provider-free assessment of whether a translation's paragraphs still say what the source's do.

This layer runs after paragraph identity has been established — by the numbered marker contract
for a new job, or by positional correspondence for a marker-less historical one — and asks a
narrower question than terminology QA does: for each identified pair, is the target plausibly the
translation of *that* source paragraph and no other?  It answers with counts only.  It never reads
a glossary, never judges wording, and never contacts anything.

Terminology findings are deliberately not mixed in.  A canonical term rendered wrongly and a
paragraph boundary that moved are different defects with different remedies, and a report that
merged them would make the automatic repair policy unable to tell them apart.
"""

from dataclasses import dataclass

from book_translator.paragraph_alignment.metrics import ParagraphMetrics, measure_pair
from book_translator.paragraph_alignment.policy import (
    ADJACENT_BOUNDARY_DRIFT_SUSPECTED,
    ALIGNMENT_NOT_EVALUABLE,
    DIALOGUE_MARKS_ABSENT,
    NUMERAL_NOT_PRESERVED,
    PARAGRAPH_WORD_RATIO_OUT_OF_BAND,
    AlignmentPolicy,
)
from book_translator.paragraph_markers.engine import marker_token, source_paragraphs
from book_translator.paragraph_markers.models import MarkerFinding

__all__ = ["AlignmentAssessment", "AlignmentFinding", "assess_alignment"]

#: How the paragraph identities being judged were established.  It changes nothing about the
#: measurements and everything about how a reader should weigh them, so it is recorded rather
#: than inferred.
MARKED = "marked"
LEGACY_POSITIONAL = "legacy_positional"


@dataclass(frozen=True, slots=True)
class AlignmentFinding:
    """One alignment defect or suspicion, carrying counts and indices but never prose."""

    code: str
    severity: str
    source_paragraph_index: int | None
    related_paragraph_index: int | None
    expected_marker: str | None
    observed_marker: str | None
    message: str


@dataclass(frozen=True, slots=True)
class AlignmentAssessment:
    """The complete alignment answer for one source/translation pair of documents."""

    policy: AlignmentPolicy
    contract_mode: str
    evaluated: bool
    metrics: tuple[ParagraphMetrics, ...]
    findings: tuple[AlignmentFinding, ...]

    @property
    def valid(self) -> bool:
        return not any(finding.severity == "error" for finding in self.findings)

    @property
    def measured_pairs(self) -> int:
        return sum(1 for item in self.metrics if self.policy.measurable(item.source_words))

    @property
    def flagged_pairs(self) -> int:
        return len(
            {
                finding.source_paragraph_index
                for finding in self.findings
                if finding.source_paragraph_index is not None
            }
        )


def _from_marker_finding(finding: MarkerFinding) -> AlignmentFinding:
    # The parser may quote a malformed marker's whole line, prose included; an alignment report
    # is persisted and shown, so only the bare marker-like token may travel with the finding.
    observed = finding.observed_marker
    return AlignmentFinding(
        code=finding.code,
        severity=finding.severity,
        source_paragraph_index=finding.source_paragraph_index,
        related_paragraph_index=None,
        expected_marker=finding.expected_marker,
        observed_marker=marker_token(observed) if observed is not None else None,
        message=finding.message,
    )


def _ratio_findings(
    metrics: tuple[ParagraphMetrics, ...], policy: AlignmentPolicy
) -> list[AlignmentFinding]:
    findings: list[AlignmentFinding] = []
    for item in metrics:
        if not policy.measurable(item.source_words) or policy.in_band(item.word_ratio):
            continue
        direction = "below" if item.word_ratio < policy.low_word_ratio else "above"
        findings.append(
            AlignmentFinding(
                code=PARAGRAPH_WORD_RATIO_OUT_OF_BAND,
                severity=policy.severity_for(PARAGRAPH_WORD_RATIO_OUT_OF_BAND),
                source_paragraph_index=item.index,
                related_paragraph_index=None,
                expected_marker=None,
                observed_marker=None,
                message=(
                    f"target/source word ratio is {direction} the policy band"
                    f" ({item.target_words} target words for {item.source_words} source words)"
                ),
            )
        )
    return findings


def _drift_findings(
    metrics: tuple[ParagraphMetrics, ...], policy: AlignmentPolicy
) -> list[AlignmentFinding]:
    """Report adjacent pairs whose deficit and excess cancel each other out.

    The conjunction is the whole point.  One paragraph short and the next long is common; one
    paragraph short, the next long, *and the two together back inside the ordinary band* is
    content conservation across a boundary, which is what migrated prose looks like and what
    independent literary variation does not.
    """

    findings: list[AlignmentFinding] = []
    for first, second in zip(metrics, metrics[1:], strict=False):
        if not (policy.measurable(first.source_words) and policy.measurable(second.source_words)):
            continue
        low, high = first.word_ratio, second.word_ratio
        complementary = (low < policy.low_word_ratio and high > policy.high_word_ratio) or (
            low > policy.high_word_ratio and high < policy.low_word_ratio
        )
        if not complementary:
            continue
        combined_source = first.source_words + second.source_words
        combined = (first.target_words + second.target_words) / combined_source
        if not policy.in_band(combined):
            continue
        findings.append(
            AlignmentFinding(
                code=ADJACENT_BOUNDARY_DRIFT_SUSPECTED,
                severity=policy.severity_for(ADJACENT_BOUNDARY_DRIFT_SUSPECTED),
                source_paragraph_index=first.index,
                related_paragraph_index=second.index,
                expected_marker=None,
                observed_marker=None,
                message=(
                    "adjacent paragraphs carry complementary word deficits and excesses whose"
                    f" combined ratio ({combined:.2f}) is inside the policy band, which is the"
                    " signature of content that crossed the boundary between them"
                ),
            )
        )
    return findings


def _content_findings(
    metrics: tuple[ParagraphMetrics, ...], policy: AlignmentPolicy
) -> list[AlignmentFinding]:
    findings: list[AlignmentFinding] = []
    for item in metrics:
        missing = item.missing_numerals
        if missing:
            findings.append(
                AlignmentFinding(
                    code=NUMERAL_NOT_PRESERVED,
                    severity=policy.severity_for(NUMERAL_NOT_PRESERVED),
                    source_paragraph_index=item.index,
                    related_paragraph_index=None,
                    expected_marker=None,
                    observed_marker=None,
                    message=(
                        f"{len(missing)} source numeral(s) have no counterpart in the target"
                        " paragraph; a spelled-out number is a legitimate reason for this"
                    ),
                )
            )
        # Only the unambiguous direction is reported: the source clearly marks direct speech and
        # the target marks none at all.  A differing *count* is not evidence, because Persian
        # typesetting legitimately renders a quoted span with a different number of marks.
        if item.source_dialogue_marks >= 2 and item.target_dialogue_marks == 0:
            findings.append(
                AlignmentFinding(
                    code=DIALOGUE_MARKS_ABSENT,
                    severity=policy.severity_for(DIALOGUE_MARKS_ABSENT),
                    source_paragraph_index=item.index,
                    related_paragraph_index=None,
                    expected_marker=None,
                    observed_marker=None,
                    message=(
                        "the source paragraph marks direct speech and the target paragraph"
                        " carries no quotation marks at all"
                    ),
                )
            )
    return findings


def assess_alignment(
    source_text: str,
    translation_text: str,
    *,
    policy: AlignmentPolicy,
    contract_mode: str = LEGACY_POSITIONAL,
    marker_findings: tuple[MarkerFinding, ...] = (),
) -> AlignmentAssessment:
    """Assess one translation's paragraph alignment against its source.

    Marker findings, when the caller has them, are carried into the same report unchanged: they
    are the strongest available statement about paragraph identity, and separating them from the
    measurements that depend on that identity would let a consumer read one without the other.
    A block-count difference makes every measurement meaningless, so it is reported as
    unevaluable rather than measured — the structural validator already blocks on that difference
    and this layer does not need to block on it twice.
    """

    findings = [_from_marker_finding(finding) for finding in marker_findings]
    source_blocks = source_paragraphs(source_text)
    target_blocks = source_paragraphs(translation_text)
    if not source_blocks or len(source_blocks) != len(target_blocks):
        findings.append(
            AlignmentFinding(
                code=ALIGNMENT_NOT_EVALUABLE,
                severity=policy.severity_for(ALIGNMENT_NOT_EVALUABLE),
                source_paragraph_index=None,
                related_paragraph_index=None,
                expected_marker=None,
                observed_marker=None,
                message=(
                    f"source carries {len(source_blocks)} paragraphs and the translation"
                    f" {len(target_blocks)}, so no pair can be measured"
                ),
            )
        )
        return AlignmentAssessment(policy, contract_mode, False, (), tuple(findings))
    metrics = tuple(
        measure_pair(index, source, target)
        for index, (source, target) in enumerate(
            zip(source_blocks, target_blocks, strict=True), start=1
        )
    )
    findings.extend(_ratio_findings(metrics, policy))
    findings.extend(_drift_findings(metrics, policy))
    findings.extend(_content_findings(metrics, policy))
    return AlignmentAssessment(policy, contract_mode, True, metrics, tuple(findings))
