"""Benchmark the provider-free audit against the current canonical workspace."""

import json
import resource
import time
from pathlib import Path

from book_translator.source_quality_audit import audit_source_quality


def main() -> None:
    started = time.perf_counter()
    report = audit_source_quality(Path.cwd()).report
    elapsed = time.perf_counter() - started
    peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        json.dumps(
            {
                "wall_seconds": round(elapsed, 6),
                "peak_memory_kib": peak_kib,
                "chapters": report.chapters_scanned,
                "pages": report.pages_inspected,
                "chapters_per_second": round(report.chapters_scanned / elapsed, 2),
                "pages_per_second": round(report.pages_inspected / elapsed, 2),
                "findings": len(report.findings),
                "audit_id": report.audit_id,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
