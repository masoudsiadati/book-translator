"""Precedence between governed concepts whose source forms name one fictional concept.

A series policy keys a concept on its exact source term, so an explicit project decision on
"the Dark One" and a corpus promotion of "Dark One" become two concepts with two targets. Every
prompt path then renders both and every enforcement path checks only the longer, which is how a
repair can be told to write the weaker rendering and be rejected for it.

Everything here is synthetic. The Persian strings are invented forms that only need to be
distinct from one another.
"""

import pytest

from book_translator.models import (
    SCHEMA_VERSION,
    CandidateStatus,
    DecisionSource,
    DecisionState,
    ObservedVariant,
    ObservedVariantSupport,
    ReferenceEvidenceQuality,
    ReferenceEvidenceSource,
    SeriesPolicyAmendmentRequest,
    SeriesProvenanceKind,
    SeriesTerm,
    SeriesTermAmendment,
    SeriesTerminologyPolicy,
    SeriesTermProvenance,
    SeriesTermRetirement,
    SeriesTermStatus,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyEnforcement,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.series_terminology.amendment import AmendedPolicy, apply_amendment
from book_translator.series_terminology.corpus import CorpusConcept, CorpusForm
from book_translator.series_terminology.identity import concept_identity
from book_translator.terminology.concept_precedence import (
    DecisionRank,
    concept_key,
    decision_rank,
    policy_concept_conflicts,
    resolve_precedence,
    strip_leading_article,
)

TARGET_LOCKED = "ارباب تاریکی"
TARGET_PROMOTED = "اهریمن"
TARGET_POWER_LOCKED = "قدرت یگانه"
TARGET_POWER_PROMOTED = "نیروی یگانه"


def _support(count: int = 40) -> ObservedVariantSupport:
    return ObservedVariantSupport(
        corpus_id="book1",
        source=ReferenceEvidenceSource.NARRATIVE,
        quality=ReferenceEvidenceQuality.CLEAN,
        observation_count=count,
    )


def _term(
    source_term: str,
    *,
    status: SeriesTermStatus,
    target: str | None,
    kind: SeriesProvenanceKind,
    aliases: tuple[str, ...] = (),
) -> SeriesTerm:
    decided = status is not SeriesTermStatus.OBSERVED_VARIANTS
    # A promoted term always carries the corpus evidence that justified it; a locked project
    # decision comes from the glossary and carries none.
    promoted = kind is SeriesProvenanceKind.PROMOTED_REFERENCE
    variants = (
        [ObservedVariant(target_form=target or "x", support=[_support()])]
        if promoted or not decided
        else []
    )
    return SeriesTerm(
        concept_id=concept_identity(source_term, "en"),
        source_term=source_term,
        aliases=list(aliases),
        classification=TerminologyType.STORY_TERM,
        status=status,
        target_term=target if decided else None,
        provenance=SeriesTermProvenance(kind=kind, origin="test", rationale="test"),
        variants=variants,
    )


def _locked_project(source: str, target: str, aliases: tuple[str, ...] = ()) -> SeriesTerm:
    return _term(
        source,
        status=SeriesTermStatus.LOCKED,
        target=target,
        kind=SeriesProvenanceKind.EXPLICIT_PROJECT,
        aliases=aliases,
    )


def _promoted(source: str, target: str) -> SeriesTerm:
    return _term(
        source,
        status=SeriesTermStatus.STRONG_REFERENCE,
        target=target,
        kind=SeriesProvenanceKind.PROMOTED_REFERENCE,
    )


# ---------------------------------------------------------------------------------------------
# Concept keying: an article and a capital are not a different fictional concept
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("the Dark One", "Dark One"),
        ("The One Power", "One Power"),
        ("a Grey Man", "Grey Man"),
        ("An Aiel", "Aiel"),
        ("Dark One", "Dark One"),
        # Only a *leading* article, and only a whole word.
        ("Theodrin", "Theodrin"),
        ("Andor", "Andor"),
        ("Children of the Light", "Children of the Light"),
    ],
)
def test_strip_leading_article_removes_only_a_leading_whole_word_article(
    term: str, expected: str
) -> None:
    assert strip_leading_article(term) == expected


def test_article_and_case_variants_share_one_concept_key() -> None:
    assert concept_key("the Dark One") == concept_key("Dark One")
    assert concept_key("The One Power") == concept_key("One Power")
    assert concept_key("the dark one") == concept_key("THE DARK ONE")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Every boundary-valid containment pair in the real Book 6 policy that is a genuinely
        # different concept. None of these may ever be collapsed.
        ("Dragon", "false Dragon"),
        ("Dragon", "Dragon's Fang"),
        ("Moiraine", "Moiraine Sedai"),
        ("Blight", "Great Blight"),
        ("angreal", "ter'angreal"),
        ("angreal", "sa'angreal"),
        ("damane", "marath'damane"),
        ("the Light", "Children of the Light"),
        ("Rand", "Rand al'Thor"),
        ("al'Thor", "Rand al'Thor"),
        ("Two Rivers", "Two Rivers folk"),
    ],
)
def test_nested_but_distinct_concepts_never_share_a_concept_key(left: str, right: str) -> None:
    assert concept_key(left) != concept_key(right)


