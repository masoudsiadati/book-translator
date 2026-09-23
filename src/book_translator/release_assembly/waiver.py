"""The explicit, content-addressed authorization to publish work the judge never saw.

A run created under a semantic gate policy is releasable only when every job holds a semantic
PASS. That rule is right and stays exactly as it is: without a waiver, this release path refuses
such a run today and keeps refusing it. What is missing is a way for a person to take that
decision *on the record* instead of either weakening the gate for everyone or paying for judge
coverage they have chosen not to buy.

A waiver is therefore narrow and auditable. It names one run, one snapshot and one policy
version; it states the semantic and mechanical counts it was authorized against; it is addressed
by those facts, so changing any of them changes its identity; and it is re-derived and compared
against the live run at release time, so a waiver that has gone stale fails closed rather than
admitting something nobody authorized. It never makes an unjudged job a passed one, and it may
not cover a job the judge actually refused.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from book_translator.models import (
    SCHEMA_VERSION,
    ReleaseSemanticWaiver,
    SemanticGateStatus,
    TranslationAcceptanceStatus,
    TranslationJob,
    TranslationRun,
)

#: The waiver boundary's own version. The release engine, the gate and its policy are unchanged
#: by it, so each keeps its own version and every record they wrote keeps its meaning.
RELEASE_WAIVER_VERSION = "1.0.0"

__all__ = [
    "RELEASE_WAIVER_VERSION",
    "ReleaseWaiverError",
    "SemanticCoverage",
    "semantic_coverage",
    "waiver_for",
    "waiver_identity",
    "waiver_refusals",
]


class ReleaseWaiverError(ValueError):
    """Raised when a waiver cannot be authorized, or cannot be trusted at release time."""


@dataclass(frozen=True, slots=True)
class SemanticCoverage:
    """The semantic state of one run's jobs, counted rather than assumed."""

    job_count: int
    judged_pass: int
    judged_not_pass: int
    not_judged: int
    accepted: int
    qa_rejected: int

    @property
    def complete(self) -> bool:
        return self.judged_pass == self.job_count


def semantic_coverage(jobs: Sequence[TranslationJob]) -> SemanticCoverage:
    """Count what the judge actually established about one run."""
    statuses = [job.semantic_status for job in jobs]
    return SemanticCoverage(
        job_count=len(jobs),
        judged_pass=sum(1 for item in statuses if item is SemanticGateStatus.PASS),
        judged_not_pass=sum(
            1 for item in statuses if item is not None and item is not SemanticGateStatus.PASS
        ),
        not_judged=sum(1 for item in statuses if item is None),
        accepted=sum(
            1 for job in jobs if job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
        ),
        qa_rejected=sum(
            1 for job in jobs if job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
        ),
    )


def waiver_identity(
    *,
    translation_run_id: str,
    snapshot_id: str,
    snapshot_manifest_sha256: str,
    semantic_gate_policy_version: str,
    coverage: SemanticCoverage,
    reason: str,
) -> tuple[str, str]:
    """Address a waiver by its scope, the state it was authorized against, and its reason."""
    payload = {
        "record_kind": "release-semantic-waiver",
        "waiver_engine_version": RELEASE_WAIVER_VERSION,
        "translation_run_id": translation_run_id,
        "snapshot_id": snapshot_id,
        "snapshot_manifest_sha256": snapshot_manifest_sha256,
        "semantic_gate_policy_version": semantic_gate_policy_version,
        "job_count": coverage.job_count,
        "judged_pass": coverage.judged_pass,
        "judged_not_pass": coverage.judged_not_pass,
        "not_judged": coverage.not_judged,
        "accepted": coverage.accepted,
        "qa_rejected": coverage.qa_rejected,
        "reason": reason,
        "decided_by": "user",
    }
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )
    digest = hashlib.sha256(encoded).hexdigest()
    return f"rw-{digest[:24]}", digest


