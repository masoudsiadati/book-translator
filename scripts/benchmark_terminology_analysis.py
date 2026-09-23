"""Synthetic reconciliation and proposal-policy benchmark."""

import hashlib
import time
import tracemalloc

from book_translator.models import (
    TerminologyAcceptancePolicy,
    TerminologyDiscoveryEvidence,
    TerminologyOccurrence,
    TerminologyProposal,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.terminology import (
    apply_proposals,
    canonical_registry_bytes,
    proposal_identity,
    reconcile_candidates,
)
from book_translator.terminology.canonical import canonical_view, exact_canonical


def main() -> None:
    evidence = tuple(
        TerminologyDiscoveryEvidence(
            source_term=f"Term{index % 2_000:04d}",
            aliases=[f"Alias{index % 2_000:04d}"],
            classification=TerminologyType.INVENTED,
            confidence=0.95,
            occurrences=[
                TerminologyOccurrence(
                    chapter_id=f"{index % 60 + 1:03d}",
                    chunk_id=f"{index % 60 + 1:03d}-{index % 5 + 1:03d}",
                )
            ],
            reason="synthetic benchmark evidence",
        )
        for index in range(10_000)
    )
    base = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-analysis",
        source_language="source-x",
        target_language="target-y",
    )
    timings = []
    hashes = []
    tracemalloc.start()
    for _ in range(3):
        started = time.perf_counter()
        candidates = reconcile_candidates(evidence, base.source_language)
        glossary = {
            "terms": {f"Term{index:04d}": f"Existing{index:04d}" for index in range(1_000)},
            "characters": {},
            "places": {},
            "groups": {},
            "unresolved_terms": [],
        }
        view = canonical_view(base, glossary)
        reused = sum(exact_canonical(item, view) is not None for item in candidates)
        proposals = tuple(
            TerminologyProposal(
                proposal_id=proposal_identity(
                    item.candidate_id, f"Target{index:04d}", base.target_language
                ),
                candidate_id=item.candidate_id,
                source_term=item.source_term,
                proposed_target=f"Target{index:04d}",
                confidence=0.95,
                classification=item.classification,
            )
            for index, item in enumerate(candidates)
        )
        updated, result = apply_proposals(
            base,
            candidates,
            proposals,
            policy=TerminologyAcceptancePolicy.HIGH_CONFIDENCE,
            threshold=0.95,
        )
        timings.append(time.perf_counter() - started)
        hashes.append(hashlib.sha256(canonical_registry_bytes(updated)).hexdigest())
        if result.applied != 2_000:
            raise RuntimeError("proposal benchmark application failed")
        if reused != 1_000:
            raise RuntimeError("canonical-view lookup benchmark failed")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if len(set(hashes)) != 1:
        raise RuntimeError("analysis benchmark output is nondeterministic")
    print(
        "evidence=10000 candidates=2000 proposals=2000 canonical_terms=1000 "
        f"median_seconds={sorted(timings)[1]:.6f}"
    )
    print(f"peak_bytes={peak} registry_sha256={hashes[0]}")


if __name__ == "__main__":
    main()
