"""Read-only semantic inspection of a planned normalization generation."""

import hashlib
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

import yaml

from book_translator.artifact_regeneration import ChapterRegenerationPlan
from book_translator.document_chunking.paragraphs import parse_document
from book_translator.normalization.markers import parse_source
from book_translator.normalization.models import ChangeRecord, TextBlock

ChangeKind = Literal[
    "paragraph_join", "paragraph_split", "text_rewrite", "whitespace_only", "metadata_only"
]


@dataclass(frozen=True, slots=True)
class NormalizationChangeRegion:
    kind: ChangeKind
    before: tuple[str, ...]
    after: tuple[str, ...]
    pass_name: str
    rule: str
    pages: tuple[int, ...]
    blocks: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class ChunkHashChange:
    sequence: int
    source: bool
    context: bool


@dataclass(frozen=True, slots=True)
class NormalizationChangeInspection:
    chapter_id: str
    current_version: str
    planned_version: str
    current_sha256: str
    planned_sha256: str
    bytes_change: bool
    regions: tuple[NormalizationChangeRegion, ...]
    chunk_hash_changes: tuple[ChunkHashChange, ...]
    planned_bytes: bytes


def _paragraphs(text: str) -> tuple[str, ...]:
    return tuple(item.text for item in parse_document(text))


def _kind(before: tuple[str, ...], after: tuple[str, ...]) -> ChangeKind:
    if len(before) > 1 and len(after) == 1:
        return "paragraph_join"
    if len(before) == 1 and len(after) > 1:
        return "paragraph_split"
    if "".join(before).split() == "".join(after).split():
        return "whitespace_only"
    return "text_rewrite"


def _provenance(
    block: TextBlock | None,
    changes: tuple[ChangeRecord, ...],
    source_blocks: dict[tuple[int, int], TextBlock],
) -> tuple[tuple[int, ...], tuple[tuple[int, int], ...], str, str]:
    if block is None:
        return (), (), "paragraph-rendering", "normalization-output"
    relevant: list[ChangeRecord] = []
    for change in changes:
        if change.pass_name != "page-boundary-continuations":
            continue
        if change.source_page is None or change.block_index is None:
            continue
        source = source_blocks.get((change.source_page, change.block_index))
        if source is not None and source.original_text in block.original_text:
            relevant.append(change)
    pages = {block.source_page, *(item.source_page for item in relevant if item.source_page)}
    blocks = {(block.source_page, block.block_index)}
    blocks.update(
        (item.source_page, item.block_index)
        for item in relevant
        if item.source_page is not None and item.block_index is not None
    )
    pass_name = relevant[0].pass_name if relevant else "paragraph-rendering"
    rule = relevant[0].description if relevant else "normalization-output"
    return tuple(sorted(pages)), tuple(sorted(blocks)), pass_name, rule


def _existing_chunk_hashes(path: Path) -> dict[int, tuple[str, str | None]]:
    hashes: dict[int, tuple[str, str | None]] = {}
    if not path.is_dir():
        return hashes
    for metadata_path in sorted(path.glob("*/metadata.yaml")):
        value = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("sequence"), int):
            source_hash = value.get("source_sha256")
            if isinstance(source_hash, str):
                context_hash = value.get("context_sha256")
                hashes[value["sequence"]] = (
                    source_hash,
                    context_hash if isinstance(context_hash, str) else None,
                )
    return hashes


def inspect_normalization_changes(
    chapter: ChapterRegenerationPlan,
) -> NormalizationChangeInspection:
    """Compare canonical cleaned bytes with the planner's exact in-memory candidate."""
    current_bytes = chapter.paths.cleaned.read_bytes()
    current_text = current_bytes.decode("utf-8")
    planned_text = chapter.normalization_result.cleaned_text
    planned_bytes = planned_text.encode("utf-8")
    before = _paragraphs(current_text)
    after = _paragraphs(planned_text)
    source_document, _ = parse_source(chapter.paths.source.read_text(encoding="utf-8"))
    source_blocks = {
        (block.source_page, block.block_index): block for block in source_document.blocks
    }
    regions: list[NormalizationChangeRegion] = []
    matcher = SequenceMatcher(a=before, b=after, autojunk=False)
    for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        old_region = before[old_start:old_end]
        new_region = after[new_start:new_end]
        planned_block = (
            chapter.normalization_result.document.blocks[new_start]
            if new_start < len(chapter.normalization_result.document.blocks)
            else None
        )
        pages, blocks, pass_name, rule = _provenance(
            planned_block, chapter.normalization_result.changes, source_blocks
        )
        regions.append(
            NormalizationChangeRegion(
                kind=_kind(old_region, new_region),
                before=old_region,
                after=new_region,
                pass_name=pass_name,
                rule=rule,
                pages=pages,
                blocks=blocks,
            )
        )
    existing_chunks = _existing_chunk_hashes(chapter.paths.chunks)
    changed_chunks = tuple(
        ChunkHashChange(
            artifact.metadata.sequence,
            existing_chunks.get(artifact.metadata.sequence, (None, None))[0]
            != artifact.metadata.source_sha256,
            existing_chunks.get(artifact.metadata.sequence, (None, None))[1]
            != artifact.metadata.context_sha256,
        )
        for artifact in chapter.chunk_artifacts
        if existing_chunks.get(artifact.metadata.sequence)
        != (artifact.metadata.source_sha256, artifact.metadata.context_sha256)
    )
    return NormalizationChangeInspection(
        chapter_id=chapter.metadata.chapter_id,
        current_version=chapter.metadata.normalization_version,
        planned_version=chapter.updated_metadata.normalization_version,
        current_sha256=hashlib.sha256(current_bytes).hexdigest(),
        planned_sha256=hashlib.sha256(planned_bytes).hexdigest(),
        bytes_change=current_bytes != planned_bytes,
        regions=tuple(regions),
        chunk_hash_changes=changed_chunks,
        planned_bytes=planned_bytes,
    )
