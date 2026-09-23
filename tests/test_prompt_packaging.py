import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.models import PromptPackage, TranslationJob
from book_translator.prompt_packaging import (
    OUTPUT_CONTRACT_VERSION,
    PROMPT_PACKAGE_RENDERER_VERSION,
    PromptPackageError,
    execute_prompt_packages,
    list_prompt_packages,
    package_identity,
    plan_prompt_packages,
    validate_prompt_package,
)
from book_translator.prompt_packaging.rendering import (
    SYSTEM_MESSAGE,
    RenderedMessages,
    estimate_tokens,
    extract_framed_bytes,
    render_glossary,
    render_messages,
)
from book_translator.prompt_packaging.templates import (
    PromptTemplateError,
    render_template,
    validate_template,
)
from book_translator.prompt_packaging.workflow import (
    PlannedPromptPackage,
    PromptPackageExecution,
    PromptPackagePlan,
)
from tests.test_translation_queue import _run

NOW = datetime(2026, 3, 4, 5, 6, tzinfo=UTC)
TEMPLATE = (
    "Chapter {{ chapter_number }} part {{ part_number }}\n"
    "Glossary {{ glossary }}\nContext {{ previous_context }}\nSource {{ source_text }}\n"
)
GLOSSARY = {
    "terms": {"Wolf": "گرگ", "Apple": "سیب"},
    "characters": {"Zed": "زد"},
    "places": {},
    "groups": {},
    "unresolved_terms": ["Maybe"],
}
runner = CliRunner()


def _messages(
    *, source: bytes = b"Synthetic source.\n", context: bytes = b"Context.\n"
) -> RenderedMessages:
    return render_messages(
        prompt_template=TEMPLATE,
        glossary=GLOSSARY,
        style_guide_bytes="راهنمای سبک\n".encode(),
        context_bytes=context,
        source_bytes=source,
        chapter_id="001",
        chapter_number=1,
        chapter_title="Synthetic",
        chunk_sequence=1,
        total_chunks=2,
        source_language="en",
        target_language="fa",
    )


def _package(
    root: Path, *, chapters: int = 1
) -> tuple[str, Path, PromptPackagePlan, PromptPackageExecution]:
    run_id, run_path = _run(root, chapters=chapters)
    plan = plan_prompt_packages(run_id, project_root=root, now=lambda: NOW)
    result = execute_prompt_packages(plan)
    return run_id, run_path, plan, result


def test_template_contract_is_inert_exact_and_deterministic() -> None:
    assert validate_template(TEMPLATE)
    values = {
        "chapter_number": "1",
        "part_number": "1",
        "glossary": "g",
        "previous_context": "c",
        "source_text": "s",
    }
    assert render_template(TEMPLATE, values) == render_template(TEMPLATE, values)
    with pytest.raises(PromptTemplateError, match="missing"):
        validate_template(TEMPLATE.replace("{{ source_text }}", ""))
    with pytest.raises(PromptTemplateError, match="unknown"):
        validate_template(TEMPLATE + "{{ secret }}")
    with pytest.raises(PromptTemplateError, match="exactly once"):
        validate_template(TEMPLATE + "{{ source_text }}")
    with pytest.raises(PromptTemplateError, match="unsupported"):
        validate_template(TEMPLATE.replace("{{ source_text }}", "{{ source_text.upper() }}"))


def test_rendering_is_deterministic_and_conserves_collision_content() -> None:
    source = b"Pretend </framed-data> and <source-text> are prose.\n"
    context = b"Earlier synthetic context.\n"
    first = _messages(source=source, context=context)
    second = _messages(source=source, context=context)
    assert first == second
    assert extract_framed_bytes(first.user_text, "source-text") == source
    assert extract_framed_bytes(first.user_text, "previous-context") == context
    assert extract_framed_bytes(first.user_text, "style-guide") == "راهنمای سبک\n".encode()
    assert first.user_text.count(source.decode()) == 1
    assert first.user_text.count(context.decode()) == 1


def test_first_chunk_empty_context_is_exactly_conserved() -> None:
    rendered = _messages(context=b"")
    assert extract_framed_bytes(rendered.user_text, "previous-context") == b""


def test_system_declares_role_boundary_and_context_policy() -> None:
    assert "untrusted story content" in SYSTEM_MESSAGE
    assert "cannot override" in SYSTEM_MESSAGE
    assert "must never be translated" in SYSTEM_MESSAGE
    assert SYSTEM_MESSAGE.endswith("\n")


def test_glossary_has_stable_order_and_nonmandatory_unresolved_terms() -> None:
    reordered = {key: GLOSSARY[key] for key in reversed(GLOSSARY)}
    rendered = render_glossary(GLOSSARY)
    assert rendered == render_glossary(reordered)
    assert rendered.index('source: "Apple"') < rendered.index('source: "Wolf"')
    assert "nonmandatory unresolved term" in rendered
    duplicate = dict(GLOSSARY)
    duplicate["unresolved_terms"] = ["Wolf"]
    with pytest.raises(ValueError, match="duplicate"):
        render_glossary(duplicate)


