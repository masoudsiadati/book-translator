"""The only structural-repair layer that touches the filesystem.

``plan_structural_repair`` diagnoses one QA-rejected job and, when the defect is a separator
defect, renders and judges the repaired text without writing anything. ``execute_structural_repair``
publishes that repair and makes it the job's effective mechanical verdict. The ordinary stage split
matters more than usual here because this is the one remedy that changes stored translated bytes
without a provider: an operator has to be able to see exactly what would change, and prove it is
whitespace, before authorizing it.

Three rules bound what may be published.

**The provider attempt is never touched.** The repaired text is a new derived artifact beside the
attempt it descends from, and the attempt, its translation, and its acceptance sidecar stay
byte-for-byte as they were. What moves is the job's effective verdict, exactly as a provider-free
re-evaluation moves it.

**A repair may only remove findings.** The repaired text is judged by the installed validators and
adopted only when block correspondence is restored and no blocking code appears that the standing
rejection did not already carry. Structural repair is not allowed to become a way of getting a
weaker QA answer.

**Nothing is guessed.** Whether a deterministic repair exists at all is decided by the pure engine,
which fails closed; this layer never widens that answer.
"""

import os
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from book_translator.models import (
    SCHEMA_VERSION,
    TerminologyRegistry,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
    TranslationStructuralRepairEvidence,
)
from book_translator.paragraph_alignment import (
    ALIGNMENT_QA_VERSION,
    LEGACY_POSITIONAL,
    assess_alignment,
    build_alignment_report,
    policy_for,
)
from book_translator.paths import ProjectPaths
from book_translator.prompt_packaging.identity import uses_paragraph_markers
from book_translator.provider_execution.artifacts import (
    load_yaml,
    private_write,
    project_relative,
    serialize_yaml,
    sha256_bytes,
)
from book_translator.structural_repair.engine import (
    STRUCTURAL_REPAIR_ENGINE_VERSION,
    StructuralDiagnosis,
    diagnose_structure,
)
from book_translator.structural_repair.models import (
    StructuralRepairPlan,
    StructuralRepairResult,
)
from book_translator.structural_repair.reporting import diagnosis_record
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
from book_translator.translation_repair import EffectiveRejection, load_effective_rejection


class StructuralRepairError(ValueError):
    """Raised when a translation's block structure cannot be repaired safely."""


def _job(state: QueueState, job_id: str) -> TranslationJob:
    selected = next((item for item in state.jobs if item.job_id == job_id), None)
    if selected is None:
        raise StructuralRepairError(f"translation job not found: {job_id}")
    return selected


def _repair_root(project_root: Path, job: TranslationJob) -> Path:
    return (
        ProjectPaths(project_root).translation_structural_repair(job.run_id, job.chunk_id, 1).parent
    )


def _history(project_root: Path, job: TranslationJob) -> tuple[Path, ...]:
    """Return every published structural repair of one job, oldest first."""
    root = _repair_root(project_root, job)
    if not root.is_dir():
        return ()
    directories = sorted(path for path in root.iterdir() if path.is_dir())
    for index, directory in enumerate(directories, start=1):
        if directory.name != f"{index:03d}":
            raise StructuralRepairError("structural repair numbering is not contiguous")
    return tuple(directory / "repair.yaml" for directory in directories)


def _frozen_terminology(
    state: QueueState, project_root: Path
) -> tuple[TerminologyRegistry, Path | None, str | None]:
    manifest = load_yaml(
        project_root / state.run.snapshot_path / "manifest.yaml", TranslationManifest
    )
    if manifest.terminology is None:
        return (
            TerminologyRegistry(
                schema_version=SCHEMA_VERSION,
                version="1.0.0",
                book_id=manifest.book.book_id,
                source_language=manifest.book.source_language,
                target_language=manifest.book.target_language,
            ),
            None,
            None,
        )
    frozen_file = manifest.terminology.frozen_file or manifest.terminology.file
    path = project_root / frozen_file
    if sha256_bytes(path.read_bytes()) != manifest.terminology.sha256:
        raise StructuralRepairError("frozen terminology resource hash mismatch")
    return load_registry(path), frozen_file, manifest.terminology.sha256


def _blocking(*reports: object) -> set[str]:
    codes: set[str] = set()
    for report in reports:
        findings = getattr(report, "findings", ())
        codes.update(item.code for item in findings if item.severity == "error")
    return codes


