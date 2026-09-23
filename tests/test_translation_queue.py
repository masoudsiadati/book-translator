from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import (
    TranslationJob,
    TranslationJobStatus,
    TranslationRun,
    TranslationRunStatus,
)
from book_translator.translation_queue import (
    QueueError,
    claim_job,
    create_run,
    list_jobs,
    list_runs,
    load_run_summary,
    plan_run,
    recover_run,
    release_job,
    retry_job,
)
from book_translator.translation_queue.policy import (
    can_transition_translation_job,
    can_transition_translation_run,
    claim_ineligibility_reason,
    claim_priority_key,
    derive_run_status,
    is_claimable,
    recompute_counters,
)
from tests.test_translation_snapshot import synthetic_project

NOW = datetime(2026, 2, 3, 4, 5, tzinfo=UTC)
SNAPSHOT_NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
runner = CliRunner()


def _snapshot(root: Path, *, chapters: int = 1) -> str:
    from book_translator.translation_snapshot import create_snapshot, plan_snapshot

    synthetic_project(root, chapters=chapters)
    result = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    return result.snapshot_id


def _run(root: Path, *, chapters: int = 1, max_attempts: int = 3) -> tuple[str, Path]:
    snapshot_id = _snapshot(root, chapters=chapters)
    plan = plan_run(
        snapshot_id,
        project_root=root,
        max_attempts=max_attempts,
        now=lambda: NOW,
    )
    result = create_run(plan)
    return result.run_id, result.path


def _write_model(path: Path, model: object) -> None:
    value = model.model_dump(mode="json", exclude_none=False)  # type: ignore[attr-defined]
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _set_job(
    run_path: Path,
    job: TranslationJob,
    *,
    status: TranslationJobStatus,
    attempt_count: int,
) -> TranslationJob:
    changed = job.model_copy(
        update={
            "status": status,
            "attempt_count": attempt_count,
            "claimed_by": None,
            "claim_token": None,
            "claimed_at": None,
            "lease_expires_at": None,
            "next_retry_at": None,
            "updated_at": NOW,
        }
    )
    _write_model(run_path / "jobs" / job.chunk_id / "job.yaml", changed)
    return changed


def test_run_plan_has_deterministic_ids_order_and_one_job_per_chunk(tmp_path: Path) -> None:
    snapshot_id = _snapshot(tmp_path, chapters=2)
    plan = plan_run(snapshot_id, project_root=tmp_path, now=lambda: NOW)

    assert plan.run.run_id.endswith("-001")
    assert [job.chapter_id for job in plan.jobs] == ["001", "002"]
    assert [job.priority for job in plan.jobs] == [1, 2]
    assert all(job.job_id == f"{plan.run.run_id}__{job.chunk_id}" for job in plan.jobs)
    assert len(plan.jobs) == len({job.chunk_id for job in plan.jobs}) == 2


def test_run_id_sequence_advances_and_dry_plan_does_not_reserve(tmp_path: Path) -> None:
    snapshot_id = _snapshot(tmp_path)
    first = plan_run(snapshot_id, project_root=tmp_path, now=lambda: NOW)
    same = plan_run(snapshot_id, project_root=tmp_path, now=lambda: NOW)
    assert first.run.run_id == same.run.run_id
    create_run(first)
    second = plan_run(snapshot_id, project_root=tmp_path, now=lambda: NOW)
    assert second.run.run_id.endswith("-002")


def test_run_publication_is_ready_atomic_and_all_jobs_pending(tmp_path: Path) -> None:
    snapshot_id = _snapshot(tmp_path, chapters=2)
    plan = plan_run(snapshot_id, project_root=tmp_path, now=lambda: NOW)
    result = create_run(plan)
    state = load_run_summary(result.path, project_root=tmp_path)

    assert state.validation.valid
    assert state.run.status is TranslationRunStatus.READY
    assert state.run.pending_count == state.run.job_count == 2
    assert all(job.status is TranslationJobStatus.PENDING for job in state.jobs)
    assert {path.name for path in result.path.iterdir() if not path.name.startswith(".")} == {
        "run.yaml",
        "queue-report.yaml",
        "jobs",
    }


def test_run_dry_plan_writes_nothing(tmp_path: Path) -> None:
    snapshot_id = _snapshot(tmp_path)
    plan = plan_run(snapshot_id, project_root=tmp_path, now=lambda: NOW)

    assert not plan.output_root.exists()


def test_atomic_publication_failure_exposes_no_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_id = _snapshot(tmp_path)
    plan = plan_run(snapshot_id, project_root=tmp_path, now=lambda: NOW)

    def fail_rename(_source: Path, _destination: Path) -> None:
        raise OSError("injected publication failure")

    monkeypatch.setattr("book_translator.translation_queue.workflow.os.rename", fail_rename)
    with pytest.raises(QueueError, match="publication failed"):
        create_run(plan)
    assert not plan.output_directory.exists()


