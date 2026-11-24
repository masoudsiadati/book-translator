import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.book_metadata import (
    OperatorAssertion,
    execute_book_metadata,
    plan_book_metadata,
)
from book_translator.cli import app
from book_translator.models import (
    BookMetadataProvenance,
    ChapterMetadata,
    ChapterStatus,
    ChunkMetadata,
    ChunkStatus,
    DecisionSource,
    SnapshotBookReference,
    SnapshotChapterReference,
    SnapshotChunkReference,
    SnapshotResourceKind,
    SnapshotResourceReference,
    TerminologyDiscoveryEvidence,
    TerminologyDiscoveryResponse,
    TerminologyProposal,
    TerminologyProposalResponse,
    TerminologyRegistry,
    TerminologyReview,
    TerminologyReviewDisposition,
    TerminologyReviewResponse,
    TerminologyType,
    TocDocument,
    TocEntry,
    TocKind,
    TranslationManifest,
    TranslationSnapshotStatus,
)
from book_translator.status_transitions import can_transition_translation_snapshot
from book_translator.terminology import (
    FakeTerminologyProvider,
    TerminologyAnalysisError,
    TerminologyProviderError,
    proposal_identity,
    save_registry,
    set_decision,
)
from book_translator.terminology.workflow import (
    TerminologyWorkflowError,
    audit_proposal_run,
    audit_review_run,
    execute_discovery,
    execute_proposals,
    execute_reviews,
    load_discovery_output,
    load_proposal_output,
    plan_discovery,
    plan_proposals,
    plan_reviews,
)
from book_translator.translation_snapshot import (
    ManifestIdentityFormat,
    SnapshotError,
    canonical_manifest_bytes,
    create_snapshot,
    list_snapshots,
    manifest_identity,
    persisted_manifest_identity,
    persisted_manifest_identity_format,
    plan_snapshot,
    validate_snapshot,
)
from book_translator.translation_snapshot.workflow import _selector_id
from tests.test_pdf import create_pdf

NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
runner = CliRunner()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _resource(root: Path, stem: str, kind: str, content: str = "synthetic\n") -> None:
    suffix = ".yaml" if stem == "glossary" else ".md"
    resource = root / f"config/{stem}{suffix}"
    resource.write_text(content, encoding="utf-8")
    _write_yaml(
        resource.with_suffix(".metadata.yaml"),
        {
            "schema_version": "1.0",
            "resource_kind": kind,
            "resource_file": resource.relative_to(root).as_posix(),
            "version": "1.0.0",
        },
    )


def _chapter(root: Path, chapter_id: str, number: int, title: str = "Synthetic") -> None:
    directory = root / f"workspace/chapters/{chapter_id}"
    chunks = directory / "chunks/001"
    chunks.mkdir(parents=True)
    cleaned = "Alpha beta.\n\nGamma delta.\n"
    source = cleaned
    context = ""
    cleaned_path = directory / "cleaned.md"
    source_path = chunks / "source.md"
    context_path = chunks / "context.md"
    cleaned_path.write_text(cleaned, encoding="utf-8")
    source_path.write_text(source, encoding="utf-8")
    context_path.write_text(context, encoding="utf-8")
    cleaned_hash = _hash_bytes(cleaned.encode())
    chunk = ChunkMetadata(
        schema_version="1.0",
        chunk_id=f"{chapter_id}-001",
        chapter_id=chapter_id,
        sequence=1,
        source_file=source_path.relative_to(root),
        source_sha256=cleaned_hash,
        context_file=context_path.relative_to(root),
        context_sha256=_hash_bytes(b""),
        context_word_count=0,
        translation_file=(chunks / "translation.md").relative_to(root),
        source_word_count=4,
        source_paragraph_count=2,
        status=ChunkStatus.PENDING,
        chunk_engine_version="1.0.0",
        cleaned_source_sha256=cleaned_hash,
        created_at=NOW,
        updated_at=NOW,
    )
    _write_yaml(chunks / "metadata.yaml", chunk)
    metadata = ChapterMetadata(
        schema_version="1.0",
        chapter_id=chapter_id,
        kind=TocKind.CHAPTER,
        number=number,
        title_en=title,
        source_start_page=number,
        source_end_page=number,
        source_file=Path(f"workspace/chapters/{chapter_id}/source.md"),
        source_sha256="a" * 64,
        cleaned_file=cleaned_path.relative_to(root),
        cleaned_sha256=cleaned_hash,
        cleaned_word_count=4,
        cleaned_paragraph_count=2,
        chunk_count=1,
        status=ChapterStatus.CHUNKED,
        extraction_version="1.0",
        normalization_version="1.0",
        chunk_engine_version="1.0.0",
        created_at=NOW,
        updated_at=NOW,
    )
    _write_yaml(directory / "metadata.yaml", metadata)
    _write_yaml(
        directory / "chunking-report.yaml",
        {
            "chapter_id": chapter_id,
            "cleaned_sha256": cleaned_hash,
            "chunk_engine_version": "1.0.0",
            "chunk_count": 1,
            "conservation_validation": True,
            "reconstructed_sha256": cleaned_hash,
        },
    )


def synthetic_project(root: Path, *, chapters: int = 1) -> None:
    (root / "config").mkdir()
    (root / "input").mkdir()
    pdf = root / "input/book.pdf"
    pdf.write_bytes(b"synthetic-pdf")
    pdf_hash = _hash_bytes(pdf.read_bytes())
    (root / "config/book.yaml").write_text(
        """book_id: synthetic-book
title_en: Synthetic Book
title_fa: کتاب ساختگی
author: Author
source_language: en
target_language: fa
pdf_path: input/book.pdf
chunk_target_words: 10
chunk_max_words: 20
previous_context_words: 2
output_format: epub3
""",
        encoding="utf-8",
    )
    _resource(
        root,
        "translation-prompt",
        "prompt",
        (
            "Chapter {{ chapter_number }} part {{ part_number }}\n"
            "Glossary: {{ glossary }}\nContext: {{ previous_context }}\n"
            "Source: {{ source_text }}\n"
        ),
    )
    _resource(
        root,
        "glossary",
        "glossary",
        "terms: {}\ncharacters: {}\nplaces: {}\ngroups: {}\nunresolved_terms: []\n",
    )
    _resource(root, "style-guide", "style_guide", "Synthetic style guide.\n")
    entries = []
    for number in range(1, chapters + 1):
        chapter_id = f"{number:03d}"
        entries.append(
            TocEntry(
                entry_id=f"chapter-{chapter_id}",
                level=1,
                kind=TocKind.CHAPTER,
                number=number,
                title_en=f"Chapter {number}",
                start_page=number,
                end_page=number,
                source_toc_title=f"Chapter {number}",
            )
        )
        _chapter(root, chapter_id, number)
    toc = TocDocument(
        schema_version="1.0",
        source="embedded",
        source_pdf=Path("input/book.pdf"),
        source_pdf_sha256=pdf_hash,
        pdf_page_count=chapters,
        entry_count=chapters,
        entries=entries,
    )
    _write_yaml(root / "workspace/toc.yaml", toc)
    (root / "state").mkdir()
    (root / "state/progress.json").write_text(
        json.dumps(
            {
                "detected_chapters": [],
                "translated_chapters": [],
                "approved_chapters": [],
                "failed_chunks": [],
            }
        ),
        encoding="utf-8",
    )


