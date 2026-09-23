"""Explicit provider-free carry-forward of effectively accepted translations."""

import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from book_translator.models import (
    SCHEMA_VERSION,
    PromptPackage,
    ReuseCompatibilityTier,
    SnapshotResourceReference,
    SourceAnomalyRegistry,
    TerminologyRegistry,
    TranslationAcceptanceEvidence,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
    TranslationQAReevaluationEvidence,
    TranslationReuseCompatibility,
    TranslationReuseEvidence,
)
from book_translator.paragraph_alignment import ALIGNMENT_QA_VERSION
from book_translator.paths import ProjectPaths
from book_translator.prompt_packaging.rendering import framed_section_digests
from book_translator.provider_execution import validate_attempt
from book_translator.provider_execution.artifacts import (
    load_yaml,
    private_write,
    project_relative,
    serialize_yaml,
    sha256_bytes,
)
from book_translator.terminology import load_registry
from book_translator.terminology.exemption_workflow import frozen_occurrence_exemptions
from book_translator.terminology.validation import STRUCTURAL_QA_VERSION, TERMINOLOGY_QA_VERSION
from book_translator.translation_acceptance_qa import assess_translation
from book_translator.translation_queue.locking import queue_lock
from book_translator.translation_queue.workflow import (
    LOCK_TIMEOUT_SECONDS,
    QueueState,
    _append_event,
    _event,
    _load_queue,
    _persist_job_and_run,
    _resolve_run_path,
)
from book_translator.translation_reuse_policy import (
    EXACT_RENDERED_REQUEST_RATIONALE,
    REUSE_COMPATIBILITY_POLICY_VERSION,
    RequestCompatibility,
    classify_rendered_divergence,
    evaluate_request_compatibility,
)

#: 1.2.0 judges a reused output with the full acceptance battery a fresh attempt faces (alignment
#: QA included) and publishes its acceptance inside the reuse record, never in ``acceptance/NNN``,
#: which belongs to the provider attempt of that number.
TRANSLATION_REUSE_ENGINE_VERSION = "1.2.0"


