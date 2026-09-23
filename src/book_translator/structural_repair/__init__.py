"""Provider-free repair of a translation's block structure.

The stage answers one question — is this ``paragraph_count_mismatch`` a misplaced separator or a
difference in content — and keeps the four things that answer it apart: ``engine`` decides and
renders, ``models`` types the plan, ``reporting`` reduces it to the persisted record, and
``workflow`` performs the ordered I/O.
"""

from book_translator.structural_repair.engine import (
    MAX_GROUP_LINES,
    MAX_LINE_DELTA,
    MAX_OPERATIONS,
    MIN_ALIGNMENT_MARGIN,
    STRUCTURAL_REPAIR_ENGINE_VERSION,
    AlignedGroup,
    Line,
    StructuralDiagnosis,
    diagnose_structure,
    lines,
    render_repair,
)
from book_translator.structural_repair.models import (
    StructuralRepairPlan,
    StructuralRepairResult,
)
from book_translator.structural_repair.reporting import diagnosis_record
from book_translator.structural_repair.workflow import (
    StructuralRepairError,
    plan_structural_repair,
    repair_translation_structure,
)

__all__ = [
    "MAX_GROUP_LINES",
    "MAX_LINE_DELTA",
    "MAX_OPERATIONS",
    "MIN_ALIGNMENT_MARGIN",
    "STRUCTURAL_REPAIR_ENGINE_VERSION",
    "AlignedGroup",
    "Line",
    "StructuralDiagnosis",
    "StructuralRepairError",
    "StructuralRepairPlan",
    "StructuralRepairResult",
    "diagnose_structure",
    "diagnosis_record",
    "lines",
    "plan_structural_repair",
    "render_repair",
    "repair_translation_structure",
]
