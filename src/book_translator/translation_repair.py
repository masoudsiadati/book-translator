"""Explicit, provider-neutral repair of QA-rejected translation attempts.

A repair request is *derived*, not frozen. An ordinary translation is defined by a rendered prompt
package whose bytes are on disk and hash-checked; a repair is built here, at the moment it is
sent, out of the rejected draft, the QA findings that rejected it, and the frozen resources the
run was created against. That makes the derivation itself part of the contract: two callers
building the same repair must land on the same bytes, and a caller that wants to *re-send* a
repair — bounded provider recovery is the only one — has to be able to prove that it did.

:class:`RepairRequest` is that proof surface. It carries the messages to send plus a hash of every
input the derivation consumed, so re-deriving a repair and comparing it against what a failed
attempt recorded is a hash comparison rather than an act of faith. Nothing in it is persisted by
this module; the hashes exist so the layers above can persist hashes instead of prose.
"""

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from book_translator.config import load_book_config
from book_translator.models import (
    ProviderName,
    TerminologyRegistry,
    TerminologyValidationReport,
    TranslationAcceptanceEvidence,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationAlignmentReport,
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationExecutionKind,
    TranslationHistoricalReevaluationEvidence,
    TranslationJob,
    TranslationJobStatus,
    TranslationManifest,
    TranslationQAReevaluationEvidence,
    TranslationQAReport,
    TranslationRepairInitiator,
    TranslationRunStatus,
    TranslationStructuralRepairEvidence,
)
from book_translator.paragraph_markers import mark_paragraphs, source_paragraphs, strip_markers
from book_translator.paths import ProjectPaths
from book_translator.prompt_packaging.identity import uses_paragraph_markers
from book_translator.prompt_packaging.rendering import PARAGRAPH_MARKER_RULES
from book_translator.provider_execution import (
    ExecutionResult,
    TranslationProvider,
    ensure_provider_model_allowed,
    execute_translation_job,
    plan_execution,
)
from book_translator.provider_execution.artifacts import canonical_json, load_yaml, sha256_bytes
from book_translator.terminology.registry import authorized_forms_payload
from book_translator.translation_queue.locking import queue_lock
from book_translator.translation_queue.policy import attempt_budget_exhausted
from book_translator.translation_queue.workflow import (
    LOCK_TIMEOUT_SECONDS,
    ClaimResult,
    QueueState,
    _append_event,
    _event,
    _load_queue,
    _persist_job_and_run,
    _resolve_run_path,
)
from book_translator.translation_repair_policy import is_active_repair, is_failed_repair

#: The original derivation: findings, source, rejected draft.  It is frozen at 1.0.0 forever
#: because a bounded provider recovery re-derives a failed repair and proves it byte-identical to
#: the one that failed; changing this derivation would make every historical repair
#: irreproducible and therefore unrecoverable.
REPAIR_PROMPT_VERSION = "1.0.0"

#: The derivation for a job whose rejected draft was produced under the numbered paragraph
#: contract.  It re-attaches the same markers to both the source and the draft, carries the
#: frozen previous context the original derivation omitted, and restates the marker rules, so a
#: repair cannot answer a structural rejection by moving prose between paragraph identities.
MARKED_REPAIR_PROMPT_VERSION = "2.0.0"


class TranslationRepairError(ValueError):
    """Raised when an immutable rejected attempt cannot safely be repaired."""


@dataclass(frozen=True, slots=True)
class RepairResult:
    run_id: str
    job_id: str
    #: ``None`` when the repaired draft is a carried-forward historical output (see below).
    repaired_from_attempt: int | None
    attempt_number: int
    provider: ProviderName
    provider_contacted: bool
    attempt_status: TranslationAttemptStatus | None
    acceptance_status: TranslationAcceptanceStatus | None
    dry_run: bool
    initiator: TranslationRepairInitiator = TranslationRepairInitiator.OPERATOR
    repaired_from_carry_forward: int | None = None