class TranslationReuseError(ValueError):
    """Raised when accepted translation reuse cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class ReuseJobPlan:
    job_id: str
    chunk_id: str
    outcome: str
    reasons: tuple[str, ...]
    source_sha256: tuple[str, str]
    context_sha256: tuple[str, str]
    tier: ReuseCompatibilityTier = ReuseCompatibilityTier.INCOMPATIBLE
    resource_drift: tuple[str, ...] = ()
    source_attempt_number: int | None = None
    source_translation_file: Path | None = None
    source_translation_sha256: str | None = None
    source_acceptance_origin: TranslationAcceptanceOrigin | None = None


@dataclass(frozen=True, slots=True)
class ReusePlan:
    run_id: str
    source_run_id: str
    jobs: tuple[ReuseJobPlan, ...]

    @property
    def inspected(self) -> int:
        return len(self.jobs)

    @property
    def reusable(self) -> int:
        return sum(job.outcome == "reusable" for job in self.jobs)

    @property
    def blocked(self) -> int:
        return sum(job.outcome == "blocked" for job in self.jobs)

    @property
    def skipped(self) -> int:
        return sum(job.outcome == "skipped" for job in self.jobs)

    @property
    def reasons(self) -> dict[str, int]:
        return dict(sorted(Counter(reason for job in self.jobs for reason in job.reasons).items()))

    @property
    def resource_drift(self) -> dict[str, int]:
        """Whole-resource differences observed, none of which block on their own."""
        return dict(
            sorted(Counter(reason for job in self.jobs for reason in job.resource_drift).items())
        )

    @property
    def tiers(self) -> dict[str, int]:
        reusable = (job.tier.value for job in self.jobs if job.outcome == "reusable")
        return dict(sorted(Counter(reusable).items()))


@dataclass(frozen=True, slots=True)
class ReuseResult:
    plan: ReusePlan
    published: int
    accepted: int
    qa_rejected: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class EffectiveSource:
    attempt: TranslationAttempt
    attempt_path: Path
    origin: TranslationAcceptanceOrigin


def _job_map(state: QueueState) -> dict[str, TranslationJob]:
    return {job.chunk_id: job for job in state.jobs}


def _package(state: QueueState, job: TranslationJob) -> PromptPackage:
    return load_yaml(
        state.path / "jobs" / job.chunk_id / "prompt-package/package.yaml", PromptPackage
    )


def _manifest(state: QueueState, project_root: Path) -> TranslationManifest:
    return load_yaml(project_root / state.run.snapshot_path / "manifest.yaml", TranslationManifest)


def _resource_intact(reference: SnapshotResourceReference | None, project_root: Path) -> bool:
    """Return whether a frozen snapshot resource is present with its recorded bytes."""
    if reference is None:
        return True
    path = project_root / (reference.frozen_file or reference.file)
    return path.is_file() and sha256_bytes(path.read_bytes()) == reference.sha256


def _verified_text(path: Path, expected_sha256: str, label: str) -> str:
    """Read a frozen QA input only after proving it still matches its recorded digest."""
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise TranslationReuseError(f"{label} is unreadable: {path}") from exc
    if sha256_bytes(content) != expected_sha256:
        raise TranslationReuseError(f"{label} no longer matches its recorded hash: {path}")
    return content.decode("utf-8")


def _rendered_request_intact(package: PromptPackage, project_root: Path) -> bool:
    """Prove the recorded message digests still describe present, unchanged rendered bytes."""
    for reference in (package.system_message, package.user_message):
        path = project_root / reference.file
        if not path.is_file() or sha256_bytes(path.read_bytes()) != reference.sha256:
            return False
    return True


def _rendered_divergence(
    old_package: PromptPackage, new_package: PromptPackage, project_root: Path
) -> tuple[str, ...]:
    """Attribute a changed request to the framed sections whose effective content differs."""
    try:
        sections = tuple(
            framed_section_digests(
                (project_root / package.user_message.file).read_text(encoding="utf-8")
            )
            for package in (old_package, new_package)
        )
    except (OSError, ValueError):
        return ("effective_projection_unavailable",)
    return classify_rendered_divergence(*sections)


def _effective_source(
    state: QueueState, job: TranslationJob, project_root: Path, now: datetime
) -> EffectiveSource | str:
    if job.acceptance_status is not TranslationAcceptanceStatus.ACCEPTED:
        return "historical_not_effectively_accepted"
    if job.accepted_attempt is None or job.acceptance_file is None or job.acceptance_sha256 is None:
        return "historical_acceptance_lineage_missing"
    validation = validate_attempt(
        state.path,
        job.job_id,
        job.accepted_attempt,
        project_root=project_root,
        now=lambda: now,
        snapshot_validation=state.snapshot_validation,
    )
    if not validation.valid:
        return "historical_attempt_invalid"
    attempt_path = (
        state.path
        / "jobs"
        / job.chunk_id
        / "attempts"
        / f"{job.accepted_attempt:03d}"
        / "attempt.yaml"
    )
    attempt = load_yaml(attempt_path, TranslationAttempt)
    if (
        attempt.status is not TranslationAttemptStatus.SUCCEEDED
        or attempt.translation_file is None
        or attempt.translation_sha256 is None
    ):
        return "historical_attempt_not_successful"
    acceptance_path = project_root / job.acceptance_file
    if (
        not acceptance_path.is_file()
        or sha256_bytes(acceptance_path.read_bytes()) != job.acceptance_sha256
    ):
        return "historical_acceptance_invalid"
    acceptance = load_yaml(acceptance_path, TranslationAcceptanceEvidence)
    if acceptance.attempt_number != job.accepted_attempt or acceptance.job_id != job.job_id:
        return "historical_acceptance_lineage_invalid"
    origin = job.acceptance_origin or TranslationAcceptanceOrigin.INITIAL_QA
    if origin is TranslationAcceptanceOrigin.QA_REEVALUATION:
        if job.latest_reevaluation_file is None or job.latest_reevaluation_sha256 is None:
            return "historical_reevaluation_missing"
        reevaluation_path = project_root / job.latest_reevaluation_file
        if (
            not reevaluation_path.is_file()
            or sha256_bytes(reevaluation_path.read_bytes()) != job.latest_reevaluation_sha256
        ):
            return "historical_reevaluation_invalid"
        reevaluation = load_yaml(reevaluation_path, TranslationQAReevaluationEvidence)
        if (
            reevaluation.resulting_status is not TranslationAcceptanceStatus.ACCEPTED
            or reevaluation.attempt_number != job.accepted_attempt
            or reevaluation.translation_sha256 != attempt.translation_sha256
        ):
            return "historical_reevaluation_lineage_invalid"
    elif acceptance.status is not TranslationAcceptanceStatus.ACCEPTED:
        return "historical_acceptance_not_accepted"
    return EffectiveSource(attempt, attempt_path, origin)


def plan_accepted_translation_reuse(
    run_id_or_path: str | Path,
    *,
    from_run: str | Path,
    project_root: Path,
    job_id: str | None = None,
    now: Callable[[], datetime] | None = None,
    new_state: QueueState | None = None,
    old_state: QueueState | None = None,
) -> ReusePlan:
    """Plan reuse for every pending job (or one), reusing queue loads a batch caller holds."""
    project_root = project_root.resolve()
    timestamp = (now or (lambda: datetime.now(UTC)))()
    new = new_state or _load_queue(_resolve_run_path(run_id_or_path, project_root), project_root)
    old = old_state or _load_queue(_resolve_run_path(from_run, project_root), project_root)
    old_jobs = _job_map(old)
    new_manifest = _manifest(new, project_root)
    workflow_compatible = _manifest(old, project_root).workflow == new_manifest.workflow
    frozen_terminology_intact = _resource_intact(new_manifest.terminology, project_root)
    plans: list[ReuseJobPlan] = []
    for job in new.jobs:
        if job_id is not None and job.job_id != job_id and job.chunk_id != job_id:
            continue
        hashes = ((job.chunk_source_sha256, ""), (job.context_sha256, ""))
        if job.status is not TranslationJobStatus.PENDING or job.acceptance_status is not None:
            if job.reuse_count:
                reuse_path = project_root / job.latest_reuse_file  # type: ignore[operator]
                if (
                    job.latest_reuse_sha256 is None
                    or not reuse_path.is_file()
                    or sha256_bytes(reuse_path.read_bytes()) != job.latest_reuse_sha256
                ):
                    plans.append(
                        ReuseJobPlan(
                            job.job_id,
                            job.chunk_id,
                            "blocked",
                            ("existing_reuse_invalid",),
                            *hashes,
                        )
                    )
                    continue
            plans.append(
                ReuseJobPlan(job.job_id, job.chunk_id, "skipped", ("already_completed",), *hashes)
            )
            continue
        source_job = old_jobs.get(job.chunk_id)
        if source_job is None:
            plans.append(
                ReuseJobPlan(
                    job.job_id, job.chunk_id, "blocked", ("historical_job_missing",), *hashes
                )
            )
            continue
        hashes = (
            (source_job.chunk_source_sha256, job.chunk_source_sha256),
            (source_job.context_sha256, job.context_sha256),
        )
        old_package, new_package = _package(old, source_job), _package(new, job)
        compatibility = evaluate_request_compatibility(old_package, new_package)
        reasons = compatibility.identity_reasons
        if not workflow_compatible:
            reasons += ("snapshot_workflow_mismatch",)
        if not frozen_terminology_intact:
            reasons += ("frozen_terminology_unavailable",)
        effective = _effective_source(old, source_job, project_root, timestamp)
        if not isinstance(effective, EffectiveSource):
            reasons += (effective,)
        elif compatibility.exact:
            # The waiver only holds while the rendered bytes behind both recorded digests are
            # still present and unchanged; otherwise request identity is asserted, not proven.
            if not _rendered_request_intact(old_package, project_root):
                reasons += ("historical_rendered_request_unavailable",)
            if not _rendered_request_intact(new_package, project_root):
                reasons += ("rendered_request_unavailable",)
        elif any(reason.endswith("message_hash_mismatch") for reason in reasons):
            reasons += _rendered_divergence(old_package, new_package, project_root)
        if reasons:
            plans.append(
                ReuseJobPlan(
                    job.job_id,
                    job.chunk_id,
                    "blocked",
                    reasons,
                    *hashes,
                    resource_drift=compatibility.resource_drift_reasons,
                )
            )
            continue
        assert isinstance(effective, EffectiveSource)
        attempt = effective.attempt
        plans.append(
            ReuseJobPlan(
                job.job_id,
                job.chunk_id,
                "reusable",
                (),
                *hashes,
                tier=compatibility.tier,
                resource_drift=compatibility.resource_drift_reasons,
                source_attempt_number=attempt.attempt_number,
                source_translation_file=attempt.translation_file,
                source_translation_sha256=attempt.translation_sha256,
                source_acceptance_origin=effective.origin,
            )
        )
    if job_id is not None and not plans:
        raise TranslationReuseError(f"translation job not found: {job_id}")
    return ReusePlan(new.run.run_id, old.run.run_id, tuple(plans))


def _replace(
    jobs: tuple[TranslationJob, ...], changed: TranslationJob
) -> tuple[TranslationJob, ...]:
    return tuple(changed if job.job_id == changed.job_id else job for job in jobs)


def _compatibility_record(
    verdict: RequestCompatibility,
    source_package: PromptPackage,
    package: PromptPackage,
    item: ReuseJobPlan,
) -> TranslationReuseCompatibility:
    """Record why resource-level drift did not change this job's provider-facing request."""
    return TranslationReuseCompatibility(
        policy_version=REUSE_COMPATIBILITY_POLICY_VERSION,
        tier=verdict.tier,
        source_snapshot_id=source_package.snapshot_id,
        target_snapshot_id=package.snapshot_id,
        source_prompt_package_id=source_package.package_id,
        target_prompt_package_id=package.package_id,
        source_system_message_sha256=source_package.system_message.sha256,
        target_system_message_sha256=package.system_message.sha256,
        source_user_message_sha256=source_package.user_message.sha256,
        target_user_message_sha256=package.user_message.sha256,
        verified_identity_checks=list(verdict.verified_identity_checks),
        waived_resource_drift=list(item.resource_drift),
        rationale=EXACT_RENDERED_REQUEST_RATIONALE,
    )


