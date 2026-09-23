"""Immutable terminology discovery and proposal run orchestration."""

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.hashing import sha256_file
from book_translator.models import (
    SCHEMA_VERSION,
    ChunkMetadata,
    SourceAnomaly,
    TerminologyAnalysisRun,
    TerminologyAnalysisStage,
    TerminologyAnalysisStatus,
    TerminologyAnalysisValidationReport,
    TerminologyCandidate,
    TerminologyCandidateOutput,
    TerminologyDiscoveryEvidence,
    TerminologyProposal,
    TerminologyProposalResponse,
    TerminologyReview,
    TerminologyReviewResponse,
    TerminologySourceReference,
    TranslationManifest,
)
from book_translator.source_anomalies import load_source_anomalies
from book_translator.source_quality_audit import audit_source_quality
from book_translator.terminology.analysis import (
    DISCOVERY_SYSTEM_MESSAGE,
    PROPOSAL_SYSTEM_MESSAGE,
    TerminologyAnalysisError,
    canonical_json_bytes,
    discovery_prompt,
    evidence_for_selection,
    parse_discovery_response,
    parse_proposal_response,
    proposal_identity,
    proposal_prompt,
    reconcile_candidates,
    source_evidence_count,
    validate_proposals,
)
from book_translator.terminology.canonical import (
    CanonicalTerm,
    canonical_constraints,
    canonical_view,
    canonical_view_identity,
    exact_canonical,
    proposal_conflicts,
)
from book_translator.terminology.providers import (
    TerminologyAnalysisProvider,
    TerminologyDiscoveryRequest,
    TerminologyProposalRequest,
    TerminologyProviderError,
    TerminologyReviewProvider,
    TerminologyReviewRequest,
)
from book_translator.terminology.registry import load_registry, registry_identity
from book_translator.terminology.review import (
    REVIEW_SYSTEM_MESSAGE,
    parse_review_response,
    review_prompt,
    validate_reviews,
)
from book_translator.terminology.target_style import target_style_adapter
from book_translator.translation_snapshot import validate_snapshot

TERMINOLOGY_ANALYSIS_VERSION = "1.5.1"
TERMINOLOGY_REVIEW_ENGINE_VERSION = "2.0.1"
REVIEW_SHARD_MAX_CANDIDATES = 60
REVIEW_SHARD_TOKEN_BUDGET = 12_000
REVIEW_SHARD_MAX_ATTEMPTS = 3
RUN_FILE = "run.yaml"
OUTPUT_FILE = "output.yaml"
REPORT_FILE = "validation-report.yaml"
REQUEST_FILE = "request.json"


@dataclass(frozen=True, slots=True)
class TerminologyRunPlan:
    project_root: Path
    output_root: Path
    run_id: str
    stage: TerminologyAnalysisStage
    snapshot_id: str
    manifest_sha256: str
    source_language: str
    target_language: str
    sources: tuple[TerminologySourceReference, ...]
    provider: str
    provider_adapter_version: str
    model: str | None
    request_sha256: str
    request_payload: dict[str, Any]
    source_lineage_kind: Literal["translation_snapshot", "translation_ready_corpus"] = (
        "translation_snapshot"
    )
    source_audit_id: str | None = None
    source_anomalies_sha256: str | None = None
    discovery_run_id: str | None = None
    registry_sha256: str | None = None
    candidates: tuple[TerminologyCandidate, ...] = ()
    canonical_terms: tuple[CanonicalTerm, ...] = ()
    reused_proposals: tuple[TerminologyProposal, ...] = ()
    proposals: tuple[TerminologyProposal, ...] = ()
    reviews: tuple[TerminologyReview, ...] = ()
    glossary_sha256: str | None = None
    proposal_run_id: str | None = None
    proposal_output_sha256: str | None = None
    canonical_view_sha256: str | None = None
    style_resource_sha256: str | None = None
    target_style_version: str | None = None
    style_guide: str = ""
    retry_of: str | None = None
    action: str = "create"

    @property
    def output_directory(self) -> Path:
        return self.output_root / self.run_id


class TerminologyWorkflowError(ValueError):
    """Raised when a terminology analysis run cannot be trusted or published."""


def _serialize(model: Any) -> str:
    rendered = yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise TerminologyWorkflowError("could not serialize terminology analysis artifact")
    return rendered


