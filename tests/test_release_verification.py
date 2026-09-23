"""Release verification: the gate between an assembled release and publication.

Every fixture is synthetic. Each failure test damages exactly one thing and proves the verifier
names that thing — a verifier that fails for the wrong reason is not evidence of anything.
"""

import hashlib
import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models.release import ReleaseArtifactReference
from book_translator.models.release_verification import (
    RELEASE_CHECK_ORDER,
    ReleaseCheck,
    ReleaseCheckStatus,
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
    execute_release_assembly,
    plan_release_assembly,
    release_content_address,
)
from book_translator.release_verification import (
    RELEASE_VERIFICATION_ENGINE_VERSION,
    ReleaseVerificationError,
    serialize_verification,
    verify_release_artifact,
    write_verification_report,
)
from tests.test_chapter_titles import TitleProvider, _translate_titles
from tests.test_release_assembly import _accepted_run, _workspace_fingerprint

runner = CliRunner()

CLOCK = datetime(2026, 5, 6, 7, 8, tzinfo=UTC)
LATER = datetime(2027, 9, 9, 9, 9, tzinfo=UTC)


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _release(root: Path, *, chapters: int = 2, with_titles: bool = False) -> Path:
    """A published release, optionally carrying translated chapter headings."""
    run_id = _accepted_run(root, chapters=chapters)
    if with_titles:
        _translate_titles(root, run_id, TitleProvider())
    plan = plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)
    execute_release_assembly(plan, project_root=root)
    return plan.output_path


def _release_with_terminology(root: Path) -> Path:
    """A release whose snapshot froze a terminology registry, so it publishes a copy of one."""
    from tests.test_chapter_titles import _run_with_titles

    run_id = _run_with_titles(
        root, title_en={"001": "The Road to Rhuidean"}, registry=("Rhuidean", "روئیدان")
    )
    plan = plan_release_assembly(run_id, output="workspace/releases/book-fa-v1", project_root=root)
    execute_release_assembly(plan, project_root=root)
    return plan.output_path


def _verify(root: Path, release: Path, *, now: datetime = CLOCK) -> ReleaseVerificationReport:
    return verify_release_artifact(release, project_root=root, now=lambda: now).report


def _rewrite(path: Path, content: str) -> None:
    """Overwrite one published release file, which is otherwise mode 0600."""
    path.chmod(0o600)
    path.write_text(content, encoding="utf-8")


def _reindex(release: Path, name: str) -> None:
    """Re-index one changed file and re-address the manifest, so only the named damage remains.

    Without re-deriving ``content_sha256`` — and the chapter record's own hash when the changed
    file is a chapter — every one of these tests would trip the manifest's self-address check and
    prove nothing about the check it is actually aiming at.
    """
    manifest_path = release / MANIFEST_FILENAME
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    content = (release / name).read_bytes()
    for artifact in manifest["artifacts"]:
        if artifact["file"] == name:
            artifact["sha256"] = _hash(content)
            artifact["byte_count"] = len(content)
    for chapter in manifest["chapters"]:
        if chapter["file"] == name:
            chapter["sha256"] = _hash(content)
    manifest["content_sha256"] = release_content_address(
        tuple(ReleaseArtifactReference.model_validate(item) for item in manifest["artifacts"])
    )
    _rewrite(manifest_path, yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))


def _status(report: ReleaseVerificationReport, check: ReleaseCheck) -> ReleaseCheckStatus:
    return next(item.status for item in report.checks if item.check is check)


def _codes(report: ReleaseVerificationReport, check: ReleaseCheck) -> set[str]:
    return {item.code for item in report.findings if item.check is check}


# --------------------------------------------------------------------------------------------
# 1. A valid release passes
# --------------------------------------------------------------------------------------------


def test_a_published_release_is_ready_for_publication(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=3)

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.READY_FOR_PUBLICATION
    assert report.findings == []
    assert report.failed_checks == []
    assert report.skipped_checks == []
    assert [item.check for item in report.checks] == list(RELEASE_CHECK_ORDER)
    assert all(item.status is ReleaseCheckStatus.PASS for item in report.checks)
    assert report.files_verified == report.files_expected
    assert report.chapters_verified == report.chapters_expected == 3
    assert report.chunks_verified == report.chunks_expected == 3
    assert report.release_verification_engine_version == RELEASE_VERIFICATION_ENGINE_VERSION
    assert report.workspace_reachable is True


