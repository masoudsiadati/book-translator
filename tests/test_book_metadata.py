from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.book_metadata import (
    BOOK_METADATA_ENGINE_VERSION,
    BookIdentityError,
    BookMetadataError,
    ConfiguredBookEvidence,
    EvidenceLine,
    OperatorAssertion,
    PdfBookEvidence,
    collect_pdf_evidence,
    derive_book_id,
    execute_book_metadata,
    is_placeholder,
    load_book_metadata,
    plan_book_metadata,
    resolve_book_metadata,
    slugify,
)
from book_translator.cli import app
from book_translator.models import BookMetadataProvenance
from tests.test_pdf import create_pdf

NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
runner = CliRunner()

PLACEHOLDER_CONFIG = ConfiguredBookEvidence(
    book_id="example-fantasy-novel-en-fa",
    title_en="Example Fantasy Novel",
    title_fa="رمان فانتزی نمونه",
    author="Example Author",
    source_language="en",
    target_language="fa",
)


def _project(root: Path, *, page_texts: list[str | None], metadata: dict[str, str]) -> None:
    (root / "config").mkdir()
    (root / "input").mkdir()
    (root / "workspace").mkdir()
    create_pdf(root / "input/book.pdf", page_texts, metadata=metadata)
    (root / "config/book.yaml").write_text(
        'book_id: "example-fantasy-novel-en-fa"\n'
        'title_en: "Example Fantasy Novel"\n'
        'title_fa: "رمان فانتزی نمونه"\n'
        'author: "Example Author"\n'
        "source_language: en\n"
        "target_language: fa\n"
        "pdf_path: input/book.pdf\n"
        "chunk_target_words: 10\n"
        "chunk_max_words: 20\n"
        "previous_context_words: 2\n"
        "output_format: epub3\n",
        encoding="utf-8",
    )


def test_slug_and_book_id_are_stable_and_filesystem_safe() -> None:
    assert slugify("The Fíres  of Heaven!") == "the-fires-of-heaven"
    assert (
        derive_book_id(title_en="A Synthetic Tale", source_language="en", target_language="fa")
        == "a-synthetic-tale-en-fa"
    )
    with pytest.raises(BookIdentityError):
        derive_book_id(title_en="کتاب", source_language="en", target_language="fa")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Example Fantasy Novel", True),
        ("example-fantasy-novel-en-fa", True),
        ("رمان فانتزی نمونه", True),
        ("Placeholder Author", True),
        ("A Synthetic Tale", False),
        ("Ymir of the Northern Reach", False),
        ("", False),
    ],
)
def test_placeholder_detection_is_generic(value: str, expected: bool) -> None:
    assert is_placeholder(value) is expected


def test_local_source_evidence_outranks_placeholder_configuration() -> None:
    pdf = PdfBookEvidence(
        page_count=20,
        title="A Synthetic Tale",
        author="Ymir Solheim",
        matter_lines=(
            EvidenceLine(3, "Ymir Solheim"),
            EvidenceLine(3, "A Synthetic Tale"),
            EvidenceLine(20, "The End of the Third Book of The Synthetic Cycle"),
        ),
    )

    resolution = resolve_book_metadata(pdf=pdf, config=PLACEHOLDER_CONFIG)

    assert resolution.value("title_en") == "A Synthetic Tale"
    assert resolution.fields["title_en"].provenance is BookMetadataProvenance.PDF_METADATA
    assert resolution.fields["title_en"].evidence == "pdf:docinfo.title+pdf:page-3"
    assert resolution.value("author") == "Ymir Solheim"
    assert resolution.value("series_name") == "The Synthetic Cycle"
    assert resolution.value("series_index") == 3
    assert resolution.fields["series_name"].provenance is BookMetadataProvenance.FRONT_MATTER
    # A placeholder book ID is discarded, so identity is derived from resolved source evidence.
    assert resolution.value("book_id") == "a-synthetic-tale-en-fa"


def test_absent_evidence_is_left_unresolved_rather_than_guessed() -> None:
    pdf = PdfBookEvidence(page_count=4, title="A Synthetic Tale", author="Ymir Solheim")

    resolution = resolve_book_metadata(pdf=pdf, config=PLACEHOLDER_CONFIG)

    for name in ("title_fa", "series_name", "series_index", "isbn", "publisher", "edition"):
        assert resolution.value(name) is None
        assert resolution.fields[name].provenance is BookMetadataProvenance.UNRESOLVED