def synthetic_epub_project(root: Path) -> None:
    synthetic_project(root)
    epub = root / "input/book.epub"
    epub.write_bytes(b"synthetic-epub")
    config = (root / "config/book.yaml").read_text(encoding="utf-8")
    (root / "config/book.yaml").write_text(
        config.replace("pdf_path: input/book.pdf", "epub_path: input/book.epub"),
        encoding="utf-8",
    )
    toc = TocDocument.model_validate(
        yaml.safe_load((root / "workspace/toc.yaml").read_text(encoding="utf-8"))
    )
    _write_yaml(
        root / "workspace/toc.yaml",
        toc.model_copy(
            update={
                "source": "epub_spine",
                "source_pdf": None,
                "source_pdf_sha256": None,
                "pdf_page_count": None,
                "source_document": Path("input/book.epub"),
                "source_document_sha256": _hash_bytes(epub.read_bytes()),
                "source_item_count": 1,
            }
        ),
    )


def test_epub_snapshot_records_generic_source_document_lineage(tmp_path: Path) -> None:
    synthetic_epub_project(tmp_path)

    plan = plan_snapshot(project_root=tmp_path)

    assert plan.manifest.book.source_pdf is None
    assert plan.manifest.book.source_document == Path("input/book.epub")
    assert plan.manifest.book.source_document_sha256 == _hash_bytes(b"synthetic-epub")
    assert plan.manifest.book.source_format == "epub"


def sample_manifest() -> TranslationManifest:
    def resource(kind: SnapshotResourceKind, letter: str) -> SnapshotResourceReference:
        return SnapshotResourceReference(
            resource_kind=kind,
            file=Path(f"config/{letter}.md"),
            sha256=letter * 64,
            version="1.0.0",
        )

    chunk = SnapshotChunkReference(
        chunk_id="001-001",
        chapter_id="001",
        sequence=1,
        source_file=Path("workspace/chapters/001/chunks/001/source.md"),
        source_sha256="d" * 64,
        source_word_count=2,
        source_paragraph_count=1,
        context_file=Path("workspace/chapters/001/chunks/001/context.md"),
        context_sha256="e" * 64,
        context_word_count=0,
        oversize=False,
    )
    chapter = SnapshotChapterReference(
        chapter_id="001",
        kind=TocKind.CHAPTER,
        number=1,
        title_en="One",
        title_fa="یک",
        cleaned_file=Path("workspace/chapters/001/cleaned.md"),
        cleaned_sha256="c" * 64,
        normalization_version="1.0",
        chunk_engine_version="1.0.0",
        chunk_count=1,
        chunks=[chunk],
    )
    return TranslationManifest(
        schema_version="1.0",
        workflow="document-translation",
        selection_mode="subset",
        book=SnapshotBookReference(
            book_id="book",
            title_en="Book",
            title_fa="کتاب",
            source_language="en",
            target_language="fa",
            source_pdf=Path("input/book.pdf"),
            source_pdf_sha256="a" * 64,
            toc_file=Path("workspace/toc.yaml"),
            toc_sha256="b" * 64,
        ),
        chapters=[chapter],
        prompt=resource(SnapshotResourceKind.PROMPT, "f"),
        glossary=resource(SnapshotResourceKind.GLOSSARY, "1"),
        style_guide=resource(SnapshotResourceKind.STYLE_GUIDE, "2"),
    )


def test_canonical_identity_is_deterministic_and_unicode_preserving() -> None:
    manifest = sample_manifest()
    restored = TranslationManifest.model_validate_json(manifest.model_dump_json())

    assert canonical_manifest_bytes(manifest) == canonical_manifest_bytes(restored)
    assert manifest_identity(manifest) == manifest_identity(restored)
    assert "کتاب".encode() in canonical_manifest_bytes(manifest)


def test_snapshot_status_transition_policy_is_frozen() -> None:
    assert can_transition_translation_snapshot(
        TranslationSnapshotStatus.DRAFT, TranslationSnapshotStatus.VALIDATED
    )
    assert can_transition_translation_snapshot(
        TranslationSnapshotStatus.VALIDATED, TranslationSnapshotStatus.FROZEN
    )
    assert not can_transition_translation_snapshot(
        TranslationSnapshotStatus.FROZEN, TranslationSnapshotStatus.DRAFT
    )


def test_persisted_canonical_prologue_id_is_a_valid_selector() -> None:
    assert _selector_id("000-prologue") == "000-prologue"


@pytest.mark.parametrize("field", ["prompt", "glossary", "style_guide"])
def test_material_resource_hash_changes_identity(field: str) -> None:
    manifest = sample_manifest()
    changed_resource = getattr(manifest, field).model_copy(update={"sha256": "9" * 64})
    changed = manifest.model_copy(update={field: changed_resource})

    assert manifest_identity(manifest) != manifest_identity(changed)


def test_resource_version_changes_identity() -> None:
    manifest = sample_manifest()
    changed = manifest.model_copy(
        update={"prompt": manifest.prompt.model_copy(update={"version": "1.1.0"})}
    )

    assert manifest_identity(manifest) != manifest_identity(changed)


def test_terminology_registry_affects_new_identity_without_rewriting_historical_manifest() -> None:
    historical = sample_manifest()
    terminology = SnapshotResourceReference(
        resource_kind=SnapshotResourceKind.TERMINOLOGY,
        file=Path("config/terminology.yaml"),
        sha256="9" * 64,
        version="1.0.0",
    )
    current = historical.model_copy(update={"terminology": terminology})
    changed = current.model_copy(
        update={"terminology": terminology.model_copy(update={"sha256": "8" * 64})}
    )

    assert "terminology" not in canonical_manifest_bytes(historical).decode()
    assert manifest_identity(historical) != manifest_identity(current)
    assert manifest_identity(current) != manifest_identity(changed)


