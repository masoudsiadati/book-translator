#!/usr/bin/env python3
"""Emit a deterministic, prose-free terminology review/application ledger."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from book_translator.models import TerminologyReviewDisposition
from book_translator.terminology.canonical import canonical_view, proposal_conflicts
from book_translator.terminology.registry import (
    candidate_identity,
    load_registry,
    registry_identity,
)
from book_translator.terminology.workflow import (
    load_discovery_output,
    load_proposal_output,
    load_review_output,
)


def _baseline(root: Path, identity: str) -> tuple[Path, Any]:
    paths = [root / "config/terminology.yaml"]
    paths.extend(sorted((root / "workspace/resources/terminology").glob("*.yaml")))
    for path in paths:
        registry = load_registry(path)
        if registry_identity(registry) == identity:
            return path, registry
    raise ValueError(f"registry baseline {identity} is unavailable")


def build_report(root: Path, review_id: str) -> dict[str, Any]:
    review_path = root / "workspace/terminology-runs" / review_id
    review_run, reviews = load_review_output(review_path)
    if review_run.proposal_run_id is None:
        raise ValueError("review run has no proposal lineage")
    proposal_path = review_path.parent / review_run.proposal_run_id
    proposal_run, proposals = load_proposal_output(proposal_path)
    if proposal_run.discovery_run_id is None:
        raise ValueError("proposal run has no discovery lineage")
    _, candidates = load_discovery_output(review_path.parent / proposal_run.discovery_run_id)
    baseline_path, baseline = _baseline(root, proposal_run.registry_sha256)
    current = load_registry(root / "config/terminology.yaml")
    current_decisions = {item.candidate_id for item in current.decisions}
    review_by_proposal = {item.proposal_id: item for item in reviews}

    dispositions: dict[str, list[str]] = {
        "new_canonical_decision_created": [],
        "new_decision_created_under_normalized_candidate_identity": [],
        "reused_existing_authoritative_decision": [],
        "rejected": [],
        "escalated": [],
        "blocked_canonical_conflict": [],
        "otherwise_intentionally_not_materialized": [],
    }
    for proposal in proposals:
        if proposal.action == "reuse_existing":
            dispositions["reused_existing_authoritative_decision"].append(proposal.proposal_id)
            continue
        review = review_by_proposal[proposal.proposal_id]
        if review.disposition is TerminologyReviewDisposition.REJECT:
            dispositions["rejected"].append(proposal.proposal_id)
        elif review.disposition is TerminologyReviewDisposition.DEFER:
            dispositions["escalated"].append(proposal.proposal_id)
        elif proposal.candidate_id in current_decisions:
            dispositions["new_canonical_decision_created"].append(proposal.proposal_id)
        elif candidate_identity(proposal.source_term.strip(), current.source_language) in (
            current_decisions
        ):
            dispositions["new_decision_created_under_normalized_candidate_identity"].append(
                proposal.proposal_id
            )
        elif review.blocked or proposal.blocked:
            dispositions["blocked_canonical_conflict"].append(proposal.proposal_id)
        else:
            dispositions["otherwise_intentionally_not_materialized"].append(proposal.proposal_id)

    candidate_by_id = {item.candidate_id: item for item in candidates}
    proposal_by_id = {item.proposal_id: item for item in proposals}
    glossary = yaml.safe_load((root / "config/glossary.yaml").read_text(encoding="utf-8"))
    view = canonical_view(current, glossary)
    current_conflicts: dict[str, dict[str, Any]] = {}
    for proposal_id in dispositions["blocked_canonical_conflict"]:
        proposal = proposal_by_id[proposal_id]
        review = review_by_proposal[proposal_id]
        if review.reviewed_target is not None:
            proposal = proposal.model_copy(update={"proposed_target": review.reviewed_target})
        conflicts = proposal_conflicts(
            proposal,
            candidate_by_id[proposal.candidate_id],
            view,
            current.target_language,
        )
        current_conflicts[proposal_id] = {
            "blocked": bool(conflicts),
            "conflicts": list(conflicts),
        }

    all_ids = [item.proposal_id for item in proposals]
    disposed_ids = [value for values in dispositions.values() for value in values]
    reviewed_ids = [item.proposal_id for item in reviews]
    reviewed_disposed = [
        value
        for key, values in dispositions.items()
        if key != "reused_existing_authoritative_decision"
        for value in values
    ]
    baseline_ids = {item.candidate_id for item in baseline.candidates}
    discovery_ids = {item.candidate_id for item in candidates}
    current_ids = {item.candidate_id for item in current.candidates}
    later_ids = sorted(current_ids - (baseline_ids | discovery_ids))
    return {
        "review_run_id": review_id,
        "proposal_run_id": proposal_run.run_id,
        "discovery_run_id": proposal_run.discovery_run_id,
        "baseline": {
            "path": baseline_path.relative_to(root).as_posix(),
            "semantic_sha256": registry_identity(baseline),
            "candidate_count": len(baseline.candidates),
            "decision_count": len(baseline.decisions),
        },
        "current": {
            "semantic_sha256": registry_identity(current),
            "candidate_count": len(current.candidates),
            "decision_count": len(current.decisions),
        },
        "counts": {
            "discovery_candidates": len(candidates),
            "proposal_records": len(proposals),
            "reviewed_records": len(reviews),
            "review_dispositions": dict(
                sorted(Counter(item.disposition.value for item in reviews).items())
            ),
            "baseline_discovery_overlap": len(baseline_ids & discovery_ids),
            "baseline_discovery_union": len(baseline_ids | discovery_ids),
            "later_candidate_only_additions": len(later_ids),
            "new_decisions": len(current.decisions) - len(baseline.decisions),
            "application_dispositions": {key: len(values) for key, values in dispositions.items()},
        },
        "dispositions": dispositions,
        "current_conflict_revalidation": dict(sorted(current_conflicts.items())),
        "candidate_arithmetic": {
            "baseline_plus_discovery_minus_overlap_plus_later": (
                len(baseline_ids)
                + len(discovery_ids)
                - len(baseline_ids & discovery_ids)
                + len(later_ids)
            ),
            "later_candidate_ids": later_ids,
        },
        "bookkeeping": {
            "normalized_candidate_identity_decisions": dispositions[
                "new_decision_created_under_normalized_candidate_identity"
            ],
            "resolved": not dispositions[
                "new_decision_created_under_normalized_candidate_identity"
            ],
        },
        "coverage": {
            "proposal_ids_unique": len(all_ids) == len(set(all_ids)),
            "disposition_ids_unique": len(disposed_ids) == len(set(disposed_ids)),
            "proposal_set_equals_disposition_set": set(all_ids) == set(disposed_ids),
            "review_ids_unique": len(reviewed_ids) == len(set(reviewed_ids)),
            "review_set_equals_nonreuse_disposition_set": set(reviewed_ids)
            == set(reviewed_disposed),
            "missing_proposal_ids": sorted(set(all_ids) - set(disposed_ids)),
            "unexpected_disposition_ids": sorted(set(disposed_ids) - set(all_ids)),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("review_run_id")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    report = build_report(args.project_root.resolve(), args.review_run_id)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