def test_invalid_or_nonfrozen_snapshot_is_refused(tmp_path: Path) -> None:
    snapshot_id = _snapshot(tmp_path)
    snapshot_path = tmp_path / f"workspace/snapshots/translation/{snapshot_id}/snapshot.yaml"
    value = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
    value["status"] = "validated"
    snapshot_path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    with pytest.raises(QueueError, match="not current, valid, and frozen"):
        plan_run(snapshot_id, project_root=tmp_path)


def test_stale_snapshot_is_refused(tmp_path: Path) -> None:
    snapshot_id = _snapshot(tmp_path)
    (tmp_path / "workspace/chapters/001/chunks/001/context.md").write_text(
        "changed\n", encoding="utf-8"
    )

    with pytest.raises(QueueError, match="not current, valid, and frozen"):
        plan_run(snapshot_id, project_root=tmp_path)


def test_snapshot_lineage_drift_blocks_claim_without_rewriting_historical_run(
    tmp_path: Path,
) -> None:
    run_id, _ = _run(tmp_path)
    (tmp_path / "workspace/chapters/001/chunks/001/context.md").write_text(
        "changed\n", encoding="utf-8"
    )

    with pytest.raises(QueueError, match="historical rather than current"):
        claim_job(run_id, project_root=tmp_path, worker_id="worker", now=lambda: NOW)

    run_path = tmp_path / f"workspace/translation-runs/{run_id}/run.yaml"
    run = TranslationRun.model_validate(yaml.safe_load(run_path.read_text()))
    assert run.status is TranslationRunStatus.READY
    state = load_run_summary(run_id, project_root=tmp_path)
    assert state.validation.valid
    assert not state.snapshot_valid


def test_claim_selects_lowest_priority_and_increments_attempt(tmp_path: Path) -> None:
    run_id, _ = _run(tmp_path, chapters=2)
    claimed = claim_job(
        run_id,
        project_root=tmp_path,
        worker_id="worker-a",
        now=lambda: NOW,
        token_factory=lambda: "secret-token",
    )
    state = load_run_summary(run_id, project_root=tmp_path)

    assert claimed.job.priority == 1
    assert claimed.job.attempt_count == 1
    assert claimed.claim_token == "secret-token"
    assert claimed.job.status is TranslationJobStatus.CLAIMED
    assert state.run.status is TranslationRunStatus.ACTIVE
    assert state.run.claimed_count == 1
    assert state.run.pending_count == 1


def test_explicit_and_chapter_filtered_claims(tmp_path: Path) -> None:
    run_id, _ = _run(tmp_path, chapters=2)
    jobs = list_jobs(run_id, project_root=tmp_path)
    explicit = claim_job(
        run_id,
        project_root=tmp_path,
        worker_id="one",
        job_id=jobs[1].job_id,
        now=lambda: NOW,
    )
    assert explicit.job.chapter_id == "002"
    chapter = claim_job(
        run_id,
        project_root=tmp_path,
        worker_id="two",
        chapter_id="001",
        now=lambda: NOW,
    )
    assert chapter.job.chapter_id == "001"


def test_two_workers_cannot_claim_same_job(tmp_path: Path) -> None:
    run_id, _ = _run(tmp_path)

    def attempt(worker: str) -> str:
        try:
            return claim_job(
                run_id, project_root=tmp_path, worker_id=worker, now=lambda: NOW
            ).job.job_id
        except QueueError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("worker-a", "worker-b")))

    assert sum(result != "refused" for result in results) == 1
    state = load_run_summary(run_id, project_root=tmp_path)
    assert state.run.claimed_count == 1


def test_nonexpired_claim_is_not_stolen(tmp_path: Path) -> None:
    run_id, _ = _run(tmp_path)
    first = claim_job(run_id, project_root=tmp_path, worker_id="one", now=lambda: NOW)

    with pytest.raises(QueueError, match="no eligible"):
        claim_job(
            run_id,
            project_root=tmp_path,
            worker_id="two",
            now=lambda: NOW + timedelta(seconds=10),
        )
    assert load_run_summary(run_id, project_root=tmp_path).jobs[0].job_id == first.job.job_id