@dataclass(frozen=True, slots=True)
class EffectiveRejection:
    """The hash-verified rejection a repair would answer, without any translated content.

    Effective acceptance is not always the original sidecar: a provider-free re-evaluation
    republishes the verdict under its own evidence. Both the repair request and the auto-repair
    policy must read the *same* effective rejection, so resolving it lives here once rather than
    twice.
    """

    evidence: (
        TranslationAcceptanceEvidence
        | TranslationQAReevaluationEvidence
        | TranslationStructuralRepairEvidence
    )
    origin: TranslationAcceptanceOrigin
    evidence_file: Path
    evidence_sha256: str
    attempt: TranslationAttempt
    attempt_file: Path
    attempt_sha256: str
    terminology_report: TerminologyValidationReport
    structural_report: TranslationQAReport
    #: Present only on evidence published after alignment QA existed.  ``None`` means the
    #: rejection was never judged against that gate, which is different from passing it.
    alignment_report: TranslationAlignmentReport | None
    terminology_qa_version: str | None
    structural_qa_version: str | None
    #: Set when the rejected draft was carried forward from a historical run rather than produced
    #: by an attempt of this job; ``attempt`` is then the historical attempt the draft came from,
    #: hash-verified through the carry-forward record, and it supplies the output contract.
    carry_forward_number: int | None = None


def _carried_forward_rejection(job: TranslationJob, *, project_root: Path) -> EffectiveRejection:
    """Resolve a rejection standing on a governed carry-forward, verifying every link by hash."""
    assert job.acceptance_file is not None and job.acceptance_sha256 is not None
    if job.latest_carry_forward_file is None or job.latest_carry_forward_sha256 is None:
        raise TranslationRepairError("carried-forward rejection lacks its carry-forward record")
    acceptance_path = project_root / job.acceptance_file
    if sha256_bytes(acceptance_path.read_bytes()) != job.acceptance_sha256:
        raise TranslationRepairError("rejection evidence hash mismatch")
    acceptance = load_yaml(acceptance_path, TranslationAcceptanceEvidence)
    record_path = project_root / job.latest_carry_forward_file
    record_bytes = record_path.read_bytes()
    if (
        sha256_bytes(record_bytes) != job.latest_carry_forward_sha256
        or acceptance.attempt_file != job.latest_carry_forward_file
        or acceptance.attempt_sha256 != job.latest_carry_forward_sha256
    ):
        raise TranslationRepairError("carry-forward record does not match its rejection evidence")
    record = load_yaml(record_path, TranslationHistoricalReevaluationEvidence)
    if (
        record.job_id != job.job_id
        or record.resulting_status is not TranslationAcceptanceStatus.QA_REJECTED
        or acceptance.status is not TranslationAcceptanceStatus.QA_REJECTED
        or acceptance.attempt_number != record.carry_forward_number
        or job.accepted_attempt != record.carry_forward_number
        or acceptance.translation_sha256 != record.translation_sha256
    ):
        raise TranslationRepairError("carry-forward rejection lineage mismatch")
    historical_path = project_root / record.source_attempt_file
    if sha256_bytes(historical_path.read_bytes()) != record.source_attempt_sha256:
        raise TranslationRepairError("historical attempt behind the carry-forward changed")
    historical = load_yaml(historical_path, TranslationAttempt)
    if (
        historical.status is not TranslationAttemptStatus.SUCCEEDED
        or historical.translation_sha256 != record.source_translation_sha256
    ):
        raise TranslationRepairError("carry-forward does not name a successful historical attempt")
    rejection = EffectiveRejection(
        evidence=acceptance,
        origin=TranslationAcceptanceOrigin.HISTORICAL_REEVALUATION,
        evidence_file=job.acceptance_file,
        evidence_sha256=job.acceptance_sha256,
        attempt=historical,
        attempt_file=record.source_attempt_file,
        attempt_sha256=record.source_attempt_sha256,
        terminology_report=acceptance.terminology_report,
        structural_report=acceptance.structural_report,
        alignment_report=acceptance.alignment_report,
        terminology_qa_version=acceptance.terminology_qa_version,
        structural_qa_version=acceptance.structural_qa_version,
        carry_forward_number=record.carry_forward_number,
    )
    if job.acceptance_origin is not TranslationAcceptanceOrigin.QA_REEVALUATION:
        return rejection
    # Current QA re-judged the carried draft and still rejects it. The re-evaluation names the
    # carry-forward acceptance it re-judged and the same carried bytes, so the draft being repaired
    # is unchanged; only the findings the repair answers are the current ones.
    if job.latest_reevaluation_file is None or job.latest_reevaluation_sha256 is None:
        raise TranslationRepairError("re-evaluated carried rejection lacks its evidence")
    reevaluation_path = project_root / job.latest_reevaluation_file
    if sha256_bytes(reevaluation_path.read_bytes()) != job.latest_reevaluation_sha256:
        raise TranslationRepairError("re-evaluation evidence hash mismatch")
    reevaluation = load_yaml(reevaluation_path, TranslationQAReevaluationEvidence)
    if (
        reevaluation.reevaluated_from_file != job.acceptance_file
        or reevaluation.reevaluated_from_sha256 != job.acceptance_sha256
        or reevaluation.resulting_status is not TranslationAcceptanceStatus.QA_REJECTED
        or reevaluation.attempt_number != record.carry_forward_number
        or reevaluation.translation_sha256 != acceptance.translation_sha256
    ):
        raise TranslationRepairError("re-evaluated carried rejection lineage mismatch")
    return replace(
        rejection,
        evidence=reevaluation,
        origin=TranslationAcceptanceOrigin.QA_REEVALUATION,
        evidence_file=job.latest_reevaluation_file,
        evidence_sha256=job.latest_reevaluation_sha256,
        terminology_report=reevaluation.terminology_report,
        structural_report=reevaluation.structural_report,
        alignment_report=None,
        terminology_qa_version=reevaluation.terminology_qa_version,
        structural_qa_version=reevaluation.structural_qa_version,
    )