def _verified_text(path: Path, expected_sha256: str, label: str) -> str:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise StructuralRepairError(f"{label} is unavailable") from exc
    if sha256_bytes(content) != expected_sha256:
        raise StructuralRepairError(f"{label} hash mismatch")
    try:
        return content.decode("utf-8")
    except UnicodeError as exc:
        raise StructuralRepairError(f"{label} is not valid UTF-8") from exc


def _refused(
    job: TranslationJob,
    rejection: EffectiveRejection,
    diagnosis: StructuralDiagnosis,
    *,
    repair_number: int,
    refusals: tuple[str, ...],
) -> StructuralRepairPlan:
    standing = _blocking(rejection.terminology_report, rejection.structural_report)
    return StructuralRepairPlan(
        run_id=job.run_id,
        job_id=job.job_id,
        chunk_id=job.chunk_id,
        job=job,
        diagnosis=diagnosis,
        applicable=False,
        refusals=refusals,
        repair_number=repair_number,
        previous_status=TranslationAcceptanceStatus.QA_REJECTED,
        projected_status=TranslationAcceptanceStatus.QA_REJECTED,
        resolved_blocking_codes=(),
        persisting_blocking_codes=tuple(sorted(standing)),
        repaired_text=None,
        evidence=None,
        existing_evidence_file=None,
    )


