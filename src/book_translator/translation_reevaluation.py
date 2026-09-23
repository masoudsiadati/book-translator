"""Explicit immutable QA re-evaluation and effective-acceptance reconciliation."""

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from book_translator.models import (
    SCHEMA_VERSION,
    TerminologyRegistry,
    TranslationAcceptanceEvidence,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationHistoricalReevaluationEvidence,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
    TranslationQAReevaluationEvidence,
)
from book_translator.paths import ProjectPaths
from book_translator.provider_execution import validate_attempt
from book_translator.provider_execution.artifacts import (
    load_yaml,
    private_write,
    project_relative,
    serialize_yaml,
    sha256_bytes,
)
from book_translator.terminology import load_registry
from book_translator.terminology import validation as qa_validation
from book_translator.terminology.exemption_workflow import frozen_occurrence_exemptions
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
from book_translator.translation_snapshot import validate_snapshot
from book_translator.translation_worker import _validate_evidence_files

QA_REEVALUATION_ENGINE_VERSION = "1.1.0"


class TranslationReevaluationError(ValueError):
    """Raised when historical QA cannot be re-evaluated safely."""


@dataclass(frozen=True, slots=True)
class ReevaluationPlan:
    run_id: str
    job_id: str
    attempt_number: int
    reevaluation_id: str
    reevaluation_number: int
    previous_status: TranslationAcceptanceStatus
    resulting_status: TranslationAcceptanceStatus
    terminology_finding_codes: tuple[str, ...]
    structural_finding_codes: tuple[str, ...]
    evidence: TranslationQAReevaluationEvidence
    existing_file: Path | None


@dataclass(frozen=True, slots=True)
class ReevaluationResult:
    run_id: str
    job_id: str
    attempt_number: int
    reevaluation_id: str
    reevaluation_number: int
    previous_status: TranslationAcceptanceStatus
    resulting_status: TranslationAcceptanceStatus
    terminology_qa_version: str
    structural_qa_version: str
    terminology_finding_codes: tuple[str, ...]
    structural_finding_codes: tuple[str, ...]
    evidence_file: Path | None
    reused: bool
    dry_run: bool


@dataclass(frozen=True, slots=True)
class ReevaluationHistory:
    historical_status: TranslationAcceptanceStatus
    effective_status: TranslationAcceptanceStatus
    acceptance_origin: TranslationAcceptanceOrigin
    attempt_number: int
    records: tuple[TranslationQAReevaluationEvidence, ...]


def _job(state: QueueState, job_id: str) -> TranslationJob:
    selected = next((item for item in state.jobs if item.job_id == job_id), None)
    if selected is None:
        raise TranslationReevaluationError(f"translation job not found: {job_id}")
    return selected


def _replace_job(
    jobs: tuple[TranslationJob, ...], changed: TranslationJob
) -> tuple[TranslationJob, ...]:
    return tuple(changed if item.job_id == changed.job_id else item for item in jobs)


def _identity(payload: dict[str, object]) -> str:
    canonical = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return f"qre-{hashlib.sha256(canonical).hexdigest()[:24]}"


def _reevaluation_root(project_root: Path, job: TranslationJob) -> Path:
    return ProjectPaths(project_root).translation_reevaluation(job.run_id, job.chunk_id, 1).parent


def _load_history(root: Path) -> tuple[tuple[Path, TranslationQAReevaluationEvidence], ...]:
    if not root.is_dir():
        return ()
    records: list[tuple[Path, TranslationQAReevaluationEvidence]] = []
    directories = sorted(path for path in root.iterdir() if path.is_dir())
    for expected_number, directory in enumerate(directories, start=1):
        if directory.name != f"{expected_number:03d}":
            raise TranslationReevaluationError("re-evaluation history numbering is not contiguous")
        evidence_path = directory / "evidence.yaml"
        evidence = load_yaml(evidence_path, TranslationQAReevaluationEvidence)
        if evidence.reevaluation_number != expected_number:
            raise TranslationReevaluationError("re-evaluation evidence number mismatch")
        records.append((evidence_path, evidence))
    return tuple(records)


def _verified_bytes(path: Path, expected_hash: str, label: str) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise TranslationReevaluationError(f"{label} resource is unavailable") from exc
    if sha256_bytes(content) != expected_hash:
        raise TranslationReevaluationError(f"{label} resource hash mismatch")
    return content


