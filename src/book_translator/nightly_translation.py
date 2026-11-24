"""Bounded, resumable orchestration over the canonical translation worker."""

from __future__ import annotations

import re
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from datetime import time as wall_time
from pathlib import Path

import yaml

from book_translator.atomic_write import atomic_write_text
from book_translator.config import load_book_config
from book_translator.hashing import sha256_file
from book_translator.models import (
    ProviderName,
    TranslationAcceptanceStatus,
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationJob,
    TranslationJobStatus,
)
from book_translator.models.nightly_translation import NightlyTranslationCheckpoint
from book_translator.paths import ProjectPaths
from book_translator.provider_execution.artifacts import load_yaml
from book_translator.translation_queue import is_claimable, load_run_summary, resolve_run_book
from book_translator.translation_queue.locking import queue_lock
from book_translator.translation_queue.workflow import QueueState
from book_translator.translation_worker import (
    WorkerOptions,
    WorkerResult,
    generated_worker_id,
    run_translation_worker,
)

WINDOW_START = wall_time(22, 0)
WINDOW_END = wall_time(5, 0)
BACKOFF_SECONDS = (60, 300, 900, 1800)
TRANSIENT_CATEGORIES = {
    "rate_limit",
    "timeout_pre_send",
    "timeout_ambiguous",
    "connection_pre_send",
    "connection_ambiguous",
    "timeout",
    "server_error",
    "nonzero_exit",
}


class NightlyTranslationError(ValueError):
    """The coordinator cannot proceed without violating its safety policy."""


def within_nightly_window(value: datetime) -> bool:
    if value.tzinfo is None:
        raise NightlyTranslationError("the machine-local clock must be timezone-aware")
    current = value.timetz().replace(tzinfo=None)
    return current >= WINDOW_START or current < WINDOW_END


def window_close(value: datetime) -> datetime:
    """Return the next 05:00 boundary for an aware local timestamp."""
    if not within_nightly_window(value):
        raise NightlyTranslationError("current local time is outside the nightly window")
    close_date = (
        value.date() + timedelta(days=1)
        if value.timetz().replace(tzinfo=None) >= WINDOW_START
        else value.date()
    )
    return datetime.combine(close_date, WINDOW_END, tzinfo=value.tzinfo)


def _attempt(project_root: Path, run_id: str, job: TranslationJob) -> TranslationAttempt | None:
    if job.successful_attempt is None:
        return None
    path = (
        ProjectPaths(project_root).translation_attempt(run_id, job.chunk_id, job.successful_attempt)
        / "attempt.yaml"
    )
    attempt = load_yaml(path, TranslationAttempt)
    if (
        attempt.status is not TranslationAttemptStatus.SUCCEEDED
        or attempt.translation_file is None
        or attempt.translation_sha256 is None
    ):
        return None
    translated = project_root / attempt.translation_file
    if not translated.is_file() or sha256_file(translated) != attempt.translation_sha256:
        raise NightlyTranslationError(
            f"usable translation artifact is missing or stale for {job.chunk_id}"
        )
    return attempt


def _coverage(
    project_root: Path, run_id: str, jobs: tuple[TranslationJob, ...]
) -> dict[str, TranslationAttempt]:
    usable: dict[str, TranslationAttempt] = {}
    for job in jobs:
        attempt = _attempt(project_root, run_id, job)
        if attempt is not None:
            usable[job.job_id] = attempt
    return usable


def _ordered_units(jobs: tuple[TranslationJob, ...]) -> list[tuple[str, list[TranslationJob]]]:
    grouped: dict[str, list[TranslationJob]] = defaultdict(list)
    for job in sorted(jobs, key=lambda item: (item.chapter_sequence, item.chunk_sequence)):
        grouped[job.chapter_id].append(job)
    return list(grouped.items())


def _checkpoint(
    state: QueueState,
    *,
    project_root: Path,
    provider_state: str,
    next_safe_action: str | None,
    now: datetime,
    draft_path: Path | None = None,
) -> NightlyTranslationCheckpoint:
    jobs = state.jobs
    usable = _coverage(project_root, state.run.run_id, jobs)
    units = _ordered_units(jobs)
    complete = [
        unit for unit, members in units if all(member.job_id in usable for member in members)
    ]
    next_unit = next(
        (
            unit
            for unit, members in units
            if any(member.job_id not in usable and is_claimable(member) for member in members)
        ),
        None,
    )
    accepted = sum(
        job.job_id in usable and job.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
        for job in jobs
    )
    rejected = sum(
        job.job_id in usable and job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
        for job in jobs
    )
    failed = sum(
        job.status in {TranslationJobStatus.FAILED, TranslationJobStatus.RETRY_PENDING}
        and job.job_id not in usable
        for job in jobs
    )
    if next_safe_action is None:
        next_safe_action = (
            "assemble complete-coverage draft"
            if len(usable) == len(jobs)
            else "start earliest pending source-order unit"
        )
    return NightlyTranslationCheckpoint(
        snapshot_id=state.run.snapshot_id,
        translation_run_id=state.run.run_id,
        total_units=len(units),
        total_chunks=len(jobs),
        units_with_complete_translated_coverage=complete,
        translated_chunks_count=len(usable),
        accepted_count=accepted,
        qa_rejected_count=rejected,
        missing_translation_count=len(jobs) - len(usable),
        failed_retry_pending_count=failed,
        pending_count=sum(is_claimable(job) for job in jobs),
        last_completed_unit=complete[-1] if complete else None,
        next_unit=next_unit,
        last_provider_state=provider_state,
        next_safe_action=next_safe_action,
        draft_path=draft_path,
        updated_at=now,
    )


