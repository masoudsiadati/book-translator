import re
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from book_translator import __version__
from book_translator.chapter_extraction import EXTRACTION_VERSION
from book_translator.cli import app
from book_translator.normalization import NORMALIZATION_VERSION

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
    assert ENGINE_VERSION.fullmatch(EXTRACTION_VERSION)
    assert ENGINE_VERSION.fullmatch(NORMALIZATION_VERSION)


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
    }:
        assert command in result.stdout
