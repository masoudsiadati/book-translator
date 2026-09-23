#!/usr/bin/env python3
"""Benchmark prompt-package planning with generated synthetic material only."""

import hashlib
import json
import time
import tracemalloc
from dataclasses import dataclass

import yaml

from book_translator.prompt_packaging import package_identity
from book_translator.prompt_packaging.rendering import (
    estimate_tokens,
    extract_framed_bytes,
    render_messages,
)

ITERATIONS = 3
JOB_COUNT = 400
CHAPTER_COUNT = 60
PROMPT = (
    "Chapter {{ chapter_number }} part {{ part_number }}\n"
    "Glossary {{ glossary }}\nContext {{ previous_context }}\nSource {{ source_text }}\n"
)


@dataclass(frozen=True)
class Measurement:
    elapsed_seconds: float
    package_count: int
    input_bytes: int
    estimated_tokens: int
    plan_hash: str


def synthetic_glossary() -> dict[str, object]:
    return {
        "terms": {f"Term {index:04d}": f"برابر {index:04d}" for index in range(500)},
        "characters": {f"Character {index:03d}": f"شخصیت {index:03d}" for index in range(80)},
        "places": {f"Place {index:03d}": f"مکان {index:03d}" for index in range(40)},
        "groups": {f"Group {index:03d}": f"گروه {index:03d}" for index in range(20)},
        "unresolved_terms": [f"Unknown {index:03d}" for index in range(20)],
    }


def run_once() -> Measurement:
    glossary = synthetic_glossary()
    style = ("Synthetic style rule.\n" * 200).encode()
    aggregate: list[dict[str, object]] = []
    total_input = 0
    total_tokens = 0
    started = time.perf_counter()
    for index in range(JOB_COUNT):
        chapter = index % CHAPTER_COUNT + 1
        source = ((f"Synthetic paragraph {index}. " * (20 + index % 60)).strip() + "\n").encode()
        context = (
            (f"Context {index}. " * (index % 20)).strip() + ("\n" if index % 20 else "")
        ).encode()
        rendered = render_messages(
            prompt_template=PROMPT,
            glossary=glossary,
            style_guide_bytes=style,
            context_bytes=context,
            source_bytes=source,
            chapter_id=f"{chapter:03d}",
            chapter_number=chapter,
            chapter_title=f"Synthetic Chapter {chapter}",
            chunk_sequence=index // CHAPTER_COUNT + 1,
            total_chunks=7,
            source_language="en",
            target_language="fa",
        )
        assert extract_framed_bytes(rendered.user_text, "source-text") == source
        assert extract_framed_bytes(rendered.user_text, "previous-context") == context
        system_hash = hashlib.sha256(rendered.system_text.encode()).hexdigest()
        user_hash = hashlib.sha256(rendered.user_text.encode()).hexdigest()
        identity = {
            "renderer_version": "1.0.0",
            "run_id": "tr-synthetic-001",
            "job_id": f"job-{index:04d}",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "context_sha256": hashlib.sha256(context).hexdigest(),
            "system_sha256": system_hash,
            "user_sha256": user_hash,
        }
        package_id, manifest_hash = package_identity(identity)
        record = {"package_id": package_id, "manifest_sha256": manifest_hash, **identity}
        yaml.safe_dump(record, allow_unicode=True, sort_keys=False)
        aggregate.append(record)
        total_input += len(source) + len(context)
        total_tokens += estimate_tokens(len(rendered.system_text) + len(rendered.user_text))
    payload = json.dumps(aggregate, sort_keys=True, separators=(",", ":")).encode()
    return Measurement(
        elapsed_seconds=time.perf_counter() - started,
        package_count=JOB_COUNT,
        input_bytes=total_input,
        estimated_tokens=total_tokens,
        plan_hash=hashlib.sha256(payload).hexdigest(),
    )


def main() -> None:
    tracemalloc.start()
    results = [run_once() for _ in range(ITERATIONS)]
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    hashes = {result.plan_hash for result in results}
    if len(hashes) != 1:
        raise SystemExit("prompt-package benchmark was nondeterministic")
    fastest = min(result.elapsed_seconds for result in results)
    report = {
        "iterations": ITERATIONS,
        "chapters": CHAPTER_COUNT,
        "jobs": JOB_COUNT,
        "input_bytes": results[0].input_bytes,
        "estimated_input_tokens": results[0].estimated_tokens,
        "elapsed_seconds": [round(result.elapsed_seconds, 6) for result in results],
        "best_packages_per_second": round(JOB_COUNT / fastest, 2),
        "peak_traced_bytes": peak,
        "deterministic_plan_sha256": results[0].plan_hash,
    }
    print(yaml.safe_dump(report, sort_keys=False), end="")


if __name__ == "__main__":
    main()
