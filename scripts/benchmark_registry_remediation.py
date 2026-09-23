"""Synthetic deterministic terminology registry remediation planning benchmark."""

import hashlib
import time
import tracemalloc
from pathlib import Path
from tempfile import TemporaryDirectory

from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.terminology.registry import (
    candidate_identity,
    canonical_registry_bytes,
    decision_identity,
    save_registry,
)
from book_translator.terminology.remediation import audit_registry_remediation
from book_translator.terminology.remediation_engine import (
    plan_registry_remediation,
    simulate_remediation_plan,
)

GROUPS = 50
EMPTY_GLOSSARY: dict[str, object] = {
    "terms": {},
    "characters": {},
    "places": {},
    "groups": {},
    "unresolved_terms": [],
}


def _pair(
    source: str, target: str, *, variants: tuple[str, ...] = ()
) -> tuple[TerminologyCandidate, TerminologyDecision]:
    candidate_id = candidate_identity(source, "en")
    candidate = TerminologyCandidate(
        candidate_id=candidate_id,
        source_term=source,
        classification=TerminologyType.STORY_TERM,
        confidence=1.0,
        status=CandidateStatus.APPROVED,
        provenance=DecisionSource.AI,
    )
    decision = TerminologyDecision(
        decision_id=decision_identity(candidate_id, "fa"),
        candidate_id=candidate_id,
        target_term=target,
        decision_source=DecisionSource.AI,
        competing_variants=list(variants),
    )
    return candidate, decision


def _registry() -> TerminologyRegistry:
    """Build a registry carrying every audited defect shape at book scale."""
    candidates: list[TerminologyCandidate] = []
    decisions: list[TerminologyDecision] = []
    for index in range(GROUPS):
        # a compound whose component target survives (validator false positive)
        for source, target, variants in (
            (f"Bryne{index:04d}", f"براین{index:04d}", ()),
            (f"Lord Bryne{index:04d}", f"لرد براین{index:04d}", ()),
            # a compound preserving no component target (conflicting target)
            (f"Temaile{index:04d}", f"تمایلی{index:04d}", ()),
            (f"Temaile{index:04d} Kinderode", f"تمایل{index:04d} کیندرود", ()),
            # a canonical target stored as another decision's competing variant
            (f"Dragon{index:04d}", f"اژدها{index:04d}", (f"لرد اژدها{index:04d}",)),
            (f"Lord Dragon{index:04d}", f"لرد اژدها{index:04d}", ()),
        ):
            candidate, decision = _pair(source, target, variants=variants)
            candidates.append(candidate)
            decisions.append(decision)
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-remediation-benchmark",
        source_language="en",
        target_language="fa",
        candidates=candidates,
        decisions=decisions,
    )
    return TerminologyRegistry.model_validate_json(canonical_registry_bytes(registry))


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    registry = _registry()
    with TemporaryDirectory() as directory:
        path = Path(directory) / "terminology.yaml"
        save_registry(path, registry)
        raw_sha = "0" * 64
        plan_times: list[float] = []
        simulation_times: list[float] = []
        plan_ids: list[str] = []
        simulated_hashes: list[str] = []
        tracemalloc.start()
        for _ in range(3):
            started = time.perf_counter()
            report = audit_registry_remediation(registry, EMPTY_GLOSSARY)
            plan = plan_registry_remediation(
                registry,
                EMPTY_GLOSSARY,
                report,
                registry_raw_sha256=raw_sha,
                glossary_sha256=raw_sha,
            )
            plan_times.append(time.perf_counter() - started)
            plan_ids.append(plan.plan_id)

            pinned = plan.model_copy(
                update={
                    "source_registry_raw_sha256": _file_sha(path),
                }
            )
            started = time.perf_counter()
            simulation = simulate_remediation_plan(path, EMPTY_GLOSSARY, pinned)
            simulation_times.append(time.perf_counter() - started)
            simulated_hashes.append(simulation.simulated_registry_semantic_sha256)
            if not simulation.real_registry_unchanged:
                raise RuntimeError("the dry run modified the registry file")
            if simulation.lookup_regression_count:
                raise RuntimeError("the dry run reported a canonical lookup regression")
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    if len(set(plan_ids)) != 1 or len(set(simulated_hashes)) != 1:
        raise RuntimeError("registry remediation planning is nondeterministic")
    print(
        f"decisions={len(registry.decisions)} candidates={len(registry.candidates)} "
        f"operations={len(plan.operations)} mutating={plan.mutating_operation_count} "
        f"unresolved={plan.unresolved_operation_count}"
    )
    print(
        f"plan_median_seconds={sorted(plan_times)[1]:.6f} "
        f"dry_run_median_seconds={sorted(simulation_times)[1]:.6f}"
    )
    print(f"peak_bytes={peak} plan_id={plan_ids[0]}")
    print(f"simulated_registry_sha256={simulated_hashes[0]}")


if __name__ == "__main__":
    main()