def test_operator_assertion_records_external_research_provenance() -> None:
    pdf = PdfBookEvidence(page_count=4, title="A Synthetic Tale", author="Ymir Solheim")

    resolution = resolve_book_metadata(
        pdf=pdf,
        config=PLACEHOLDER_CONFIG,
        assertions={
            "isbn": OperatorAssertion("9780312850548", BookMetadataProvenance.EXTERNAL_RESEARCH)
        },
    )

    assert resolution.value("isbn") == "9780312850548"
    assert resolution.fields["isbn"].provenance is BookMetadataProvenance.EXTERNAL_RESEARCH


def test_bibliographic_patterns_resolve_isbn_publisher_and_year() -> None:
    pdf = PdfBookEvidence(
        page_count=6,
        title="A Synthetic Tale",
        author="Ymir Solheim",
        matter_lines=(
            EvidenceLine(4, "ISBN 978-0-306-40615-7"),
            EvidenceLine(4, "Published by Synthetic House"),
            EvidenceLine(4, "Copyright © 1993 by Ymir Solheim"),
            EvidenceLine(4, "First mass market edition"),
        ),
    )

    resolution = resolve_book_metadata(pdf=pdf, config=PLACEHOLDER_CONFIG)

    assert resolution.value("isbn") == "9780306406157"
    assert resolution.value("publisher") == "Synthetic House"
    assert resolution.value("publication_year") == 1993
    assert resolution.value("edition") == "First mass market edition"


def test_invalid_isbn_checksum_is_refused() -> None:
    pdf = PdfBookEvidence(
        page_count=6,
        title="A Synthetic Tale",
        author="Ymir Solheim",
        matter_lines=(EvidenceLine(4, "ISBN 978-0-306-40615-8"),),
    )

    assert resolve_book_metadata(pdf=pdf, config=PLACEHOLDER_CONFIG).value("isbn") is None


def test_evidence_collection_ignores_prose_length_lines(tmp_path: Path) -> None:
    prose = " ".join(f"word{number}" for number in range(1, 60))
    pdf_path = tmp_path / "book.pdf"
    create_pdf(pdf_path, ["A Synthetic Tale", prose], metadata={"title": "A Synthetic Tale"})

    evidence = collect_pdf_evidence(pdf_path)

    assert evidence.title == "A Synthetic Tale"
    assert any(line.text == "A Synthetic Tale" for line in evidence.matter_lines)
    assert all(len(line.text) <= 120 for line in evidence.matter_lines)


def test_resolution_is_persisted_deterministically_and_idempotently(tmp_path: Path) -> None:
    _project(
        tmp_path,
        page_texts=["A Synthetic Tale", "The End of the Second Book of The Synthetic Cycle"],
        metadata={"title": "A Synthetic Tale", "author": "Ymir Solheim"},
    )

    plan = plan_book_metadata(project_root=tmp_path, now=lambda: NOW)
    published = execute_book_metadata(plan)
    first_bytes = published.read_bytes()

    assert plan.action == "create"
    assert published == tmp_path / "workspace/metadata.yaml"
    assert plan.metadata.metadata_version == BOOK_METADATA_ENGINE_VERSION
    assert plan.metadata.title_en == "A Synthetic Tale"
    assert plan.metadata.author == "Ymir Solheim"
    assert plan.metadata.series_name == "The Synthetic Cycle"
    assert plan.metadata.series_index == 2
    assert plan.metadata.title_fa == ""
    assert plan.metadata.source_pdf_name == "book.pdf"
    assert "title_fa" in plan.unresolved_fields
    assert plan.placeholder_fields == ()
    assert first_bytes.endswith(b"\n")

    repeated = plan_book_metadata(
        project_root=tmp_path, now=lambda: datetime(2027, 1, 1, tzinfo=UTC)
    )
    execute_book_metadata(repeated)

    assert repeated.action == "skip"
    assert published.read_bytes() == first_bytes
    assert load_book_metadata(tmp_path) == plan.metadata


def test_unresolvable_required_metadata_is_refused(tmp_path: Path) -> None:
    _project(tmp_path, page_texts=["nothing bibliographic here"], metadata={})

    with pytest.raises(BookMetadataError, match="title_en could not be resolved"):
        plan_book_metadata(project_root=tmp_path, now=lambda: NOW)