def test_legacy_terminology_reference_identity_omits_absent_frozen_file() -> None:
    terminology = SnapshotResourceReference(
        resource_kind=SnapshotResourceKind.TERMINOLOGY,
        file=Path("config/terminology.yaml"),
        sha256="9" * 64,
        version="1.0.0",
    )
    manifest = sample_manifest().model_copy(update={"terminology": terminology})
    payload = json.loads(canonical_manifest_bytes(manifest))

    assert "frozen_file" not in payload["terminology"]


@pytest.mark.parametrize(
    "snapshot_id",
    [
        "translation-fa-e0de165b014572f45a784847",
        "translation-fa-00e997ab2fc3e8f859794d15",
        "translation-fa-5841fd98484dbcbe7908655e",
        "translation-fa-401467944cfb5347754fcae1",
    ],
)
def test_existing_legacy_snapshot_identity_is_history_compatible(snapshot_id: str) -> None:
    snapshot_root = Path(__file__).parents[1] / "workspace/snapshots/translation" / snapshot_id
    raw_manifest = yaml.safe_load((snapshot_root / "manifest.yaml").read_text(encoding="utf-8"))
    snapshot = yaml.safe_load((snapshot_root / "snapshot.yaml").read_text(encoding="utf-8"))
    manifest = TranslationManifest.model_validate(raw_manifest)

    derived_id, derived_sha = persisted_manifest_identity(raw_manifest, manifest)

    assert persisted_manifest_identity_format(raw_manifest) is ManifestIdentityFormat.LEGACY_V1
    assert derived_id == snapshot_id
    assert derived_sha == snapshot["manifest_sha256"]


def test_modified_legacy_manifest_identity_fails(tmp_path: Path) -> None:
    snapshot_id = "translation-fa-401467944cfb5347754fcae1"
    source_root = Path(__file__).parents[1] / "workspace/snapshots/translation" / snapshot_id
    snapshot_root = tmp_path / "workspace/snapshots/translation" / snapshot_id
    shutil.copytree(source_root, snapshot_root)
    raw_manifest = yaml.safe_load((snapshot_root / "manifest.yaml").read_text(encoding="utf-8"))
    raw_manifest["workflow"] = "modified-workflow"
    _write_yaml(snapshot_root / "manifest.yaml", raw_manifest)

    validation = validate_snapshot(snapshot_root, project_root=tmp_path, now=lambda: NOW)

    assert not validation.report.valid
    assert "snapshot identity mismatch" in validation.report.errors
    assert "manifest hash mismatch" in validation.report.errors


def test_duplicate_chapter_and_chunk_ids_are_rejected() -> None:
    manifest = sample_manifest()
    with pytest.raises(ValueError, match="chapter IDs"):
        TranslationManifest.model_validate(
            {**manifest.model_dump(), "chapters": [manifest.chapters[0], manifest.chapters[0]]}
        )
    duplicate_chunk = manifest.chapters[0].chunks[0].model_copy(update={"chapter_id": "002"})
    duplicate = manifest.chapters[0].model_copy(
        update={"chapter_id": "002", "chunks": [duplicate_chunk]}
    )
    with pytest.raises(ValueError, match="chunk IDs"):
        TranslationManifest.model_validate(
            {**manifest.model_dump(), "chapters": [manifest.chapters[0], duplicate]}
        )


def test_chunk_sequence_gap_is_rejected() -> None:
    chapter = sample_manifest().chapters[0]
    broken = chapter.chunks[0].model_copy(update={"sequence": 2})
    with pytest.raises(ValueError, match="contiguous"):
        chapter.model_copy(update={"chunks": [broken]}).model_validate(
            {**chapter.model_dump(), "chunks": [broken.model_dump()]}
        )


