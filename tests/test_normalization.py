import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.atomic_write import atomic_write_text
from book_translator.cli import app
from book_translator.models import ChapterMetadata, ChapterStatus, TocKind
from book_translator.normalization.engine import NORMALIZATION_VERSION, PASS_ORDER, normalize_text
from book_translator.normalization.markers import MarkerError, parse_source
from book_translator.normalization.workflow import (
    ChapterNormalizationError,
    execute_normalization,
    plan_normalization,
)

runner = CliRunner()
NOW = datetime(2026, 1, 2, tzinfo=UTC)


def source_text(body: str, *, page: int = 1) -> str:
    return f"<!-- source-page: {page} -->\n\n{body.rstrip()}\n"


def make_chapter(
    root: Path,
    number: int | None,
    body: str,
    *,
    title: str = "Synthetic Chapter",
) -> Path:
    name = "000-prologue" if number is None else f"{number:03d}"
    directory = root / "workspace/chapters" / name
    directory.mkdir(parents=True)
    source = source_text(body)
    source_path = directory / "source.md"
    source_path.write_text(source, encoding="utf-8")
    metadata = ChapterMetadata(
        schema_version="1.0",
        chapter_id=name,
        kind=TocKind.PROLOGUE if number is None else TocKind.CHAPTER,
        number=0 if number is None else number,
        title_en=title,
        source_start_page=1,
        source_end_page=1,
        source_file=Path(f"workspace/chapters/{name}/source.md"),
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        cleaned_file=Path(f"workspace/chapters/{name}/cleaned.md"),
        source_word_count=len(body.split()),
        source_paragraph_count=len(body.split("\n\n")),
        status=ChapterStatus.EXTRACTED,
        extraction_version="1.0",
        normalization_version="",
        created_at=NOW,
        updated_at=NOW,
    )
    directory.joinpath("metadata.yaml").write_text(
        yaml.safe_dump(metadata.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    return directory


def test_marker_removal_and_unrelated_comment_preservation() -> None:
    result = normalize_text(source_text("Alpha line.\n<!-- editorial-note -->\nOmega line."))
    assert "source-page" not in result.cleaned_text
    assert "<!-- editorial-note -->" in result.cleaned_text
    assert result.page_markers_removed == 1
    assert result.document.page_transitions == (1,)


@pytest.mark.parametrize(
    "marker",
    [
        "<!-- source-page: x -->",
        "<!-- source-page: 0 -->",
        "<!-- source-page: 1-->",
        "prefix <!-- source-page: 1 -->",
    ],
)
def test_malformed_canonical_looking_marker_rejected(marker: str) -> None:
    with pytest.raises(MarkerError, match="malformed|before"):
        normalize_text(f"{marker}\n\nText\n")


def test_prose_dialogue_hyphen_and_punctuation_normalization() -> None:
    source = source_text(
        '"Then word is seeping out." Alviarin could have\n'
        "been discussing seven-\nstriped cloth—blue–and well-made."
    )
    result = normalize_text(source)
    assert result.cleaned_text == (
        '"Then word is seeping out." Alviarin could have been discussing '
        "seven-striped cloth—blue–and well-made.\n"
    )
    assert result.hyphen_repairs == 1
    assert result.wrapped_line_joins == 1
    assert "sevenstriped" not in result.cleaned_text
    assert result.cleaned_text.count("—") == 1 and result.cleaned_text.count("–") == 1


def test_consecutive_end_of_line_hyphens_are_each_repaired() -> None:
    result = normalize_text(source_text("multi-\nline-\ncompound"))

    assert result.cleaned_text == "multi-line-compound\n"
    assert result.hyphen_repairs == 2
    assert result.wrapped_line_joins == 0


def test_blocks_headings_and_paragraph_boundaries_are_preserved() -> None:
    result = normalize_text(
        source_text(
            "Prologue\n\n(Serpent and Wheel)\n\nThe First Sparks Fall\n\n"
            'First wrapped\nparagraph.\n\n"Separate dialogue."'
        )
    )
    assert result.cleaned_text.split("\n\n") == [
        "Prologue",
        "(Serpent and Wheel)",
        "The First Sparks Fall",
        "First wrapped paragraph.",
        '"Separate dialogue."\n',
    ]
    assert "Prologue (Serpent" not in result.cleaned_text


def test_clear_page_boundary_continuation_joins_and_counts_physical_lines() -> None:
    source = (
        "<!-- source-page: 1 -->\n\nA synthetic sentence wraps\n"
        "across the end of one page\n"
        "<!-- source-page: 2 -->\ncontinued on the next physical\npage.\n"
    )
    result = normalize_text(source)
    assert result.cleaned_text == (
        "A synthetic sentence wraps across the end of one page continued on the next "
        "physical page.\n"
    )
    assert result.document.source_physical_line_count == 4
    assert result.wrapped_line_joins == 2
    assert result.page_continuation_joins == 1
    assert result.page_markers_removed == 2


def test_reviewed_real_page_boundaries_follow_structural_classification() -> None:
    project_root = Path(__file__).resolve().parents[1]
    reviewed = (
        ("002", 87, False),
        ("008", 180, False),
        ("015", 267, False),
        ("024", 398, True),
        ("034", 525, True),
        ("039", 577, False),
        ("039", 578, False),
        ("043", 650, False),
        ("050", 806, True),
    )
    for chapter_id, right_page, expected_join in reviewed:
        source = (project_root / "workspace/chapters" / chapter_id / "source.md").read_text(
            encoding="utf-8"
        )
        parsed, _ = parse_source(source)
        right = next(block for block in parsed.blocks if block.source_page == right_page)
        result = normalize_text(source)
        joins = {
            (change.source_page, change.block_index)
            for change in result.changes
            if change.change_type == "page_boundary_continuation_join"
        }
        assert ((right_page, right.block_index) in joins) is expected_join


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("A face held what might have been smile or", "grimace before it faded away."),
        ("Light crossed the floor of", "polished redstone beneath them."),
    ],
)
def test_prologue_page_boundary_regressions_join(left: str, right: str) -> None:
    source = (
        f"<!-- source-page: 7 -->\n\nSynthetic lead-in wraps\n{left}\n"
        f"<!-- source-page: 8 -->\n{right}\nand continues here.\n"
    )

    result = normalize_text(source)

    assert f"{left} {right}" in result.cleaned_text
    assert "\n\n" not in result.cleaned_text
    assert result.page_continuation_joins == 1