def _load[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise TerminologyWorkflowError(f"invalid terminology artifact {path}: {exc}") from exc


def _run_id(payload: dict[str, Any]) -> tuple[str, str]:
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return f"ta-{digest[:24]}", digest


def _snapshot(
    snapshot: str | Path, project_root: Path
) -> tuple[Path, TranslationManifest, str, str]:
    path = Path(snapshot)
    if not path.is_absolute():
        direct = project_root / path
        path = (
            direct if direct.is_dir() else project_root / "workspace/snapshots/translation" / path
        )
    result = validate_snapshot(path, project_root=project_root)
    if not result.report.valid or result.manifest is None or result.snapshot is None:
        raise TerminologyWorkflowError("terminology analysis requires a valid frozen snapshot")
    return path, result.manifest, result.snapshot.manifest_sha256, result.snapshot.snapshot_id


def _selected_sources(
    manifest: TranslationManifest,
    selectors: Iterable[str] | None,
    chunk_selectors: Iterable[str] | None,
    project_root: Path,
) -> tuple[TerminologySourceReference, ...]:
    selected = {
        "000-prologue"
        if value.strip().lower() in {"prologue", "0", "000-prologue"}
        else f"{int(value):03d}"
        if value.isdigit()
        else value
        for value in (selectors or ())
    }
    known = {chapter.chapter_id for chapter in manifest.chapters}
    if selected - known:
        raise TerminologyWorkflowError("selected chapter is absent from snapshot")
    sources = []
    selected_chunks = set(chunk_selectors or ())
    known_chunks = {chunk.chunk_id for chapter in manifest.chapters for chunk in chapter.chunks}
    if selected_chunks - known_chunks:
        raise TerminologyWorkflowError("selected chunk is absent from snapshot")
    for chapter in manifest.chapters:
        if selected and chapter.chapter_id not in selected:
            continue
        for chunk in chapter.chunks:
            if selected_chunks and chunk.chunk_id not in selected_chunks:
                continue
            source = project_root / chunk.source_file
            context = project_root / chunk.context_file
            if (
                sha256_file(source) != chunk.source_sha256
                or sha256_file(context) != chunk.context_sha256
            ):
                raise TerminologyWorkflowError(
                    "selected frozen source lineage is no longer readable"
                )
            sources.append(
                TerminologySourceReference(
                    chapter_id=chapter.chapter_id,
                    chunk_id=chunk.chunk_id,
                    source_file=chunk.source_file.as_posix(),
                    source_sha256=chunk.source_sha256,
                    context_file=chunk.context_file.as_posix(),
                    context_sha256=chunk.context_sha256,
                )
            )
    if not sources:
        raise TerminologyWorkflowError("terminology selection contains no chunks")
    return tuple(sources)


def plan_discovery(
    snapshot: str | Path,
    *,
    project_root: Path,
    selectors: Iterable[str] | None = None,
    chunk_selectors: Iterable[str] | None = None,
    provider: str = "fake",
    provider_adapter_version: str = "1.0.0",
    model: str | None = None,
    output_root: Path | None = None,
    retry_of: str | None = None,
) -> TerminologyRunPlan:
    project_root = project_root.resolve()
    _, manifest, manifest_hash, snapshot_id = _snapshot(snapshot, project_root)
    sources = _selected_sources(manifest, selectors, chunk_selectors, project_root)
    payload = {
        "stage": "discovery",
        "engine_version": TERMINOLOGY_ANALYSIS_VERSION,
        "snapshot_id": snapshot_id,
        "manifest_sha256": manifest_hash,
        "sources": [item.model_dump(mode="json") for item in sources],
        "provider": provider,
        "provider_adapter_version": provider_adapter_version,
        "model": model,
        "prompt_sha256": hashlib.sha256(DISCOVERY_SYSTEM_MESSAGE.encode()).hexdigest(),
        "retry_of": retry_of,
    }
    run_id, request_hash = _run_id(payload)
    root = (output_root or project_root / "workspace/terminology-runs").resolve()
    if retry_of is not None and not (
        (root / retry_of).is_dir() or (root / f".{retry_of}.requesting.json").is_file()
    ):
        raise TerminologyWorkflowError("--retry-of does not reference retained request evidence")
    action = "skip" if (root / run_id).is_dir() else "create"
    return TerminologyRunPlan(
        project_root,
        root,
        run_id,
        TerminologyAnalysisStage.DISCOVERY,
        str(payload["snapshot_id"]),
        manifest_hash,
        manifest.book.source_language,
        manifest.book.target_language,
        sources,
        provider,
        provider_adapter_version,
        model,
        request_hash,
        payload,
        retry_of=retry_of,
        action=action,
    )


def plan_ready_corpus_discovery(
    *,
    project_root: Path,
    provider: str = "fake",
    provider_adapter_version: str = "1.0.0",
    model: str | None = None,
    output_root: Path | None = None,
    retry_of: str | None = None,
) -> TerminologyRunPlan:
    """Plan discovery over the current audited translation-ready corpus, without a snapshot."""
    project_root = project_root.resolve()
    audit = audit_source_quality(project_root).report
    if audit.blocking_findings or audit.chapters_translation_ready != audit.chapters_scanned:
        raise TerminologyWorkflowError(
            "current-corpus terminology discovery requires every audited chapter "
            "to be translation-ready"
        )
    sources: list[TerminologySourceReference] = []
    for chapter_dir in sorted((project_root / "workspace/chapters").iterdir()):
        if not chapter_dir.is_dir():
            continue
        for metadata_file in sorted((chapter_dir / "chunks").glob("*/metadata.yaml")):
            metadata = _load(metadata_file, ChunkMetadata)
            source = project_root / metadata.source_file
            context = project_root / metadata.context_file
            context_sha256 = (
                metadata.context_sha256 or hashlib.sha256(context.read_bytes()).hexdigest()
            )
            if (
                sha256_file(source) != metadata.source_sha256
                or sha256_file(context) != context_sha256
            ):
                raise TerminologyWorkflowError("current translation-ready chunk lineage is stale")
            sources.append(
                TerminologySourceReference(
                    chapter_id=metadata.chapter_id,
                    chunk_id=metadata.chunk_id,
                    source_file=metadata.source_file.as_posix(),
                    source_sha256=metadata.source_sha256,
                    context_file=metadata.context_file.as_posix(),
                    context_sha256=context_sha256,
                    source_word_count=metadata.source_word_count,
                )
            )
    anomaly_path = project_root / "config/source-anomalies.yaml"
    anomaly_hash = sha256_file(anomaly_path) if anomaly_path.is_file() else None
    inventory = {
        "audit_id": audit.audit_id,
        "sources": [item.model_dump(mode="json") for item in sources],
        "source_anomalies_sha256": anomaly_hash,
    }
    inventory_hash = hashlib.sha256(canonical_json_bytes(inventory)).hexdigest()
    payload = {
        "stage": "discovery",
        "engine_version": TERMINOLOGY_ANALYSIS_VERSION,
        "source_lineage_kind": "translation_ready_corpus",
        "source_audit_id": audit.audit_id,
        "inventory_sha256": inventory_hash,
        "sources": inventory["sources"],
        "source_anomalies_sha256": anomaly_hash,
        "provider": provider,
        "provider_adapter_version": provider_adapter_version,
        "model": model,
        "prompt_sha256": hashlib.sha256(DISCOVERY_SYSTEM_MESSAGE.encode()).hexdigest(),
        "retry_of": retry_of,
    }
    run_id, request_hash = _run_id(payload)
    root = (output_root or project_root / "workspace/terminology-runs").resolve()
    if retry_of is not None and not (
        (root / retry_of).is_dir() or (root / f".{retry_of}.requesting.json").is_file()
    ):
        raise TerminologyWorkflowError("--retry-of does not reference retained request evidence")
    config = yaml.safe_load((project_root / "config/book.yaml").read_text(encoding="utf-8"))
    return TerminologyRunPlan(
        project_root=project_root,
        output_root=root,
        run_id=run_id,
        stage=TerminologyAnalysisStage.DISCOVERY,
        snapshot_id=f"terminology-corpus-{inventory_hash[:24]}",
        manifest_sha256=inventory_hash,
        source_language=str(config["source_language"]),
        target_language=str(config["target_language"]),
        sources=tuple(sources),
        provider=provider,
        provider_adapter_version=provider_adapter_version,
        model=model,
        request_sha256=request_hash,
        request_payload=payload,
        source_lineage_kind="translation_ready_corpus",
        source_audit_id=audit.audit_id,
        source_anomalies_sha256=anomaly_hash,
        retry_of=retry_of,
        action="skip" if (root / run_id).is_dir() else "create",
    )


def _publish(
    plan: TerminologyRunPlan,
    output: Any,
    report: TerminologyAnalysisValidationReport,
    timestamp: datetime,
    *,
    contacted: bool = True,
) -> TerminologyAnalysisRun:
    output_text = _serialize(output)
    output_hash = hashlib.sha256(output_text.encode("utf-8")).hexdigest()
    run = TerminologyAnalysisRun(
        schema_version=SCHEMA_VERSION,
        run_id=plan.run_id,
        stage=plan.stage,
        engine_version=TERMINOLOGY_ANALYSIS_VERSION,
        snapshot_id=plan.snapshot_id,
        snapshot_manifest_sha256=plan.manifest_sha256,
        source_lineage_kind=plan.source_lineage_kind,
        source_audit_id=plan.source_audit_id,
        source_anomalies_sha256=plan.source_anomalies_sha256,
        source_language=plan.source_language,
        target_language=plan.target_language,
        sources=list(plan.sources),
        discovery_run_id=plan.discovery_run_id,
        proposal_run_id=plan.proposal_run_id,
        proposal_output_sha256=plan.proposal_output_sha256,
        registry_sha256=plan.registry_sha256,
        canonical_view_sha256=plan.canonical_view_sha256,
        style_resource_sha256=plan.style_resource_sha256,
        target_style_version=plan.target_style_version,
        provider=plan.provider,
        provider_adapter_version=plan.provider_adapter_version,
        model=plan.model,
        request_sha256=plan.request_sha256,
        output_file=OUTPUT_FILE,
        output_sha256=output_hash,
        validation_file=REPORT_FILE,
        status=TerminologyAnalysisStatus.VALIDATED,
        contacted=contacted,
        retry_of=plan.retry_of,
        created_at=timestamp,
    )
    plan.output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{plan.run_id}.staging.", dir=plan.output_root))
    try:
        atomic_write_text(
            staging / REQUEST_FILE,
            json.dumps(plan.request_payload, sort_keys=True, indent=2) + "\n",
        )
        atomic_write_text(staging / OUTPUT_FILE, output_text)
        atomic_write_text(staging / REPORT_FILE, _serialize(report))
        atomic_write_text(staging / RUN_FILE, _serialize(run))
        if sha256_file(staging / OUTPUT_FILE) != output_hash:
            raise TerminologyWorkflowError("staged terminology output hash mismatch")
        os.replace(staging, plan.output_directory)
    finally:
        if staging.exists():
            import shutil

            shutil.rmtree(staging)
    return run


def _publish_failed(
    plan: TerminologyRunPlan,
    raw_output: Any,
    error: Exception,
    timestamp: datetime,
) -> TerminologyAnalysisRun:
    value = (
        raw_output.model_dump(mode="json", exclude_none=False)
        if hasattr(raw_output, "model_dump")
        else [
            item.model_dump(mode="json", exclude_none=False)
            if hasattr(item, "model_dump")
            else item
            for item in raw_output
        ]
        if isinstance(raw_output, list)
        else raw_output
    )
    output_text = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    output_hash = hashlib.sha256(output_text.encode("utf-8")).hexdigest()
    report = TerminologyAnalysisValidationReport(
        schema_version=SCHEMA_VERSION,
        run_id=plan.run_id,
        valid=False,
        stage=plan.stage,
        source_count=len(plan.sources),
        item_count=0,
        merged_duplicate_count=0,
        conflict_count=0,
        errors=[str(error)],
        created_at=timestamp,
    )
    run = TerminologyAnalysisRun(
        schema_version=SCHEMA_VERSION,
        run_id=plan.run_id,
        stage=plan.stage,
        engine_version=TERMINOLOGY_ANALYSIS_VERSION,
        snapshot_id=plan.snapshot_id,
        snapshot_manifest_sha256=plan.manifest_sha256,
        source_lineage_kind=plan.source_lineage_kind,
        source_audit_id=plan.source_audit_id,
        source_anomalies_sha256=plan.source_anomalies_sha256,
        source_language=plan.source_language,
        target_language=plan.target_language,
        sources=list(plan.sources),
        discovery_run_id=plan.discovery_run_id,
        proposal_run_id=plan.proposal_run_id,
        proposal_output_sha256=plan.proposal_output_sha256,
        registry_sha256=plan.registry_sha256,
        canonical_view_sha256=plan.canonical_view_sha256,
        style_resource_sha256=plan.style_resource_sha256,
        target_style_version=plan.target_style_version,
        provider=plan.provider,
        provider_adapter_version=plan.provider_adapter_version,
        model=plan.model,
        request_sha256=plan.request_sha256,
        output_file=OUTPUT_FILE,
        output_sha256=output_hash,
        validation_file=REPORT_FILE,
        status=TerminologyAnalysisStatus.FAILED,
        contacted=True,
        failure_code="provider_output_validation_failed",
        failure_message=str(error),
        retry_of=plan.retry_of,
        created_at=timestamp,
    )
    plan.output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{plan.run_id}.failed.", dir=plan.output_root))
    try:
        atomic_write_text(
            staging / REQUEST_FILE,
            json.dumps(plan.request_payload, sort_keys=True, indent=2) + "\n",
        )
        atomic_write_text(staging / OUTPUT_FILE, output_text)
        atomic_write_text(staging / REPORT_FILE, _serialize(report))
        atomic_write_text(staging / RUN_FILE, _serialize(run))
        os.replace(staging, plan.output_directory)
    finally:
        if staging.exists():
            import shutil

            shutil.rmtree(staging)
    return run


def execute_discovery(
    plan: TerminologyRunPlan,
    provider: TerminologyAnalysisProvider,
    *,
    now: Callable[[], datetime] | None = None,
) -> TerminologyAnalysisRun:
    if plan.action == "skip":
        return _load(plan.output_directory / RUN_FILE, TerminologyAnalysisRun)
    if provider.provider_name != plan.provider:
        raise TerminologyWorkflowError("terminology provider does not match planned provider")
    if provider.adapter_version != plan.provider_adapter_version:
        raise TerminologyWorkflowError(
            "terminology provider version does not match planned version"
        )
    reservation = plan.output_root / f".{plan.run_id}.requesting.json"
    if reservation.exists():
        raise TerminologyWorkflowError(
            "ambiguous prior terminology request blocks automatic replay"
        )
    plan.output_root.mkdir(parents=True, exist_ok=True)
    source_values = [
        (item, (plan.project_root / item.source_file).read_bytes()) for item in plan.sources
    ]
    anomalies: tuple[SourceAnomaly, ...] = ()
    anomaly_path = plan.project_root / "config/source-anomalies.yaml"
    if plan.source_anomalies_sha256 is not None:
        if sha256_file(anomaly_path) != plan.source_anomalies_sha256:
            raise TerminologyWorkflowError(
                "source anomaly registry changed after discovery planning"
            )
        anomalies = tuple(load_source_anomalies(anomaly_path).anomalies)
    all_evidence: list[TerminologyDiscoveryEvidence] = []
    rejected_evidence_count = 0
    raw_responses: list[Any] = []
    batches: list[list[tuple[TerminologySourceReference, bytes]]] = []
    if plan.source_lineage_kind == "translation_snapshot":
        batches.append(source_values)
    else:
        for value in source_values:
            words = value[0].source_word_count or 0
            current_words = (
                sum(item.source_word_count or 0 for item, _ in batches[-1]) if batches else 0
            )
            if not batches or current_words + words > 18_000:
                batches.append([])
            batches[-1].append(value)
    for index, batch in enumerate(batches):
        filtered_values: list[tuple[TerminologySourceReference, str]] = []
        for item, source in batch:
            text = source.decode("utf-8")
            for anomaly in anomalies:
                if anomaly.chapter_id == item.chapter_id and anomaly.original_text in text:
                    text = text.replace(anomaly.original_text, " ")
            filtered_values.append((item, text))
        combined_text = "\n".join(text for _, text in filtered_values)
        atomic_write_text(
            reservation,
            json.dumps(
                {
                    "run_id": plan.run_id,
                    "request_sha256": plan.request_sha256,
                    "batch_index": index,
                    "batch_count": len(batches),
                }
            )
            + "\n",
            overwrite=reservation.exists(),
        )
        request = TerminologyDiscoveryRequest(
            request_id=f"{plan.request_sha256}:{index:04d}",
            system_message=DISCOVERY_SYSTEM_MESSAGE,
            user_message="\n".join(
                discovery_prompt(
                    text.encode("utf-8"), chapter_id=item.chapter_id, chunk_id=item.chunk_id
                )
                for item, text in filtered_values
            ),
            source_language=plan.source_language,
            target_language=plan.target_language,
            chapter_id="selection",
            chunk_id="selection",
        )
        raw_response: Any = None
        try:
            raw_response = provider.discover(request)
        except TerminologyProviderError as exc:
            if not exc.ambiguous:
                reservation.unlink(missing_ok=True)
            raise
        raw_responses.append(raw_response)
        try:
            response = parse_discovery_response(raw_response)
            if plan.source_lineage_kind == "translation_ready_corpus":
                for discovered in response.candidates:
                    if source_evidence_count(discovered.source_term, combined_text) < 1:
                        rejected_evidence_count += 1
                        continue
                    supported_aliases = [
                        alias
                        for alias in discovered.aliases
                        if source_evidence_count(alias, combined_text) > 0
                    ]
                    rejected_evidence_count += len(discovered.aliases) - len(supported_aliases)
                    filtered = response.model_copy(
                        update={
                            "candidates": [
                                discovered.model_copy(update={"aliases": supported_aliases})
                            ]
                        }
                    )
                    all_evidence.extend(
                        evidence_for_selection(
                            filtered,
                            tuple(
                                (item.chapter_id, item.chunk_id, text)
                                for item, text in filtered_values
                            ),
                        )
                    )
            else:
                all_evidence.extend(
                    evidence_for_selection(
                        response,
                        tuple(
                            (item.chapter_id, item.chunk_id, text) for item, text in filtered_values
                        ),
                    )
                )
        except (TerminologyAnalysisError, UnicodeError) as exc:
            failed_output: Any = (
                raw_responses[0] if len(raw_responses) == 1 else {"batches": raw_responses}
            )
            _publish_failed(plan, failed_output, exc, (now or (lambda: datetime.now(UTC)))())
            raise
    try:
        evidence = tuple(all_evidence)
        candidates = reconcile_candidates(evidence, plan.source_language)
    except (TerminologyAnalysisError, UnicodeError) as exc:
        _publish_failed(plan, raw_response, exc, (now or (lambda: datetime.now(UTC)))())
        raise
    timestamp = (now or (lambda: datetime.now(UTC)))()
    candidate_output = TerminologyCandidateOutput(
        schema_version=SCHEMA_VERSION, candidates=list(candidates)
    )
    duplicates = len(evidence) - len(candidates)
    report = TerminologyAnalysisValidationReport(
        schema_version=SCHEMA_VERSION,
        run_id=plan.run_id,
        valid=True,
        stage=plan.stage,
        source_count=len(plan.sources),
        chapter_count=len({item.chapter_id for item in plan.sources}),
        source_word_count=sum(item.source_word_count or 0 for item in plan.sources),
        raw_occurrence_count=sum(
            occurrence.count for item in candidates for occurrence in item.occurrences
        ),
        provider_call_count=len(batches),
        item_count=len(candidates),
        merged_duplicate_count=max(0, duplicates),
        conflict_count=sum(bool(item.classification_conflicts) for item in candidates),
        warnings=(
            [f"discarded_unsupported_evidence:{rejected_evidence_count}"]
            if rejected_evidence_count
            else []
        ),
        created_at=timestamp,
    )
    run = _publish(plan, candidate_output, report, timestamp)
    reservation.unlink(missing_ok=True)
    return run


def load_discovery_output(
    run_path: Path,
) -> tuple[TerminologyAnalysisRun, tuple[TerminologyCandidate, ...]]:
    run = _load(run_path / RUN_FILE, TerminologyAnalysisRun)
    if (
        run.stage is not TerminologyAnalysisStage.DISCOVERY
        or sha256_file(run_path / OUTPUT_FILE) == ""
    ):
        raise TerminologyWorkflowError("proposal input is not a discovery run")
    output = _load(run_path / OUTPUT_FILE, TerminologyCandidateOutput)
    if sha256_file(run_path / OUTPUT_FILE) != run.output_sha256:
        raise TerminologyWorkflowError("discovery output hash mismatch")
    return run, tuple(output.candidates)


def plan_proposals(
    discovery_run: str | Path,
    *,
    project_root: Path,
    registry_path: Path = Path("config/terminology.yaml"),
    glossary_path: Path = Path("config/glossary.yaml"),
    style_path: Path = Path("config/style-guide.md"),
    provider: str = "fake",
    provider_adapter_version: str = "1.0.0",
    model: str | None = None,
    output_root: Path | None = None,
    retry_of: str | None = None,
) -> TerminologyRunPlan:
    project_root = project_root.resolve()
    run_path = Path(discovery_run)
    if not run_path.is_absolute():
        direct = project_root / run_path
        run_path = (
            direct if direct.is_dir() else project_root / "workspace/terminology-runs" / run_path
        )
    discovery, candidates = load_discovery_output(run_path)
    registry = load_registry(project_root / registry_path)
    resolved_glossary = project_root / glossary_path
    resolved_style = project_root / style_path
    glossary = yaml.safe_load(resolved_glossary.read_text(encoding="utf-8"))
    view = canonical_view(registry, glossary)
    reused: list[TerminologyProposal] = []
    undecided: list[TerminologyCandidate] = []
    for item in candidates:
        existing = exact_canonical(item, view)
        if existing is None:
            undecided.append(item)
            continue
        reused.append(
            TerminologyProposal(
                proposal_id=proposal_identity(
                    item.candidate_id, existing.target_term, registry.target_language
                ),
                candidate_id=item.candidate_id,
                source_term=item.source_term,
                proposed_target=existing.target_term,
                confidence=1.0,
                canonicalization_confidence=1.0,
                classification=item.classification,
                action="reuse_existing",
                canonical_source=("locked_registry" if existing.locked else existing.origin),
            )
        )
    undecided_tuple = tuple(undecided)
    payload = {
        "stage": "proposal",
        "engine_version": TERMINOLOGY_ANALYSIS_VERSION,
        "target_style_version": target_style_adapter(registry.target_language).version,
        "discovery_run_id": discovery.run_id,
        "discovery_output_sha256": discovery.output_sha256,
        "registry_sha256": registry_identity(registry),
        "glossary_sha256": sha256_file(resolved_glossary),
        "style_resource_sha256": sha256_file(resolved_style),
        "provider": provider,
        "provider_adapter_version": provider_adapter_version,
        "model": model,
        "candidate_ids": [item.candidate_id for item in undecided_tuple],
        "reused_candidate_ids": [item.candidate_id for item in reused],
        "prompt_sha256": hashlib.sha256(PROPOSAL_SYSTEM_MESSAGE.encode()).hexdigest(),
        "retry_of": retry_of,
    }
    run_id, request_hash = _run_id(payload)
    root = (output_root or project_root / "workspace/terminology-runs").resolve()
    if retry_of is not None and not (
        (root / retry_of).is_dir() or (root / f".{retry_of}.requesting.json").is_file()
    ):
        raise TerminologyWorkflowError("--retry-of does not reference retained request evidence")
    return TerminologyRunPlan(
        project_root,
        root,
        run_id,
        TerminologyAnalysisStage.PROPOSAL,
        discovery.snapshot_id,
        discovery.snapshot_manifest_sha256,
        discovery.source_language,
        discovery.target_language,
        tuple(discovery.sources),
        provider,
        provider_adapter_version,
        model,
        request_hash,
        payload,
        source_lineage_kind=discovery.source_lineage_kind,
        source_audit_id=discovery.source_audit_id,
        source_anomalies_sha256=discovery.source_anomalies_sha256,
        discovery_run_id=discovery.run_id,
        registry_sha256=registry_identity(registry),
        candidates=undecided_tuple,
        canonical_terms=view,
        reused_proposals=tuple(reused),
        glossary_sha256=sha256_file(resolved_glossary),
        style_resource_sha256=sha256_file(resolved_style),
        target_style_version=target_style_adapter(registry.target_language).version,
        retry_of=retry_of,
        action="skip" if (root / run_id).is_dir() else "create",
    )


def execute_proposals(
    plan: TerminologyRunPlan,
    provider: TerminologyAnalysisProvider,
    *,
    registry_path: Path = Path("config/terminology.yaml"),
    glossary_path: Path = Path("config/glossary.yaml"),
    style_path: Path = Path("config/style-guide.md"),
    now: Callable[[], datetime] | None = None,
) -> TerminologyAnalysisRun:
    if plan.action == "skip":
        return _load(plan.output_directory / RUN_FILE, TerminologyAnalysisRun)
    registry = load_registry(plan.project_root / registry_path)
    if (
        provider.provider_name != plan.provider
        or provider.adapter_version != plan.provider_adapter_version
    ):
        raise TerminologyWorkflowError(
            "terminology proposal provider does not match planned adapter"
        )
    if registry_identity(registry) != plan.registry_sha256:
        raise TerminologyWorkflowError("terminology registry changed after proposal planning")
    if sha256_file(plan.project_root / glossary_path) != plan.glossary_sha256:
        raise TerminologyWorkflowError("legacy glossary changed after proposal planning")
    if sha256_file(plan.project_root / style_path) != plan.style_resource_sha256:
        raise TerminologyWorkflowError("style resource changed after proposal planning")
    if not plan.candidates:
        timestamp = (now or (lambda: datetime.now(UTC)))()
        output = TerminologyProposalResponse(
            schema_version=SCHEMA_VERSION, proposals=list(plan.reused_proposals)
        )
        report = TerminologyAnalysisValidationReport(
            schema_version=SCHEMA_VERSION,
            run_id=plan.run_id,
            valid=True,
            stage=plan.stage,
            source_count=len(plan.sources),
            item_count=len(plan.reused_proposals),
            merged_duplicate_count=0,
            conflict_count=0,
            warnings=[],
            created_at=timestamp,
        )
        return _publish(plan, output, report, timestamp, contacted=False)
    reservation = plan.output_root / f".{plan.run_id}.requesting.json"
    if reservation.exists():
        raise TerminologyWorkflowError(
            "ambiguous prior terminology proposal blocks automatic replay"
        )
    plan.output_root.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        reservation,
        json.dumps({"run_id": plan.run_id, "request_sha256": plan.request_sha256}) + "\n",
    )
    proposal_batches = tuple(
        plan.candidates[index : index + 100] for index in range(0, len(plan.candidates), 100)
    )
    validated_proposals: list[TerminologyProposal] = []
    raw_responses: list[Any] = []
    for index, batch in enumerate(proposal_batches):
        relevant_constraints = tuple(
            term
            for candidate in batch
            for term in canonical_constraints(candidate, plan.canonical_terms)
        )
        canonical_payload = tuple(
            {
                "source_term": term.source_term,
                "target_term": term.target_term,
                "origin": term.origin,
                "locked": term.locked,
                "source_variants": list(term.source_variants),
                "competing_variants": list(term.competing_variants),
                "instruction": "preserve canonical lexical identity; natural morphology is allowed",
            }
            for term in sorted(set(relevant_constraints), key=lambda item: item.source_term)
        )
        atomic_write_text(
            reservation,
            json.dumps(
                {
                    "run_id": plan.run_id,
                    "request_sha256": plan.request_sha256,
                    "batch_index": index,
                    "batch_count": len(proposal_batches),
                }
            )
            + "\n",
            overwrite=reservation.exists(),
        )
        request = TerminologyProposalRequest(
            request_id=f"{plan.request_sha256}:{index:04d}",
            system_message=PROPOSAL_SYSTEM_MESSAGE,
            user_message=proposal_prompt(batch, registry, canonical_payload),
            source_language=plan.source_language,
            target_language=plan.target_language,
            candidates=batch,
            registry=registry,
        )
        try:
            raw_response = provider.propose(request)
        except TerminologyProviderError as exc:
            if not exc.ambiguous:
                reservation.unlink(missing_ok=True)
            raise
        raw_responses.append(raw_response)
        try:
            response = parse_proposal_response(raw_response)
            validated_proposals.extend(
                validate_proposals(
                    response,
                    batch,
                    plan.target_language,
                    canonical_terms=plan.canonical_terms,
                )
            )
        except TerminologyAnalysisError as exc:
            failed_output: Any = (
                raw_responses[0] if len(raw_responses) == 1 else {"batches": raw_responses}
            )
            _publish_failed(plan, failed_output, exc, (now or (lambda: datetime.now(UTC)))())
            raise
    proposals = tuple(validated_proposals)
    combined = tuple(
        sorted((*plan.reused_proposals, *proposals), key=lambda item: item.proposal_id)
    )
    output = TerminologyProposalResponse(schema_version=SCHEMA_VERSION, proposals=list(combined))
    timestamp = (now or (lambda: datetime.now(UTC)))()
    report = TerminologyAnalysisValidationReport(
        schema_version=SCHEMA_VERSION,
        run_id=plan.run_id,
        valid=True,
        stage=plan.stage,
        source_count=len(plan.sources),
        item_count=len(combined),
        merged_duplicate_count=0,
        conflict_count=sum(item.blocked for item in combined),
        created_at=timestamp,
    )
    run = _publish(plan, output, report, timestamp)
    reservation.unlink(missing_ok=True)
    return run