# ---------------------------------------------------------------------------------------------
# Precedence ordering
# ---------------------------------------------------------------------------------------------


def test_decision_rank_orders_user_over_project_over_other_locked_over_strong_reference() -> None:
    user_locked = decision_rank(SeriesTermStatus.LOCKED, SeriesProvenanceKind.EXPLICIT_USER)
    project_locked = decision_rank(SeriesTermStatus.LOCKED, SeriesProvenanceKind.EXPLICIT_PROJECT)
    other_locked = decision_rank(SeriesTermStatus.LOCKED, SeriesProvenanceKind.PRIOR_VOLUME)
    strong = decision_rank(
        SeriesTermStatus.STRONG_REFERENCE, SeriesProvenanceKind.PROMOTED_REFERENCE
    )
    weak = decision_rank(SeriesTermStatus.OBSERVED_VARIANTS, SeriesProvenanceKind.UNRESOLVED)
    assert user_locked > project_locked > other_locked > strong > weak
    assert user_locked is DecisionRank.EXPLICIT_USER_LOCKED
    assert weak is DecisionRank.WEAKER_EVIDENCE


def test_an_explicit_user_decision_outranks_an_explicit_project_decision() -> None:
    user = _term(
        "the Dark One",
        status=SeriesTermStatus.LOCKED,
        target="x",
        kind=SeriesProvenanceKind.EXPLICIT_USER,
    )
    project = _locked_project("Dark One", TARGET_PROMOTED)
    resolution = resolve_precedence([project, user])
    assert resolution.dominant_of(project.concept_id) == user.concept_id
    assert resolution.is_superseded(project.concept_id)
    assert not resolution.is_superseded(user.concept_id)


# ---------------------------------------------------------------------------------------------
# Resolution over a policy
# ---------------------------------------------------------------------------------------------


def test_a_locked_project_decision_supersedes_a_conflicting_promoted_article_variant() -> None:
    locked = _locked_project("the Dark One", TARGET_LOCKED)
    promoted = _promoted("Dark One", TARGET_PROMOTED)
    resolution = resolve_precedence([locked, promoted])

    assert resolution.is_superseded(promoted.concept_id)
    assert not resolution.is_superseded(locked.concept_id)
    assert resolution.dominant_of(promoted.concept_id) == locked.concept_id
    # The dominant concept absorbs the superseded spelling so the span is still matched.
    assert "Dark One" in resolution.source_forms_of(locked.concept_id)
    assert "the Dark One" in resolution.source_forms_of(locked.concept_id)


def test_both_reported_conflicts_resolve_to_the_locked_project_target() -> None:
    terms = [
        _locked_project("the Dark One", TARGET_LOCKED),
        _promoted("Dark One", TARGET_PROMOTED),
        _locked_project("The One Power", TARGET_POWER_LOCKED),
        _promoted("One Power", TARGET_POWER_PROMOTED),
    ]
    resolution = resolve_precedence(terms)
    assert resolution.active_target("the Dark One") == TARGET_LOCKED
    assert resolution.active_target("Dark One") == TARGET_LOCKED
    assert resolution.active_target("The One Power") == TARGET_POWER_LOCKED
    assert resolution.active_target("One Power") == TARGET_POWER_LOCKED


def test_agreeing_article_variants_are_not_superseded() -> None:
    """Two spellings that already name the same target are not a conflict to resolve."""
    locked = _locked_project("the Dark One", TARGET_LOCKED)
    agreeing = _promoted("Dark One", TARGET_LOCKED)
    resolution = resolve_precedence([locked, agreeing])
    assert not resolution.is_superseded(agreeing.concept_id)
    assert resolution.conflicts == ()


def test_unrelated_nested_terms_are_never_superseded() -> None:
    dragon = _promoted("Dragon", "اژدها")
    false_dragon = _promoted("false Dragon", "اژدهای دروغین")
    two_rivers = _promoted("Two Rivers", "دورود")
    folk = _locked_project("Two Rivers folk", "مردم دو رود")
    resolution = resolve_precedence([dragon, false_dragon, two_rivers, folk])
    assert resolution.conflicts == ()
    for term in (dragon, false_dragon, two_rivers, folk):
        assert not resolution.is_superseded(term.concept_id)


