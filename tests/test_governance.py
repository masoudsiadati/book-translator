import re
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from book_translator import __version__
from book_translator.book_identity import BOOK_IDENTITY_MIGRATION_VERSION
from book_translator.book_metadata import BOOK_METADATA_ENGINE_VERSION
from book_translator.chapter_extraction import EXTRACTION_VERSION
from book_translator.chapter_titles import (
    CHAPTER_TITLE_ENGINE_VERSION,
    CHAPTER_TITLE_PROMPT_VERSION,
    CHAPTER_TITLE_QA_VERSION,
)
from book_translator.chapter_titles.revision import CHAPTER_TITLE_REVISION_VERSION
from book_translator.cli import app
from book_translator.document_chunking import CHUNK_ENGINE_VERSION
from book_translator.epub import EPUB_BUILDER_VERSION
from book_translator.execution_resolution import EXECUTION_RESOLUTION_ENGINE_VERSION
from book_translator.final_qa import (
    FINAL_QA_ANALYSIS_VERSION,
    FINAL_QA_AUDIT_ENGINE_VERSION,
    FINAL_QA_ORCHESTRATOR_VERSION,
    FINAL_QA_RESOLUTION_POLICY_VERSION,
    REPAIR_ESCALATION_VERSION,
)
from book_translator.historical_reevaluation_policy import (
    HISTORICAL_REEVALUATION_POLICY_VERSION,
)
from book_translator.normalization import NORMALIZATION_VERSION
from book_translator.paragraph_alignment import ALIGNMENT_QA_VERSION, EN_FA_V1
from book_translator.paragraph_markers import PARAGRAPH_MARKER_CONTRACT_VERSION
from book_translator.provider_execution import PROVIDER_EXECUTOR_VERSION, PROVIDER_PREFLIGHT_VERSION
from book_translator.provider_execution.providers import (
    CLAUDE_CODE_ADAPTER_VERSION,
    CODEX_CLI_ADAPTER_VERSION,
    FAKE_PROVIDER_VERSION,
    OPENAI_RESPONSES_ADAPTER_VERSION,
)
from book_translator.provider_recovery_policy import PROVIDER_RECOVERY_POLICY_VERSION
from book_translator.release_assembly import RELEASE_ASSEMBLY_ENGINE_VERSION
from book_translator.release_assembly.waiver import RELEASE_WAIVER_VERSION
from book_translator.release_verification import RELEASE_VERIFICATION_ENGINE_VERSION
from book_translator.semantic_gate import SEMANTIC_GATE_ENGINE_VERSION, SEMANTIC_GATE_POLICY_VERSION
from book_translator.semantic_qa import (
    EN_FA_JUDGE_V1,
    JUDGE_PROMPT_VERSION,
    JUDGE_SCHEMA_VERSION,
    SEMANTIC_COVERAGE_POLICY_VERSION,
    SEMANTIC_JUDGE_ENGINE_VERSION,
)
from book_translator.semantic_qa.displacement import DISPLACEMENT_CLASSIFIER_VERSION
from book_translator.semantic_qa.engine import SECOND_LOOK_CONTEXT_POLICY_VERSION
from book_translator.semantic_revision import (
    REVISION_PROMPT_VERSION,
    SEMANTIC_REVISION_POLICY_VERSION,
)
from book_translator.semantic_revision.targeted import TARGETED_SEMANTIC_REVISION_VERSION
from book_translator.series_terminology.amendment import SERIES_POLICY_AMENDMENT_VERSION
from book_translator.source_quality_audit import (
    AUDIT_ENGINE_VERSION,
    INTERRUPTED_TOKEN_DETECTOR_VERSION,
    JOINED_TOKEN_DETECTOR_VERSION,
)
from book_translator.structural_repair import STRUCTURAL_REPAIR_ENGINE_VERSION
from book_translator.terminology.concept_precedence import CONCEPT_PRECEDENCE_VERSION
from book_translator.terminology.exemptions import TERMINOLOGY_EXEMPTION_ENGINE_VERSION
from book_translator.terminology.questions import QUESTION_ENGINE_VERSION
from book_translator.terminology.residual_reconciliation import (
    RESIDUAL_RECONCILIATION_ENGINE_VERSION,
)
from book_translator.terminology.validation import (
    STRUCTURAL_QA_VERSION,
    TERMINOLOGY_QA_VERSION,
)
from book_translator.translation_auto_repair import AUTO_REPAIR_ENGINE_VERSION
from book_translator.translation_comparison import TRANSLATION_COMPARISON_VERSION
from book_translator.translation_historical_reevaluation import (
    HISTORICAL_REEVALUATION_ENGINE_VERSION,
)
from book_translator.translation_migration import TRANSLATION_MIGRATION_VERSION
from book_translator.translation_provider_recovery import PROVIDER_RECOVERY_ENGINE_VERSION
from book_translator.translation_queue import TRANSLATION_QUEUE_VERSION
from book_translator.translation_repair import (
    MARKED_REPAIR_PROMPT_VERSION,
    REPAIR_PROMPT_VERSION,
)
from book_translator.translation_repair_policy import AUTO_REPAIR_POLICY_VERSION
from book_translator.translation_reuse import TRANSLATION_REUSE_ENGINE_VERSION
from book_translator.translation_reuse_policy import REUSE_COMPATIBILITY_POLICY_VERSION
from book_translator.translation_snapshot import SNAPSHOT_ENGINE_VERSION
from book_translator.translation_worker import TRANSLATION_WORKER_VERSION
from book_translator.translation_worker_pool import WORKER_POOL_VERSION

