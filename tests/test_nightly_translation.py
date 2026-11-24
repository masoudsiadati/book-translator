from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from typer.testing import CliRunner

from book_translator import nightly_translation
from book_translator.cli import app
from book_translator.models import (
    TranslationAcceptanceStatus,
    TranslationJob,
    TranslationJobStatus,
)
from book_translator.models.nightly_translation import NightlyTranslationCheckpoint
from book_translator.nightly_translation import (
    NightlyTranslationError,
    _retry_delay,
    window_close,
    within_nightly_window,
)
from book_translator.translation_queue.locking import QueueLockTimeout, queue_lock

LOCAL = ZoneInfo("Asia/Tehran")
NOW = datetime(2026, 11, 7, 23, 0, tzinfo=LOCAL)
RUN_ID = "tr-0123456789ab-001"
runner = CliRunner()


def _pending_job(unit: int, chunk: int = 1, *, covered_rejection: bool = False) -> TranslationJob:
    chapter_id = f"{unit:03d}"
    return TranslationJob.model_construct(
        job_id=f"{RUN_ID}__{chapter_id}-{chunk:03d}",
        run_id=RUN_ID,
        chapter_id=chapter_id,
        chapter_sequence=unit,
        chunk_id=f"{chapter_id}-{chunk:03d}",
        chunk_sequence=chunk,
        status=TranslationJobStatus.PENDING,
        priority=unit,
        attempt_count=0,
        abandoned_attempt_count=0,
        max_attempts=3,
        acceptance_status=(TranslationAcceptanceStatus.QA_REJECTED if covered_rejection else None),
    )


