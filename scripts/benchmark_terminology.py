"""Synthetic deterministic terminology relevance and validation benchmark."""

import hashlib
import time
import tracemalloc

from book_translator.models import DecisionSource, TerminologyRegistry, TerminologyType
from book_translator.terminology import canonical_registry_bytes, set_decision, validate_terminology


def main() -> None:
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-benchmark",
        source_language="x-source",
        target_language="x-target",
    )
    for index in range(2_000):
        registry = set_decision(
            registry,
            source_term=f"LoreTerm{index:04d}",
            target_term=f"TargetTerm{index:04d}",
            classification=TerminologyType.STORY_TERM,
            decision_source=DecisionSource.AI,
            locked=False,
        )
    source = " ".join(f"LoreTerm{index:04d}" for index in range(0, 2_000, 20)) + "\n"
    translation = " ".join(f"TargetTerm{index:04d}" for index in range(0, 2_000, 20)) + "\n"
    observations: list[float] = []
    hashes: list[str] = []
    tracemalloc.start()
    for _ in range(3):
        started = time.perf_counter()
        report = validate_terminology(source, translation, registry)
        payload = canonical_registry_bytes(registry)
        observations.append(time.perf_counter() - started)
        hashes.append(hashlib.sha256(payload).hexdigest())
        if not report.valid or report.checked_decisions != 100:
            raise RuntimeError("terminology benchmark validation failed")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if len(set(hashes)) != 1:
        raise RuntimeError("terminology registry identity is nondeterministic")
    print(f"terms=2000 relevant=100 median_seconds={sorted(observations)[1]:.6f}")
    print(f"peak_bytes={peak} registry_sha256={hashes[0]}")


if __name__ == "__main__":
    main()