def test_release_requires_owner_and_preserves_attempt(tmp_path: Path) -> None:
    run_id, _ = _run(tmp_path)
    claimed = claim_job(run_id, project_root=tmp_path, worker_id="owner", now=lambda: NOW)
    with pytest.raises(QueueError, match="does not own"):
        release_job(
            run_id,
            claimed.job.job_id,
            project_root=tmp_path,
            worker_id="other",
            now=lambda: NOW,
        )
    released = release_job(
        run_id,
        claimed.job.job_id,
        project_root=tmp_path,
        worker_id="owner",
        reason="administrative release",
        now=lambda: NOW,
    )
    assert released.status is TranslationJobStatus.PENDING
    assert released.attempt_count == 1
    assert released.claim_token is None


def test_expired_lease_recovery_and_dry_run(tmp_path: Path) -> None:
    run_id, _ = _run(tmp_path)
    claimed = claim_job(
        run_id,
        project_root=tmp_path,
        worker_id="worker",
        lease_seconds=10,
        now=lambda: NOW,
    )
    dry = recover_run(
        run_id,
        project_root=tmp_path,
        now=lambda: NOW + timedelta(seconds=11),
        dry_run=True,
    )
    assert dry.expired_claims == (claimed.job.job_id,)
    assert (
        load_run_summary(run_id, project_root=tmp_path).jobs[0].status
        is TranslationJobStatus.CLAIMED
    )
    recovered = recover_run(run_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=11))
    job = load_run_summary(run_id, project_root=tmp_path).jobs[0]
    assert recovered.expired_claims == (claimed.job.job_id,)
    assert job.status is TranslationJobStatus.PENDING
    assert job.attempt_count == 1
    assert job.claim_token is None


def test_retry_backoff_due_and_exhaustion(tmp_path: Path) -> None:
    run_id, run_path = _run(tmp_path, max_attempts=2)
    state = load_run_summary(run_id, project_root=tmp_path)
    failed = _set_job(
        run_path,
        state.jobs[0],
        status=TranslationJobStatus.FAILED,
        attempt_count=1,
    )
    scheduled = retry_job(
        run_id,
        failed.job_id,
        project_root=tmp_path,
        reason="synthetic failure",
        now=lambda: NOW,
    )
    assert scheduled.next_retry_at == NOW + timedelta(seconds=60)
    early = recover_run(run_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=59))
    assert early.due_retries == ()
    due = recover_run(run_id, project_root=tmp_path, now=lambda: NOW + timedelta(seconds=60))
    assert due.due_retries == (failed.job_id,)
    current = load_run_summary(run_id, project_root=tmp_path).jobs[0]
    exhausted = _set_job(
        run_path,
        current,
        status=TranslationJobStatus.FAILED,
        attempt_count=2,
    )
    with pytest.raises(QueueError, match="exhausted"):
        retry_job(
            run_id,
            exhausted.job_id,
            project_root=tmp_path,
            reason="again",
            now=lambda: NOW,
        )


def test_maximum_attempt_pending_job_cannot_be_claimed(tmp_path: Path) -> None:
    run_id, run_path = _run(tmp_path, max_attempts=1)
    state = load_run_summary(run_id, project_root=tmp_path)
    _set_job(
        run_path,
        state.jobs[0],
        status=TranslationJobStatus.PENDING,
        attempt_count=1,
    )
    with pytest.raises(QueueError, match="no eligible"):
        claim_job(run_id, project_root=tmp_path, worker_id="worker", now=lambda: NOW)


def test_counter_mismatch_detected_and_repaired(tmp_path: Path) -> None:
    run_id, run_path = _run(tmp_path)
    run_path_file = run_path / "run.yaml"
    run = TranslationRun.model_validate(yaml.safe_load(run_path_file.read_text()))
    corrupted = run.model_copy(update={"pending_count": 0})
    _write_model(run_path_file, corrupted)

    state = load_run_summary(run_id, project_root=tmp_path)
    assert state.validation.counter_mismatch
    dry = recover_run(run_id, project_root=tmp_path, now=lambda: NOW, dry_run=True)
    assert dry.counter_repair
    assert (
        TranslationRun.model_validate(yaml.safe_load(run_path_file.read_text())).pending_count == 0
    )
    recover_run(run_id, project_root=tmp_path, now=lambda: NOW)
    assert load_run_summary(run_id, project_root=tmp_path).validation.valid


def test_structural_corruption_blocks_recovery(tmp_path: Path) -> None:
    run_id, run_path = _run(tmp_path)
    (run_path / "jobs/001-001/job.yaml").unlink()

    result = recover_run(run_id, project_root=tmp_path, now=lambda: NOW)

    assert result.blockers


def test_extra_job_directory_is_structural_corruption(tmp_path: Path) -> None:
    run_id, run_path = _run(tmp_path)
    extra = run_path / "jobs/extra"
    extra.mkdir()

    with pytest.raises(QueueError, match="coverage"):
        load_run_summary(run_id, project_root=tmp_path)


