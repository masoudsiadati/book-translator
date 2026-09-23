#!/usr/bin/env python3
"""Benchmark provider-free Phase 4 planning and governance with synthetic prose."""

import hashlib
import json
import time
import tracemalloc
from dataclasses import dataclass

from book_translator.literary_editing.engine import (
    ChapterParagraph,
    assess_mechanical_candidate,
    measure_diff,
    merge_editor_responses,
    plan_sections,
)
from book_translator.literary_editing.parsing import EDITOR_SCHEMA_VERSION, parse_editor_response

ITERATIONS = 3
CHAPTERS = 5
PARAGRAPHS_PER_CHAPTER = 80


@dataclass(frozen=True, slots=True)
class Measurement:
    elapsed_seconds: float
    paragraph_count: int
    input_characters: int
    stable_hash: str


def _paragraph(chapter: int, index: int) -> ChapterParagraph:
    repeated = " ".join(f"synthetic{number}" for number in range(18 + index % 10))
    return ChapterParagraph(
        paragraph_id=f"P{index:04d}",
        paragraph_index=index,
        chunk_id=f"{chapter:03d}-{(index - 1) // 25 + 1:03d}",
        chunk_paragraph_index=(index - 1) % 25 + 1,
        source_text=f"Synthetic chapter {chapter} paragraph {index}: {repeated}.",
        faithful_text=f"بند ساختگی فصل {chapter} شمارهٔ {index}: متن آزمایشی پایدار و روشن است.",
    )


def run_once() -> Measurement:
    records: list[dict[str, object]] = []
    input_characters = 0
    started = time.perf_counter()
    for chapter in range(1, CHAPTERS + 1):
        paragraphs = tuple(
            _paragraph(chapter, index) for index in range(1, PARAGRAPHS_PER_CHAPTER + 1)
        )
        sections = plan_sections(paragraphs)
        responses = []
        for section in sections:
            editable = tuple(paragraphs[index] for index in section.editable_indexes)
            response_text = json.dumps(
                {
                    "schema_version": "1.0",
                    "editor_schema_version": EDITOR_SCHEMA_VERSION,
                    "paragraphs": [
                        {
                            "paragraph_id": item.paragraph_id,
                            "edited_text": (
                                item.faithful_text.replace("پایدار و روشن", "روان و روشن")
                                if item.paragraph_index % 2
                                else item.faithful_text
                            ),
                            "changed": bool(item.paragraph_index % 2),
                            "reasons": (["rhythm"] if item.paragraph_index % 2 else ["no_change"]),
                        }
                        for item in editable
                    ],
                    "multi_paragraph_edit_requests": [],
                },
                ensure_ascii=False,
            )
            responses.append(
                parse_editor_response(
                    response_text,
                    expected_ids=tuple(item.paragraph_id for item in editable),
                    faithful_texts=tuple(item.faithful_text for item in editable),
                )
            )
        values, _, governed = merge_editor_responses(paragraphs, sections, tuple(responses))
        mechanics = assess_mechanical_candidate(paragraphs, values, registry=None)
        diffs = [
            measure_diff(item.faithful_text, value)
            for item, value in zip(paragraphs, values, strict=True)
        ]
        input_characters += sum(
            len(item.source_text) + len(item.faithful_text) for item in paragraphs
        )
        records.append(
            {
                "chapter": chapter,
                "sections": len(sections),
                "governed": sorted(governed),
                "marker_valid": mechanics.marker_identity_preserved,
                "structural_valid": mechanics.structural_report.valid,
                "alignment_valid": mechanics.alignment_report.valid,
                "diff_classes": [item.diff_class.value for item in diffs],
                "candidate_hash": hashlib.sha256("\n\n".join(values).encode()).hexdigest(),
            }
        )
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return Measurement(
        elapsed_seconds=time.perf_counter() - started,
        paragraph_count=CHAPTERS * PARAGRAPHS_PER_CHAPTER,
        input_characters=input_characters,
        stable_hash=hashlib.sha256(encoded).hexdigest(),
    )


def main() -> None:
    tracemalloc.start()
    results = [run_once() for _ in range(ITERATIONS)]
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    hashes = {result.stable_hash for result in results}
    if len(hashes) != 1:
        raise SystemExit("literary-editing benchmark was nondeterministic")
    fastest = min(result.elapsed_seconds for result in results)
    print(
        json.dumps(
            {
                "iterations": ITERATIONS,
                "chapters": CHAPTERS,
                "paragraphs": results[0].paragraph_count,
                "input_characters": results[0].input_characters,
                "elapsed_seconds": [round(item.elapsed_seconds, 6) for item in results],
                "best_paragraphs_per_second": round(results[0].paragraph_count / fastest, 2),
                "peak_traced_bytes": peak,
                "deterministic_governance_sha256": results[0].stable_hash,
            },
            indent=2,
        )
        + "\n",
        end="",
    )


if __name__ == "__main__":
    main()
