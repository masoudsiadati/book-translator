"""Final QA resolution for a completed translation run.

The stage answers one question — why does a finished book still hold QA rejections, and which of
them may be resolved without a person — and keeps the four things that answer it apart:
``analysis`` classifies the evidence, ``policy`` decides what each class permits, ``workflow``
performs the ordered I/O, and the CLI renders it.
"""

from book_translator.final_qa.analysis import (
    FINAL_QA_ANALYSIS_VERSION,
    CompetingVariantAnalysis,
    ParagraphDiagnosis,
    analyze_competing_variants,
    diagnose_paragraphs,
)
from book_translator.final_qa.policy import (
    FINAL_QA_RESOLUTION_POLICY_VERSION,
    FinalQAAssessment,
    assess_final_qa,
    classify_competing_variant,
)
from book_translator.final_qa.workflow import (
    DEFAULT_MAX_CYCLES,
    FINAL_QA_AUDIT_ENGINE_VERSION,
    FINAL_QA_ORCHESTRATOR_VERSION,
    MAX_ESCALATED_REPAIR_ATTEMPTS,
    REPAIR_ESCALATION_VERSION,
    FinalizationResult,
    FinalQAAuditPlan,
    FinalQAError,
    JobDiagnosis,
    execute_final_qa_audit,
    finalize_translation_run,
    operator_required_terminology,
    plan_final_qa_audit,
)

__all__ = [
    "DEFAULT_MAX_CYCLES",
    "FINAL_QA_ANALYSIS_VERSION",
    "FINAL_QA_AUDIT_ENGINE_VERSION",
    "FINAL_QA_ORCHESTRATOR_VERSION",
    "FINAL_QA_RESOLUTION_POLICY_VERSION",
    "MAX_ESCALATED_REPAIR_ATTEMPTS",
    "REPAIR_ESCALATION_VERSION",
    "CompetingVariantAnalysis",
    "FinalQAAssessment",
    "FinalQAAuditPlan",
    "FinalQAError",
    "FinalizationResult",
    "JobDiagnosis",
    "ParagraphDiagnosis",
    "analyze_competing_variants",
    "assess_final_qa",
    "classify_competing_variant",
    "diagnose_paragraphs",
    "execute_final_qa_audit",
    "finalize_translation_run",
    "operator_required_terminology",
    "plan_final_qa_audit",
]
