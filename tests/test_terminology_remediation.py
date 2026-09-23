from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import (
    CandidateStatus,
    DecisionSource,
    TerminologyCandidate,
    TerminologyDecision,
    TerminologyOccurrence,
    TerminologyRegistry,
    TerminologyType,
)
from book_translator.models.terminology_remediation import TerminologyFindingFamily
from book_translator.terminology import candidate_identity, decision_identity, save_registry
from book_translator.terminology.remediation import (
    TerminologyRemediationAction,
    TerminologyRemediationCategory,
    audit_registry_remediation,
    inspect_remediation_finding,
)

runner = CliRunner()


def registry() -> TerminologyRegistry:
    return TerminologyRegistry.model_construct(
        schema_version="1.0",
        registry_kind="terminology",
        version="1.0.1",
        book_id="synthetic-remediation",
        source_language="en",
        target_language="fa",
        candidates=[],
        decisions=[],
    )


def candidate(
    source_term: str,
    *,
    classification: TerminologyType = TerminologyType.PERSON,
    aliases: tuple[str, ...] = (),
    occurrences: tuple[TerminologyOccurrence, ...] = (),
) -> TerminologyCandidate:
    return TerminologyCandidate.model_construct(
        candidate_id=candidate_identity(source_term, "en"),
        source_term=source_term,
        classification=classification,
        confidence=1.0,
        status=CandidateStatus.APPROVED,
        provenance=DecisionSource.AI,
        aliases=list(aliases),
        occurrences=list(occurrences),
        context_sensitive=False,
        classification_conflicts=[],
        evidence_notes=[],
    )


def decision(
    source_term: str,
    target_term: str,
    *,
    classification: TerminologyType = TerminologyType.PERSON,
    decision_source: DecisionSource = DecisionSource.USER,
    locked: bool = False,
    competing_variants: tuple[str, ...] = (),
) -> TerminologyDecision:
    candidate_id = candidate_identity(source_term, "en")
    return TerminologyDecision.model_construct(
        decision_id=decision_identity(candidate_id, "fa"),
        candidate_id=candidate_id,
        target_term=target_term,
        decision_source=decision_source,
        locked=locked,
        required=True,
        competing_variants=list(competing_variants),
        contextual_notes=[],
    )


def append(
    current: TerminologyRegistry, *items: tuple[TerminologyCandidate, TerminologyDecision]
) -> TerminologyRegistry:
    candidates = list(current.candidates)
    decisions = list(current.decisions)
    for cand, dec in items:
        candidates.append(cand)
        decisions.append(dec)
    return current.model_copy(update={"candidates": candidates, "decisions": decisions})


def glossary() -> dict[str, object]:
    return {
        "terms": {},
        "characters": {},
        "places": {},
        "groups": {},
        "unresolved_terms": [],
    }


def test_registry_remediation_audit_classifies_primary_categories() -> None:
    current = append(
        registry(),
        (
            candidate("Gareth Bryne", classification=TerminologyType.PERSON),
            decision("Gareth Bryne", "گرث براین", locked=True),
        ),
        (
            candidate("Lord Gareth Bryne", classification=TerminologyType.TITLE),
            decision("Lord Gareth Bryne", "لرد گرث براین", classification=TerminologyType.TITLE),
        ),
        (
            candidate("Mat Cauthon", classification=TerminologyType.PERSON, aliases=("Mat",)),
            decision("Mat Cauthon", "مت کاتن", locked=True),
        ),
        (
            candidate("Mat", classification=TerminologyType.PERSON),
            decision(
                "Mat",
                "مت",
                classification=TerminologyType.PERSON,
                decision_source=DecisionSource.AI,
            ),
        ),
        (
            candidate("Eldar", classification=TerminologyType.PLACE),
            TerminologyDecision.model_construct(
                decision_id=decision_identity(candidate_identity("Eldar", "en"), "fa"),
                candidate_id=candidate_identity("Eldar", "en"),
                target_term="الدار",
                decision_source=DecisionSource.USER,
                locked=True,
                required=True,
                competing_variants=["الدارى"],
                contextual_notes=[],
            ),
        ),
        (
            candidate("Dragon", classification=TerminologyType.PERSON),
            decision("Dragon", "اژدها", classification=TerminologyType.PERSON, locked=True),
        ),
        (
            candidate("Lord Dragon", classification=TerminologyType.TITLE),
            decision("Lord Dragon", "ناسازگار", classification=TerminologyType.TITLE),
        ),
        (
            candidate("River Eldar", classification=TerminologyType.PLACE),
            TerminologyDecision.model_construct(
                decision_id=decision_identity(candidate_identity("River Eldar", "en"), "fa"),
                candidate_id=candidate_identity("River Eldar", "en"),
                target_term="رود الدارى",
                decision_source=DecisionSource.AI,
                locked=False,
                required=True,
                contextual_notes=[],
            ),
        ),
    )

    report = audit_registry_remediation(current, glossary())
    by_source = {
        item.source_term: item
        for item in report.findings
        if item.finding_family is TerminologyFindingFamily.CANONICAL_VALIDATOR
    }

    assert "Lord Gareth Bryne" not in by_source
    assert by_source["Mat"].remediation_category is (
        TerminologyRemediationCategory.ALIAS_MISCLASSIFICATION
    )
    assert by_source["Mat"].proposed_action is TerminologyRemediationAction.CONVERT_TO_ALIAS
    assert by_source["Lord Dragon"].remediation_category is (
        TerminologyRemediationCategory.CONFLICTING_TARGET
    )
    assert by_source["Lord Dragon"].proposed_action is (
        TerminologyRemediationAction.CHANGE_TARGET_REQUIRES_REVIEW
    )
    assert by_source["River Eldar"].remediation_category is (
        TerminologyRemediationCategory.INVALID_COMPETING_VARIANT
    )
    assert by_source["River Eldar"].proposed_action is (
        TerminologyRemediationAction.REMOVE_INVALID_VARIANT
    )
    assert report.registry_finding_count == 3
    assert report.validator_false_positive_count == 0
    assert report.definitely_invalid_count == 3
    # The exact-enforcement family reports the same registry independently, and every decision it
    # flags here is locked, so it proposes no change at all.
    assert report.exactness_finding_count == 3
    assert report.owner_governed_count == 3
    assert report.findings_total == 6