@dataclass(frozen=True, slots=True)
class _JudgedOutput:
    """The immutable draft a re-evaluation judges, and the record that proves where it came from."""

    attempt_id: str
    record_path: Path
    record_bytes: bytes
    translation_file: Path
    translation_sha256: str


def _provider_output(
    state: QueueState, job: TranslationJob, attempt_number: int, project_root: Path, now: datetime
) -> _JudgedOutput:
    attempt_validation = validate_attempt(
        state.path, job.job_id, attempt_number, project_root=project_root, now=lambda: now
    )
    if not attempt_validation.valid:
        raise TranslationReevaluationError("selected provider attempt failed persisted validation")
    attempt_path = (
        ProjectPaths(project_root).translation_attempt(job.run_id, job.chunk_id, attempt_number)
        / "attempt.yaml"
    )
    attempt_bytes = attempt_path.read_bytes()
    attempt = load_yaml(attempt_path, TranslationAttempt)
    if (
        attempt.status is not TranslationAttemptStatus.SUCCEEDED
        or attempt.translation_file is None
        or attempt.translation_sha256 is None
    ):
        raise TranslationReevaluationError("selected attempt is not provider-successful")
    return _JudgedOutput(
        attempt.attempt_id,
        attempt_path,
        attempt_bytes,
        attempt.translation_file,
        attempt.translation_sha256,
    )


def _carried_output(
    job: TranslationJob,
    carry_forward_number: int,
    historical: TranslationAcceptanceEvidence,
    project_root: Path,
) -> _JudgedOutput:
    """Resolve a carried-forward draft through its own carry-forward record, by hash.

    A carried draft has no attempt of this job behind it: the carry-forward acceptance names the
    carry-forward record as its attempt, and that record names the bytes it carried. Every link is
    re-proven here, so re-judging the draft can never adopt bytes the record did not carry.
    """
    if job.latest_carry_forward_file is None or job.latest_carry_forward_sha256 is None:
        raise TranslationReevaluationError("carried-forward job lacks its carry-forward record")
    record_path = project_root / job.latest_carry_forward_file
    record_bytes = _verified_bytes(
        record_path, job.latest_carry_forward_sha256, "carry-forward record"
    )
    record = load_yaml(record_path, TranslationHistoricalReevaluationEvidence)
    if (
        record.run_id != job.run_id
        or record.job_id != job.job_id
        or record.carry_forward_number != carry_forward_number
        or historical.attempt_file != job.latest_carry_forward_file
        or historical.attempt_sha256 != job.latest_carry_forward_sha256
        or record.translation_file != historical.translation_file
        or record.translation_sha256 != historical.translation_sha256
    ):
        raise TranslationReevaluationError("carry-forward lineage does not match its acceptance")
    return _JudgedOutput(
        historical.attempt_id,
        record_path,
        record_bytes,
        historical.translation_file,
        historical.translation_sha256,
    )


