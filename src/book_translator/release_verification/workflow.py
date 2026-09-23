"""The only release-verification layer that touches the filesystem — and it only reads.

``verify_release_artifact`` opens a release directory, proves what can be proved, and returns a
verdict. ``write_verification_report`` is the one function that writes anything, and only to the
path an operator names.

The stage is deliberately the narrowest in the project. It has no provider import and no path to
one, it opens every file read-only, it never touches a translation run, snapshot, job, attempt,
acceptance record, or the terminology registry, and its only non-determinism is the report's own
timestamp — which is excluded from the report's content address precisely so that two runs over an
unchanged release are comparable byte for byte.

It fails closed in both directions. A check that finds a discrepancy fails; a check whose evidence
is unreachable is *skipped* and says so, and a skipped check is never presented as a pass.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.chapter_titles.artifact import (
    load_chapter_title_artifact,
)
from book_translator.chapter_titles.engine import ChapterTitleError
from book_translator.chapter_titles.qa import validate_chapter_titles
from book_translator.models import SCHEMA_VERSION, TerminologyRegistry
from book_translator.models.release import ReleaseLineageDocument, ReleaseManifest
from book_translator.models.release_verification import (
    RELEASE_CHECK_ORDER,
    ReleaseCheck,
    ReleaseCheckStatus,
    ReleaseVerificationCheck,
    ReleaseVerificationFinding,
    ReleaseVerificationReport,
    ReleaseVerificationStatus,
)
from book_translator.paths import ProjectPaths
from book_translator.release_assembly import (
    BOOK_FILENAME,
    LINEAGE_FILENAME,
    MANIFEST_FILENAME,
    METADATA_FILENAME,
    TERMINOLOGY_FILENAME,
)
from book_translator.release_verification.engine import (
    RELEASE_VERIFICATION_ENGINE_VERSION,
    ReleaseVerificationError,
    check_book,
    check_chapters,
    check_manifest,
    check_provenance,
    check_terminology,
    check_titles,
    check_translations,
    report_content_address,
)
from book_translator.terminology import load_registry


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load[ModelT: BaseModel](path: Path, model: type[ModelT], *, as_json: bool = False) -> ModelT:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw) if as_json else yaml.safe_load(raw)
        return model.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError, ValidationError) as exc:
        raise ReleaseVerificationError(f"unreadable release artifact {path.name}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ReleaseVerification:
    """One complete verdict, plus the report path when one was written."""

    report: ReleaseVerificationReport
    release_path: Path
    report_file: Path | None = None

    @property
    def ready(self) -> bool:
        return self.report.status is ReleaseVerificationStatus.READY_FOR_PUBLICATION


def _read_release_files(
    release_path: Path,
) -> tuple[dict[str, bytes], dict[str, str], dict[str, int]]:
    contents: dict[str, bytes] = {}
    for path in sorted(release_path.rglob("*")):
        if not path.is_file():
            continue
        name = path.relative_to(release_path).as_posix()
        if name == MANIFEST_FILENAME:
            continue
        try:
            contents[name] = path.read_bytes()
        except OSError as exc:
            raise ReleaseVerificationError(f"unreadable release file {name}") from exc
    return (
        contents,
        {name: _sha256(content) for name, content in contents.items()},
        {name: len(content) for name, content in contents.items()},
    )


def _decoded_chapters(
    manifest: ReleaseManifest, contents: dict[str, bytes]
) -> tuple[dict[str, str], tuple[ReleaseVerificationFinding, ...]]:
    """Decode every chapter file, reporting the ones that are not valid UTF-8."""
    texts: dict[str, str] = {}
    findings: list[ReleaseVerificationFinding] = []
    for record in manifest.chapters:
        name = record.file.as_posix()
        raw = contents.get(name)
        if raw is None:
            continue
        try:
            texts[name] = raw.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(
                ReleaseVerificationFinding(
                    check=ReleaseCheck.CHAPTERS,
                    code="invalid_utf8",
                    severity="error",
                    subject=record.chapter_id,
                    message="chapter file is not valid UTF-8",
                )
            )
    return texts, tuple(findings)


def _title_evidence(
    manifest: ReleaseManifest, *, project_root: Path, workspace_reachable: bool
) -> tuple[str | None, dict[str, str] | None, tuple[str, ...]]:
    """Read the referenced title artifact and re-judge it, without changing a byte of it."""
    if manifest.chapter_title_artifact_id is None or not workspace_reachable:
        return None, None, ()
    path = ProjectPaths(project_root).chapter_title_artifact(manifest.chapter_title_artifact_id)
    try:
        artifact = load_chapter_title_artifact(path)
    except (ChapterTitleError, ValueError):
        return None, None, ()
    registry = _frozen_registry(manifest, project_root)
    report = validate_chapter_titles(
        expected=tuple((record.chapter_id, record.title_en) for record in manifest.chapters),
        records=tuple(artifact.document.chapters),
        registry=registry,
    )
    codes = tuple(item.code for item in report.findings if item.severity == "error")
    return artifact.content_sha256, artifact.overlay(), codes


def _frozen_registry(manifest: ReleaseManifest, project_root: Path) -> TerminologyRegistry:
    """Load the terminology the release named — the registry its titles are held to."""
    empty = TerminologyRegistry(
        schema_version=SCHEMA_VERSION,
        version="1.0.0",
        book_id=manifest.book_id,
        source_language=manifest.source_language,
        target_language=manifest.target_language,
    )
    if manifest.terminology_sha256 is None:
        return empty
    path = ProjectPaths(project_root).terminology_resource(manifest.terminology_sha256)
    if not path.is_file():
        return empty
    try:
        return load_registry(path)
    except ValueError:
        return empty


def _frozen_sha256(project_root: Path, category: str, digest: str | None) -> str | None:
    """Hash the workspace's own frozen copy of one resource, if it is still there."""
    if digest is None:
        return None
    path = ProjectPaths(project_root).frozen_resource(category, digest)
    if not path.is_file():
        return None
    try:
        return _sha256(path.read_bytes())
    except OSError:
        return None