def test_an_undecided_concept_is_never_superseded_and_never_supersedes() -> None:
    locked = _locked_project("the Dark One", TARGET_LOCKED)
    observed = _term(
        "Dark One",
        status=SeriesTermStatus.OBSERVED_VARIANTS,
        target=TARGET_PROMOTED,
        kind=SeriesProvenanceKind.UNRESOLVED,
    )
    resolution = resolve_precedence([locked, observed])
    assert not resolution.is_superseded(observed.concept_id)
    assert resolution.conflicts == ()


def test_resolution_is_deterministic_regardless_of_input_order() -> None:
    locked = _locked_project("the Dark One", TARGET_LOCKED)
    promoted = _promoted("Dark One", TARGET_PROMOTED)
    forward = resolve_precedence([locked, promoted])
    backward = resolve_precedence([promoted, locked])
    assert forward.conflicts == backward.conflicts
    assert forward.superseded == backward.superseded


def test_conflicts_name_both_sides_and_the_reason() -> None:
    locked = _locked_project("the Dark One", TARGET_LOCKED)
    promoted = _promoted("Dark One", TARGET_PROMOTED)
    (conflict,) = resolve_precedence([locked, promoted]).conflicts
    assert conflict.dominant_source_term == "the Dark One"
    assert conflict.superseded_source_term == "Dark One"
    assert conflict.dominant_target == TARGET_LOCKED
    assert conflict.superseded_target == TARGET_PROMOTED


# ---------------------------------------------------------------------------------------------
# Import: a promoted article variant of a locked term must never become a rival decision
# ---------------------------------------------------------------------------------------------


def _corpus_concept(label: str, target: str, corpus_id: str, count: int) -> CorpusConcept:
    return CorpusConcept(
        corpus_id,
        f"TC-{corpus_id}-{label}",
        "terminology",
        label,
        "story_term",
        (label,),
        (
            CorpusForm(
                corpus_id=corpus_id,
                source=ReferenceEvidenceSource.NARRATIVE,
                quality=ReferenceEvidenceQuality.CLEAN,
                target_form=target,
                observation_count=count,
                reference_id=f"TTF-{corpus_id}-{label}",
                observed_surfaces=((target, count),),
            ),
        ),
    )


def _build(explicit_source: str, promoted_source: str) -> SeriesTerminologyPolicy:
    from book_translator.series_terminology.bootstrap import ExplicitDecision, build_policy

    policy, _ = build_policy(
        series_id="synthetic-series",
        source_language="en",
        target_language="fa",
        corpus_concepts=[
            _corpus_concept(promoted_source, TARGET_PROMOTED, "book1", 188),
            _corpus_concept(promoted_source, TARGET_PROMOTED, "book2", 67),
        ],
        explicit=[
            ExplicitDecision(
                explicit_source,
                TARGET_LOCKED,
                TerminologyType.STORY_TERM,
                SeriesProvenanceKind.EXPLICIT_PROJECT,
                "config/glossary.yaml",
                "mandatory project glossary entry",
            )
        ],
    )
    return policy


def _find(policy: SeriesTerminologyPolicy, source: str) -> SeriesTerm:
    return next(term for term in policy.terms if term.source_term == source)


def test_import_refuses_to_promote_an_article_variant_of_a_locked_term() -> None:
    """The exact Book 6 shape: glossary locks "the Dark One", the corpus attests "Dark One"."""
    policy = _build("the Dark One", "Dark One")
    locked = _find(policy, "the Dark One")
    assert locked.status is SeriesTermStatus.LOCKED
    assert locked.target_term == TARGET_LOCKED

    promoted = _find(policy, "Dark One")
    assert promoted.status is SeriesTermStatus.OBSERVED_VARIANTS
    assert promoted.target_term is None
    # The corpus evidence is kept exactly; only the decision layer is withheld.
    assert [variant.target_form for variant in promoted.variants] == [TARGET_PROMOTED]
    assert (
        sum(item.observation_count for variant in promoted.variants for item in variant.support)
        == 255
    )
    assert any("the Dark One" in note for note in promoted.notes)


def test_import_still_promotes_a_variant_that_agrees_with_the_locked_target() -> None:
    from book_translator.series_terminology.bootstrap import ExplicitDecision, build_policy

    policy, _ = build_policy(
        series_id="synthetic-series",
        source_language="en",
        target_language="fa",
        corpus_concepts=[
            _corpus_concept("Dark One", TARGET_LOCKED, "book1", 188),
            _corpus_concept("Dark One", TARGET_LOCKED, "book2", 67),
        ],
        explicit=[
            ExplicitDecision(
                "the Dark One",
                TARGET_LOCKED,
                TerminologyType.STORY_TERM,
                SeriesProvenanceKind.EXPLICIT_PROJECT,
                "config/glossary.yaml",
                "agrees",
            )
        ],
    )
    assert _find(policy, "Dark One").status is SeriesTermStatus.STRONG_REFERENCE


