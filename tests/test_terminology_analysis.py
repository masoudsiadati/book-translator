import pytest

from book_translator.models import (
    DecisionSource,
    TerminologyAcceptancePolicy,
    TerminologyCandidate,
    TerminologyDiscoveryEvidence,
    TerminologyDiscoveryResponse,
    TerminologyOccurrence,
    TerminologyProposal,
    TerminologyProposalResponse,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.terminology import (
    FakeTerminologyProvider,
    TerminologyAnalysisError,
    TerminologyProviderError,
    apply_proposals,
    canonical_registry_bytes,
    parse_discovery_response,
    proposal_identity,
    reconcile_candidates,
    registry_identity,
    set_decision,
    validate_proposals,
)
from book_translator.terminology.analysis import evidence_for_selection, source_evidence_count
from book_translator.terminology.canonical import (
    canonical_view,
    canonicalization_confidence,
    exact_canonical,
    proposal_conflicts,
)
from book_translator.terminology.providers import (
    TerminologyDiscoveryRequest,
    TerminologyProposalRequest,
)
from book_translator.terminology.target_style import (
    PERSIAN_TARGET_STYLE_VERSION,
    canonical_targets_equal,
    target_style_adapter,
)


def registry() -> TerminologyRegistry:
    return TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="generic-synthetic-book",
        source_language="src-lang",
        target_language="dst-lang",
    )


def evidence(
    *,
    classification: TerminologyType = TerminologyType.PERSON,
    confidence: float = 0.8,
    alias: str | None = None,
    chapter: str = "001",
    context_sensitive: bool = False,
) -> TerminologyDiscoveryEvidence:
    return TerminologyDiscoveryEvidence(
        source_term="Veyra",
        aliases=[alias] if alias else [],
        classification=classification,
        confidence=confidence,
        context_sensitive=context_sensitive,
        occurrences=[TerminologyOccurrence(chapter_id=chapter, chunk_id=f"{chapter}-001")],
        reason="synthetic evidence",
    )


def candidate() -> TerminologyCandidate:
    return reconcile_candidates((evidence(),), "src-lang")[0]


def proposal(confidence: float = 0.9, target: str = "Target-Veyra") -> TerminologyProposal:
    item = candidate()
    return TerminologyProposal(
        proposal_id=proposal_identity(item.candidate_id, target, "dst-lang"),
        candidate_id=item.candidate_id,
        source_term=item.source_term,
        proposed_target=target,
        confidence=confidence,
        classification=item.classification,
    )


def discovery_request() -> TerminologyDiscoveryRequest:
    return TerminologyDiscoveryRequest("r", "system", "user", "src", "dst", "001", "001-001")


def proposal_request() -> TerminologyProposalRequest:
    item = candidate()
    return TerminologyProposalRequest("r", "system", "user", "src", "dst", (item,), registry())


def test_structured_candidate_parsing_and_malformed_response() -> None:
    response = TerminologyDiscoveryResponse(schema_version="1.0", candidates=[evidence()])
    assert parse_discovery_response(response).candidates[0].source_term == "Veyra"
    with pytest.raises(TerminologyAnalysisError, match="malformed structured discovery"):
        parse_discovery_response({"schema_version": "1.0", "candidates": "invalid"})


def test_proposal_validation_requires_every_requested_candidate() -> None:
    with pytest.raises(TerminologyAnalysisError, match="omitted required candidate proposal"):
        validate_proposals(
            TerminologyProposalResponse(schema_version="1.0", proposals=[]),
            (candidate(),),
            "dst-lang",
        )


def test_source_evidence_uses_only_conservative_typographic_normalization() -> None:
    source = "The ji’e‐toh rule names Veyra."
    assert source_evidence_count("ji'e-toh", source) == 1
    assert source_evidence_count("Veyra", source) == 1
    assert source_evidence_count("veyra", source) == 0
    assert source_evidence_count("Veyras", source) == 0


