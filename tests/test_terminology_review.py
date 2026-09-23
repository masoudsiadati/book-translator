from pathlib import Path

import pytest

from book_translator.models import (
    DecisionSource,
    TerminologyAcceptancePolicy,
    TerminologyAnalysisStage,
    TerminologyCandidate,
    TerminologyProposal,
    TerminologyRegistry,
    TerminologyReview,
    TerminologyReviewDisposition,
    TerminologyReviewResponse,
    TerminologyType,
)
from book_translator.terminology import apply_proposals, candidate_identity, set_decision
from book_translator.terminology.analysis import TerminologyAnalysisError, proposal_identity
from book_translator.terminology.canonical import canonical_view
from book_translator.terminology.providers import (
    FakeTerminologyProvider,
    TerminologyProviderError,
    TerminologyReviewRequest,
)
from book_translator.terminology.review import (
    parse_review_response,
    review_identity,
    review_prompt,
    validate_reviews,
)
from book_translator.terminology.workflow import TerminologyRunPlan, _review_risk, _review_shards


def registry() -> TerminologyRegistry:
    return TerminologyRegistry(
        schema_version="1.0",
        version="1.0.0",
        book_id="synthetic-review",
        source_language="source-x",
        target_language="target-y",
    )


def glossary() -> dict[str, object]:
    return {
        "terms": {},
        "characters": {},
        "places": {},
        "groups": {},
        "unresolved_terms": [],
    }


def candidate(source: str = "Order Veyra") -> TerminologyCandidate:
    return TerminologyCandidate(
        candidate_id=candidate_identity(source, "source-x"),
        source_term=source,
        classification=TerminologyType.ORGANIZATION,
        confidence=0.96,
        provenance=DecisionSource.AI,
    )


def proposal(item: TerminologyCandidate | None = None) -> TerminologyProposal:
    item = item or candidate()
    target = "Target Veyra"
    return TerminologyProposal(
        proposal_id=proposal_identity(item.candidate_id, target, "target-y"),
        candidate_id=item.candidate_id,
        source_term=item.source_term,
        proposed_target=target,
        confidence=0.97,
        canonicalization_confidence=0.96,
        classification=item.classification,
    )


def review(
    disposition: TerminologyReviewDisposition,
    *,
    target: str | None,
    item: TerminologyProposal | None = None,
) -> TerminologyReview:
    item = item or proposal()
    return TerminologyReview(
        review_id="tr-" + "0" * 24,
        proposal_id=item.proposal_id,
        disposition=disposition,
        reviewed_target=target,
        confidence=0.93,
        meaning_fidelity=0.9,
        target_naturalness=0.9,
        transliteration_stability=0.9,
        terminology_consistency=0.9,
        register_appropriateness=0.9,
        morphology_suitability=0.9,
        ambiguity_safety=0.9,
        reason_codes=["synthetic_review"],
        notes=[],
    )


@pytest.mark.parametrize(
    ("disposition", "target"),
    [
        (TerminologyReviewDisposition.ACCEPT, "Target Veyra"),
        (TerminologyReviewDisposition.REVISE, "Better Veyra"),
        (TerminologyReviewDisposition.REJECT, None),
        (TerminologyReviewDisposition.DEFER, None),
    ],
)
def test_review_dispositions_validate(
    disposition: TerminologyReviewDisposition, target: str | None
) -> None:
    value = review(disposition, target=target)
    result = validate_reviews(
        TerminologyReviewResponse(schema_version="1.0", reviews=[value]),
        (proposal(),),
        (candidate(),),
        (),
        "target-y",
    )
    assert result[0].review_id == review_identity(proposal().proposal_id, disposition, target)


@pytest.mark.parametrize("target", ["Target Veyra", ""])
def test_revise_rejects_identical_or_empty_raw_target(target: str) -> None:
    value = review(TerminologyReviewDisposition.REVISE, target=target)
    with pytest.raises(TerminologyAnalysisError, match="different non-empty"):
        validate_reviews(
            TerminologyReviewResponse(schema_version="1.0", reviews=[value]),
            (proposal(),),
            (candidate(),),
            (),
            "target-y",
        )


