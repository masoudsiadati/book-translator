"""Synthetic benchmark for deterministic translation snapshot manifests."""

import hashlib
import json
import statistics
import time
import tracemalloc
from pathlib import Path

import yaml

from book_translator.models import (
    SnapshotBookReference,
    SnapshotChapterReference,
    SnapshotChunkReference,
    SnapshotResourceKind,
    SnapshotResourceReference,
    TocKind,
    TranslationManifest,
)
from book_translator.translation_snapshot.identity import canonical_manifest_bytes

CHAPTER_COUNT = 60
CHUNK_COUNT = 400
ITERATIONS = 3


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_manifest() -> TranslationManifest:
    chapters: list[SnapshotChapterReference] = []
    remaining = CHUNK_COUNT
    sequence_base = 0
    for chapter_number in range(1, CHAPTER_COUNT + 1):
        chapter_id = f"{chapter_number:03d}"
        count = remaining // (CHAPTER_COUNT - chapter_number + 1)
        remaining -= count
        chunks = []
        for sequence in range(1, count + 1):
            sequence_base += 1
            chunk_id = f"{chapter_id}-{sequence:03d}"
            base = f"workspace/chapters/{chapter_id}/chunks/{sequence:03d}"
            chunks.append(
                SnapshotChunkReference(
                    chunk_id=chunk_id,
                    chapter_id=chapter_id,
                    sequence=sequence,
                    source_file=Path(f"{base}/source.md"),
                    source_sha256=_digest(f"source-{sequence_base}"),
                    source_word_count=1800,
                    source_paragraph_count=30,
                    context_file=Path(f"{base}/context.md"),
                    context_sha256=_digest(f"context-{sequence_base}"),
                    context_word_count=250 if sequence > 1 else 0,
                    oversize=False,
                )
            )
        chapters.append(
            SnapshotChapterReference(
                chapter_id=chapter_id,
                kind=TocKind.CHAPTER,
                number=chapter_number,
                title_en=f"Synthetic Chapter {chapter_number}",
                title_fa="",
                cleaned_file=Path(f"workspace/chapters/{chapter_id}/cleaned.md"),
                cleaned_sha256=_digest(f"cleaned-{chapter_number}"),
                normalization_version="1.0",
                chunk_engine_version="1.0.0",
                chunk_count=count,
                chunks=chunks,
            )
        )

    def resource(kind: SnapshotResourceKind, name: str) -> SnapshotResourceReference:
        return SnapshotResourceReference(
            resource_kind=kind,
            file=Path(f"config/{name}"),
            sha256=_digest(name),
            version="1.0.0",
        )

    return TranslationManifest(
        schema_version="1.0",
        workflow="document-translation",
        selection_mode="full",
        book=SnapshotBookReference(
            book_id="synthetic-book",
            title_en="Synthetic Book",
            title_fa="کتاب ساختگی",
            source_language="en",
            target_language="fa",
            source_pdf=Path("input/synthetic.pdf"),
            source_pdf_sha256=_digest("pdf"),
            toc_file=Path("workspace/toc.yaml"),
            toc_sha256=_digest("toc"),
        ),
        chapters=chapters,
        prompt=resource(SnapshotResourceKind.PROMPT, "translation-prompt.md"),
        glossary=resource(SnapshotResourceKind.GLOSSARY, "glossary.yaml"),
        style_guide=resource(SnapshotResourceKind.STYLE_GUIDE, "style-guide.md"),
    )


def main() -> None:
    elapsed: list[float] = []
    hashes: list[str] = []
    peak_memory: list[int] = []
    yaml_bytes = 0
    canonical_bytes = 0
    for _ in range(ITERATIONS):
        tracemalloc.start()
        started = time.perf_counter()
        manifest = build_manifest()
        canonical = canonical_manifest_bytes(manifest)
        digest = hashlib.sha256(canonical).hexdigest()
        rendered = yaml.safe_dump(
            manifest.model_dump(mode="json"), allow_unicode=True, sort_keys=False
        ).encode("utf-8")
        TranslationManifest.model_validate_json(json.dumps(manifest.model_dump(mode="json")))
        elapsed.append(time.perf_counter() - started)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        hashes.append(digest)
        peak_memory.append(peak)
        yaml_bytes = len(rendered)
        canonical_bytes = len(canonical)
    if len(set(hashes)) != 1:
        raise RuntimeError("translation snapshot benchmark is nondeterministic")
    print(f"chapters: {CHAPTER_COUNT}")
    print(f"chunks: {CHUNK_COUNT}")
    print(f"canonical bytes: {canonical_bytes}")
    print(f"YAML bytes: {yaml_bytes}")
    print(f"iterations: {ITERATIONS}")
    print(f"elapsed seconds: {', '.join(f'{value:.6f}' for value in elapsed)}")
    print(f"median seconds: {statistics.median(elapsed):.6f}")
    print(f"peak traced bytes: {max(peak_memory)}")
    print(f"manifest SHA-256: {hashes[0]}")


if __name__ == "__main__":
    main()