def load_effective_rejection(
    job: TranslationJob,
    *,
    project_root: Path,
    allow_failed_repair: bool = False,
    allow_active_repair: bool = False,
) -> EffectiveRejection:
    """Resolve and hash-verify the effective mechanical rejection of one job.

    This proves the rejection is real and untampered; it deliberately says nothing about whether
    a repair is permitted, which is the caller's policy or budget question.

    ``allow_failed_repair`` additionally accepts the one state a failed repair leaves behind: the
    job is ``failed``, but the rejection it was answering is untouched and still standing. Only
    the recovery boundary passes it, and only to *read* — deciding whether that repair may be
    re-sent, and restoring the draft first if it may. Every hash check below still applies, so
    the tolerance widens which job states may be read and nothing else.

    ``allow_active_repair`` accepts the state a repair occupies while it is *in flight*: claimed,
    or running behind an execution reservation. Only the reservation-resolution boundary passes
    it, and only to read — to prove that a half-finished execution really was answering this
    rejection before it decides that the rejection does not supersede the attempt. The same
    caveat applies: every hash check still runs, and nothing else widens.
    """
    repairable_state = (
        job.status is TranslationJobStatus.SUCCEEDED
        or (allow_failed_repair and is_failed_repair(job))
        or (allow_active_repair and is_active_repair(job))
    )
    if (
        not repairable_state
        or job.acceptance_status is not TranslationAcceptanceStatus.QA_REJECTED
        or job.accepted_attempt is None
        or job.acceptance_file is None
        or job.acceptance_sha256 is None
        or job.acceptance_origin is None
    ):
        raise TranslationRepairError("only a provider-succeeded QA-rejected job can be repaired")
    if job.carry_forward_count and job.acceptance_origin in {
        TranslationAcceptanceOrigin.HISTORICAL_REEVALUATION,
        TranslationAcceptanceOrigin.QA_REEVALUATION,
    }:
        return _carried_forward_rejection(job, project_root=project_root)
    evidence_path = project_root / job.acceptance_file
    evidence_bytes = evidence_path.read_bytes()
    if sha256_bytes(evidence_bytes) != job.acceptance_sha256:
        raise TranslationRepairError("rejection evidence hash mismatch")
    historical = load_yaml(evidence_path, TranslationAcceptanceEvidence)
    evidence: (
        TranslationAcceptanceEvidence
        | TranslationQAReevaluationEvidence
        | TranslationStructuralRepairEvidence
    ) = historical
    evidence_file = job.acceptance_file
    evidence_sha256 = job.acceptance_sha256
    evidence_status = historical.status
    if job.latest_reevaluation_file is not None:
        if job.latest_reevaluation_sha256 is None:
            raise TranslationRepairError("re-evaluation evidence hash is missing")
        reevaluation_path = project_root / job.latest_reevaluation_file
        reevaluation_bytes = reevaluation_path.read_bytes()
        if sha256_bytes(reevaluation_bytes) != job.latest_reevaluation_sha256:
            raise TranslationRepairError("re-evaluation evidence hash mismatch")
        reevaluation = load_yaml(reevaluation_path, TranslationQAReevaluationEvidence)
        if (
            reevaluation.reevaluated_from_file != job.acceptance_file
            or reevaluation.reevaluated_from_sha256 != job.acceptance_sha256
        ):
            raise TranslationRepairError("re-evaluation evidence lineage mismatch")
        evidence = reevaluation
        evidence_file = job.latest_reevaluation_file
        evidence_sha256 = job.latest_reevaluation_sha256
        evidence_status = reevaluation.resulting_status
    if job.latest_structural_repair_file is not None:
        # A structural repair republishes the mechanical verdict over translated bytes it
        # derived itself, so it supersedes whatever defined effective acceptance before it —
        # and it names that predecessor, which is what makes the supersession checkable.
        if job.latest_structural_repair_sha256 is None:
            raise TranslationRepairError("structural repair evidence hash is missing")
        repair_path = project_root / job.latest_structural_repair_file
        if sha256_bytes(repair_path.read_bytes()) != job.latest_structural_repair_sha256:
            raise TranslationRepairError("structural repair evidence hash mismatch")
        repair = load_yaml(repair_path, TranslationStructuralRepairEvidence)
        if (
            repair.superseded_evidence_file != evidence_file
            or repair.superseded_evidence_sha256 != evidence_sha256
        ):
            raise TranslationRepairError("structural repair evidence lineage mismatch")
        evidence = repair
        evidence_file = job.latest_structural_repair_file
        evidence_sha256 = job.latest_structural_repair_sha256
        evidence_status = repair.resulting_status
    attempt_path = project_root / evidence.attempt_file
    attempt_bytes = attempt_path.read_bytes()
    if sha256_bytes(attempt_bytes) != evidence.attempt_sha256:
        raise TranslationRepairError("rejected attempt metadata hash mismatch")
    attempt = load_yaml(attempt_path, TranslationAttempt)
    if (
        evidence_status is not TranslationAcceptanceStatus.QA_REJECTED
        or evidence.attempt_number != job.accepted_attempt
        or attempt.status is not TranslationAttemptStatus.SUCCEEDED
        or attempt.translation_file is None
    ):
        raise TranslationRepairError("rejection evidence does not identify a successful attempt")
    alignment_report = (
        evidence.alignment_report
        if isinstance(evidence, TranslationAcceptanceEvidence | TranslationStructuralRepairEvidence)
        else None
    )
    return EffectiveRejection(
        evidence=evidence,
        origin=job.acceptance_origin,
        evidence_file=evidence_file,
        evidence_sha256=evidence_sha256,
        attempt=attempt,
        attempt_file=evidence.attempt_file,
        attempt_sha256=evidence.attempt_sha256,
        terminology_report=evidence.terminology_report,
        structural_report=evidence.structural_report,
        alignment_report=alignment_report,
        terminology_qa_version=evidence.terminology_qa_version,
        structural_qa_version=evidence.structural_qa_version,
    )


