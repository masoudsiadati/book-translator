"""Occurrence-specific terminology exemptions: one adjudicated span, never the whole spelling.

Every sentence here is invented. The shape mirrors the real defect: a story term whose lowercase
alias is also an ordinary English verb, where one occurrence is the story sense and another is
someone keeping still.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from book_translator.historical_reevaluation_policy import evaluate_exemption_compatibility
from book_translator.models import (
    DecisionSource,
    HistoricalCompatibilityTier,
    HistoricalRequestCompatibility,
    OccurrenceSense,
    SeriesTerminologyPolicy,
    SeriesTermStatus,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.prompt_packaging.rendering import render_terminology
from book_translator.semantic_qa.engine import build_tasks
from book_translator.series_terminology.context import build_effective_context
from book_translator.series_terminology.policy import empty_policy, set_explicit_term
from book_translator.terminology import set_decision, validate_terminology
from book_translator.terminology.exemptions import (
    TerminologyExemptionError,
    plan_occurrence_exemption,
    resolve_occurrence_exemptions,
    with_exemption,
)
from book_translator.terminology.registry import (
    ExemptOccurrences,
    TerminologyError,
    applicable_decisions,
    canonically_matched_decisions,
)

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
CANONICAL = "خنثی‌شده"
# Invented renderings: the story sense, and an ordinary "made still".
STORY_SENSE = "خواهر خنثی‌شده بود."
ORDINARY_SENSE = "مرا دستانش را آرام کرد."

POWER_PARAGRAPH = "The envoy had been stilled at the tower."
ORDINARY_PARAGRAPH = "Mira stilled her hands and waited."


def _registry() -> TerminologyRegistry:
    base = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
    )
    return set_decision(
        base,
        source_term="Stilled",
        target_term=CANONICAL,
        classification=TerminologyType.STORY_TERM,
        decision_source=DecisionSource.USER,
        locked=True,
        aliases=("stilled",),
        allowed_target_forms=("خنثی شده", "خنثی شدن"),
    )


def _policy() -> SeriesTerminologyPolicy:
    return set_explicit_term(
        empty_policy(series_id="synthetic-series", source_language="en", target_language="fa"),
        source_term="Stilled",
        target_term=CANONICAL,
        classification=TerminologyType.STORY_TERM,
        status=SeriesTermStatus.LOCKED,
        rationale="participle decision",
        aliases=("stilled",),
        allowed_target_forms=("خنثی شده", "خنثی شدن"),
    )


def _exempt(
    source: str,
    occurrence: int,
    *,
    registry: TerminologyRegistry | None = None,
    policy: SeriesTerminologyPolicy | None = None,
    chunk_id: str = "012-001",
) -> tuple[Any, ExemptOccurrences]:
    registry = registry or _registry()
    planned = plan_occurrence_exemption(
        registry=registry,
        policy=policy,
        chunk_id=chunk_id,
        source_text=source,
        spelling="stilled",
        occurrence_index=occurrence,
        sense=OccurrenceSense.ORDINARY_ENGLISH,
        rationale="ordinary English: keeping still",
        created_at=NOW,
    )
    exemptions, added = with_exemption(None, planned.record, book_id="synthetic-book")
    assert added
    resolved = resolve_occurrence_exemptions(
        exemptions, registry=registry, policy=policy, chunk_id=chunk_id, source_text=source
    )
    assert resolved is not None
    return exemptions, resolved


def _codes(source: str, translation: str, exemptions: ExemptOccurrences | None) -> set[str]:
    report = validate_terminology(source, translation, _registry(), exemptions)
    return {item.code for item in report.findings if item.severity == "error"}


# --- QA: the ordinary sense stops selecting the decision --------------------------------------


def test_ordinary_sense_occurrence_is_rejected_without_and_accepted_with_its_exemption() -> None:
    source = ORDINARY_PARAGRAPH + "\n"
    translation = ORDINARY_SENSE + "\n"
    _, exempt = _exempt(source, 1)

    assert _codes(source, translation, None) == {"canonical_term_missing"}
    assert _codes(source, translation, exempt) == set()


def test_one_power_occurrence_beside_an_exempt_one_stays_governed() -> None:
    source = f"{POWER_PARAGRAPH}\n\n{ORDINARY_PARAGRAPH}\n"
    _, exempt = _exempt(source, 2)

    # The story sense still demands the canonical target even though its neighbour is exempt.
    assert _codes(source, "سفیر در برج آرام شد.\n\n" + ORDINARY_SENSE + "\n", exempt) == {
        "canonical_term_missing"
    }
    assert _codes(source, STORY_SENSE + "\n\n" + ORDINARY_SENSE + "\n", exempt) == set()


def test_an_exemption_names_one_occurrence_not_the_spelling() -> None:
    source = f"{ORDINARY_PARAGRAPH}\n\n{POWER_PARAGRAPH}\n"
    _, exempt = _exempt(source, 1)
    registry = _registry()

    assert canonically_matched_decisions(registry, source, exempt)
    assert canonically_matched_decisions(registry, ORDINARY_PARAGRAPH + "\n") != ()


# --- prompt selection honours the same span -------------------------------------------------


def test_prompt_selection_drops_a_decision_only_its_exempt_occurrence_selected() -> None:
    registry = _registry()
    ordinary_only = ORDINARY_PARAGRAPH + "\n"
    _, exempt = _exempt(ordinary_only, 1)
    assert applicable_decisions(registry, ordinary_only, "", exempt) == ()
    assert CANONICAL not in render_terminology(registry, ordinary_only, "", None, exempt)
    assert CANONICAL in render_terminology(registry, ordinary_only, "")

    mixed = f"{POWER_PARAGRAPH}\n\n{ORDINARY_PARAGRAPH}\n"
    _, mixed_exempt = _exempt(mixed, 2)
    assert len(applicable_decisions(registry, mixed, "", mixed_exempt)) == 1


def test_the_same_words_in_the_context_window_stay_governed() -> None:
    registry = _registry()
    source = ORDINARY_PARAGRAPH + "\n"
    _, exempt = _exempt(source, 1)
    assert len(applicable_decisions(registry, source, ORDINARY_PARAGRAPH, exempt)) == 1


def test_series_policy_tier_withholds_only_the_exempt_concept_span() -> None:
    policy = _policy()
    source = ORDINARY_PARAGRAPH + "\n"
    _, exempt = _exempt(source, 1, policy=policy)

    assert build_effective_context(policy, source_text=source).locked
    assert not build_effective_context(policy, source_text=source, exemptions=exempt).locked
    mixed = f"{POWER_PARAGRAPH}\n\n{ORDINARY_PARAGRAPH}\n"
    _, mixed_exempt = _exempt(mixed, 2, policy=policy)
    assert build_effective_context(policy, source_text=mixed, exemptions=mixed_exempt).locked


def test_semantic_constraints_are_rebased_onto_the_exempt_paragraph_only() -> None:
    source = f"{POWER_PARAGRAPH}\n\n{ORDINARY_PARAGRAPH}\n"
    translation = f"{STORY_SENSE}\n\n{ORDINARY_SENSE}\n"
    _, exempt = _exempt(source, 2)
    tasks = build_tasks(source, translation, registry=_registry(), exemptions=exempt)

    assert [bool(task.terminology) for task in tasks] == [True, False]
    unexempted = build_tasks(source, translation, registry=_registry())
    assert [bool(task.terminology) for task in unexempted] == [True, True]


# --- fail closed ------------------------------------------------------------------------------


def test_a_changed_source_refuses_the_exemption() -> None:
    source = ORDINARY_PARAGRAPH + "\n"
    exemptions, _ = _exempt(source, 1)
    with pytest.raises(TerminologyExemptionError, match="source changed"):
        resolve_occurrence_exemptions(
            exemptions,
            registry=_registry(),
            policy=None,
            chunk_id="012-001",
            source_text=source.replace("Mira", "Nora"),
        )


def test_resolved_spans_are_refused_against_any_other_text() -> None:
    source = ORDINARY_PARAGRAPH + "\n"
    _, exempt = _exempt(source, 1)
    with pytest.raises(TerminologyError, match="different source text"):
        validate_terminology(POWER_PARAGRAPH + "\n", STORY_SENSE + "\n", _registry(), exempt)


def test_a_redecided_term_refuses_the_exemption() -> None:
    source = ORDINARY_PARAGRAPH + "\n"
    exemptions, _ = _exempt(source, 1)
    retargeted = set_decision(
        TerminologyRegistry(
            schema_version="1.0",
            version="1.0.0",
            book_id="synthetic-book",
            source_language="en",
            target_language="fa",
        ),
        source_term="Stilled",
        target_term="بریده‌شده",
        classification=TerminologyType.STORY_TERM,
        decision_source=DecisionSource.USER,
        locked=True,
        aliases=("stilled",),
    )
    with pytest.raises(TerminologyExemptionError, match="decision changed"):
        resolve_occurrence_exemptions(
            exemptions,
            registry=retargeted,
            policy=None,
            chunk_id="012-001",
            source_text=source,
        )


def test_an_ordinal_that_names_nothing_is_refused() -> None:
    with pytest.raises(TerminologyExemptionError, match="not an occurrence 2"):
        _exempt(ORDINARY_PARAGRAPH + "\n", 2)


def test_an_exemption_is_scoped_to_its_own_chunk() -> None:
    source = ORDINARY_PARAGRAPH + "\n"
    exemptions, _ = _exempt(source, 1)
    assert (
        resolve_occurrence_exemptions(
            exemptions, registry=_registry(), policy=None, chunk_id="041-002", source_text=source
        )
        is None
    )


def test_re_adjudicating_an_occurrence_differently_is_refused() -> None:
    source = ORDINARY_PARAGRAPH + "\n"
    exemptions, _ = _exempt(source, 1)
    record = exemptions.exemptions[0]
    same, added = with_exemption(exemptions, record, book_id="synthetic-book")
    assert same == exemptions and not added
    moved = record.model_copy(update={"decision_id": "td-" + "0" * 24})
    with pytest.raises(TerminologyExemptionError, match="different exemption"):
        with_exemption(exemptions, moved, book_id="synthetic-book")


# --- historical carry-forward: exemptions must be the whole difference ------------------------


def _package(user_sha: str) -> Any:
    reference = SimpleNamespace(sha256="a" * 64, version="1.0.0")
    return SimpleNamespace(
        chunk_source_sha256="1" * 64,
        context_sha256="2" * 64,
        source_language="en",
        target_language="fa",
        system_message=SimpleNamespace(sha256="3" * 64),
        user_message=SimpleNamespace(sha256=user_sha),
        output_contract_version="persian-translation-marked-paragraphs/1.0.0",
        terminology=reference,
        prompt=reference,
        glossary=reference,
        style_guide=reference,
        source_anomalies=None,
        series_terminology=reference,
        reference_context=reference,
        terminology_context_contract_version="1.1.0",
        renderer_version="1.5.0",
        total_chapter_chunks=4,
    )


@pytest.mark.parametrize(
    ("counterfactual", "tier", "reasons"),
    [
        ("4" * 64, HistoricalCompatibilityTier.OCCURRENCE_EXEMPTION_DRIFT, ()),
        (
            "6" * 64,
            HistoricalCompatibilityTier.UNSAFE_DRIFT,
            ("occurrence_exemptions_not_whole_difference",),
        ),
    ],
)
def test_exemption_tier_requires_the_counterfactual_to_reproduce_the_history(
    counterfactual: str, tier: HistoricalCompatibilityTier, reasons: tuple[str, ...]
) -> None:
    verdict = evaluate_exemption_compatibility(
        _package("4" * 64),
        _package("5" * 64),
        old_sections={"terminology-policy": "7" * 64},
        new_sections={"terminology-policy": "8" * 64},
        historical_output_contract="persian-translation-marked-paragraphs/1.0.0",
        counterfactual_user_message_sha256=counterfactual,
        exemption_ids=("toe-" + "9" * 24,),
    )
    assert verdict.tier is tier
    assert verdict.blocking_reasons == reasons


def test_persisted_exemption_tier_must_carry_its_proof() -> None:
    fields: dict[str, Any] = {
        "policy_version": "1.2.0",
        "tier": HistoricalCompatibilityTier.OCCURRENCE_EXEMPTION_DRIFT,
        "source_snapshot_id": "translation-fa-" + "a" * 24,
        "target_snapshot_id": "translation-fa-" + "b" * 24,
        "source_prompt_package_id": "pp-" + "c" * 24,
        "target_prompt_package_id": "pp-" + "d" * 24,
        "source_system_message_sha256": "3" * 64,
        "target_system_message_sha256": "3" * 64,
        "source_user_message_sha256": "4" * 64,
        "target_user_message_sha256": "5" * 64,
        "source_output_contract": "c",
        "target_output_contract": "c",
        "verified_identity_checks": ["source_hash"],
        "rationale": "exemptions",
        "occurrence_exemption_ids": ["toe-" + "9" * 24],
        "counterfactual_user_message_sha256": "4" * 64,
    }
    HistoricalRequestCompatibility.model_validate(fields)
    with pytest.raises(ValueError, match="whole difference"):
        HistoricalRequestCompatibility.model_validate(
            {**fields, "counterfactual_user_message_sha256": "6" * 64}
        )
    with pytest.raises(ValueError, match="belong to one tier"):
        HistoricalRequestCompatibility.model_validate({**fields, "occurrence_exemption_ids": []})