def test_selection_requires_primary_and_each_alias_to_have_independent_evidence() -> None:
    observed = evidence(alias="Lady Veyra")
    response = TerminologyDiscoveryResponse(schema_version="1.0", candidates=[observed])
    validated = evidence_for_selection(response, (("001", "001-001", "Veyra met Lady Veyra."),))
    assert validated[0].occurrences[0].count == 2

    absent_alias = evidence(alias="V. Veyra")
    with pytest.raises(TerminologyAnalysisError, match="provider alias is absent"):
        evidence_for_selection(
            TerminologyDiscoveryResponse(schema_version="1.0", candidates=[absent_alias]),
            (("001", "001-001", "Veyra met Lady Veyra."),),
        )


def test_selection_rejects_genuinely_absent_candidate() -> None:
    with pytest.raises(TerminologyAnalysisError, match="provider candidate is absent"):
        evidence_for_selection(
            TerminologyDiscoveryResponse(schema_version="1.0", candidates=[evidence()]),
            (("001", "001-001", "Unrelated synthetic source."),),
        )


def empty_glossary() -> dict[str, object]:
    return {
        "terms": {},
        "characters": {},
        "places": {},
        "groups": {},
        "unresolved_terms": [],
    }


def test_unified_view_prefers_locked_registry_and_detects_canonical_subterm() -> None:
    current = set_decision(
        registry(),
        source_term="Orin",
        target_term="Yorin",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    glossary = empty_glossary()
    glossary["characters"] = {"Orin": "Legacy-Orin"}
    view = canonical_view(current, glossary)
    phrase = reconcile_candidates(
        (
            TerminologyDiscoveryEvidence(
                source_term="Captain Orin",
                classification=TerminologyType.TITLE,
                confidence=0.95,
                reason="synthetic formal title",
            ),
        ),
        "src-lang",
    )[0]
    bad = TerminologyProposal(
        proposal_id="tp-" + "0" * 24,
        candidate_id=phrase.candidate_id,
        source_term=phrase.source_term,
        proposed_target="Captain Zorin",
        confidence=0.99,
        classification=phrase.classification,
    )
    good = bad.model_copy(update={"proposed_target": "Captain Yorin-ha"})
    assert proposal_conflicts(bad, phrase, view, "dst-lang") == ("canonical_subterm_conflict",)
    assert proposal_conflicts(good, phrase, view, "dst-lang") == ()


def test_unrelated_substring_does_not_create_subterm_constraint() -> None:
    current = set_decision(
        registry(),
        source_term="Orin",
        target_term="Yorin",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    phrase = reconcile_candidates(
        (
            TerminologyDiscoveryEvidence(
                source_term="Orinth",
                classification=TerminologyType.PLACE,
                confidence=0.9,
                reason="synthetic place",
            ),
        ),
        "src-lang",
    )[0]
    value = TerminologyProposal(
        proposal_id="tp-" + "0" * 24,
        candidate_id=phrase.candidate_id,
        source_term=phrase.source_term,
        proposed_target="Unrelated",
        confidence=0.9,
        classification=phrase.classification,
    )
    assert (
        proposal_conflicts(value, phrase, canonical_view(current, empty_glossary()), "dst-lang")
        == ()
    )


def test_context_sensitive_scope_confidence_is_separate_from_provider_confidence() -> None:
    risky = candidate().model_copy(
        update={
            "classification": TerminologyType.CONTEXT_SENSITIVE,
            "context_sensitive": True,
            "confidence": 0.99,
        }
    )
    assert canonicalization_confidence(risky) == 0.0
    updated, result = apply_proposals(
        registry(),
        (risky,),
        (proposal(0.99),),
        policy=TerminologyAcceptancePolicy.AUTOMATIC,
        threshold=0.9,
    )
    assert result.applied == 0
    assert not updated.decisions


def test_english_article_variant_is_isolated_and_narrow() -> None:
    english = registry().model_copy(update={"source_language": "en"})
    glossary = empty_glossary()
    glossary["terms"] = {"the Orin Order": "CanonicalOrder"}
    variant = reconcile_candidates(
        (
            TerminologyDiscoveryEvidence(
                source_term="Orin Order",
                classification=TerminologyType.ORGANIZATION,
                confidence=0.9,
                reason="synthetic organization",
            ),
        ),
        "en",
    )[0]
    assert exact_canonical(variant, canonical_view(english, glossary)) is not None
    non_english = english.model_copy(update={"source_language": "xx"})
    assert exact_canonical(variant, canonical_view(non_english, glossary)) is None


@pytest.mark.parametrize(
    "policy", [TerminologyAcceptancePolicy.HIGH_CONFIDENCE, TerminologyAcceptancePolicy.AUTOMATIC]
)
def test_canonical_conflict_is_never_applied(policy: TerminologyAcceptancePolicy) -> None:
    blocked = proposal(0.99).model_copy(
        update={"blocked": True, "conflict_codes": ["canonical_subterm_conflict"]}
    )
    updated, result = apply_proposals(
        registry(), (candidate(),), (blocked,), policy=policy, threshold=0.9
    )
    assert result.applied == 0
    assert result.conflicts == 1
    assert not updated.decisions


def test_legacy_canonical_conflict_blocks_application_under_every_policy() -> None:
    glossary = empty_glossary()
    glossary["characters"] = {"Veyra": "Existing-Veyra"}
    view = canonical_view(registry(), glossary)
    for policy in (
        TerminologyAcceptancePolicy.MANUAL,
        TerminologyAcceptancePolicy.HIGH_CONFIDENCE,
        TerminologyAcceptancePolicy.AUTOMATIC,
    ):
        updated, result = apply_proposals(
            registry(),
            (candidate(),),
            (proposal(0.99, "Conflicting-Veyra"),),
            policy=policy,
            threshold=0.9,
            canonical_terms=view,
        )
        assert result.applied == 0
        assert result.conflicts == 1
        assert not updated.decisions


def test_safe_proper_name_with_scope_evidence_is_high_confidence_eligible() -> None:
    safe = candidate().model_copy(update={"confidence": 0.95})
    updated, result = apply_proposals(
        registry(),
        (safe,),
        (proposal(0.95),),
        policy=TerminologyAcceptancePolicy.HIGH_CONFIDENCE,
        threshold=0.9,
    )
    assert result.applied == 1
    assert updated.decisions[0].target_term == "Target-Veyra"


def test_persian_zwnj_morphology_preserves_canonical_lexical_identity() -> None:
    current = set_decision(
        registry(),
        source_term="Orin",
        target_term="اورین",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    phrase = reconcile_candidates(
        (
            TerminologyDiscoveryEvidence(
                source_term="Captain Orin",
                classification=TerminologyType.TITLE,
                confidence=0.95,
                reason="synthetic title",
            ),
        ),
        "src-lang",
    )[0]
    value = TerminologyProposal(
        proposal_id="tp-" + "0" * 24,
        candidate_id=phrase.candidate_id,
        source_term=phrase.source_term,
        proposed_target="اورین‌ها",
        confidence=0.95,
        classification=phrase.classification,
    )
    assert proposal_conflicts(value, phrase, canonical_view(current, empty_glossary()), "fa") == ()


def test_persian_compound_head_plural_preserves_canonical_lexical_identity() -> None:
    current = set_decision(
        registry(),
        source_term="Order",
        target_term="نگهبان روشنایی",
        classification=TerminologyType.ORGANIZATION,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    phrase = reconcile_candidates(
        (
            TerminologyDiscoveryEvidence(
                source_term="Order War",
                classification=TerminologyType.HISTORICAL_EVENT,
                confidence=0.95,
                reason="synthetic historical event",
            ),
        ),
        "src-lang",
    )[0]
    value = TerminologyProposal(
        proposal_id="tp-" + "0" * 24,
        candidate_id=phrase.candidate_id,
        source_term=phrase.source_term,
        proposed_target="جنگ نگهبانان روشنایی",
        confidence=0.95,
        classification=phrase.classification,
    )
    assert proposal_conflicts(value, phrase, canonical_view(current, empty_glossary()), "fa") == ()


def test_duplicate_alias_occurrence_and_classification_reconciliation() -> None:
    reconciled = reconcile_candidates(
        (
            evidence(alias="Lady Veyra", chapter="001", confidence=0.8),
            evidence(
                alias="V. Veyra",
                chapter="040",
                confidence=0.95,
                classification=TerminologyType.TITLE,
                context_sensitive=True,
            ),
        ),
        "src-lang",
    )[0]
    assert reconciled.aliases == ["Lady Veyra", "V. Veyra"]
    assert [item.chapter_id for item in reconciled.occurrences] == ["001", "040"]
    assert reconciled.classification is TerminologyType.TITLE
    assert reconciled.classification_conflicts == [TerminologyType.PERSON]
    assert reconciled.context_sensitive
    assert reconcile_candidates((evidence(), evidence()), "src-lang") == reconcile_candidates(
        (evidence(), evidence()), "src-lang"
    )


def test_validated_ai_proposal_gets_deterministic_identity() -> None:
    item = candidate()
    raw = proposal().model_copy(update={"proposal_id": "tp-" + "0" * 24})
    result = validate_proposals(
        TerminologyProposalResponse(schema_version="1.0", proposals=[raw]),
        (item,),
        "dst-lang",
    )
    assert result[0].proposal_id == proposal_identity(item.candidate_id, "Target-Veyra", "dst-lang")


def test_ai_proposal_discards_canonical_target_components_from_known_variants() -> None:
    item = candidate()
    raw = proposal(target="Target Veyra").model_copy(
        update={"known_variants": ["Target", "Veyra", "Different Veyra"]}
    )
    result = validate_proposals(
        TerminologyProposalResponse(schema_version="1.0", proposals=[raw]),
        (item,),
        "dst-lang",
    )
    assert result[0].known_variants == ["Different Veyra"]


def test_persian_ai_proposal_is_style_normalized_with_raw_provider_evidence() -> None:
    item = candidate()
    raw = proposal(target="گَرِث").model_copy(update={"proposal_id": "tp-" + "0" * 24})
    result = validate_proposals(
        TerminologyProposalResponse(schema_version="1.0", proposals=[raw]),
        (item,),
        "fa",
    )[0]
    assert result.proposed_target == "گرث"
    assert result.provider_proposed_target == "گَرِث"
    assert result.proposal_id == proposal_identity(item.candidate_id, "گرث", "fa")
    assert target_style_adapter("fa-IR").version == PERSIAN_TARGET_STYLE_VERSION


def test_canonical_target_comparison_is_exact_except_configured_presentation_marks() -> None:
    assert canonical_targets_equal("مورگِیز", "مورگیز", "fa")
    assert not canonical_targets_equal("می\u200cرود", "میرود", "fa")
    assert not canonical_targets_equal("نام", "نامی", "fa")
    assert not canonical_targets_equal("Cats", "Cat", "en")
    assert not canonical_targets_equal("colour", "color", "en")


def test_target_style_adapter_never_mutates_locked_or_legacy_canonical_storage() -> None:
    locked = set_decision(
        registry().model_copy(update={"target_language": "fa"}),
        source_term="Veyra",
        target_term="آجای",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    glossary = empty_glossary()
    glossary["characters"] = {"Orin": "اُرین"}
    view = canonical_view(locked, glossary)
    assert locked.decisions[0].target_term == "آجای"
    assert {item.target_term for item in view} == {"آجای", "اُرین"}


@pytest.mark.parametrize(
    ("policy", "confidence", "threshold", "expected"),
    [
        (TerminologyAcceptancePolicy.MANUAL, 1.0, 0.9, 0),
        (TerminologyAcceptancePolicy.HIGH_CONFIDENCE, 0.89, 0.9, 0),
        (TerminologyAcceptancePolicy.HIGH_CONFIDENCE, 0.9, 0.9, 0),
        (TerminologyAcceptancePolicy.AUTOMATIC, 0.1, 0.9, 1),
    ],
)
def test_acceptance_policies(
    policy: TerminologyAcceptancePolicy, confidence: float, threshold: float, expected: int
) -> None:
    updated, result = apply_proposals(
        registry(),
        (candidate(),),
        (proposal(confidence),),
        policy=policy,
        threshold=threshold,
    )
    assert result.applied == expected
    assert len(updated.decisions) == expected
    if expected:
        assert updated.decisions[0].decision_source is DecisionSource.AI
        assert not updated.decisions[0].locked


@pytest.mark.parametrize("source", [DecisionSource.USER, DecisionSource.AI])
def test_existing_higher_precedence_decision_wins(source: DecisionSource) -> None:
    current = set_decision(
        registry(),
        source_term="Veyra",
        target_term="Existing",
        classification=TerminologyType.PERSON,
        decision_source=source,
        locked=source is DecisionSource.USER,
    )
    updated, result = apply_proposals(
        current,
        (candidate(),),
        (proposal(target="Conflicting"),),
        policy=TerminologyAcceptancePolicy.AUTOMATIC,
        threshold=0.0,
    )
    assert updated.decisions[0].target_term == "Existing"
    assert result.conflicts == 1


@pytest.mark.parametrize("source", [DecisionSource.IMPORTED, DecisionSource.INFERRED])
def test_ai_proposal_cannot_replace_any_canonical_decision(source: DecisionSource) -> None:
    current = set_decision(
        registry(),
        source_term="Veyra",
        target_term="Older",
        classification=TerminologyType.PERSON,
        decision_source=source,
        locked=False,
    )
    updated, result = apply_proposals(
        current,
        (candidate(),),
        (proposal(target="AI-Canonical"),),
        policy=TerminologyAcceptancePolicy.AUTOMATIC,
        threshold=0.0,
    )
    assert result.applied == 0
    assert result.conflicts == 1
    assert updated.decisions[0].target_term == "Older"


def test_incremental_candidate_changes_hash_but_reuses_existing_decision() -> None:
    current = set_decision(
        registry(),
        source_term="Veyra",
        target_term="Existing",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.AI,
        locked=False,
    )
    before = registry_identity(current)
    later = reconcile_candidates(
        (
            TerminologyDiscoveryEvidence(
                source_term="Orinth",
                aliases=[],
                classification=TerminologyType.PLACE,
                confidence=0.91,
                occurrences=[TerminologyOccurrence(chapter_id="040", chunk_id="040-001")],
                reason="synthetic later chapter",
            ),
        ),
        "src-lang",
    )[0]
    updated, _ = apply_proposals(
        current,
        (candidate(), later),
        (),
        policy=TerminologyAcceptancePolicy.MANUAL,
        threshold=0.9,
    )
    assert updated.decisions[0].target_term == "Existing"
    assert registry_identity(updated) != before
    assert canonical_registry_bytes(updated) == canonical_registry_bytes(updated)


@pytest.mark.parametrize(
    ("scenario", "retryable", "ambiguous"),
    [
        ("retryable_failure", True, False),
        ("non_retryable_failure", False, False),
        ("ambiguous_failure", False, True),
    ],
)
def test_fake_provider_failures(scenario: str, retryable: bool, ambiguous: bool) -> None:
    provider = FakeTerminologyProvider(scenario)
    with pytest.raises(TerminologyProviderError) as raised:
        provider.discover(discovery_request())
    assert raised.value.retryable is retryable
    assert raised.value.ambiguous is ambiguous


def test_fake_provider_malformed_duplicates_and_conflicting_proposals() -> None:
    response = TerminologyDiscoveryResponse(schema_version="1.0", candidates=[evidence()])
    duplicated = FakeTerminologyProvider(
        "duplicate_candidates", discovery_response=response
    ).discover(discovery_request())
    assert len(duplicated.candidates) == 2
    with pytest.raises(TerminologyAnalysisError):
        parse_discovery_response(FakeTerminologyProvider("malformed").discover(discovery_request()))

    proposals = TerminologyProposalResponse(schema_version="1.0", proposals=[proposal()])
    conflicting = FakeTerminologyProvider(
        "conflicting_proposals", proposal_response=proposals
    ).propose(proposal_request())
    with pytest.raises(TerminologyAnalysisError, match="conflicting proposals"):
        validate_proposals(conflicting, (candidate(),), "dst-lang")
