"""The one mechanical acceptance battery every accepting boundary runs.

The worker judged a provider draft with terminology, structural (with scoped source anomalies),
and paragraph-alignment QA; the carry-forward boundaries ran only the first two, so a draft carried
into a run could be accepted without the alignment judgement the same draft would face as a fresh
attempt. Keeping the battery here means "accepted" means the same thing whichever boundary said
it. Pure: callers load and hash-verify every input first.
"""

from collections.abc import Callable
from dataclasses import dataclass

from book_translator.models import (
    SourceAnomalyRegistry,
    TerminologyRegistry,
    TerminologyValidationReport,
    TranslationAlignmentReport,
    TranslationQAReport,
)
from book_translator.paragraph_alignment import (
    LEGACY_POSITIONAL,
    MARKED,
    assess_alignment,
    build_alignment_report,
    policy_for,
)
from book_translator.prompt_packaging.identity import uses_paragraph_markers
from book_translator.terminology import validate_terminology, validate_translation_qa
from book_translator.terminology.registry import ExemptOccurrences


@dataclass(frozen=True, slots=True)
class AcceptanceAssessment:
    terminology_report: TerminologyValidationReport
    structural_report: TranslationQAReport
    alignment_report: TranslationAlignmentReport

    @property
    def accepted(self) -> bool:
        return (
            self.terminology_report.valid
            and self.structural_report.valid
            and self.alignment_report.valid
        )


def assess_translation(
    source_text: str,
    translation_text: str,
    *,
    registry: TerminologyRegistry,
    target_language: str,
    output_contract: str | None,
    anomaly_registry: SourceAnomalyRegistry | None = None,
    chapter_id: str | None = None,
    terminology_validator: Callable[..., TerminologyValidationReport] = validate_terminology,
    structural_validator: Callable[..., TranslationQAReport] = validate_translation_qa,
    occurrence_exemptions: ExemptOccurrences | None = None,
) -> AcceptanceAssessment:
    """Judge one draft under the contract it was *produced* against, never current configuration.

    ``terminology_validator`` and ``structural_validator`` exist so a boundary can keep its own
    injection seam; every caller in production passes the installed validators.
    ``occurrence_exemptions`` are the frozen, already-proven exemptions of this exact source.
    """
    terminology_report = (
        terminology_validator(source_text, translation_text, registry)
        if occurrence_exemptions is None
        else terminology_validator(source_text, translation_text, registry, occurrence_exemptions)
    )
    if anomaly_registry is None:
        structural_report = structural_validator(
            source_text, translation_text, target_language=target_language
        )
    else:
        structural_report = structural_validator(
            source_text,
            translation_text,
            target_language=target_language,
            source_anomaly_registry=anomaly_registry,
            chapter_id=chapter_id,
        )
    marked = output_contract is not None and uses_paragraph_markers(output_contract)
    alignment_report = build_alignment_report(
        assess_alignment(
            source_text,
            translation_text,
            policy=policy_for(target_language),
            contract_mode=MARKED if marked else LEGACY_POSITIONAL,
        ),
        source_text=source_text,
        translation_text=translation_text,
    )
    return AcceptanceAssessment(terminology_report, structural_report, alignment_report)