def test_a_release_with_translated_headings_is_ready(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2, with_titles=True)

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.READY_FOR_PUBLICATION
    assert report.chapter_title_artifact_id is not None
    assert report.titles_verified == report.titles_expected == 2
    assert _status(report, ReleaseCheck.TITLES) is ReleaseCheckStatus.PASS


# --------------------------------------------------------------------------------------------
# 2. A missing manifest fails
# --------------------------------------------------------------------------------------------


def test_a_directory_without_a_manifest_is_not_a_release(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    (release / MANIFEST_FILENAME).unlink()

    with pytest.raises(ReleaseVerificationError, match="no release manifest"):
        _verify(root, release)


def test_a_missing_release_directory_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _release(root, chapters=1)

    with pytest.raises(ReleaseVerificationError, match="no release directory"):
        _verify(root, root / "workspace/releases/absent")


# --------------------------------------------------------------------------------------------
# 3 and 4. Modified files
# --------------------------------------------------------------------------------------------


def test_a_file_changed_after_publication_fails_the_manifest_check(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)
    report_before = _verify(root, release)
    _rewrite(release / METADATA_FILENAME, "tampered: true\n")

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert _status(report, ReleaseCheck.MANIFEST) is ReleaseCheckStatus.FAIL
    assert "file_hash_mismatch" in _codes(report, ReleaseCheck.MANIFEST)
    assert report.content_sha256 != report_before.content_sha256


def test_a_missing_indexed_file_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release_with_terminology(root)
    (release / TERMINOLOGY_FILENAME).unlink()

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert "missing_file" in _codes(report, ReleaseCheck.MANIFEST)
    assert "missing_terminology" in _codes(report, ReleaseCheck.TERMINOLOGY)


def test_a_file_the_manifest_never_indexed_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    (release / "stray.md").write_text("سلام\n", encoding="utf-8")

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert "unindexed_file" in _codes(report, ReleaseCheck.MANIFEST)


def test_a_manifest_that_does_not_address_itself_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    manifest_path = release / MANIFEST_FILENAME
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["content_sha256"] = "0" * 64
    _rewrite(manifest_path, yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))

    report = _verify(root, release)

    assert "content_address_mismatch" in _codes(report, ReleaseCheck.MANIFEST)


def test_a_modified_book_fails_book_integrity(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)
    book = release / BOOK_FILENAME
    _rewrite(book, book.read_text(encoding="utf-8") + "\nپاراگراف اضافه\n")
    _reindex(release, BOOK_FILENAME)

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert _status(report, ReleaseCheck.MANIFEST) is ReleaseCheckStatus.PASS
    assert _status(report, ReleaseCheck.BOOK_INTEGRITY) is ReleaseCheckStatus.FAIL
    assert "book_is_not_its_chapters" in _codes(report, ReleaseCheck.BOOK_INTEGRITY)


def test_a_book_that_is_not_valid_utf8_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    book = release / BOOK_FILENAME
    book.chmod(0o600)
    book.write_bytes(b"\xff\xfe not utf-8\n")
    _reindex(release, BOOK_FILENAME)

    report = _verify(root, release)

    assert "invalid_utf8" in _codes(report, ReleaseCheck.BOOK_INTEGRITY)


# --------------------------------------------------------------------------------------------
# 5 and 6. Chapters
# --------------------------------------------------------------------------------------------