def test_balanced_dialogue_and_narration_split_inside_sentence_joins() -> None:
    source = (
        '<!-- source-page: 56 -->\n\n"I agree." A speaker answered\n'
        'evenly. "We can continue with\n'
        '<!-- source-page: 57 -->\nlowercase prose across the page\nboundary."\n'
    )

    result = normalize_text(source)

    assert "with lowercase prose" in result.cleaned_text
    assert "\n\n" not in result.cleaned_text
    assert result.page_continuation_joins == 1


def test_chapter_style_mixed_dialogue_page_split_joins() -> None:
    source = (
        '<!-- source-page: 913 -->\n\n"A short reply." The speaker said it\n'
        'calmly. "The wider statement continues for\n'
        "<!-- source-page: 914 -->\nstructural reasons, then finishes. A new sentence\n"
        "continues within the extracted block.\n"
    )

    result = normalize_text(source)

    assert "for structural reasons" in result.cleaned_text
    assert len(result.document.blocks) == 1
    assert result.page_continuation_joins == 1


def test_unclosed_dialogue_with_prior_sentence_and_incomplete_clause_joins() -> None:
    source = (
        '<!-- source-page: 10 -->\n\n"A question? If the guardian learns\n'
        "that we lost the traveler, she may begin\n"
        '<!-- source-page: 11 -->\nto ask more questions about\nthe missing party, yes?"\n'
    )

    result = normalize_text(source)

    assert "may begin to ask" in result.cleaned_text
    assert result.page_continuation_joins == 1


def test_capitalized_proper_name_split_across_page_joins() -> None:
    source = (
        "<!-- source-page: 20 -->\n\nThe synthetic description ends with the name\n"
        "of the ancient ruler Lews\n"
        "<!-- source-page: 21 -->\nTherin's standard remained above\nthe distant tower.\n"
    )

    result = normalize_text(source)

    assert "Lews Therin's standard" in result.cleaned_text
    assert result.page_continuation_joins == 1


def test_one_line_tail_of_later_unclosed_dialogue_clause_joins() -> None:
    source = (
        '<!-- source-page: 30 -->\n\n"A complete thought." The speaker paused and said,\n'
        '"There is still something to\nconsider before we decide to l\n'
        '<!-- source-page: 31 -->\nleave now?"\n'
    )

    result = normalize_text(source)

    assert "decide to l leave now?" in result.cleaned_text
    assert result.page_continuation_joins == 1