ROOT = Path(__file__).parents[1]
SEMANTIC_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
ENGINE_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?$")


def test_application_version_is_valid_and_synchronized() -> None:
    version_bytes = (ROOT / "VERSION").read_bytes()
    version = version_bytes.decode("utf-8").rstrip("\n")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert version_bytes == b"0.5.0\n"
    assert SEMANTIC_VERSION.fullmatch(version)
    assert project["project"]["version"] == version
    assert __version__ == version


def test_required_governance_files_exist() -> None:
    required = {
        "ARCHITECTURE.md",
        "CHANGELOG.md",
        "ROADMAP.md",
        "VERSION",
        "docs/developer-charter.md",
        "docs/coding-standards.md",
        "docs/testing.md",
        "docs/review-process.md",
        "docs/benchmark.md",
        "docs/release-process.md",
    }

    assert all((ROOT / relative).is_file() for relative in required)


def test_agents_links_required_governance_entry_points() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    required_references = {
        "ARCHITECTURE.md",
        "docs/developer-charter.md",
        "docs/coding-standards.md",
        "docs/testing.md",
        "docs/review-process.md",
        "docs/benchmark.md",
        "docs/release-process.md",
        "ROADMAP.md",
        "CHANGELOG.md",
    }

    assert all(reference in agents for reference in required_references)