def test_provenance_must_agree_with_resolved_values(tmp_path: Path) -> None:
    _project(
        tmp_path,
        page_texts=["A Synthetic Tale"],
        metadata={"title": "A Synthetic Tale", "author": "Ymir Solheim"},
    )
    plan = plan_book_metadata(project_root=tmp_path, now=lambda: NOW)
    execute_book_metadata(plan)
    path = tmp_path / "workspace/metadata.yaml"
    corrupted = yaml.safe_load(path.read_text(encoding="utf-8"))
    corrupted["series_name"] = "The Synthetic Cycle"
    path.write_text(yaml.safe_dump(corrupted, allow_unicode=True), encoding="utf-8")

    with pytest.raises(BookMetadataError, match="contradicts its resolved value"):
        load_book_metadata(tmp_path)


def test_cli_dry_run_writes_nothing_and_apply_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(
        tmp_path,
        page_texts=["A Synthetic Tale"],
        metadata={"title": "A Synthetic Tale", "author": "Ymir Solheim"},
    )
    monkeypatch.chdir(tmp_path)

    dry = runner.invoke(app, ["resolve-book-metadata"])
    assert dry.exit_code == 0
    assert not (tmp_path / "workspace/metadata.yaml").exists()

    applied = runner.invoke(
        app,
        ["resolve-book-metadata", "--apply", "--set", "publisher=Synthetic House"],
    )

    assert applied.exit_code == 0
    metadata = load_book_metadata(tmp_path)
    assert metadata is not None
    assert metadata.publisher == "Synthetic House"
    assert (
        next(item for item in metadata.provenance if item.field == "publisher").provenance
        is BookMetadataProvenance.CONFIGURATION
    )


def _minimal_config(**extra: str | int) -> dict[str, str | int]:
    config: dict[str, str | int] = {
        "book_id": "example-fantasy-novel-en-fa",
        "title_en": "Example Fantasy Novel",
        "title_fa": "رمان فانتزی نمونه",
        "author": "Example Author",
        "source_language": "en",
        "target_language": "fa",
        "pdf_path": "input/book.pdf",
        "chunk_target_words": 10,
        "chunk_max_words": 20,
        "previous_context_words": 2,
        "output_format": "epub3",
    }
    config.update(extra)
    return config


def test_book_config_accepts_optional_series_name_and_index() -> None:
    from book_translator.config import BookConfig

    legacy = BookConfig.model_validate(_minimal_config())
    assert legacy.series_name is None
    assert legacy.series_index is None

    configured = BookConfig.model_validate(
        _minimal_config(series_name="The Synthetic Cycle", series_index=6)
    )
    assert configured.series_name == "The Synthetic Cycle"
    assert configured.series_index == 6


def test_resolver_takes_series_from_configuration_with_configuration_provenance() -> None:
    pdf = PdfBookEvidence(page_count=4, title="A Synthetic Tale", author="Ymir Solheim")
    config = ConfiguredBookEvidence(
        book_id="a-synthetic-tale-en-fa",
        title_en="A Synthetic Tale",
        title_fa="",
        author="Ymir Solheim",
        source_language="en",
        target_language="fa",
        series_name="The Synthetic Cycle",
        series_index=6,
    )

    resolution = resolve_book_metadata(pdf=pdf, config=config)

    assert resolution.value("series_name") == "The Synthetic Cycle"
    assert resolution.value("series_index") == 6
    for name in ("series_name", "series_index"):
        assert resolution.fields[name].provenance is BookMetadataProvenance.CONFIGURATION
        assert resolution.fields[name].evidence == f"config/book.yaml:{name}"
        assert name not in " ".join(resolution.warnings)
    # title_fa resolution is untouched by the series configuration.
    assert resolution.fields["title_fa"].provenance is BookMetadataProvenance.UNRESOLVED