def test_pure_counter_status_and_transition_policy(tmp_path: Path) -> None:
    run_id, _ = _run(tmp_path)
    state = load_run_summary(run_id, project_root=tmp_path)
    assert recompute_counters(state.jobs).pending == 1
    assert derive_run_status(state.jobs, TranslationRunStatus.CREATED) is TranslationRunStatus.READY
    assert can_transition_translation_run(TranslationRunStatus.CREATED, TranslationRunStatus.READY)
    assert can_transition_translation_job(
        TranslationJobStatus.PENDING, TranslationJobStatus.CLAIMED
    )
    assert not can_transition_translation_job(
        TranslationJobStatus.PENDING, TranslationJobStatus.SUCCEEDED
    )
    succeeded = state.jobs[0].model_copy(
        update={"status": TranslationJobStatus.SUCCEEDED, "completed_at": NOW}
    )
    assert (
        derive_run_status((succeeded,), TranslationRunStatus.ACTIVE) is TranslationRunStatus.ACTIVE
    )
    failed = state.jobs[0].model_copy(
        update={
            "status": TranslationJobStatus.FAILED,
            "attempt_count": state.jobs[0].max_attempts,
        }
    )
    assert (
        derive_run_status((failed,), TranslationRunStatus.ACTIVE)
        is TranslationRunStatus.COMPLETED_WITH_FAILURES
    )


def test_pure_claim_eligibility_and_ordering_primitives(tmp_path: Path) -> None:
    run_id, _ = _run(tmp_path, chapters=2)
    jobs = list_jobs(run_id, project_root=tmp_path)

    assert all(is_claimable(job) for job in jobs)
    assert all(claim_ineligibility_reason(job) is None for job in jobs)
    assert [job.job_id for job in sorted(reversed(jobs), key=claim_priority_key)] == [
        job.job_id for job in jobs
    ]
    blocked = jobs[0].model_copy(update={"status": TranslationJobStatus.BLOCKED})
    exhausted = jobs[0].model_copy(update={"attempt_count": jobs[0].max_attempts})
    assert not is_claimable(blocked) and not is_claimable(exhausted)
    assert claim_ineligibility_reason(blocked) == "job status is blocked"
    assert claim_ineligibility_reason(exhausted) == "attempt budget is exhausted (3/3)"


def test_list_empty_root_and_job_filters(tmp_path: Path) -> None:
    assert list_runs(project_root=tmp_path) == ()
    run_id, _ = _run(tmp_path, chapters=2)
    assert len(list_runs(project_root=tmp_path)) == 1
    assert len(list_jobs(run_id, project_root=tmp_path, chapter_id="002")) == 1
    assert len(list_jobs(run_id, project_root=tmp_path, limit=1)) == 1


def test_global_status_reports_queue_without_mutating_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    progress = tmp_path / "state/progress.json"
    run_id, _ = _run(tmp_path)
    before = progress.read_bytes()
    claim_job(run_id, project_root=tmp_path, worker_id="worker", now=lambda: NOW)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Translation runs" in result.stdout
    assert "Claimed queue jobs" in result.stdout
    assert progress.read_bytes() == before


def test_cli_plan_list_show_and_token_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_id = _snapshot(tmp_path)
    monkeypatch.chdir(tmp_path)
    dry = runner.invoke(app, ["create-translation-run", snapshot_id, "--dry-run", "--show-plan"])
    assert dry.exit_code == 0
    assert "Alpha beta" not in dry.stdout
    created = runner.invoke(app, ["create-translation-run", snapshot_id])
    assert created.exit_code == 0
    run_id = plan_run(snapshot_id, project_root=tmp_path, now=lambda: NOW).run.run_id
    first_run_id = run_id.removesuffix("002") + "001"
    claimed = claim_job(
        first_run_id,
        project_root=tmp_path,
        worker_id="worker",
        now=lambda: NOW,
        token_factory=lambda: "PRIVATE-CLAIM-TOKEN",
    )
    listed = runner.invoke(app, ["list-translation-jobs", first_run_id])
    shown = runner.invoke(app, ["show-translation-run", first_run_id])
    runs = runner.invoke(app, ["list-translation-runs"])
    assert claimed.claim_token not in listed.stdout + shown.stdout + runs.stdout
    assert "Alpha beta" not in listed.stdout + shown.stdout + runs.stdout


def test_existing_commands_remain_registered() -> None:
    result = runner.invoke(app, ["--help"])
    for command in {
        "create-translation-run",
        "list-translation-runs",
        "show-translation-run",
        "list-translation-jobs",
        "claim-translation-job",
        "release-translation-job",
        "retry-translation-job",
        "recover-translation-run",
        "create-translation-snapshot",
        "chunk-chapters",
    }:
        assert command in result.stdout
