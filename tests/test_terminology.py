from pathlib import Path

import pytest
import yaml
from pytest import MonkeyPatch
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    TerminologyCandidate,
    TerminologyOccurrence,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.prompt_packaging.rendering import render_messages
from book_translator.terminology import (
    TerminologyError,
    candidate_identity,
    canonical_registry_bytes,
    legacy_glossary_decisions,
    registry_identity,
    relevant_decisions,
    save_registry,
    set_decision,
    source_term_matches,
    validate_terminology,
    validate_translation_qa,
)

runner = CliRunner()


def registry(source_language: str = "en", target_language: str = "fa") -> TerminologyRegistry:
    return TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language=source_language,
        target_language=target_language,
    )


def decided_registry(
    *, context_sensitive: bool = False, competing: tuple[str, ...] = ()
) -> TerminologyRegistry:
    return set_decision(
        registry(),
        source_term="Veyra",
        target_term="ویرا",
        classification=(
            TerminologyType.CONTEXT_SENSITIVE if context_sensitive else TerminologyType.PERSON
        ),
        decision_source=DecisionSource.USER,
        locked=True,
        aliases=("Lady Veyra",),
        competing_variants=competing,
        context_sensitive=context_sensitive,
    )


def test_registry_serialization_and_identity_are_deterministic(tmp_path: Path) -> None:
    first = decided_registry()
    reordered = first.model_copy(
        update={
            "candidates": list(reversed(first.candidates)),
            "decisions": list(reversed(first.decisions)),
        }
    )
    assert canonical_registry_bytes(first) == canonical_registry_bytes(reordered)
    assert registry_identity(first) == registry_identity(reordered)
    path = tmp_path / "terminology.yaml"
    save_registry(path, first)
    first_bytes = path.read_bytes()
    save_registry(path, reordered)
    assert path.read_bytes() == first_bytes


def test_candidate_lifecycle_occurrences_and_aliases() -> None:
    candidate = TerminologyCandidate(
        candidate_id=candidate_identity("Veyra", "en"),
        source_term="Veyra",
        classification=TerminologyType.PERSON,
        confidence=0.72,
        status=CandidateStatus.CANDIDATE,
        provenance=DecisionSource.AI,
        aliases=["Lady Veyra"],
        occurrences=[TerminologyOccurrence(chapter_id="001", chunk_id="001-002", count=2)],
    )
    rejected = candidate.model_copy(update={"status": CandidateStatus.REJECTED})
    assert rejected.status is CandidateStatus.REJECTED
    assert candidate.occurrences[0].count == 2


def test_user_priority_and_lock_prevent_silent_overwrite() -> None:
    current = decided_registry()
    try:
        set_decision(
            current,
            source_term="Veyra",
            target_term="ویرا-دیگر",
            classification=TerminologyType.PERSON,
            decision_source=DecisionSource.AI,
            locked=False,
        )
    except TerminologyError as exc:
        assert "locked" in str(exc)
    else:
        raise AssertionError("locked decision was overwritten")


def test_lower_priority_cannot_replace_unlocked_user_decision() -> None:
    current = set_decision(
        registry(),
        source_term="Veyra",
        target_term="ویرا",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=False,
    )
    try:
        set_decision(
            current,
            source_term="Veyra",
            target_term="دیگر",
            classification=TerminologyType.PERSON,
            decision_source=DecisionSource.INFERRED,
            locked=False,
        )
    except TerminologyError as exc:
        assert "lower-priority" in str(exc)
    else:
        raise AssertionError("lower-priority decision was accepted")


def test_context_sensitive_term_is_not_globally_frozen() -> None:
    current = decided_registry(context_sensitive=True)
    assert relevant_decisions(current, "Veyra appears here") == ()


def test_registry_is_language_and_book_generic() -> None:
    current = set_decision(
        registry("ja", "de"),
        source_term="星門",
        target_term="Sternentor",
        classification=TerminologyType.ARTIFACT,
        decision_source=DecisionSource.AI,
        locked=False,
    )
    assert current.source_language == "ja" and current.target_language == "de"