def test_full_snapshot_publication_validation_and_skip(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    plan = plan_snapshot(project_root=tmp_path)
    result = create_snapshot(plan, now=lambda: NOW)
    validation = validate_snapshot(result.path, project_root=tmp_path, now=lambda: NOW)
    repeated = plan_snapshot(project_root=tmp_path, label="different")

    assert not result.skipped
    assert validation.report.valid
    assert repeated.action == "skip"
    assert repeated.snapshot_id == result.snapshot_id
    assert repeated.warnings
    assert create_snapshot(repeated, now=lambda: NOW).skipped


def test_snapshot_freezes_exact_terminology_bytes_for_historical_qa(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    terminology = tmp_path / "config/terminology.yaml"
    terminology.write_text(
        "schema_version: '1.0'\nregistry_kind: terminology\nversion: 1.0.0\n"
        "book_id: synthetic-book\nsource_language: en\ntarget_language: fa\n"
        "candidates: []\ndecisions: []\n",
        encoding="utf-8",
    )
    _write_yaml(
        tmp_path / "config/terminology.metadata.yaml",
        {
            "schema_version": "1.0",
            "resource_kind": "terminology",
            "resource_file": "config/terminology.yaml",
            "version": "1.0.0",
        },
    )
    original = terminology.read_bytes()
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    manifest = TranslationManifest.model_validate(
        yaml.safe_load((snapshot.path / "manifest.yaml").read_text(encoding="utf-8"))
    )
    assert manifest.terminology is not None and manifest.terminology.frozen_file is not None
    frozen = tmp_path / manifest.terminology.frozen_file
    assert frozen.read_bytes() == original

    terminology.write_text("changed: true\n", encoding="utf-8")
    validation = validate_snapshot(snapshot.path, project_root=tmp_path, now=lambda: NOW)
    assert validation.report.valid
    assert not validation.lineage_current
    assert frozen.read_bytes() == original


def test_new_snapshot_missing_frozen_terminology_bytes_fails(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    terminology = tmp_path / "config/terminology.yaml"
    terminology.write_text("synthetic: terminology\n", encoding="utf-8")
    _write_yaml(
        tmp_path / "config/terminology.metadata.yaml",
        {
            "schema_version": "1.0",
            "resource_kind": "terminology",
            "resource_file": "config/terminology.yaml",
            "version": "1.0.0",
        },
    )
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    manifest_path = snapshot.path / "manifest.yaml"
    raw_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert (
        persisted_manifest_identity_format(raw_manifest)
        is ManifestIdentityFormat.FROZEN_RESOURCES_V1
    )
    frozen = tmp_path / raw_manifest["terminology"]["frozen_file"]
    frozen.unlink()

    validation = validate_snapshot(snapshot.path, project_root=tmp_path, now=lambda: NOW)

    assert not validation.report.valid
    assert "frozen terminology resource is missing or stale" in validation.report.errors


def test_modified_new_manifest_identity_fails(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    terminology = tmp_path / "config/terminology.yaml"
    terminology.write_text("synthetic: terminology\n", encoding="utf-8")
    _write_yaml(
        tmp_path / "config/terminology.metadata.yaml",
        {
            "schema_version": "1.0",
            "resource_kind": "terminology",
            "resource_file": "config/terminology.yaml",
            "version": "1.0.0",
        },
    )
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    manifest_path = snapshot.path / "manifest.yaml"
    raw_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    raw_manifest["workflow"] = "modified-workflow"
    _write_yaml(manifest_path, raw_manifest)

    validation = validate_snapshot(snapshot.path, project_root=tmp_path, now=lambda: NOW)

    assert not validation.report.valid
    assert "snapshot identity mismatch" in validation.report.errors
    assert "manifest hash mismatch" in validation.report.errors


def test_subset_is_canonicalized_and_label_excluded_from_identity(tmp_path: Path) -> None:
    synthetic_project(tmp_path, chapters=2)
    first = plan_snapshot(project_root=tmp_path, selectors=["2", "1"], label="one")
    second = plan_snapshot(project_root=tmp_path, selectors=["chapter-001", "002"], label="two")

    assert [chapter.chapter_id for chapter in first.manifest.chapters] == ["001", "002"]
    assert first.snapshot_id == second.snapshot_id


def test_incomplete_full_book_refuses_before_writing(tmp_path: Path) -> None:
    synthetic_project(tmp_path, chapters=2)
    (tmp_path / "workspace/chapters/002/metadata.yaml").unlink()

    with pytest.raises(SnapshotError, match="invalid metadata"):
        plan_snapshot(project_root=tmp_path)
    assert not (tmp_path / "workspace/snapshots").exists()


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("workspace/chapters/001/cleaned.md", "stale cleaned"),
        ("workspace/chapters/001/chunks/001/source.md", "stale source"),
        ("workspace/chapters/001/chunks/001/context.md", "stale context"),
    ],
)
def test_stale_artifact_hash_is_rejected(tmp_path: Path, relative: str, message: str) -> None:
    synthetic_project(tmp_path)
    (tmp_path / relative).write_text("changed\n", encoding="utf-8")

    with pytest.raises(SnapshotError, match=message):
        plan_snapshot(project_root=tmp_path)


def test_nonpending_chunk_and_translation_file_are_rejected(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    metadata_path = tmp_path / "workspace/chapters/001/chunks/001/metadata.yaml"
    value = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    value["status"] = "translated"
    _write_yaml(metadata_path, value)
    with pytest.raises(SnapshotError, match="not pending"):
        plan_snapshot(project_root=tmp_path)
    value["status"] = "pending"
    _write_yaml(metadata_path, value)
    (metadata_path.parent / "translation.md").write_text("synthetic\n", encoding="utf-8")
    with pytest.raises(SnapshotError, match="translation artifact"):
        plan_snapshot(project_root=tmp_path)


def test_inconsistent_chunk_counts_are_rejected(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    metadata_path = tmp_path / "workspace/chapters/001/chunks/001/metadata.yaml"
    value = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    value["source_word_count"] = 99
    _write_yaml(metadata_path, value)

    with pytest.raises(SnapshotError, match="structural counts"):
        plan_snapshot(project_root=tmp_path)


def test_missing_chunk_and_invalid_conservation_are_rejected(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    chunk_directory = tmp_path / "workspace/chapters/001/chunks/001"
    renamed = chunk_directory.with_name("002")
    chunk_directory.rename(renamed)
    with pytest.raises(SnapshotError, match="incomplete chunk sequence"):
        plan_snapshot(project_root=tmp_path)
    renamed.rename(chunk_directory)
    report_path = tmp_path / "workspace/chapters/001/chunking-report.yaml"
    report = yaml.safe_load(report_path.read_text(encoding="utf-8"))
    report["conservation_validation"] = False
    _write_yaml(report_path, report)
    with pytest.raises(SnapshotError, match="conservation"):
        plan_snapshot(project_root=tmp_path)


def test_missing_or_invalid_resource_version_is_rejected(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    metadata = tmp_path / "config/translation-prompt.metadata.yaml"
    metadata.unlink()
    with pytest.raises(SnapshotError, match="invalid translation-prompt.metadata.yaml"):
        plan_snapshot(project_root=tmp_path)
    _write_yaml(
        metadata,
        {
            "schema_version": "1.0",
            "resource_kind": "prompt",
            "resource_file": "config/translation-prompt.md",
            "version": "latest",
        },
    )
    with pytest.raises(SnapshotError, match="invalid translation-prompt.metadata.yaml"):
        plan_snapshot(project_root=tmp_path)


def test_dry_plan_and_empty_list_write_nothing(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    plan = plan_snapshot(project_root=tmp_path)

    assert plan.action == "create"
    assert list_snapshots(project_root=tmp_path) == ()
    assert not (tmp_path / "workspace/snapshots").exists()


def test_cli_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synthetic_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["create-translation-snapshot", "--dry-run"])

    assert result.exit_code == 0
    assert not (tmp_path / "workspace/snapshots").exists()


def test_validation_distinguishes_historical_integrity_from_current_lineage(
    tmp_path: Path,
) -> None:
    synthetic_project(tmp_path)
    result = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    (tmp_path / "config/glossary.yaml").write_text("changed\n", encoding="utf-8")

    validation = validate_snapshot(result.path, project_root=tmp_path, now=lambda: NOW)

    assert validation.report.valid
    assert not validation.lineage_current
    assert validation.lineage_warnings


def test_cli_reports_historically_valid_stale_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synthetic_project(tmp_path)
    result = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    (tmp_path / "config/glossary.yaml").write_text("changed\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cli_result = runner.invoke(app, ["validate-translation-snapshot", result.snapshot_id])

    assert cli_result.exit_code == 0
    assert "Historically valid" in cli_result.stdout and "yes" in cli_result.stdout
    assert "Current lineage" in cli_result.stdout and "no" in cli_result.stdout


def test_adding_current_terminology_makes_old_snapshot_historical(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    result = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    terminology = tmp_path / "config/terminology.yaml"
    terminology.write_text(
        "schema_version: '1.0'\n"
        "registry_kind: terminology\n"
        "version: 1.0.0\n"
        "book_id: synthetic-book\n"
        "source_language: en\n"
        "target_language: fa\n"
        "candidates: []\n"
        "decisions: []\n",
        encoding="utf-8",
    )
    _write_yaml(
        tmp_path / "config/terminology.metadata.yaml",
        {
            "schema_version": "1.0",
            "resource_kind": "terminology",
            "resource_file": "config/terminology.yaml",
            "version": "1.0.0",
        },
    )

    validation = validate_snapshot(result.path, project_root=tmp_path, now=lambda: NOW)
    assert validation.report.valid
    assert not validation.lineage_current
    assert plan_snapshot(project_root=tmp_path).snapshot_id != result.snapshot_id


def test_immutable_discovery_and_proposal_runs_are_incremental_and_idempotent(
    tmp_path: Path,
) -> None:
    synthetic_project(tmp_path, chapters=2)
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    current = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )
    save_registry(tmp_path / "config/terminology.yaml", current)
    discovery_response = TerminologyDiscoveryResponse(
        schema_version="1.0",
        candidates=[
            TerminologyDiscoveryEvidence(
                source_term="Alpha",
                aliases=[],
                classification=TerminologyType.PERSON,
                confidence=0.95,
                reason="synthetic proper name",
            )
        ],
    )
    discovery_plan = plan_discovery(
        snapshot.snapshot_id,
        project_root=tmp_path,
        selectors=["1", "2"],
    )
    discovery = execute_discovery(
        discovery_plan,
        FakeTerminologyProvider(discovery_response=discovery_response),
        now=lambda: NOW,
    )
    _, candidates = load_discovery_output(discovery_plan.output_directory)
    assert discovery.status.value == "validated"
    assert len(candidates) == 1
    assert [item.chapter_id for item in candidates[0].occurrences] == ["001", "002"]
    repeated = plan_discovery(
        snapshot.snapshot_id,
        project_root=tmp_path,
        selectors=["001", "002"],
    )
    assert repeated.run_id == discovery_plan.run_id and repeated.action == "skip"

    proposed_target = "آلفا"
    proposal_response = TerminologyProposalResponse(
        schema_version="1.0",
        proposals=[
            TerminologyProposal(
                proposal_id=proposal_identity(candidates[0].candidate_id, proposed_target, "fa"),
                candidate_id=candidates[0].candidate_id,
                source_term="Alpha",
                proposed_target=proposed_target,
                confidence=0.96,
                classification=TerminologyType.PERSON,
            )
        ],
    )
    proposal_plan = plan_proposals(discovery.run_id, project_root=tmp_path)
    proposal_run = execute_proposals(
        proposal_plan,
        FakeTerminologyProvider(proposal_response=proposal_response),
        now=lambda: NOW,
    )
    _, proposals = load_proposal_output(proposal_plan.output_directory)
    assert proposal_run.discovery_run_id == discovery.run_id
    assert proposals[0].proposed_target == proposed_target
    before_audit = {
        path.name: path.read_bytes() for path in proposal_plan.output_directory.iterdir()
    }
    audit_proposal_run(proposal_plan.output_directory, project_root=tmp_path)
    assert before_audit == {
        path.name: path.read_bytes() for path in proposal_plan.output_directory.iterdir()
    }
    assert validate_snapshot(snapshot.path, project_root=tmp_path).report.valid


def test_proposal_plan_reuses_exact_legacy_and_registry_canonical_terms(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    registry = set_decision(
        TerminologyRegistry(
            schema_version="1.0",
            version="1.0.0",
            book_id="synthetic-book",
            source_language="en",
            target_language="fa",
        ),
        source_term="Gamma",
        target_term="RegistryTarget",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    save_registry(tmp_path / "config/terminology.yaml", registry)
    _write_yaml(
        tmp_path / "config/terminology.metadata.yaml",
        {
            "schema_version": "1.0",
            "resource_kind": "terminology",
            "resource_file": "config/terminology.yaml",
            "version": "1.0.0",
        },
    )
    _write_yaml(
        tmp_path / "config/glossary.yaml",
        {
            "terms": {"Alpha": "LegacyTarget"},
            "characters": {},
            "places": {},
            "groups": {},
            "unresolved_terms": [],
        },
    )
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    discovery_response = TerminologyDiscoveryResponse(
        schema_version="1.0",
        candidates=[
            TerminologyDiscoveryEvidence(
                source_term=source,
                classification=TerminologyType.PERSON,
                confidence=0.95,
                reason="synthetic name",
            )
            for source in ("Alpha", "Gamma")
        ],
    )
    discovery_plan = plan_discovery(snapshot.snapshot_id, project_root=tmp_path)
    discovery = execute_discovery(
        discovery_plan,
        FakeTerminologyProvider(discovery_response=discovery_response),
        now=lambda: NOW,
    )
    plan = plan_proposals(discovery.run_id, project_root=tmp_path)
    assert plan.candidates == ()
    assert {item.proposed_target for item in plan.reused_proposals} == {
        "LegacyTarget",
        "RegistryTarget",
    }
    assert all(item.action == "reuse_existing" for item in plan.reused_proposals)
    result = execute_proposals(
        plan, FakeTerminologyProvider("non_retryable_failure"), now=lambda: NOW
    )
    assert result.contacted is False


def test_discovery_cli_dry_run_selects_chapter_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synthetic_project(tmp_path, chapters=2)
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "discover-terminology",
            snapshot.snapshot_id,
            "--chapter",
            "2",
            "--chunk",
            "002-001",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "Selected chunks: 1" in result.output
    assert not (tmp_path / "workspace/terminology-runs").exists()


def test_ambiguous_discovery_blocks_automatic_replay(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    plan = plan_discovery(snapshot.snapshot_id, project_root=tmp_path)

    with pytest.raises(TerminologyProviderError, match="synthetic ambiguous failure"):
        execute_discovery(plan, FakeTerminologyProvider("ambiguous_failure"), now=lambda: NOW)
    with pytest.raises(TerminologyWorkflowError, match="blocks automatic replay"):
        execute_discovery(plan, FakeTerminologyProvider(), now=lambda: NOW)


def test_provider_contacted_validation_failure_is_auditable_and_requires_new_retry_id(
    tmp_path: Path,
) -> None:
    synthetic_project(tmp_path)
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    registry_path = tmp_path / "config/terminology.yaml"
    save_registry(
        registry_path,
        TerminologyRegistry(
            schema_version="1.0",
            version="1.0.0",
            book_id="synthetic-book",
            source_language="en",
            target_language="fa",
        ),
    )
    registry_before = registry_path.read_bytes()
    manifest_before = (snapshot.path / "manifest.yaml").read_bytes()
    plan = plan_discovery(snapshot.snapshot_id, project_root=tmp_path)
    hallucinated = TerminologyDiscoveryResponse(
        schema_version="1.0",
        candidates=[
            TerminologyDiscoveryEvidence(
                source_term="AbsentSyntheticName",
                classification=TerminologyType.PERSON,
                confidence=0.99,
                reason="synthetic invalid evidence",
            )
        ],
    )

    with pytest.raises(TerminologyAnalysisError, match="absent from selected source"):
        execute_discovery(
            plan,
            FakeTerminologyProvider(discovery_response=hallucinated),
            now=lambda: NOW,
        )

    marker = plan.output_root / f".{plan.run_id}.requesting.json"
    assert marker.is_file()
    run_data = yaml.safe_load((plan.output_directory / "run.yaml").read_text(encoding="utf-8"))
    output_data = yaml.safe_load(
        (plan.output_directory / "output.yaml").read_text(encoding="utf-8")
    )
    assert run_data["status"] == "failed"
    assert run_data["contacted"] is True
    assert output_data["candidates"][0]["source_term"] == "AbsentSyntheticName"
    assert registry_path.read_bytes() == registry_before
    assert (snapshot.path / "manifest.yaml").read_bytes() == manifest_before

    repeated = plan_discovery(snapshot.snapshot_id, project_root=tmp_path)
    assert repeated.run_id == plan.run_id
    assert repeated.action == "skip"
    retry = plan_discovery(snapshot.snapshot_id, project_root=tmp_path, retry_of=plan.run_id)
    assert retry.run_id != plan.run_id
    assert retry.retry_of == plan.run_id
    assert retry.action == "create"


def test_review_run_is_immutable_deterministic_and_failure_durable(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    save_registry(
        tmp_path / "config/terminology.yaml",
        TerminologyRegistry(
            schema_version="1.0",
            version="1.0.0",
            book_id="synthetic-book",
            source_language="en",
            target_language="fa",
        ),
    )
    discovery_plan = plan_discovery(snapshot.snapshot_id, project_root=tmp_path)
    discovery = execute_discovery(
        discovery_plan,
        FakeTerminologyProvider(
            discovery_response=TerminologyDiscoveryResponse(
                schema_version="1.0",
                candidates=[
                    TerminologyDiscoveryEvidence(
                        source_term="Alpha",
                        classification=TerminologyType.PERSON,
                        confidence=0.96,
                        reason="synthetic name",
                    )
                ],
            )
        ),
        now=lambda: NOW,
    )
    _, candidates = load_discovery_output(discovery_plan.output_directory)
    proposed = TerminologyProposal(
        proposal_id=proposal_identity(candidates[0].candidate_id, "آلفا", "fa"),
        candidate_id=candidates[0].candidate_id,
        source_term="Alpha",
        proposed_target="آلفا",
        confidence=0.97,
        classification=TerminologyType.PERSON,
    )
    proposal_plan = plan_proposals(discovery.run_id, project_root=tmp_path)
    proposal_run = execute_proposals(
        proposal_plan,
        FakeTerminologyProvider(
            proposal_response=TerminologyProposalResponse(
                schema_version="1.0", proposals=[proposed]
            )
        ),
        now=lambda: NOW,
    )
    review_value = TerminologyReview(
        review_id="tr-" + "0" * 24,
        proposal_id=proposed.proposal_id,
        disposition=TerminologyReviewDisposition.ACCEPT,
        reviewed_target="آلفا",
        confidence=0.95,
        meaning_fidelity=0.9,
        target_naturalness=0.9,
        transliteration_stability=0.9,
        terminology_consistency=0.9,
        register_appropriateness=0.9,
        morphology_suitability=0.9,
        ambiguity_safety=0.9,
        reason_codes=["synthetic_accept"],
        notes=[],
    )
    review_plan = plan_reviews(proposal_run.run_id, project_root=tmp_path)
    repeated = plan_reviews(proposal_run.run_id, project_root=tmp_path)
    assert repeated.run_id == review_plan.run_id
    review_run = execute_reviews(
        review_plan,
        FakeTerminologyProvider(
            review_response=TerminologyReviewResponse(schema_version="1.0", reviews=[review_value])
        ),
        now=lambda: NOW,
    )
    assert review_run.proposal_run_id == proposal_run.run_id
    before = {path.name: path.read_bytes() for path in review_plan.output_directory.iterdir()}
    audit_review_run(review_plan.output_directory, project_root=tmp_path)
    assert before == {
        path.name: path.read_bytes() for path in review_plan.output_directory.iterdir()
    }

    failed_plan = plan_reviews(
        proposal_run.run_id, project_root=tmp_path, model="synthetic-malformed"
    )
    with pytest.raises(TerminologyWorkflowError, match="malformed"):
        execute_reviews(failed_plan, FakeTerminologyProvider("malformed"), now=lambda: NOW)
    assert not failed_plan.output_directory.exists()
    staging = failed_plan.output_root / f".{failed_plan.run_id}.review"
    assert (staging / "plan.json").is_file()
    assert len(list(staging.glob("*.attempt-01.yaml"))) == 1
    assert not list(staging.glob("*.validated.yaml"))


def test_review_cli_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synthetic_project(tmp_path)
    # A missing proposal is rejected during planning, before any provider or output write.
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["review-terminology-proposals", "ta-" + "0" * 24, "--dry-run"],
    )
    assert result.exit_code == 1
    assert not (tmp_path / "workspace/terminology-runs").exists()


def test_conflicting_incomplete_snapshot_requires_force(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    initial = plan_snapshot(project_root=tmp_path)
    initial.output_directory.mkdir(parents=True)
    (initial.output_directory / "manifest.yaml").write_text("broken", encoding="utf-8")

    conflict = plan_snapshot(project_root=tmp_path)
    assert conflict.action == "conflict"
    replacement = plan_snapshot(project_root=tmp_path, force=True)
    result = create_snapshot(replacement, now=lambda: NOW)
    assert validate_snapshot(result.path, project_root=tmp_path, now=lambda: NOW).report.valid


def test_publication_failure_exposes_no_frozen_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synthetic_project(tmp_path)
    plan = plan_snapshot(project_root=tmp_path)

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("injected rename failure")

    monkeypatch.setattr("book_translator.translation_snapshot.workflow.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        create_snapshot(plan, now=lambda: NOW)

    assert not plan.output_directory.exists()


def test_list_and_status_report_frozen_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synthetic_project(tmp_path)
    create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    monkeypatch.chdir(tmp_path)

    listed = runner.invoke(app, ["list-translation-snapshots"])
    status = runner.invoke(app, ["status"])

    assert listed.exit_code == 0
    assert list_snapshots(project_root=tmp_path)[0].validation_state == "current"
    assert "Translation snapshots" in status.stdout
    assert "Frozen translation snapshots" in status.stdout


def test_cli_is_private_and_commands_are_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synthetic_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["create-translation-snapshot", "--dry-run", "--show-manifest"])
    help_result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Alpha beta" not in result.stdout
    for command in {
        "create-translation-snapshot",
        "validate-translation-snapshot",
        "list-translation-snapshots",
    }:
        assert command in help_result.stdout


def _write_open_questions(root: Path, *, question_id: str = "tq-" + "a" * 24) -> bytes:
    questions = root / "config/terminology-questions.yaml"
    questions.write_text(
        "schema_version: '1.0'\nregistry_kind: terminology_questions\nversion: 1.0.0\n"
        "book_id: synthetic-book\nquestions:\n"
        f"- question_id: {question_id}\n"
        "  candidate_id: tc-" + "b" * 24 + "\n"
        "  decision_id: null\n"
        "  source_term: Aunt Veyra\n"
        "  provisional_target: خاله ویرا\n"
        "  category: kinship_side\n"
        "  question: Is Veyra a maternal or paternal aunt?\n"
        "  status: open\n"
        "  blocking: false\n"
        "  evidence_status: inconclusive\n"
        "  chapters: []\n"
        "  chunks: []\n"
        "  research:\n"
        "    current_book: checked_inconclusive\n"
        "    web: not_checked\n"
        "    other_volumes: not_checked\n"
        "    notes: []\n"
        "  provenance: current_book\n"
        "  evidence_sha256: null\n"
        "  resolution: null\n"
        "  created_at: '2026-03-04T00:00:00Z'\n"
        "  superseded_by: null\n",
        encoding="utf-8",
    )
    _write_yaml(
        root / "config/terminology-questions.metadata.yaml",
        {
            "schema_version": "1.0",
            "resource_kind": "open_questions",
            "resource_file": "config/terminology-questions.yaml",
            "version": "1.0.0",
        },
    )
    return questions.read_bytes()


def test_snapshot_freezes_open_question_bytes(tmp_path: Path) -> None:
    synthetic_project(tmp_path)
    original = _write_open_questions(tmp_path)
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    manifest = TranslationManifest.model_validate(
        yaml.safe_load((snapshot.path / "manifest.yaml").read_text(encoding="utf-8"))
    )
    assert manifest.open_questions is not None
    assert manifest.open_questions.frozen_file is not None
    frozen = tmp_path / manifest.open_questions.frozen_file
    assert frozen.read_bytes() == original
    assert manifest.open_questions.sha256 == _hash_bytes(original)


def test_resolving_a_question_makes_the_old_snapshot_historical_not_invalid(
    tmp_path: Path,
) -> None:
    """A later resolution must produce new lineage, never rewrite a frozen snapshot."""
    synthetic_project(tmp_path)
    original = _write_open_questions(tmp_path)
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    manifest = TranslationManifest.model_validate(
        yaml.safe_load((snapshot.path / "manifest.yaml").read_text(encoding="utf-8"))
    )
    assert manifest.open_questions is not None
    assert manifest.open_questions.frozen_file is not None
    frozen = tmp_path / manifest.open_questions.frozen_file

    (tmp_path / "config/terminology-questions.yaml").write_text(
        original.decode("utf-8").replace("status: open", "status: superseded"),
        encoding="utf-8",
    )
    validation = validate_snapshot(snapshot.path, project_root=tmp_path, now=lambda: NOW)
    assert validation.report.valid
    assert not validation.lineage_current
    assert frozen.read_bytes() == original


def _write_source_anomalies(root: Path) -> bytes:
    registry = root / "config/source-anomalies.yaml"
    registry.write_text(
        "schema_version: '1.0'\nregistry_kind: source_anomalies\nversion: 1.0.0\n"
        "book_id: synthetic-book\nanomalies: []\n",
        encoding="utf-8",
    )
    _write_yaml(
        root / "config/source-anomalies.metadata.yaml",
        {
            "schema_version": "1.0",
            "resource_kind": "source_anomalies",
            "resource_file": "config/source-anomalies.yaml",
            "version": "1.0.0",
        },
    )
    return registry.read_bytes()


def _write_terminology(root: Path) -> bytes:
    registry = root / "config/terminology.yaml"
    registry.write_text(
        "schema_version: '1.0'\nregistry_kind: terminology\nversion: 1.0.0\n"
        "book_id: synthetic-book\nsource_language: en\ntarget_language: fa\n"
        "candidates: []\ndecisions: []\n",
        encoding="utf-8",
    )
    _write_yaml(
        root / "config/terminology.metadata.yaml",
        {
            "schema_version": "1.0",
            "resource_kind": "terminology",
            "resource_file": "config/terminology.yaml",
            "version": "1.0.0",
        },
    )
    return registry.read_bytes()


def _fully_resourced_project(root: Path) -> dict[str, bytes]:
    """A synthetic project carrying every mutable translation input a snapshot must freeze."""
    synthetic_project(root)
    return {
        "prompt": (root / "config/translation-prompt.md").read_bytes(),
        "glossary": (root / "config/glossary.yaml").read_bytes(),
        "style_guide": (root / "config/style-guide.md").read_bytes(),
        "terminology": _write_terminology(root),
        "source_anomalies": _write_source_anomalies(root),
        "open_questions": _write_open_questions(root),
    }


RESOURCE_SOURCES = {
    "prompt": "config/translation-prompt.md",
    "glossary": "config/glossary.yaml",
    "style_guide": "config/style-guide.md",
    "terminology": "config/terminology.yaml",
    "source_anomalies": "config/source-anomalies.yaml",
    "open_questions": "config/terminology-questions.yaml",
}
RESOURCE_CATEGORIES = {
    "prompt": "prompts",
    "glossary": "glossaries",
    "style_guide": "style-guides",
    "terminology": "terminology",
    "source_anomalies": "source-anomalies",
    "open_questions": "open-questions",
}


@pytest.mark.parametrize("resource", sorted(RESOURCE_SOURCES))
def test_every_mutable_translation_input_is_durably_frozen(tmp_path: Path, resource: str) -> None:
    originals = _fully_resourced_project(tmp_path)
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    manifest = TranslationManifest.model_validate(
        yaml.safe_load((snapshot.path / "manifest.yaml").read_text(encoding="utf-8"))
    )

    reference = getattr(manifest, resource)
    assert reference is not None
    assert reference.frozen_file is not None
    assert reference.file == Path(RESOURCE_SOURCES[resource])
    assert reference.sha256 == _hash_bytes(originals[resource])
    expected_suffix = Path(RESOURCE_SOURCES[resource]).suffix
    assert reference.frozen_file == Path(
        f"workspace/resources/{RESOURCE_CATEGORIES[resource]}/{reference.sha256}{expected_suffix}"
    )
    assert (tmp_path / reference.frozen_file).read_bytes() == originals[resource]


@pytest.mark.parametrize("resource", sorted(RESOURCE_SOURCES))
def test_editing_current_config_cannot_invalidate_frozen_historical_bytes(
    tmp_path: Path, resource: str
) -> None:
    originals = _fully_resourced_project(tmp_path)
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    manifest = TranslationManifest.model_validate(
        yaml.safe_load((snapshot.path / "manifest.yaml").read_text(encoding="utf-8"))
    )
    reference = getattr(manifest, resource)
    assert reference is not None and reference.frozen_file is not None
    frozen = tmp_path / reference.frozen_file
    snapshot_before = {path.name: path.read_bytes() for path in sorted(snapshot.path.iterdir())}

    (tmp_path / RESOURCE_SOURCES[resource]).write_text(
        "changed after the snapshot was frozen\n", encoding="utf-8"
    )
    validation = validate_snapshot(snapshot.path, project_root=tmp_path, now=lambda: NOW)

    assert validation.report.valid
    assert f"frozen_{resource}_resource" in validation.report.checks_performed
    assert not validation.lineage_current
    assert frozen.read_bytes() == originals[resource]
    assert {path.name: path.read_bytes() for path in sorted(snapshot.path.iterdir())} == (
        snapshot_before
    )


@pytest.mark.parametrize("resource", sorted(RESOURCE_SOURCES))
def test_deleted_frozen_resource_makes_a_new_snapshot_invalid(
    tmp_path: Path, resource: str
) -> None:
    _fully_resourced_project(tmp_path)
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    manifest = TranslationManifest.model_validate(
        yaml.safe_load((snapshot.path / "manifest.yaml").read_text(encoding="utf-8"))
    )
    reference = getattr(manifest, resource)
    assert reference is not None and reference.frozen_file is not None
    (tmp_path / reference.frozen_file).unlink()

    validation = validate_snapshot(snapshot.path, project_root=tmp_path, now=lambda: NOW)

    assert not validation.report.valid
    assert any("is missing or stale" in error for error in validation.report.errors)


def test_creation_refuses_a_manifest_without_durable_frozen_artifacts(tmp_path: Path) -> None:
    _fully_resourced_project(tmp_path)
    plan = plan_snapshot(project_root=tmp_path)
    unfrozen = plan.manifest.model_copy(
        update={"prompt": plan.manifest.prompt.model_copy(update={"frozen_file": None})}
    )

    with pytest.raises(SnapshotError, match="no durable frozen artifact"):
        create_snapshot(
            plan.__class__(
                project_root=plan.project_root,
                output_root=plan.output_root,
                snapshot_id=plan.snapshot_id,
                manifest_sha256=plan.manifest_sha256,
                manifest=unfrozen,
                label=None,
                action="create",
                warnings=(),
            ),
            now=lambda: NOW,
        )
    assert not plan.output_directory.exists()


def _resolve_synthetic_book_metadata(root: Path, **overrides: str) -> None:
    """Publish a canonical resolved book metadata record for the synthetic project."""
    create_pdf(
        root / "input/book.pdf",
        ["A Synthetic Tale", "The End of the Second Book of The Synthetic Cycle"],
        metadata={"title": "A Synthetic Tale", "author": "Ymir Solheim"},
    )
    pdf_hash = _hash_bytes((root / "input/book.pdf").read_bytes())
    toc = yaml.safe_load((root / "workspace/toc.yaml").read_text(encoding="utf-8"))
    toc["source_pdf_sha256"] = pdf_hash
    _write_yaml(root / "workspace/toc.yaml", toc)
    assertions = {
        name: OperatorAssertion(value, BookMetadataProvenance.CONFIGURATION)
        for name, value in overrides.items()
    }
    execute_book_metadata(
        plan_book_metadata(project_root=root, assertions=assertions, now=lambda: NOW)
    )


def test_resolved_book_metadata_is_frozen_and_participates_in_snapshot_identity(
    tmp_path: Path,
) -> None:
    _fully_resourced_project(tmp_path)
    configured_only = plan_snapshot(project_root=tmp_path)
    assert configured_only.manifest.book.metadata_file is None
    assert configured_only.warnings

    _resolve_synthetic_book_metadata(tmp_path)
    resolved = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    manifest = TranslationManifest.model_validate(
        yaml.safe_load((resolved.path / "manifest.yaml").read_text(encoding="utf-8"))
    )
    metadata_bytes = (tmp_path / "workspace/metadata.yaml").read_bytes()

    assert resolved.snapshot_id != configured_only.snapshot_id
    assert manifest.book.title_en == "A Synthetic Tale"
    assert manifest.book.author == "Ymir Solheim"
    assert manifest.book.series_name == "The Synthetic Cycle"
    assert manifest.book.series_index == 2
    assert manifest.book.metadata_sha256 == _hash_bytes(metadata_bytes)
    assert manifest.book.frozen_metadata_file is not None
    assert (tmp_path / manifest.book.frozen_metadata_file).read_bytes() == metadata_bytes

    validation = validate_snapshot(resolved.path, project_root=tmp_path, now=lambda: NOW)
    assert validation.report.valid
    assert "frozen_book_metadata" in validation.report.checks_performed


def test_changed_book_metadata_creates_a_distinct_snapshot_identity(tmp_path: Path) -> None:
    _fully_resourced_project(tmp_path)
    _resolve_synthetic_book_metadata(tmp_path)
    first = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    first_bytes = {path.name: path.read_bytes() for path in sorted(first.path.iterdir())}

    _resolve_synthetic_book_metadata(tmp_path, publisher="Synthetic House")
    second = plan_snapshot(project_root=tmp_path)

    assert second.snapshot_id != first.snapshot_id
    assert second.manifest_sha256 != first_bytes["snapshot.yaml"].decode("utf-8")
    assert {path.name: path.read_bytes() for path in sorted(first.path.iterdir())} == first_bytes
    assert validate_snapshot(first.path, project_root=tmp_path, now=lambda: NOW).report.valid


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("book_id", "example-fantasy-novel-en-fa"),
        ("title_en", "Example Fantasy Novel"),
        ("author", "Example Author"),
    ],
)
def test_authoritative_full_snapshot_refuses_placeholder_book_metadata(
    tmp_path: Path, field: str, value: str
) -> None:
    _fully_resourced_project(tmp_path)
    _resolve_synthetic_book_metadata(tmp_path, **{field: value})

    with pytest.raises(SnapshotError, match=f"refuses placeholder book metadata: {field}"):
        plan_snapshot(project_root=tmp_path)
    assert not (tmp_path / "workspace/snapshots").exists()


def test_placeholder_persian_title_is_refused_for_a_full_snapshot(tmp_path: Path) -> None:
    _fully_resourced_project(tmp_path)
    _resolve_synthetic_book_metadata(tmp_path, title_fa="رمان فانتزی نمونه")

    with pytest.raises(SnapshotError, match="refuses placeholder book metadata: title_fa"):
        plan_snapshot(project_root=tmp_path)