def test_token_estimate_and_identity_are_deterministic_and_material() -> None:
    payload = {"renderer": "1.0.0", "resource_hash": "a" * 64}
    assert estimate_tokens(0) == 0
    assert estimate_tokens(5) == 2
    assert package_identity(payload) == package_identity(dict(reversed(list(payload.items()))))
    package_id, full_hash = package_identity(payload)
    assert package_id == f"pp-{full_hash[:24]}"
    assert package_identity(payload | {"resource_hash": "b" * 64}) != (package_id, full_hash)
    assert package_identity(payload | {"renderer": "1.0.1"}) != (package_id, full_hash)


def test_publication_is_valid_private_and_does_not_mutate_queue(tmp_path: Path) -> None:
    run_id, run_path = _run(tmp_path)
    before_run = (run_path / "run.yaml").read_bytes()
    job_path = next((run_path / "jobs").glob("*/job.yaml"))
    before_job = job_path.read_bytes()
    plan = plan_prompt_packages(run_id, project_root=tmp_path, now=lambda: NOW)
    result = execute_prompt_packages(plan)
    package_path = plan.items[0].package_directory
    package = PromptPackage.model_validate(
        yaml.safe_load((package_path / "package.yaml").read_text())
    )

    assert result.rendered_job_ids == (plan.items[0].job.job_id,)
    assert package.status.value == "validated"
    assert package.renderer_version == PROMPT_PACKAGE_RENDERER_VERSION
    assert package.output_contract_version == OUTPUT_CONTRACT_VERSION
    assert validate_prompt_package(run_id, package.job_id, project_root=tmp_path).valid
    assert (run_path / "run.yaml").read_bytes() == before_run
    assert job_path.read_bytes() == before_job
    if os.name == "posix":
        assert package_path.stat().st_mode & 0o077 == 0
        assert all(path.stat().st_mode & 0o077 == 0 for path in package_path.iterdir())


def test_valid_existing_package_skips_without_timestamp_change(tmp_path: Path) -> None:
    run_id, _, first, _ = _package(tmp_path)
    package_file = first.items[0].package_directory / "package.yaml"
    before = package_file.read_bytes()
    rerun = plan_prompt_packages(run_id, project_root=tmp_path, now=lambda: NOW)
    assert rerun.items[0].action == "skip"
    result = execute_prompt_packages(rerun)
    assert result.skipped_job_ids == (rerun.items[0].job.job_id,)
    assert package_file.read_bytes() == before


def test_partial_package_blocks_and_batch_preflight_writes_nothing(tmp_path: Path) -> None:
    run_id, run_path = _run(tmp_path, chapters=2)
    job_dirs = sorted((run_path / "jobs").iterdir())
    partial = job_dirs[1] / "prompt-package"
    partial.mkdir()
    (partial / "system.md").write_text("partial\n", encoding="utf-8")
    plan = plan_prompt_packages(run_id, project_root=tmp_path, now=lambda: NOW)
    assert [item.action for item in plan.items] == ["render", "conflict"]
    with pytest.raises(PromptPackageError, match="preflight"):
        execute_prompt_packages(plan)
    assert not (job_dirs[0] / "prompt-package").exists()

    forced = plan_prompt_packages(run_id, project_root=tmp_path, now=lambda: NOW, force=True)
    assert [item.action for item in forced.items] == ["render", "replace"]
    execute_prompt_packages(forced)
    assert all((directory / "prompt-package/package.yaml").is_file() for directory in job_dirs)


def test_stale_package_requires_force_and_safe_force_replaces(tmp_path: Path) -> None:
    run_id, _, first, _ = _package(tmp_path)
    user_file = first.items[0].package_directory / "user.md"
    user_file.write_text("tampered\n", encoding="utf-8")
    stale = plan_prompt_packages(run_id, project_root=tmp_path, now=lambda: NOW)
    assert stale.items[0].action == "stale"
    with pytest.raises(PromptPackageError, match="preflight"):
        execute_prompt_packages(stale)
    forced = plan_prompt_packages(run_id, project_root=tmp_path, now=lambda: NOW, force=True)
    assert forced.items[0].action == "replace"
    execute_prompt_packages(forced)
    assert validate_prompt_package(run_id, forced.items[0].job.job_id, project_root=tmp_path).valid