def test_prompt_filters_relevance_and_marks_locked_decision() -> None:
    current = decided_registry()
    messages = render_messages(
        prompt_template=(
            "{{ chapter_number }} {{ part_number }} {{ glossary }} "
            "{{ previous_context }} {{ source_text }}"
        ),
        glossary={
            "terms": {},
            "characters": {},
            "places": {},
            "groups": {},
            "unresolved_terms": [],
        },
        style_guide_bytes=b"synthetic style\n",
        context_bytes=b"unrelated context\n",
        source_bytes=b"Veyra returns.\n",
        chapter_id="001",
        chapter_number=1,
        chapter_title="Synthetic",
        chunk_sequence=1,
        total_chunks=1,
        source_language="en",
        target_language="fa",
        terminology_registry=current,
    )
    assert "user_locked: true" in messages.glossary_text
    assert "allowing natural target-language inflection" in messages.glossary_text
    irrelevant = messages.glossary_text.replace("Veyra", "")
    assert "OtherTerm" not in irrelevant


def test_validator_detects_missing_and_competing_variant_without_rewrite() -> None:
    current = decided_registry(competing=("ویراا",))
    translation = "ویراا آمد.\n"
    report = validate_terminology("Veyra arrived.\n", translation, current)
    assert {item.code for item in report.findings} == {
        "canonical_term_missing",
        "competing_variant",
    }
    assert translation == "ویراا آمد.\n"
    assert validate_terminology("Veyra arrived.\n", "ویرا آمد.\n", current).valid


def alias_registry(source_term: str, alias: str, target_term: str) -> TerminologyRegistry:
    return set_decision(
        registry(),
        source_term=source_term,
        target_term=target_term,
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=True,
        aliases=(alias,),
    )


@pytest.mark.parametrize(
    ("source_term", "alias", "target_term", "source", "translation"),
    [
        (
            "Mara Tomanes",
            "Mara",
            "مارا تومانس",
            "Siuan was traveling as Mara.\n",
            "سیوان با نام مارا سفر می‌کرد.\n",
        ),
        (
            "Gareth Bryne",
            "Bryne",
            "گرث براین",
            "Bryne walked across the room.\n",
            "براین از اتاق گذشت.\n",
        ),
    ],
)
def test_source_alias_does_not_require_full_canonical_target(
    source_term: str, alias: str, target_term: str, source: str, translation: str
) -> None:
    current = alias_registry(source_term, alias, target_term)

    report = validate_terminology(source, translation, current)

    assert report.valid
    assert not report.findings
    assert report.checked_decisions == 0


def test_full_source_term_requires_and_accepts_full_canonical_target() -> None:
    current = alias_registry("Mara Tomanes", "Mara", "مارا تومانس")

    assert validate_terminology("Mara Tomanes.\n", "«مارا تومانس.»\n", current).valid
    missing = validate_terminology("Mara Tomanes.\n", "مارا.\n", current)
    assert {finding.code for finding in missing.findings} == {"canonical_term_missing"}


def test_separate_alias_and_full_occurrences_are_valid() -> None:
    current = alias_registry("Mara Tomanes", "Mara", "مارا تومانس")
    report = validate_terminology(
        "Mara waited. Mara Tomanes entered.\n",
        "مارا منتظر ماند. مارا تومانس وارد شد.\n",
        current,
    )

    assert report.valid
    assert report.checked_decisions == 1


def test_overlapping_alias_is_suppressed_by_authoritative_full_match() -> None:
    current = alias_registry("Mara Tomanes", "Mara", "مارا تومانس")

    matches = source_term_matches(current, "Mara Tomanes.\n")

    assert [(match.term, match.is_alias) for match in matches] == [("Mara Tomanes", False)]


@pytest.mark.parametrize(
    "translation",
    [
        "اژدهای دروغین آمد.\n",
        "اژدهای دروغینی آمد.\n",
        "اژدهای دروغین\u200cها آمدند.\n",
        "اژدهای دروغین\u200cهای افسانه آمدند.\n",
    ],
)
def test_persian_canonical_inflections_preserve_exact_lexeme(translation: str) -> None:
    current = set_decision(
        registry(),
        source_term="false Dragon",
        target_term="اژدهای دروغین",
        classification=TerminologyType.TITLE,
        decision_source=DecisionSource.USER,
        locked=True,
    )

    assert validate_terminology("A false Dragon appeared.\n", translation, current).valid