def _plan(
    state: QueueState,
    job_id: str,
    attempt_number: int,
    *,
    project_root: Path,
    now: datetime,
) -> ReevaluationPlan:
    job = _job(state, job_id)
    if job.status is not TranslationJobStatus.SUCCEEDED:
        raise TranslationReevaluationError("only provider-succeeded jobs can be re-evaluated")
    if job.structural_repair_count:
        # Re-evaluation judges the provider attempt's own output. A structural repair has already
        # republished the verdict over derived bytes, so running it here would assess a draft
        # this job no longer stands on and quietly regress its acceptance.
        raise TranslationReevaluationError(
            "a structural repair defines this job's effective verdict; re-evaluate the repair"
        )
    if (
        job.acceptance_status is None
        or job.accepted_attempt != attempt_number
        or job.acceptance_file is None
        or job.acceptance_sha256 is None
    ):
        raise TranslationReevaluationError(
            "re-evaluation requires historical acceptance evidence for the selected attempt"
        )

    historical_path = project_root / job.acceptance_file
    historical_bytes = _verified_bytes(
        historical_path, job.acceptance_sha256, "historical acceptance evidence"
    )
    historical = load_yaml(historical_path, TranslationAcceptanceEvidence)
    if (
        historical.run_id != job.run_id
        or historical.job_id != job.job_id
        or historical.attempt_number != attempt_number
        or historical.status
        not in {TranslationAcceptanceStatus.ACCEPTED, TranslationAcceptanceStatus.QA_REJECTED}
    ):
        raise TranslationReevaluationError("historical acceptance evidence lineage mismatch")
    _validate_evidence_files(historical, project_root=project_root)
    # A carried-forward draft is judged through its carry-forward record; every other verdict
    # stands on a provider attempt of this job.
    judged = (
        _carried_output(job, attempt_number, historical, project_root)
        if job.carry_forward_count
        else _provider_output(state, job, attempt_number, project_root, now)
    )

    snapshot_result = validate_snapshot(
        project_root / state.run.snapshot_path, project_root=project_root, now=lambda: now
    )
    if (
        not snapshot_result.report.valid
        or snapshot_result.snapshot is None
        or snapshot_result.manifest is None
        or snapshot_result.snapshot.snapshot_id != job.snapshot_id
        or snapshot_result.snapshot.manifest_sha256 != job.manifest_sha256
    ):
        raise TranslationReevaluationError("frozen translation snapshot failed validation")
    manifest: TranslationManifest = snapshot_result.manifest
    prompt_bytes = _verified_bytes(
        project_root / manifest.prompt.file, manifest.prompt.sha256, "frozen prompt"
    )
    style_bytes = _verified_bytes(
        project_root / manifest.style_guide.file, manifest.style_guide.sha256, "frozen style"
    )
    source_bytes = _verified_bytes(
        project_root / job.chunk_source_file, job.chunk_source_sha256, "frozen source"
    )
    translation_bytes = _verified_bytes(
        project_root / judged.translation_file, judged.translation_sha256, "translation"
    )
    terminology_file = None
    terminology_hash = None
    if manifest.terminology is None:
        registry = TerminologyRegistry(
            schema_version=SCHEMA_VERSION,
            version="1.0.0",
            book_id=manifest.book.book_id,
            source_language=manifest.book.source_language,
            target_language=manifest.book.target_language,
        )
    else:
        terminology_file = manifest.terminology.frozen_file or manifest.terminology.file
        terminology_hash = manifest.terminology.sha256
        terminology_path = project_root / terminology_file
        _verified_bytes(terminology_path, terminology_hash, "frozen terminology")
        registry = load_registry(terminology_path)

    try:
        source_text = source_bytes.decode("utf-8")
        translation_text = translation_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise TranslationReevaluationError("QA inputs are not valid UTF-8") from exc
    exemptions = frozen_occurrence_exemptions(
        manifest,
        project_root,
        registry=registry,
        chunk_id=job.chunk_id,
        source_text=source_text,
    )
    terminology_report = qa_validation.validate_terminology(
        source_text, translation_text, registry, exemptions
    )
    structural_report = qa_validation.validate_translation_qa(
        source_text, translation_text, target_language=job.target_language
    )
    accepted = terminology_report.valid and structural_report.valid
    resulting_status = (
        TranslationAcceptanceStatus.ACCEPTED
        if accepted
        else TranslationAcceptanceStatus.QA_REJECTED
    )
    identity = _identity(
        {
            "reevaluation_engine_version": QA_REEVALUATION_ENGINE_VERSION,
            "terminology_qa_version": qa_validation.TERMINOLOGY_QA_VERSION,
            "structural_qa_version": qa_validation.STRUCTURAL_QA_VERSION,
            "run_id": job.run_id,
            "job_id": job.job_id,
            "attempt_number": attempt_number,
            "attempt_sha256": sha256_bytes(judged.record_bytes),
            "source_sha256": job.chunk_source_sha256,
            "translation_sha256": judged.translation_sha256,
            "terminology_sha256": terminology_hash,
            "prompt_sha256": sha256_bytes(prompt_bytes),
            "style_sha256": sha256_bytes(style_bytes),
            "reevaluated_from_sha256": job.acceptance_sha256,
            # Present only when exemptions reached this job, so earlier identities are unchanged.
            **(
                {"occurrence_exemption_ids": list(exemptions.exemption_ids)}
                if exemptions is not None
                else {}
            ),
        }
    )
    history = _load_history(_reevaluation_root(project_root, job))
    if len(history) not in {job.reevaluation_count, job.reevaluation_count + 1}:
        raise TranslationReevaluationError("job re-evaluation count does not match history")
    if len(history) == job.reevaluation_count and history:
        latest_path, latest = history[-1]
        if job.latest_reevaluation_file is None:
            # A delivered repair superseded the history: it may only describe older verdicts.
            if latest.reevaluated_from_file == job.acceptance_file:
                raise TranslationReevaluationError(
                    "re-evaluation history describes the standing verdict but is not referenced"
                )
        elif (
            job.latest_reevaluation_file != project_relative(latest_path, project_root)
            or job.latest_reevaluation_sha256 != sha256_bytes(latest_path.read_bytes())
            or latest.resulting_status is not job.acceptance_status
        ):
            raise TranslationReevaluationError("effective re-evaluation pointer is inconsistent")
    existing = next(
        ((path, evidence) for path, evidence in history if evidence.reevaluation_id == identity),
        None,
    )
    if len(history) == job.reevaluation_count + 1 and (
        existing is None or existing[1] is not history[-1][1]
    ):
        raise TranslationReevaluationError(
            "unreconciled re-evaluation evidence does not match the current QA identity"
        )
    number = existing[1].reevaluation_number if existing else len(history) + 1
    evidence = TranslationQAReevaluationEvidence(
        schema_version=SCHEMA_VERSION,
        reevaluation_engine_version=QA_REEVALUATION_ENGINE_VERSION,
        terminology_qa_version=qa_validation.TERMINOLOGY_QA_VERSION,
        structural_qa_version=qa_validation.STRUCTURAL_QA_VERSION,
        run_id=job.run_id,
        job_id=job.job_id,
        attempt_id=judged.attempt_id,
        attempt_number=attempt_number,
        reevaluation_id=identity,
        reevaluation_number=number,
        reevaluated_from_file=job.acceptance_file,
        reevaluated_from_sha256=sha256_bytes(historical_bytes),
        attempt_file=project_relative(judged.record_path, project_root),
        attempt_sha256=sha256_bytes(judged.record_bytes),
        source_file=job.chunk_source_file,
        source_sha256=job.chunk_source_sha256,
        translation_file=judged.translation_file,
        translation_sha256=judged.translation_sha256,
        terminology_file=terminology_file,
        terminology_sha256=terminology_hash,
        prompt_file=manifest.prompt.file,
        prompt_sha256=manifest.prompt.sha256,
        style_file=manifest.style_guide.file,
        style_sha256=manifest.style_guide.sha256,
        terminology_report=terminology_report,
        structural_report=structural_report,
        previous_status=job.acceptance_status,
        resulting_status=resulting_status,
        accepted=accepted,
        created_at=existing[1].created_at if existing else now,
    )
    if existing is not None:
        persisted = existing[1]
        if persisted.model_dump(exclude={"previous_status"}) != evidence.model_dump(
            exclude={"previous_status"}
        ):
            raise TranslationReevaluationError(
                "matching re-evaluation identity has conflicting data"
            )
    return ReevaluationPlan(
        job.run_id,
        job.job_id,
        attempt_number,
        identity,
        number,
        job.acceptance_status,
        resulting_status,
        tuple(item.code for item in terminology_report.findings),
        tuple(item.code for item in structural_report.findings),
        evidence,
        existing[0] if existing else None,
    )


