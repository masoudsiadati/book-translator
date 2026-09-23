from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.book_identity import (
    BOOK_IDENTITY_MIGRATION_VERSION,
    BookIdentityMigrationError,
    BookIdentityRewriteError,
    ScalarRewrite,
    apply_scalar_rewrites,
    execute_book_identity_migration,
    load_migrations,
    migration_identity,
    plan_book_identity_migration,
    semantic_identity,
    strip_paths,
)
from book_translator.book_metadata import execute_book_metadata, plan_book_metadata
from book_translator.cli import app
from book_translator.models import OpenQuestionRegistry, SourceAnomalyRegistry, TerminologyRegistry
from book_translator.source_anomalies import anomaly_identity, load_source_anomalies
from book_translator.terminology.registry import load_registry
from tests.test_book_metadata import _project

NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
runner = CliRunner()

OLD_ID = "example-fantasy-novel-en-fa"
NEW_ID = "a-synthetic-tale-en-fa"

TERMINOLOGY = """schema_version: '1.0'
registry_kind: terminology
version: 1.0.0
book_id: example-fantasy-novel-en-fa
source_language: en
target_language: fa
candidates:
- candidate_id: tc-{candidate}
  source_term: Alpha
  classification: person
  confidence: 0.95
  status: approved
  provenance: ai
  aliases: []
  occurrences: []
  context_sensitive: false
  classification_conflicts: []
  evidence_notes: []
decisions:
- decision_id: td-{decision}
  candidate_id: tc-{candidate}
  target_term: آلفا
  decision_source: user
  decision_state: confirmed
  locked: true
  required: true
  competing_variants: []
  contextual_notes: []
"""

QUESTIONS = """schema_version: '1.0'
registry_kind: terminology_questions
version: 1.0.0
book_id: example-fantasy-novel-en-fa
questions: []
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _anomalies(book_id: str = OLD_ID) -> str:
    original = 'A token fra —"'
    import hashlib

    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    anomaly_id = anomaly_identity(
        book_id=book_id,
        chapter_id="008",
        page_from=10,
        page_to=11,
        source_location="page-boundary:10-11",
        category=__import__(
            "book_translator.models", fromlist=["SourceAnomalyCategory"]
        ).SourceAnomalyCategory.TRUNCATED_TOKEN,
        original_sha256=digest,
    )
    return f"""schema_version: '1.0'
registry_kind: source_anomalies
registry_version: 1.0.0
anomalies:
  - anomaly_id: {anomaly_id}
    book_id: {book_id}
    chapter_id: '008'
    page_from: 10
    page_to: 11
    source_location: page-boundary:10-11
    category: truncated_token
    status: confirmed_source_defect
    severity: local
    blocking: false
    original_text: "A token fra \\u2014\\""
    original_sha256: {digest}
    source_file: workspace/chapters/008/source.md
    source_file_sha256: {"b" * 64}
    evidence:
      rendered_pdf_checked: true
      pdf_text_layer_matches: true
      immutable_source_matches: true
      extraction_at_fault: false
      replacement_known: false
      evidence_files: []
      evidence_sha256: []
    operator_note: >-
      Synthetic note whose folded block must survive the migration byte for byte.
    resolution: null
    created_at: '2026-01-01T00:00:00+00:00'