@pytest.mark.parametrize(
    "translation",
    ["اژدهای دروغ آمد.\n", "اژدهای دروغینا آمد.\n", "اژدهای دروغینها آمدند.\n"],
)
def test_persian_canonical_policy_rejects_competing_spellings(translation: str) -> None:
    current = set_decision(
        registry(),
        source_term="false Dragon",
        target_term="اژدهای دروغین",
        classification=TerminologyType.TITLE,
        decision_source=DecisionSource.USER,
        locked=True,
    )

    assert not validate_terminology("A false Dragon appeared.\n", translation, current).valid


def test_required_demonym_does_not_accept_semantic_root_paraphrase() -> None:
    current = set_decision(
        registry(),
        source_term="Cairhienin",
        target_term="کایرینی",
        classification=TerminologyType.PEOPLE,
        decision_source=DecisionSource.USER,
        locked=True,
    )

    report = validate_terminology("The Cairhienin throne stood.\n", "تخت کایرین بود.\n", current)
    assert {finding.code for finding in report.findings} == {"canonical_term_missing"}


def test_canonical_components_are_not_valid_competing_variants() -> None:
    with pytest.raises(TerminologyError, match="must not occur lexically"):
        set_decision(
            registry(),
            source_term="Barim Halle",
            target_term="باریم هال",
            classification=TerminologyType.PERSON,
            decision_source=DecisionSource.USER,
            locked=True,
            competing_variants=("باریم", "هال"),
        )


def test_historical_canonical_components_cannot_trigger_competing_qa() -> None:
    current = decided_registry()
    historical = current.model_copy(
        update={
            "decisions": [
                current.decisions[0].model_copy(
                    update={"target_term": "باریم هال", "competing_variants": ["باریم", "هال"]}
                )
            ]
        }
    )
    report = validate_terminology("Veyra arrived.\n", "باریم هال آمد.\n", historical)
    assert report.valid
    assert not report.findings


def test_genuine_standalone_and_full_phrase_variants_still_fail() -> None:
    spelling = decided_registry(competing=("ویراا",))
    spelling_report = validate_terminology("Veyra arrived.\n", "ویراا آمد.\n", spelling)
    assert "competing_variant" in {item.code for item in spelling_report.findings}

    phrase = set_decision(
        registry(),
        source_term="the Dragon Reborn",
        target_term="اژدهای باززاده",
        classification=TerminologyType.TITLE,
        decision_source=DecisionSource.USER,
        locked=True,
        competing_variants=("اژدهای زاده‌شده از نو",),
    )
    phrase_report = validate_terminology(
        "the Dragon Reborn returned.\n", "اژدهای زاده‌شده از نو بازگشت.\n", phrase
    )
    assert "competing_variant" in {item.code for item in phrase_report.findings}


def test_translation_qa_detects_merge_and_mechanical_findings() -> None:
    report = validate_translation_qa(
        "One.\n\nTwo.\n",
        "یک.  \nدو.\t\ufffd\u200c\u200c\u064a\u0643\n",
        target_language="fa",
    )
    codes = {item.code for item in report.findings}
    assert "paragraph_count_mismatch" in codes
    assert {"trailing_double_space", "tab", "replacement_character", "doubled_zwnj"} <= codes
    assert {"arabic_yeh", "arabic_kaf"} <= codes


def test_legacy_glossary_compatibility_and_stilling_terms() -> None:
    raw = yaml.safe_load(Path("config/glossary.yaml").read_text(encoding="utf-8"))
    adapted = legacy_glossary_decisions(raw)
    assert ("Stilling", "خنثی شدن", False) in adapted
    assert ("gentled", "خنثی‌شده", False) in adapted


def test_cli_locked_override_is_scriptable(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    save_registry(config / "terminology.yaml", registry())
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "set-terminology",
            "Veyra",
            "ویرا",
            "--type",
            "person",
            "--decision-source",
            "user",
            "--locked",
        ],
    )
    assert result.exit_code == 0
    persisted = yaml.safe_load((config / "terminology.yaml").read_text(encoding="utf-8"))
    assert persisted["decisions"][0]["locked"] is True


def test_cli_validation_is_read_only(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    save_registry(config / "terminology.yaml", decided_registry())
    source = tmp_path / "source.md"
    translation = tmp_path / "translation.md"
    source.write_text("Veyra arrived.\n", encoding="utf-8")
    translation.write_text("ویرا آمد.\n", encoding="utf-8")
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "validate-translation-qa",
            "--source",
            str(source),
            "--translation",
            str(translation),
        ],
    )

    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert result.exit_code == 0
    assert after == before
