"""Synthetic terminology review validation benchmark."""

import hashlib
import time
import tracemalloc

from book_translator.models import (
    DecisionSource,
    TerminologyCandidate,
    TerminologyProposal,
    TerminologyReview,
    TerminologyReviewDisposition,
    TerminologyReviewResponse,
    TerminologyType,
)
from book_translator.terminology.analysis import canonical_json_bytes, proposal_identity
from book_translator.terminology.registry import candidate_identity
from book_translator.terminology.review import validate_reviews


def main() -> None:
    candidates = tuple(
        TerminologyCandidate(
            candidate_id=candidate_identity(f"Term{index:04d}", "source-x"),
            source_term=f"Term{index:04d}",
            classification=TerminologyType.INVENTED,
            confidence=0.96,
            provenance=DecisionSource.AI,
        )
        for index in range(2_000)
    )
    proposals = tuple(
        TerminologyProposal(
            proposal_id=proposal_identity(item.candidate_id, f"Target{index:04d}", "target-y"),
            candidate_id=item.candidate_id,
            source_term=item.source_term,
            proposed_target=f"Target{index:04d}",
            confidence=0.97,
            classification=item.classification,
        )
        for index, item in enumerate(candidates)
    )
    response = TerminologyReviewResponse(
        schema_version="1.0",
        reviews=[
            TerminologyReview(
                review_id="tr-" + "0" * 24,
                proposal_id=item.proposal_id,
                disposition=TerminologyReviewDisposition.ACCEPT,
                reviewed_target=item.proposed_target,
                confidence=0.95,
                meaning_fidelity=0.9,
                target_naturalness=0.9,
                transliteration_stability=0.9,
                terminology_consistency=0.9,
                register_appropriateness=0.9,
                morphology_suitability=0.9,
                ambiguity_safety=0.9,
                reason_codes=["synthetic_accept"],
                notes=[],
            )
            for item in proposals
        ],
    )
    timings = []
    hashes = []
    tracemalloc.start()
    for _ in range(3):
        started = time.perf_counter()
        validated = validate_reviews(response, proposals, candidates, (), "target-y")
        timings.append(time.perf_counter() - started)
        hashes.append(
            hashlib.sha256(
                canonical_json_bytes([item.model_dump(mode="json") for item in validated])
            ).hexdigest()
        )
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if len(set(hashes)) != 1:
        raise RuntimeError("review benchmark output is nondeterministic")
    print(f"reviews=2000 median_seconds={sorted(timings)[1]:.6f}")
    print(f"peak_bytes={peak} review_sha256={hashes[0]}")


if __name__ == "__main__":
    main()