def test_import_still_promotes_a_nested_but_distinct_concept() -> None:
    """ "Two Rivers" is not an article variant of "Two Rivers folk" and keeps its own decision."""
    from book_translator.series_terminology.bootstrap import ExplicitDecision, build_policy

    policy, _ = build_policy(
        series_id="synthetic-series",
        source_language="en",
        target_language="fa",
        corpus_concepts=[
            _corpus_concept("Two Rivers", "دورود", "book1", 90),
            _corpus_concept("Two Rivers", "دورود", "book2", 40),
        ],
        explicit=[
            ExplicitDecision(
                "Two Rivers folk",
                "مردم دو رود",
                TerminologyType.PEOPLE,
                SeriesProvenanceKind.EXPLICIT_PROJECT,
                "config/glossary.yaml",
                "distinct concept",
            )
        ],
    )
    assert _find(policy, "Two Rivers").status is SeriesTermStatus.STRONG_REFERENCE
    assert _find(policy, "Two Rivers").target_term == "دورود"


# ---------------------------------------------------------------------------------------------
# Derivation: one concept yields one active decision, and it is the governing one
# ---------------------------------------------------------------------------------------------


def _policy_with_conflict() -> SeriesTerminologyPolicy:
    """A policy carrying the exact Book 6 shape, built directly so the import guard is bypassed."""
    return SeriesTerminologyPolicy(
        schema_version=SCHEMA_VERSION,
        version="1.0.0",
        series_id="synthetic-series",
        source_language="en",
        target_language="fa",
        promotion_rule_version="cross-source-agreement/1.0.0",
        terms=[
            _locked_project("the Dark One", TARGET_LOCKED),
            _promoted("Dark One", TARGET_PROMOTED),
            _locked_project("The One Power", TARGET_POWER_LOCKED),
            _promoted("One Power", TARGET_POWER_PROMOTED),
            _promoted("Two Rivers", "دورود"),
        ],
    )


SOURCE_WITH_BOTH_FORMS = (
    "He served the Dark One. The Dark One's shadow lay on the land, and men whispered that the "
    "Dark One stirred. A channeler reached for the One Power; The One Power answered. Two Rivers "
    "folk knew better than to name Dark One aloud, or to seize One Power untrained."
)


def _derive(
    policy: SeriesTerminologyPolicy, text: str = SOURCE_WITH_BOTH_FORMS
) -> TerminologyRegistry:
    from book_translator.series_terminology.derivation import ChunkText, derive_book_registry

    registry, _ = derive_book_registry(
        policy,
        book_id="synthetic-book",
        chunks=[ChunkText(chunk_id="001-001", chapter_id="001", source_text=text)],
    )
    return registry


def _decision_for(registry: TerminologyRegistry, source_term: str) -> TerminologyDecision | None:
    candidates = {item.candidate_id: item for item in registry.candidates}
    for decision in registry.decisions:
        if candidates[decision.candidate_id].source_term == source_term:
            return decision
    return None


def test_derivation_emits_no_decision_for_a_superseded_concept() -> None:
    registry = _derive(_policy_with_conflict())
    assert _decision_for(registry, "Dark One") is None
    assert _decision_for(registry, "One Power") is None


def test_derivation_keeps_the_governing_decision_and_absorbs_the_weaker_spelling() -> None:
    registry = _derive(_policy_with_conflict())
    dark = _decision_for(registry, "the Dark One")
    assert dark is not None
    assert dark.target_term == TARGET_LOCKED

    candidates = {item.source_term: item for item in registry.candidates}
    assert "Dark One" in candidates["the Dark One"].aliases
    assert "One Power" in candidates["The One Power"].aliases


def test_derivation_preserves_the_superseded_corpus_evidence_as_notes() -> None:
    registry = _derive(_policy_with_conflict())
    candidates = {item.source_term: item for item in registry.candidates}
    notes = " ".join(candidates["the Dark One"].evidence_notes)
    assert TARGET_PROMOTED in notes, "the displaced corpus form must survive as evidence"

    # ...and as an enforceable rival of the locked target, which is what the evidence always was.
    dark = _decision_for(registry, "the Dark One")
    assert dark is not None
    assert TARGET_PROMOTED in dark.competing_variants


def test_derivation_leaves_an_unrelated_nested_concept_alone() -> None:
    registry = _derive(_policy_with_conflict())
    two_rivers = _decision_for(registry, "Two Rivers")
    assert two_rivers is not None
    assert two_rivers.target_term == "دورود"


def test_the_weaker_rendering_is_never_an_authorized_target_form() -> None:
    """اهریمن is a historical variant, not a grammatical realization of ارباب تاریکی."""
    registry = _derive(_policy_with_conflict())
    dark = _decision_for(registry, "the Dark One")
    assert dark is not None
    assert TARGET_PROMOTED not in dark.allowed_target_forms


# ---------------------------------------------------------------------------------------------
# Selection: every consumer sees one instruction per concept
# ---------------------------------------------------------------------------------------------


