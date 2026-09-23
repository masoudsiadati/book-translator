"""Preservation of provider prose that the numbered paragraph contract rejects.

A provider answer that arrived whole but broke the marker contract is refused as a translation and
kept as a private diagnostic artifact, so an expensive call can be inspected instead of lost.  These
tests pin the boundary of that preservation: what is kept, what is never kept, how it is described
without prose, and that nothing downstream ever mistakes it for a translation.
"""

import os
import re
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import (
    ProviderErrorCategory,
    ProviderName,
    TranslationAttempt,
    TranslationAttemptStatus,
    TranslationAttemptValidationReport,
    TranslationJobStatus,
)
from book_translator.paragraph_markers import marker_for
from book_translator.prompt_packaging import (
    MARKED_OUTPUT_CONTRACT_VERSION,
    execute_prompt_packages,
    plan_prompt_packages,
)
from book_translator.provider_execution import (
    ExecutionPlan,
    ProviderExecutionError,
    ProviderResult,
    ProviderUsage,
    execute_translation_job,
    list_attempts,
    plan_execution,
    validate_attempt,
)
from book_translator.provider_execution.artifacts import load_yaml, sha256_bytes
from book_translator.provider_execution.errors import ErrorClassification
from book_translator.provider_execution.result_validation import (
    candidate_output,
    extract_translation,
    validate_candidate,
)
from book_translator.translation_queue import claim_job, create_run, load_run_summary, plan_run
from book_translator.translation_snapshot import create_snapshot, plan_snapshot
from book_translator.translation_worker import run_translation_worker
from tests.test_translation_auto_repair import ScriptedProvider
from tests.test_translation_queue import NOW, SNAPSHOT_NOW
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import _options, _run_path

runner = CliRunner()

FIRST = "آلفا بتا."
SECOND = "گاما دلتا."
COMMENTARY = "Here is the translation you asked for:"
MARKED_ANSWER = f"{marker_for(1)}\n{FIRST}\n\n{marker_for(2)}\n{SECOND}\n"
PLAIN_ANSWER = f"{FIRST}\n\n{SECOND}\n"
MISSING_SECOND = f"{marker_for(1)}\n{FIRST}\n"
PREAMBLE_THEN_MARKED = f"{COMMENTARY}\n\n{MARKED_ANSWER}"
MANY_DEFECTS = f"{COMMENTARY}\n\n{marker_for(1)}\n{FIRST}\n[[P2]] {SECOND}\n"
SOURCE = b"Alpha beta.\n\nGamma delta.\n"