def test_persian_style_only_revision_is_not_a_semantic_revision() -> None:
    item = proposal().model_copy(update={"proposed_target": "مارا تومانِس"})
    value = review(TerminologyReviewDisposition.REVISE, target="مارا تومانِس", item=item)
    with pytest.raises(TerminologyAnalysisError, match="different non-empty"):
        validate_reviews(
            TerminologyReviewResponse(schema_version="1.0", reviews=[value]),
            (item,),
            (candidate(),),
            (),
            "fa",
        )

    removed = review(TerminologyReviewDisposition.REVISE, target="مارا تومانِس", item=item)
    assert removed.reviewed_target == item.proposed_target


def test_genuinely_different_persian_revision_is_normalized_and_auditable() -> None:
    item = proposal().model_copy(update={"proposed_target": "هدف"})
    value = review(TerminologyReviewDisposition.REVISE, target="بِهتر", item=item)
    result = validate_reviews(
        TerminologyReviewResponse(schema_version="1.0", reviews=[value]),
        (item,),
        (candidate(),),
        (),
        "fa",
    )[0]
    assert result.reviewed_target == "بهتر"
    assert result.provider_reviewed_target == "بِهتر"


def test_exact_persisted_failed_review_values_remain_rejected() -> None:
    item = proposal().model_copy(
        update={
            "proposal_id": "tp-29c79e2dada8bfdaa015000b",
            "source_term": "Mara Tomanes",
            "proposed_target": "مارا تومانِس",
        }
    )
    value = review(TerminologyReviewDisposition.REVISE, target="مارا تومانِس", item=item)
    with pytest.raises(TerminologyAnalysisError, match="different non-empty"):
        validate_reviews(
            TerminologyReviewResponse(schema_version="1.0", reviews=[value]),
            (item,),
            (candidate("Mara Tomanes"),),
            (),
            "fa",
        )


def test_malformed_duplicate_missing_and_reuse_review_are_rejected() -> None:
    with pytest.raises(TerminologyAnalysisError, match="malformed"):
        parse_review_response({"schema_version": "1.0", "reviews": [{"invalid": True}]})
    value = review(TerminologyReviewDisposition.ACCEPT, target="Target Veyra")
    response = TerminologyReviewResponse(schema_version="1.0", reviews=[value, value])
    with pytest.raises(TerminologyAnalysisError, match="duplicate"):
        validate_reviews(response, (proposal(),), (candidate(),), (), "target-y")
    with pytest.raises(TerminologyAnalysisError, match="omitted"):
        validate_reviews(
            TerminologyReviewResponse(schema_version="1.0"),
            (proposal(),),
            (candidate(),),
            (),
            "target-y",
        )
    reused = proposal().model_copy(update={"action": "reuse_existing"})
    with pytest.raises(TerminologyAnalysisError, match="reuse-existing"):
        validate_reviews(
            response.model_copy(update={"reviews": [value]}),
            (reused,),
            (candidate(),),
            (),
            "target-y",
        )