def test_a_bare_occurrence_of_the_absorbed_spelling_selects_the_governing_decision() -> None:
    from book_translator.terminology.registry import canonically_matched_decisions

    text = "Men feared Dark One and reached for One Power."
    registry = _derive(_policy_with_conflict(), text)
    selected = sorted(
        decision.target_term for _, decision in canonically_matched_decisions(registry, text)
    )
    # The bare spellings are all this text contains, and each resolves to the locked target.
    assert selected == sorted([TARGET_LOCKED, TARGET_POWER_LOCKED])


def test_prompt_selection_never_renders_two_targets_for_one_concept() -> None:
    from book_translator.terminology.registry import relevant_decisions

    registry = _derive(_policy_with_conflict())
    rendered = [
        (candidate.source_term, decision.target_term)
        for candidate, decision in relevant_decisions(registry, SOURCE_WITH_BOTH_FORMS)
    ]
    targets = [target for _, target in rendered]
    assert TARGET_PROMOTED not in targets
    assert TARGET_POWER_PROMOTED not in targets
    assert TARGET_LOCKED in targets
    assert TARGET_POWER_LOCKED in targets
    # One instruction per concept, not two.
    assert len(targets) == len(set(targets))


# ---------------------------------------------------------------------------------------------
# Phase 3: the governed amendment retires a displaced decision without losing its evidence
# ---------------------------------------------------------------------------------------------


def _amendment_request() -> SeriesPolicyAmendmentRequest:
    return SeriesPolicyAmendmentRequest(
        schema_version=SCHEMA_VERSION,
        series_id="synthetic-series",
        resulting_policy_version="1.1.0",
        rationale="consolidate article variants onto the locked project concept",
        terms=[
            SeriesTermAmendment(
                source_term="the Dark One",
                target_term=TARGET_LOCKED,
                aliases=["Dark One"],
                rationale="the article variant names the same concept",
            )
        ],
        retired_terms=[
            SeriesTermRetirement(
                source_term="Dark One",
                rationale="displaced by the locked project decision on 'the Dark One'",
            )
        ],
    )


def _apply(policy: SeriesTerminologyPolicy, request: SeriesPolicyAmendmentRequest) -> AmendedPolicy:
    return apply_amendment(policy, request, amendment_id="spa-" + "0" * 24)


def test_an_amendment_retires_a_displaced_decision_to_evidence() -> None:
    amended = _apply(_policy_with_conflict(), _amendment_request())
    retired = _find(amended.policy, "Dark One")
    assert retired.status is SeriesTermStatus.OBSERVED_VARIANTS
    assert retired.target_term is None
    assert retired.allowed_target_forms == []


def test_retirement_keeps_every_observed_variant() -> None:
    before = _find(_policy_with_conflict(), "Dark One")
    amended = _apply(_policy_with_conflict(), _amendment_request())
    after = _find(amended.policy, "Dark One")
    assert [v.target_form for v in after.variants] == [v.target_form for v in before.variants]
    assert after.variants == before.variants, "corpus evidence is preserved byte for byte"


def test_retirement_records_why_and_who() -> None:
    amended = _apply(_policy_with_conflict(), _amendment_request())
    retired = _find(amended.policy, "Dark One")
    assert "spa-" in retired.provenance.origin
    assert "the Dark One" in retired.provenance.rationale
    assert any("STRONG_REFERENCE" in note for note in retired.notes)


def test_the_governing_concept_gains_the_alias() -> None:
    amended = _apply(_policy_with_conflict(), _amendment_request())
    assert "Dark One" in _find(amended.policy, "the Dark One").aliases


def test_retiring_an_unknown_term_is_refused_whole() -> None:
    from book_translator.series_terminology.amendment import SeriesPolicyAmendmentConflict

    request = SeriesPolicyAmendmentRequest(
        schema_version=SCHEMA_VERSION,
        series_id="synthetic-series",
        resulting_policy_version="1.1.0",
        rationale="x",
        terms=[
            SeriesTermAmendment(
                source_term="the Dark One", target_term=TARGET_LOCKED, rationale="x"
            )
        ],
        retired_terms=[SeriesTermRetirement(source_term="Nonexistent", rationale="x")],
    )
    with pytest.raises(SeriesPolicyAmendmentConflict):
        _apply(_policy_with_conflict(), request)


def test_retiring_a_user_owned_decision_is_refused_whole() -> None:
    from book_translator.series_terminology.amendment import SeriesPolicyAmendmentConflict

    policy = SeriesTerminologyPolicy(
        schema_version=SCHEMA_VERSION,
        version="1.0.0",
        series_id="synthetic-series",
        source_language="en",
        target_language="fa",
        promotion_rule_version="cross-source-agreement/1.0.0",
        terms=[
            _locked_project("the Dark One", TARGET_LOCKED),
            _term(
                "Dark One",
                status=SeriesTermStatus.STRONG_REFERENCE,
                target=TARGET_PROMOTED,
                kind=SeriesProvenanceKind.EXPLICIT_USER,
            ),
        ],
    )
    with pytest.raises(SeriesPolicyAmendmentConflict, match="user decision"):
        _apply(policy, _amendment_request())


