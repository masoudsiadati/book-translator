"""Deterministic construction of the persisted structural-repair records."""

from book_translator.models import (
    StructuralOperationRecord,
    StructuralRepairDiagnosis,
)
from book_translator.structural_repair.engine import (
    STRUCTURAL_REPAIR_ENGINE_VERSION,
    StructuralDiagnosis,
)


def diagnosis_record(diagnosis: StructuralDiagnosis) -> StructuralRepairDiagnosis:
    """Reduce one structural diagnosis to its persisted form.

    Only the non-identity groups are recorded. A reader re-deriving the repair needs to know
    where the structure disagreed, and listing every matching line beside them would bury that
    in a list as long as the chunk.
    """
    return StructuralRepairDiagnosis(
        structural_repair_engine_version=STRUCTURAL_REPAIR_ENGINE_VERSION,
        defect_class=diagnosis.defect_class,
        source_paragraphs=diagnosis.source_paragraphs,
        translation_paragraphs=diagnosis.translation_paragraphs,
        paragraph_delta=diagnosis.paragraph_delta,
        source_lines=diagnosis.source_lines,
        translation_lines=diagnosis.translation_lines,
        line_delta=diagnosis.line_delta,
        blank_line_artifact=diagnosis.blank_line_artifact,
        deterministic=diagnosis.deterministic,
        alignment_cost_milli=diagnosis.alignment_cost_milli,
        alignment_margin_milli=diagnosis.alignment_margin_milli,
        operations=[
            StructuralOperationRecord(
                kind=group.kind,
                source_index=group.source_index,
                source_lines=group.source_lines,
                translation_index=group.translation_index,
                translation_lines=group.translation_lines,
                cost_milli=max(0, group.cost_milli),
            )
            for group in diagnosis.operations
        ],
        refusals=list(diagnosis.refusals),
    )


__all__ = ["diagnosis_record"]