def _check(
    check: ReleaseCheck,
    findings: tuple[ReleaseVerificationFinding, ...],
    *,
    examined: int,
    expected: int,
    detail: str,
    skipped: bool = False,
) -> ReleaseVerificationCheck:
    codes = sorted({item.code for item in findings if item.check is check})
    if skipped:
        status = ReleaseCheckStatus.SKIPPED
    elif codes:
        status = ReleaseCheckStatus.FAIL
    else:
        status = ReleaseCheckStatus.PASS
    return ReleaseVerificationCheck(
        check=check,
        status=status,
        examined=examined,
        expected=expected,
        detail=detail,
        finding_codes=codes,
    )


def verify_release_artifact(
    release_path: str | Path,
    *,
    project_root: Path,
    now: Callable[[], datetime] | None = None,
) -> ReleaseVerification:
    """Prove one release is complete, consistent, immutable, and traceable. Reads only."""
    project_root = project_root.resolve()
    candidate = Path(release_path)
    resolved = (candidate if candidate.is_absolute() else project_root / candidate).resolve()
    if not resolved.is_dir():
        raise ReleaseVerificationError(f"no release directory at {resolved}")
    manifest_path = resolved / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ReleaseVerificationError(f"no release manifest at {manifest_path}")
    manifest_sha256 = _sha256(manifest_path.read_bytes())
    clock = now or (lambda: datetime.now(UTC))
    try:
        manifest = _load(manifest_path, ReleaseManifest)
    except ReleaseVerificationError:
        # A manifest that does not satisfy its own contract is a verification *result*, not a
        # crash: an operator asking whether a release may be published deserves the answer "no,
        # its index is invalid" in the same shape as every other answer.
        return _invalid_manifest_verdict(
            resolved, project_root=project_root, manifest_sha256=manifest_sha256, now=clock
        )

    contents, file_hashes, file_sizes = _read_release_files(resolved)
    workspace_reachable = ProjectPaths(project_root).workspace.is_dir()

    findings: list[ReleaseVerificationFinding] = []

    manifest_findings, files_verified = check_manifest(
        manifest=manifest,
        published=tuple(contents),
        file_hashes=file_hashes,
        file_sizes=file_sizes,
    )
    findings.extend(manifest_findings)

    chapter_texts, decode_findings = _decoded_chapters(manifest, contents)
    findings.extend(decode_findings)

    book_bytes = contents.get(BOOK_FILENAME)
    findings.extend(
        check_book(manifest=manifest, book_bytes=book_bytes, chapter_texts=chapter_texts)
    )

    chapter_findings, chapters_verified = check_chapters(
        manifest=manifest, chapter_texts=chapter_texts
    )
    findings.extend(chapter_findings)

    lineage_raw = contents.get(LINEAGE_FILENAME)
    lineage: ReleaseLineageDocument | None = None
    if lineage_raw is not None:
        try:
            lineage = ReleaseLineageDocument.model_validate(json.loads(lineage_raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            findings.append(
                ReleaseVerificationFinding(
                    check=ReleaseCheck.PROVENANCE,
                    code="unreadable_lineage",
                    severity="error",
                    message="the release's chunk lineage could not be read",
                )
            )
    translation_findings, chunks_verified = check_translations(manifest=manifest, lineage=lineage)
    findings.extend(translation_findings)
    findings.extend(check_provenance(manifest=manifest, lineage=lineage))

    artifact_sha256, overlay, qa_codes = _title_evidence(
        manifest, project_root=project_root, workspace_reachable=workspace_reachable
    )
    title_findings, titles_verified = check_titles(
        manifest=manifest,
        artifact_content_sha256=artifact_sha256,
        title_overlay=overlay,
        title_qa_error_codes=qa_codes,
    )
    titles_expected = sum(
        1 for record in manifest.chapters if record.heading_source.value == "title_target_artifact"
    )
    titles_skipped = manifest.chapter_title_artifact_id is not None and not workspace_reachable
    if not titles_skipped:
        findings.extend(title_findings)

    released_terminology = file_hashes.get(TERMINOLOGY_FILENAME)
    released_metadata = file_hashes.get(METADATA_FILENAME)
    terminology_findings = check_terminology(
        manifest=manifest,
        released_terminology_sha256=released_terminology,
        frozen_terminology_sha256=(
            _frozen_sha256(project_root, "terminology", manifest.terminology_sha256)
            if workspace_reachable
            else None
        ),
        released_metadata_sha256=released_metadata,
        frozen_metadata_sha256=(
            _frozen_sha256(project_root, "book-metadata", manifest.book_metadata_sha256)
            if workspace_reachable
            else None
        ),
    )
    findings.extend(terminology_findings)

    ordered = sorted(
        findings,
        key=lambda item: (
            RELEASE_CHECK_ORDER.index(item.check),
            item.subject or "",
            item.code,
        ),
    )
    resource_count = sum(
        1 for value in (manifest.terminology_sha256, manifest.book_metadata_sha256) if value
    )
    checks = [
        _check(
            ReleaseCheck.MANIFEST,
            tuple(ordered),
            examined=files_verified,
            expected=len(manifest.artifacts),
            detail="indexed files present, hashed, sized, and content-addressed",
        ),
        _check(
            ReleaseCheck.BOOK_INTEGRITY,
            tuple(ordered),
            examined=0 if book_bytes is None else 1,
            expected=1,
            detail="book present, canonical UTF-8, and identical to its chapters joined",
        ),
        _check(
            ReleaseCheck.CHAPTERS,
            tuple(ordered),
            examined=chapters_verified,
            expected=manifest.chapter_count,
            detail="every chapter present once, in order, with matching counts and heading",
        ),
        _check(
            ReleaseCheck.TRANSLATIONS,
            tuple(ordered),
            examined=chunks_verified,
            expected=manifest.chunk_count,
            detail="every frozen chunk accepted and tiling its chapter without a gap",
        ),
        _check(
            ReleaseCheck.TITLES,
            tuple(ordered),
            examined=0 if titles_skipped else titles_verified,
            expected=titles_expected,
            detail=(
                "title artifact unreachable without the canonical workspace"
                if titles_skipped
                else "referenced title artifact present, hashed, complete, and QA-clean"
            ),
            skipped=titles_skipped,
        ),
        _check(
            ReleaseCheck.TERMINOLOGY,
            tuple(ordered),
            examined=resource_count,
            expected=resource_count,
            detail="released frozen resources are byte-identical to the resources named",
        ),
        _check(
            ReleaseCheck.PROVENANCE,
            tuple(ordered),
            examined=0 if lineage is None else len(lineage.chunks),
            expected=manifest.chunk_count,
            detail="every chunk names its hashes, acceptance, attempt, and provider resolution",
        ),
    ]
    failed = [item.check for item in checks if item.status is ReleaseCheckStatus.FAIL]
    skipped = [item.check for item in checks if item.status is ReleaseCheckStatus.SKIPPED]
    status = (
        ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
        if failed or any(item.severity == "error" for item in ordered)
        else ReleaseVerificationStatus.READY_FOR_PUBLICATION
    )
    try:
        relative = Path(resolved.relative_to(project_root).as_posix())
    except ValueError:
        relative = Path(resolved.name)

    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "release_verification_engine_version": RELEASE_VERIFICATION_ENGINE_VERSION,
        "release_id": manifest.release_id,
        "release_path": relative.as_posix(),
        "release_assembly_engine_version": manifest.release_assembly_engine_version,
        "source_translation_run": manifest.source_translation_run,
        "snapshot_id": manifest.snapshot_id,
        "snapshot_manifest_sha256": manifest.snapshot_manifest_sha256,
        "manifest_sha256": manifest_sha256,
        "release_content_sha256": manifest.content_sha256,
        "book_sha256": file_hashes.get(BOOK_FILENAME),
        "terminology_sha256": manifest.terminology_sha256,
        "chapter_title_artifact_id": manifest.chapter_title_artifact_id,
        "chapter_title_artifact_sha256": manifest.chapter_title_artifact_sha256,
        "files_verified": files_verified,
        "files_expected": len(manifest.artifacts),
        "chapters_verified": chapters_verified,
        "chapters_expected": manifest.chapter_count,
        "chunks_verified": chunks_verified,
        "chunks_expected": manifest.chunk_count,
        "titles_verified": 0 if titles_skipped else titles_verified,
        "titles_expected": titles_expected,
        "workspace_reachable": workspace_reachable,
        "checks": [item.model_dump(mode="json") for item in checks],
        "findings": [item.model_dump(mode="json") for item in ordered],
        "failed_checks": [item.value for item in failed],
        "skipped_checks": [item.value for item in skipped],
        "status": status.value,
    }
    report = ReleaseVerificationReport.model_validate(
        {
            **payload,
            "content_sha256": report_content_address(payload),
            "created_at": clock(),
        }
    )
    return ReleaseVerification(report=report, release_path=resolved)


def _invalid_manifest_verdict(
    release_path: Path,
    *,
    project_root: Path,
    manifest_sha256: str,
    now: Callable[[], datetime],
) -> ReleaseVerification:
    """Report an unreadable manifest as a failed manifest check, not as an exception.

    Nothing downstream of the index can be measured against an index that does not parse, so every
    other check is *skipped* rather than passed — a release whose inventory is invalid has been
    verified in exactly one respect, and the report says so.
    """
    try:
        relative = Path(release_path.relative_to(project_root).as_posix())
    except ValueError:
        relative = Path(release_path.name)
    finding = ReleaseVerificationFinding(
        check=ReleaseCheck.MANIFEST,
        code="invalid_manifest_schema",
        severity="error",
        message="the release manifest does not satisfy its own contract",
    )
    checks = [
        ReleaseVerificationCheck(
            check=check,
            status=(
                ReleaseCheckStatus.FAIL
                if check is ReleaseCheck.MANIFEST
                else ReleaseCheckStatus.SKIPPED
            ),
            examined=0,
            expected=0,
            detail=(
                "the manifest could not be read as a release index"
                if check is ReleaseCheck.MANIFEST
                else "nothing can be measured against an invalid manifest"
            ),
            finding_codes=[finding.code] if check is ReleaseCheck.MANIFEST else [],
        )
        for check in RELEASE_CHECK_ORDER
    ]
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "release_verification_engine_version": RELEASE_VERIFICATION_ENGINE_VERSION,
        "release_id": release_path.name,
        "release_path": relative.as_posix(),
        "manifest_sha256": manifest_sha256,
        "files_verified": 0,
        "files_expected": 0,
        "chapters_verified": 0,
        "chapters_expected": 0,
        "chunks_verified": 0,
        "chunks_expected": 0,
        "titles_verified": 0,
        "titles_expected": 0,
        "workspace_reachable": ProjectPaths(project_root).workspace.is_dir(),
        "checks": [item.model_dump(mode="json") for item in checks],
        "findings": [finding.model_dump(mode="json")],
        "failed_checks": [ReleaseCheck.MANIFEST.value],
        "skipped_checks": [
            check.value for check in RELEASE_CHECK_ORDER if check is not ReleaseCheck.MANIFEST
        ],
        "status": ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION.value,
    }
    report = ReleaseVerificationReport.model_validate(
        {**payload, "content_sha256": report_content_address(payload), "created_at": now()}
    )
    return ReleaseVerification(report=report, release_path=release_path)


