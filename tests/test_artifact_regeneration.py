import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.artifact_regeneration import (
    ArtifactRegenerationError,
    _load_immutable_manifests,
    execute_artifact_regeneration,
    plan_artifact_regeneration,
)
from book_translator.cli import app
from book_translator.document_chunking import ChunkingConfig
from book_translator.document_chunking.workflow import (
    execute_chapter_chunking,
    plan_chapter_chunking,
)
from book_translator.models import ChapterMetadata, ChapterStatus, TocKind
from book_translator.normalization import NORMALIZATION_VERSION, normalize_text
from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
from book_translator.translation_queue import create_run, plan_run
from book_translator.translation_snapshot import (
    create_snapshot,
    plan_snapshot,
    validate_snapshot,
)

NOW = datetime(2026, 4, 5, 6, 7, tzinfo=UTC)
runner = CliRunner()


def _write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _resource(root: Path, stem: str, kind: str, content: str) -> None:
    suffix = ".yaml" if stem == "glossary" else ".md"
    path = root / "config" / f"{stem}{suffix}"
    path.write_text(content, encoding="utf-8")
    _write_yaml(
        path.with_suffix(".metadata.yaml"),
        {
            "schema_version": "1.0",
            "resource_kind": kind,
            "resource_file": path.relative_to(root).as_posix(),
            "version": "1.0.0",
        },
    )


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists():
        return digest.hexdigest()
    for candidate in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(candidate.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(candidate.read_bytes())
    return digest.hexdigest()


def _legacy_project(root: Path) -> tuple[str, str]:
    (root / "config").mkdir()
    (root / "input").mkdir()
    pdf = root / "input/book.pdf"
    pdf.write_bytes(b"synthetic-pdf")
    pdf_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (root / "config/book.yaml").write_text(
        """book_id: synthetic-book
title_en: Synthetic Book
title_fa: کتاب ساختگی
author: Synthetic Author
source_language: en
target_language: fa
pdf_path: input/book.pdf
chunk_target_words: 10
chunk_max_words: 20
previous_context_words: 4
output_format: epub3
""",
        encoding="utf-8",
    )
    _resource(
        root,
        "translation-prompt",
        "prompt",
        "Chapter {{ chapter_number }} part {{ part_number }}\n"
        "{{ glossary }}\n{{ previous_context }}\n{{ source_text }}\n",
    )
    _resource(
        root,
        "glossary",
        "glossary",
        "terms: {}\ncharacters: {}\nplaces: {}\ngroups: {}\nunresolved_terms: []\n",
    )
    _resource(root, "style-guide", "style_guide", "Synthetic style.\n")
    chapter = root / "workspace/chapters/001"
    chapter.mkdir(parents=True)
    source = (
        "<!-- source-page: 1 -->\n\n"
        "Alpha opening line\ncontinues toward the boundary\n"
        "<!-- source-page: 2 -->\n"
        "lowercase continuation line\nand finishes here.\n\n"
        "Separate synthetic paragraph with enough words for another planned chunk.\n"
    )
    source_path = chapter / "source.md"
    source_path.write_text(source, encoding="utf-8")
    current_cleaned = normalize_text(source).cleaned_text
    legacy_cleaned = current_cleaned.replace("boundary lowercase", "boundary\n\nlowercase", 1)
    assert legacy_cleaned != current_cleaned
    cleaned_path = chapter / "cleaned.md"
    cleaned_path.write_text(legacy_cleaned, encoding="utf-8")
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    cleaned_hash = hashlib.sha256(legacy_cleaned.encode()).hexdigest()
    metadata = ChapterMetadata(
        schema_version="1.0",
        chapter_id="001",
        kind=TocKind.CHAPTER,
        number=1,
        title_en="Synthetic Chapter",
        source_start_page=1,
        source_end_page=2,
        source_file=Path("workspace/chapters/001/source.md"),
        source_sha256=source_hash,
        cleaned_file=Path("workspace/chapters/001/cleaned.md"),
        cleaned_sha256=cleaned_hash,
        source_word_count=len(source.split()),
        cleaned_word_count=len(legacy_cleaned.split()),
        source_paragraph_count=2,
        cleaned_paragraph_count=len(legacy_cleaned.rstrip("\n").split("\n\n")),
        status=ChapterStatus.NORMALIZED,
        extraction_version="1.0",
        normalization_version="1.0",
        created_at=NOW,
        updated_at=NOW,
    )
    _write_yaml(chapter / "metadata.yaml", metadata)
    _write_yaml(
        chapter / "normalization-report.yaml",
        {
            "schema_version": "1.0",
            "chapter_id": "001",
            "source_sha256": source_hash,
            "cleaned_sha256": cleaned_hash,
            "normalization_version": "1.0",
            "cleaned_paragraph_count": metadata.cleaned_paragraph_count,
            "warnings": [],
        },
    )
    _write_yaml(
        root / "workspace/toc.yaml",
        {
            "schema_version": "1.0",
            "source": "embedded",
            "source_pdf": "input/book.pdf",
            "source_pdf_sha256": pdf_hash,
            "pdf_page_count": 2,
            "entry_count": 1,
            "entries": [
                {
                    "entry_id": "chapter-001",
                    "level": 1,
                    "kind": "chapter",
                    "number": 1,
                    "title_en": "Synthetic Chapter",
                    "title_fa": "",
                    "symbol": None,
                    "start_page": 1,
                    "end_page": 2,
                    "source_toc_title": "Chapter 1",
                }
            ],
        },
    )
    chunk_plan = plan_chapter_chunking(
        project_root=root,
        config=ChunkingConfig(10, 20, 4),
        now=lambda: NOW,
    )
    execute_chapter_chunking(chunk_plan)
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: NOW)
    run = create_run(plan_run(snapshot.snapshot_id, project_root=root, now=lambda: NOW))
    execute_prompt_packages(plan_prompt_packages(run.run_id, project_root=root, now=lambda: NOW))
    return snapshot.snapshot_id, run.run_id


