"""Bounded semantic revision: a second provider role that corrects only named defects."""

from book_translator.semantic_revision.bounds import (
    RevisionScopeError,
    SplicedRevision,
    measure_bounds,
    splice_window,
)
from book_translator.semantic_revision.policy import (
    MAX_SEMANTIC_REVISIONS_PER_WINDOW,
    SEMANTIC_REVISION_POLICY_VERSION,
    terminology_regressed,
)
from book_translator.semantic_revision.providers import (
    REVISER_OUTPUT_CONTRACT,
    ReviserCall,
    SemanticReviserError,
    TransportSemanticReviser,
)
from book_translator.semantic_revision.rendering import (
    REVISER_SYSTEM_MESSAGE,
    REVISION_PROMPT_VERSION,
    render_revision_request,
)
from book_translator.semantic_revision.targeted import (
    TARGETED_SEMANTIC_REVISION_VERSION,
    TargetedRevisionError,
)

__all__ = [
    "MAX_SEMANTIC_REVISIONS_PER_WINDOW",
    "TARGETED_SEMANTIC_REVISION_VERSION",
    "REVISER_OUTPUT_CONTRACT",
    "REVISER_SYSTEM_MESSAGE",
    "REVISION_PROMPT_VERSION",
    "SEMANTIC_REVISION_POLICY_VERSION",
    "ReviserCall",
    "RevisionScopeError",
    "SemanticReviserError",
    "SplicedRevision",
    "TargetedRevisionError",
    "TransportSemanticReviser",
    "measure_bounds",
    "render_revision_request",
    "splice_window",
    "terminology_regressed",
]