def load_proposal_output(
    run_path: Path,
) -> tuple[TerminologyAnalysisRun, tuple[TerminologyProposal, ...]]:
    run = _load(run_path / RUN_FILE, TerminologyAnalysisRun)
    response = _load(run_path / OUTPUT_FILE, TerminologyProposalResponse)
    if run.stage is not TerminologyAnalysisStage.PROPOSAL:
        raise TerminologyWorkflowError("run is not a terminology proposal run")
    return run, tuple(response.proposals)


def _evidence_excerpt(term: str, text: str, radius: int = 240) -> str:
    index = text.find(term)
    if index < 0:
        return ""
    start = max(0, index - radius)
    end = min(len(text), index + len(term) + radius)
    return text[start:end]


def _review_risk(candidate: TerminologyCandidate, proposal: TerminologyProposal) -> str:
    """Return a deterministic evidence/routing class; risk is not an automatic deferral."""
    if proposal.blocked or proposal.conflict_codes:
        return "canonical-conflict"
    if candidate.context_sensitive or candidate.classification_conflicts:
        return "high"
    if candidate.confidence < 0.75 or proposal.confidence < 0.75:
        return "high"
    if candidate.confidence < 0.9 or proposal.confidence < 0.9:
        return "medium"
    return "low"


def _estimated_tokens(value: Any) -> int:
    # A conservative provider-neutral estimate. Identity includes the rendered prompt bytes too.
    return (len(canonical_json_bytes(value)) + 2) // 3


