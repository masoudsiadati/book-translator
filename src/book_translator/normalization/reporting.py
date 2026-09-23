"""Small, prose-free normalization report construction and YAML serialization."""

from datetime import datetime
from typing import Any

import yaml

from book_translator.normalization.models import NormalizationResult

SCHEMA_VERSION = "1.0"


def build_report(
    *,
    chapter_id: str,
    source_sha256: str,
    cleaned_sha256: str,
    normalization_version: str,
    result: NormalizationResult,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "source_sha256": source_sha256,
        "cleaned_sha256": cleaned_sha256,
        "normalization_version": normalization_version,
        "passes_run": list(result.passes_run),
        "source_physical_line_count": result.document.source_physical_line_count,
        "cleaned_paragraph_count": len(result.document.blocks),
        "page_markers_removed": result.page_markers_removed,
        "page_continuation_joins": result.page_continuation_joins,
        "page_transitions": list(result.document.page_transitions),
        "wrapped_line_joins": result.wrapped_line_joins,
        "hyphen_repairs": result.hyphen_repairs,
        "ambiguous_blocks": result.ambiguous_blocks,
        "warnings": list(result.warnings),
        "changes": [
            {
                key: value
                for key, value in {
                    "pass_name": change.pass_name,
                    "source_page": change.source_page,
                    "block_index": change.block_index,
                    "line_index": change.line_index,
                    "change_type": change.change_type,
                    "description": change.description,
                }.items()
                if value is not None
            }
            for change in result.changes
        ],
        "created_at": created_at.isoformat(),
    }


def serialize_yaml(data: dict[str, Any]) -> str:
    rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    if not isinstance(rendered, str):
        raise ValueError("could not serialize normalization report")
    return rendered
