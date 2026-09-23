#!/usr/bin/env python3
"""Benchmark provider-executor core with synthetic inputs and the fake provider only."""

import hashlib
import json
import time
import tracemalloc
from dataclasses import asdict, dataclass

import yaml

from book_translator.provider_execution.artifacts import canonical_json
from book_translator.provider_execution.protocol import ProviderRequest
from book_translator.provider_execution.providers.fake import FakeTranslationProvider
from book_translator.provider_execution.result_validation import extract_translation

ITERATIONS = 3
JOB_COUNT = 400


@dataclass(frozen=True)
class Measurement:
    elapsed_seconds: float
    serialized_bytes: int
    plan_hash: str


def run_once() -> Measurement:
    provider = FakeTranslationProvider()
    records: list[dict[str, object]] = []
    serialized_bytes = 0
    started = time.perf_counter()
    for index in range(JOB_COUNT):
        request = ProviderRequest(
            request_id=f"preq-{index:024x}",
            idempotency_key=hashlib.sha256(f"job-{index}".encode()).hexdigest(),
            run_id="tr-000000000000-001",
            job_id=f"tr-000000000000-001__001-{index + 1:03d}",
            attempt_number=1,
            system_message="Synthetic system instructions.\n",
            user_message=("Synthetic packaged input. " * (20 + index % 40)) + "\n",
            source_language="en",
            target_language="fa",
            output_contract="persian-translation-text/1.0.0",
            model="fake-v1",
            timeout_seconds=60,
            max_output_tokens=2000,
            metadata={"job": f"job-{index}"},
        )
        response = provider.execute(request)
        translation = extract_translation(response, b"Synthetic source.\n")
        request_bytes = canonical_json(
            {
                "request_id": request.request_id,
                "idempotency_key": request.idempotency_key,
                "model": request.model,
                "system_sha256": hashlib.sha256(request.system_message.encode()).hexdigest(),
                "user_sha256": hashlib.sha256(request.user_message.encode()).hexdigest(),
            }
        )
        record = {
            "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
            "response_id": response.response_id,
            "translation_sha256": hashlib.sha256(translation.content).hexdigest(),
            "usage": asdict(response.usage),
            "status": "succeeded",
        }
        serialized = yaml.safe_dump(record, sort_keys=False)
        serialized_bytes += len(request_bytes) + len(serialized.encode()) + len(translation.content)
        records.append(record)
    aggregate = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return Measurement(
        time.perf_counter() - started,
        serialized_bytes,
        hashlib.sha256(aggregate).hexdigest(),
    )


def main() -> None:
    tracemalloc.start()
    results = [run_once() for _ in range(ITERATIONS)]
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if len({result.plan_hash for result in results}) != 1:
        raise SystemExit("provider-executor benchmark was nondeterministic")
    fastest = min(result.elapsed_seconds for result in results)
    report = {
        "iterations": ITERATIONS,
        "jobs": JOB_COUNT,
        "serialized_artifact_bytes": results[0].serialized_bytes,
        "elapsed_seconds": [round(result.elapsed_seconds, 6) for result in results],
        "best_jobs_per_second": round(JOB_COUNT / fastest, 2),
        "peak_traced_bytes": peak,
        "deterministic_structural_sha256": results[0].plan_hash,
    }
    print(yaml.safe_dump(report, sort_keys=False), end="")


if __name__ == "__main__":
    main()