def _review_shards(
    plan: TerminologyRunPlan, review_items: tuple[dict[str, Any], ...]
) -> tuple[dict[str, Any], ...]:
    """Plan stable token/count-bounded shards without splitting candidates."""
    proposal_by_id = {item.proposal_id: item for item in plan.proposals}
    ordered = sorted(
        review_items,
        key=lambda item: (
            {"canonical-conflict": 0, "high": 1, "medium": 2, "low": 3}[item["risk_class"]],
            item["proposal"]["proposal_id"],
        ),
    )
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for item in ordered:
        tokens = _estimated_tokens(item)
        if current and (
            len(current) >= REVIEW_SHARD_MAX_CANDIDATES
            or current_tokens + tokens > REVIEW_SHARD_TOKEN_BUDGET
            or current[-1]["risk_class"] != item["risk_class"]
        ):
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(item)
        current_tokens += tokens
    if current:
        groups.append(current)
    shards: list[dict[str, Any]] = []
    for index, items in enumerate(groups):
        ids = [item["proposal"]["proposal_id"] for item in items]
        prompt = review_prompt(tuple(items), plan.style_guide)
        identity_payload = {
            "proposal_run_id": plan.proposal_run_id,
            "proposal_ids": ids,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "style_resource_sha256": plan.style_resource_sha256,
            "provider": plan.provider,
            "provider_adapter_version": plan.provider_adapter_version,
            "model": plan.model,
            "review_engine_version": TERMINOLOGY_REVIEW_ENGINE_VERSION,
        }
        digest = hashlib.sha256(canonical_json_bytes(identity_payload)).hexdigest()
        shards.append(
            {
                "shard_id": f"trs-{digest[:24]}",
                "index": index,
                "risk_class": items[0]["risk_class"],
                "proposal_ids": ids,
                "estimated_tokens": _estimated_tokens({"items": items, "style": plan.style_guide}),
                "prompt_sha256": identity_payload["prompt_sha256"],
                "items": items,
            }
        )
        # Defensive coverage assertion also catches accidental planner drift during refactors.
        if any(
            proposal_by_id[item_id].candidate_id != item["proposal"]["candidate_id"]
            for item_id, item in zip(ids, items, strict=True)
        ):
            raise TerminologyWorkflowError("review shard proposal/candidate mismatch")
    flattened = [item for shard in shards for item in shard["proposal_ids"]]
    if len(flattened) != len(set(flattened)) or set(flattened) != set(proposal_by_id):
        raise TerminologyWorkflowError("review shard plan does not exactly cover proposals")
    return tuple(shards)