def test_implemented_engine_versions_are_explicit_and_stable() -> None:
    assert SEMANTIC_VERSION.fullmatch(BOOK_METADATA_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(BOOK_IDENTITY_MIGRATION_VERSION)
    assert ENGINE_VERSION.fullmatch(EXTRACTION_VERSION)
    assert ENGINE_VERSION.fullmatch(NORMALIZATION_VERSION)
    assert SEMANTIC_VERSION.fullmatch(CHUNK_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(SNAPSHOT_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(TRANSLATION_QUEUE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(PROVIDER_EXECUTOR_VERSION)
    assert SEMANTIC_VERSION.fullmatch(EXECUTION_RESOLUTION_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(FAKE_PROVIDER_VERSION)
    assert SEMANTIC_VERSION.fullmatch(OPENAI_RESPONSES_ADAPTER_VERSION)
    assert SEMANTIC_VERSION.fullmatch(CODEX_CLI_ADAPTER_VERSION)
    assert SEMANTIC_VERSION.fullmatch(CLAUDE_CODE_ADAPTER_VERSION)
    assert SEMANTIC_VERSION.fullmatch(PROVIDER_PREFLIGHT_VERSION)
    assert SEMANTIC_VERSION.fullmatch(RESIDUAL_RECONCILIATION_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(QUESTION_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(TRANSLATION_REUSE_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(REUSE_COMPATIBILITY_POLICY_VERSION)
    assert SEMANTIC_VERSION.fullmatch(HISTORICAL_REEVALUATION_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(HISTORICAL_REEVALUATION_POLICY_VERSION)
    assert SEMANTIC_VERSION.fullmatch(TRANSLATION_MIGRATION_VERSION)
    assert SEMANTIC_VERSION.fullmatch(SERIES_POLICY_AMENDMENT_VERSION)
    assert SEMANTIC_VERSION.fullmatch(TERMINOLOGY_EXEMPTION_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(CONCEPT_PRECEDENCE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(REPAIR_PROMPT_VERSION)
    assert SEMANTIC_VERSION.fullmatch(MARKED_REPAIR_PROMPT_VERSION)
    assert SEMANTIC_VERSION.fullmatch(AUTO_REPAIR_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(TRANSLATION_WORKER_VERSION)
    assert SEMANTIC_VERSION.fullmatch(WORKER_POOL_VERSION)
    assert SEMANTIC_VERSION.fullmatch(AUTO_REPAIR_POLICY_VERSION)
    assert SEMANTIC_VERSION.fullmatch(PROVIDER_RECOVERY_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(PROVIDER_RECOVERY_POLICY_VERSION)
    assert SEMANTIC_VERSION.fullmatch(FINAL_QA_RESOLUTION_POLICY_VERSION)
    assert SEMANTIC_VERSION.fullmatch(FINAL_QA_ANALYSIS_VERSION)
    assert SEMANTIC_VERSION.fullmatch(FINAL_QA_AUDIT_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(FINAL_QA_ORCHESTRATOR_VERSION)
    assert SEMANTIC_VERSION.fullmatch(REPAIR_ESCALATION_VERSION)
    assert SEMANTIC_VERSION.fullmatch(STRUCTURAL_REPAIR_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(RELEASE_ASSEMBLY_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(RELEASE_WAIVER_VERSION)
    assert SEMANTIC_VERSION.fullmatch(AUDIT_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(INTERRUPTED_TOKEN_DETECTOR_VERSION)
    assert SEMANTIC_VERSION.fullmatch(JOINED_TOKEN_DETECTOR_VERSION)
    assert SEMANTIC_VERSION.fullmatch(RELEASE_VERIFICATION_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(EPUB_BUILDER_VERSION)
    assert SEMANTIC_VERSION.fullmatch(CHAPTER_TITLE_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(CHAPTER_TITLE_PROMPT_VERSION)
    assert SEMANTIC_VERSION.fullmatch(CHAPTER_TITLE_QA_VERSION)
    assert SEMANTIC_VERSION.fullmatch(CHAPTER_TITLE_REVISION_VERSION)
    assert SEMANTIC_VERSION.fullmatch(TERMINOLOGY_QA_VERSION)
    assert SEMANTIC_VERSION.fullmatch(STRUCTURAL_QA_VERSION)
    assert SEMANTIC_VERSION.fullmatch(ALIGNMENT_QA_VERSION)
    assert SEMANTIC_VERSION.fullmatch(PARAGRAPH_MARKER_CONTRACT_VERSION)
    assert SEMANTIC_VERSION.fullmatch(EN_FA_V1.policy_version)
    assert SEMANTIC_VERSION.fullmatch(SEMANTIC_JUDGE_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(JUDGE_PROMPT_VERSION)
    assert SEMANTIC_VERSION.fullmatch(JUDGE_SCHEMA_VERSION)
    assert SEMANTIC_VERSION.fullmatch(SEMANTIC_COVERAGE_POLICY_VERSION)
    assert SEMANTIC_VERSION.fullmatch(EN_FA_JUDGE_V1.policy_version)
    assert SEMANTIC_VERSION.fullmatch(SEMANTIC_GATE_ENGINE_VERSION)
    assert SEMANTIC_VERSION.fullmatch(SEMANTIC_GATE_POLICY_VERSION)
    assert SEMANTIC_VERSION.fullmatch(SECOND_LOOK_CONTEXT_POLICY_VERSION)
    assert SEMANTIC_VERSION.fullmatch(REVISION_PROMPT_VERSION)
    assert SEMANTIC_VERSION.fullmatch(SEMANTIC_REVISION_POLICY_VERSION)
    assert SEMANTIC_VERSION.fullmatch(TARGETED_SEMANTIC_REVISION_VERSION)
    assert SEMANTIC_VERSION.fullmatch(DISPLACEMENT_CLASSIFIER_VERSION)
    assert SEMANTIC_VERSION.fullmatch(TRANSLATION_COMPARISON_VERSION)


def test_existing_cli_commands_remain_registered() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in {
        "status",
        "inspect-pdf",
        "extract-pages",
        "discover-toc",
        "extract-chapters",
        "normalize-chapters",
        "chunk-chapters",
        "resolve-book-metadata",
        "migrate-book-identity",
        "freeze-translation-resources",
        "create-translation-snapshot",
        "validate-translation-snapshot",
        "list-translation-snapshots",
        "create-translation-run",
        "list-translation-runs",
        "show-translation-run",
        "list-translation-jobs",
        "check-translation-provider",
        "claim-translation-job",
        "release-translation-job",
        "retry-translation-job",
        "recover-translation-run",
        "resolve-translation-reservation",
        "inspect-translation-reservations",
        "audit-terminology-exact-enforcement",
        "audit-terminology-registry-remediation",
        "plan-terminology-remediation",
        "apply-terminology-remediation",
        "show-terminology-remediation-plan",
        "show-terminology-remediation-operation",
        "build-terminology-conflict-graph",
        "show-terminology-conflict-component",
        "show-terminology-reconciliation-run",
        "show-terminology-reconciliation-provenance",
        "reject-terminology-candidate",
        "open-terminology-question",
        "list-terminology-questions",
        "resolve-terminology-question",
        "correct-terminology-decision",
        "report-terminology-readiness",
        "analyze-terminology-impact",
        "reevaluate-translation-job",
        "reuse-accepted-translations",
        "reevaluate-historical-output",
        "migrate-translation-run",
        "amend-series-terminology",
        "exempt-terminology-occurrence",
        "audit-final-qa",
        "finalize-translation-run",
        "export-translation-run",
        "authorize-release-waiver",
        "translate-chapter-titles",
        "revise-chapter-titles",
        "verify-release",
        "audit-paragraph-alignment",
        "audit-semantic-coverage",
        "run-semantic-gate",
        "revise-translation-paragraph",
        "compare-translation-runs",
        "build-epub",
    }:
        assert command in result.stdout
