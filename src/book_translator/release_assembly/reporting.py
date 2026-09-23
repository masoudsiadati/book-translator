"""Deterministic release record construction and serialization. No I/O, no clock of its own."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel

from book_translator.models import SCHEMA_VERSION
from book_translator.models.release import (
    ReleaseArtifactReference,
    ReleaseArtifactRole,
    ReleaseChapterRecord,
    ReleaseChunkLineage,
    ReleaseLineageDocument,
    ReleaseManifest,
    ReleaseSemanticWaiverReference,
    ReleaseTranslationReport,
)
from book_translator.models.translation_queue import TranslationRun
from book_translator.models.translation_snapshot import TranslationManifest, TranslationSnapshot
from book_translator.release_assembly.engine import (
    RELEASE_ASSEMBLY_ENGINE_VERSION,
    ReleaseAssemblyError,
)
from book_translator.release_assembly.identity import release_content_address
from book_translator.release_assembly.models import ReleaseFile

MANIFEST_FILENAME = "manifest.yaml"
BOOK_FILENAME = "book.md"
METADATA_FILENAME = "metadata.yaml"
TERMINOLOGY_FILENAME = "terminology.yaml"
TRANSLATION_REPORT_FILENAME = "translation-report.yaml"
LINEAGE_FILENAME = "provenance/translation-lineage.json"
CHAPTERS_DIRECTORY = "chapters"


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def serialize_release_yaml(model: BaseModel) -> str:
    """Render one release record as deterministic UTF-8 YAML in declared field order."""
    rendered = yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise ReleaseAssemblyError("could not serialize a release record")
    return rendered


def serialize_release_json(model: BaseModel) -> str:
    """Render one release record as deterministic UTF-8 JSON in declared field order."""
    payload = model.model_dump(mode="json", exclude_none=False)
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def release_file(relative_path: str, role: ReleaseArtifactRole, content: bytes) -> ReleaseFile:
    return ReleaseFile(relative_path=relative_path, role=role, content=content)


def artifact_reference(file: ReleaseFile) -> ReleaseArtifactReference:
    return ReleaseArtifactReference(
        file=Path(file.relative_path),
        role=file.role,
        sha256=sha256_bytes(file.content),
        byte_count=len(file.content),
    )


def build_lineage_document(
    *,
    release_id: str,
    run: TranslationRun,
    manifest: TranslationManifest,
    chunks: tuple[ReleaseChunkLineage, ...],
) -> ReleaseLineageDocument:
    return ReleaseLineageDocument(
        schema_version=SCHEMA_VERSION,
        release_assembly_engine_version=RELEASE_ASSEMBLY_ENGINE_VERSION,
        release_id=release_id,
        book_id=manifest.book.book_id,
        source_translation_run=run.run_id,
        snapshot_id=run.snapshot_id,
        snapshot_manifest_sha256=run.snapshot_manifest_sha256,
        chunk_count=len(chunks),
        chunks=list(chunks),
    )


def build_translation_report(
    *,
    release_id: str,
    run: TranslationRun,
    snapshot: TranslationSnapshot,
    manifest: TranslationManifest,
    chunks: tuple[ReleaseChunkLineage, ...],
    provider_attempts: int,
    abandoned_attempts: int,
    provider_repairs: int,
    provider_recoveries: int,
    repair_escalations: int,
    structural_repairs: int,
    qa_reevaluations: int,
    cross_run_reuses: int,
    historical_carry_forwards: int,
    semantic_judged_pass: int = 0,
    semantic_not_judged: int = 0,
    semantic_waiver: ReleaseSemanticWaiverReference | None = None,
) -> ReleaseTranslationReport:
    """Summarize how the run reached full acceptance, in counters only."""

    def tally(values: tuple[str | None, ...]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            key = value if value is not None else "unrecorded"
            counts[key] = counts.get(key, 0) + 1
        return {key: counts[key] for key in sorted(counts)}

    def inventory(values: tuple[str | None, ...]) -> list[str]:
        return sorted({value for value in values if value is not None})

    return ReleaseTranslationReport(
        schema_version=SCHEMA_VERSION,
        release_assembly_engine_version=RELEASE_ASSEMBLY_ENGINE_VERSION,
        release_id=release_id,
        run_id=run.run_id,
        snapshot_id=run.snapshot_id,
        snapshot_manifest_sha256=run.snapshot_manifest_sha256,
        queue_engine_version=run.queue_engine_version,
        run_status=run.status.value,
        source_language=run.source_language,
        target_language=run.target_language,
        chapter_count=len(manifest.chapters),
        total_chunks=len(chunks),
        accepted_chunks=len(chunks),
        qa_rejected_chunks=0,
        provider_attempts=provider_attempts,
        abandoned_attempts=abandoned_attempts,
        provider_repairs=provider_repairs,
        provider_recoveries=provider_recoveries,
        repair_escalations=repair_escalations,
        structural_repairs=structural_repairs,
        qa_reevaluations=qa_reevaluations,
        cross_run_reuses=cross_run_reuses,
        historical_carry_forwards=historical_carry_forwards,
        acceptance_origins=tally(tuple(chunk.acceptance_origin.value for chunk in chunks)),
        providers=tally(tuple(chunk.provider for chunk in chunks)),
        models=tally(tuple(chunk.model for chunk in chunks)),
        terminology_qa_versions=inventory(tuple(chunk.terminology_qa_version for chunk in chunks)),
        structural_qa_versions=inventory(tuple(chunk.structural_qa_version for chunk in chunks)),
        prompt_version=snapshot.prompt_version,
        glossary_version=snapshot.glossary_version,
        style_guide_version=snapshot.style_guide_version,
        terminology_version=snapshot.terminology_version,
        source_anomalies_version=snapshot.source_anomalies_version,
        open_questions_version=snapshot.open_questions_version,
        semantic_gate_policy_version=run.semantic_gate_policy_version,
        semantic_judged_pass=semantic_judged_pass,
        semantic_not_judged=semantic_not_judged,
        semantic_waiver=semantic_waiver,
    )


def build_manifest(
    *,
    release_id: str,
    run: TranslationRun,
    manifest: TranslationManifest,
    chapters: tuple[ReleaseChapterRecord, ...],
    files: tuple[ReleaseFile, ...],
    book_metadata_sha256: str | None,
    terminology_sha256: str | None,
    terminology_version: str | None,
    chapter_title_artifact_id: str | None,
    chapter_title_artifact_sha256: str | None,
    chunk_count: int,
    paragraph_count: int,
    word_count: int,
    created_at: datetime,
    semantic_waiver: ReleaseSemanticWaiverReference | None = None,
) -> ReleaseManifest:
    artifacts = tuple(
        sorted(
            (artifact_reference(file) for file in files),
            key=lambda item: item.file.as_posix(),
        )
    )
    return ReleaseManifest(
        schema_version=SCHEMA_VERSION,
        release_assembly_engine_version=RELEASE_ASSEMBLY_ENGINE_VERSION,
        release_id=release_id,
        book_id=manifest.book.book_id,
        source_language=run.source_language,
        target_language=run.target_language,
        source_translation_run=run.run_id,
        snapshot_id=run.snapshot_id,
        snapshot_manifest_sha256=run.snapshot_manifest_sha256,
        book_metadata_file=Path(METADATA_FILENAME) if book_metadata_sha256 else None,
        book_metadata_sha256=book_metadata_sha256,
        terminology_file=Path(TERMINOLOGY_FILENAME) if terminology_sha256 else None,
        terminology_sha256=terminology_sha256,
        terminology_version=terminology_version,
        chapter_title_artifact_id=chapter_title_artifact_id,
        chapter_title_artifact_sha256=chapter_title_artifact_sha256,
        semantic_waiver=semantic_waiver,
        chapter_count=len(chapters),
        chunk_count=chunk_count,
        accepted_translation_count=chunk_count,
        paragraph_count=paragraph_count,
        word_count=word_count,
        chapters=list(chapters),
        artifacts=list(artifacts),
        content_sha256=release_content_address(artifacts),
        created_at=created_at,
    )
