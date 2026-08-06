"""Prose-free Document Chunk Engine report generation."""

import hashlib
from datetime import datetime
from typing import Any

import yaml

from book_translator.document_chunking.engine import CHUNK_ENGINE_VERSION
from book_translator.document_chunking.models import DocumentChunkPlan

REPORT_SCHEMA_VERSION = "1.0"


def build_chunking_report(
    *, chapter_id: str, cleaned_sha256: str, plan: DocumentChunkPlan, created_at: datetime
) -> dict[str, Any]:
    counts = [chunk.word_count for chunk in plan.chunks]
    reconstructed_hash = hashlib.sha256(plan.reconstructed_text.encode("utf-8")).hexdigest()
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "cleaned_sha256": cleaned_sha256,
        "chunk_engine_version": CHUNK_ENGINE_VERSION,
        "target_words": plan.config.target_words,
        "maximum_words": plan.config.max_words,
        "context_words": plan.config.context_words,
        "minimum_preferred_words": plan.config.minimum_preferred_words,
        "source_paragraph_count": len(plan.paragraphs),
        "source_word_count": plan.source_word_count,
        "chunk_count": len(plan.chunks),
        "normal_chunk_count": len(plan.chunks) - plan.oversize_chunk_count,
        "oversize_chunk_count": plan.oversize_chunk_count,
        "minimum_chunk_words": min(counts),
        "maximum_chunk_words": max(counts),
        "average_chunk_words": round(sum(counts) / len(counts), 3),
        "scene_separators_found": plan.scene_separators_found,
        "scene_boundaries_used": plan.scene_boundaries_used,
        "tail_rebalancing_performed": plan.tail_rebalanced,
        "total_context_words": plan.total_context_words,
        "conservation_validation": True,
        "reconstructed_sha256": reconstructed_hash,
        "warnings": list(plan.warnings),
        "created_at": created_at.isoformat(),
    }


def serialize_yaml(data: dict[str, Any]) -> str:
    rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    if not isinstance(rendered, str):
        raise ValueError("could not serialize chunking YAML")
    return rendered
