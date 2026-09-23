"""Provider-free cross-snapshot migration of a series-policy book's historical run.

One synthetic series book, one historical run under the marked paragraph contract, one governed
policy amendment that authorizes a further target form for the locked Ajah decision, and one
successor snapshot and run. Each chunk exercises one migration class:

* ``001-001`` wrote the newly authorized form, so its old rejection clears (class 2);
* ``002-001`` was accepted and names Ajah only in its context, so its request moved (class 1);
* ``003-001`` was accepted and never names Ajah, so its request is identical (class 1, reuse);
* ``004-001`` wrote a form nobody authorized, so it stays rejected and is repaired (class 3);
* ``005-001`` failed at the provider and has no output to carry (fresh translation).

Every Persian string is invented; none is a translation.
"""

import hashlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from book_translator.cli import app
from book_translator.historical_reevaluation_policy import evaluate_series_term_drift
from book_translator.models import (
    ProviderName,
    SeriesPolicyAmendmentRequest,
    SeriesProvenanceKind,
    SeriesTermStatus,
    TerminologyType,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationExecutionKind,
    TranslationHistoricalReevaluationEvidence,
    TranslationJobStatus,
)
from book_translator.paragraph_markers import mark_paragraphs
from book_translator.prompt_packaging import execute_prompt_packages, plan_prompt_packages
from book_translator.prompt_packaging.identity import MARKED_OUTPUT_CONTRACT_VERSION
from book_translator.provider_execution import list_attempts
from book_translator.provider_execution.artifacts import load_yaml
from book_translator.series_terminology.derivation import ChunkText
from book_translator.series_terminology.workflow import (
    execute_book_terminology,
    execute_policy_amendment,
    execute_reference_context,
    plan_book_terminology,
    plan_policy_amendment,
    plan_reference_context,
)
from book_translator.translation_migration import (
    MigrationClass,
    MigrationError,
    migrate_run,
    plan_run_migration,
)
from book_translator.translation_queue import load_run_summary
from book_translator.translation_queue.workflow import create_run, plan_run
from book_translator.translation_repair import TranslationRepairError, repair_translation_job
from book_translator.translation_repair_policy import is_auto_repair_candidate
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import run_translation_worker
from tests.test_series_terminology import BOOK, FORM_AJAH_USER, runner, series_project
from tests.test_terminology_target_forms import _policy, _term
from tests.test_translation_queue import NOW
from tests.test_translation_worker import FailingProvider, SyntheticProvider, _options

# Not a realization of the canonical ("آجای") by morphology alone, so only an amendment admits it.
AUTHORIZED = "آجاها"
UNAUTHORIZED = "انجمن"
CHUNKS = (
    ChunkText("001-001", "001", "The Ajah met.\n\nThey left.\n", ""),
    ChunkText("002-001", "002", "Lanfear laughed.\n\nShe waited.\n", "Ajah\n"),
    ChunkText("003-001", "003", "Thom waited.\n\nThe night was long.\n", ""),
    ChunkText("004-001", "004", "The Ajah spoke.\n\nThey slept.\n", ""),
    ChunkText("005-001", "005", "Elayne rode.\n\nShe stopped.\n", ""),
)
OUTPUTS = {
    "001-001": f"{AUTHORIZED} دیدار کردند.\n\nآن‌ها رفتند.\n",
    "002-001": "لانفیر خندید.\n\nاو منتظر ماند.\n",
    "003-001": "تام منتظر ماند.\n\nشب بلند بود.\n",
    "004-001": f"{UNAUTHORIZED} سخن گفت.\n\nآن‌ها خوابیدند.\n",
}
REPAIRED_004 = f"{FORM_AJAH_USER} سخن گفت.\n\nآن‌ها خوابیدند.\n"


def _run(root: Path) -> str:
    snapshot = create_snapshot(plan_snapshot(project_root=root, book=BOOK), now=lambda: NOW)
    run = create_run(
        plan_run(
            snapshot.path,
            project_root=root,
            output_contract_version=MARKED_OUTPUT_CONTRACT_VERSION,
            now=lambda: NOW,
        )
    )
    execute_prompt_packages(plan_prompt_packages(run.path, project_root=root, now=lambda: NOW))
    return run.run_id


def _provider(job: Any) -> Any:
    if job.chunk_id == "005-001":
        return FailingProvider(retryable=False)
    return SyntheticProvider(mark_paragraphs(OUTPUTS[job.chunk_id]))


