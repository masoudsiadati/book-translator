"""Provider-free revision of a published chapter-title artifact."""

from pathlib import Path

import pytest
import yaml

from book_translator.chapter_titles import ChapterTitleError
from book_translator.chapter_titles.artifact import (
    discover_chapter_title_artifact,
    load_chapter_title_artifact,
    load_chapter_title_provenance,
)
from book_translator.chapter_titles.revision import (
    CHAPTER_TITLE_REVISION_VERSION,
    TitleOverride,
    revision_identity,
)
from book_translator.chapter_titles.revision_workflow import (
    ChapterTitleRevisionPlan,
    ChapterTitleRevisionResult,
    execute_chapter_title_revision,
    plan_chapter_title_revision,
)
from book_translator.models.chapter_title import ChapterTitleOrigin
from tests.test_chapter_titles import TITLE_CLOCK, _run_with_titles, _translate_titles

CORRECTED = "عنوان اصلاح‌شده"
REASON = "the published title diverges from the governed prose"


def _artifact_bytes(path: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(path.iterdir()) if p.is_file()}


DISTINCT = {"001": "Lion on the Hill", "002": "A New Arrival"}


def _published(root: Path, *, title_en: dict[str, str] | None = DISTINCT) -> tuple[Path, str]:
    run_id = _run_with_titles(root, chapters=2, title_en=title_en)
    parent_path, _ = _translate_titles(root, run_id)
    return parent_path, run_id


def _revise(
    root: Path, parent_path: Path, overrides: dict[str, TitleOverride], *, apply: bool = True
) -> tuple[ChapterTitleRevisionPlan, ChapterTitleRevisionResult | None]:
    plan = plan_chapter_title_revision(parent_path.name, project_root=root, overrides=overrides)
    if not apply:
        return plan, None
    return plan, execute_chapter_title_revision(plan, now=lambda: TITLE_CLOCK)


def _override(chapter_id: str, title: str = CORRECTED) -> dict[str, TitleOverride]:
    return {chapter_id: TitleOverride(chapter_id=chapter_id, title_fa=title, reason=REASON)}


# --- the derived artifact ---------------------------------------------------------------------


def test_a_revision_replaces_only_the_named_title_and_carries_the_rest(tmp_path: Path) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)
    parent = load_chapter_title_artifact(parent_path)
    target = parent.document.chapters[0].chapter_id
    before = parent.overlay()

    plan, result = _revise(root, parent_path, _override(target))
    assert result is not None
    revision = load_chapter_title_artifact(result.output_path)

    after = revision.overlay()
    assert after[target] == CORRECTED
    assert {k: v for k, v in after.items() if k != target} == {
        k: v for k, v in before.items() if k != target
    }
    assert revision.document.parent_artifact_id == parent.artifact_id
    assert revision.manifest.parent_artifact_id == parent.artifact_id
    assert revision.manifest.chapter_title_revision_version == CHAPTER_TITLE_REVISION_VERSION
    assert revision.manifest.provider_calls == 0
    assert revision.manifest.override_title_count == 1
    assert revision.manifest.inherited_title_count == len(after) - 1
    assert result.provider_calls == 0
    assert [record.chapter_sequence for record in revision.document.chapters] == list(
        range(1, len(after) + 1)
    )
    assert all(record.title_fa.strip() for record in revision.document.chapters)


def test_the_parent_artifact_is_never_touched(tmp_path: Path) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)
    before = _artifact_bytes(parent_path)
    target = load_chapter_title_artifact(parent_path).document.chapters[0].chapter_id

    _, result = _revise(root, parent_path, _override(target))

    assert _artifact_bytes(parent_path) == before
    assert result is not None and result.output_path != parent_path


def test_a_revision_is_addressed_by_its_parent_and_its_overrides(tmp_path: Path) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)
    parent = load_chapter_title_artifact(parent_path)
    target = parent.document.chapters[0].chapter_id
    expected, _ = revision_identity(
        parent_artifact_id=parent.artifact_id,
        parent_content_sha256=parent.content_sha256,
        overrides=(TitleOverride(target, CORRECTED, REASON),),
    )

    plan, _ = _revise(root, parent_path, _override(target), apply=False)

    assert plan.artifact_id == expected
    assert plan.artifact_id != parent.artifact_id
    other, _ = revision_identity(
        parent_artifact_id=parent.artifact_id,
        parent_content_sha256=parent.content_sha256,
        overrides=(TitleOverride(target, CORRECTED + "ی", REASON),),
    )
    assert other != expected


def test_provenance_records_the_parent_the_replacement_and_the_carried_history(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)
    parent = load_chapter_title_artifact(parent_path)
    parent_history = {
        record.chapter_id: record for record in load_chapter_title_provenance(parent_path).titles
    }
    target = parent.document.chapters[0].chapter_id

    _, result = _revise(root, parent_path, _override(target))
    assert result is not None
    history = {
        record.chapter_id: record
        for record in load_chapter_title_provenance(result.output_path).titles
    }

    replaced = history[target]
    assert replaced.origin is ChapterTitleOrigin.OPERATOR_OVERRIDE
    assert replaced.parent_artifact_id == parent.artifact_id
    assert replaced.superseded_title_fa == parent.overlay()[target]
    assert replaced.override_reason == REASON
    assert replaced.decided_by == "user"
    assert replaced.provider is None and replaced.translated_at is None
    carried = [record for chapter_id, record in history.items() if chapter_id != target]
    for record in carried:
        assert record.origin is ChapterTitleOrigin.INHERITED
        assert record.parent_artifact_id == parent.artifact_id
        # The carried title is still the provider's answer and still says whose it was.
        assert record.provider == parent_history[record.chapter_id].provider
        assert record.superseded_title_fa is None


