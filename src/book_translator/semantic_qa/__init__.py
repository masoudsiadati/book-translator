"""The provider-backed Semantic Coverage Judge."""

from book_translator.semantic_qa.engine import (
    CONTEXT_RADIUS,
    JudgeBatch,
    ParagraphTask,
    SemanticTaskError,
    aligned_paragraphs,
    build_batches,
    build_tasks,
    paragraph_identity,
)
from book_translator.semantic_qa.parsing import SemanticResponseError, parse_judge_response
from book_translator.semantic_qa.policy import (
    EN_FA_JUDGE_V1,
    SEMANTIC_COVERAGE_POLICY_VERSION,
    SemanticPolicy,
    apply_policy,
)
from book_translator.semantic_qa.providers import (
    JUDGE_OUTPUT_CONTRACT,
    FakeSemanticJudgeProvider,
    JudgeCall,
    SemanticJudgeError,
    TransportSemanticJudge,
    judge_request_id,
    passing_response,
)
from book_translator.semantic_qa.rendering import (
    JUDGE_PROMPT_VERSION,
    JUDGE_SCHEMA_VERSION,
    SYSTEM_MESSAGE,
    render_user_message,
)
from book_translator.semantic_qa.workflow import (
    SEMANTIC_JUDGE_ENGINE_VERSION,
    ChunkSelection,
    SemanticCoverageError,
    SemanticCoveragePlan,
    execute_semantic_coverage,
    plan_semantic_coverage,
)

__all__ = [
    "CONTEXT_RADIUS",
    "EN_FA_JUDGE_V1",
    "JUDGE_OUTPUT_CONTRACT",
    "JUDGE_PROMPT_VERSION",
    "JUDGE_SCHEMA_VERSION",
    "SEMANTIC_COVERAGE_POLICY_VERSION",
    "SEMANTIC_JUDGE_ENGINE_VERSION",
    "SYSTEM_MESSAGE",
    "ChunkSelection",
    "FakeSemanticJudgeProvider",
    "JudgeBatch",
    "JudgeCall",
    "ParagraphTask",
    "SemanticCoverageError",
    "SemanticCoveragePlan",
    "SemanticJudgeError",
    "SemanticPolicy",
    "SemanticResponseError",
    "SemanticTaskError",
    "TransportSemanticJudge",
    "aligned_paragraphs",
    "apply_policy",
    "build_batches",
    "build_tasks",
    "execute_semantic_coverage",
    "judge_request_id",
    "paragraph_identity",
    "parse_judge_response",
    "passing_response",
    "plan_semantic_coverage",
    "render_user_message",
]