def test_registry_remediation_audit_reports_identity_mismatch_and_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = registry().model_copy(
        update={
            "candidates": [
                candidate("Two Rivers' ", classification=TerminologyType.PLACE),
                candidate("Two Rivers'", classification=TerminologyType.PLACE),
            ],
            "decisions": [
                decision(
                    "Two Rivers'", "دو رود", classification=TerminologyType.PLACE, locked=False
                )
            ],
        }
    )
    report = audit_registry_remediation(current, glossary())
    assert report.bookkeeping_mismatch is not None
    assert report.bookkeeping_mismatch.original_candidate_id == candidate_identity(
        "Two Rivers' ", "en"
    )
    assert report.bookkeeping_mismatch.normalized_candidate_id == candidate_identity(
        "Two Rivers'", "en"
    )

    config = tmp_path / "config"
    config.mkdir()
    save_registry(config / "terminology.yaml", current)
    (config / "glossary.yaml").write_text(
        "terms: {}\ncharacters: {}\nplaces: {}\ngroups: {}\nunresolved_terms: []\n",
        encoding="utf-8",
    )
    before = (config / "terminology.yaml").read_bytes()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["audit-terminology-registry-remediation", "--json"])
    after = (config / "terminology.yaml").read_bytes()
    assert result.exit_code == 0
    assert before == after


def test_registry_remediation_audit_is_deterministic_and_inspectable() -> None:
    current = append(
        registry(),
        (
            candidate("Mat Cauthon", classification=TerminologyType.PERSON, aliases=("Mat",)),
            decision("Mat Cauthon", "مت کاتن", locked=True),
        ),
        (
            candidate("Mat", classification=TerminologyType.PERSON),
            decision("Mat", "مت", decision_source=DecisionSource.AI),
        ),
    )
    first = audit_registry_remediation(current, glossary())
    second = audit_registry_remediation(current, glossary())
    assert [item.source_term for item in first.findings] == ["Mat"]
    assert [item.finding_id for item in first.findings] == [
        item.finding_id for item in second.findings
    ]
    assert inspect_remediation_finding(first, first.findings[0].finding_id).source_term == "Mat"


def test_registry_remediation_cli_filters_and_show_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = append(
        registry(),
        (
            candidate("Gareth Bryne", classification=TerminologyType.PERSON),
            decision("Gareth Bryne", "گرث براین", locked=True),
        ),
        (
            candidate("Mat Cauthon", classification=TerminologyType.PERSON, aliases=("Mat",)),
            decision("Mat Cauthon", "مت کاتن", locked=True),
        ),
        (
            candidate("Mat", classification=TerminologyType.PERSON),
            decision("Mat", "مت", decision_source=DecisionSource.AI),
        ),
    )
    config = tmp_path / "config"
    config.mkdir()
    save_registry(config / "terminology.yaml", current)
    (config / "glossary.yaml").write_text(
        "terms: {}\ncharacters: {}\nplaces: {}\ngroups: {}\nunresolved_terms: []\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "audit-terminology-registry-remediation",
            "--category",
            "alias_misclassification",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = yaml.safe_load(result.stdout)
    assert len(payload["findings"]) == 1
    finding_id = payload["findings"][0]["finding_id"]
    show = runner.invoke(app, ["show-terminology-remediation-finding", finding_id])
    assert show.exit_code == 0
    assert finding_id in show.stdout