def _synthetic_coordinator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[SimpleNamespace, list[Any], set[str], Any]:
    jobs = (
        _pending_job(1, covered_rejection=True),
        _pending_job(2),
        _pending_job(2, 2),
        _pending_job(3),
        _pending_job(4),
        _pending_job(5),
    )
    state = SimpleNamespace(
        path=tmp_path / "synthetic-run",
        run=SimpleNamespace(run_id=RUN_ID, snapshot_id="ts-synthetic"),
        jobs=jobs,
    )
    covered = {jobs[0].job_id}
    worker_options: list[Any] = []

    monkeypatch.setattr(nightly_translation, "load_run_summary", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(nightly_translation, "resolve_run_book", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        nightly_translation,
        "load_book_config",
        lambda _path: SimpleNamespace(
            translation_provider_models={"codex-cli": []},
            provider_timeout_seconds=600,
            provider_max_output_tokens=12000,
            provider_store_raw_response=False,
        ),
    )
    monkeypatch.setattr(
        nightly_translation,
        "_coverage",
        lambda _root, _run_id, members: {
            job.job_id: object() for job in members if job.job_id in covered
        },
    )

    def worker(_run_id: str, *, options: Any, **_kwargs: Any) -> SimpleNamespace:
        worker_options.append(options)
        assert options.job_id is not None
        covered.add(options.job_id)
        return SimpleNamespace(jobs=())

    return state, worker_options, covered, worker


@pytest.mark.parametrize(
    ("hour", "minute", "allowed"),
    [(21, 59, False), (22, 0, True), (4, 59, True), (5, 0, False)],
)
def test_nightly_window_uses_inclusive_start_and_exclusive_end(
    hour: int, minute: int, allowed: bool
) -> None:
    assert within_nightly_window(datetime(2026, 11, 7, hour, minute, tzinfo=LOCAL)) is allowed


def test_nightly_window_requires_an_aware_local_clock() -> None:
    with pytest.raises(NightlyTranslationError, match="timezone-aware"):
        within_nightly_window(datetime(2026, 11, 7, 22, 0))


def test_window_close_crosses_midnight_only_for_evening_start() -> None:
    assert window_close(datetime(2026, 11, 7, 23, 0, tzinfo=LOCAL)) == datetime(
        2026, 11, 8, 5, 0, tzinfo=LOCAL
    )
    assert window_close(datetime(2026, 11, 8, 2, 0, tzinfo=LOCAL)) == datetime(
        2026, 11, 8, 5, 0, tzinfo=LOCAL
    )


def test_retry_after_is_preferred_over_conservative_backoff() -> None:
    assert _retry_delay("rate limit; retry-after: 47 seconds", 0) == 47
    assert [_retry_delay(None, index) for index in range(5)] == [60, 300, 900, 1800, 1800]


def test_usable_qa_rejection_counts_as_translated_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rejected = _pending_job(1, covered_rejection=True)
    accepted = _pending_job(2).model_copy(
        update={"acceptance_status": TranslationAcceptanceStatus.ACCEPTED}
    )
    attempts = {rejected.job_id: object(), accepted.job_id: object()}
    monkeypatch.setattr(
        nightly_translation,
        "_attempt",
        lambda _root, _run_id, job: attempts[job.job_id],
    )

    coverage = nightly_translation._coverage(tmp_path, RUN_ID, (rejected, accepted))

    assert set(coverage) == {rejected.job_id, accepted.job_id}


def test_complete_coverage_draft_consumes_canonical_marker_free_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _pending_job(1)
    run_path = tmp_path / "synthetic-run"
    translation = run_path / "jobs" / job.chunk_id / "attempts" / "001" / "translation.md"
    translation.parent.mkdir(parents=True)
    translation.write_text(
        "Synthetic paragraph one.\n\nSynthetic paragraph two.\n", encoding="utf-8"
    )
    attempt = SimpleNamespace(translation_file=translation.relative_to(tmp_path))
    state: Any = SimpleNamespace(
        path=run_path,
        run=SimpleNamespace(run_id=RUN_ID),
        jobs=(job,),
    )
    monkeypatch.setattr(
        nightly_translation,
        "_coverage",
        lambda _root, _run_id, _jobs: {job.job_id: attempt},
    )

    draft = nightly_translation._build_draft(tmp_path, state)

    assert draft == Path("synthetic-run/coverage-draft.md")
    assert (tmp_path / draft).read_text(encoding="utf-8") == (
        "# 001\n\n\nSynthetic paragraph one.\n\nSynthetic paragraph two.\n"
    )


def test_complete_coverage_checkpoint_names_draft_assembly_before_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, worker_options, covered, worker = _synthetic_coordinator(monkeypatch, tmp_path)
    state.jobs = tuple(
        job.model_copy(
            update={
                "status": TranslationJobStatus.SUCCEEDED,
                "successful_attempt": 1,
                "acceptance_status": TranslationAcceptanceStatus.ACCEPTED,
            }
        )
        for job in state.jobs
    )
    covered.update(job.job_id for job in state.jobs)

    def fail_draft(_root: Path, _state: Any) -> Path:
        raise RuntimeError("synthetic draft failure")

    monkeypatch.setattr(nightly_translation, "_build_draft", fail_draft)

    with pytest.raises(RuntimeError, match="synthetic draft failure"):
        nightly_translation.run_nightly_translation(
            RUN_ID,
            project_root=tmp_path,
            clock=lambda: NOW,
            worker=worker,
        )

    checkpoint = NightlyTranslationCheckpoint.model_validate(
        yaml.safe_load((state.path / "nightly-checkpoint.yaml").read_text(encoding="utf-8"))
    )
    assert checkpoint.pending_count == 0
    assert checkpoint.next_unit is None
    assert checkpoint.next_safe_action == "assemble complete-coverage draft"
    assert worker_options == []


def test_default_batch_starts_three_earliest_untranslated_units_with_safe_worker_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state, worker_options, _covered, worker = _synthetic_coordinator(monkeypatch, tmp_path)

    checkpoint = nightly_translation.run_nightly_translation(
        RUN_ID,
        project_root=tmp_path,
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
        worker=worker,
    )

    assert [options.job_id for options in worker_options] == [
        f"{RUN_ID}__002-001",
        f"{RUN_ID}__002-002",
        f"{RUN_ID}__003-001",
        f"{RUN_ID}__004-001",
    ]
    assert checkpoint.qa_rejected_count == 1
    assert checkpoint.translated_chunks_count == 5
    assert all(options.workers == options.max_jobs == 1 for options in worker_options)
    assert all(not options.auto_repair for options in worker_options)
    assert all(not options.include_qa_rejected for options in worker_options)
    assert all(not options.retry_provider_failures for options in worker_options)
    assert all(not options.auto_recover_provider_failures for options in worker_options)


def test_cli_omitting_max_units_requests_a_three_unit_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[int] = []

    def run(_run_id: str, *, project_root: Path, max_units: int) -> NightlyTranslationCheckpoint:
        requested.append(max_units)
        return NightlyTranslationCheckpoint(
            snapshot_id="ts-synthetic",
            translation_run_id=RUN_ID,
            total_units=5,
            total_chunks=5,
            units_with_complete_translated_coverage=[],
            translated_chunks_count=0,
            accepted_count=0,
            qa_rejected_count=0,
            missing_translation_count=5,
            failed_retry_pending_count=0,
            pending_count=5,
            next_safe_action="resume",
            updated_at=NOW,
        )

    monkeypatch.setattr(nightly_translation, "run_nightly_translation", run)

    result = runner.invoke(app, ["run-nightly-translation", RUN_ID])

    assert result.exit_code == 0
    assert requested == [3]


def test_lock_contention_fails_before_the_worker_is_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, worker_options, _covered, worker = _synthetic_coordinator(monkeypatch, tmp_path)
    lock_path = state.path / ".nightly.lock"

    with (
        queue_lock(lock_path, timeout_seconds=0),
        pytest.raises(QueueLockTimeout, match="timed out acquiring queue lock"),
    ):
        nightly_translation.run_nightly_translation(
            RUN_ID,
            project_root=tmp_path,
            clock=lambda: NOW,
            worker=worker,
        )

    assert worker_options == []
