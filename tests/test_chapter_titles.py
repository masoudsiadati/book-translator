"""Governed chapter-title translation, and the release overlay it feeds.

Every fixture is synthetic. The Persian below is real Persian script written for these tests; no
book prose or real chapter title appears in this repository.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.chapter_titles import (
    DOCUMENT_FILENAME,
    MANIFEST_FILENAME,
    PROVENANCE_FILENAME,
    ChapterTitleError,
    chapter_title_identity,
    discover_chapter_title_artifact,
    distinct_source_titles,
    execute_chapter_titles,
    is_target_script,
    load_chapter_title_artifact,
    load_chapter_title_provenance,
    normalize_provider_title,
    plan_chapter_titles,
    render_title_terminology,
    require_source_title,
    title_sha256,
    validate_chapter_titles,
)
from book_translator.cli import app
from book_translator.models import (
    SCHEMA_VERSION,
    ChapterTitleOrigin,
    DecisionSource,
    ReleaseHeadingSource,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.models.book import TocKind
from book_translator.models.chapter_title import ChapterTitleRecord
from book_translator.paths import ProjectPaths
from book_translator.provider_execution import ProviderRequest, ProviderResult, ProviderUsage
from book_translator.release_assembly import (
    LINEAGE_FILENAME,
    ReleaseAssemblyError,
    chapter_heading,
    execute_release_assembly,
    plan_release_assembly,
)
from book_translator.terminology import save_registry, set_decision
from tests.test_release_assembly import (
    TRANSLATION,
    _accepted_run,
    _manifest,
    _workspace_fingerprint,
)
from tests.test_translation_worker import _run_path

runner = CliRunner()

TITLE_CLOCK = datetime(2026, 4, 5, 6, 7, tzinfo=UTC)
#: The synthetic project titles every chapter "Synthetic", so one answer covers the whole book.
SYNTHETIC_TITLE_FA = "ساختگی"


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class TitleProvider:
    """A provider that answers chapter titles from a fixed table and counts its calls."""

    provider_name = "fake"
    adapter_version = "test-1.0.0"

    def __init__(
        self, answers: dict[str, str] | None = None, *, default: str | None = None
    ) -> None:
        self.answers = answers or {}
        self.default = default if default is not None else SYNTHETIC_TITLE_FA
        self.requests: list[ProviderRequest] = []

    def execute(self, request: ProviderRequest) -> ProviderResult:
        self.requests.append(request)
        source = json.loads(request.user_message.split("source_title:\n")[1].split("\n")[0])
        return ProviderResult(
            response_id="synthetic-title",
            model=request.model or "fake-v1",
            status="completed",
            text_outputs=(self.answers.get(source, self.default),),
            usage=ProviderUsage(5, 5, 10),
            sanitized_response={"status": "completed"},
        )


class RefusingProvider:
    """A provider that must never be reached."""

    provider_name = "fake"
    adapter_version = "test-1.0.0"

    def execute(self, request: ProviderRequest) -> ProviderResult:
        raise AssertionError("a provider was contacted when none should have been")


def _titled_run(root: Path, *, chapters: int = 2) -> str:
    """A completed, fully accepted run over the stock synthetic chapters."""
    return _accepted_run(root, chapters=chapters)


def _retitle_chapters(root: Path, titles: dict[str, str], *, field: str = "title_en") -> None:
    """Set chapter titles in canonical metadata, before any snapshot freezes them.

    A frozen manifest is hash-verified, so a title cannot be changed after the fact — which is
    the point. Anything a test wants the snapshot to carry has to be true of the workspace first.
    """
    for chapter_id, title in titles.items():
        path = root / "workspace/chapters" / chapter_id / "metadata.yaml"
        metadata = yaml.safe_load(path.read_text(encoding="utf-8"))
        metadata[field] = title
        path.write_text(
            yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    if field == "title_en":
        toc_path = root / "workspace/toc.yaml"
        toc = yaml.safe_load(toc_path.read_text(encoding="utf-8"))
        for entry in toc["entries"]:
            key = f"{entry['number']:03d}" if entry.get("number") else None
            if key in titles:
                entry["title_en"] = titles[key]
        toc_path.write_text(
            yaml.safe_dump(toc, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )


def _run_with_titles(
    root: Path,
    *,
    chapters: int = 1,
    title_en: dict[str, str] | None = None,
    title_fa: dict[str, str] | None = None,
    registry: tuple[str, str] | None = None,
) -> str:
    """Build a completed accepted run whose frozen snapshot carries the titles a test needs."""
    from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
    from book_translator.translation_queue import create_run, plan_run
    from book_translator.translation_snapshot import create_snapshot, plan_snapshot
    from tests.test_release_assembly import _translate as translate_chunks
    from tests.test_release_assembly import _write_book_metadata
    from tests.test_translation_queue import NOW, SNAPSHOT_NOW
    from tests.test_translation_snapshot import synthetic_project

    root.mkdir(parents=True, exist_ok=True)
    synthetic_project(root, chapters=chapters)
    _write_book_metadata(root)
    if title_en:
        _retitle_chapters(root, title_en)
    if title_fa:
        _retitle_chapters(root, title_fa, field="title_fa")
    if registry is not None:
        _registry(root, source=registry[0], target=registry[1])
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    created = create_run(plan_run(snapshot.snapshot_id, project_root=root, now=lambda: NOW))
    execute_prompt_packages(
        plan_prompt_packages(created.run_id, project_root=root, now=lambda: NOW)
    )
    translate_chunks(root, created.run_id, (TRANSLATION,), jobs=chapters)
    return created.run_id


def _translate_titles(
    root: Path, run_id: str, provider: TitleProvider | None = None, **updates: object
) -> tuple[Path, TitleProvider]:
    adapter = provider or TitleProvider()
    options: dict[str, object] = {"project_root": root}
    options.update({key: value for key, value in updates.items() if key in {"output", "force"}})
    plan = plan_chapter_titles(run_id, **options)  # type: ignore[arg-type]
    result = execute_chapter_titles(
        plan, project_root=root, provider=adapter, now=lambda: TITLE_CLOCK
    )
    return result.output_path, adapter


def _registry(root: Path, *, source: str, target: str) -> TerminologyRegistry:
    registry = TerminologyRegistry(
        schema_version=SCHEMA_VERSION,
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )
    registry = set_decision(
        registry,
        source_term=source,
        target_term=target,
        classification=TerminologyType.STORY_TERM,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    save_registry(root / "config/terminology.yaml", registry)
    (root / "config/terminology.metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "resource_kind": "terminology",
                "resource_file": "config/terminology.yaml",
                "version": "1.0.0",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return registry


# --------------------------------------------------------------------------------------------
# Pure engine
# --------------------------------------------------------------------------------------------


def test_a_provider_answer_is_reduced_to_one_heading_or_refused() -> None:
    assert normalize_provider_title("  کلاغ‌ها  ") == "کلاغ‌ها"
    assert normalize_provider_title('"کلاغ‌ها"') == "کلاغ‌ها"
    assert normalize_provider_title("«کلاغ‌ها»") == "کلاغ‌ها"
    for refused in (
        "",
        "   ",
        "کلاغ‌ها\n\nو توضیح اضافه",
        "```\nکلاغ‌ها\n```",
        "<framed-data>کلاغ‌ها</framed-data>",
        "ک" * 300,
    ):
        with pytest.raises(ChapterTitleError):
            normalize_provider_title(refused)


def test_a_title_must_carry_target_script_and_a_source_must_exist() -> None:
    assert is_target_script("کلاغ‌ها")
    assert not is_target_script("Ravens")
    assert require_source_title("001", "  Ravens  ") == "Ravens"
    with pytest.raises(ChapterTitleError, match="no source title"):
        require_source_title("001", "   ")
    with pytest.raises(ChapterTitleError, match="multi-line"):
        require_source_title("001", "Ravens\nand crows")


def test_identical_source_titles_are_asked_once() -> None:
    assert distinct_source_titles(("A", "B", "A", "C", "B")) == ("A", "B", "C")


def test_identity_addresses_the_inputs_and_not_the_answer() -> None:
    chapters = (("001", "chapter", 1, "Ravens"),)
    first, digest = chapter_title_identity(
        book_id="b",
        source_language="en",
        target_language="fa",
        snapshot_id="s",
        snapshot_manifest_sha256="a" * 64,
        terminology_sha256="b" * 64,
        chapters=chapters,
    )
    again, _ = chapter_title_identity(
        book_id="b",
        source_language="en",
        target_language="fa",
        snapshot_id="s",
        snapshot_manifest_sha256="a" * 64,
        terminology_sha256="b" * 64,
        chapters=chapters,
    )
    changed, _ = chapter_title_identity(
        book_id="b",
        source_language="en",
        target_language="fa",
        snapshot_id="s",
        snapshot_manifest_sha256="a" * 64,
        terminology_sha256="b" * 64,
        chapters=(("001", "chapter", 1, "Crows"),),
    )

    assert first == again
    assert first != changed
    assert first == f"ct-{digest[:24]}"


def test_a_terminology_block_carries_only_the_decisions_this_title_contains() -> None:
    registry = TerminologyRegistry(
        schema_version=SCHEMA_VERSION,
        version="1.0.0",
        book_id="b",
        source_language="en",
        target_language="fa",
    )
    registry = set_decision(
        registry,
        source_term="Rhuidean",
        target_term="روئیدان",
        classification=TerminologyType.PLACE,
        decision_source=DecisionSource.USER,
        locked=True,
    )

    assert "روئیدان" in render_title_terminology(registry, "The Road to Rhuidean")
    assert render_title_terminology(registry, "Twilight") == "canonical_terminology: []\n"


# --------------------------------------------------------------------------------------------
# QA
# --------------------------------------------------------------------------------------------


def _record(chapter_id: str, sequence: int, title_en: str, title_fa: str) -> ChapterTitleRecord:
    return ChapterTitleRecord(
        chapter_id=chapter_id,
        chapter_sequence=sequence,
        kind=TocKind.CHAPTER,
        number=sequence,
        title_en=title_en,
        title_fa=title_fa,
        source_sha256=title_sha256(title_en),
        translation_sha256=title_sha256(title_fa),
        origin=ChapterTitleOrigin.PROVIDER_TRANSLATION,
    )


def _empty_registry() -> TerminologyRegistry:
    return TerminologyRegistry(
        schema_version=SCHEMA_VERSION,
        version="1.0.0",
        book_id="b",
        source_language="en",
        target_language="fa",
    )


def test_qa_accepts_a_complete_consistent_persian_title_set() -> None:
    report = validate_chapter_titles(
        expected=(("001", "Ravens"), ("002", "Twilight")),
        records=(_record("001", 1, "Ravens", "کلاغ‌ها"), _record("002", 2, "Twilight", "گرگ‌ومیش")),
        registry=_empty_registry(),
    )

    assert report.valid
    assert report.findings == []
    assert report.checked_chapters == 2
    assert report.provider_contacted is False


def test_qa_fails_a_chapter_with_no_title() -> None:
    report = validate_chapter_titles(
        expected=(("001", "Ravens"), ("002", "Twilight")),
        records=(_record("001", 1, "Ravens", "کلاغ‌ها"),),
        registry=_empty_registry(),
    )

    assert not report.valid
    assert [finding.code for finding in report.findings] == ["missing_title"]
    assert report.findings[0].chapter_id == "002"


def test_qa_fails_an_empty_or_untranslated_or_non_persian_title() -> None:
    report = validate_chapter_titles(
        expected=(("001", "Ravens"),),
        records=(_record("001", 1, "Ravens", "Corvus"),),
        registry=_empty_registry(),
    )

    assert not report.valid
    assert "not_target_script" in {finding.code for finding in report.findings}
    # An empty or untranslated target title never reaches QA: the contract refuses the record.
    with pytest.raises(ValueError, match="min_length|at least 1"):
        _record("001", 1, "Ravens", "")
    with pytest.raises(ValueError, match="must differ from its source"):
        _record("001", 1, "Ravens", "Ravens")


def test_qa_fails_when_a_registry_name_is_not_preserved() -> None:
    registry = _empty_registry()
    registry = set_decision(
        registry,
        source_term="Rhuidean",
        target_term="روئیدان",
        classification=TerminologyType.PLACE,
        decision_source=DecisionSource.USER,
        locked=True,
    )

    kept = validate_chapter_titles(
        expected=(("001", "Rhuidean"),),
        records=(_record("001", 1, "Rhuidean", "روئیدان"),),
        registry=registry,
    )
    dropped = validate_chapter_titles(
        expected=(("001", "Rhuidean"),),
        records=(_record("001", 1, "Rhuidean", "شهر گمشده"),),
        registry=registry,
    )

    assert kept.valid
    assert kept.checked_terminology_decisions == 1
    assert not dropped.valid
    assert {finding.code for finding in dropped.findings} == {"terminology_not_preserved"}


def test_qa_fails_a_title_set_that_contradicts_itself() -> None:
    report = validate_chapter_titles(
        expected=(("001", "Ravens"), ("002", "Ravens")),
        records=(_record("001", 1, "Ravens", "کلاغ‌ها"), _record("002", 2, "Ravens", "زاغ‌ها")),
        registry=_empty_registry(),
    )

    assert not report.valid
    assert "inconsistent_duplicate_title" in {finding.code for finding in report.findings}


def test_qa_fails_a_chapter_the_snapshot_never_froze_or_a_drifted_source_title() -> None:
    unknown = validate_chapter_titles(
        expected=(("001", "Ravens"),),
        records=(_record("001", 1, "Ravens", "کلاغ‌ها"), _record("009", 2, "Ghosts", "ارواح")),
        registry=_empty_registry(),
    )
    drifted = validate_chapter_titles(
        expected=(("001", "Crows"),),
        records=(_record("001", 1, "Ravens", "کلاغ‌ها"),),
        registry=_empty_registry(),
    )

    assert "unknown_chapter" in {finding.code for finding in unknown.findings}
    assert "source_title_drift" in {finding.code for finding in drifted.findings}


def test_qa_names_a_chapter_and_a_code_but_never_a_title() -> None:
    report = validate_chapter_titles(
        expected=(("001", "Ravens"),),
        records=(_record("001", 1, "Ravens", "Ravens Extra"),),
        registry=_empty_registry(),
    )

    assert not report.valid
    for finding in report.findings:
        assert "Ravens" not in finding.message


# --------------------------------------------------------------------------------------------
# Translation: coverage, determinism, deduplication
# --------------------------------------------------------------------------------------------


def test_every_chapter_receives_a_title(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=3)

    path, provider = _translate_titles(root, run_id)

    artifact = load_chapter_title_artifact(path)
    assert artifact.document.chapter_count == 3
    assert sorted(artifact.overlay()) == ["001", "002", "003"]
    assert all(value == SYNTHETIC_TITLE_FA for value in artifact.overlay().values())
    assert artifact.manifest.qa_valid is True
    # All three chapters share one English title, so one call answers the book.
    assert len(provider.requests) == 1
    assert artifact.manifest.provider_calls == 1
    assert artifact.manifest.translated_title_count == 1
    assert artifact.manifest.deduplicated_title_count == 2


def test_one_english_title_never_produces_two_persian_titles(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=3)

    # A provider that answers differently every time cannot make the artifact disagree with
    # itself, because an identical title is asked exactly once.
    class DriftingProvider(TitleProvider):
        def execute(self, request: ProviderRequest) -> ProviderResult:
            result = super().execute(request)
            return ProviderResult(
                response_id=result.response_id,
                model=result.model,
                status="completed",
                text_outputs=(f"{SYNTHETIC_TITLE_FA}{'ی' * len(self.requests)}",),
                usage=result.usage,
                sanitized_response=result.sanitized_response,
            )

    path, provider = _translate_titles(root, run_id, DriftingProvider())

    titles = set(load_chapter_title_artifact(path).overlay().values())
    assert len(titles) == 1
    assert len(provider.requests) == 1


def test_a_chapter_with_no_source_title_stops_the_stage(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=2)
    metadata = root / "workspace/chapters/002/metadata.yaml"
    stored = yaml.safe_load(metadata.read_text(encoding="utf-8"))
    stored["title_en"] = "   "
    metadata.write_text(
        yaml.safe_dump(stored, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    # The snapshot is frozen, so the plan reads the frozen title; blank the frozen one instead.
    snapshot = ProjectPaths(root).translation_snapshot(
        yaml.safe_load((_run_path(root, run_id) / "run.yaml").read_text(encoding="utf-8"))[
            "snapshot_id"
        ]
    )
    manifest = yaml.safe_load((snapshot / "manifest.yaml").read_text(encoding="utf-8"))
    manifest["chapters"][1]["title_en"] = ""
    (snapshot / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises((ChapterTitleError, ValueError)):
        plan_chapter_titles(run_id, project_root=root)


def test_a_provider_answer_that_is_not_a_heading_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=1)

    with pytest.raises(ChapterTitleError, match="more than one line"):
        _translate_titles(root, run_id, TitleProvider(default="عنوان\n\nو توضیح"))
    assert not ProjectPaths(root).chapter_title_artifacts.exists()


def test_an_english_answer_is_refused_by_qa_before_publication(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=1)

    with pytest.raises(ChapterTitleError, match="not_target_script"):
        _translate_titles(root, run_id, TitleProvider(default="Still English"))
    assert not ProjectPaths(root).chapter_title_artifacts.exists()


def test_a_recorded_target_title_is_never_sent_to_a_provider(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run_with_titles(root, title_fa={"001": "عنوان دست‌نویس"})

    plan = plan_chapter_titles(run_id, project_root=root)
    assert plan.planned_provider_calls == 0
    result = execute_chapter_titles(
        plan, project_root=root, provider=RefusingProvider(), now=lambda: TITLE_CLOCK
    )

    assert result.provider_calls == 0
    assert result.records[0].title_fa == "عنوان دست‌نویس"
    assert result.records[0].origin is ChapterTitleOrigin.RECORDED_TITLE


# --------------------------------------------------------------------------------------------
# Determinism and immutability
# --------------------------------------------------------------------------------------------


def test_the_same_answers_reproduce_the_same_artifact_bytes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=2)

    first_path, _ = _translate_titles(root, run_id)
    first_document = (first_path / DOCUMENT_FILENAME).read_bytes()
    first = load_chapter_title_artifact(first_path)

    plan = plan_chapter_titles(run_id, project_root=root, force=True)
    execute_chapter_titles(
        plan,
        project_root=root,
        provider=TitleProvider(),
        now=lambda: TITLE_CLOCK + timedelta(days=1),
    )
    second = load_chapter_title_artifact(first_path)

    assert first.artifact_id == second.artifact_id
    assert first.content_sha256 == second.content_sha256
    assert (first_path / DOCUMENT_FILENAME).read_bytes() == first_document
    assert first.manifest.created_at != second.manifest.created_at


def test_a_published_artifact_is_never_replaced_without_force(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=1)
    path, _ = _translate_titles(root, run_id)
    published = (path / DOCUMENT_FILENAME).read_bytes()

    with pytest.raises(ChapterTitleError, match="--force"):
        execute_chapter_titles(
            plan_chapter_titles(run_id, project_root=root),
            project_root=root,
            provider=TitleProvider(default="عنوان دیگر"),
            now=lambda: TITLE_CLOCK,
        )

    assert (path / DOCUMENT_FILENAME).read_bytes() == published


def test_force_refuses_a_directory_that_is_not_this_artifact(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=1)
    plan = plan_chapter_titles(run_id, project_root=root, force=True)
    plan.output_path.mkdir(parents=True)
    (plan.output_path / "notes.txt").write_text("not an artifact\n", encoding="utf-8")

    with pytest.raises(ChapterTitleError, match="not a chapter-title artifact"):
        execute_chapter_titles(
            plan, project_root=root, provider=TitleProvider(), now=lambda: TITLE_CLOCK
        )
    assert (plan.output_path / "notes.txt").is_file()


def test_a_tampered_artifact_file_is_detected_on_load(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=1)
    path, _ = _translate_titles(root, run_id)
    document = path / DOCUMENT_FILENAME
    document.chmod(0o600)
    document.write_text(document.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ChapterTitleError, match="changed after publication"):
        load_chapter_title_artifact(path)


def test_an_artifact_may_never_be_written_into_canonical_workspace_state(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=1)

    for destination in (
        "workspace/chapters/001",
        "workspace/translation-runs/anything",
        "workspace/snapshots/translation",
        "workspace/releases/book-fa-v1",
    ):
        with pytest.raises(ChapterTitleError, match="canonical workspace state"):
            plan_chapter_titles(run_id, project_root=root, output=destination)


# --------------------------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------------------------


def test_provenance_records_every_title_without_prompts_or_responses(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=3)
    path, _ = _translate_titles(root, run_id)

    provenance = load_chapter_title_provenance(path)
    raw = (path / PROVENANCE_FILENAME).read_text(encoding="utf-8")

    assert provenance.chapter_count == 3
    assert [record.chapter_id for record in provenance.titles] == ["001", "002", "003"]
    assert provenance.qa.valid
    assert provenance.source_translation_run == run_id
    translated = [
        record
        for record in provenance.titles
        if record.origin is ChapterTitleOrigin.PROVIDER_TRANSLATION
    ]
    assert len(translated) == 1
    assert translated[0].provider == "fake"
    assert translated[0].model == "fake-v1"
    assert translated[0].translated_at is not None
    assert translated[0].request_sha256 is not None
    reused = [
        record for record in provenance.titles if record.origin is ChapterTitleOrigin.DEDUPLICATED
    ]
    assert all(record.deduplicated_from_chapter_id == "001" for record in reused)
    # No prompt, no response body, no framing.
    assert "canonical_terminology" not in raw
    assert "system_message" not in raw and "user_message" not in raw
    assert "sanitized_response" not in raw


# --------------------------------------------------------------------------------------------
# Zero translation-run mutation
# --------------------------------------------------------------------------------------------


def test_translating_titles_mutates_no_translation_state(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=2)
    before = _workspace_fingerprint(root)

    _translate_titles(root, run_id)

    after = _workspace_fingerprint(root)
    added = set(after) - set(before)
    assert all(path.startswith("workspace/resources/chapter-titles/") for path in added)
    assert {path: after[path] for path in before} == before


def test_a_dry_run_plan_writes_nothing_and_contacts_nobody(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=2)
    before = _workspace_fingerprint(root)

    plan = plan_chapter_titles(run_id, project_root=root)

    assert plan.planned_provider_calls == 1
    assert plan.chapter_count == 2
    assert all(item.system_message and item.user_message for item in plan.titles)
    assert _workspace_fingerprint(root) == before
    assert not ProjectPaths(root).chapter_title_artifacts.exists()


# --------------------------------------------------------------------------------------------
# Release integration
# --------------------------------------------------------------------------------------------


def test_headings_prefer_the_workspace_then_the_artifact_then_the_source() -> None:
    assert chapter_heading(title_target="یک", title_source="One", title_artifact="آرتیفکت") == (
        "# یک",
        ReleaseHeadingSource.TITLE_TARGET,
    )
    assert chapter_heading(title_target="", title_source="One", title_artifact="آرتیفکت") == (
        "# آرتیفکت",
        ReleaseHeadingSource.TITLE_TARGET_ARTIFACT,
    )
    assert chapter_heading(title_target="", title_source="One", title_artifact=None) == (
        "# One",
        ReleaseHeadingSource.TITLE_SOURCE,
    )


def test_a_release_without_a_title_artifact_keeps_its_english_headings(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=2)

    plan = plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)
    execute_release_assembly(plan, project_root=root)
    manifest = _manifest(plan.output_path)

    assert manifest.chapter_title_artifact_id is None
    assert manifest.chapter_title_artifact_sha256 is None
    assert all(
        record.heading_source is ReleaseHeadingSource.TITLE_SOURCE for record in manifest.chapters
    )
    assert manifest.chapters[0].heading == "# Synthetic"


def test_a_release_uses_the_title_artifact_when_one_exists(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=2)
    artifact_path, _ = _translate_titles(root, run_id)
    artifact = load_chapter_title_artifact(artifact_path)

    plan = plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)
    execute_release_assembly(plan, project_root=root)
    release = plan.output_path
    manifest = _manifest(release)

    assert manifest.chapter_title_artifact_id == artifact.artifact_id
    assert manifest.chapter_title_artifact_sha256 == artifact.content_sha256
    assert all(
        record.heading_source is ReleaseHeadingSource.TITLE_TARGET_ARTIFACT
        for record in manifest.chapters
    )
    assert manifest.chapters[0].heading == f"# {SYNTHETIC_TITLE_FA}"
    chapter = (release / manifest.chapters[0].file).read_text(encoding="utf-8")
    assert chapter == f"# {SYNTHETIC_TITLE_FA}\n\n{TRANSLATION}"
    assert (release / "book.md").read_text(encoding="utf-8").startswith(f"# {SYNTHETIC_TITLE_FA}\n")


def test_the_export_can_be_told_to_ignore_a_title_artifact(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=1)
    _translate_titles(root, run_id)

    plan = plan_release_assembly(
        run_id,
        output="workspace/releases/book-fa-v1",
        project_root=root,
        use_chapter_titles=False,
    )

    assert plan.manifest.chapter_title_artifact_id is None
    assert plan.manifest.chapters[0].heading == "# Synthetic"


def test_a_release_refuses_a_title_artifact_that_no_longer_passes_qa(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=2)
    artifact_path, _ = _translate_titles(root, run_id)
    # Drop a chapter from the published set and re-index it, so the artifact is internally
    # consistent but no longer covers the snapshot.
    document = yaml.safe_load((artifact_path / DOCUMENT_FILENAME).read_text(encoding="utf-8"))
    document["chapters"] = document["chapters"][:1]
    document["chapter_count"] = 1
    serialized = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    (artifact_path / DOCUMENT_FILENAME).chmod(0o600)
    (artifact_path / DOCUMENT_FILENAME).write_text(serialized, encoding="utf-8")
    manifest = yaml.safe_load((artifact_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    for reference in manifest["artifacts"]:
        if reference["file"] == DOCUMENT_FILENAME:
            reference["sha256"] = _hash(serialized.encode("utf-8"))
            reference["byte_count"] = len(serialized.encode("utf-8"))
    (artifact_path / MANIFEST_FILENAME).chmod(0o600)
    (artifact_path / MANIFEST_FILENAME).write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ReleaseAssemblyError, match="missing_title"):
        plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)


def test_two_artifacts_for_one_snapshot_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=1)
    first, _ = _translate_titles(root, run_id)
    rival = ProjectPaths(root).chapter_title_artifacts / ("ct-" + "f" * 24)
    rival.mkdir(parents=True)
    for name in (DOCUMENT_FILENAME, MANIFEST_FILENAME, PROVENANCE_FILENAME):
        (rival / name).write_bytes((first / name).read_bytes())

    with pytest.raises(ChapterTitleError, match="more than one"):
        discover_chapter_title_artifact(
            project_root=root,
            snapshot_id=load_chapter_title_artifact(first).manifest.snapshot_id,
            snapshot_manifest_sha256=(
                load_chapter_title_artifact(first).manifest.snapshot_manifest_sha256
            ),
        )


def test_the_export_still_contacts_no_provider_with_a_title_artifact_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=2)
    _translate_titles(root, run_id)

    from book_translator.provider_execution import providers

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("release assembly contacted a provider")

    for adapter in (
        "ClaudeCodeTranslationProvider",
        "CodexCliTranslationProvider",
        "FakeTranslationProvider",
        "OpenAIResponsesProvider",
    ):
        monkeypatch.setattr(providers, adapter, refuse)

    plan = plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)
    execute_release_assembly(plan, project_root=root)

    assert _manifest(plan.output_path).chapter_title_artifact_id is not None


def test_the_release_stage_still_holds_no_path_to_a_provider() -> None:
    package = Path(__file__).parents[1] / "src/book_translator/release_assembly"
    for path in package.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "provider_execution" not in source, f"{path.name} reaches the provider boundary"


def test_a_release_with_titles_stays_reproducible(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=2)
    _translate_titles(root, run_id)

    first = plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)
    execute_release_assembly(first, project_root=root)
    second = plan_release_assembly(
        run_id, output="workspace/releases/book-fa-v1", project_root=root, force=True
    )
    execute_release_assembly(second, project_root=root)

    assert first.manifest.content_sha256 == second.manifest.content_sha256
    assert [item.model_dump() for item in first.manifest.artifacts] == [
        item.model_dump() for item in second.manifest.artifacts
    ]
    lineage = json.loads((first.output_path / LINEAGE_FILENAME).read_text(encoding="utf-8"))
    assert lineage["chunk_count"] == first.manifest.chunk_count


def test_exporting_with_titles_mutates_nothing_outside_the_release(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=2)
    _translate_titles(root, run_id)
    before = _workspace_fingerprint(root)

    execute_release_assembly(
        plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root),
        project_root=root,
    )

    assert _workspace_fingerprint(root) == before


# --------------------------------------------------------------------------------------------
# Terminology preservation end to end
# --------------------------------------------------------------------------------------------


def test_a_registry_name_survives_into_the_release_heading(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _run_with_titles(
        root,
        title_en={"001": "The Road to Rhuidean"},
        registry=("Rhuidean", "روئیدان"),
    )

    plan = plan_chapter_titles(run_id, project_root=root)
    assert plan.titles[0].enforced_decision_ids
    assert "روئیدان" in plan.titles[0].user_message

    execute_chapter_titles(
        plan,
        project_root=root,
        provider=TitleProvider({"The Road to Rhuidean": "راه روئیدان"}),
        now=lambda: TITLE_CLOCK,
    )
    release = plan_release_assembly(
        run_id, output="workspace/releases/book-fa-v1", project_root=root
    )
    execute_release_assembly(release, project_root=root)

    assert release.manifest.chapters[0].heading == "# راه روئیدان"
    assert release.manifest.chapter_title_artifact_id is not None


def test_a_title_that_drops_a_registry_name_is_refused_before_publication(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id = _run_with_titles(
        root,
        title_en={"001": "The Road to Rhuidean"},
        registry=("Rhuidean", "روئیدان"),
    )

    with pytest.raises(ChapterTitleError, match="terminology_not_preserved"):
        execute_chapter_titles(
            plan_chapter_titles(run_id, project_root=root),
            project_root=root,
            provider=TitleProvider({"The Road to Rhuidean": "راه شهر گمشده"}),
            now=lambda: TITLE_CLOCK,
        )
    assert not ProjectPaths(root).chapter_title_artifacts.exists()


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def test_the_cli_dry_run_reports_zero_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=2)
    monkeypatch.chdir(root)

    result = runner.invoke(
        app, ["translate-chapter-titles", run_id, "--provider", "fake", "--dry-run", "--show-plan"]
    )

    assert result.exit_code == 0, result.stdout
    assert "Provider calls: 0" in result.stdout
    assert "Files written: 0" in result.stdout
    assert "mutations: 0" in result.stdout
    assert not ProjectPaths(root).chapter_title_artifacts.exists()


def test_the_cli_requires_a_provider_unless_it_is_a_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=1)
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["translate-chapter-titles", run_id])

    assert result.exit_code == 1
    assert not ProjectPaths(root).chapter_title_artifacts.exists()


def test_the_release_manifest_shows_the_title_artifact_in_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    run_id = _titled_run(root, chapters=1)
    artifact_path, _ = _translate_titles(root, run_id)
    monkeypatch.chdir(root)

    result = runner.invoke(
        app,
        ["export-translation-run", run_id, "--output", "workspace/releases/book-fa-v1"],
    )

    assert result.exit_code == 0, result.stdout
    assert load_chapter_title_artifact(artifact_path).artifact_id[:12] in result.stdout.replace(
        "\n", ""
    )