def test_retirement_and_amendment_of_the_same_term_is_refused() -> None:
    from book_translator.series_terminology.amendment import SeriesPolicyAmendmentConflict

    request = SeriesPolicyAmendmentRequest(
        schema_version=SCHEMA_VERSION,
        series_id="synthetic-series",
        resulting_policy_version="1.1.0",
        rationale="x",
        terms=[
            SeriesTermAmendment(
                source_term="the Dark One", target_term=TARGET_LOCKED, rationale="x"
            )
        ],
        retired_terms=[SeriesTermRetirement(source_term="the Dark One", rationale="x")],
    )
    with pytest.raises(SeriesPolicyAmendmentConflict, match="both amended and retired"):
        _apply(_policy_with_conflict(), request)


def test_an_amendment_without_retirements_keeps_its_recorded_bytes() -> None:
    """The new field is absent from a request that does not use it, so old identities hold."""
    request = SeriesPolicyAmendmentRequest(
        schema_version=SCHEMA_VERSION,
        series_id="synthetic-series",
        resulting_policy_version="1.1.0",
        rationale="x",
        terms=[
            SeriesTermAmendment(
                source_term="the Dark One", target_term=TARGET_LOCKED, rationale="x"
            )
        ],
    )
    assert "retired_terms" not in request.model_dump(mode="json")


# ---------------------------------------------------------------------------------------------
# Phase 4: the conflict class is detected automatically
# ---------------------------------------------------------------------------------------------


def test_policy_validation_reports_both_reported_conflicts() -> None:
    conflicts = policy_concept_conflicts(_policy_with_conflict())
    assert {item.superseded_source_term for item in conflicts} == {"Dark One", "One Power"}


def test_policy_validation_is_silent_once_the_amendment_is_applied() -> None:
    amended = _apply(_policy_with_conflict(), _amendment_request())
    remaining = {item.superseded_source_term for item in policy_concept_conflicts(amended.policy)}
    assert "Dark One" not in remaining


def test_policy_validation_never_reports_a_nested_but_distinct_concept() -> None:
    conflicts = policy_concept_conflicts(_policy_with_conflict())
    reported = {item.superseded_source_term for item in conflicts} | {
        item.dominant_source_term for item in conflicts
    }
    assert "Two Rivers" not in reported


# ---------------------------------------------------------------------------------------------
# Phase 5: one shared selector, so a prompt never asks for what enforcement will not accept
# ---------------------------------------------------------------------------------------------


def _registry_with_nested_alias() -> TerminologyRegistry:
    """A long term whose alias is also a shorter, separately decided concept's spelling."""
    from book_translator.terminology.registry import candidate_identity, decision_identity

    def pair(
        source: str, target: str, aliases: list[str]
    ) -> tuple[TerminologyCandidate, TerminologyDecision]:
        cid = candidate_identity(source, "en")
        return (
            TerminologyCandidate(
                candidate_id=cid,
                source_term=source,
                classification=TerminologyType.STORY_TERM,
                confidence=1.0,
                status=CandidateStatus.APPROVED,
                provenance=DecisionSource.IMPORTED,
                aliases=aliases,
            ),
            TerminologyDecision(
                decision_id=decision_identity(cid, "fa"),
                candidate_id=cid,
                target_term=target,
                decision_source=DecisionSource.IMPORTED,
                decision_state=DecisionState.CONFIRMED,
                locked=False,
                required=True,
                enforcement=TerminologyEnforcement.EXACT,
            ),
        )

    long_c, long_d = pair("Amyrlin Seat", "مقام امیرلین", [])
    short_c, short_d = pair("Amyrlin", "امیرلین", [])
    return TerminologyRegistry(
        schema_version=SCHEMA_VERSION,
        version="1.0.0",
        book_id="synthetic-book",
        source_language="en",
        target_language="fa",
        candidates=[long_c, short_c],
        decisions=[long_d, short_d],
    )


def test_the_shared_selector_and_the_validator_agree_on_an_overlap() -> None:
    """A prompt must never be told to honour a term the validator will not look for."""
    from book_translator.terminology.registry import (
        applicable_decisions,
        canonically_matched_decisions,
    )

    registry = _registry_with_nested_alias()
    text = "She addressed the Amyrlin Seat."
    prompted = {c.source_term for c, _ in applicable_decisions(registry, text)}
    enforced = {c.source_term for c, _ in canonically_matched_decisions(registry, text)}
    assert prompted == enforced == {"Amyrlin Seat"}


