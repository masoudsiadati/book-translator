"""Governed, all-or-nothing series policy amendments and the lineage they leave behind.

The synthetic policy holds one explicit user decision (Ajah), promoted strong references, and
observed variants. Every Persian string is an invented form, never a translation.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from book_translator.cli import app
from book_translator.models import (
    SeriesPolicyAmendmentRecord,
    SeriesPolicyAmendmentRequest,
    SeriesProvenanceKind,
    SeriesTermStatus,
)
from book_translator.paths import ProjectPaths
from book_translator.series_terminology.amendment import (
    SeriesPolicyAmendmentConflict,
    amendment_identity,
    apply_amendment,
)
from book_translator.series_terminology.policy import find_term, load_policy, save_policy
from book_translator.series_terminology.workflow import (
    execute_book_terminology,
    execute_policy_amendment,
    explain_policy_move,
    load_policy_amendments,
    plan_book_terminology,
    plan_policy_amendment,
)
from book_translator.terminology import load_registry
from tests.test_series_terminology import (
    BOOK,
    FORM_AJAH_USER,
    FORM_SEDAI,
    runner,
    series_project,
    synthetic_policy,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
SEDAI_SPACED = "آیز سدای"
SEDAI_PLURAL = "آیزسدای‌ها"


def _request(*terms: dict[str, Any], version: str = "1.1.0") -> SeriesPolicyAmendmentRequest:
    return SeriesPolicyAmendmentRequest.model_validate(
        {
            "schema_version": "1.0",
            "series_id": "synthetic-series",
            "resulting_policy_version": version,
            "rationale": "synthetic governed amendment",
            "terms": list(terms),
        }
    )


def _sedai(**updates: Any) -> dict[str, Any]:
    return {
        "source_term": "Aes Sedai",
        "status": "LOCKED",
        "target_term": FORM_SEDAI,
        "allowed_target_forms": [SEDAI_SPACED, SEDAI_PLURAL],
        "aliases": ["Sedai"],
        "rationale": "explicitly settled",
        **updates,
    }


def _write_request(root: Path, request: SeriesPolicyAmendmentRequest) -> Path:
    path = root / "amendment.yaml"
    path.write_text(yaml.safe_dump(request.model_dump(mode="json"), allow_unicode=True))
    return path


def _policy_path(root: Path) -> Path:
    return root / "config/series/synthetic/terminology-policy.yaml"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- pure application ----------------------------------------------------------------------------


def test_amendment_restates_the_term_and_keeps_its_evidence() -> None:
    policy = synthetic_policy()
    before = find_term(policy, "Aes Sedai")
    assert before is not None

    amended = apply_amendment(policy, _request(_sedai()), amendment_id="spa-" + "0" * 24)

    after = find_term(amended.policy, "Aes Sedai")
    assert after is not None
    assert after.status is SeriesTermStatus.LOCKED
    assert after.provenance.kind is SeriesProvenanceKind.EXPLICIT_USER
    assert after.provenance.origin == "amend-series-terminology:spa-" + "0" * 24
    assert sorted(after.allowed_target_forms) == sorted([SEDAI_SPACED, SEDAI_PLURAL])
    assert after.aliases == ["Sedai"]
    assert after.variants == before.variants and after.conflicts == before.conflicts
    assert amended.policy.version == "1.1.0"
    (change,) = amended.changes
    assert change.status_before is SeriesTermStatus.STRONG_REFERENCE
    assert change.provenance_before is SeriesProvenanceKind.PROMOTED_REFERENCE
    assert change.aliases_added == ["Sedai"] and change.aliases_removed == []
    assert sorted(change.forms_added) == sorted([SEDAI_SPACED, SEDAI_PLURAL])
    # Nothing else in the policy moved.
    untouched = {term.concept_id: term for term in policy.terms if term.source_term != "Aes Sedai"}
    assert {
        term.concept_id: term for term in amended.policy.terms if term.concept_id in untouched
    } == untouched


@pytest.mark.parametrize(
    ("terms", "reason"),
    [
        ([_sedai(), {**_sedai(), "source_term": "still"}], "no such series term"),
        ([_sedai(), {**_sedai(source_term="Ajah", target_term="x", aliases=[])}], "target"),
        (
            [{**_sedai(source_term="Ajah", target_term=FORM_AJAH_USER, aliases=[])}],
            None,
        ),
        ([_sedai(aliases=["Ajah"])], "claimed by the user decision"),
        (
            [
                _sedai(
                    source_term="Ajah",
                    target_term=FORM_AJAH_USER,
                    status="STRONG_REFERENCE",
                    aliases=[],
                    allowed_target_forms=[],
                )
            ],
            "force",
        ),
    ],
    ids=["creates_term", "user_target", "user_restated", "user_alias", "user_force"],
)
def test_an_amendment_touching_a_user_decision_is_refused_whole(
    terms: list[dict[str, Any]], reason: str | None
) -> None:
    policy = synthetic_policy()
    snapshot = policy.model_dump()
    request = _request(*terms)

    if reason is None:
        # Restating a user decision without changing it is not a conflict.
        apply_amendment(policy, request, amendment_id="spa-" + "1" * 24)
        return
    with pytest.raises(SeriesPolicyAmendmentConflict, match=reason):
        apply_amendment(policy, request, amendment_id="spa-" + "1" * 24)
    assert policy.model_dump() == snapshot


def test_withdrawing_what_a_user_authorized_is_refused() -> None:
    policy = apply_amendment(
        synthetic_policy(), _request(_sedai()), amendment_id="spa-" + "2" * 24
    ).policy

    for narrowed, reason in (
        (_sedai(allowed_target_forms=[SEDAI_SPACED]), "withdraws a form"),
        (_sedai(aliases=[]), "withdraws an alias"),
    ):
        with pytest.raises(SeriesPolicyAmendmentConflict, match=reason):
            apply_amendment(
                policy, _request(narrowed, version="1.2.0"), amendment_id="spa-" + "3" * 24
            )


@pytest.mark.parametrize(
    ("request_updates", "reason"),
    [({"resulting_policy_version": "1.0.0"}, "not newer"), ({"series_id": "other"}, "series")],
)
def test_version_and_series_must_match(request_updates: dict[str, Any], reason: str) -> None:
    request = _request(_sedai()).model_copy(update=request_updates)
    with pytest.raises(SeriesPolicyAmendmentConflict, match=reason):
        apply_amendment(synthetic_policy(), request, amendment_id="spa-" + "4" * 24)


def test_request_contract_rejects_ambiguous_lists() -> None:
    with pytest.raises(ValueError):
        _request(_sedai(aliases=["Aes Sedai"]))
    with pytest.raises(ValueError):
        _request(_sedai(allowed_target_forms=[FORM_SEDAI]))
    with pytest.raises(ValueError):
        _request(_sedai(), _sedai())
    with pytest.raises(ValueError):
        _request(_sedai(status="OBSERVED_VARIANTS"))


# --- workflow and lineage ------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_apply_records_before_replacing(tmp_path: Path) -> None:
    series_project(tmp_path)
    policy_path = _policy_path(tmp_path)
    before = _sha(policy_path)
    request_path = _write_request(tmp_path, _request(_sedai()))

    plan = plan_policy_amendment(
        project_root=tmp_path, request_path=request_path, book=BOOK, now=lambda: NOW
    )
    assert plan.action == "apply" and _sha(policy_path) == before
    assert not ProjectPaths(tmp_path).series_policy_amendments.exists()
    assert plan.record.amendment_id == amendment_identity(before, _request(_sedai()))

    execute_policy_amendment(plan)

    (record,) = load_policy_amendments(tmp_path)
    assert record.policy_sha256_before == before
    assert record.policy_sha256_after == _sha(policy_path) != before
    assert load_policy(policy_path).version == "1.1.0"
    metadata = yaml.safe_load(policy_path.with_suffix(".metadata.yaml").read_text())
    assert metadata["version"] == "1.1.0"
    again = plan_policy_amendment(
        project_root=tmp_path, request_path=request_path, book=BOOK, now=lambda: NOW
    )
    assert again.action == "skip"
    assert execute_policy_amendment(again).record == record


def test_an_interrupted_application_is_completed_not_duplicated(tmp_path: Path) -> None:
    series_project(tmp_path)
    policy_path = _policy_path(tmp_path)
    original = policy_path.read_bytes()
    request_path = _write_request(tmp_path, _request(_sedai()))
    execute_policy_amendment(
        plan_policy_amendment(project_root=tmp_path, request_path=request_path, book=BOOK)
    )
    # Simulate a crash after the record and before the policy replacement.
    policy_path.write_bytes(original)

    plan = plan_policy_amendment(project_root=tmp_path, request_path=request_path, book=BOOK)
    assert plan.action == "complete"
    execute_policy_amendment(plan)

    assert len(load_policy_amendments(tmp_path)) == 1
    assert _sha(policy_path) == plan.record.policy_sha256_after


def test_a_policy_edited_after_planning_is_refused(tmp_path: Path) -> None:
    series_project(tmp_path)
    policy_path = _policy_path(tmp_path)
    plan = plan_policy_amendment(
        project_root=tmp_path, request_path=_write_request(tmp_path, _request(_sedai())), book=BOOK
    )
    save_policy(load_policy(policy_path).model_copy(update={"version": "1.0.1"}), policy_path)

    with pytest.raises(ValueError, match="changed after the amendment was planned"):
        execute_policy_amendment(plan)
    assert not ProjectPaths(tmp_path).series_policy_amendments.exists()


def test_a_conflicting_amendment_leaves_policy_and_lineage_untouched(tmp_path: Path) -> None:
    series_project(tmp_path)
    policy_path = _policy_path(tmp_path)
    before = policy_path.read_bytes()
    request = _request(_sedai(), _sedai(source_term="Ajah", target_term="x", aliases=[]))

    with pytest.raises(SeriesPolicyAmendmentConflict):
        plan_policy_amendment(
            project_root=tmp_path, request_path=_write_request(tmp_path, request), book=BOOK
        )
    assert policy_path.read_bytes() == before
    assert not ProjectPaths(tmp_path).series_policy_amendments.exists()


def test_lineage_explains_a_chain_of_amendments_and_nothing_else(tmp_path: Path) -> None:
    series_project(tmp_path)
    policy_path = _policy_path(tmp_path)
    origin = _sha(policy_path)
    execute_policy_amendment(
        plan_policy_amendment(
            project_root=tmp_path,
            request_path=_write_request(tmp_path, _request(_sedai())),
            book=BOOK,
        )
    )
    middle = _sha(policy_path)
    lan = {
        "source_term": "Lan",
        "status": "LOCKED",
        "target_term": "لان",
        "aliases": ["al'Lan"],
        "rationale": "settled",
    }
    execute_policy_amendment(
        plan_policy_amendment(
            project_root=tmp_path,
            request_path=_write_request(tmp_path, _request(lan, version="1.2.0")),
            book=BOOK,
        )
    )
    head = _sha(policy_path)
    records = load_policy_amendments(tmp_path)

    chain = explain_policy_move(
        records, from_sha256=origin, to_sha256=head, series_id="synthetic-series"
    )
    assert chain is not None and [item.policy_sha256_after for item in chain] == [middle, head]
    assert (
        explain_policy_move(records, from_sha256=head, to_sha256=head, series_id="synthetic-series")
        == ()
    )
    assert (
        explain_policy_move(
            records, from_sha256=head, to_sha256=origin, series_id="synthetic-series"
        )
        is None
    )
    assert (
        explain_policy_move(records, from_sha256=origin, to_sha256=head, series_id="other") is None
    )


def test_the_derived_registry_carries_the_amended_decision(tmp_path: Path) -> None:
    series_project(tmp_path)
    execute_policy_amendment(
        plan_policy_amendment(
            project_root=tmp_path,
            request_path=_write_request(tmp_path, _request(_sedai())),
            book=BOOK,
        )
    )

    plan = execute_book_terminology(plan_book_terminology(project_root=tmp_path, book=BOOK))
    registry = load_registry(plan.registry_path)

    candidate = next(item for item in registry.candidates if item.source_term == "Aes Sedai")
    decision = next(
        item for item in registry.decisions if item.candidate_id == candidate.candidate_id
    )
    assert decision.locked and sorted(decision.allowed_target_forms) == sorted(
        [SEDAI_SPACED, SEDAI_PLURAL]
    )


def test_record_contract_requires_a_moved_policy() -> None:
    record = {
        "schema_version": "1.0",
        "amendment_engine_version": "1.0.0",
        "amendment_id": "spa-" + "5" * 24,
        "series_id": "synthetic-series",
        "policy_file": "config/series/synthetic/terminology-policy.yaml",
        "policy_sha256_before": "a" * 64,
        "policy_sha256_after": "a" * 64,
        "policy_identity_before": "b" * 64,
        "policy_identity_after": "c" * 64,
        "policy_version_before": "1.0.0",
        "policy_version_after": "1.1.0",
        "request": _request(_sedai()).model_dump(mode="json"),
        "request_sha256": "d" * 64,
        "changes": [],
        "created_at": NOW.isoformat(),
    }
    with pytest.raises(ValueError):
        SeriesPolicyAmendmentRecord.model_validate(record)


def test_cli_dry_run_then_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    series_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    policy_path = _policy_path(tmp_path)
    before = _sha(policy_path)
    request_path = _write_request(tmp_path, _request(_sedai()))

    result = runner.invoke(app, ["amend-series-terminology", str(request_path), "--book", BOOK])
    assert result.exit_code == 0, result.output
    assert "apply (dry run)" in result.output and _sha(policy_path) == before

    result = runner.invoke(
        app, ["amend-series-terminology", str(request_path), "--book", BOOK, "--apply"]
    )
    assert result.exit_code == 0, result.output
    assert _sha(policy_path) != before

    refused = _write_request(
        tmp_path, _request(_sedai(source_term="Ajah", target_term="x", aliases=[]), version="1.2.0")
    )
    result = runner.invoke(
        app, ["amend-series-terminology", str(refused), "--book", BOOK, "--apply"]
    )
    assert result.exit_code == 1 and "nothing applied" in result.output
