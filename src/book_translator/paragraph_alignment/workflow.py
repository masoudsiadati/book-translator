"""Read-only alignment audit over one completed translation run.

This is the legacy path.  A run created before the numbered paragraph contract existed has no
markers to check, and retroactively failing its attempts for lacking something nobody asked them
for would be dishonest bookkeeping.  What such a run *can* be asked is the narrower question the
measurements answer on their own: taking block order as the identity, does any paragraph look
like the translation of a different one?

The boundary is deliberately the narrowest in the project.  It opens a run, resolves each job's
effective acceptance through the same canonical resolver release assembly uses, reads the frozen
source and the accepted translation, measures, and writes one report of its own.  It never
contacts a provider, never touches acceptance, status, counters, or any translated byte, and the
report it publishes lives outside every run directory so that even its own output cannot be
mistaken for run state.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.models import (
    SCHEMA_VERSION,
    TranslationAttempt,
    TranslationJob,
)
from book_translator.models.paragraph_alignment import (
    ParagraphAlignmentAuditEntry,
    ParagraphAlignmentAuditReport,
    ParagraphAlignmentFinding,
)
from book_translator.paragraph_alignment.engine import LEGACY_POSITIONAL, MARKED, assess_alignment
from book_translator.paragraph_alignment.policy import ALIGNMENT_QA_VERSION, policy_for
from book_translator.paths import ProjectPaths
from book_translator.prompt_packaging.identity import uses_paragraph_markers
from book_translator.provider_execution import ExecutorError
from book_translator.provider_execution.artifacts import load_yaml, serialize_yaml, sha256_bytes
from book_translator.release_assembly import ReleaseAssemblyError
from book_translator.release_assembly.workflow import load_effective_acceptance
from book_translator.translation_queue import load_run_summary

__all__ = [
    "AlignmentAuditError",
    "AlignmentAuditPlan",
    "execute_alignment_audit",
    "plan_alignment_audit",
]


class AlignmentAuditError(ValueError):
    """Raised when an alignment audit cannot be produced from a run's own evidence."""


@dataclass(frozen=True, slots=True)
class AlignmentAuditPlan:
    """The finished read-only report, plus where it would be written."""

    run_id: str
    report: ParagraphAlignmentAuditReport
    destination: Path


def _verified_text(path: Path, expected_sha256: str, label: str) -> str:
    content = path.read_bytes()
    if sha256_bytes(content) != expected_sha256:
        raise AlignmentAuditError(f"{label} hash mismatch: {path}")
    return content.decode("utf-8")


def _contract_mode(attempt_file: Path, project_root: Path, *, run_contract: str | None) -> str:
    """Report the output contract the accepted bytes were actually produced under.

    A carried-forward translation names its reuse evidence where an ordinary acceptance names an
    attempt, because the attempt it descends from belongs to another run.  Rather than reach into
    that run, the audit falls back to the contract this run was created against, which is the
    contract the reuse was proven compatible with.
    """
    try:
        attempt = load_yaml(project_root / attempt_file, TranslationAttempt)
    except (ExecutorError, ValidationError, OSError):
        return MARKED if uses_paragraph_markers(run_contract) else LEGACY_POSITIONAL
    return MARKED if uses_paragraph_markers(attempt.output_contract) else LEGACY_POSITIONAL


def _entry(
    job: TranslationJob, *, project_root: Path, run_contract: str | None
) -> ParagraphAlignmentAuditEntry:
    acceptance = load_effective_acceptance(job, project_root=project_root)
    source_text = _verified_text(
        project_root / acceptance.source_file, acceptance.source_sha256, "frozen source"
    )
    translation_text = _verified_text(
        project_root / acceptance.translation_file,
        acceptance.translation_sha256,
        "accepted translation",
    )
    assessment = assess_alignment(
        source_text,
        translation_text,
        policy=policy_for(job.target_language),
        contract_mode=_contract_mode(
            acceptance.attempt_file, project_root, run_contract=run_contract
        ),
    )
    findings = [
        ParagraphAlignmentFinding(
            code=finding.code,
            severity="error" if finding.severity == "error" else "warning",
            source_paragraph_index=finding.source_paragraph_index,
            related_paragraph_index=finding.related_paragraph_index,
            expected_marker=finding.expected_marker,
            observed_marker=finding.observed_marker,
            message=finding.message,
        )
        for finding in assessment.findings
    ]
    return ParagraphAlignmentAuditEntry(
        job_id=job.job_id,
        chapter_id=job.chapter_id,
        chunk_id=job.chunk_id,
        acceptance_origin=acceptance.origin.value,
        contract_mode="marked" if assessment.contract_mode == MARKED else "legacy_positional",
        evaluated=assessment.evaluated,
        aligned_pairs=len(assessment.metrics),
        measured_pairs=assessment.measured_pairs,
        flagged_pairs=assessment.flagged_pairs,
        error_findings=sum(1 for item in findings if item.severity == "error"),
        warning_findings=sum(1 for item in findings if item.severity == "warning"),
        findings=findings,
    )


def plan_alignment_audit(
    run_id_or_path: str | Path,
    *,
    project_root: Path,
    chapter_id: str | None = None,
    job_id: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> AlignmentAuditPlan:
    """Measure every effectively accepted translation in one run, changing nothing."""
    project_root = project_root.resolve()
    state = load_run_summary(run_id_or_path, project_root=project_root)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    selected = tuple(
        job
        for job in state.jobs
        if (chapter_id is None or job.chapter_id == chapter_id)
        and (job_id is None or job.job_id == job_id)
    )
    if not selected:
        raise AlignmentAuditError("no translation jobs match the audit selection")
    entries: list[ParagraphAlignmentAuditEntry] = []
    skipped: list[str] = []
    for job in selected:
        try:
            entries.append(
                _entry(
                    job,
                    project_root=project_root,
                    run_contract=state.run.output_contract_version,
                )
            )
        except (
            ReleaseAssemblyError,
            AlignmentAuditError,
            ExecutorError,
            ValidationError,
            OSError,
        ) as exc:
            # A job without an effective acceptance is not a finding about the translation; the
            # audit records that it could not be measured rather than inventing a verdict.
            skipped.append(f"{job.chunk_id}: {exc}")
    counts: dict[str, int] = {}
    for entry in entries:
        for finding in entry.findings:
            counts[finding.code] = counts.get(finding.code, 0) + 1
    policy = policy_for(state.run.target_language)
    report = ParagraphAlignmentAuditReport(
        schema_version=SCHEMA_VERSION,
        alignment_qa_version=ALIGNMENT_QA_VERSION,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        run_id=state.run.run_id,
        snapshot_id=state.run.snapshot_id,
        run_output_contract_version=state.run.output_contract_version,
        assessed_jobs=len(entries),
        skipped_jobs=len(skipped),
        aligned_pairs=sum(entry.aligned_pairs for entry in entries),
        measured_pairs=sum(entry.measured_pairs for entry in entries),
        jobs_with_findings=sum(1 for entry in entries if entry.findings),
        jobs_with_blocking_findings=sum(1 for entry in entries if entry.error_findings),
        finding_counts=dict(sorted(counts.items())),
        entries=entries,
        skipped=skipped,
        created_at=timestamp,
    )
    destination = (
        ProjectPaths(project_root).paragraph_alignment_audit(state.run.run_id) / "report.yaml"
    )
    return AlignmentAuditPlan(state.run.run_id, report, destination)


def execute_alignment_audit(plan: AlignmentAuditPlan) -> Path:
    """Publish the audit report atomically, outside every run directory."""
    plan.destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(plan.destination, serialize_yaml(plan.report))
    return plan.destination
