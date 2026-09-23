"""Retry semantics for a job that already carries published provider evidence.

A prompt package is immutable input, so a published attempt describes a past provider call and never
makes that input stale. These tests pin the distinction: an unchanged package stays valid for an
explicit retry, real drift still invalidates it, and the guards that actually authorise re-execution
stay where they belong — in the queue and the executor, not in package validation.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from book_translator.execution_resolution import (
    execute_reservation_resolution,
    plan_reservation_resolution,
)
from book_translator.models import (
    ExecutionReservationResolutionKind,
    ProviderName,
    TranslationAcceptanceStatus,
    TranslationAttemptStatus,
    TranslationJob,
    TranslationJobStatus,
)
from book_translator.prompt_packaging import validate_prompt_package
from book_translator.provider_execution import (
    ExecutionPlan,
    ExecutorError,
    execute_translation_job,
    list_attempts,
    plan_execution,
)
from book_translator.translation_queue import (
    QueueError,
    claim_job,
    load_run_summary,
    recover_run,
    retry_job,
)
from book_translator.translation_queue.policy import is_claimable
from book_translator.translation_worker import run_translation_worker
from tests.test_execution_resolution import _ProcessCrash
from tests.test_translation_queue import NOW
from tests.test_translation_worker import (
    FailingProvider,
    SyntheticProvider,
    _options,
    _prepared_run,
    _terminology_run,
)

EXECUTION_NOW = NOW + timedelta(seconds=10)
RETRY_NOW = NOW + timedelta(days=1)
QA_REJECTING_TRANSLATION = "فقط یک بند.\n"
ACCEPTABLE_TRANSLATION = "ترجمه نخست.\n\nترجمه دوم.\n"


def _job(root: Path, run_id: str, job_id: str) -> TranslationJob:
    state = load_run_summary(run_id, project_root=root)
    return next(job for job in state.jobs if job.job_id == job_id)


def _job_directory(root: Path, run_id: str, job_id: str) -> Path:
    state = load_run_summary(run_id, project_root=root)
    return state.path / "jobs" / _job(root, run_id, job_id).chunk_id


def _files(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _claim(root: Path, run_id: str, job_id: str, *, now: Callable[[], datetime], token: str) -> str:
    """Move an eligible job back to a live claim exactly the way the worker does."""
    job = _job(root, run_id, job_id)
    due = now()
    if job.status is TranslationJobStatus.FAILED:
        retry_job(run_id, job_id, project_root=root, reason="operator retry", now=now)
        backoff = load_run_summary(run_id, project_root=root).run.retry_backoff_seconds
        due = due + timedelta(seconds=backoff + 1)
    recover_run(run_id, project_root=root, now=lambda: due)
    return claim_job(
        run_id,
        project_root=root,
        worker_id="worker",
        lease_seconds=900,
        job_id=job_id,
        now=lambda: due,
        token_factory=lambda: token,
    ).claim_token


def _retry_plan(
    root: Path,
    run_id: str,
    job_id: str,
    claim_token: str,
    *,
    now: Callable[[], datetime],
) -> ExecutionPlan:
    return plan_execution(
        run_id,
        job_id,
        project_root=root,
        worker_id="worker",
        claim_token=claim_token,
        provider=ProviderName.FAKE,
        model="fake-v1",
        timeout_seconds=120,
        max_output_tokens=1000,
        now=now,
    )


def _failed_once(root: Path) -> tuple[str, str]:
    """Publish exactly one failed provider attempt, leaving a second job so the run stays active."""
    run_id = _prepared_run(root, chapters=2)
    result = run_translation_worker(
        run_id,
        project_root=root,
        options=_options(),
        provider_factory=lambda _job: FailingProvider(retryable=False),
        now=lambda: EXECUTION_NOW,
    )
    job_id = result.jobs[0].job_id
    assert _job(root, run_id, job_id).status is TranslationJobStatus.FAILED
    attempts = list_attempts(run_id, job_id, project_root=root)
    assert [(item.attempt.attempt_number, item.attempt.status) for item in attempts] == [
        (1, TranslationAttemptStatus.FAILED)
    ]
    return run_id, job_id


def test_published_failed_attempt_keeps_the_prompt_package_valid_for_retry(tmp_path: Path) -> None:
    run_id, job_id = _failed_once(tmp_path)
    attempts_directory = _job_directory(tmp_path, run_id, job_id) / "attempts"
    published = _files(attempts_directory)

    assert published, "the regression only means anything with published attempt evidence"
    assert validate_prompt_package(run_id, job_id, project_root=tmp_path).valid

    token = _claim(tmp_path, run_id, job_id, now=lambda: RETRY_NOW, token="retry-token")
    plan = _retry_plan(tmp_path, run_id, job_id, token, now=lambda: RETRY_NOW)

    assert plan.attempt_directory.name == "002"
    assert plan.request.attempt_number == 2
    assert _files(attempts_directory) == published


def test_qa_rejected_draft_keeps_a_valid_package_while_ordinary_retry_stays_refused(
    tmp_path: Path,
) -> None:
    run_id = _terminology_run(tmp_path)
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider(QA_REJECTING_TRANSLATION),
        now=lambda: EXECUTION_NOW,
    )
    job_id = result.jobs[0].job_id
    job = _job(tmp_path, run_id, job_id)

    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    assert validate_prompt_package(run_id, job_id, project_root=tmp_path).valid

    # Mechanical QA rejection is not a provider failure. The successful draft still blocks ordinary
    # re-translation and repair remains the sanctioned path; package validity never grants either.
    assert not is_claimable(job)
    with pytest.raises(QueueError, match="only a failed job"):
        retry_job(run_id, job_id, project_root=tmp_path, reason="retry", now=lambda: RETRY_NOW)


@pytest.mark.parametrize("target", ["package-message", "chunk-source", "frozen-resource"])
def test_genuine_drift_still_refuses_retry(tmp_path: Path, target: str) -> None:
    run_id, job_id = _failed_once(tmp_path)
    token = _claim(tmp_path, run_id, job_id, now=lambda: RETRY_NOW, token="retry-token")
    state = load_run_summary(run_id, project_root=tmp_path)
    if target == "package-message":
        path = _job_directory(tmp_path, run_id, job_id) / "prompt-package/user.md"
    elif target == "chunk-source":
        path = tmp_path / _job(tmp_path, run_id, job_id).chunk_source_file
    else:
        manifest = yaml.safe_load(
            (tmp_path / state.run.snapshot_path / "manifest.yaml").read_text(encoding="utf-8")
        )
        glossary = manifest["glossary"]
        path = tmp_path / (glossary.get("frozen_file") or glossary["file"])
    path.write_text("drifted synthetic content\n", encoding="utf-8")

    assert not validate_prompt_package(run_id, job_id, project_root=tmp_path).valid
    with pytest.raises(ExecutorError):
        _retry_plan(tmp_path, run_id, job_id, token, now=lambda: RETRY_NOW)


def test_accepted_translation_refuses_every_unsafe_retry(tmp_path: Path) -> None:
    run_id = _prepared_run(tmp_path, chapters=2)
    result = run_translation_worker(
        run_id,
        project_root=tmp_path,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider(ACCEPTABLE_TRANSLATION),
        now=lambda: EXECUTION_NOW,
    )
    job_id = result.jobs[0].job_id
    job = _job(tmp_path, run_id, job_id)
    before = _files(_job_directory(tmp_path, run_id, job_id))

    assert job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert not is_claimable(job)
    with pytest.raises(QueueError, match="only a failed job"):
        retry_job(run_id, job_id, project_root=tmp_path, reason="retry", now=lambda: RETRY_NOW)
    with pytest.raises(QueueError):
        _claim(tmp_path, run_id, job_id, now=lambda: RETRY_NOW, token="retry-token")
    assert _files(_job_directory(tmp_path, run_id, job_id)) == before


def test_abandoned_reservation_and_published_failure_retry_into_a_fresh_slot(
    tmp_path: Path,
) -> None:
    """Reproduce the published shape: retired slot 001, failed attempt 002, retry must reach 003."""
    run_id = _prepared_run(tmp_path, chapters=2)
    state = load_run_summary(run_id, project_root=tmp_path)
    job_id = state.jobs[0].job_id
    first_token = _claim(tmp_path, run_id, job_id, now=lambda: NOW, token="first-token")
    stranded = _retry_plan(tmp_path, run_id, job_id, first_token, now=lambda: EXECUTION_NOW)
    with pytest.raises(KeyboardInterrupt):
        execute_translation_job(
            stranded,
            worker_id="worker",
            claim_token=first_token,
            provider=_ProcessCrash(),
            now=lambda: EXECUTION_NOW,
        )

    resolved_at = NOW + timedelta(hours=2)
    resolution = plan_reservation_resolution(
        run_id,
        job_id,
        attempt_number=1,
        resolution=ExecutionReservationResolutionKind.PROVIDER_NOT_CONTACTED,
        reason="synthetic worker died before any provider request could be issued",
        project_root=tmp_path,
        now=lambda: resolved_at,
    )
    execute_reservation_resolution(resolution, project_root=tmp_path, now=lambda: resolved_at)
    preserved_reservation = stranded.reservation_path.read_bytes()
    resolution_record = (tmp_path / resolution.resolution_file).read_bytes()

    second_token = _claim(tmp_path, run_id, job_id, now=lambda: RETRY_NOW, token="second-token")
    second = _retry_plan(tmp_path, run_id, job_id, second_token, now=lambda: RETRY_NOW)
    assert second.attempt_directory.name == "002"
    execute_translation_job(
        second,
        worker_id="worker",
        claim_token=second_token,
        provider=FailingProvider(retryable=False),
        now=lambda: RETRY_NOW,
    )

    job = _job(tmp_path, run_id, job_id)
    assert job.status is TranslationJobStatus.FAILED
    assert (job.attempt_count, job.abandoned_attempt_count) == (2, 1)
    assert validate_prompt_package(run_id, job_id, project_root=tmp_path).valid

    later = RETRY_NOW + timedelta(days=1)
    third_token = _claim(tmp_path, run_id, job_id, now=lambda: later, token="third-token")
    third = _retry_plan(tmp_path, run_id, job_id, third_token, now=lambda: later)

    assert third.attempt_directory.name == "003"
    assert stranded.reservation_path.read_bytes() == preserved_reservation
    assert (tmp_path / resolution.resolution_file).read_bytes() == resolution_record
    assert (_job_directory(tmp_path, run_id, job_id) / "attempts/002/attempt.yaml").is_file()