def plan_reviews(
    proposal_run: str | Path,
    *,
    project_root: Path,
    registry_path: Path = Path("config/terminology.yaml"),
    glossary_path: Path = Path("config/glossary.yaml"),
    style_path: Path = Path("config/style-guide.md"),
    provider: str = "fake",
    provider_adapter_version: str = "1.0.0",
    model: str | None = None,
    output_root: Path | None = None,
    retry_of: str | None = None,
) -> TerminologyRunPlan:
    project_root = project_root.resolve()
    path = Path(proposal_run)
    if not path.is_absolute():
        direct = project_root / path
        path = direct if direct.is_dir() else project_root / "workspace/terminology-runs" / path
    proposal_metadata, proposals = load_proposal_output(path)
    if proposal_metadata.discovery_run_id is None:
        raise TerminologyWorkflowError("proposal run has no discovery lineage")
    _, candidates = load_discovery_output(path.parent / proposal_metadata.discovery_run_id)
    registry = load_registry(project_root / registry_path)
    glossary_file = project_root / glossary_path
    style_file = project_root / style_path
    glossary = yaml.safe_load(glossary_file.read_text(encoding="utf-8"))
    style_guide = style_file.read_text(encoding="utf-8")
    view = canonical_view(registry, glossary)
    reviewable = tuple(item for item in proposals if item.action == "ai_proposal")
    payload = {
        "stage": "review",
        "engine_version": TERMINOLOGY_ANALYSIS_VERSION,
        "target_style_version": target_style_adapter(proposal_metadata.target_language).version,
        "proposal_run_id": proposal_metadata.run_id,
        "proposal_output_sha256": proposal_metadata.output_sha256,
        "registry_sha256": registry_identity(registry),
        "canonical_view_sha256": canonical_view_identity(view),
        "glossary_sha256": sha256_file(glossary_file),
        "style_resource_sha256": sha256_file(style_file),
        "provider": provider,
        "provider_adapter_version": provider_adapter_version,
        "model": model,
        "proposal_ids": [item.proposal_id for item in reviewable],
        "prompt_sha256": hashlib.sha256(REVIEW_SYSTEM_MESSAGE.encode()).hexdigest(),
        "retry_of": retry_of,
    }
    run_id, request_hash = _run_id(payload)
    root = (output_root or project_root / "workspace/terminology-runs").resolve()
    if retry_of is not None and not (
        (root / retry_of).is_dir() or (root / f".{retry_of}.requesting.json").is_file()
    ):
        raise TerminologyWorkflowError("--retry-of does not reference retained request evidence")
    return TerminologyRunPlan(
        project_root,
        root,
        run_id,
        TerminologyAnalysisStage.REVIEW,
        proposal_metadata.snapshot_id,
        proposal_metadata.snapshot_manifest_sha256,
        proposal_metadata.source_language,
        proposal_metadata.target_language,
        tuple(proposal_metadata.sources),
        provider,
        provider_adapter_version,
        model,
        request_hash,
        payload,
        source_lineage_kind=proposal_metadata.source_lineage_kind,
        source_audit_id=proposal_metadata.source_audit_id,
        source_anomalies_sha256=proposal_metadata.source_anomalies_sha256,
        discovery_run_id=proposal_metadata.discovery_run_id,
        proposal_run_id=proposal_metadata.run_id,
        proposal_output_sha256=proposal_metadata.output_sha256,
        registry_sha256=registry_identity(registry),
        candidates=candidates,
        proposals=reviewable,
        canonical_terms=view,
        glossary_sha256=sha256_file(glossary_file),
        canonical_view_sha256=canonical_view_identity(view),
        style_resource_sha256=sha256_file(style_file),
        target_style_version=target_style_adapter(proposal_metadata.target_language).version,
        style_guide=style_guide,
        retry_of=retry_of,
        action="skip" if (root / run_id).is_dir() else "create",
    )