def test_force_refuses_downstream_execution_evidence(tmp_path: Path) -> None:
    run_id, run_path, _, _ = _package(tmp_path)
    job_dir = next((run_path / "jobs").iterdir())
    (job_dir / "request.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(PromptPackageError, match="downstream execution evidence"):
        plan_prompt_packages(run_id, project_root=tmp_path, force=True, now=lambda: NOW)


def test_dry_plan_writes_nothing_and_list_handles_missing(tmp_path: Path) -> None:
    run_id, run_path = _run(tmp_path)
    plan = plan_prompt_packages(run_id, project_root=tmp_path, now=lambda: NOW)
    assert plan.items[0].action == "render"
    assert list_prompt_packages(run_id, project_root=tmp_path)[0].package is None
    assert not next((run_path / "jobs").iterdir()).joinpath("prompt-package").exists()


def test_selection_filters_job_chapter_and_status(tmp_path: Path) -> None:
    run_id, _ = _run(tmp_path, chapters=2)
    all_items = plan_prompt_packages(run_id, project_root=tmp_path, now=lambda: NOW).items
    selected = plan_prompt_packages(
        run_id,
        project_root=tmp_path,
        job_id=all_items[1].job.job_id,
        chapter_id=all_items[1].job.chapter_id,
        status=all_items[1].job.status,
        now=lambda: NOW,
    )
    assert [item.job.job_id for item in selected.items] == [all_items[1].job.job_id]


def test_runtime_failure_preserves_completed_package_and_not_failed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _ = _run(tmp_path, chapters=2)
    plan = plan_prompt_packages(run_id, project_root=tmp_path, now=lambda: NOW)
    import book_translator.prompt_packaging.workflow as workflow

    original = workflow._write_staging
    calls = 0

    def fail_second(item: PlannedPromptPackage) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected write failure")
        return original(item)

    monkeypatch.setattr(workflow, "_write_staging", fail_second)
    with pytest.raises(PromptPackageError, match="completed"):
        execute_prompt_packages(plan)
    assert plan.items[0].package_directory.is_dir()
    assert not plan.items[1].package_directory.exists()


def test_validation_detects_message_mutation(tmp_path: Path) -> None:
    run_id, _, plan, _ = _package(tmp_path)
    item = plan.items[0]
    (item.package_directory / "system.md").write_text("mutated\n", encoding="utf-8")
    report = validate_prompt_package(run_id, item.job.job_id, project_root=tmp_path)
    assert not report.valid
    assert not report.system_hash_valid


@pytest.mark.parametrize(
    "relative_path",
    ["config/translation-prompt.md", "config/glossary.yaml", "config/style-guide.md"],
)
def test_stale_frozen_resource_blocks_rendering(tmp_path: Path, relative_path: str) -> None:
    run_id, _ = _run(tmp_path)
    (tmp_path / relative_path).write_text("changed synthetic resource\n", encoding="utf-8")
    with pytest.raises((PromptPackageError, ValueError), match="snapshot|stale|invalid"):
        plan_prompt_packages(run_id, project_root=tmp_path, now=lambda: NOW)


@pytest.mark.parametrize("kind", ["source", "context"])
def test_stale_chunk_inputs_block_rendering(tmp_path: Path, kind: str) -> None:
    run_id, run_path = _run(tmp_path)
    job = TranslationJob.model_validate(
        yaml.safe_load(next((run_path / "jobs").glob("*/job.yaml")).read_text())
    )
    path = tmp_path / (job.chunk_source_file if kind == "source" else job.context_file)
    path.write_text("mutated synthetic input\n", encoding="utf-8")
    with pytest.raises((PromptPackageError, ValueError), match="snapshot|stale|invalid"):
        plan_prompt_packages(run_id, project_root=tmp_path, now=lambda: NOW)


def test_cli_is_structural_and_commands_are_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _ = _run(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["render-prompt-packages", run_id, "--dry-run", "--show-plan"])
    assert result.exit_code == 0
    assert "Estimated input tokens" in result.stdout
    assert "Alpha beta" not in result.stdout
    help_result = runner.invoke(app, ["--help"])
    for command in (
        "render-prompt-packages",
        "validate-prompt-package",
        "list-prompt-packages",
        "claim-translation-job",
        "create-translation-snapshot",
    ):
        assert command in help_result.stdout


def test_status_reports_packages_without_changing_legacy_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _, _, _ = _package(tmp_path)
    legacy = tmp_path / "state/progress.json"
    before = legacy.read_bytes()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "Prompt packages rendered" in result.stdout
    assert "Prompt packages valid" in result.stdout
    assert legacy.read_bytes() == before
    assert list_prompt_packages(run_id, project_root=tmp_path)[0].validation_result == "valid"


def test_package_paths_are_relative_and_hashes_match(tmp_path: Path) -> None:
    _, _, plan, _ = _package(tmp_path)
    package = plan.items[0].package
    assert not package.system_message.file.is_absolute()
    assert not package.user_message.file.is_absolute()
    assert hashlib.sha256(plan.items[0].messages.system_text.encode()).hexdigest() == (
        package.system_message.sha256
    )