def _frozen_context(job: TranslationJob, *, project_root: Path) -> str:
    """Return the job's frozen previous context, hash-verified.

    The original repair derivation sent the source and the rejected draft but not the context the
    translation was produced against, so a whole-chunk repair could rewrite an opening paragraph
    without seeing what preceded it.  Supplying it costs nothing and is comprehension-only, the
    same status it has in the ordinary prompt package.
    """
    context_path = project_root / job.context_file
    context_bytes = context_path.read_bytes()
    if sha256_bytes(context_bytes) != job.context_sha256:
        raise TranslationRepairError("frozen previous-context hash mismatch")
    return context_bytes.decode("utf-8")


def _job(state: QueueState, job_id: str) -> TranslationJob:
    selected = next((item for item in state.jobs if item.job_id == job_id), None)
    if selected is None:
        raise TranslationRepairError(f"translation job not found: {job_id}")
    return selected


@dataclass(frozen=True, slots=True)
class RepairRequest:
    """One derived repair request, plus a hash of every input the derivation consumed.

    ``system`` and ``user`` are the only fields carrying prose, and they exist to be sent, never
    to be stored. Everything else is a digest, which is what lets provider recovery prove it
    rebuilt the same request without a durable record ever holding book content.
    """

    rejection: EffectiveRejection
    repaired_from_attempt: int | None
    system: str
    user: str
    system_sha256: str
    user_sha256: str
    source_sha256: str
    context_sha256: str
    rejected_translation_sha256: str
    prompt_sha256: str
    style_guide_sha256: str
    terminology_sha256: str | None
    prompt_version: str = REPAIR_PROMPT_VERSION
    repaired_from_carry_forward: int | None = None

    @property
    def identity_sha256(self) -> str:
        """Digest the whole derivation, so one comparison answers "is this the same repair?".

        The individual message hashes already prove the bytes; this additionally binds them to
        the lineage and the derivation version, so a repair that happens to render identically
        from a *different* rejection is not mistaken for the same request.
        """
        lineage: dict[str, object] = (
            {}
            if self.repaired_from_carry_forward is None
            else {"repaired_from_carry_forward": self.repaired_from_carry_forward}
        )
        return sha256_bytes(
            canonical_json(
                {
                    **lineage,
                    "repair_prompt_version": self.prompt_version,
                    "repaired_from_attempt": self.repaired_from_attempt,
                    "rejection_evidence_sha256": self.rejection.evidence_sha256,
                    "rejected_attempt_sha256": self.rejection.attempt_sha256,
                    "source_sha256": self.source_sha256,
                    "context_sha256": self.context_sha256,
                    "rejected_translation_sha256": self.rejected_translation_sha256,
                    "prompt_sha256": self.prompt_sha256,
                    "style_guide_sha256": self.style_guide_sha256,
                    "terminology_sha256": self.terminology_sha256,
                    "system_sha256": self.system_sha256,
                    "user_sha256": self.user_sha256,
                }
            )
        )