def _publish_one(
    state: QueueState,
    old: QueueState,
    item: ReuseJobPlan,
    project_root: Path,
    now: datetime,
) -> TranslationAcceptanceStatus:
    job = next(job for job in state.jobs if job.job_id == item.job_id)
    source_job = _job_map(old)[item.chunk_id]
    assert (
        item.source_attempt_number
        and item.source_translation_file
        and item.source_translation_sha256
    )
    attempt_path = (
        old.path
        / "jobs"
        / item.chunk_id
        / "attempts"
        / f"{item.source_attempt_number:03d}"
        / "attempt.yaml"
    )
    source_bytes = (project_root / item.source_translation_file).read_bytes()
    if sha256_bytes(source_bytes) != item.source_translation_sha256:
        raise TranslationReuseError("historical translation changed after reuse planning")
    destination = ProjectPaths(project_root).translation_reuse(job.run_id, job.chunk_id, 1)
    translation_path = destination / "translation.md"
    evidence_path = destination / "evidence.yaml"
    package = _package(state, job)
    source_package = _package(old, source_job)
    verdict = evaluate_request_compatibility(source_package, package)
    if not verdict.exact:
        raise TranslationReuseError("reuse compatibility changed after planning")
    terminology_sha256 = package.terminology.sha256 if package.terminology is not None else "none"
    compatibility = {
        "source_sha256": job.chunk_source_sha256,
        "context_sha256": job.context_sha256,
        "target_language": job.target_language,
        "terminology_sha256": terminology_sha256,
        "prompt_sha256": package.prompt.sha256,
        "glossary_sha256": package.glossary.sha256,
        "style_sha256": package.style_guide.sha256,
        "renderer_version": package.renderer_version,
        "output_contract_version": package.output_contract_version,
    }
    evidence = TranslationReuseEvidence(
        schema_version=SCHEMA_VERSION,
        reuse_engine_version=TRANSLATION_REUSE_ENGINE_VERSION,
        run_id=job.run_id,
        job_id=job.job_id,
        reuse_number=1,
        source_run_id=old.run.run_id,
        source_job_id=source_job.job_id,
        source_attempt_number=item.source_attempt_number,
        source_attempt_file=project_relative(attempt_path, project_root),
        source_attempt_sha256=sha256_bytes(attempt_path.read_bytes()),
        source_translation_file=item.source_translation_file,
        source_translation_sha256=item.source_translation_sha256,
        source_acceptance_file=source_job.acceptance_file,  # type: ignore[arg-type]
        source_acceptance_sha256=source_job.acceptance_sha256,  # type: ignore[arg-type]
        source_acceptance_origin=item.source_acceptance_origin,  # type: ignore[arg-type]
        source_reevaluation_file=source_job.latest_reevaluation_file,
        source_reevaluation_sha256=source_job.latest_reevaluation_sha256,
        compatibility=compatibility,
        rendered_request_compatibility=_compatibility_record(
            verdict, source_package, package, item
        ),
        translation_file=project_relative(translation_path, project_root),
        translation_sha256=item.source_translation_sha256,
        created_at=now,
    )
    evidence_bytes = serialize_yaml(evidence).encode("utf-8")
    source_text = _verified_text(
        project_root / job.chunk_source_file, job.chunk_source_sha256, "frozen chunk source"
    )
    translation_text = source_bytes.decode("utf-8")
    new_manifest = _manifest(state, project_root)
    terminology_reference = new_manifest.terminology
    if terminology_reference is None:
        registry = TerminologyRegistry(
            schema_version=SCHEMA_VERSION,
            version="1.0.0",
            book_id="reuse",
            source_language=job.source_language,
            target_language=job.target_language,
        )
        terminology_file = None
        terminology_hash = None
    else:
        # Current QA must run against the new run's frozen registry, not the mutable config file
        # the snapshot merely names; the two diverge exactly when reuse matters most.
        terminology_file = terminology_reference.frozen_file or terminology_reference.file
        terminology_hash = terminology_reference.sha256
        _verified_text(project_root / terminology_file, terminology_hash, "frozen terminology")
        registry = load_registry(project_root / terminology_file)
    anomaly_registry = None
    if new_manifest.source_anomalies is not None:
        anomaly_file = (
            new_manifest.source_anomalies.frozen_file or new_manifest.source_anomalies.file
        )
        _verified_text(
            project_root / anomaly_file, new_manifest.source_anomalies.sha256, "frozen anomalies"
        )
        anomaly_registry = load_yaml(project_root / anomaly_file, SourceAnomalyRegistry)
    source_attempt = load_yaml(attempt_path, TranslationAttempt)
    # The same battery a fresh attempt faces, under the contract the output was produced against.
    assessment = assess_translation(
        source_text,
        translation_text,
        registry=registry,
        target_language=job.target_language,
        output_contract=source_attempt.output_contract,
        anomaly_registry=anomaly_registry,
        chapter_id=job.chapter_id,
        occurrence_exemptions=frozen_occurrence_exemptions(
            new_manifest,
            project_root,
            registry=registry,
            chunk_id=job.chunk_id,
            source_text=source_text,
        ),
    )
    accepted = assessment.accepted
    status = (
        TranslationAcceptanceStatus.ACCEPTED
        if accepted
        else TranslationAcceptanceStatus.QA_REJECTED
    )

    def acceptance_for(reuse_hash: str, created_at: datetime) -> TranslationAcceptanceEvidence:
        return TranslationAcceptanceEvidence(
            schema_version=SCHEMA_VERSION,
            acceptance_engine_version=TRANSLATION_REUSE_ENGINE_VERSION,
            run_id=job.run_id,
            job_id=job.job_id,
            attempt_id=f"{job.job_id}__reuse-001",
            attempt_number=1,
            attempt_file=project_relative(evidence_path, project_root),
            attempt_sha256=reuse_hash,
            source_file=job.chunk_source_file,
            source_sha256=job.chunk_source_sha256,
            translation_file=project_relative(translation_path, project_root),
            translation_sha256=item.source_translation_sha256,  # type: ignore[arg-type]
            terminology_file=terminology_file,
            terminology_sha256=terminology_hash,
            terminology_qa_version=TERMINOLOGY_QA_VERSION,
            structural_qa_version=STRUCTURAL_QA_VERSION,
            alignment_qa_version=ALIGNMENT_QA_VERSION,
            terminology_report=assessment.terminology_report,
            structural_report=assessment.structural_report,
            alignment_report=assessment.alignment_report,
            status=status,
            accepted=accepted,
            created_at=created_at,
        )

    # Inside the reuse record rather than acceptance/001, which belongs to provider attempt 001.
    acceptance_path = destination / "acceptance.yaml"
    if destination.exists():
        persisted = load_yaml(evidence_path, TranslationReuseEvidence)
        if persisted.model_dump(exclude={"created_at"}) != evidence.model_dump(
            exclude={"created_at"}
        ):
            raise TranslationReuseError("existing reuse evidence conflicts with current plan")
        persisted_acceptance = load_yaml(acceptance_path, TranslationAcceptanceEvidence)
        expected = acceptance_for(sha256_bytes(evidence_path.read_bytes()), now)
        if persisted_acceptance.model_dump(exclude={"created_at"}) != expected.model_dump(
            exclude={"created_at"}
        ):
            raise TranslationReuseError("existing reuse acceptance conflicts with current QA")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = Path(tempfile.mkdtemp(prefix=".001.staging.", dir=destination.parent))
        try:
            private_write(staging / "translation.md", source_bytes)
            private_write(staging / "evidence.yaml", evidence_bytes)
            private_write(
                staging / "acceptance.yaml",
                serialize_yaml(acceptance_for(sha256_bytes(evidence_bytes), now)).encode("utf-8"),
            )
            os.rename(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    reuse_hash = sha256_bytes(evidence_path.read_bytes())
    changed = job.model_copy(
        update={
            "status": TranslationJobStatus.SUCCEEDED,
            "completed_at": now,
            "acceptance_status": status,
            "acceptance_origin": TranslationAcceptanceOrigin.REUSE_QA,
            "accepted_attempt": 1,
            "acceptance_file": project_relative(acceptance_path, project_root),
            "acceptance_sha256": sha256_bytes(acceptance_path.read_bytes()),
            "reuse_count": 1,
            "latest_reuse_file": project_relative(evidence_path, project_root),
            "latest_reuse_sha256": reuse_hash,
            "updated_at": now,
            "recent_events": _append_event(
                job, _event("translation_reused", now, reason=status.value)
            ),
        }
    )
    _persist_job_and_run(state, _replace(state.jobs, changed), changed, now)
    return status


def reuse_accepted_translations(
    run_id_or_path: str | Path,
    *,
    from_run: str | Path,
    project_root: Path,
    job_id: str | None = None,
    dry_run: bool = False,
    now: Callable[[], datetime] | None = None,
) -> ReuseResult:
    timestamp = (now or (lambda: datetime.now(UTC)))()
    plan = plan_accepted_translation_reuse(
        run_id_or_path,
        from_run=from_run,
        project_root=project_root,
        job_id=job_id,
        now=lambda: timestamp,
    )
    if dry_run:
        return ReuseResult(plan, 0, 0, 0, True)
    project_root = project_root.resolve()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    with queue_lock(run_path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        state = _load_queue(run_path, project_root)
        old = _load_queue(_resolve_run_path(from_run, project_root), project_root)
        accepted = rejected = published = 0
        for item in plan.jobs:
            if item.outcome != "reusable":
                continue
            status = _publish_one(state, old, item, project_root, timestamp)
            published += 1
            accepted += status is TranslationAcceptanceStatus.ACCEPTED
            rejected += status is TranslationAcceptanceStatus.QA_REJECTED
            state = _load_queue(run_path, project_root)
        return ReuseResult(plan, published, accepted, rejected, False)