def test_stale_normalization_and_chunks_are_detected(tmp_path: Path) -> None:
    _legacy_project(tmp_path)

    plan = plan_artifact_regeneration(project_root=tmp_path, selectors=["1"], now=lambda: NOW)
    chapter = plan.chapters[0]

    assert chapter.source_current
    assert not chapter.normalization_current
    assert not chapter.chunking_current
    assert chapter.cleaned_hash_would_change
    assert chapter.chunk_hashes_would_change
    assert chapter.action == "replace-normalization-and-chunks"
    assert chapter.next_step == "regenerate-normalization"
    assert chapter.immutable_lineage.exists
    assert not chapter.blockers


def test_regeneration_accepts_supported_historical_snapshot_formats() -> None:
    project_root = Path(__file__).parents[1]

    manifests = _load_immutable_manifests(project_root)

    assert {
        "translation-fa-e0de165b014572f45a784847",
        "translation-fa-00e997ab2fc3e8f859794d15",
        "translation-fa-5841fd98484dbcbe7908655e",
        "translation-fa-401467944cfb5347754fcae1",
    }.issubset({snapshot_id for snapshot_id, _ in manifests})


def test_default_cli_dry_run_performs_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy_project(tmp_path)
    before = _tree_hash(tmp_path / "workspace")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["regenerate-artifacts", "--chapter", "1"])

    assert result.exit_code == 0, result.stdout
    assert "read-only dry run" in result.stdout
    assert _tree_hash(tmp_path / "workspace") == before


def test_safe_regeneration_updates_normalized_and_chunks(tmp_path: Path) -> None:
    _legacy_project(tmp_path)
    plan = plan_artifact_regeneration(project_root=tmp_path, selectors=["1"], now=lambda: NOW)

    result = execute_artifact_regeneration(plan)
    repeated = plan_artifact_regeneration(project_root=tmp_path, selectors=["1"], now=lambda: NOW)
    metadata = yaml.safe_load(
        (tmp_path / "workspace/chapters/001/metadata.yaml").read_text(encoding="utf-8")
    )

    assert result.normalized_ids == ("001",)
    assert result.chunked_ids == ("001",)
    assert metadata["normalization_version"] == NORMALIZATION_VERSION
    assert repeated.chapters[0].normalization_current
    assert repeated.chapters[0].chunking_current
    assert repeated.chapters[0].action == "none"
    assert repeated.chapters[0].next_step == "create-new-snapshot"


def test_version_only_normalization_staleness_preserves_current_chunks(tmp_path: Path) -> None:
    _legacy_project(tmp_path)
    execute_artifact_regeneration(
        plan_artifact_regeneration(project_root=tmp_path, selectors=["1"], now=lambda: NOW)
    )
    metadata_path = tmp_path / "workspace/chapters/001/metadata.yaml"
    report_path = tmp_path / "workspace/chapters/001/normalization-report.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    report = yaml.safe_load(report_path.read_text(encoding="utf-8"))
    metadata["normalization_version"] = "1.0"
    report["normalization_version"] = "1.0"
    _write_yaml(metadata_path, metadata)
    _write_yaml(report_path, report)
    chunks_before = _tree_hash(tmp_path / "workspace/chapters/001/chunks")

    plan = plan_artifact_regeneration(project_root=tmp_path, selectors=["1"], now=lambda: NOW)
    execute_artifact_regeneration(plan)

    assert plan.chapters[0].action == "replace-normalization"
    assert not plan.chapters[0].cleaned_hash_would_change
    assert plan.chapters[0].chunking_current
    assert _tree_hash(tmp_path / "workspace/chapters/001/chunks") == chunks_before


def test_cli_apply_regenerates_selected_mutable_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["regenerate-artifacts", "--chapter", "1", "--apply"])
    metadata = yaml.safe_load(
        (tmp_path / "workspace/chapters/001/metadata.yaml").read_text(encoding="utf-8")
    )

    assert result.exit_code == 0, result.stdout
    assert "apply" in result.stdout
    assert metadata["normalization_version"] == NORMALIZATION_VERSION


