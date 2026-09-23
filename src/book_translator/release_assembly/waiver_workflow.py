"""Publishing and reading a release-time semantic-gate waiver.

Authorizing one is its own act, separate from the release that uses it: a person decides it, it
is written once under its own content address, and the release that later presents it proves it
still describes the run it names. Nothing here can create, change or infer a semantic verdict,
and nothing here touches a job, an acceptance, a snapshot or a title artifact.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.models import ReleaseSemanticWaiver, TranslationJob, TranslationRun
from book_translator.paths import ProjectPaths
from book_translator.release_assembly.waiver import (
    ReleaseWaiverError,
    SemanticCoverage,
    semantic_coverage,
    waiver_for,
    waiver_refusals,
)

WAIVER_SUFFIX = ".yaml"

__all__ = [
    "ReleaseWaiverPlan",
    "execute_release_waiver",
    "load_release_waiver",
    "plan_release_waiver",
    "verified_waiver",
]


def _serialize(waiver: ReleaseSemanticWaiver) -> str:
    rendered = yaml.safe_dump(
        waiver.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise ReleaseWaiverError("could not serialize a release waiver")
    return rendered


def waiver_path(project_root: Path, waiver_id: str) -> Path:
    return ProjectPaths(project_root).release_waiver(waiver_id)


def load_release_waiver(path: Path) -> ReleaseSemanticWaiver:
    """Read one waiver record, proving it is a waiver and not something shaped like one."""
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return ReleaseSemanticWaiver.model_validate(payload)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise ReleaseWaiverError(f"unreadable release waiver {path}: {exc}") from exc


def resolve_release_waiver(
    project_root: Path, waiver_id_or_path: str | Path
) -> tuple[Path, ReleaseSemanticWaiver]:
    candidate = Path(waiver_id_or_path)
    path = (
        candidate.resolve()
        if candidate.is_absolute() or len(candidate.parts) > 1
        else waiver_path(project_root, str(waiver_id_or_path))
    )
    return path, load_release_waiver(path)


def verified_waiver(
    project_root: Path,
    waiver_id_or_path: str | Path,
    *,
    run: TranslationRun,
    jobs: tuple[TranslationJob, ...],
) -> tuple[Path, ReleaseSemanticWaiver, str]:
    """Load the waiver and prove it still describes this exact run, or fail closed."""
    path, waiver = resolve_release_waiver(project_root, waiver_id_or_path)
    refusals = waiver_refusals(waiver, run, jobs)
    if refusals:
        raise ReleaseWaiverError(
            "the semantic-gate waiver does not authorize this release: " + ", ".join(refusals)
        )
    return path, waiver, hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReleaseWaiverPlan:
    project_root: Path
    path: Path
    waiver: ReleaseSemanticWaiver
    coverage: SemanticCoverage
    action: str

    @property
    def waiver_id(self) -> str:
        return self.waiver.waiver_id


def plan_release_waiver(
    run: TranslationRun,
    jobs: tuple[TranslationJob, ...],
    *,
    project_root: Path,
    reason: str,
    now: Callable[[], datetime] | None = None,
) -> ReleaseWaiverPlan:
    """Derive the waiver this run would need. Writes nothing."""
    project_root = project_root.resolve()
    waiver = waiver_for(run, jobs, reason=reason, created_at=(now or (lambda: datetime.now(UTC)))())
    path = waiver_path(project_root, waiver.waiver_id)
    action = "authorize"
    if path.exists():
        existing = load_release_waiver(path)
        if existing.model_dump(exclude={"created_at"}) != waiver.model_dump(exclude={"created_at"}):
            raise ReleaseWaiverError(
                f"a different waiver already holds this address: {waiver.waiver_id}"
            )
        action = "skip"
        waiver = existing
    return ReleaseWaiverPlan(
        project_root=project_root,
        path=path,
        waiver=waiver,
        coverage=semantic_coverage(jobs),
        action=action,
    )


def execute_release_waiver(plan: ReleaseWaiverPlan) -> ReleaseWaiverPlan:
    """Publish the waiver once. An identical one already on disk is reused, never rewritten."""
    if plan.action == "skip":
        return plan
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    if plan.path.exists():  # pragma: no cover - planning proved it absent or identical
        raise ReleaseWaiverError(f"release waiver already exists: {plan.path}")
    atomic_write_text(plan.path, _serialize(plan.waiver))
    return plan
