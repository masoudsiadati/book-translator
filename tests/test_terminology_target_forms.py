"""Explicitly authorized target realizations of one canonical decision (terminology QA 1.8.0).

A canonical decision names one preferred target rendering. Some governed concepts are legitimately
written in more than one grammatical shape in the target language — a participle and a verbal
noun of the same term, say — and "exact" enforcement used to accept only the preferred literal.
A decision may now list ``allowed_target_forms``: further realizations a person or the project
explicitly authorized. Exact enforcement means at least one authorized realization occurs, each
matched with the same lexical boundaries and contracted inflections as the preferred target.

Since terminology QA 1.11.0 an authorized compound verb also conjugates (``خنثی شدند``, and the
transitive ``خنثی کردن``), which is grammar applied to the authorized form, not a new form. Every
negative case below is a form that looks related — a shared word, a longer token, a paraphrase, a
derivation — and stays a violation because nobody authorized it.
The source terms are synthetic; the Persian forms are generic vocabulary, not book prose.
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.final_qa.analysis import analyze_competing_variants
from book_translator.models import (
    CompetingVariantClass,
    DecisionSource,
    ObservedVariant,
    ObservedVariantSupport,
    ReferenceEvidenceQuality,
    ReferenceEvidenceSource,
    SeriesProvenanceKind,
    SeriesTerm,
    SeriesTerminologyPolicy,
    SeriesTermProvenance,
    SeriesTermStatus,
    TerminologyDecision,
    TerminologyEnforcement,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.series_terminology.bootstrap import (
    build_policy,
    explicit_decisions_from_registry,
)
from book_translator.series_terminology.derivation import ChunkText, derive_book_registry
from book_translator.series_terminology.identity import canonical_policy_bytes, concept_identity
from book_translator.series_terminology.policy import (
    SeriesTerminologyError,
    load_policy,
    save_policy,
    serialized_policy,
    set_explicit_term,
)
from book_translator.terminology import (
    TerminologyError,
    load_registry,
    save_registry,
    set_decision,
    validate_terminology,
)
from book_translator.terminology.enforcement import resolve_enforcement
from book_translator.terminology.registry import canonical_registry_bytes

runner = CliRunner()
ZWNJ = "‌"

#: The preferred rendering: a participle written with a ZWNJ.
PREFERRED = f"خنثی{ZWNJ}شده"
#: The same participle written with a space. Normalization does not equate the two.
SPACED = "خنثی شده"
#: The verbal-noun realization of the same concept.
VERBAL = "خنثی شدن"
FORMS = (SPACED, VERBAL)

SOURCE_TERM = "hushed"
SOURCE = "He was hushed before dawn.\n"


def _registry(
    *,
    forms: tuple[str, ...] = FORMS,
    competing: tuple[str, ...] = (),
    source_term: str = SOURCE_TERM,
    target_term: str = PREFERRED,
    classification: TerminologyType = TerminologyType.STORY_TERM,
    decision_source: DecisionSource = DecisionSource.USER,
    locked: bool = True,
    enforcement: TerminologyEnforcement | None = None,
) -> TerminologyRegistry:
    return set_decision(
        TerminologyRegistry(
            schema_version="1.0",
            version="1.0.0",
            book_id="synthetic-book",
            source_language="en",
            target_language="fa",
        ),
        source_term=source_term,
        target_term=target_term,
        classification=classification,
        decision_source=decision_source,
        locked=locked,
        competing_variants=competing,
        allowed_target_forms=forms,
        enforcement=enforcement,
    )


def _codes(source: str, translation: str, registry: TerminologyRegistry) -> set[str]:
    return {item.code for item in validate_terminology(source, translation, registry).findings}


# ------------------------------------------------------------------------------------------
# Accepted realizations
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        PREFERRED,
        SPACED,
        VERBAL,
        # Contracted inflections apply to every authorized realization, not only the preferred one.
        f"{PREFERRED}{ZWNJ}اش",
        f"{PREFERRED}{ZWNJ}ای",
        f"{SPACED}{ZWNJ}شان",
        f"{VERBAL}ش",
        f"{VERBAL}شان",
        f"{VERBAL}{ZWNJ}ش",
        # An authorized compound verb conjugates, in either voice, with its own separators
        # (terminology QA 1.11.0).
        "خنثی شدند",
        "خنثی کردن",
        f"خنثی{ZWNJ}شدن",
    ],
)
def test_every_authorized_realization_satisfies_exact_enforcement(written: str) -> None:
    registry = _registry()
    assert resolve_enforcement(registry.candidates[0], registry.decisions[0]) is (
        TerminologyEnforcement.EXACT
    )

    report = validate_terminology(SOURCE, f"او پیش از سپیده {written} را پذیرفت.\n", registry)

    assert report.valid, [item.code for item in report.findings]
    assert not report.findings


@pytest.mark.parametrize(
    "written",
    [
        # An unrelated phrase that shares a word with the authorized forms.
        "خنثی ماند",
        # Authorized forms as accidental substrings of longer tokens.
        f"نا{PREFERRED}",
        f"{VERBAL}گاه",
        f"{SPACED}ها",
        # An unlisted semantic paraphrase.
        f"بی{ZWNJ}اثر شده",
        # A derivation that is not a conjugation: the participle nominalized with its own stem
        # change.
        f"خنثی{ZWNJ}شدگان",
    ],
)
def test_what_nobody_authorized_is_still_a_missing_canonical(written: str) -> None:
    registry = _registry()

    report = validate_terminology(SOURCE, f"او پیش از سپیده {written} را پذیرفت.\n", registry)

    assert not report.valid
    assert "canonical_term_missing" in {item.code for item in report.findings}


@pytest.mark.parametrize(
    ("written", "accepted"),
    [(PREFERRED, True), (SPACED, False), (VERBAL, False), (f"{PREFERRED}{ZWNJ}اش", True)],
)
def test_a_decision_without_authorized_forms_keeps_the_literal_exact_behavior(
    written: str, accepted: bool
) -> None:
    registry = _registry(forms=())
    assert registry.decisions[0].allowed_target_forms == []

    report = validate_terminology(SOURCE, f"او {written} را پذیرفت.\n", registry)

    assert report.valid is accepted
    assert ("canonical_term_missing" in {item.code for item in report.findings}) is not accepted


def test_the_space_variant_is_accepted_only_because_it_is_listed() -> None:
    # Spacing is never normalized: with only the ZWNJ spelling authorized the spaced participle is
    # another spelling. A decision that authorizes a spaced form of the compound (the participle,
    # or since 1.11.0 the spaced verbal noun it conjugates from) admits the spaced participle.
    translation = f"او {SPACED} را پذیرفت.\n"
    assert "canonical_term_missing" in _codes(SOURCE, translation, _registry(forms=()))
    assert not _codes(SOURCE, translation, _registry(forms=(SPACED,)))
    assert not _codes(SOURCE, translation, _registry(forms=(VERBAL,)))


def test_the_rule_is_generic_rather_than_bound_to_one_term() -> None:
    registry = _registry(
        source_term="Keeper",
        target_term="نگهبان",
        forms=("پاسدار",),
        classification=TerminologyType.TITLE,
    )
    assert not _codes("The Keeper waited.\n", "پاسدارش منتظر ماند.\n", registry)
    assert "canonical_term_missing" in _codes(
        "The Keeper waited.\n", "پاسدارانه منتظر ماند.\n", registry
    )


def test_authorized_forms_are_a_target_side_rule_for_any_language() -> None:
    registry = TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-book",
        source_language="fa",
        target_language="en",
    )
    registry = set_decision(
        registry,
        source_term="نگهبان",
        target_term="Warder",
        classification=TerminologyType.TITLE,
        decision_source=DecisionSource.USER,
        locked=True,
        allowed_target_forms=("Warders",),
    )
    assert not _codes("نگهبان آمد.\n", "The Warders came.\n", registry)
    assert "canonical_term_missing" in _codes("نگهبان آمد.\n", "The Wardership came.\n", registry)


def test_mixed_realizations_account_for_every_governed_occurrence() -> None:
    registry = _registry(competing=("بی" + ZWNJ + "اثر",))
    source = "He was hushed, and she was hushed too.\n"

    assert not _codes(source, f"او {PREFERRED} شد و او هم به {VERBAL} تن داد.\n", registry)
    codes = _codes(source, f"او {PREFERRED} شد و او هم بی{ZWNJ}اثر ماند.\n", registry)
    assert codes == {"competing_variant"}


def _with_recorded_variant(registry: TerminologyRegistry, variant: str) -> TerminologyRegistry:
    decision = registry.decisions[0].model_copy(update={"competing_variants": [variant]})
    return registry.model_copy(update={"decisions": [decision]})


def test_a_competing_variant_inside_an_authorized_form_is_not_a_rival() -> None:
    # The governed edit refuses this shape; a registry written by other means can still carry it,
    # and enforcement must not turn part of an authorized realization into a rival rendering.
    source = "He was hushed, and she was hushed too.\n"
    # One realization for two governed occurrences is a deficit, so only the enforceability of the
    # recorded variant decides whether a finding appears.
    translation = f"او {VERBAL} را پذیرفت و او آرام ماند.\n"

    authorized = _with_recorded_variant(_registry(forms=(VERBAL,)), "شدن")
    unauthorized = _with_recorded_variant(_registry(forms=()), "شدن")

    assert not _codes(source, translation, authorized)
    assert _codes(source, translation, unauthorized) == {
        "canonical_term_missing",
        "competing_variant",
    }


def test_contextual_and_morphological_enforcement_also_honor_authorized_forms() -> None:
    contextual = _registry(
        locked=False,
        decision_source=DecisionSource.AI,
        enforcement=TerminologyEnforcement.CONTEXTUAL,
    )
    assert not _codes(SOURCE, f"او {VERBAL} را پذیرفت.\n", contextual)
    assert "canonical_term_unverified" in _codes(SOURCE, "او آرام شد.\n", contextual)

    morphological = _registry(
        locked=False,
        decision_source=DecisionSource.AI,
        enforcement=TerminologyEnforcement.MORPHOLOGICAL,
        target_term="نگهبان",
        forms=("پاسداران",),
    )
    # Morphological enforcement strips derivation from the authorized form itself, as it always
    # has from the preferred target; it never widens what the translation's own tokens may be.
    assert not _codes(SOURCE, "پاسدار آمد.\n", morphological)
    assert "canonical_term_missing" in _codes(SOURCE, "پاسدارها آمدند.\n", morphological)


# ------------------------------------------------------------------------------------------
# Registry contract
# ------------------------------------------------------------------------------------------


def _decision(**updates: object) -> TerminologyDecision:
    payload: dict[str, object] = {
        "decision_id": "td-" + "0" * 24,
        "candidate_id": "tc-" + "0" * 24,
        "target_term": PREFERRED,
        "decision_source": DecisionSource.USER,
    }
    payload.update(updates)
    return TerminologyDecision.model_validate(payload)


@pytest.mark.parametrize(
    "updates",
    [
        {"allowed_target_forms": [PREFERRED]},
        {"allowed_target_forms": [VERBAL, VERBAL]},
        {"allowed_target_forms": [""]},
        {"allowed_target_forms": [f" {VERBAL}"]},
        {"allowed_target_forms": [VERBAL], "competing_variants": [VERBAL]},
    ],
    ids=["repeats_target", "duplicate", "empty", "unstripped", "also_competing"],
)
def test_decision_contract_rejects_ambiguous_authorized_forms(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _decision(**updates)


def test_set_decision_refuses_a_competing_variant_inside_an_authorized_form() -> None:
    with pytest.raises(TerminologyError, match="authorized target form"):
        _registry(forms=(VERBAL,), competing=("شدن",))


def test_a_registry_without_authorized_forms_serializes_exactly_as_before(tmp_path: Path) -> None:
    registry = _registry(forms=())
    assert "allowed_target_forms" not in registry.decisions[0].model_dump(mode="json")
    assert b"allowed_target_forms" not in canonical_registry_bytes(registry)
    path = tmp_path / "registry.yaml"
    save_registry(path, registry)
    assert "allowed_target_forms" not in path.read_text(encoding="utf-8")


def test_authorized_forms_survive_a_registry_round_trip_in_order(tmp_path: Path) -> None:
    registry = _registry(forms=(VERBAL, SPACED))
    assert registry.decisions[0].allowed_target_forms == [VERBAL, SPACED]
    path = tmp_path / "registry.yaml"
    save_registry(path, registry)
    persisted = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert persisted["decisions"][0]["allowed_target_forms"] == [VERBAL, SPACED]
    assert canonical_registry_bytes(load_registry(path)) == canonical_registry_bytes(registry)


def test_cli_set_terminology_records_authorized_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    save_registry(config / "terminology.yaml", _registry(forms=(), locked=False))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "set-terminology",
            SOURCE_TERM,
            PREFERRED,
            "--type",
            "story_term",
            "--target-form",
            VERBAL,
            "--target-form",
            SPACED,
        ],
    )

    assert result.exit_code == 0, result.output
    persisted = yaml.safe_load((config / "terminology.yaml").read_text(encoding="utf-8"))
    assert persisted["decisions"][0]["allowed_target_forms"] == [VERBAL, SPACED]


# ------------------------------------------------------------------------------------------
# Series policy and derived registry
# ------------------------------------------------------------------------------------------


def _term(
    source_term: str,
    *,
    status: SeriesTermStatus = SeriesTermStatus.LOCKED,
    target_term: str | None = PREFERRED,
    forms: tuple[str, ...] = (),
    kind: SeriesProvenanceKind = SeriesProvenanceKind.EXPLICIT_PROJECT,
    variants: tuple[ObservedVariant, ...] = (),
) -> SeriesTerm:
    return SeriesTerm(
        concept_id=concept_identity(source_term, "en"),
        source_term=source_term,
        classification=TerminologyType.STORY_TERM,
        status=status,
        target_term=target_term,
        allowed_target_forms=list(forms),
        provenance=SeriesTermProvenance(kind=kind, origin="synthetic", rationale="synthetic"),
        variants=list(variants),
    )


def _policy(*terms: SeriesTerm) -> SeriesTerminologyPolicy:
    return SeriesTerminologyPolicy(
        schema_version="1.0",
        version="1.0.0",
        series_id="synthetic-series",
        source_language="en",
        target_language="fa",
        promotion_rule_version="synthetic/1.0.0",
        terms=list(terms),
    )


def _narrative(form: str) -> ObservedVariant:
    return ObservedVariant(
        target_form=form,
        support=[
            ObservedVariantSupport(
                corpus_id="book1",
                source=ReferenceEvidenceSource.NARRATIVE,
                quality=ReferenceEvidenceQuality.CLEAN,
                observation_count=3,
            )
        ],
    )


CHUNKS = (ChunkText("001-001", "001", SOURCE, ""),)


def test_policy_authorized_forms_project_onto_the_derived_decision() -> None:
    policy = _policy(_term(SOURCE_TERM, forms=(VERBAL,), variants=(_narrative(VERBAL),)))

    registry, _ = derive_book_registry(policy, book_id="synthetic-book", chunks=CHUNKS)

    decision = registry.decisions[0]
    assert decision.enforcement is TerminologyEnforcement.EXACT
    assert decision.allowed_target_forms == [VERBAL]
    # Corpus evidence for an authorized form is agreement, never a rival to enforce.
    assert decision.competing_variants == []
    assert not _codes(SOURCE, f"او {VERBAL} را پذیرفت.\n", registry)


def test_a_policy_without_authorized_forms_derives_and_serializes_as_before() -> None:
    policy = _policy(_term(SOURCE_TERM, variants=(_narrative(VERBAL),)))

    registry, _ = derive_book_registry(policy, book_id="synthetic-book", chunks=CHUNKS)

    assert registry.decisions[0].allowed_target_forms == []
    assert registry.decisions[0].competing_variants == [VERBAL]
    assert "allowed_target_forms" not in serialized_policy(policy)
    assert b"allowed_target_forms" not in canonical_policy_bytes(policy)
    assert b"allowed_target_forms" not in canonical_registry_bytes(registry)
    assert "canonical_term_missing" in _codes(SOURCE, f"او {VERBAL} را پذیرفت.\n", registry)


@pytest.mark.parametrize(
    "build",
    [
        lambda: _term(
            SOURCE_TERM,
            status=SeriesTermStatus.OBSERVED_VARIANTS,
            target_term=None,
            forms=(VERBAL,),
            kind=SeriesProvenanceKind.UNRESOLVED,
            variants=(_narrative(VERBAL),),
        ),
        lambda: _term(SOURCE_TERM, forms=(PREFERRED,)),
        lambda: _term(SOURCE_TERM, forms=(VERBAL, VERBAL)),
    ],
    ids=["undecided_term", "repeats_target", "duplicate"],
)
def test_policy_contract_rejects_unauthorizable_forms(build: object) -> None:
    with pytest.raises(ValidationError):
        build()  # type: ignore[operator]


def test_explicit_user_edit_authorizes_forms_and_round_trips(tmp_path: Path) -> None:
    edited = set_explicit_term(
        _policy(_term(SOURCE_TERM)),
        source_term=SOURCE_TERM,
        target_term=PREFERRED,
        classification=TerminologyType.STORY_TERM,
        rationale="participle and verbal noun are one governed concept",
        allowed_target_forms=(VERBAL, SPACED),
    )
    term = edited.terms[0]
    assert term.provenance.kind is SeriesProvenanceKind.EXPLICIT_USER
    assert term.allowed_target_forms == [VERBAL, SPACED]
    path = tmp_path / "policy.yaml"
    save_policy(edited, path)
    assert canonical_policy_bytes(load_policy(path)) == canonical_policy_bytes(edited)
    with pytest.raises(SeriesTerminologyError, match="authorized target form"):
        set_explicit_term(
            edited,
            source_term=SOURCE_TERM,
            target_term=PREFERRED,
            classification=TerminologyType.STORY_TERM,
            rationale="invalid",
            allowed_target_forms=(PREFERRED,),
        )


def test_policy_import_carries_forms_a_user_decision_authorized() -> None:
    registry = _registry(forms=(VERBAL,), locked=False)

    policy, report = build_policy(
        series_id="synthetic-series",
        source_language="en",
        target_language="fa",
        corpus_concepts=(),
        explicit=explicit_decisions_from_registry(registry, origin="config/terminology.yaml"),
    )

    term = policy.terms[0]
    assert term.status is SeriesTermStatus.LOCKED
    assert term.allowed_target_forms == [VERBAL]
    assert report.locked == 1


# ------------------------------------------------------------------------------------------
# Final-QA analysis agrees with the validator
# ------------------------------------------------------------------------------------------


def test_final_qa_analysis_counts_authorized_forms_as_the_canonical() -> None:
    rival = f"بی{ZWNJ}اثر"
    translation = f"او {VERBAL} را پذیرفت و {rival} ماند.\n"

    (with_forms,) = analyze_competing_variants(
        source_text=SOURCE,
        translation_text=translation,
        frozen_registry=_registry(forms=(VERBAL,), competing=(rival,)),
    )
    (without_forms,) = analyze_competing_variants(
        source_text=SOURCE,
        translation_text=translation,
        frozen_registry=_registry(forms=(), competing=(rival,)),
    )

    assert with_forms.canonical_present and with_forms.canonical_occurrences == 1
    assert not with_forms.enforceable
    assert not without_forms.canonical_present and without_forms.canonical_occurrences == 0
    assert without_forms.variant_class is CompetingVariantClass.CANONICAL_ABSENT
    assert without_forms.enforceable