def test_the_shared_selector_still_selects_a_standalone_shorter_term() -> None:
    from book_translator.terminology.registry import applicable_decisions

    registry = _registry_with_nested_alias()
    text = "She addressed the Amyrlin Seat, then the Amyrlin alone."
    prompted = {c.source_term for c, _ in applicable_decisions(registry, text)}
    assert prompted == {"Amyrlin Seat", "Amyrlin"}


def test_the_shared_selector_reads_the_context_window_too() -> None:
    from book_translator.terminology.registry import applicable_decisions

    registry = _registry_with_nested_alias()
    prompted = {c.source_term for c, _ in applicable_decisions(registry, "", "The Amyrlin waited.")}
    assert prompted == {"Amyrlin"}


def test_the_shared_selector_never_renders_a_conflicting_pair() -> None:
    from book_translator.terminology.registry import applicable_decisions

    registry = _derive(_policy_with_conflict())
    targets = [d.target_term for _, d in applicable_decisions(registry, SOURCE_WITH_BOTH_FORMS)]
    assert TARGET_PROMOTED not in targets
    assert TARGET_POWER_PROMOTED not in targets
    assert len(targets) == len(set(targets))


def test_every_prompt_path_uses_the_shared_selector() -> None:
    """The repair path must not keep its own weaker precedence rules."""
    import inspect

    from book_translator.literary_editing import engine as literary_engine
    from book_translator.prompt_packaging import rendering as prompt_rendering
    from book_translator.semantic_qa import engine as semantic_engine

    for module in (prompt_rendering, semantic_engine, literary_engine):
        source = inspect.getsource(module)
        assert "relevant_decisions(" not in source, (
            f"{module.__name__} still uses the overlap-blind selector"
        )
        assert "applicable_decisions(" in source


def test_the_repair_derivation_stays_frozen_at_its_recorded_version() -> None:
    """Bounded provider recovery re-derives a failed repair and proves it byte-identical.

    The repair path is fixed by making the *registry* state one decision per concept, never by
    changing this derivation: a change here would make every historical repair irreproducible and
    therefore unrecoverable. This test exists so that constraint is not discovered by breaking it.
    """
    from book_translator.translation_repair import (
        MARKED_REPAIR_PROMPT_VERSION,
        REPAIR_PROMPT_VERSION,
    )

    assert REPAIR_PROMPT_VERSION == "1.0.0"
    assert MARKED_REPAIR_PROMPT_VERSION == "2.0.0"


def test_a_consolidated_registry_offers_repair_only_the_governing_instruction() -> None:
    """What repair can select is bounded by what the registry decides, and that is now one thing."""
    registry = _derive(_policy_with_conflict())
    selectable = {
        candidate.source_term: decision.target_term
        for candidate in registry.candidates
        for decision in registry.decisions
        if decision.candidate_id == candidate.candidate_id and decision.required
    }
    assert TARGET_PROMOTED not in selectable.values()
    assert TARGET_POWER_PROMOTED not in selectable.values()
    assert selectable["the Dark One"] == TARGET_LOCKED
    assert selectable["The One Power"] == TARGET_POWER_LOCKED


def test_after_the_amendment_the_bare_spelling_is_still_enforced_to_the_locked_target() -> None:
    """The end-to-end property the whole change exists for.

    Retirement alone would leave "Dark One" naming an evidence-only concept, and a byte-exact
    matcher would stop enforcing the occurrences it matched. The alias on the governing concept is
    what keeps them enforced, and it must win over the retired concept's own spelling.
    """
    from book_translator.terminology.registry import canonically_matched_decisions

    amended = _apply(_policy_with_conflict(), _amendment_request())
    text = "Men feared Dark One, and feared the Dark One more."
    registry = _derive(amended.policy, text)
    selected = {d.target_term for _, d in canonically_matched_decisions(registry, text)}
    assert selected == {TARGET_LOCKED}
    assert TARGET_PROMOTED not in selected


def test_after_the_amendment_one_decision_governs_the_concept() -> None:
    """The retired concept contributes no rival decision, whichever spelling names the candidate.

    A candidate is named by the spelling the book actually uses, so in a text that only contains
    the bare form the surviving candidate is called "Dark One". What matters is that there is one
    decision for the concept and it carries the locked target.
    """
    amended = _apply(_policy_with_conflict(), _amendment_request())
    registry = _derive(amended.policy, "Men feared Dark One.")
    governing = [
        d
        for c in registry.candidates
        for d in registry.decisions
        if d.candidate_id == c.candidate_id and c.source_term in {"Dark One", "the Dark One"}
    ]
    assert len(governing) == 1
    assert governing[0].target_term == TARGET_LOCKED