def execute_reviews(
    plan: TerminologyRunPlan,
    provider: TerminologyReviewProvider,
    *,
    registry_path: Path = Path("config/terminology.yaml"),
    glossary_path: Path = Path("config/glossary.yaml"),
    style_path: Path = Path("config/style-guide.md"),
    now: Callable[[], datetime] | None = None,
    only_shard_id: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> TerminologyAnalysisRun:
    if plan.action == "skip":
        return _load(plan.output_directory / RUN_FILE, TerminologyAnalysisRun)
    registry = load_registry(plan.project_root / registry_path)
    glossary = yaml.safe_load((plan.project_root / glossary_path).read_text(encoding="utf-8"))
    view = canonical_view(registry, glossary)
    if registry_identity(registry) != plan.registry_sha256:
        raise TerminologyWorkflowError("terminology registry changed after review planning")
    if canonical_view_identity(view) != plan.canonical_view_sha256:
        raise TerminologyWorkflowError("canonical terminology view changed after review planning")
    if sha256_file(plan.project_root / style_path) != plan.style_resource_sha256:
        raise TerminologyWorkflowError("style resource changed after review planning")
    if (
        provider.provider_name != plan.provider
        or provider.adapter_version != plan.provider_adapter_version
    ):
        raise TerminologyWorkflowError("terminology review provider does not match planned adapter")
    candidate_by_id = {item.candidate_id: item for item in plan.candidates}
    source_parts = [
        (item, (plan.project_root / item.source_file).read_text(encoding="utf-8"))
        for item in plan.sources
    ]
    review_items = []
    for proposal in plan.proposals:
        candidate = candidate_by_id[proposal.candidate_id]
        constraints = canonical_constraints(candidate, plan.canonical_terms)
        evidence = []
        evidence_limit = 1 if _review_risk(candidate, proposal) == "low" else 4
        for source, text in source_parts:
            excerpt = _evidence_excerpt(candidate.source_term, text)
            if excerpt:
                evidence.append(
                    {
                        "chapter_id": source.chapter_id,
                        "chunk_id": source.chunk_id,
                        "excerpt": excerpt,
                    }
                )
            if len(evidence) >= evidence_limit:
                break
        review_items.append(
            {
                "proposal": proposal.model_dump(mode="json"),
                "candidate": candidate.model_dump(mode="json")
                | {
                    "occurrences": [
                        item.model_dump(mode="json")
                        for item in candidate.occurrences[:evidence_limit]
                    ]
                },
                "occurrence_count": sum(item.count for item in candidate.occurrences),
                "risk_class": _review_risk(candidate, proposal),
                "selected_evidence": evidence,
                "canonical_constraints": [
                    {
                        "source_term": item.source_term,
                        "target_term": item.target_term,
                        "locked": item.locked,
                        "origin": item.origin,
                    }
                    for item in constraints
                ],
                "source_language": plan.source_language,
                "target_language": plan.target_language,
            }
        )
    plan.output_root.mkdir(parents=True, exist_ok=True)
    shards = _review_shards(plan, tuple(review_items))
    staging = plan.output_root / f".{plan.run_id}.review"
    staging.mkdir(exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "run_id": plan.run_id,
        "proposal_run_id": plan.proposal_run_id,
        "review_engine_version": TERMINOLOGY_REVIEW_ENGINE_VERSION,
        "provider": plan.provider,
        "provider_adapter_version": plan.provider_adapter_version,
        "model": plan.model,
        "shards": [{k: v for k, v in shard.items() if k != "items"} for shard in shards],
    }
    manifest_path = staging / "plan.json"
    rendered_manifest = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if manifest_path.exists() and manifest_path.read_text(encoding="utf-8") != rendered_manifest:
        raise TerminologyWorkflowError(
            "persisted review shard plan differs from deterministic plan"
        )
    if not manifest_path.exists():
        atomic_write_text(manifest_path, rendered_manifest)
    if only_shard_id is not None and only_shard_id not in {s["shard_id"] for s in shards}:
        raise TerminologyWorkflowError("unknown terminology review shard")
    validated_reviews: list[TerminologyReview] = []
    proposal_by_id = {item.proposal_id: item for item in plan.proposals}
    for shard in shards:
        shard_id = shard["shard_id"]
        result_path = staging / f"{shard_id}.validated.yaml"
        if result_path.exists():
            response = _load(result_path, TerminologyReviewResponse)
            proposal_batch = tuple(proposal_by_id[item] for item in shard["proposal_ids"])
            validated_reviews.extend(
                validate_reviews(
                    response,
                    proposal_batch,
                    plan.candidates,
                    plan.canonical_terms,
                    plan.target_language,
                )
            )
            continue
        if only_shard_id is not None and shard_id != only_shard_id:
            continue
        proposal_batch = tuple(proposal_by_id[item] for item in shard["proposal_ids"])
        item_batch = tuple(shard["items"])
        attempts = sorted(staging.glob(f"{shard_id}.attempt-*.yaml"))
        if len(attempts) >= REVIEW_SHARD_MAX_ATTEMPTS:
            raise TerminologyWorkflowError(
                f"review shard {shard_id} exhausted {REVIEW_SHARD_MAX_ATTEMPTS} attempts"
            )
        if progress is not None:
            progress(
                f"Review shard {shard['index'] + 1}/{len(shards)} | "
                f"Candidates: {len(proposal_batch)} | Provider: {plan.provider} | "
                "Status: requesting"
            )
        request = TerminologyReviewRequest(
            request_id=f"{plan.request_sha256}:{shard_id}",
            system_message=REVIEW_SYSTEM_MESSAGE,
            user_message=review_prompt(item_batch, plan.style_guide),
            source_language=plan.source_language,
            target_language=plan.target_language,
            proposal_ids=tuple(item.proposal_id for item in proposal_batch),
        )
        raw_response = provider.review(request)
        attempt_path = staging / f"{shard_id}.attempt-{len(attempts) + 1:02d}.yaml"
        persisted_response = (
            raw_response.model_dump(mode="json")
            if isinstance(raw_response, BaseModel)
            else raw_response
        )
        atomic_write_text(
            attempt_path,
            yaml.safe_dump(persisted_response, allow_unicode=True, sort_keys=False),
        )
        try:
            response = parse_review_response(raw_response)
            values = validate_reviews(
                response,
                proposal_batch,
                plan.candidates,
                plan.canonical_terms,
                plan.target_language,
            )
            validated_response = TerminologyReviewResponse(
                schema_version=SCHEMA_VERSION, reviews=list(values)
            )
            atomic_write_text(result_path, _serialize(validated_response))
            validated_reviews.extend(values)
            if progress is not None:
                progress(
                    f"Review shard {shard['index'] + 1}/{len(shards)} | "
                    f"Candidates: {len(proposal_batch)} | Provider: {plan.provider} | "
                    "Status: accepted"
                )
        except TerminologyAnalysisError as exc:
            raise TerminologyWorkflowError(
                f"review shard {shard_id} failed strict validation: {exc}"
            ) from exc
    missing = [
        s["shard_id"] for s in shards if not (staging / f"{s['shard_id']}.validated.yaml").exists()
    ]
    if missing:
        raise TerminologyWorkflowError(
            f"review remains incomplete; missing validated shards: {', '.join(missing)}"
        )
    reviews = tuple(validated_reviews)
    if len(reviews) != len(plan.proposals) or {r.proposal_id for r in reviews} != {
        p.proposal_id for p in plan.proposals
    }:
        raise TerminologyWorkflowError(
            "aggregate review does not exactly cover proposal review set"
        )
    timestamp = (now or (lambda: datetime.now(UTC)))()
    output = TerminologyReviewResponse(schema_version=SCHEMA_VERSION, reviews=list(reviews))
    report = TerminologyAnalysisValidationReport(
        schema_version=SCHEMA_VERSION,
        run_id=plan.run_id,
        valid=True,
        stage=plan.stage,
        source_count=len(plan.sources),
        item_count=len(reviews),
        merged_duplicate_count=0,
        conflict_count=sum(item.blocked for item in reviews),
        created_at=timestamp,
    )
    run = _publish(plan, output, report, timestamp)
    return run


def load_review_output(
    run_path: Path,
) -> tuple[TerminologyAnalysisRun, tuple[TerminologyReview, ...]]:
    run = _load(run_path / RUN_FILE, TerminologyAnalysisRun)
    response = _load(run_path / OUTPUT_FILE, TerminologyReviewResponse)
    if run.stage is not TerminologyAnalysisStage.REVIEW:
        raise TerminologyWorkflowError("run is not a terminology review run")
    if sha256_file(run_path / OUTPUT_FILE) != run.output_sha256:
        raise TerminologyWorkflowError("review output hash mismatch")
    return run, tuple(response.reviews)


def audit_proposal_run(
    run_path: Path,
    *,
    project_root: Path,
    registry_path: Path = Path("config/terminology.yaml"),
    glossary_path: Path = Path("config/glossary.yaml"),
) -> tuple[dict[str, Any], ...]:
    """Re-evaluate an immutable proposal artifact against current canonical knowledge."""
    from book_translator.terminology.canonical import (
        canonicalization_confidence,
        proposal_conflicts,
    )

    run, proposals = load_proposal_output(run_path)
    if run.discovery_run_id is None:
        raise TerminologyWorkflowError("proposal run has no discovery lineage")
    _, candidates = load_discovery_output(run_path.parent / run.discovery_run_id)
    by_id = {item.candidate_id: item for item in candidates}
    registry = load_registry(project_root / registry_path)
    glossary = yaml.safe_load((project_root / glossary_path).read_text(encoding="utf-8"))
    view = canonical_view(registry, glossary)
    results: list[dict[str, Any]] = []
    for proposal in proposals:
        candidate = by_id[proposal.candidate_id]
        exact = exact_canonical(candidate, view)
        conflicts = proposal_conflicts(proposal, candidate, view, registry.target_language)
        scope_confidence = canonicalization_confidence(candidate)
        results.append(
            {
                "source_term": candidate.source_term,
                "proposed_target": proposal.proposed_target,
                "action": "reuse-existing" if exact is not None else "ai-proposal",
                "existing_target": exact.target_term if exact is not None else None,
                "canonical_source": (
                    "locked_registry"
                    if exact is not None and exact.locked
                    else exact.origin
                    if exact is not None
                    else None
                ),
                "conflicts": list(conflicts),
                "auto_eligible": exact is None and scope_confidence > 0.0 and not conflicts,
                "canonicalization_confidence": scope_confidence,
            }
        )
    return tuple(results)


def audit_review_run(
    run_path: Path,
    *,
    project_root: Path,
    registry_path: Path = Path("config/terminology.yaml"),
    glossary_path: Path = Path("config/glossary.yaml"),
) -> tuple[dict[str, Any], ...]:
    """Re-evaluate immutable review targets against current canonical terminology."""
    run, reviews = load_review_output(run_path)
    if run.proposal_run_id is None:
        raise TerminologyWorkflowError("review run has no proposal lineage")
    proposal_run, proposals = load_proposal_output(run_path.parent / run.proposal_run_id)
    if proposal_run.discovery_run_id is None:
        raise TerminologyWorkflowError("proposal run has no discovery lineage")
    _, candidates = load_discovery_output(run_path.parent / proposal_run.discovery_run_id)
    candidate_by_id = {item.candidate_id: item for item in candidates}
    proposal_by_id = {item.proposal_id: item for item in proposals}
    registry = load_registry(project_root / registry_path)
    glossary = yaml.safe_load((project_root / glossary_path).read_text(encoding="utf-8"))
    view = canonical_view(registry, glossary)
    rows: list[dict[str, Any]] = []
    for review in reviews:
        proposal = proposal_by_id[review.proposal_id]
        candidate = candidate_by_id[proposal.candidate_id]
        conflicts: tuple[str, ...] = ()
        if review.reviewed_target is not None:
            conflicts = proposal_conflicts(
                proposal.model_copy(update={"proposed_target": review.reviewed_target}),
                candidate,
                view,
                registry.target_language,
            )
        rows.append(
            {
                "proposal_id": review.proposal_id,
                "source_term": candidate.source_term,
                "disposition": review.disposition.value,
                "blocked": bool(conflicts),
                "conflicts": list(conflicts),
            }
        )
    return tuple(rows)