def test_explicit_assertion_still_outranks_configured_series() -> None:
    pdf = PdfBookEvidence(page_count=4, title="A Synthetic Tale", author="Ymir Solheim")
    config = ConfiguredBookEvidence(
        book_id="a-synthetic-tale-en-fa",
        title_en="A Synthetic Tale",
        title_fa="",
        author="Ymir Solheim",
        source_language="en",
        target_language="fa",
        series_name="The Synthetic Cycle",
        series_index=6,
    )

    resolution = resolve_book_metadata(
        pdf=pdf,
        config=config,
        assertions={
            "series_name": OperatorAssertion(
                "The Other Cycle", BookMetadataProvenance.EXTERNAL_RESEARCH
            ),
            "series_index": OperatorAssertion("7", BookMetadataProvenance.EXTERNAL_RESEARCH),
        },
    )

    assert resolution.value("series_name") == "The Other Cycle"
    assert resolution.value("series_index") == "7"
    for name in ("series_name", "series_index"):
        assert resolution.fields[name].provenance is BookMetadataProvenance.EXTERNAL_RESEARCH
        assert resolution.fields[name].evidence == "operator assertion"


def test_legacy_configuration_without_series_keeps_source_resolution() -> None:
    # PLACEHOLDER_CONFIG names no series, so front matter still decides and absence stays
    # unresolved: the pre-existing behavior for a legacy config is byte-for-byte preserved.
    with_matter = PdfBookEvidence(
        page_count=20,
        title="A Synthetic Tale",
        author="Ymir Solheim",
        matter_lines=(EvidenceLine(20, "The End of the Third Book of The Synthetic Cycle"),),
    )
    resolved = resolve_book_metadata(pdf=with_matter, config=PLACEHOLDER_CONFIG)
    assert resolved.value("series_name") == "The Synthetic Cycle"
    assert resolved.value("series_index") == 3
    assert resolved.fields["series_name"].provenance is BookMetadataProvenance.FRONT_MATTER
    assert resolved.fields["series_index"].provenance is BookMetadataProvenance.FRONT_MATTER
    assert resolved.fields["series_name"].evidence == "pdf:page-20"

    without_matter = PdfBookEvidence(page_count=4, title="A Synthetic Tale", author="Ymir Solheim")
    unresolved = resolve_book_metadata(pdf=without_matter, config=PLACEHOLDER_CONFIG)
    assert unresolved.fields["series_name"].provenance is BookMetadataProvenance.UNRESOLVED
    assert unresolved.fields["series_index"].provenance is BookMetadataProvenance.UNRESOLVED
    assert unresolved.fields["series_name"].evidence == (
        "no series statement in front or back matter"
    )
    assert unresolved.fields["series_index"].evidence == (
        "no volume statement in front or back matter"
    )


def test_book_scoped_configured_series_persists_and_resolves_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config/books").mkdir(parents=True)
    (tmp_path / "input").mkdir()
    (tmp_path / "workspace").mkdir()
    create_pdf(
        tmp_path / "input/book.pdf",
        ["A Synthetic Tale"],
        metadata={"title": "A Synthetic Tale", "author": "Ymir Solheim"},
    )
    (tmp_path / "config/books/synthetic.yaml").write_text(
        yaml.safe_dump(
            _minimal_config(
                title_fa="داستان مصنوعی", series_name="The Synthetic Cycle", series_index=6
            ),
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    first = plan_book_metadata(project_root=tmp_path, now=lambda: NOW, book="synthetic")
    published = execute_book_metadata(first)
    first_bytes = published.read_bytes()

    assert first.action == "create"
    assert published == tmp_path / "workspace/books/synthetic/metadata.yaml"
    assert first.metadata.title_fa == "داستان مصنوعی"
    assert first.metadata.series_name == "The Synthetic Cycle"
    assert first.metadata.series_index == 6
    for name in ("title_fa", "series_name", "series_index"):
        assert name not in first.unresolved_fields
        assert (
            next(item for item in first.metadata.provenance if item.field == name).provenance
            is BookMetadataProvenance.CONFIGURATION
        )
    assert not any(
        warning.startswith(("title_fa ", "series_name ", "series_index "))
        for warning in first.warnings
    )

    second = plan_book_metadata(
        project_root=tmp_path, now=lambda: datetime(2027, 1, 1, tzinfo=UTC), book="synthetic"
    )
    execute_book_metadata(second)

    assert second.action == "skip"
    assert second.unresolved_fields == first.unresolved_fields
    assert published.read_bytes() == first_bytes

    dry = runner.invoke(app, ["resolve-book-metadata", "--book", "synthetic"])
    assert dry.exit_code == 0
    assert "skip" in dry.stdout
    assert published.read_bytes() == first_bytes