def test_reviewed_target_cannot_break_locked_subterm() -> None:
    current = set_decision(
        registry(),
        source_term="Veyra",
        target_term="CanonicalVeyra",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    value = review(TerminologyReviewDisposition.REVISE, target="Better Other")
    result = validate_reviews(
        TerminologyReviewResponse(schema_version="1.0", reviews=[value]),
        (proposal(),),
        (candidate(),),
        canonical_view(current, glossary()),
        "target-y",
    )
    assert result[0].blocked
    assert "canonical_subterm_conflict" in result[0].conflict_codes
    updated, application = apply_proposals(
        current,
        (candidate(),),
        (proposal(),),
        policy=TerminologyAcceptancePolicy.AUTOMATIC,
        threshold=0.9,
        canonical_terms=canonical_view(current, glossary()),
        reviews=result,
        review_required=True,
    )
    assert application.applied == 0
    assert updated.decisions[0].target_term == "CanonicalVeyra"


def test_application_revalidates_stale_review_conflict_flags() -> None:
    current = set_decision(
        registry().model_copy(update={"target_language": "fa"}),
        source_term="Order",
        target_term="نگهبان روشنایی",
        classification=TerminologyType.ORGANIZATION,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    reviewed_candidate = candidate("Order War").model_copy(
        update={"classification": TerminologyType.HISTORICAL_EVENT}
    )
    reviewed_proposal = proposal(reviewed_candidate).model_copy(
        update={"proposed_target": "جنگ نگهبانان روشنایی"}
    )
    stale_review = review(
        TerminologyReviewDisposition.ACCEPT, target="جنگ نگهبانان روشنایی"
    ).model_copy(
        update={
            "proposal_id": reviewed_proposal.proposal_id,
            "reviewed_target": "جنگ نگهبانان روشنایی",
            "blocked": True,
            "conflict_codes": ["canonical_subterm_conflict"],
        }
    )
    updated, application = apply_proposals(
        current,
        (reviewed_candidate,),
        (reviewed_proposal,),
        policy=TerminologyAcceptancePolicy.AUTOMATIC,
        threshold=0.9,
        canonical_terms=canonical_view(current, glossary()),
        reviews=(stale_review,),
        review_required=True,
    )
    assert application.applied == 1
    assert application.conflicts == 0
    assert len(updated.decisions) == 2


def test_reviewed_target_cannot_override_exact_canonical_decision() -> None:
    exact_candidate = candidate("Veyra")
    exact_proposal = proposal(exact_candidate)
    current = set_decision(
        registry(),
        source_term="Veyra",
        target_term="CanonicalVeyra",
        classification=TerminologyType.PERSON,
        decision_source=DecisionSource.USER,
        locked=True,
    )
    value = review(
        TerminologyReviewDisposition.ACCEPT,
        target=exact_proposal.proposed_target,
        item=exact_proposal,
    )
    result = validate_reviews(
        TerminologyReviewResponse(schema_version="1.0", reviews=[value]),
        (exact_proposal,),
        (exact_candidate,),
        canonical_view(current, glossary()),
        "target-y",
    )
    assert result[0].blocked
    assert "exact_canonical_conflict" in result[0].conflict_codes


@pytest.mark.parametrize(
    ("disposition", "target", "applied"),
    [
        (TerminologyReviewDisposition.ACCEPT, "Target Veyra", "Target Veyra"),
        (TerminologyReviewDisposition.REVISE, "Better Veyra", "Better Veyra"),
        (TerminologyReviewDisposition.REJECT, None, None),
        (TerminologyReviewDisposition.DEFER, None, None),
    ],
)
def test_review_gate_controls_application(
    disposition: TerminologyReviewDisposition, target: str | None, applied: str | None
) -> None:
    reviewed = validate_reviews(
        TerminologyReviewResponse(
            schema_version="1.0", reviews=[review(disposition, target=target)]
        ),
        (proposal(),),
        (candidate(),),
        (),
        "target-y",
    )
    updated, result = apply_proposals(
        registry(),
        (candidate(),),
        (proposal(),),
        policy=TerminologyAcceptancePolicy.AUTOMATIC,
        threshold=0.9,
        reviews=reviewed,
        review_required=True,
    )
    assert result.applied == (1 if applied else 0)
    actual = updated.decisions[0].target_term if updated.decisions else None
    assert actual == applied


def test_review_required_blocks_unreviewed_high_confidence_proposal() -> None:
    updated, result = apply_proposals(
        registry(),
        (candidate(),),
        (proposal(),),
        policy=TerminologyAcceptancePolicy.HIGH_CONFIDENCE,
        threshold=0.9,
        review_required=True,
    )
    assert result.applied == 0
    assert result.findings[0].code == "review_required"
    assert not updated.decisions


def test_fake_review_provider_and_style_prompt_are_deterministic() -> None:
    response = TerminologyReviewResponse(
        schema_version="1.0",
        reviews=[review(TerminologyReviewDisposition.ACCEPT, target="Target Veyra")],
    )
    request = TerminologyReviewRequest("r", "s", "u", "x", "y", (proposal().proposal_id,))
    provider = FakeTerminologyProvider(review_response=response)
    assert provider.review(request) == provider.review(request)
    assert "style_resource" in review_prompt(({"proposal": "synthetic"},), "orthography policy")


def test_persian_style_resource_supplies_diacritic_policy_to_review() -> None:
    style = Path("config/style-guide.md").read_text(encoding="utf-8")
    assert "فتحه، کسره و ضمه" in style
    rendered = review_prompt(({"proposal": "synthetic"},), style)
    assert "فتحه، کسره و ضمه" in rendered


@pytest.mark.parametrize("scenario", ["missing_review", "duplicate_review", "conflicting_review"])
def test_fake_review_provider_validation_scenarios(scenario: str) -> None:
    response = TerminologyReviewResponse(
        schema_version="1.0",
        reviews=[review(TerminologyReviewDisposition.ACCEPT, target="Target Veyra")],
    )
    request = TerminologyReviewRequest("r", "s", "u", "x", "y", (proposal().proposal_id,))
    raw = FakeTerminologyProvider(scenario, review_response=response).review(request)
    with pytest.raises(TerminologyAnalysisError):
        validate_reviews(raw, (proposal(),), (candidate(),), (), "target-y")


@pytest.mark.parametrize(
    "scenario", ["retryable_failure", "non_retryable_failure", "ambiguous_failure"]
)
def test_fake_review_provider_failure_scenarios(scenario: str) -> None:
    request = TerminologyReviewRequest("r", "s", "u", "x", "y", (proposal().proposal_id,))
    with pytest.raises(TerminologyProviderError):
        FakeTerminologyProvider(scenario).review(request)


def test_review_shard_plan_is_deterministic_bounded_and_complete(tmp_path: Path) -> None:
    candidates = tuple(candidate(f"Synthetic Term {index}") for index in range(125))
    proposals = tuple(proposal(item) for item in candidates)
    plan = TerminologyRunPlan(
        project_root=tmp_path,
        output_root=tmp_path,
        run_id="ta-" + "1" * 24,
        stage=TerminologyAnalysisStage.REVIEW,
        snapshot_id="synthetic",
        manifest_sha256="1" * 64,
        source_language="source-x",
        target_language="target-y",
        sources=(),
        provider="fake",
        provider_adapter_version="1.0.0",
        model=None,
        request_sha256="2" * 64,
        request_payload={},
        proposal_run_id="ta-" + "2" * 24,
        style_resource_sha256="3" * 64,
        style_guide="synthetic style",
        candidates=candidates,
        proposals=proposals,
    )
    items = tuple(
        {
            "proposal": value.model_dump(mode="json"),
            "candidate": item.model_dump(mode="json"),
            "risk_class": "low",
        }
        for item, value in zip(candidates, proposals, strict=True)
    )
    first = _review_shards(plan, items)
    second = _review_shards(plan, items)
    assert first == second
    assert all(len(shard["proposal_ids"]) <= 60 for shard in first)
    flattened = [proposal_id for shard in first for proposal_id in shard["proposal_ids"]]
    assert len(flattened) == len(set(flattened)) == 125
    assert set(flattened) == {item.proposal_id for item in proposals}


def test_risk_planning_does_not_turn_high_risk_into_deferral() -> None:
    item = candidate().model_copy(update={"context_sensitive": True})
    assert _review_risk(item, proposal(item)) == "high"
    assert _review_risk(candidate(), proposal()) == "low"


def test_unexpected_review_id_is_rejected() -> None:
    other = candidate("Other Veyra")
    unexpected = review(
        TerminologyReviewDisposition.ACCEPT, target="Target Veyra", item=proposal(other)
    )
    with pytest.raises(TerminologyAnalysisError, match="unknown"):
        validate_reviews(
            TerminologyReviewResponse(schema_version="1.0", reviews=[unexpected]),
            (proposal(),),
            (candidate(), other),
            (),
            "target-y",
        )