def test_separate_capitalized_prose_paragraph_does_not_join() -> None:
    source = (
        "<!-- source-page: 40 -->\n\nA deliberately unfinished synthetic phrase\n"
        "ending in an ordinary Word\n"
        "<!-- source-page: 41 -->\nSeparate prose begins on\nthe following page.\n"
    )

    result = normalize_text(source)

    assert result.page_continuation_joins == 0


@pytest.mark.parametrize("prefix", ["- ", "* ", "+ ", "1. ", "2) "])
def test_capitalized_possessive_list_block_does_not_join(prefix: str) -> None:
    source = (
        "<!-- source-page: 40 -->\n\nA deliberately unfinished synthetic phrase\n"
        "ending with the name Lews\n"
        f"<!-- source-page: 41 -->\n{prefix}Therin's listed item wraps\n"
        "onto another physical line.\n"
    )

    result = normalize_text(source)

    assert result.page_continuation_joins == 0


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("A complete synthetic paragraph\nends here.", "lowercase is intentional\nhere."),
        ("An intentionally unfinished label\nfor context", "Separate Paragraph\nstarts here."),
        ('"A dialogue block continues\nwithout a closing mark', "quietly on the next\npage."),
        ("“A curly dialogue block continues\nwithout a closing mark", "quietly next\npage."),
        ("A complete paragraph\nends here.", '"New dialogue starts\non the next page."'),
        ("A synthetic paragraph ends\nbefore the marker", "***"),
        ("A synthetic boundary hyphen\nremains-", "unchanged across the\nnext page."),
    ],
)
def test_genuine_or_structural_page_boundaries_remain_separate(left: str, right: str) -> None:
    source = f"<!-- source-page: 1 -->\n\n{left}\n<!-- source-page: 2 -->\n{right}\n"

    result = normalize_text(source)

    assert "\n\n" in result.cleaned_text
    assert result.page_continuation_joins == 0


def test_poetry_like_lines_are_preserved_and_ambiguous_blocks_warn() -> None:
    poem = normalize_text(
        source_text("One short line\nAnother short line\nA third short line\nLast line")
    )
    assert (
        poem.cleaned_text == "One short line\nAnother short line\nA third short line\nLast line\n"
    )
    assert poem.wrapped_line_joins == 0
    ambiguous = normalize_text(source_text("Short line\nAnother short line\n" + "x" * 80))
    assert ambiguous.ambiguous_blocks == 1
    assert ambiguous.warnings


def test_rendering_counts_and_determinism() -> None:
    source = source_text("One wrapped\nline.\n\nSecond block.")
    first = normalize_text(source)
    second = normalize_text(source)
    assert first == second
    assert first.cleaned_text == "One wrapped line.\n\nSecond block.\n"
    assert len(first.document.blocks) == 2
    assert len(first.cleaned_text.split()) == 5
    assert first.cleaned_text.endswith("\n") and not first.cleaned_text.endswith("\n\n")
    assert PASS_ORDER[-1] == "validation"


@pytest.mark.parametrize("selector", ["prologue", "0"])
def test_prologue_selector_forms(tmp_path: Path, selector: str) -> None:
    make_chapter(tmp_path, None, "A synthetic line.")
    plan = plan_normalization(project_root=tmp_path, selector=selector)
    assert plan.chapters[0].metadata.chapter_id == "000-prologue"


@pytest.mark.parametrize("selector", ["1", "001", "chapter-001"])
def test_numbered_selector_forms(tmp_path: Path, selector: str) -> None:
    make_chapter(tmp_path, 1, "A synthetic line.")
    plan = plan_normalization(project_root=tmp_path, selector=selector)
    assert plan.chapters[0].metadata.chapter_id == "001"