"""


def _migratable_project(root: Path) -> None:
    """A resolved project whose registries still carry the previous book identity."""
    _project(
        root,
        page_texts=["A Synthetic Tale"],
        metadata={"title": "A Synthetic Tale", "author": "Ymir Solheim"},
    )
    execute_book_metadata(plan_book_metadata(project_root=root, now=lambda: NOW))
    _write(
        root / "config/terminology.yaml",
        TERMINOLOGY.format(candidate="a" * 24, decision="b" * 24),
    )
    _write(root / "config/terminology-questions.yaml", QUESTIONS)
    _write(root / "config/source-anomalies.yaml", _anomalies())


def test_scalar_rewrite_preserves_every_other_byte() -> None:
    text = "book_id: old\nnote: >-\n  book_id: old is quoted prose\nother: old\n"

    result = apply_scalar_rewrites(text, [ScalarRewrite("book_id", "old", "new")])

    # Only the exact addressed scalar moves: prose inside a folded block and an unrelated key stay.
    assert result.text == "book_id: new\nnote: >-\n  book_id: old is quoted prose\nother: old\n"
    assert result.counts == (1,)
    assert result.changed_lines == (1,)


def test_scalar_rewrite_preserves_indentation_and_quoting() -> None:
    text = '  - anomaly_id: "sa-old"\n    book_id: old\t\n'

    result = apply_scalar_rewrites(
        text,
        [ScalarRewrite("anomaly_id", "sa-old", "sa-new"), ScalarRewrite("book_id", "old", "n")],
    )

    assert result.text == '  - anomaly_id: "sa-new"\n    book_id: n\t\n'


def test_absent_scalar_is_refused() -> None:
    with pytest.raises(BookIdentityRewriteError, match="expected scalar was not found"):
        apply_scalar_rewrites("book_id: old\n", [ScalarRewrite("missing", "old", "new")])
    with pytest.raises(BookIdentityRewriteError, match="must change its value"):
        apply_scalar_rewrites("book_id: old\n", [ScalarRewrite("book_id", "old", "old")])


def test_strip_paths_and_semantic_identity_ignore_identity_fields() -> None:
    before = {"book_id": "a", "items": [{"id": "1", "value": "keep"}]}
    after = {"book_id": "b", "items": [{"id": "2", "value": "keep"}]}
    paths = (("book_id",), ("items", "*", "id"))

    assert strip_paths(before, paths) == {"items": [{"value": "keep"}]}
    assert semantic_identity(before, paths) == semantic_identity(after, paths)
    assert semantic_identity(before, (("book_id",),)) != semantic_identity(after, (("book_id",),))


def test_migration_identity_is_content_addressed() -> None:
    identity = migration_identity(
        previous_book_id=OLD_ID, book_id=NEW_ID, registries=[("config/a.yaml", "a" * 64)]
    )

    assert identity.startswith("bim-") and len(identity) == 28
    assert identity == migration_identity(
        previous_book_id=OLD_ID, book_id=NEW_ID, registries=[("config/a.yaml", "a" * 64)]
    )
    assert identity != migration_identity(
        previous_book_id=OLD_ID, book_id=NEW_ID, registries=[("config/a.yaml", "b" * 64)]
    )


def test_migration_moves_identity_without_changing_semantic_content(tmp_path: Path) -> None:
    _migratable_project(tmp_path)
    before = {
        path: (tmp_path / path).read_text(encoding="utf-8")
        for path in (
            "config/terminology.yaml",
            "config/terminology-questions.yaml",
            "config/source-anomalies.yaml",
        )
    }

    plan = plan_book_identity_migration(project_root=tmp_path, now=lambda: NOW)
    record_file = execute_book_identity_migration(plan)

    assert plan.action == "migrate"
    assert plan.previous_book_id == OLD_ID
    assert plan.book_id == NEW_ID
    assert record_file is not None and record_file.is_file()
    for path, original in before.items():
        after = (tmp_path / path).read_text(encoding="utf-8")
        assert after != original
        # Reversing exactly the recorded identity substitutions restores the original bytes.
        restored = after.replace(NEW_ID, OLD_ID)
        for item in plan.registries:
            if item.file.as_posix() != path:
                continue
            for change in item.record.derived_identity_changes:
                restored = restored.replace(change.after, change.before)
        assert restored == original
    assert load_registry(tmp_path / "config/terminology.yaml").book_id == NEW_ID
    assert (
        OpenQuestionRegistry.model_validate(
            yaml.safe_load((tmp_path / "config/terminology-questions.yaml").read_text("utf-8"))
        ).book_id
        == NEW_ID
    )
    assert load_source_anomalies(tmp_path / "config/source-anomalies.yaml").anomalies[
        0
    ].book_id == (NEW_ID)


def test_migration_preserves_registry_semantics_and_decisions(tmp_path: Path) -> None:
    _migratable_project(tmp_path)
    registry_path = tmp_path / "config/terminology.yaml"
    before = load_registry(registry_path)
    before_semantic = semantic_identity(before.model_dump(mode="json"), (("book_id",),))

    execute_book_identity_migration(
        plan_book_identity_migration(project_root=tmp_path, now=lambda: NOW)
    )
    after = load_registry(registry_path)

    assert semantic_identity(after.model_dump(mode="json"), (("book_id",),)) == before_semantic
    assert len(after.candidates) == len(before.candidates)
    assert [item.model_dump() for item in after.decisions] == [
        item.model_dump() for item in before.decisions
    ]


def test_book_scoped_anomaly_identity_is_re_derived(tmp_path: Path) -> None:
    _migratable_project(tmp_path)
    before = load_source_anomalies(tmp_path / "config/source-anomalies.yaml").anomalies[0]

    plan = plan_book_identity_migration(project_root=tmp_path, now=lambda: NOW)
    execute_book_identity_migration(plan)
    after = load_source_anomalies(tmp_path / "config/source-anomalies.yaml").anomalies[0]

    assert after.anomaly_id != before.anomaly_id
    assert after.anomaly_id == anomaly_identity(
        book_id=NEW_ID,
        chapter_id=after.chapter_id,
        page_from=after.page_from,
        page_to=after.page_to,
        source_location=after.source_location,
        category=after.category,
        original_sha256=after.original_sha256,
    )
    assert after.model_dump(exclude={"anomaly_id", "book_id"}) == before.model_dump(
        exclude={"anomaly_id", "book_id"}
    )
    changes = [
        change
        for item in plan.registries
        for change in item.record.derived_identity_changes
        if item.file.as_posix() == "config/source-anomalies.yaml"
    ]
    assert [(change.field, change.before, change.after) for change in changes] == [
        ("anomaly_id", before.anomaly_id, after.anomaly_id)
    ]


def test_migration_record_is_immutable_lineage(tmp_path: Path) -> None:
    _migratable_project(tmp_path)
    plan = plan_book_identity_migration(project_root=tmp_path, now=lambda: NOW)
    record_file = execute_book_identity_migration(plan)
    assert record_file is not None

    records = load_migrations(tmp_path)

    assert len(records) == 1
    record = records[0]
    assert record.migration_id == plan.migration_id
    assert record.engine_version == BOOK_IDENTITY_MIGRATION_VERSION
    assert record.provenance == "canonical_book_metadata"
    assert record.previous_book_id == OLD_ID and record.book_id == NEW_ID
    assert record.semantic_content_unchanged
    assert [item.file.as_posix() for item in record.registries] == [
        "config/source-anomalies.yaml",
        "config/terminology-questions.yaml",
        "config/terminology.yaml",
    ]
    for item in record.registries:
        assert item.before_sha256 != item.after_sha256
        assert item.book_id_occurrences >= 1


def test_second_migration_is_a_skip_and_writes_nothing(tmp_path: Path) -> None:
    _migratable_project(tmp_path)
    execute_book_identity_migration(
        plan_book_identity_migration(project_root=tmp_path, now=lambda: NOW)
    )
    after = {path.name: path.read_bytes() for path in sorted((tmp_path / "config").glob("*.yaml"))}

    repeated = plan_book_identity_migration(project_root=tmp_path, now=lambda: NOW)

    assert repeated.action == "skip"
    assert repeated.registries == ()
    assert execute_book_identity_migration(repeated) is None
    assert {
        path.name: path.read_bytes() for path in sorted((tmp_path / "config").glob("*.yaml"))
    } == after


def test_migration_refuses_registry_drift_after_planning(tmp_path: Path) -> None:
    _migratable_project(tmp_path)
    plan = plan_book_identity_migration(project_root=tmp_path, now=lambda: NOW)
    questions = tmp_path / "config/terminology-questions.yaml"
    questions.write_text(QUESTIONS.replace("version: 1.0.0", "version: 1.1.0"), encoding="utf-8")

    with pytest.raises(BookIdentityMigrationError, match="changed after planning"):
        execute_book_identity_migration(plan)


def test_migration_requires_resolved_book_metadata(tmp_path: Path) -> None:
    _project(
        tmp_path,
        page_texts=["A Synthetic Tale"],
        metadata={"title": "A Synthetic Tale", "author": "Ymir Solheim"},
    )
    _write(root := tmp_path / "config/terminology-questions.yaml", QUESTIONS)
    assert root.is_file()

    with pytest.raises(BookIdentityMigrationError, match="canonical book metadata is required"):
        plan_book_identity_migration(project_root=tmp_path, now=lambda: NOW)


def test_cli_dry_run_writes_nothing_then_apply_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _migratable_project(tmp_path)
    before = (tmp_path / "config/terminology.yaml").read_bytes()
    monkeypatch.chdir(tmp_path)

    dry = runner.invoke(app, ["migrate-book-identity"])

    assert dry.exit_code == 0
    assert (tmp_path / "config/terminology.yaml").read_bytes() == before
    assert not (tmp_path / "workspace/migrations").exists()

    applied = runner.invoke(app, ["migrate-book-identity", "--apply"])

    assert applied.exit_code == 0
    assert (tmp_path / "config/terminology.yaml").read_bytes() != before
    assert len(load_migrations(tmp_path)) == 1
    assert (
        SourceAnomalyRegistry.model_validate(
            yaml.safe_load((tmp_path / "config/source-anomalies.yaml").read_text("utf-8"))
        )
        .anomalies[0]
        .book_id
        == NEW_ID
    )
    assert (
        TerminologyRegistry.model_validate(
            yaml.safe_load((tmp_path / "config/terminology.yaml").read_text("utf-8"))
        ).book_id
        == NEW_ID
    )