def _write_checkpoint(path: Path, checkpoint: NightlyTranslationCheckpoint) -> None:
    rendered = yaml.safe_dump(
        checkpoint.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
    )
    atomic_write_text(path, rendered, overwrite=True)


def _build_draft(project_root: Path, state: QueueState) -> Path:
    usable = _coverage(project_root, state.run.run_id, state.jobs)
    if len(usable) != len(state.jobs):
        raise NightlyTranslationError("draft assembly requires complete translated coverage")
    destination = state.path / "coverage-draft.md"
    parts: list[str] = []
    for unit, members in _ordered_units(state.jobs):
        parts.append(f"# {unit}\n")
        for job in members:
            attempt = usable[job.job_id]
            assert attempt.translation_file is not None
            parts.append(
                (project_root / attempt.translation_file).read_text(encoding="utf-8").strip()
            )
        parts.append("")
    atomic_write_text(destination, "\n\n".join(parts).rstrip() + "\n", overwrite=True)
    return Path(destination.relative_to(project_root).as_posix())


def _retry_delay(error: str | None, attempt_index: int) -> int:
    if error:
        match = re.search(r"retry(?:-| )after[^0-9]*(\d+)", error, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return BACKOFF_SECONDS[min(attempt_index, len(BACKOFF_SECONDS) - 1)]


def run_nightly_translation(
    run_id: str,
    *,
    project_root: Path,
    max_units: int = 3,
    clock: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    worker: Callable[..., WorkerResult] = run_translation_worker,
) -> NightlyTranslationCheckpoint:
    """Process at most `max_units` in one local-window invocation, then exit."""
    if max_units < 1:
        raise NightlyTranslationError("max_units must be at least one")
    project_root = project_root.resolve()
    clock = clock or (lambda: datetime.now().astimezone())
    started = clock()
    if not within_nightly_window(started):
        raise NightlyTranslationError(
            "provider work is allowed only from 22:00 through 04:59 local time"
        )
    state = load_run_summary(run_id, project_root=project_root)
    config = load_book_config(
        ProjectPaths(project_root, resolve_run_book(run_id, project_root=project_root)).book_config
    )
    if set(config.translation_provider_models) != {ProviderName.CODEX_CLI.value}:
        raise NightlyTranslationError("nightly books must allow only the codex-cli provider")
    checkpoint_path = state.path / "nightly-checkpoint.yaml"
    with queue_lock(state.path / ".nightly.lock", timeout_seconds=0):
        state = load_run_summary(run_id, project_root=project_root)
        initial = _checkpoint(
            state,
            project_root=project_root,
            provider_state="not_contacted",
            next_safe_action=None,
            now=started,
        )
        _write_checkpoint(checkpoint_path, initial)
        if initial.translated_chunks_count == initial.total_chunks:
            draft = _build_draft(project_root, state)
            complete = _checkpoint(
                state,
                project_root=project_root,
                provider_state="coverage_complete",
                next_safe_action="stop; user decides QA cleanup strategy",
                now=clock(),
                draft_path=draft,
            )
            _write_checkpoint(checkpoint_path, complete)
            return complete

        touched: set[str] = set()
        transient_index = 0
        while within_nightly_window(clock()):
            state = load_run_summary(run_id, project_root=project_root)
            usable = _coverage(project_root, run_id, state.jobs)
            candidate = next(
                (
                    (unit, job)
                    for unit, members in _ordered_units(state.jobs)
                    for job in members
                    if job.job_id not in usable
                    and is_claimable(job)
                    and (unit in touched or len(touched) < max_units)
                ),
                None,
            )
            if candidate is None:
                break
            unit, job = candidate
            touched.add(unit)
            before = _checkpoint(
                state,
                project_root=project_root,
                provider_state="ready",
                next_safe_action=f"execute {job.chunk_id}",
                now=clock(),
            )
            _write_checkpoint(checkpoint_path, before)
            result = worker(
                run_id,
                project_root=project_root,
                options=WorkerOptions(
                    provider=ProviderName.CODEX_CLI,
                    model=None,
                    worker_id=generated_worker_id(),
                    max_jobs=1,
                    workers=1,
                    lease_seconds=900,
                    timeout_seconds=config.provider_timeout_seconds,
                    max_output_tokens=config.provider_max_output_tokens,
                    store_raw_response=config.provider_store_raw_response,
                    retry_provider_failures=False,
                    stop_on_error=True,
                    job_id=job.job_id,
                    auto_repair=False,
                    include_qa_rejected=False,
                    auto_recover_provider_failures=False,
                    include_failed=False,
                ),
            )
            latest = result.jobs[-1] if result.jobs else None
            if latest is not None and latest.provider_failure_class in TRANSIENT_CATEGORIES:
                delay = _retry_delay(latest.error, transient_index)
                transient_index += 1
                remaining = (window_close(clock()) - clock()).total_seconds()
                if delay >= remaining:
                    break
                sleeper(delay)

        state = load_run_summary(run_id, project_root=project_root)
        usable = _coverage(project_root, run_id, state.jobs)
        final_draft = _build_draft(project_root, state) if len(usable) == len(state.jobs) else None
        final = _checkpoint(
            state,
            project_root=project_root,
            provider_state="coverage_complete" if final_draft else "stopped_cleanly",
            next_safe_action=(
                "stop; user decides QA cleanup strategy"
                if final_draft
                else "resume earliest claimable untranslated chunk in next nightly window"
            ),
            now=clock(),
            draft_path=final_draft,
        )
        _write_checkpoint(checkpoint_path, final)
        return final