def test_a_missing_chapter_file_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)
    manifest = yaml.safe_load((release / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    (release / manifest["chapters"][1]["file"]).unlink()

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert "missing_chapter" in _codes(report, ReleaseCheck.CHAPTERS)
    assert report.chapters_verified == 1
    assert report.chapters_expected == 2


def test_a_duplicate_chapter_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)
    manifest_path = release / MANIFEST_FILENAME
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["chapters"][1]["chapter_id"] = manifest["chapters"][0]["chapter_id"]
    _rewrite(manifest_path, yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert "duplicate_chapter" in _codes(report, ReleaseCheck.CHAPTERS)


def test_a_chapter_whose_heading_was_changed_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    manifest = yaml.safe_load((release / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    chapter = release / manifest["chapters"][0]["file"]
    body = chapter.read_text(encoding="utf-8").split("\n\n", 1)[1]
    _rewrite(chapter, f"# عنوان دیگر\n\n{body}")
    _reindex(release, manifest["chapters"][0]["file"])

    report = _verify(root, release)

    assert "heading_mismatch" in _codes(report, ReleaseCheck.CHAPTERS)


# --------------------------------------------------------------------------------------------
# 7 and 10. Provenance and translation coverage
# --------------------------------------------------------------------------------------------


def _rewrite_lineage(release: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    path = release / LINEAGE_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    _rewrite(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _reindex(release, LINEAGE_FILENAME)


def test_a_release_without_provenance_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    (release / LINEAGE_FILENAME).unlink()

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert "missing_lineage" in _codes(report, ReleaseCheck.PROVENANCE)
    assert "missing_lineage" in _codes(report, ReleaseCheck.TRANSLATIONS)


def test_a_chunk_missing_from_the_lineage_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2)

    def drop(payload: dict[str, Any]) -> None:
        chunks = payload["chunks"]
        assert isinstance(chunks, list)
        del chunks[1]
        payload["chunk_count"] = len(chunks)

    _rewrite_lineage(release, drop)
    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert "chunk_count_mismatch" in _codes(report, ReleaseCheck.TRANSLATIONS)
    assert "incomplete_provenance" in _codes(report, ReleaseCheck.PROVENANCE)
    assert report.chunks_verified == 1
    assert report.chunks_expected == 2


def test_a_chunk_that_is_not_accepted_can_never_be_published(tmp_path: Path) -> None:
    """The lineage contract itself refuses a rejected chunk, so tampering makes it unreadable."""
    root = tmp_path / "project"
    release = _release(root, chapters=1)

    def reject(payload: dict[str, Any]) -> None:
        chunks = payload["chunks"]
        assert isinstance(chunks, list)
        chunks[0]["acceptance_status"] = "qa_rejected"

    _rewrite_lineage(release, reject)
    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert "unreadable_lineage" in _codes(report, ReleaseCheck.PROVENANCE)
    assert "missing_lineage" in _codes(report, ReleaseCheck.TRANSLATIONS)
    assert report.chunks_verified == 0


def test_a_paragraph_gap_between_chunks_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)

    def shift(payload: dict[str, Any]) -> None:
        chunks = payload["chunks"]
        assert isinstance(chunks, list)
        chunks[0]["paragraph_offset"] = 3

    _rewrite_lineage(release, shift)
    report = _verify(root, release)

    assert "paragraph_coverage_gap" in _codes(report, ReleaseCheck.TRANSLATIONS)


def test_a_contradictory_provider_resolution_can_never_be_published(tmp_path: Path) -> None:
    """A provider resolution that contradicts its provider is refused by the contract too."""
    root = tmp_path / "project"
    release = _release(root, chapters=1)

    def blank(payload: dict[str, Any]) -> None:
        chunks = payload["chunks"]
        assert isinstance(chunks, list)
        chunks[0]["provider"] = None
        chunks[0]["provider_resolution"] = "attempt"

    _rewrite_lineage(release, blank)
    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert "unreadable_lineage" in _codes(report, ReleaseCheck.PROVENANCE)


def test_lineage_describing_another_run_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)

    def relabel(payload: dict[str, Any]) -> None:
        payload["snapshot_manifest_sha256"] = "0" * 64

    _rewrite_lineage(release, relabel)
    report = _verify(root, release)

    assert "lineage_identity_mismatch" in _codes(report, ReleaseCheck.PROVENANCE)


# --------------------------------------------------------------------------------------------
# 8. Terminology
# --------------------------------------------------------------------------------------------


def test_a_released_terminology_copy_that_is_not_the_named_resource_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    release = _release_with_terminology(root)
    _rewrite(release / TERMINOLOGY_FILENAME, "tampered: true\n")
    _reindex(release, TERMINOLOGY_FILENAME)

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert _status(report, ReleaseCheck.MANIFEST) is ReleaseCheckStatus.PASS
    assert "terminology_hash_mismatch" in _codes(report, ReleaseCheck.TERMINOLOGY)


def test_a_release_is_checked_against_the_workspace_frozen_resource(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release_with_terminology(root)
    manifest = yaml.safe_load((release / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    frozen = ProjectPaths(root).terminology_resource(manifest["terminology_sha256"])
    frozen.chmod(0o600)
    frozen.write_text("drifted: true\n", encoding="utf-8")

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert "frozen_terminology_drift" in _codes(report, ReleaseCheck.TERMINOLOGY)


# --------------------------------------------------------------------------------------------
# 9. Title artifacts
# --------------------------------------------------------------------------------------------


def test_a_title_artifact_whose_content_address_moved_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1, with_titles=True)
    manifest_path = release / MANIFEST_FILENAME
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["chapter_title_artifact_sha256"] = "0" * 64
    _rewrite(manifest_path, yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert "title_artifact_hash_mismatch" in _codes(report, ReleaseCheck.TITLES)


def test_a_referenced_title_artifact_that_is_gone_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1, with_titles=True)
    manifest = yaml.safe_load((release / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    shutil.rmtree(ProjectPaths(root).chapter_title_artifact(manifest["chapter_title_artifact_id"]))

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert "title_artifact_unavailable" in _codes(report, ReleaseCheck.TITLES)


def test_a_heading_that_is_not_its_recorded_title_fails(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1, with_titles=True)
    manifest_path = release / MANIFEST_FILENAME
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    chapter = release / manifest["chapters"][0]["file"]
    body = chapter.read_text(encoding="utf-8").split("\n\n", 1)[1]
    manifest["chapters"][0]["heading"] = "# عنوان جعلی"
    _rewrite(manifest_path, yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))
    _rewrite(chapter, f"# عنوان جعلی\n\n{body}")
    _reindex(release, manifest["chapters"][0]["file"])

    report = _verify(root, release)

    assert "heading_is_not_its_title" in _codes(report, ReleaseCheck.TITLES)


def test_a_manifest_that_breaks_its_own_contract_is_reported_not_raised(
    tmp_path: Path,
) -> None:
    """An invalid index is a verdict, not a crash — and nothing downstream is called passing."""
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    manifest_path = release / MANIFEST_FILENAME
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["chapters"][0]["heading_source"] = "title_target_artifact"
    _rewrite(manifest_path, yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))

    report = _verify(root, release)

    assert report.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert _codes(report, ReleaseCheck.MANIFEST) == {"invalid_manifest_schema"}
    assert report.failed_checks == [ReleaseCheck.MANIFEST]
    assert report.skipped_checks == [
        check for check in RELEASE_CHECK_ORDER if check is not ReleaseCheck.MANIFEST
    ]
    assert not any(item.status is ReleaseCheckStatus.PASS for item in report.checks)


def test_a_copied_release_skips_workspace_evidence_rather_than_passing_it(
    tmp_path: Path,
) -> None:
    """A release verified away from its workspace is verified only as far as it can be."""
    root = tmp_path / "project"
    release = _release(root, chapters=1, with_titles=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copytree(release, elsewhere / "book-fa-v1")

    report = verify_release_artifact(
        elsewhere / "book-fa-v1", project_root=elsewhere, now=lambda: CLOCK
    )

    assert report.report.workspace_reachable is False
    assert report.report.skipped_checks == [ReleaseCheck.TITLES]
    assert _status(report.report, ReleaseCheck.TITLES) is ReleaseCheckStatus.SKIPPED
    assert report.report.titles_verified == 0
    # Everything provable from the bytes alone still passes, and the release is still gated on it.
    assert report.report.status is ReleaseVerificationStatus.READY_FOR_PUBLICATION
    assert report.report.failed_checks == []


# --------------------------------------------------------------------------------------------
# 11. Repeated verification is identical
# --------------------------------------------------------------------------------------------


def test_repeating_a_verification_reproduces_the_same_verdict(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2, with_titles=True)

    first = _verify(root, release)
    second = _verify(root, release, now=LATER)

    assert first.content_sha256 == second.content_sha256
    assert first.model_dump(exclude={"created_at"}) == second.model_dump(exclude={"created_at"})
    assert first.created_at != second.created_at
    assert serialize_verification(first) != serialize_verification(second)


def test_a_failing_verification_is_reproducible_too(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    _rewrite(release / METADATA_FILENAME, "tampered: true\n")

    first = _verify(root, release)
    second = _verify(root, release, now=LATER)

    assert first.status is ReleaseVerificationStatus.NOT_READY_FOR_PUBLICATION
    assert first.content_sha256 == second.content_sha256


# --------------------------------------------------------------------------------------------
# 12 and 13. No provider, no mutation
# --------------------------------------------------------------------------------------------


def test_the_verification_stage_holds_no_path_to_a_provider() -> None:
    package = Path(__file__).parents[1] / "src/book_translator/release_verification"
    sources = {path.name: path.read_text(encoding="utf-8") for path in package.glob("*.py")}

    assert sources
    for name, source in sources.items():
        assert "provider_execution" not in source, f"{name} reaches the provider boundary"
        assert "TranslationProvider" not in source, f"{name} names a provider adapter"


def test_verification_succeeds_while_every_provider_adapter_is_sabotaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2, with_titles=True)

    from book_translator.provider_execution import providers

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("verification contacted a provider")

    for adapter in (
        "ClaudeCodeTranslationProvider",
        "CodexCliTranslationProvider",
        "FakeTranslationProvider",
        "OpenAIResponsesProvider",
    ):
        monkeypatch.setattr(providers, adapter, refuse)

    assert _verify(root, release).status is ReleaseVerificationStatus.READY_FOR_PUBLICATION


def test_verification_mutates_no_byte_of_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=2, with_titles=True)
    before = {
        path.relative_to(root).as_posix(): _hash(path.read_bytes())
        for path in sorted((root / "workspace").rglob("*"))
        if path.is_file()
    }

    _verify(root, release)
    _verify(root, release, now=LATER)

    after = {
        path.relative_to(root).as_posix(): _hash(path.read_bytes())
        for path in sorted((root / "workspace").rglob("*"))
        if path.is_file()
    }
    assert after == before
    assert _workspace_fingerprint(root) == _workspace_fingerprint(root)


def test_a_report_may_never_be_written_into_the_release_it_judges(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    verification = verify_release_artifact(release, project_root=root, now=lambda: CLOCK)

    for destination in ("workspace/releases/book-fa-v1", "workspace/releases/book-fa-v1/v.yaml"):
        with pytest.raises(ReleaseVerificationError, match="inside the release"):
            write_verification_report(verification, destination, project_root=root)
    assert not (release / "v.yaml").exists()


def test_a_report_is_written_where_it_is_asked_for(tmp_path: Path) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    verification = verify_release_artifact(release, project_root=root, now=lambda: CLOCK)

    as_yaml = write_verification_report(verification, "verification.yaml", project_root=root)
    as_json = write_verification_report(verification, "verification.json", project_root=root)

    assert yaml.safe_load(as_yaml.read_text(encoding="utf-8"))["status"] == (
        "READY_FOR_PUBLICATION"
    )
    assert json.loads(as_json.read_text(encoding="utf-8"))["release_id"] == "book-fa-v1"


# --------------------------------------------------------------------------------------------
# 14. CLI
# --------------------------------------------------------------------------------------------


def test_the_cli_reports_a_ready_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    _release(root, chapters=2, with_titles=True)
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["verify-release", "workspace/releases/book-fa-v1", "--verbose"])

    assert result.exit_code == 0, result.stdout
    assert "READY_FOR_PUBLICATION" in result.stdout
    assert "Provider contacted: no" in result.stdout
    assert "mutations: 0" in result.stdout
    for check in RELEASE_CHECK_ORDER:
        assert check.value.replace("_", " ").title() in result.stdout


def test_the_cli_exits_nonzero_on_an_unpublishable_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    release = _release(root, chapters=1)
    _rewrite(release / METADATA_FILENAME, "tampered: true\n")
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["verify-release", "workspace/releases/book-fa-v1"])

    assert result.exit_code == 1
    assert "NOT_READY_FOR_PUBLICATION" in result.stdout


def test_the_cli_emits_machine_readable_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _release(root, chapters=1)
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["verify-release", "workspace/releases/book-fa-v1", "--json"])

    assert result.exit_code == 0, result.stdout
    assert "release_verification_engine_version" in result.stdout
    assert "READY_FOR_PUBLICATION" in result.stdout


def test_the_cli_writes_the_report_where_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _release(root, chapters=1)
    monkeypatch.chdir(root)

    result = runner.invoke(
        app,
        [
            "verify-release",
            "workspace/releases/book-fa-v1",
            "--output",
            "verification.yaml",
        ],
    )

    assert result.exit_code == 0, result.stdout
    written = yaml.safe_load((root / "verification.yaml").read_text(encoding="utf-8"))
    assert written["status"] == "READY_FOR_PUBLICATION"
    assert written["release_id"] == "book-fa-v1"
    assert len(written["checks"]) == len(RELEASE_CHECK_ORDER)
