"""Deterministic identity for one chapter-title artifact.

The identity addresses the *inputs* — the snapshot's chapters and titles, the frozen terminology,
and the engine and prompt versions — not the answer that came back. That is deliberate: a release
has to be able to ask "is there a title artifact for this snapshot" without already knowing what
the titles are, and a second run over the same inputs has to land on the same directory so it
collides with the published answer instead of quietly creating a rival one.

What came back is addressed separately, by ``content_sha256`` over the published titles.
"""

import hashlib
import json
from typing import Any

from book_translator.chapter_titles.engine import (
    CHAPTER_TITLE_ENGINE_VERSION,
    CHAPTER_TITLE_PROMPT_VERSION,
)


def canonical_identity_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        + b"\n"
    )


def chapter_title_identity(
    *,
    book_id: str,
    source_language: str,
    target_language: str,
    snapshot_id: str,
    snapshot_manifest_sha256: str,
    terminology_sha256: str | None,
    chapters: tuple[tuple[str, str, int | None, str], ...],
) -> tuple[str, str]:
    """Return the artifact ID and full digest for one set of translatable chapter titles.

    ``chapters`` is ``(chapter_id, kind, number, title_en)`` in snapshot order.
    """
    payload: dict[str, Any] = {
        "artifact_kind": "chapter-titles",
        "engine_version": CHAPTER_TITLE_ENGINE_VERSION,
        "prompt_version": CHAPTER_TITLE_PROMPT_VERSION,
        "book_id": book_id,
        "source_language": source_language,
        "target_language": target_language,
        "snapshot_id": snapshot_id,
        "snapshot_manifest_sha256": snapshot_manifest_sha256,
        "terminology_sha256": terminology_sha256,
        "chapters": [
            {"chapter_id": chapter_id, "kind": kind, "number": number, "title_en": title}
            for chapter_id, kind, number, title in chapters
        ],
    }
    digest = hashlib.sha256(canonical_identity_bytes(payload)).hexdigest()
    return f"ct-{digest[:24]}", digest


def title_content_address(pairs: tuple[tuple[str, str, str], ...]) -> str:
    """Hash the produced titles themselves: ``(chapter_id, title_en, title_fa)`` in order."""
    payload = [list(item) for item in pairs]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