def test_normalization_writes_report_metadata_and_preserves_source(tmp_path: Path) -> None:
    directory = make_chapter(tmp_path, 1, "Ordinary wrapped\nprose seven-\nstriped.")
    source_before = directory.joinpath("source.md").read_bytes()
    plan = plan_normalization(project_root=tmp_path, selector="1", now=lambda: NOW)
    execution = execute_normalization(plan, now=lambda: NOW)
    cleaned = directory.joinpath("cleaned.md")
    metadata = ChapterMetadata.model_validate(
        yaml.safe_load(directory.joinpath("metadata.yaml").read_text())
    )
    report = yaml.safe_load(directory.joinpath("normalization-report.yaml").read_text())
    assert execution.normalized_ids == ("001",)
    assert directory.joinpath("source.md").read_bytes() == source_before
    assert metadata.status is ChapterStatus.NORMALIZED
    assert metadata.cleaned_sha256 == hashlib.sha256(cleaned.read_bytes()).hexdigest()
    assert metadata.cleaned_word_count == 4 and metadata.cleaned_paragraph_count == 1
    assert metadata.normalization_version == NORMALIZATION_VERSION
    assert metadata.created_at == NOW
    assert report["cleaned_sha256"] == metadata.cleaned_sha256
    assert report["hyphen_repairs"] == 1 and report["wrapped_line_joins"] == 1
    assert report["source_physical_line_count"] == 3
    assert "Ordinary wrapped" not in directory.joinpath("normalization-report.yaml").read_text()


def test_skip_stale_partial_and_cleaned_hash_validation(tmp_path: Path) -> None:
    directory = make_chapter(tmp_path, 1, "Synthetic prose.")
    execute_normalization(plan_normalization(project_root=tmp_path), now=lambda: NOW)
    assert plan_normalization(project_root=tmp_path).chapters[0].action == "skip"
    assert (
        plan_normalization(project_root=tmp_path, normalization_version="testing-2")
        .chapters[0]
        .action
        == "stale"
    )
    directory.joinpath("cleaned.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ChapterNormalizationError, match="cleaned hash"):
        plan_normalization(project_root=tmp_path)

    other = make_chapter(tmp_path, 2, "Other synthetic prose.")
    other.joinpath("cleaned.md").write_text("partial\n", encoding="utf-8")
    plan = plan_normalization(project_root=tmp_path, selector="2")
    assert plan.chapters[0].action == "conflict"
    with pytest.raises(ChapterNormalizationError, match="preflight blocked"):
        execute_normalization(plan)


@pytest.mark.parametrize("corruption", ["metadata_word_count", "report_join_count"])
def test_skip_refuses_tampered_normalization_counts(tmp_path: Path, corruption: str) -> None:
    directory = make_chapter(tmp_path, 1, "Synthetic wrapped\nprose.")
    execute_normalization(plan_normalization(project_root=tmp_path), now=lambda: NOW)
    if corruption == "metadata_word_count":
        path = directory / "metadata.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["cleaned_word_count"] += 1
    else:
        path = directory / "normalization-report.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["wrapped_line_joins"] += 1
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ChapterNormalizationError, match="integrity"):
        plan_normalization(project_root=tmp_path)


def test_force_replacement_and_downstream_refusal(tmp_path: Path) -> None:
    directory = make_chapter(tmp_path, 1, "Synthetic prose.")
    execute_normalization(plan_normalization(project_root=tmp_path), now=lambda: NOW)
    forced = plan_normalization(project_root=tmp_path, force=True)
    assert forced.chapters[0].action == "replace"
    execute_normalization(forced, now=lambda: NOW)
    directory.joinpath("chunks/001").mkdir(parents=True)
    directory.joinpath("chunks/001/source.md").write_text("downstream", encoding="utf-8")
    conflict = plan_normalization(project_root=tmp_path, force=True)
    assert conflict.chapters[0].action == "conflict"
    with pytest.raises(ChapterNormalizationError, match="preflight blocked"):
        execute_normalization(conflict)


