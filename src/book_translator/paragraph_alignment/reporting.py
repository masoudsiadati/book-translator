"""Deterministic construction of the persisted alignment report from a pure assessment."""

import hashlib

from book_translator.models import SCHEMA_VERSION
from book_translator.models.paragraph_alignment import (
    ParagraphAlignmentFinding,
    TranslationAlignmentReport,
)
from book_translator.paragraph_alignment.engine import AlignmentAssessment
from book_translator.paragraph_alignment.policy import ALIGNMENT_QA_VERSION
from book_translator.paragraph_markers.engine import PARAGRAPH_MARKER_CONTRACT_VERSION

__all__ = ["build_alignment_report"]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_alignment_report(
    assessment: AlignmentAssessment, *, source_text: str, translation_text: str
) -> TranslationAlignmentReport:
    """Render one assessment as the persisted contract, hash-linked to the texts it judged."""
    marked = assessment.contract_mode == "marked"
    return TranslationAlignmentReport(
        schema_version=SCHEMA_VERSION,
        alignment_qa_version=ALIGNMENT_QA_VERSION,
        policy_id=assessment.policy.policy_id,
        policy_version=assessment.policy.policy_version,
        contract_mode="marked" if marked else "legacy_positional",
        marker_contract_version=PARAGRAPH_MARKER_CONTRACT_VERSION if marked else None,
        evaluated=assessment.evaluated,
        valid=assessment.valid,
        source_sha256=_hash(source_text),
        translation_sha256=_hash(translation_text),
        aligned_pairs=len(assessment.metrics),
        measured_pairs=assessment.measured_pairs,
        flagged_pairs=assessment.flagged_pairs,
        findings=[
            ParagraphAlignmentFinding(
                code=finding.code,
                severity="error" if finding.severity == "error" else "warning",
                source_paragraph_index=finding.source_paragraph_index,
                related_paragraph_index=finding.related_paragraph_index,
                expected_marker=finding.expected_marker,
                observed_marker=finding.observed_marker,
                message=finding.message,
            )
            for finding in assessment.findings
        ],
    )
