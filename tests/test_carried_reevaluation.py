"""Provider-free QA re-evaluation of a carried-forward draft (QA Re-evaluation 1.1.0).

A carried-forward draft has no attempt of the destination job behind it: its acceptance names the
carry-forward record as its attempt. Re-evaluation used to require a provider attempt, and the job
contract forbade a re-evaluation origin on a carried job, so a carried draft that newer QA would
accept could never receive that verdict. These cases pin what the route may and may not do: it
judges the immutable carried bytes, it publishes a new re-evaluation that names the carry-forward
acceptance it re-judged, and it never rewrites the translation, the carry-forward record, or the
historical verdict.
"""

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from book_translator.models import (
    ProviderName,
    TranslationAcceptanceOrigin,
    TranslationAcceptanceStatus,
    TranslationJob,
)
from book_translator.paragraph_markers import mark_paragraphs
from book_translator.release_assembly.workflow import load_effective_acceptance
from book_translator.terminology import validation as qa_validation
from book_translator.translation_migration import migrate_run
from book_translator.translation_queue import load_run_summary
from book_translator.translation_reevaluation import (
    TranslationReevaluationError,
    reevaluate_translation_job,
)
from book_translator.translation_repair import repair_translation_job
from tests.test_translation_migration import REPAIRED_004, migration  # noqa: F401
from tests.test_translation_queue import NOW
from tests.test_translation_worker import SyntheticProvider

CARRIED = "004-001"


def _carried(root: Path, run: str) -> TranslationJob:
    return next(
        job for job in load_run_summary(run, project_root=root).jobs if job.chunk_id == CARRIED
    )


def _history(root: Path, job: TranslationJob) -> dict[str, str]:
    """Hash every immutable input of the carried verdict."""
    paths = (job.acceptance_file, job.latest_carry_forward_file)
    assert all(path is not None for path in paths)
    files = [root / path for path in paths if path is not None]
    carried = files[0].parent / "translation.md"
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in [*files, carried]}


@pytest.fixture
def carried(migration: tuple[Path, str, str]) -> tuple[Path, str]:  # noqa: F811
    root, old, new = migration
    migrate_run(new, from_run=old, project_root=root, dry_run=False, now=lambda: NOW)
    job = _carried(root, new)
    assert job.acceptance_origin is TranslationAcceptanceOrigin.HISTORICAL_REEVALUATION
    assert job.acceptance_status is TranslationAcceptanceStatus.QA_REJECTED
    return root, new


def _accepting_qa(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for a newer terminology QA that accepts the carried draft."""
    original = qa_validation.validate_terminology

    def accepting(*args: Any, **kwargs: Any) -> Any:
        return original(*args, **kwargs).model_copy(update={"valid": True, "findings": []})

    monkeypatch.setattr(qa_validation, "validate_terminology", accepting)


def test_a_carried_draft_newer_qa_accepts_receives_the_verdict(
    carried: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, run = carried
    before = _carried(root, run)
    history = _history(root, before)
    _accepting_qa(monkeypatch)

    result = reevaluate_translation_job(
        run, before.job_id, 1, project_root=root, now=lambda: NOW + timedelta(seconds=90)
    )

    assert result.resulting_status is TranslationAcceptanceStatus.ACCEPTED
    after = _carried(root, run)
    assert after.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    assert after.acceptance_origin is TranslationAcceptanceOrigin.QA_REEVALUATION
    # The carried lineage is untouched: same historical verdict, record, and translated bytes.
    assert after.acceptance_file == before.acceptance_file
    assert after.acceptance_sha256 == before.acceptance_sha256
    assert after.carry_forward_count == before.carry_forward_count
    assert after.latest_carry_forward_file == before.latest_carry_forward_file
    assert after.attempt_count == 0 and after.successful_attempt is None
    assert _history(root, after) == history
    # The release boundary resolves the new verdict through the carry-forward it re-judged.
    effective = load_effective_acceptance(after, project_root=root)
    assert effective.evidence_file == after.latest_reevaluation_file
    assert effective.attempt_file == before.latest_carry_forward_file
    assert str(root / effective.translation_file) in history


def test_a_dry_run_writes_nothing(
    carried: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, run = carried
    before = _carried(root, run)
    _accepting_qa(monkeypatch)

    result = reevaluate_translation_job(run, before.job_id, 1, project_root=root, dry_run=True)

    assert result.dry_run and result.resulting_status is TranslationAcceptanceStatus.ACCEPTED
    assert _carried(root, run) == before
    job_directory = load_run_summary(run, project_root=root).path / "jobs" / CARRIED
    assert not (job_directory / "reevaluations").exists()


def test_a_still_rejected_carried_draft_can_still_be_repaired(carried: tuple[Path, str]) -> None:
    root, run = carried
    job = _carried(root, run)

    rejected = reevaluate_translation_job(
        run, job.job_id, 1, project_root=root, now=lambda: NOW + timedelta(seconds=90)
    )
    assert rejected.resulting_status is TranslationAcceptanceStatus.QA_REJECTED
    assert _carried(root, run).acceptance_origin is TranslationAcceptanceOrigin.QA_REEVALUATION

    calls: list[str] = []
    repaired = repair_translation_job(
        run,
        job.job_id,
        project_root=root,
        provider=ProviderName.FAKE,
        model="fake-v1",
        worker_id="repair-worker",
        lease_seconds=900,
        timeout_seconds=120,
        max_output_tokens=1000,
        provider_adapter=SyntheticProvider(mark_paragraphs(REPAIRED_004), calls),
        now=lambda: NOW + timedelta(seconds=120),
    )

    assert calls == [job.job_id]
    assert repaired.repaired_from_carry_forward == 1
    assert repaired.acceptance_status is TranslationAcceptanceStatus.ACCEPTED
    # The repair supersedes the re-evaluation it answered. The job record still satisfies the
    # contract and loads: the history stays counted, but it no longer defines the verdict.
    settled = _carried(root, run)
    assert settled.acceptance_origin is TranslationAcceptanceOrigin.INITIAL_QA
    assert settled.reevaluation_count == 1
    assert settled.latest_reevaluation_file is None and settled.latest_reevaluation_sha256 is None
    assert settled.carry_forward_count == 0
    effective = load_effective_acceptance(settled, project_root=root)
    assert effective.evidence_file == settled.acceptance_file
    # And the repaired draft can itself be re-evaluated, numbered after the older history.
    again = reevaluate_translation_job(
        run, job.job_id, settled.accepted_attempt or 0, project_root=root, dry_run=True
    )
    assert again.reevaluation_number == 2


def test_tampered_carried_bytes_are_refused(
    carried: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, run = carried
    job = _carried(root, run)
    _accepting_qa(monkeypatch)
    translation = (root / job.acceptance_file).parent / "translation.md"  # type: ignore[operator]
    translation.write_text("دستکاری شده.\n\nآن‌ها خوابیدند.\n", encoding="utf-8")

    with pytest.raises((TranslationReevaluationError, ValueError)):
        reevaluate_translation_job(run, job.job_id, 1, project_root=root, dry_run=True)