def build_repair_request(
    run_path: Path,
    job: TranslationJob,
    *,
    project_root: Path,
    allow_failed_repair: bool = False,
) -> RepairRequest:
    """Derive one repair request from immutable evidence and frozen resources.

    Every input is hash-verified before it is used, so a tampered rejection, a tampered draft, or
    a frozen resource that no longer matches the manifest stops the derivation rather than
    silently changing what would be sent. The job's attempt budget is deliberately *not* checked
    here: whether another attempt may be spent is a policy question, and the callers that spend
    one ask it themselves.
    """
    rejection = load_effective_rejection(
        job, project_root=project_root, allow_failed_repair=allow_failed_repair
    )
    evidence = rejection.evidence
    attempt = rejection.attempt
    if attempt.translation_file is None:
        raise TranslationRepairError("rejection evidence does not identify a successful attempt")
    source_bytes = (project_root / evidence.source_file).read_bytes()
    rejected_bytes = (project_root / evidence.translation_file).read_bytes()
    if sha256_bytes(source_bytes) != evidence.source_sha256:
        raise TranslationRepairError("rejected attempt source hash mismatch")
    if sha256_bytes(rejected_bytes) != evidence.translation_sha256:
        raise TranslationRepairError("rejected translation hash mismatch")
    source = source_bytes.decode("utf-8")
    rejected = rejected_bytes.decode("utf-8")
    snapshot = project_root / _load_queue(run_path, project_root).run.snapshot_path
    manifest = load_yaml(snapshot / "manifest.yaml", TranslationManifest)
    prompt_bytes = (project_root / manifest.prompt.file).read_bytes()
    style_bytes = (project_root / manifest.style_guide.file).read_bytes()
    if sha256_bytes(prompt_bytes) != manifest.prompt.sha256:
        raise TranslationRepairError("frozen prompt resource hash mismatch")
    if sha256_bytes(style_bytes) != manifest.style_guide.sha256:
        raise TranslationRepairError("frozen style resource hash mismatch")
    prompt_text = prompt_bytes.decode("utf-8")
    style_text = style_bytes.decode("utf-8")
    terminology_file = (
        (manifest.terminology.frozen_file or manifest.terminology.file)
        if manifest.terminology is not None
        else None
    )
    registry = None
    if terminology_file is not None and manifest.terminology is not None:
        terminology_path = project_root / terminology_file
        if sha256_bytes(terminology_path.read_bytes()) != manifest.terminology.sha256:
            raise TranslationRepairError("frozen terminology resource hash mismatch")
        registry = load_yaml(terminology_path, TerminologyRegistry)
    decisions: list[dict[str, object]] = []
    if registry is not None:
        candidates = {item.candidate_id: item for item in registry.candidates}
        requested = {item.source_term for item in evidence.terminology_report.findings}
        for decision in registry.decisions:
            candidate = candidates[decision.candidate_id]
            if candidate.source_term in requested and decision.required:
                decisions.append(
                    {
                        "source_term": candidate.source_term,
                        "required_target_term": decision.target_term,
                        # Absent unless the decision authorizes forms, so every repair derived
                        # before authorized forms existed re-derives byte for byte.
                        **authorized_forms_payload(decision.allowed_target_forms),
                    }
                )
    terminology_findings = []
    for finding in evidence.terminology_report.findings:
        governing = next(
            (item for item in decisions if item["source_term"] == finding.source_term),
            None,
        )
        entry: dict[str, object] = {
            "code": finding.code,
            "source_term": finding.source_term,
            "required_target_term": (
                governing["required_target_term"] if governing is not None else None
            ),
        }
        if governing is not None:
            entry.update(
                {
                    key: governing[key]
                    for key in ("allowed_target_forms", "form_rule")
                    if key in governing
                }
            )
        terminology_findings.append(entry)
    findings = {
        "terminology": terminology_findings,
        "structural": [
            {
                "code": item.code,
                "source_paragraph_count": evidence.structural_report.source_paragraph_count,
                "translation_paragraph_count": (
                    evidence.structural_report.translation_paragraph_count
                ),
            }
            for item in evidence.structural_report.findings
        ],
        "canonical_terminology": decisions,
    }
    system = (
        "You repair a rejected literary translation. Preserve all correct translation content and "
        "make only the changes necessary to resolve the supplied QA findings. Do not summarize, "
        "omit, reorder, or broadly rewrite. Preserve source paragraph boundaries exactly. Use each "
        "required canonical term exactly, allowing only target-language morphology supported by "
        "the "
        "project constraints. Return translation prose only, with no commentary or markup.\n\n"
        "Existing prompt constraints "
        f"(length={len(prompt_text.encode('utf-8'))} bytes):\n{prompt_text}\n\n"
        f"Style constraints (length={len(style_text.encode('utf-8'))} bytes):\n{style_text}\n"
    )
    payload = json.dumps(findings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    prompt_version = REPAIR_PROMPT_VERSION
    if uses_paragraph_markers(attempt.output_contract):
        prompt_version = MARKED_REPAIR_PROMPT_VERSION
        source_blocks = source_paragraphs(source)
        rejected_blocks = source_paragraphs(rejected)
        if not source_blocks or len(source_blocks) != len(rejected_blocks):
            # Under the marked contract a provider-successful draft always carries exactly one
            # block per source paragraph, because the executor refuses anything else before a
            # translation artifact exists.  A draft that does not is corrupt, and re-attaching
            # markers to it would be inventing the identities this contract exists to prove.
            raise TranslationRepairError(
                "marked-contract draft does not carry one block per source paragraph"
            )
        marked_source = mark_paragraphs(source)
        marked_rejected = mark_paragraphs(rejected)
        if strip_markers(marked_source) != source or strip_markers(marked_rejected) != rejected:
            raise TranslationRepairError("marker re-attachment did not conserve the repair inputs")
        context_text = _frozen_context(job, project_root=project_root)
        system = system.replace(
            "Return translation prose only, with no commentary or markup.\n\n",
            "Return the numbered paragraph document described below, with no commentary.\n\n"
            + PARAGRAPH_MARKER_RULES
            + "\n",
        )
        user = (
            f"REPAIR_FINDINGS_JSON length={len(payload.encode('utf-8'))}\n{payload}\n"
            f"PREVIOUS_CONTEXT length={len(context_text.encode('utf-8'))}"
            " comprehension_only=true do_not_translate=true\n"
            f"{context_text}\n"
            f"MARKED_SOURCE_CHUNK length={len(marked_source.encode('utf-8'))}\n{marked_source}\n"
            "MARKED_REJECTED_TRANSLATION length="
            f"{len(marked_rejected.encode('utf-8'))}\n{marked_rejected}"
        )
    else:
        user = (
            f"REPAIR_FINDINGS_JSON length={len(payload.encode('utf-8'))}\n{payload}\n"
            f"SOURCE_CHUNK length={len(source.encode('utf-8'))}\n{source}\n"
            f"REJECTED_TRANSLATION length={len(rejected.encode('utf-8'))}\n{rejected}"
        )
    return RepairRequest(
        rejection=rejection,
        # A carried-forward draft has no attempt of this job behind it; its lineage is the
        # carry-forward number instead, and the two are never both set.
        repaired_from_attempt=(
            evidence.attempt_number if rejection.carry_forward_number is None else None
        ),
        repaired_from_carry_forward=rejection.carry_forward_number,
        system=system,
        user=user,
        system_sha256=sha256_bytes(system.encode("utf-8")),
        user_sha256=sha256_bytes(user.encode("utf-8")),
        source_sha256=evidence.source_sha256,
        context_sha256=job.context_sha256,
        rejected_translation_sha256=evidence.translation_sha256,
        prompt_sha256=manifest.prompt.sha256,
        style_guide_sha256=manifest.style_guide.sha256,
        terminology_sha256=(
            manifest.terminology.sha256 if manifest.terminology is not None else None
        ),
        prompt_version=prompt_version,
    )


def _repair_inputs(run_path: Path, job: TranslationJob, project_root: Path) -> RepairRequest:
    """Derive the repair request for a job that is about to spend an attempt slot on it."""
    if attempt_budget_exhausted(job):
        raise TranslationRepairError("translation job has exhausted its maximum attempts")
    return build_repair_request(run_path, job, project_root=project_root)


def _claim_repair(
    run_path: Path,
    job_id: str,
    *,
    project_root: Path,
    worker_id: str,
    lease_seconds: int,
    initiator: TranslationRepairInitiator,
    now: datetime,
) -> ClaimResult:
    with queue_lock(run_path / ".queue.lock", timeout_seconds=LOCK_TIMEOUT_SECONDS):
        state = _load_queue(run_path, project_root)
        selected = _job(state, job_id)
        _repair_inputs(run_path, selected, project_root)
        token = secrets.token_urlsafe(32)
        claimed = selected.model_copy(
            update={
                "status": TranslationJobStatus.CLAIMED,
                "attempt_count": selected.attempt_count + 1,
                "claimed_by": worker_id,
                "claim_token": token,
                "claimed_at": now,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "completed_at": None,
                "updated_at": now,
                # The event type distinguishes an unattended policy decision from an operator
                # one at a glance; the durable answer lives in the auto-repair decision record,
                # because the event ring buffer rotates.
                "recent_events": _append_event(
                    selected,
                    _event(
                        "auto_repair_claimed"
                        if initiator is TranslationRepairInitiator.AUTOMATIC
                        else "repair_claimed",
                        now,
                        worker_id=worker_id,
                        reason=f"repair initiated by {initiator.value} decision",
                    ),
                ),
            }
        )
        jobs = tuple(claimed if item.job_id == job_id else item for item in state.jobs)
        _persist_job_and_run(
            state, jobs, claimed, now, force_run_status=TranslationRunStatus.ACTIVE
        )
        return ClaimResult(claimed, token)


def repair_translation_job(
    run_id_or_path: str | Path,
    job_id: str,
    *,
    project_root: Path,
    provider: ProviderName,
    model: str | None,
    worker_id: str,
    lease_seconds: int,
    timeout_seconds: int,
    max_output_tokens: int,
    reasoning_effort: str | None = None,
    store_raw_response: bool = True,
    dry_run: bool = False,
    initiator: TranslationRepairInitiator = TranslationRepairInitiator.OPERATOR,
    provider_adapter: TranslationProvider | None = None,
    now: Callable[[], datetime] | None = None,
) -> RepairResult:
    project_root = project_root.resolve()
    run_path = _resolve_run_path(run_id_or_path, project_root)
    timestamp = (now or (lambda: datetime.now(UTC)))()
    state = _load_queue(run_path, project_root)
    selected = _job(state, job_id)
    request = _repair_inputs(run_path, selected, project_root)
    if dry_run:
        return RepairResult(
            selected.run_id,
            job_id,
            request.repaired_from_attempt,
            selected.attempt_count + 1,
            provider,
            False,
            None,
            None,
            True,
            initiator,
            request.repaired_from_carry_forward,
        )
    # The executor re-checks this when it plans, but a repair claim spends an attempt slot first;
    # a provider/model the run's book does not allow must be refused before that happens.
    ensure_provider_model_allowed(
        load_book_config(ProjectPaths(project_root, state.book).book_config), provider, model
    )
    claim = _claim_repair(
        run_path,
        job_id,
        project_root=project_root,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        initiator=initiator,
        now=timestamp,
    )
    plan = plan_execution(
        run_path,
        job_id,
        project_root=project_root,
        worker_id=worker_id,
        claim_token=claim.claim_token,
        provider=provider,
        model=model,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
        store_raw_response=store_raw_response,
        execution_kind=TranslationExecutionKind.REPAIR,
        repaired_from_attempt=request.repaired_from_attempt,
        repaired_from_carry_forward=request.repaired_from_carry_forward,
        system_message=request.system,
        user_message=request.user,
        now=now,
    )
    execution: ExecutionResult = execute_translation_job(
        plan, worker_id=worker_id, claim_token=claim.claim_token, provider=provider_adapter, now=now
    )
    acceptance = None
    if execution.attempt_status is TranslationAttemptStatus.SUCCEEDED:
        # Imported here on purpose: the repair mechanism is a lower layer than the worker that
        # orchestrates it, and a module-level edge back to the worker would make the automatic
        # repair orchestration above it unimportable.
        from book_translator.translation_worker import accept_provider_success

        acceptance = accept_provider_success(run_path, job_id, project_root=project_root, now=now)
    return RepairResult(
        selected.run_id,
        job_id,
        request.repaired_from_attempt,
        claim.job.attempt_count,
        provider,
        execution.provider_contacted,
        execution.attempt_status,
        acceptance,
        False,
        initiator,
        request.repaired_from_carry_forward,
    )
