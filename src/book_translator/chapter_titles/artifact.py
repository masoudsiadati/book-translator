"""Read-only access to a published chapter-title artifact.

This module is what a *consumer* imports — release assembly in particular. It reads, verifies, and
hands back titles. It has no provider import, no writer, and no clock, so nothing that consumes a
title artifact can accidentally acquire the ability to produce one.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from book_translator.chapter_titles.engine import ChapterTitleError
from book_translator.models.chapter_title import (
    ChapterTitleDocument,
    ChapterTitleManifest,
    ChapterTitleProvenance,
)

CHAPTER_TITLES_CATEGORY = "chapter-titles"
DOCUMENT_FILENAME = "chapter-titles.yaml"
MANIFEST_FILENAME = "manifest.yaml"
PROVENANCE_FILENAME = "provenance.json"


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
        return model.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError, ValidationError) as exc:
        raise ChapterTitleError(f"unreadable chapter-title artifact {path}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ChapterTitleArtifact:
    """One published, hash-verified chapter-title artifact."""

    path: Path
    manifest: ChapterTitleManifest
    document: ChapterTitleDocument

    @property
    def artifact_id(self) -> str:
        return self.manifest.artifact_id

    @property
    def content_sha256(self) -> str:
        return self.document.content_sha256

    def overlay(self) -> dict[str, str]:
        """Return ``chapter_id -> title_fa`` for every chapter this artifact carries."""
        return {record.chapter_id: record.title_fa for record in self.document.chapters}


def load_chapter_title_artifact(path: Path) -> ChapterTitleArtifact:
    """Load one artifact directory, proving every indexed file still hashes as published."""
    manifest = _load(path / MANIFEST_FILENAME, ChapterTitleManifest)
    for reference in manifest.artifacts:
        target = path / reference.file
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise ChapterTitleError(
                f"chapter-title artifact file is missing: {reference.file.as_posix()}"
            ) from exc
        if sha256_bytes(content) != reference.sha256 or len(content) != reference.byte_count:
            raise ChapterTitleError(
                f"chapter-title artifact file changed after publication: "
                f"{reference.file.as_posix()}"
            )
    document = _load(path / DOCUMENT_FILENAME, ChapterTitleDocument)
    if (
        document.artifact_id != manifest.artifact_id
        or document.content_sha256 != manifest.content_sha256
        or document.snapshot_id != manifest.snapshot_id
        or document.snapshot_manifest_sha256 != manifest.snapshot_manifest_sha256
    ):
        raise ChapterTitleError("chapter-title manifest and document disagree")
    return ChapterTitleArtifact(path=path, manifest=manifest, document=document)


def load_chapter_title_provenance(path: Path) -> ChapterTitleProvenance:
    """Load one artifact's history."""
    return _load(path / PROVENANCE_FILENAME, ChapterTitleProvenance)


def discover_chapter_title_artifact(
    *, project_root: Path, snapshot_id: str, snapshot_manifest_sha256: str
) -> ChapterTitleArtifact | None:
    """Find the published title artifact for one snapshot, or prove there is none.

    Discovery matches on the *snapshot*, not on the full input identity, so a later terminology
    generation does not silently make an existing artifact invisible and drop a release back to
    English headings. What protects correctness instead is that the consumer re-runs title QA
    against its own frozen terminology and refuses a set that no longer holds.

    A provider-free revision is the one exception to "two artifacts for one snapshot is an
    operator question". A revision names the artifact it derives from, so the pair is ordered
    rather than ambiguous: every artifact some other artifact revises is superseded, and what is
    left is the head of the chain. Two artifacts that revise nothing, or two heads, still fail
    closed, because that really is a question only a person can answer.
    """
    from book_translator.paths import ProjectPaths

    root = ProjectPaths(project_root).chapter_title_artifacts
    if not root.is_dir():
        return None
    matches: list[ChapterTitleArtifact] = []
    for candidate in sorted(root.iterdir(), key=lambda item: item.name):
        if not candidate.is_dir() or candidate.name.startswith("."):
            continue
        if not (candidate / MANIFEST_FILENAME).is_file():
            continue
        artifact = load_chapter_title_artifact(candidate)
        if (
            artifact.manifest.snapshot_id == snapshot_id
            and artifact.manifest.snapshot_manifest_sha256 == snapshot_manifest_sha256
        ):
            matches.append(artifact)
    if not matches:
        return None
    superseded = {
        artifact.manifest.parent_artifact_id
        for artifact in matches
        if artifact.manifest.parent_artifact_id is not None
    }
    heads = [artifact for artifact in matches if artifact.artifact_id not in superseded]
    if len(heads) != 1:
        listed = ", ".join(item.artifact_id for item in matches)
        raise ChapterTitleError(
            f"more than one chapter-title artifact matches this snapshot ({listed}); "
            "name the one to use explicitly"
        )
    return heads[0]