@pytest.fixture(autouse=True)
def _no_real_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here may reach a Claude, Codex, or OpenAI process."""

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("a real provider process was invoked")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


class FailingProvider:
    provider_name = "fake"
    adapter_version = "test-1.0.0"

    def __init__(self, classification: ErrorClassification) -> None:
        self.classification = classification

    def execute(self, request: object) -> ProviderResult:
        raise ProviderExecutionError(self.classification)


class ShapedProvider:
    """A provider that returns an arbitrary result shape (status, outputs) for one call."""

    provider_name = "fake"
    adapter_version = "test-1.0.0"

    def __init__(self, result: ProviderResult) -> None:
        self.result = result

    def execute(self, request: object) -> ProviderResult:
        return self.result


def _clock(offset: int) -> Callable[[], datetime]:
    return lambda: NOW + timedelta(seconds=offset)


def _marked_run(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    synthetic_project(root)
    snapshot = create_snapshot(plan_snapshot(project_root=root), now=lambda: SNAPSHOT_NOW)
    created = create_run(
        plan_run(
            snapshot.snapshot_id,
            project_root=root,
            output_contract_version=MARKED_OUTPUT_CONTRACT_VERSION,
            now=lambda: NOW,
        )
    )
    execute_prompt_packages(
        plan_prompt_packages(created.run_id, project_root=root, now=lambda: NOW)
    )
    return created.run_id


def _claimed_plan(root: Path, run_id: str) -> tuple[ExecutionPlan, str]:
    claim = claim_job(
        run_id,
        project_root=root,
        worker_id="worker",
        lease_seconds=900,
        now=lambda: NOW,
        token_factory=lambda: "secret-claim-token",
    )
    plan = plan_execution(
        run_id,
        claim.job.job_id,
        project_root=root,
        worker_id="worker",
        claim_token=claim.claim_token,
        provider=ProviderName.FAKE,
        model="fake-v1",
        timeout_seconds=120,
        max_output_tokens=1000,
        now=_clock(10),
    )
    return plan, claim.claim_token


def _execute(root: Path, provider: object) -> tuple[str, ExecutionPlan, TranslationAttempt]:
    run_id = _marked_run(root)
    plan, token = _claimed_plan(root, run_id)
    execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        provider=provider,  # type: ignore[arg-type]
        now=_clock(10),
        monotonic=lambda: 1.0,
    )
    return run_id, plan, load_yaml(plan.attempt_directory / "attempt.yaml", TranslationAttempt)


def _attempt_yaml(plan: ExecutionPlan) -> dict[str, object]:
    loaded = yaml.safe_load((plan.attempt_directory / "attempt.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _write_attempt_yaml(plan: ExecutionPlan, data: dict[str, object]) -> None:
    (plan.attempt_directory / "attempt.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _private(path: Path) -> bool:
    return os.name != "posix" or path.stat().st_mode & 0o077 == 0


# --------------------------------------------------------------------------------------------
# Pure result validation
# --------------------------------------------------------------------------------------------


def test_candidate_output_is_the_single_normalized_answer_or_nothing() -> None:
    shaped = ("  \r\n" + FIRST + "\r\n\r\n",)
    result = ProviderResult("id", "m", "completed", shaped, ProviderUsage())
    assert candidate_output(result) == "  \n" + FIRST + "\n"
    for shape in (
        ProviderResult("id", "m", "completed", ("",)),
        ProviderResult("id", "m", "completed", ("one", "two")),
        ProviderResult("id", "m", "incomplete", ("text",), incomplete_reason="limit"),
    ):
        with pytest.raises(ProviderExecutionError) as excinfo:
            candidate_output(shape)
        assert excinfo.value.marker_diagnostics is None


def test_validate_candidate_carries_prose_free_marker_diagnostics() -> None:
    with pytest.raises(ProviderExecutionError) as excinfo:
        validate_candidate(MANY_DEFECTS, SOURCE, output_contract=MARKED_OUTPUT_CONTRACT_VERSION)
    error = excinfo.value
    assert error.classification.category is ProviderErrorCategory.MALFORMED_RESPONSE
    diagnostics = error.marker_diagnostics
    assert diagnostics is not None
    assert diagnostics.expected_marker_count == 2
    assert diagnostics.observed_marker_count == 1
    codes = {finding.code for finding in diagnostics.findings}
    assert codes == {"text_before_first_marker", "marker_missing", "marker_malformed"}
    malformed = next(f for f in diagnostics.findings if f.code == "marker_malformed")
    assert malformed.observed_marker == "[[P2]]"
    missing = next(f for f in diagnostics.findings if f.code == "marker_missing")
    assert missing.expected_marker == marker_for(2) and missing.source_paragraph_index == 2
    rendered = str(error) + repr(diagnostics)
    for prose in (FIRST, SECOND, COMMENTARY):
        assert prose not in rendered
    # The composed entry point behaves exactly as before.
    assert extract_translation(
        ProviderResult("id", "m", "completed", (MARKED_ANSWER,)),
        SOURCE,
        output_contract=MARKED_OUTPUT_CONTRACT_VERSION,
    ).content == PLAIN_ANSWER.encode("utf-8")


# --------------------------------------------------------------------------------------------
# A. A conformant answer publishes a translation and nothing else
# --------------------------------------------------------------------------------------------


def test_valid_marked_output_publishes_translation_without_rejected_artifact(
    tmp_path: Path,
) -> None:
    run_id, plan, attempt = _execute(tmp_path / "project", ScriptedProvider((MARKED_ANSWER,)))
    assert attempt.status is TranslationAttemptStatus.SUCCEEDED
    assert (plan.attempt_directory / "translation.md").read_bytes() == PLAIN_ANSWER.encode()
    assert not (plan.attempt_directory / "rejected-output.md").exists()
    assert attempt.rejected_output_file is None and attempt.rejected_output_sha256 is None
    assert attempt.marker_findings == []
    assert attempt.expected_marker_count is None and attempt.observed_marker_count is None
    report = validate_attempt(run_id, attempt.job_id, 1, project_root=tmp_path / "project")
    assert report.valid and report.rejected_output_hash_valid is True


# --------------------------------------------------------------------------------------------
# B. A missing marker keeps the exact candidate bytes privately
# --------------------------------------------------------------------------------------------


def test_marker_missing_preserves_exact_candidate_as_private_rejected_output(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    windows_shaped = MISSING_SECOND.replace("\n", "\r\n") + "\r\n\r\n"
    run_id, plan, attempt = _execute(root, ScriptedProvider((windows_shaped,)))
    rejected = plan.attempt_directory / "rejected-output.md"

    assert attempt.status is TranslationAttemptStatus.FAILED
    assert attempt.error_category is ProviderErrorCategory.MALFORMED_RESPONSE
    assert attempt.translation_file is None and attempt.translation_sha256 is None
    assert not (plan.attempt_directory / "translation.md").exists()
    assert rejected.is_file() and _private(rejected)
    assert rejected.read_bytes() == MISSING_SECOND.encode("utf-8")
    assert attempt.rejected_output_file == rejected.relative_to(root)
    assert attempt.rejected_output_sha256 == sha256_bytes(rejected.read_bytes())
    assert attempt.expected_marker_count == 2 and attempt.observed_marker_count == 1
    findings = [
        (f.code, f.expected_marker, f.source_paragraph_index) for f in attempt.marker_findings
    ]
    assert findings == [("marker_missing", marker_for(2), 2)]
    report = validate_attempt(run_id, attempt.job_id, 1, project_root=root)
    assert report.valid and report.rejected_output_hash_valid is True
    assert "rejected_output_hash_and_privacy" in report.checks_performed
    listed = list_attempts(run_id, attempt.job_id, project_root=root)
    assert [item.validation for item in listed] == ["valid"]
    job = load_run_summary(run_id, project_root=root).jobs[0]
    assert job.status is TranslationJobStatus.FAILED
    assert job.successful_attempt is None and job.acceptance_status is None
    assert job.last_error_category == "malformed_response"


# --------------------------------------------------------------------------------------------
# C. Preamble before the first marker
# --------------------------------------------------------------------------------------------


def test_text_before_first_marker_is_preserved_with_a_structured_finding(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _, plan, attempt = _execute(root, ScriptedProvider((PREAMBLE_THEN_MARKED,)))
    rejected = plan.attempt_directory / "rejected-output.md"
    assert attempt.status is TranslationAttemptStatus.FAILED
    assert rejected.read_bytes() == PREAMBLE_THEN_MARKED.encode("utf-8")
    assert attempt.rejected_output_sha256 == sha256_bytes(rejected.read_bytes())
    assert [(f.code, f.expected_marker) for f in attempt.marker_findings] == [
        ("text_before_first_marker", marker_for(1))
    ]
    assert attempt.expected_marker_count == 2 and attempt.observed_marker_count == 2
    assert not (plan.attempt_directory / "translation.md").exists()


# --------------------------------------------------------------------------------------------
# D. Several findings, no prose anywhere but the rejected artifact
# --------------------------------------------------------------------------------------------


def test_multiple_marker_findings_are_persisted_without_prose(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id, plan, attempt = _execute(root, ScriptedProvider((MANY_DEFECTS,)))

    assert attempt.status is TranslationAttemptStatus.FAILED
    assert attempt.error_category is ProviderErrorCategory.MALFORMED_RESPONSE
    assert attempt.retryable and not attempt.ambiguous
    assert attempt.translation_file is None
    assert attempt.rejected_output_file is not None
    assert attempt.rejected_output_sha256 is not None
    assert re.fullmatch(r"[0-9a-f]{64}", attempt.rejected_output_sha256)
    assert (root / attempt.rejected_output_file).read_bytes() == MANY_DEFECTS.encode("utf-8")
    codes = sorted(f.code for f in attempt.marker_findings)
    assert codes == ["marker_malformed", "marker_missing", "text_before_first_marker"]
    malformed = next(f for f in attempt.marker_findings if f.code == "marker_malformed")
    assert malformed.observed_marker == "[[P2]]"
    assert all(f.severity == "error" for f in attempt.marker_findings)
    assert attempt.sanitized_error_message is not None
    assert "marker_missing" in attempt.sanitized_error_message
    metadata = "".join(
        (plan.attempt_directory / name).read_text(encoding="utf-8")
        for name in ("attempt.yaml", "validation-report.yaml", "request.json", "response.json")
    )
    for prose in (FIRST, SECOND, COMMENTARY):
        assert prose not in metadata
    assert "secret-claim-token" not in metadata
    job = load_run_summary(run_id, project_root=root).jobs[0]
    assert job.status is TranslationJobStatus.FAILED and job.acceptance_status is None
    assert job.last_error_message is not None and COMMENTARY not in job.last_error_message


# --------------------------------------------------------------------------------------------
# E/F. No candidate, no artifact
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "category"),
    [
        (ScriptedProvider(("",)), ProviderErrorCategory.EMPTY_RESPONSE),
        (ScriptedProvider(("   \n\n",)), ProviderErrorCategory.EMPTY_RESPONSE),
        (
            ShapedProvider(
                ProviderResult(
                    "id", "m", "incomplete", (MARKED_ANSWER,), incomplete_reason="max_output"
                )
            ),
            ProviderErrorCategory.OUTPUT_TRUNCATED,
        ),
        (
            ShapedProvider(ProviderResult("id", "m", "completed", (MARKED_ANSWER, MARKED_ANSWER))),
            ProviderErrorCategory.MALFORMED_RESPONSE,
        ),
        (
            FailingProvider(
                ErrorClassification(
                    ProviderErrorCategory.RATE_LIMIT, True, False, False, "synthetic limit", 429
                )
            ),
            ProviderErrorCategory.RATE_LIMIT,
        ),
        (
            FailingProvider(
                ErrorClassification(
                    ProviderErrorCategory.AUTHENTICATION, False, False, False, "synthetic auth"
                )
            ),
            ProviderErrorCategory.AUTHENTICATION,
        ),
        (
            FailingProvider(
                ErrorClassification(
                    ProviderErrorCategory.CONNECTION_AMBIGUOUS, True, True, False, "cut off"
                )
            ),
            ProviderErrorCategory.CONNECTION_AMBIGUOUS,
        ),
    ],
    ids=[
        "empty",
        "whitespace",
        "truncated",
        "multiple_outputs",
        "rate_limit",
        "authentication",
        "ambiguous_transport",
    ],
)
def test_no_trustworthy_candidate_means_no_rejected_artifact(
    tmp_path: Path, provider: object, category: ProviderErrorCategory
) -> None:
    root = tmp_path / "project"
    run_id, plan, attempt = _execute(root, provider)
    assert attempt.status is not TranslationAttemptStatus.SUCCEEDED
    assert attempt.error_category is category
    assert not (plan.attempt_directory / "rejected-output.md").exists()
    assert not (plan.attempt_directory / "translation.md").exists()
    assert attempt.rejected_output_file is None and attempt.rejected_output_sha256 is None
    assert attempt.marker_findings == [] and attempt.expected_marker_count is None
    assert validate_attempt(run_id, attempt.job_id, 1, project_root=root).valid


def test_lease_expiry_after_a_rejected_answer_keeps_no_candidate(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _marked_run(root)
    plan, token = _claimed_plan(root, run_id)
    times = iter((NOW + timedelta(seconds=10),) * 2 + (NOW + timedelta(seconds=901),))
    execute_translation_job(
        plan,
        worker_id="worker",
        claim_token=token,
        provider=ScriptedProvider((MISSING_SECOND,)),
        now=lambda: next(times),
        monotonic=lambda: 1.0,
    )
    attempt = load_yaml(plan.attempt_directory / "attempt.yaml", TranslationAttempt)
    assert attempt.status is TranslationAttemptStatus.AMBIGUOUS
    assert attempt.error_category is ProviderErrorCategory.LEASE_INVALID
    assert attempt.rejected_output_file is None and attempt.marker_findings == []
    assert not (plan.attempt_directory / "rejected-output.md").exists()


# --------------------------------------------------------------------------------------------
# G. The validity checker sees every way the artifact can go wrong
# --------------------------------------------------------------------------------------------


def test_validator_detects_missing_rejected_output(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id, plan, attempt = _execute(root, ScriptedProvider((MISSING_SECOND,)))
    (plan.attempt_directory / "rejected-output.md").unlink()
    report = validate_attempt(run_id, attempt.job_id, 1, project_root=root)
    assert not report.valid and report.rejected_output_hash_valid is False
    assert report.translation_hash_valid and report.response_hash_valid
    assert list_attempts(run_id, attempt.job_id, project_root=root)[0].validation == "invalid"


def test_validator_detects_rejected_output_hash_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id, plan, attempt = _execute(root, ScriptedProvider((MISSING_SECOND,)))
    (plan.attempt_directory / "rejected-output.md").write_text(MARKED_ANSWER, encoding="utf-8")
    report = validate_attempt(run_id, attempt.job_id, 1, project_root=root)
    assert not report.valid and report.rejected_output_hash_valid is False
    assert report.translation_hash_valid


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_validator_detects_insecure_rejected_output_permissions(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id, plan, attempt = _execute(root, ScriptedProvider((MISSING_SECOND,)))
    os.chmod(plan.attempt_directory / "rejected-output.md", 0o644)
    report = validate_attempt(run_id, attempt.job_id, 1, project_root=root)
    assert not report.valid and report.permissions_valid is False
    assert report.rejected_output_hash_valid is True


def test_validator_detects_rejected_hash_without_file_reference(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id, plan, attempt = _execute(root, ScriptedProvider((MISSING_SECOND,)))
    data = _attempt_yaml(plan)
    data["rejected_output_file"] = None
    _write_attempt_yaml(plan, data)
    report = validate_attempt(run_id, attempt.job_id, 1, project_root=root)
    assert not report.valid and report.rejected_output_hash_valid is False


def test_validator_refuses_a_succeeded_attempt_that_carries_rejected_output(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id, plan, attempt = _execute(root, ScriptedProvider((MISSING_SECOND,)))
    data = _attempt_yaml(plan)
    data["status"] = "succeeded"
    data["error_category"] = None
    _write_attempt_yaml(plan, data)
    report = validate_attempt(run_id, attempt.job_id, 1, project_root=root)
    assert not report.valid and report.status_consistent is False
    assert report.rejected_output_hash_valid is True


def test_validator_refuses_translation_and_rejected_output_together(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id, plan, attempt = _execute(root, ScriptedProvider((MARKED_ANSWER,)))
    translation = plan.attempt_directory / "translation.md"
    rejected = plan.attempt_directory / "rejected-output.md"
    rejected.write_bytes(translation.read_bytes())
    os.chmod(rejected, 0o600)
    data = _attempt_yaml(plan)
    data["rejected_output_file"] = str(rejected.relative_to(root).as_posix())
    data["rejected_output_sha256"] = sha256_bytes(rejected.read_bytes())
    _write_attempt_yaml(plan, data)
    report = validate_attempt(run_id, attempt.job_id, 1, project_root=root)
    assert not report.valid and report.status_consistent is False


def test_validator_refuses_an_ambiguous_attempt_that_carries_rejected_output(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    run_id, plan, attempt = _execute(root, ScriptedProvider((MISSING_SECOND,)))
    data = _attempt_yaml(plan)
    data["status"] = "ambiguous"
    data["ambiguous"] = True
    _write_attempt_yaml(plan, data)
    report = validate_attempt(run_id, attempt.job_id, 1, project_root=root)
    assert not report.valid and report.status_consistent is False


def test_validator_refuses_marker_findings_on_a_succeeded_attempt(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id, plan, attempt = _execute(root, ScriptedProvider((MARKED_ANSWER,)))
    data = _attempt_yaml(plan)
    data["marker_findings"] = [
        {
            "code": "marker_missing",
            "severity": "error",
            "source_paragraph_index": 2,
            "related_paragraph_index": None,
            "expected_marker": marker_for(2),
            "observed_marker": None,
            "message": "an expected paragraph marker is absent from the document",
        }
    ]
    data["expected_marker_count"] = 2
    data["observed_marker_count"] = 1
    _write_attempt_yaml(plan, data)
    report = validate_attempt(run_id, attempt.job_id, 1, project_root=root)
    assert not report.valid and report.status_consistent is False


# --------------------------------------------------------------------------------------------
# H. Attempts written before the fields existed still load and validate
# --------------------------------------------------------------------------------------------


NEW_KEYS = (
    "rejected_output_file",
    "rejected_output_sha256",
    "marker_findings",
    "expected_marker_count",
    "observed_marker_count",
)


def test_pre_existing_attempt_files_without_new_fields_load_and_validate(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id, plan, attempt = _execute(root, ScriptedProvider((MARKED_ANSWER,)))
    data = _attempt_yaml(plan)
    assert all(key in data for key in NEW_KEYS)
    for key in NEW_KEYS:
        del data[key]
    _write_attempt_yaml(plan, data)
    loaded = load_yaml(plan.attempt_directory / "attempt.yaml", TranslationAttempt)
    assert loaded.rejected_output_file is None and loaded.marker_findings == []
    report = validate_attempt(run_id, attempt.job_id, 1, project_root=root)
    assert report.valid and report.rejected_output_hash_valid is True


def test_historical_failed_marked_attempt_shape_still_loads() -> None:
    """The exact field set of the production attempt whose prose was already discarded."""
    historical = {
        "schema_version": "1.0",
        "attempt_id": "tr-000000000000-001__000-prologue-001__attempt-001",
        "attempt_number": 1,
        "execution_kind": "translation",
        "repaired_from_attempt": None,
        "run_id": "tr-000000000000-001",
        "job_id": "tr-000000000000-001__000-prologue-001",
        "snapshot_id": "translation-fa-000000000000000000000000",
        "prompt_package_id": "pp-000000000000000000000000",
        "provider": "claude-code",
        "provider_adapter_version": "1.2.0",
        "provider_executor_version": "1.0.0",
        "model_requested": "opus",
        "model_returned": "claude-opus-5",
        "provider_response_id": "00000000-0000-0000-0000-000000000000",
        "request_file": "workspace/translation-runs/x/jobs/y/attempts/001/request.json",
        "request_sha256": "0" * 64,
        "response_file": "workspace/translation-runs/x/jobs/y/attempts/001/response.json",
        "response_sha256": "1" * 64,
        "raw_response_stored": True,
        "translation_file": None,
        "translation_sha256": None,
        "output_contract": MARKED_OUTPUT_CONTRACT_VERSION,
        "status": "failed",
        "started_at": "2026-09-15T14:14:10.584729Z",
        "completed_at": "2026-09-15T14:20:55.203529Z",
        "latency_milliseconds": 333267,
        "input_tokens": 6,
        "output_tokens": 16179,
        "total_tokens": 75418,
        "estimated_input_tokens": 12511,
        "max_output_tokens": 12000,
        "sdk_retry_count": 0,
        "stop_reason": None,
        "error_category": "malformed_response",
        "retryable": True,
        "ambiguous": False,
        "sanitized_error_message": (
            "provider output violated the numbered paragraph contract: "
            "marker_missing, text_before_first_marker"
        ),
        "warnings": [],
    }
    attempt = TranslationAttempt.model_validate(historical)
    assert attempt.rejected_output_file is None and attempt.rejected_output_sha256 is None
    assert attempt.marker_findings == [] and attempt.observed_marker_count is None


def test_validation_reports_written_before_the_check_still_load() -> None:
    report = TranslationAttemptValidationReport.model_validate(
        {
            "schema_version": "1.0",
            "attempt_id": "x__attempt-001",
            "valid": True,
            "job_lineage_valid": True,
            "package_lineage_valid": True,
            "request_hash_valid": True,
            "response_hash_valid": True,
            "translation_hash_valid": True,
            "status_consistent": True,
            "permissions_valid": True,
            "checks_performed": ["job_and_package_lineage"],
            "warnings": [],
            "errors": [],
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    assert report.rejected_output_hash_valid is None


# --------------------------------------------------------------------------------------------
# I/J. Only translation.md is ever a translation; nothing downstream reads the rejected artifact
# --------------------------------------------------------------------------------------------


def test_worker_never_assesses_or_accepts_a_rejected_answer(tmp_path: Path) -> None:
    root = tmp_path / "project"
    run_id = _marked_run(root)
    run_translation_worker(
        run_id,
        project_root=root,
        options=_options(max_jobs=1),
        provider_factory=lambda _job: ScriptedProvider((MANY_DEFECTS,)),
        now=_clock(10),
    )
    state = load_run_summary(run_id, project_root=root)
    job = state.jobs[0]
    attempt_directory = _run_path(root, run_id) / "jobs/001-001/attempts/001"
    # The worker schedules a retry for a retryable structural refusal; it never assesses it.
    assert job.status in {TranslationJobStatus.FAILED, TranslationJobStatus.RETRY_PENDING}
    assert job.last_error_category == "malformed_response"
    assert job.acceptance_status is None and job.accepted_attempt is None
    assert job.acceptance_file is None and job.successful_attempt is None
    assert (attempt_directory / "rejected-output.md").is_file()
    assert not (attempt_directory / "translation.md").exists()
    assert not (_run_path(root, run_id) / "jobs/001-001/acceptance").exists()
    assert state.run.provider_succeeded_unassessed_count == 0
    assert state.run.accepted_count == 0


def test_only_the_provider_boundary_knows_the_rejected_artifact() -> None:
    """Acceptance, QA, repair, reuse, release, and editing code never name the artifact.

    The recovery policy is the one deliberate exception: it must treat the artifact as stored
    provider output so that a job carrying one is never replayed as if nothing had arrived.
    """
    package = Path(__file__).resolve().parents[1] / "src/book_translator"
    allowed = {
        "provider_execution/executor.py",
        "provider_execution/result_validation.py",
        "provider_execution/errors.py",
        "models/provider_execution.py",
        "translation_provider_recovery.py",
        "cli.py",
    }
    mentions = {
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if re.search(r"rejected-output\.md|rejected_output_", path.read_text(encoding="utf-8"))
    }
    assert mentions <= allowed, sorted(mentions - allowed)
    assert "provider_execution/executor.py" in mentions


def test_attempt_listing_reports_the_rejected_artifact_without_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    run_id, _, attempt = _execute(root, ScriptedProvider((MANY_DEFECTS,)))
    monkeypatch.chdir(root)
    monkeypatch.setenv("COLUMNS", "400")
    result = runner.invoke(
        app, ["show-translation-attempt", run_id, attempt.job_id, "1"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    flat = result.output.replace("\n", "")
    assert "malformed_response" in flat
    assert "Rejected output" in flat and "yes" in flat
    assert "marker_missing" in flat and "text_before_first_marker" in flat
    for prose in (FIRST, SECOND, COMMENTARY):
        assert prose not in result.output