def _amend(root: Path) -> None:
    request = SeriesPolicyAmendmentRequest.model_validate(
        {
            "schema_version": "1.0",
            "series_id": "synthetic-series",
            "resulting_policy_version": "1.1.0",
            "rationale": "authorize the plural realization of the Ajah decision",
            "terms": [
                {
                    "source_term": "Ajah",
                    "target_term": FORM_AJAH_USER,
                    "allowed_target_forms": [AUTHORIZED],
                    "rationale": "explicit plural realization",
                }
            ],
        }
    )
    path = root / "amendment.yaml"
    path.write_text(yaml.safe_dump(request.model_dump(mode="json"), allow_unicode=True))
    execute_policy_amendment(plan_policy_amendment(project_root=root, request_path=path, book=BOOK))
    execute_book_terminology(plan_book_terminology(project_root=root, book=BOOK))


def _tree(path: Path) -> dict[str, str]:
    return {
        item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


@pytest.fixture
def migration(tmp_path: Path) -> tuple[Path, str, str]:
    series_project(tmp_path, chunks=CHUNKS)
    execute_book_terminology(plan_book_terminology(project_root=tmp_path, book=BOOK))
    execute_reference_context(plan_reference_context(project_root=tmp_path, book=BOOK))
    old = _run(tmp_path)
    run_translation_worker(
        old,
        project_root=tmp_path,
        options=_options(max_jobs=10),
        provider_factory=_provider,
        now=lambda: NOW + timedelta(seconds=10),
    )
    statuses = {
        job.chunk_id: job.acceptance_status
        for job in load_run_summary(old, project_root=tmp_path).jobs
    }
    assert statuses == {
        "001-001": TranslationAcceptanceStatus.QA_REJECTED,
        "002-001": TranslationAcceptanceStatus.ACCEPTED,
        "003-001": TranslationAcceptanceStatus.ACCEPTED,
        "004-001": TranslationAcceptanceStatus.QA_REJECTED,
        "005-001": None,
    }
    _amend(tmp_path)
    new = _run(tmp_path)
    return tmp_path, old, new


def test_every_job_is_classified_from_evidence(migration: tuple[Path, str, str]) -> None:
    root, old, new = migration

    plan = plan_run_migration(new, from_run=old, project_root=root)

    by_chunk = {item.chunk_id: item for item in plan.jobs}
    assert by_chunk["001-001"].migration_class is MigrationClass.REASSESS_REJECTED
    assert by_chunk["002-001"].migration_class is MigrationClass.REASSESS_ACCEPTED
    assert by_chunk["003-001"].migration_class is MigrationClass.REUSE_EXACT
    assert by_chunk["004-001"].migration_class is MigrationClass.CARRY_FORWARD_FOR_REPAIR
    assert by_chunk["005-001"].migration_class is MigrationClass.FRESH_TRANSLATION
    assert by_chunk["005-001"].reasons == ("historical_job_failed_without_provider_output",)
    reassessment = by_chunk["001-001"].reassessment
    assert reassessment is not None and reassessment.governed_transition_ids
    assert plan.provider_attempts_preserved == 4


def test_apply_publishes_governed_evidence_and_leaves_history_untouched(
    migration: tuple[Path, str, str],
) -> None:
    root, old, new = migration
    old_path = load_run_summary(old, project_root=root).path
    before = _tree(old_path)

    result = migrate_run(new, from_run=old, project_root=root, dry_run=False, now=lambda: NOW)

    assert result.provider_calls == 0
    assert result.published == {
        "carry_forward_for_repair": 1,
        "reassess_accepted_output": 1,
        "reassess_rejected_output": 1,
        "reuse_exact": 1,
    }
    assert _tree(old_path) == before
    state = load_run_summary(new, project_root=root)
    jobs = {job.chunk_id: job for job in state.jobs}
    assert jobs["001-001"].acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert jobs["001-001"].acceptance_origin is TranslationAcceptanceOrigin.HISTORICAL_REEVALUATION
    assert jobs["003-001"].acceptance_origin is TranslationAcceptanceOrigin.REUSE_QA
    assert jobs["004-001"].acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert jobs["005-001"].status is TranslationJobStatus.PENDING
    assert jobs["005-001"].attempt_count == 0
    assert all(job.attempt_count == 0 for job in jobs.values())
    assert state.run.accepted_count == 3 and state.run.qa_rejected_count == 1
    # Acceptance evidence sits inside each carried record; no provider slot is occupied.
    for chunk in ("001-001", "002-001", "003-001", "004-001"):
        assert not (state.path / "jobs" / chunk / "acceptance").exists()
    record = load_yaml(
        root / jobs["001-001"].latest_carry_forward_file,  # type: ignore[operator]
        TranslationHistoricalReevaluationEvidence,
    )
    drift = record.compatibility.terminology_drift
    assert drift is not None and drift.lineage_kind == "series_policy" and drift.explained
    assert (
        record.compatibility.source_manifest_sha256 != record.compatibility.target_manifest_sha256
    )
    assert record.alignment_report is not None and record.alignment_report.valid
    assert record.source_attempt_sha256 and record.source_translation_sha256

    again = migrate_run(new, from_run=old, project_root=root, dry_run=False, now=lambda: NOW)
    assert again.published == {}
    assert len(again.plan.of(MigrationClass.ALREADY_MIGRATED)) == 4


def test_a_carried_rejection_is_repaired_by_an_explicit_operator_repair_only(
    migration: tuple[Path, str, str],
) -> None:
    root, old, new = migration
    migrate_run(new, from_run=old, project_root=root, dry_run=False, now=lambda: NOW)
    job = next(
        job for job in load_run_summary(new, project_root=root).jobs if job.chunk_id == "004-001"
    )
    assert not is_auto_repair_candidate(job)
    calls: list[str] = []

    result = repair_translation_job(
        new,
        job.job_id,
        project_root=root,
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="repair-worker",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        provider_adapter=SyntheticProvider(mark_paragraphs(REPAIRED_004), calls),
        now=lambda: NOW + timedelta(seconds=60),
    )

    assert calls == [job.job_id]
    assert result.repaired_from_attempt is None and result.repaired_from_carry_forward == 1
    assert result.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    (attempt,) = list_attempts(new, job.job_id, project_root=root)
    assert attempt.attempt.execution_kind is TranslationExecutionKind.REPAIR
    assert attempt.attempt.repaired_from_carry_forward == 1
    repaired = next(
        job for job in load_run_summary(new, project_root=root).jobs if job.chunk_id == "004-001"
    )
    assert repaired.accepted_attempt == 1 and repaired.carry_forward_count == 0
    assert repaired.acceptance_file is not None
    assert repaired.acceptance_file.as_posix().endswith("acceptance/001/evidence.yaml")
    # The carried record stays on disk as immutable lineage.
    assert (root / str(job.latest_carry_forward_file)).is_file()


def test_a_decision_changed_between_plan_and_publication_is_refused(
    migration: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, old, new = migration
    from book_translator import translation_historical_reevaluation, translation_migration

    real = translation_historical_reevaluation.plan_historical_output_reevaluation
    calls = {"count": 0}

    def drifting(*args: Any, **kwargs: Any) -> Any:
        plan = real(*args, **kwargs)
        calls["count"] += 1
        # Planning runs once per re-assessed job; every later call is the under-lock re-plan.
        if calls["count"] > 3:
            return replace(plan, projected_status=None)
        return plan

    monkeypatch.setattr(translation_migration, "plan_historical_output_reevaluation", drifting)
    with pytest.raises(MigrationError, match="changed after planning"):
        migrate_run(new, from_run=old, project_root=root, dry_run=False, now=lambda: NOW)


def test_repair_refuses_to_start_from_a_tampered_carried_record(
    migration: tuple[Path, str, str],
) -> None:
    root, old, new = migration
    migrate_run(new, from_run=old, project_root=root, dry_run=False, now=lambda: NOW)
    job = next(
        job for job in load_run_summary(new, project_root=root).jobs if job.chunk_id == "004-001"
    )
    record = root / str(job.latest_carry_forward_file)
    record.write_text(record.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(TranslationRepairError):
        repair_translation_job(
            new,
            job.job_id,
            project_root=root,
            provider=ProviderName.FAKE,
            model="fake-v1",
            worker_id="repair-worker",
            lease_seconds=900,
            timeout_seconds=120,
            max_output_tokens=1000,
            dry_run=True,
        )


def test_cli_dry_run_writes_nothing(
    migration: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, old, new = migration
    new_path = load_run_summary(new, project_root=root).path
    before = _tree(new_path)
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["migrate-translation-run", new, "--from-run", old])

    assert result.exit_code == 0, result.output
    assert "reuse_exact" in result.output and "dry run" in result.output
    assert _tree(new_path) == before


# --- the pure term-level drift classifier --------------------------------------------------------


def _locked(
    source: str, target: str, *, aliases: tuple[str, ...] = (), forms: tuple[str, ...] = ()
) -> Any:
    term = _term(source, target_term=target, forms=forms, kind=SeriesProvenanceKind.EXPLICIT_USER)
    return term.model_copy(update={"aliases": list(aliases)})


PARTICIPLE = "خنثی‌شده"
GERUND = "خنثی شدن"


def test_widened_forms_are_waived() -> None:
    old = _policy(_locked("hushed", PARTICIPLE))
    new = _policy(_locked("hushed", PARTICIPLE, forms=(GERUND,)))

    drift = evaluate_series_term_drift(old, new, source_text="He was hushed.\n", context_text="")

    assert drift.blocking_reasons == () and len(drift.waived_concepts) == 1


def test_a_withdrawn_alias_still_governed_elsewhere_is_waived_and_an_orphaned_one_blocks() -> None:
    old = _policy(
        _locked("hushing", GERUND, aliases=("hushed", "soft")),
        _locked("Hushed", PARTICIPLE, aliases=("hushed",)),
    )
    new = _policy(
        _locked("hushing", GERUND, forms=(PARTICIPLE,)),
        _locked("Hushed", PARTICIPLE, aliases=("hushed",), forms=(GERUND,)),
    )

    governed = evaluate_series_term_drift(old, new, source_text="He was hushed.\n", context_text="")
    orphaned = evaluate_series_term_drift(old, new, source_text="A soft voice.\n", context_text="")
    unreached = evaluate_series_term_drift(old, new, source_text="Nothing here.\n", context_text="")

    assert governed.blocking_reasons == ()
    assert any(
        reason.startswith("series_withdrawn_instruction_reaches_job:")
        for reason in orphaned.blocking_reasons
    )
    assert unreached.blocking_reasons == () and unreached.waived_concepts == ()


@pytest.mark.parametrize(
    ("new_term", "reason"),
    [
        (_locked("hushed", GERUND), "series_decision_changed"),
        (_locked("hushed", PARTICIPLE, aliases=("quiet",)), "series_alias_added_reaches_job"),
        (
            _locked("hushed", PARTICIPLE).model_copy(
                update={"classification": TerminologyType.PERSON}
            ),
            "series_classification_changed",
        ),
    ],
)
def test_changes_that_alter_the_historical_request_block(new_term: Any, reason: str) -> None:
    old = _policy(_locked("hushed", PARTICIPLE, forms=(GERUND,)))
    new = _policy(new_term)

    drift = evaluate_series_term_drift(
        old, new, source_text="quiet, he was hushed.\n", context_text=""
    )

    assert any(item.startswith(reason) for item in drift.blocking_reasons)


def test_withdrawn_forms_and_status_changes_block() -> None:
    old = _policy(_locked("hushed", PARTICIPLE, forms=(GERUND,)))
    narrowed = _policy(_locked("hushed", PARTICIPLE))
    weakened = _policy(
        _locked("hushed", PARTICIPLE, forms=(GERUND,)).model_copy(
            update={"status": SeriesTermStatus.STRONG_REFERENCE}
        )
    )
    text = "He was hushed.\n"

    assert any(
        r.startswith("series_authorized_form_withdrawn")
        for r in evaluate_series_term_drift(
            old, narrowed, source_text=text, context_text=""
        ).blocking_reasons
    )
    assert any(
        r.startswith("series_decision_changed")
        for r in evaluate_series_term_drift(
            old, weakened, source_text=text, context_text=""
        ).blocking_reasons
    )


def test_a_provider_failure_during_a_carried_repair_is_never_replayed_automatically(
    migration: tuple[Path, str, str],
) -> None:
    from book_translator.translation_provider_recovery import (
        is_repair_recovery_candidate,
        plan_provider_recovery,
    )

    root, old, new = migration
    migrate_run(new, from_run=old, project_root=root, dry_run=False, now=lambda: NOW)
    job_id = next(
        job.job_id
        for job in load_run_summary(new, project_root=root).jobs
        if job.chunk_id == "004-001"
    )
    repair_translation_job(
        new,
        job_id,
        project_root=root,
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="repair-worker",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        provider_adapter=FailingProvider(retryable=True),
        now=lambda: NOW + timedelta(seconds=60),
    )
    failed = next(
        job for job in load_run_summary(new, project_root=root).jobs if job.job_id == job_id
    )
    assert failed.status is TranslationJobStatus.FAILED
    # The carried rejection still stands and still names its carry-forward.
    assert failed.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert failed.carry_forward_count == 1
    assert is_repair_recovery_candidate(failed) and not is_auto_repair_candidate(failed)

    plan = plan_provider_recovery(
        new, job_id, project_root=root, now=lambda: NOW + timedelta(hours=1)
    )

    assert plan.repair is None and plan.restored_successful_attempt is None
    assert not plan.recoverable
