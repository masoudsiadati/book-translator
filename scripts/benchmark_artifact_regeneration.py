"""Synthetic benchmark for read-only mutable-artifact staleness planning."""

import hashlib
import json
import statistics
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

import yaml

from book_translator.artifact_regeneration import plan_artifact_regeneration
from book_translator.document_chunking import ChunkingConfig
from book_translator.document_chunking.workflow import (
    execute_chapter_chunking,
    plan_chapter_chunking,
)
from book_translator.models import ChapterMetadata, ChapterStatus, TocKind
from book_translator.normalization.workflow import execute_normalization, plan_normalization

CHAPTER_COUNT = 12
BLOCKS_PER_CHAPTER = 80
ITERATIONS = 3
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _resource(root: Path, stem: str, kind: str, content: str) -> None:
    suffix = ".yaml" if stem == "glossary" else ".md"
    path = root / "config" / f"{stem}{suffix}"
    path.write_text(content, encoding="utf-8")
    _write_yaml(
        path.with_suffix(".metadata.yaml"),
        {
            "schema_version": "1.0",
            "resource_kind": kind,
            "resource_file": path.relative_to(root).as_posix(),
            "version": "1.0.0",
        },
    )


def _source(number: int) -> str:
    blocks = []
    for index in range(BLOCKS_PER_CHAPTER):
        blocks.append(
            f"Synthetic chapter {number} block {index} contains deterministic words\n"
            "and a wrapped continuation for local planning only."
        )
    return "<!-- source-page: 1 -->\n\n" + "\n\n".join(blocks) + "\n"


def _project(root: Path) -> None:
    (root / "config").mkdir()
    (root / "input").mkdir()
    pdf = root / "input/book.pdf"
    pdf.write_bytes(b"synthetic-benchmark-pdf")
    pdf_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (root / "config/book.yaml").write_text(
        """book_id: synthetic-benchmark
title_en: Synthetic Benchmark
title_fa: معیار ساختگی
author: Synthetic Author
source_language: en
target_language: fa
pdf_path: input/book.pdf
chunk_target_words: 240
chunk_max_words: 320
previous_context_words: 40
output_format: epub3
""",
        encoding="utf-8",
    )
    _resource(root, "translation-prompt", "prompt", "Synthetic prompt.\n")
    _resource(
        root,
        "glossary",
        "glossary",
        "terms: {}\ncharacters: {}\nplaces: {}\ngroups: {}\nunresolved_terms: []\n",
    )
    _resource(root, "style-guide", "style_guide", "Synthetic style.\n")
    entries = []
    for number in range(1, CHAPTER_COUNT + 1):
        chapter_id = f"{number:03d}"
        directory = root / "workspace/chapters" / chapter_id
        directory.mkdir(parents=True)
        source = _source(number)
        source_path = directory / "source.md"
        source_path.write_text(source, encoding="utf-8")
        metadata = ChapterMetadata(
            schema_version="1.0",
            chapter_id=chapter_id,
            kind=TocKind.CHAPTER,
            number=number,
            title_en=f"Synthetic {number}",
            source_start_page=1,
            source_end_page=1,
            source_file=Path(f"workspace/chapters/{chapter_id}/source.md"),
            source_sha256=hashlib.sha256(source.encode()).hexdigest(),
            cleaned_file=Path(f"workspace/chapters/{chapter_id}/cleaned.md"),
            source_word_count=len(source.split()),
            source_paragraph_count=BLOCKS_PER_CHAPTER,
            status=ChapterStatus.EXTRACTED,
            extraction_version="1.0",
            normalization_version="",
            created_at=NOW,
            updated_at=NOW,
        )
        _write_yaml(directory / "metadata.yaml", metadata)
        entries.append(
            {
                "entry_id": f"chapter-{chapter_id}",
                "level": 1,
                "kind": "chapter",
                "number": number,
                "title_en": f"Synthetic {number}",
                "title_fa": "",
                "symbol": None,
                "start_page": 1,
                "end_page": 1,
                "source_toc_title": f"Chapter {number}",
            }
        )
    _write_yaml(
        root / "workspace/toc.yaml",
        {
            "schema_version": "1.0",
            "source": "embedded",
            "source_pdf": "input/book.pdf",
            "source_pdf_sha256": pdf_hash,
            "pdf_page_count": 1,
            "entry_count": len(entries),
            "entries": entries,
        },
    )
    execute_normalization(
        plan_normalization(project_root=root, normalization_version="1.0", now=lambda: NOW),
        now=lambda: NOW,
    )
    execute_chapter_chunking(
        plan_chapter_chunking(
            project_root=root,
            config=ChunkingConfig(240, 320, 40),
            now=lambda: NOW,
        )
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="book-translator-regeneration-benchmark-") as raw:
        root = Path(raw)
        _project(root)
        selectors = [str(number) for number in range(1, CHAPTER_COUNT + 1)]
        timings: list[float] = []
        peaks: list[int] = []
        digests: list[str] = []
        for _ in range(ITERATIONS):
            tracemalloc.start()
            started = time.perf_counter()
            plan = plan_artifact_regeneration(
                project_root=root, selectors=selectors, now=lambda: NOW
            )
            timings.append(time.perf_counter() - started)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            payload = [
                {
                    "chapter": item.metadata.chapter_id,
                    "action": item.action,
                    "cleaned": item.planned_cleaned_sha256,
                    "chunks": [
                        (chunk.metadata.source_sha256, chunk.metadata.context_sha256)
                        for chunk in item.chunk_artifacts
                    ],
                }
                for item in plan.chapters
            ]
            digests.append(
                hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )
            peaks.append(peak)
        if len(set(digests)) != 1:
            raise RuntimeError("regeneration planning was not deterministic")
        source_bytes = sum(
            path.stat().st_size for path in root.glob("workspace/chapters/*/source.md")
        )
        print(f"chapters={CHAPTER_COUNT}")
        print(f"blocks={CHAPTER_COUNT * BLOCKS_PER_CHAPTER}")
        print(f"source_bytes={source_bytes}")
        print(f"timings_seconds={','.join(f'{value:.6f}' for value in timings)}")
        print(f"median_seconds={statistics.median(timings):.6f}")
        print(f"peak_traced_bytes={max(peaks)}")
        print(f"plan_sha256={digests[0]}")


if __name__ == "__main__":
    main()