def waiver_for(
    run: TranslationRun,
    jobs: Sequence[TranslationJob],
    *,
    reason: str,
    created_at: datetime,
) -> ReleaseSemanticWaiver:
    """Derive the waiver one run currently needs, or refuse to author one it does not."""
    if not reason.strip():
        raise ReleaseWaiverError("a waiver records why publication was authorized")
    if run.semantic_gate_policy_version is None:
        raise ReleaseWaiverError(
            "this run is under no semantic gate policy, so there is nothing to waive"
        )
    coverage = semantic_coverage(jobs)
    if coverage.judged_not_pass:
        raise ReleaseWaiverError(
            f"{coverage.judged_not_pass} job(s) were judged and did not pass; a failed judgement "
            "is repaired, never waived"
        )
    if not coverage.not_judged:
        raise ReleaseWaiverError("every job is judged; this run needs no waiver")
    if coverage.qa_rejected or coverage.accepted != coverage.job_count:
        raise ReleaseWaiverError(
            "a waiver covers semantic coverage only; mechanical acceptance must already hold"
        )
    waiver_id, digest = waiver_identity(
        translation_run_id=run.run_id,
        snapshot_id=run.snapshot_id,
        snapshot_manifest_sha256=run.snapshot_manifest_sha256,
        semantic_gate_policy_version=run.semantic_gate_policy_version,
        coverage=coverage,
        reason=reason.strip(),
    )
    return ReleaseSemanticWaiver(
        schema_version=SCHEMA_VERSION,
        waiver_engine_version=RELEASE_WAIVER_VERSION,
        waiver_id=waiver_id,
        translation_run_id=run.run_id,
        snapshot_id=run.snapshot_id,
        snapshot_manifest_sha256=run.snapshot_manifest_sha256,
        semantic_gate_policy_version=run.semantic_gate_policy_version,
        job_count=coverage.job_count,
        judged_pass=coverage.judged_pass,
        not_judged=coverage.not_judged,
        accepted=coverage.accepted,
        reason=reason.strip(),
        content_sha256=digest,
        created_at=created_at,
    )


def waiver_refusals(
    waiver: ReleaseSemanticWaiver, run: TranslationRun, jobs: Sequence[TranslationJob]
) -> tuple[str, ...]:
    """Every reason this waiver may not admit this release, in the order they are checked."""
    refusals: list[str] = []
    if waiver.waiver_engine_version != RELEASE_WAIVER_VERSION:
        refusals.append(f"waiver_engine_version_mismatch:{waiver.waiver_engine_version}")
    expected_id, digest = waiver_identity(
        translation_run_id=waiver.translation_run_id,
        snapshot_id=waiver.snapshot_id,
        snapshot_manifest_sha256=waiver.snapshot_manifest_sha256,
        semantic_gate_policy_version=waiver.semantic_gate_policy_version,
        coverage=SemanticCoverage(
            job_count=waiver.job_count,
            judged_pass=waiver.judged_pass,
            judged_not_pass=waiver.judged_not_pass,
            not_judged=waiver.not_judged,
            accepted=waiver.accepted,
            qa_rejected=waiver.qa_rejected,
        ),
        reason=waiver.reason,
    )
    if expected_id != waiver.waiver_id or digest != waiver.content_sha256:
        refusals.append("waiver_identity_mismatch")
    if waiver.translation_run_id != run.run_id:
        refusals.append(f"waiver_names_another_run:{waiver.translation_run_id}")
    if waiver.snapshot_id != run.snapshot_id:
        refusals.append(f"waiver_names_another_snapshot:{waiver.snapshot_id}")
    if waiver.snapshot_manifest_sha256 != run.snapshot_manifest_sha256:
        refusals.append("waiver_names_another_snapshot_manifest")
    if waiver.semantic_gate_policy_version != run.semantic_gate_policy_version:
        refusals.append("waiver_names_another_gate_policy")
    coverage = semantic_coverage(jobs)
    if coverage.judged_not_pass:
        refusals.append(f"judged_without_passing:{coverage.judged_not_pass}")
    if (
        waiver.job_count != coverage.job_count
        or waiver.judged_pass != coverage.judged_pass
        or waiver.not_judged != coverage.not_judged
        or waiver.accepted != coverage.accepted
        or waiver.qa_rejected != coverage.qa_rejected
    ):
        refusals.append("waiver_is_stale")
    return tuple(refusals)


def unjudged_jobs(jobs: Iterable[TranslationJob]) -> tuple[str, ...]:
    """The jobs a waiver would admit: the ones that hold no semantic verdict at all."""
    return tuple(job.job_id for job in jobs if job.semantic_status is None)