def test_a_decision_takes_a_spelling_from_evidence_only_as_an_article_variant() -> None:
    """The alias transfer is for consolidation, never for an arbitrary alias.

    "Amyrlin" is its own concept that "Amyrlin Seat" merely lists as an alias, and the real policy
    holds it as evidence-only. A decision must not absorb it and start enforcing the office's
    rendering on every bare mention of the title. Only an article or case variant of the decision's
    own spelling may be taken.
    """
    policy = SeriesTerminologyPolicy(
        schema_version=SCHEMA_VERSION,
        version="1.0.0",
        series_id="synthetic-series",
        source_language="en",
        target_language="fa",
        promotion_rule_version="cross-source-agreement/1.0.0",
        terms=[
            _locked_project("Amyrlin Seat", "مقام امیرلین", aliases=("Amyrlin",)),
            _term(
                "Amyrlin",
                status=SeriesTermStatus.OBSERVED_VARIANTS,
                target="امیرلین",
                kind=SeriesProvenanceKind.UNRESOLVED,
            ),
            _locked_project("the Dark One", TARGET_LOCKED, aliases=("Dark One",)),
            _term(
                "Dark One",
                status=SeriesTermStatus.OBSERVED_VARIANTS,
                target=TARGET_PROMOTED,
                kind=SeriesProvenanceKind.UNRESOLVED,
            ),
        ],
    )
    text = "The Amyrlin Seat spoke, the Amyrlin waited, and the Dark One stirred; Dark One."
    registry = _derive(policy, text)
    by_term = {c.source_term: c for c in registry.candidates}

    # The separate concept keeps its own spelling and stays undecided.
    assert "Amyrlin" in by_term
    assert "Amyrlin" not in by_term["Amyrlin Seat"].aliases

    # The article variant is still consolidated onto the decision that governs it.
    assert "Dark One" not in by_term
    assert "Dark One" in by_term["the Dark One"].aliases


# ---------------------------------------------------------------------------------------------
# An article/case variant alias enforces; an arbitrary alias still does not
# ---------------------------------------------------------------------------------------------


def test_an_article_variant_alias_is_an_occurrence_of_the_concept() -> None:
    """The consolidated spelling must enforce, or consolidation only half-works.

    The book writes "the One Power" where the policy concept is "The One Power". Matching reaches
    it through the alias, but enforcement dropped every alias match, so the occurrence was governed
    by nothing at all and any rendering passed QA.
    """
    from book_translator.series_terminology.derivation import ChunkText, derive_book_registry
    from book_translator.terminology.registry import canonically_matched_decisions

    amended = _apply(_policy_with_conflict(), _amendment_request())
    # The book writes the capitalized form somewhere, so that spelling names the candidate and
    # the lowercase and bare forms are only aliases -- the real Book 6 shape.
    registry, _ = derive_book_registry(
        amended.policy,
        book_id="synthetic-book",
        chunks=[
            ChunkText(chunk_id="001-001", chapter_id="001", source_text="The One Power is one."),
            ChunkText(
                chunk_id="001-002", chapter_id="001", source_text="She seized the One Power."
            ),
        ],
    )
    named = {c.candidate_id: c for c in registry.candidates}
    power = next(c for c in registry.candidates if "One Power" in c.source_term)
    assert power.source_term == "The One Power", "the capitalized spelling names the candidate"
    assert "One Power" in power.aliases

    chunk = "She seized the One Power."
    selected = {d.target_term for _, d in canonically_matched_decisions(registry, chunk)}
    assert TARGET_POWER_LOCKED in selected, (
        "an article/case variant of the canonical is an occurrence of the concept"
    )
    assert named


def test_an_arbitrary_alias_still_does_not_enforce_its_concepts_decision() -> None:
    """ "Amyrlin" is not an article variant of "Amyrlin Seat" and must not carry its rendering."""
    from book_translator.terminology.registry import canonically_matched_decisions

    policy = SeriesTerminologyPolicy(
        schema_version=SCHEMA_VERSION,
        version="1.0.0",
        series_id="synthetic-series",
        source_language="en",
        target_language="fa",
        promotion_rule_version="cross-source-agreement/1.0.0",
        terms=[_locked_project("Amyrlin Seat", "مقام امیرلین", aliases=("Amyrlin",))],
    )
    from book_translator.series_terminology.derivation import ChunkText, derive_book_registry

    registry, _ = derive_book_registry(
        policy,
        book_id="synthetic-book",
        chunks=[
            ChunkText(chunk_id="001-001", chapter_id="001", source_text="The Amyrlin Seat rose."),
            ChunkText(chunk_id="001-002", chapter_id="001", source_text="The Amyrlin waited."),
        ],
    )
    seat = next(c for c in registry.candidates if c.source_term == "Amyrlin Seat")
    assert "Amyrlin" in seat.aliases
    selected = {
        d.target_term for _, d in canonically_matched_decisions(registry, "The Amyrlin waited.")
    }
    assert selected == set(), "a bare title must not carry the office's rendering"