def plan_structural_repair(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
    now: Callable[[], datetime] | None = None,
) -> StructuralRepairPlan:
    """Diagnose and, where possible, fully derive one provider-free structural repair.

    Reads only. Every input is hash-verified before it is used, and the repaired text is judged
    by the installed validators here rather than after publication, so a caller can see the QA
    answer the repair would produce before authorizing anything.
    """
    project_root = project_root.resolve()
    timestamp = (now or (lambda: datetime.now(UTC)))()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    state = _load_queue(run_path, project_root)
    job = _job(state, job_id)
    if (
        job.status is not TranslationJobStatus.SUCCEEDED
        or job.acceptance_status is not TranslationAcceptanceStatus.QA_REJECTED
    ):
        raise StructuralRepairError(
            "only a provider-succeeded QA-rejected job can be structurally repaired"
        )
    rejection = load_effective_rejection(job, project_root=project_root)
    if rejection.carry_forward_number is not None:
        raise StructuralRepairError(
            "a carried-forward historical draft is repaired only by an explicit operator repair"
        )
    evidence = rejection.evidence
    source_text = _verified_text(
        project_root / evidence.source_file, evidence.source_sha256, "frozen source"
    )
    original_text = _verified_text(
        project_root / evidence.translation_file,
        evidence.translation_sha256,
        "standing translation",
    )
    diagnosis = diagnose_structure(source_text, original_text)
    published = _history(project_root, job)
    if uses_paragraph_markers(rejection.attempt.output_contract):
        # Under the numbered paragraph contract a provider-successful draft has exactly one block
        # per source paragraph, proven at the provider boundary.  A block-count difference here
        # therefore means a stored artifact is corrupt rather than that a separator is misplaced,
        # and re-deriving separators would satisfy the count by splitting a paragraph nobody
        # proved is the right one.  That is precisely the failure this refusal exists to prevent.
        return _refused(
            job,
            rejection,
            diagnosis,
            repair_number=len(published) + 1,
            refusals=("marked_contract_forbids_deterministic_separator_repair",),
        )
    if job.latest_structural_repair_file is not None:
        # One structural repair already defines this job's effective verdict. Repairing the
        # repair would build a supersession chain nothing downstream can follow, and there is
        # nothing left to repair anyway: the first one restored block correspondence or was
        # never adopted.
        return _refused(
            job,
            rejection,
            diagnosis,
            repair_number=len(published),
            refusals=("structural_repair_already_effective",),
        )
    if not diagnosis.deterministic:
        return _refused(
            job,
            rejection,
            diagnosis,
            repair_number=len(published) + 1,
            refusals=diagnosis.refusals or ("block_structure_already_corresponds",),
        )
    repaired_text = diagnosis.repaired_text
    assert repaired_text is not None
    repaired_sha256 = sha256_bytes(repaired_text.encode("utf-8"))
    # An identical repair already on disk is the same repair, whatever interrupted the last
    # attempt to adopt it; re-publishing it under a new number would orphan evidence.
    reusable = next(
        (
            (path, record)
            for path, record in (
                (path, load_yaml(path, TranslationStructuralRepairEvidence)) for path in published
            )
            if record.superseded_evidence_sha256 == rejection.evidence_sha256
            and record.translation_sha256 == repaired_sha256
        ),
        None,
    )
    repair_number = reusable[1].repair_number if reusable else len(published) + 1
    registry, terminology_file, terminology_sha256 = _frozen_terminology(state, project_root)
    exemptions = frozen_occurrence_exemptions(
        load_yaml(project_root / state.run.snapshot_path / "manifest.yaml", TranslationManifest),
        project_root,
        registry=registry,
        chunk_id=job.chunk_id,
        source_text=source_text,
    )
    terminology_report = qa_validation.validate_terminology(
        source_text, repaired_text, registry, exemptions
    )
    structural_report = qa_validation.validate_translation_qa(
        source_text, repaired_text, target_language=job.target_language
    )
    alignment_report = build_alignment_report(
        assess_alignment(
            source_text,
            repaired_text,
            policy=policy_for(job.target_language),
            contract_mode=LEGACY_POSITIONAL,
        ),
        source_text=source_text,
        translation_text=repaired_text,
    )
    standing = _blocking(rejection.terminology_report, rejection.structural_report)
    # Alignment codes join the resulting set but never the standing one, and that asymmetry is
    # correct rather than an oversight: a repair only exists because the block counts differed,
    # and differing counts make alignment unevaluable, so the rejected text cannot have carried a
    # blocking alignment finding.  Anything blocking here was introduced by this repair — which
    # is exactly the case of restoring count equality by splitting the wrong paragraph.
    resulting = _blocking(terminology_report, structural_report) | {
        item.code for item in alignment_report.findings if item.severity == "error"
    }
    introduced = sorted(resulting - standing)
    if introduced:
        return _refused(
            job,
            rejection,
            diagnosis,
            repair_number=repair_number,
            refusals=tuple(f"repair_introduces:{code}" for code in introduced),
        )
    if not structural_report.valid and "paragraph_count_mismatch" in resulting:
        return _refused(
            job,
            rejection,
            diagnosis,
            repair_number=repair_number,
            refusals=("repair_does_not_restore_block_correspondence",),
        )
    accepted = terminology_report.valid and structural_report.valid and alignment_report.valid
    status = (
        TranslationAcceptanceStatus.ACCEPTED
        if accepted
        else TranslationAcceptanceStatus.QA_REJECTED
    )
    destination = ProjectPaths(project_root).translation_structural_repair(
        job.run_id, job.chunk_id, repair_number
    )
    record = TranslationStructuralRepairEvidence(
        schema_version=SCHEMA_VERSION,
        structural_repair_engine_version=STRUCTURAL_REPAIR_ENGINE_VERSION,
        terminology_qa_version=qa_validation.TERMINOLOGY_QA_VERSION,
        structural_qa_version=qa_validation.STRUCTURAL_QA_VERSION,
        alignment_qa_version=ALIGNMENT_QA_VERSION,
        run_id=job.run_id,
        job_id=job.job_id,
        repair_number=repair_number,
        superseded_evidence_file=rejection.evidence_file,
        superseded_evidence_sha256=rejection.evidence_sha256,
        superseded_origin=rejection.origin,
        attempt_id=rejection.attempt.attempt_id,
        attempt_number=evidence.attempt_number,
        attempt_file=rejection.attempt_file,
        attempt_sha256=rejection.attempt_sha256,
        source_file=evidence.source_file,
        source_sha256=evidence.source_sha256,
        original_translation_file=evidence.translation_file,
        original_translation_sha256=evidence.translation_sha256,
        translation_file=project_relative(destination / "translation.md", project_root),
        translation_sha256=repaired_sha256,
        terminology_file=terminology_file,
        terminology_sha256=terminology_sha256,
        diagnosis=diagnosis_record(diagnosis),
        superseded_blocking_codes=sorted(standing),
        terminology_report=terminology_report,
        structural_report=structural_report,
        alignment_report=alignment_report,
        previous_status=TranslationAcceptanceStatus.QA_REJECTED,
        resulting_status=status,
        accepted=accepted,
        created_at=timestamp,
    )
    return StructuralRepairPlan(
        run_id=job.run_id,
        job_id=job.job_id,
        chunk_id=job.chunk_id,
        job=job,
        diagnosis=diagnosis,
        applicable=True,
        refusals=(),
        repair_number=repair_number,
        previous_status=TranslationAcceptanceStatus.QA_REJECTED,
        projected_status=status,
        resolved_blocking_codes=tuple(sorted(standing - resulting)),
        persisting_blocking_codes=tuple(sorted(resulting)),
        repaired_text=repaired_text,
        evidence=record,
        existing_evidence_file=reusable[0] if reusable else None,
    )