def _publish(plan: ReevaluationPlan, project_root: Path, job: TranslationJob) -> tuple[Path, bool]:
    if plan.existing_file is not None:
        return plan.existing_file, True
    root = _reevaluation_root(project_root, job)
    destination = root / f"{plan.reevaluation_number:03d}"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    staging = Path(tempfile.mkdtemp(prefix=".reevaluation.staging.", dir=root))
    os.chmod(staging, 0o700)
    try:
        private_write(staging / "evidence.yaml", serialize_yaml(plan.evidence).encode("utf-8"))
        os.rename(staging, destination)
    except FileExistsError as exc:
        raise TranslationReevaluationError("re-evaluation publication destination exists") from exc
    finally:
        if staging.exists():
            staging.rmdir()
    return destination / "evidence.yaml", False


def reevaluate_translation_job(
    run_id_or_path: str | Path,
    job_id: str,
    attempt_number: int,
    *,
    project_root: Path,
    dry_run: bool = False,
    now: Callable[[], datetime] | None = None,
) -> ReevaluationResult:
    if attempt_number < 1:
        raise TranslationReevaluationError("attempt number must be positive")
    project_root = project_root.resolve()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    if dry_run:
        plan = _plan(
            _load_queue(run_path, project_root),
            job_id,
            attempt_number,
            project_root=project_root,
            now=timestamp,
        )
        return _result(plan, None, plan.existing_file is not None, True)
    with queue_lock(run_path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        state = _load_queue(run_path, project_root)
        plan = _plan(state, job_id, attempt_number, project_root=project_root, now=timestamp)
        selected = _job(state, job_id)
        evidence_path, reused = _publish(plan, project_root, selected)
        evidence_bytes = evidence_path.read_bytes()
        relative_evidence = project_relative(evidence_path, project_root)
        evidence_hash = sha256_bytes(evidence_bytes)
        if (
            reused
            and selected.acceptance_status is plan.resulting_status
            and selected.accepted_attempt == attempt_number
            and selected.acceptance_origin is TranslationAcceptanceOrigin.QA_REEVALUATION
            and selected.latest_reevaluation_file == relative_evidence
            and selected.latest_reevaluation_sha256 == evidence_hash
        ):
            return _result(plan, relative_evidence, True, False)
        changed = selected.model_copy(
            update={
                "acceptance_status": plan.resulting_status,
                "accepted_attempt": attempt_number,
                "acceptance_origin": TranslationAcceptanceOrigin.QA_REEVALUATION,
                "reevaluation_count": max(selected.reevaluation_count, plan.reevaluation_number),
                "latest_reevaluation_file": relative_evidence,
                "latest_reevaluation_sha256": evidence_hash,
                "updated_at": timestamp,
                "recent_events": _append_event(
                    selected,
                    _event(
                        "translation_qa_reevaluated",
                        timestamp,
                        reason=plan.resulting_status.value,
                    ),
                ),
            }
        )
        if changed != selected:
            _persist_job_and_run(state, _replace_job(state.jobs, changed), changed, timestamp)
        return _result(plan, relative_evidence, reused, False)


def _result(
    plan: ReevaluationPlan, evidence_file: Path | None, reused: bool, dry_run: bool
) -> ReevaluationResult:
    return ReevaluationResult(
        plan.run_id,
        plan.job_id,
        plan.attempt_number,
        plan.reevaluation_id,
        plan.reevaluation_number,
        plan.previous_status,
        plan.resulting_status,
        plan.evidence.terminology_qa_version,
        plan.evidence.structural_qa_version,
        plan.terminology_finding_codes,
        plan.structural_finding_codes,
        evidence_file,
        reused,
        dry_run,
    )


def inspect_translation_reevaluations(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
) -> ReevaluationHistory:
    project_root = project_root.resolve()
    state = _load_queue(_resolve_run_path(run_id_or_path, project_root), project_root)
    job = _job(state, job_id)
    if job.acceptance_status is None or job.accepted_attempt is None or job.acceptance_file is None:
        raise TranslationReevaluationError("translation job has no acceptance history")
    historical = load_yaml(project_root / job.acceptance_file, TranslationAcceptanceEvidence)
    if (
        job.acceptance_sha256 is None
        or sha256_bytes((project_root / job.acceptance_file).read_bytes()) != job.acceptance_sha256
    ):
        raise TranslationReevaluationError("historical acceptance evidence hash mismatch")
    records = tuple(
        evidence for _, evidence in _load_history(_reevaluation_root(project_root, job))
    )
    if len(records) != job.reevaluation_count:
        raise TranslationReevaluationError("job re-evaluation count does not match history")
    if records and job.latest_reevaluation_file is not None:
        latest_path = (
            _reevaluation_root(project_root, job)
            / f"{records[-1].reevaluation_number:03d}"
            / "evidence.yaml"
        )
        if (
            job.latest_reevaluation_file != project_relative(latest_path, project_root)
            or job.latest_reevaluation_sha256 != sha256_bytes(latest_path.read_bytes())
            or records[-1].resulting_status is not job.acceptance_status
        ):
            raise TranslationReevaluationError("effective acceptance pointer is inconsistent")
    origin = job.acceptance_origin or TranslationAcceptanceOrigin.INITIAL_QA
    return ReevaluationHistory(
        historical.status,
        job.acceptance_status,
        origin,
        job.accepted_attempt,
        records,
    )