def test_valid_normalization_skips_after_chapter_advances_to_chunked(tmp_path: Path) -> None:
    directory = make_chapter(tmp_path, 1, "Synthetic prose.")
    execute_normalization(plan_normalization(project_root=tmp_path), now=lambda: NOW)
    metadata_path = directory / "metadata.yaml"
    metadata = ChapterMetadata.model_validate(yaml.safe_load(metadata_path.read_text()))
    advanced = metadata.model_copy(update={"status": ChapterStatus.CHUNKED, "chunk_count": 1})
    metadata_path.write_text(
        yaml.safe_dump(advanced.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    directory.joinpath("chunks/001").mkdir(parents=True)

    plan = plan_normalization(project_root=tmp_path, selector="1")

    assert plan.chapters[0].action == "skip"


def test_dry_run_cli_writes_nothing_and_prints_no_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = make_chapter(tmp_path, None, "SECRET SYNTHETIC BOOK PROSE")
    metadata_before = directory.joinpath("metadata.yaml").read_bytes()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["normalize-chapters", "--chapter", "prologue", "--dry-run"])
    assert result.exit_code == 0
    assert "SECRET SYNTHETIC BOOK PROSE" not in result.stdout + result.stderr
    assert not directory.joinpath("cleaned.md").exists()
    assert not directory.joinpath("normalization-report.yaml").exists()
    assert directory.joinpath("metadata.yaml").read_bytes() == metadata_before


def test_batch_preflight_prevents_known_partial_writes(tmp_path: Path) -> None:
    first = make_chapter(tmp_path, 1, "First synthetic prose.")
    second = make_chapter(tmp_path, 2, "Second synthetic prose.")
    second.joinpath("cleaned.md").write_text("partial\n", encoding="utf-8")
    plan = plan_normalization(project_root=tmp_path)
    with pytest.raises(ChapterNormalizationError, match="preflight blocked"):
        execute_normalization(plan)
    assert not first.joinpath("cleaned.md").exists()


def test_runtime_failure_preserves_completed_and_leaves_failed_metadata_unclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = make_chapter(tmp_path, 1, "First synthetic prose.")
    second = make_chapter(tmp_path, 2, "Second synthetic prose.")
    plan = plan_normalization(project_root=tmp_path)
    real_write = atomic_write_text

    def failing_write(destination: Path, content: str, *, overwrite: bool = False) -> None:
        if destination == second / "normalization-report.yaml":
            raise OSError("injected report failure")
        real_write(destination, content, overwrite=overwrite)

    monkeypatch.setattr("book_translator.normalization.workflow.atomic_write_text", failing_write)
    with pytest.raises(ChapterNormalizationError, match="completed: 001"):
        execute_normalization(plan, now=lambda: NOW)
    first_metadata = ChapterMetadata.model_validate(
        yaml.safe_load(first.joinpath("metadata.yaml").read_text())
    )
    second_metadata = ChapterMetadata.model_validate(
        yaml.safe_load(second.joinpath("metadata.yaml").read_text())
    )
    assert first_metadata.status is ChapterStatus.NORMALIZED
    assert second_metadata.status is ChapterStatus.EXTRACTED
    assert second.joinpath("cleaned.md").exists()
    rerun = plan_normalization(project_root=tmp_path)
    assert [item.action for item in rerun.chapters] == ["skip", "conflict"]


def test_lowercase_edge_continues_into_capitalized_possessive_name_across_page() -> None:
    source = (
        "<!-- source-page: 377 -->\n\nThe synthetic clause wraps across lines,\n"
        "pausing for a moment to finger\n"
        "<!-- source-page: 378 -->\nAldan’s heavy ring. Another sentence\n"
        "continues within the extracted block.\n"
    )

    result = normalize_text(source)

    assert "to finger Aldan’s heavy ring" in result.cleaned_text
    assert len(result.document.blocks) == 1
    assert result.page_continuation_joins == 1
    assert [
        change.description
        for change in result.changes
        if change.change_type == "page_boundary_continuation_join"
    ] == ["lowercase-possessive-name-continuation"]


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # A terminal left edge keeps a possessive-led new paragraph separate.
        ("A complete synthetic paragraph\nends here.", "Aldan's tone was cold\nand level."),
        # Trailing punctuation on the left edge is not a bare unfinished word.
        ("A synthetic clause pauses,\nthen stops here,", "Aldan's tone was cold\nand level."),
        # A closing quotation mark ends the left edge even without a full stop.
        ('"A synthetic dialogue line ends\nwith a closing mark"', "Aldan's reply\nwas curt."),
        # A capitalized non-possessive right edge stays a plain paragraph boundary.
        ("A synthetic clause wraps and\nstops on the word", "Aldan turned toward\nthe door."),
        # A one-line right block is structural and stays protected.
        ("A synthetic clause wraps and\nstops on the word", "Aldan's ring."),
    ],
)
def test_possessive_name_rule_keeps_genuine_boundaries_separate(left: str, right: str) -> None:
    source = f"<!-- source-page: 1 -->\n\n{left}\n<!-- source-page: 2 -->\n{right}\n"

    result = normalize_text(source)

    assert result.page_continuation_joins == 0
    assert len(result.document.blocks) == 2


def test_possessive_name_rule_requires_different_canonical_pages() -> None:
    source = (
        "<!-- source-page: 1 -->\n\nA synthetic clause wraps and\nstops on the word\n\n"
        "Aldan's ring glowed on\nthe table.\n"
    )

    result = normalize_text(source)

    assert result.page_continuation_joins == 0
    assert len(result.document.blocks) == 2