def _publish(plan: StructuralRepairPlan, project_root: Path) -> tuple[Path, Path]:
    evidence = plan.evidence
    assert evidence is not None and plan.repaired_text is not None
    destination = ProjectPaths(project_root).translation_structural_repair(
        plan.run_id, plan.chunk_id, evidence.repair_number
    )
    evidence_path = destination / "repair.yaml"
    if destination.exists():
        persisted = load_yaml(evidence_path, TranslationStructuralRepairEvidence)
        if persisted.model_dump(exclude={"created_at"}) != evidence.model_dump(
            exclude={"created_at"}
        ):
            raise StructuralRepairError(
                "existing structural repair evidence conflicts with the current plan"
            )
        return evidence_path, destination / "translation.md"
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    staging = Path(tempfile.mkdtemp(prefix=".structural-repair.staging.", dir=destination.parent))
    os.chmod(staging, 0o700)
    try:
        private_write(staging / "translation.md", plan.repaired_text.encode("utf-8"))
        private_write(staging / "repair.yaml", serialize_yaml(evidence).encode("utf-8"))
        os.rename(staging, destination)
    except FileExistsError as exc:
        raise StructuralRepairError("structural repair destination exists") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return evidence_path, destination / "translation.md"


def _adopt(
    plan: StructuralRepairPlan,
    state: QueueState,
    evidence_path: Path,
    project_root: Path,
    now: datetime,
) -> TranslationJob:
    evidence = plan.evidence
    assert evidence is not None
    job = _job(state, plan.job_id)
    changed = job.model_copy(
        update={
            "acceptance_status": evidence.resulting_status,
            "acceptance_origin": TranslationAcceptanceOrigin.STRUCTURAL_REPAIR,
            "structural_repair_count": evidence.repair_number,
            "latest_structural_repair_file": project_relative(evidence_path, project_root),
            "latest_structural_repair_sha256": sha256_bytes(evidence_path.read_bytes()),
            "updated_at": now,
            "recent_events": _append_event(
                job,
                _event(
                    "translation_structurally_repaired",
                    now,
                    reason=f"{evidence.diagnosis.defect_class.value}:"
                    f"{evidence.resulting_status.value}",
                ),
            ),
        }
    )
    if changed != job:
        _persist_job_and_run(
            state,
            tuple(changed if item.job_id == changed.job_id else item for item in state.jobs),
            changed,
            now,
        )
    return changed


def repair_translation_structure(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
    dry_run: bool = True,
    now: Callable[[], datetime] | None = None,
) -> StructuralRepairResult:
    """Repair one job's block structure without contacting any provider.

    A dry run diagnoses, renders, and judges the repair and writes nothing at all — which is the
    same work the applied path does, so the two can never disagree about what would happen.
    """
    project_root = project_root.resolve()
    timestamp = (now or (lambda: datetime.now(UTC)))()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    plan = plan_structural_repair(
        run_path, job_id, project_root=project_root, now=lambda: timestamp
    )
    if dry_run or not plan.applicable:
        return StructuralRepairResult(
            plan=plan,
            applied=False,
            reused=False,
            dry_run=dry_run,
            resulting_status=plan.previous_status,
            evidence_file=None,
            translation_file=None,
        )
    with queue_lock(run_path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        confirmed = plan_structural_repair(
            run_path, job_id, project_root=project_root, now=lambda: timestamp
        )
        if not confirmed.applicable or confirmed.evidence is None:
            raise StructuralRepairError(
                "structural repair changed after planning: " + ", ".join(confirmed.refusals)
            )
        state = _load_queue(run_path, project_root)
        evidence_path, translation_path = _publish(confirmed, project_root)
        job = _adopt(confirmed, state, evidence_path, project_root, timestamp)
        return StructuralRepairResult(
            plan=confirmed,
            applied=True,
            reused=confirmed.already_applied,
            dry_run=False,
            resulting_status=job.acceptance_status or confirmed.projected_status,
            evidence_file=project_relative(evidence_path, project_root),
            translation_file=project_relative(translation_path, project_root),
        )


__all__ = [
    "StructuralRepairError",
    "plan_structural_repair",
    "repair_translation_structure",
]