def test_immutable_snapshot_and_run_remain_identical_and_historical(tmp_path: Path) -> None:
    snapshot_id, run_id = _legacy_project(tmp_path)
    snapshot_path = tmp_path / "workspace/snapshots/translation" / snapshot_id
    run_path = tmp_path / "workspace/translation-runs" / run_id
    snapshot_before = _tree_hash(snapshot_path)
    run_before = _tree_hash(run_path)

    execute_artifact_regeneration(
        plan_artifact_regeneration(project_root=tmp_path, selectors=["1"], now=lambda: NOW)
    )
    validation = validate_snapshot(snapshot_id, project_root=tmp_path, now=lambda: NOW)

    assert _tree_hash(snapshot_path) == snapshot_before
    assert _tree_hash(run_path) == run_before
    assert list(run_path.glob("jobs/*/prompt-package/package.yaml"))
    assert not list(run_path.glob("jobs/*/attempts/*/attempt.yaml"))
    assert validation.report.valid
    assert not validation.lineage_current
    assert validation.lineage_warnings


def test_new_snapshot_identity_changes_after_content_regeneration(tmp_path: Path) -> None:
    old_snapshot_id, _ = _legacy_project(tmp_path)
    execute_artifact_regeneration(
        plan_artifact_regeneration(project_root=tmp_path, selectors=["1"], now=lambda: NOW)
    )

    new_plan = plan_snapshot(project_root=tmp_path)

    assert new_plan.snapshot_id != old_snapshot_id


def test_glossary_only_change_requires_snapshot_but_not_mutable_regeneration(
    tmp_path: Path,
) -> None:
    _legacy_project(tmp_path)
    execute_artifact_regeneration(
        plan_artifact_regeneration(project_root=tmp_path, selectors=["1"], now=lambda: NOW)
    )
    current_snapshot = create_snapshot(plan_snapshot(project_root=tmp_path), now=lambda: NOW)
    mutable_before = _tree_hash(tmp_path / "workspace/chapters/001")
    glossary = tmp_path / "config/glossary.yaml"
    glossary.write_text(
        "terms: {synthetic: ساختگی}\ncharacters: {}\nplaces: {}\n"
        "groups: {}\nunresolved_terms: []\n",
        encoding="utf-8",
    )
    metadata_path = tmp_path / "config/glossary.metadata.yaml"
    resource_metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    resource_metadata["version"] = "1.1.0"
    _write_yaml(metadata_path, resource_metadata)

    regeneration = plan_artifact_regeneration(
        project_root=tmp_path, selectors=["1"], now=lambda: NOW
    )
    next_snapshot = plan_snapshot(project_root=tmp_path)

    assert regeneration.chapters[0].action == "none"
    assert regeneration.chapters[0].next_step == "create-new-snapshot"
    assert next_snapshot.snapshot_id != current_snapshot.snapshot_id
    assert _tree_hash(tmp_path / "workspace/chapters/001") == mutable_before


def test_regeneration_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _legacy_project(first)
    _legacy_project(second)
    first_plan = plan_artifact_regeneration(project_root=first, selectors=["1"], now=lambda: NOW)
    second_plan = plan_artifact_regeneration(project_root=second, selectors=["1"], now=lambda: NOW)

    execute_artifact_regeneration(first_plan)
    execute_artifact_regeneration(second_plan)

    assert first_plan.chapters[0].planned_cleaned_sha256 == (
        second_plan.chapters[0].planned_cleaned_sha256
    )
    assert _tree_hash(first / "workspace/chapters/001") == _tree_hash(
        second / "workspace/chapters/001"
    )


def test_failed_staged_publication_restores_all_canonical_artifacts(tmp_path: Path) -> None:
    _legacy_project(tmp_path)
    chapter_path = tmp_path / "workspace/chapters/001"
    before = _tree_hash(chapter_path)
    plan = plan_artifact_regeneration(project_root=tmp_path, selectors=["1"], now=lambda: NOW)

    def fail_after_chunks(name: str) -> None:
        if name == "chunks":
            raise OSError("injected staged publication failure")

    with pytest.raises(ArtifactRegenerationError, match="prior canonical artifacts were restored"):
        execute_artifact_regeneration(plan, after_publish=fail_after_chunks)

    assert _tree_hash(chapter_path) == before
    assert not list(chapter_path.glob(".regeneration-*"))


def test_inspection_command_is_read_only_and_reports_hash_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy_project(tmp_path)
    before = _tree_hash(tmp_path / "workspace")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["inspect-artifact-staleness", "--chapter", "1"])

    assert result.exit_code == 0, result.stdout
    assert "replace-normalization-and-chunks" in result.stdout
    assert "change" in result.stdout
    assert _tree_hash(tmp_path / "workspace") == before