def serialize_verification(report: ReleaseVerificationReport, *, as_json: bool = False) -> str:
    """Render one verdict deterministically, in declared field order."""
    payload = report.model_dump(mode="json", exclude_none=False)
    if as_json:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    rendered = yaml.safe_dump(
        payload, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    if not isinstance(rendered, str):
        raise ReleaseVerificationError("could not serialize the verification report")
    return rendered


def write_verification_report(
    verification: ReleaseVerification, output: str | Path, *, project_root: Path
) -> Path:
    """Write the verdict where an operator asked for it — never into the release itself.

    A release is content-addressed by its own manifest. Dropping a report inside it would make
    the next verification fail on an unindexed file, so the one place this stage may not write is
    the directory it just judged.
    """
    project_root = project_root.resolve()
    candidate = Path(output)
    resolved = (candidate if candidate.is_absolute() else project_root / candidate).resolve()
    release = verification.release_path.resolve()
    if resolved == release or release in resolved.parents:
        raise ReleaseVerificationError(
            "a verification report may not be written inside the release it verifies"
        )
    content = serialize_verification(
        verification.report, as_json=resolved.suffix.lower() == ".json"
    )
    try:
        atomic_write_text(resolved, content, overwrite=True)
    except OSError as exc:
        raise ReleaseVerificationError(f"could not write {resolved.name}") from exc
    return resolved