# --- the release resolver ----------------------------------------------------------------------


def test_the_resolver_selects_the_revision_over_its_parent(tmp_path: Path) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)
    parent = load_chapter_title_artifact(parent_path)
    target = parent.document.chapters[0].chapter_id

    _, result = _revise(root, parent_path, _override(target))
    assert result is not None
    found = discover_chapter_title_artifact(
        project_root=root,
        snapshot_id=parent.document.snapshot_id,
        snapshot_manifest_sha256=parent.document.snapshot_manifest_sha256,
    )

    assert found is not None
    assert found.artifact_id == result.artifact_id
    assert found.overlay()[target] == CORRECTED


def test_a_chain_of_revisions_resolves_to_its_head(tmp_path: Path) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)
    parent = load_chapter_title_artifact(parent_path)
    first, second = (record.chapter_id for record in parent.document.chapters[:2])

    _, one = _revise(root, parent_path, _override(first))
    assert one is not None
    _, two = _revise(root, one.output_path, _override(second, CORRECTED + " دو"))
    assert two is not None

    found = discover_chapter_title_artifact(
        project_root=root,
        snapshot_id=parent.document.snapshot_id,
        snapshot_manifest_sha256=parent.document.snapshot_manifest_sha256,
    )
    assert found is not None and found.artifact_id == two.artifact_id
    assert found.overlay()[first] == CORRECTED
    assert found.overlay()[second] == CORRECTED + " دو"


# --- fail closed ---------------------------------------------------------------------------------


def test_an_unknown_chapter_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)

    with pytest.raises(ChapterTitleError, match="not in the parent artifact"):
        _revise(root, parent_path, _override("042"), apply=False)


def test_an_empty_revision_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)

    with pytest.raises(ChapterTitleError, match="at least one title"):
        _revise(root, parent_path, {}, apply=False)


def test_replacing_a_title_with_itself_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)
    parent = load_chapter_title_artifact(parent_path)
    target = parent.document.chapters[0].chapter_id

    with pytest.raises(ChapterTitleError, match="equals the published title"):
        _revise(root, parent_path, _override(target, parent.overlay()[target]), apply=False)


@pytest.mark.parametrize("title", ["", "   ", "two\nlines"])
def test_a_malformed_title_is_refused(tmp_path: Path, title: str) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)
    target = load_chapter_title_artifact(parent_path).document.chapters[0].chapter_id

    with pytest.raises(ChapterTitleError):
        _revise(root, parent_path, _override(target, title), apply=False)


def test_a_missing_reason_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)
    target = load_chapter_title_artifact(parent_path).document.chapters[0].chapter_id

    with pytest.raises(ChapterTitleError, match="records why"):
        _revise(
            root,
            parent_path,
            {target: TitleOverride(target, CORRECTED, "   ")},
            apply=False,
        )


def test_a_tampered_parent_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)
    document = parent_path / "chapter-titles.yaml"
    document.write_text(document.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    target = "001"

    with pytest.raises(ChapterTitleError, match="changed after publication"):
        _revise(root, parent_path, _override(target), apply=False)


def test_republishing_the_same_revision_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)
    target = load_chapter_title_artifact(parent_path).document.chapters[0].chapter_id
    _revise(root, parent_path, _override(target))

    with pytest.raises(ChapterTitleError, match="already published"):
        _revise(root, parent_path, _override(target), apply=False)


def test_planning_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)
    target = load_chapter_title_artifact(parent_path).document.chapters[0].chapter_id
    before = {p: p.read_bytes() for p in sorted(parent_path.parent.rglob("*")) if p.is_file()}

    plan, _ = _revise(root, parent_path, _override(target), apply=False)

    assert not plan.destination.exists()
    assert {
        p: p.read_bytes() for p in sorted(parent_path.parent.rglob("*")) if p.is_file()
    } == before


def test_splitting_two_identical_english_titles_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    # Both chapters keep the stock identical English title, so the artifact gives them one
    # Persian title. Correcting only one of them would make the book disagree with itself.
    parent_path, _ = _published(root, title_en=None)
    target = load_chapter_title_artifact(parent_path).document.chapters[0].chapter_id

    with pytest.raises(ChapterTitleError, match="inconsistent_duplicate_title"):
        _revise(root, parent_path, _override(target), apply=False)


# --- the translated artifact's own contract is unchanged -----------------------------------------


def test_a_translated_artifact_records_no_revision_lineage(tmp_path: Path) -> None:
    root = tmp_path / "project"
    parent_path, _ = _published(root)
    manifest = yaml.safe_load((parent_path / "manifest.yaml").read_text(encoding="utf-8"))

    # The additive fields are omitted, not null, so a published artifact keeps its exact bytes.
    for field in (
        "parent_artifact_id",
        "chapter_title_revision_version",
        "inherited_title_count",
        "override_title_count",
    ):
        assert field not in manifest
    assert load_chapter_title_artifact(parent_path).manifest.parent_artifact_id is None
