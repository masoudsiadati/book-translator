"""Deterministic canonical workspace paths without filesystem side effects."""

import re
from dataclasses import dataclass
from pathlib import Path

from book_translator.models.book import TocKind

#: Canonical directory name and chapter ID of the prologue narrative unit.
PROLOGUE_CHAPTER_ID = "000-prologue"
#: Canonical directory name and chapter ID of the epilogue narrative unit. It sorts after every
#: numbered chapter and can never collide with one because numbered names are exactly three digits.
EPILOGUE_CHAPTER_ID = "999-epilogue"
#: TOC kinds that form the narrative translation corpus; everything else is excluded by default.
NARRATIVE_KINDS: frozenset[TocKind] = frozenset(
    {TocKind.PROLOGUE, TocKind.CHAPTER, TocKind.EPILOGUE}
)

_BOOK_WORKSPACE_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")
_NUMBERED_CHAPTER_ID = re.compile(r"[0-9]{3}")


def book_workspace_id(value: str) -> str:
    """Validate the ID of a book-scoped workspace under ``workspace/books/<id>/``."""
    if not _BOOK_WORKSPACE_ID.fullmatch(value) or value in {".", ".."}:
        raise ValueError(
            "book workspace ID must be filesystem-safe lowercase ASCII "
            "(letters, digits, '.', '_', '-') and start with a letter or digit"
        )
    return value


def chapter_directory_name(
    number: int | None = None, *, prologue: bool = False, epilogue: bool = False
) -> str:
    if prologue and epilogue:
        raise ValueError("a chapter cannot be both prologue and epilogue")
    if prologue:
        if number not in (None, 0):
            raise ValueError("prologue number must be omitted or zero")
        return PROLOGUE_CHAPTER_ID
    if epilogue:
        if number is not None:
            raise ValueError("epilogue number must be omitted")
        return EPILOGUE_CHAPTER_ID
    if number is None or not 1 <= number <= 999:
        raise ValueError("chapter number must be between 1 and 999")
    return f"{number:03d}"


def narrative_chapter_id(kind: TocKind, number: int | None) -> str:
    """Derive the canonical chapter ID of one narrative TOC entry."""
    if kind is TocKind.PROLOGUE:
        return chapter_directory_name(number, prologue=True)
    if kind is TocKind.EPILOGUE:
        return chapter_directory_name(number, epilogue=True)
    if kind is TocKind.CHAPTER:
        return chapter_directory_name(number)
    raise ValueError(f"TOC kind {kind.value} has no narrative chapter identity")


def parse_narrative_chapter_id(chapter_id: str) -> tuple[TocKind, int | None]:
    """Invert `narrative_chapter_id`; the prologue carries number 0 and the epilogue none."""
    if chapter_id == PROLOGUE_CHAPTER_ID:
        return TocKind.PROLOGUE, 0
    if chapter_id == EPILOGUE_CHAPTER_ID:
        return TocKind.EPILOGUE, None
    if _NUMBERED_CHAPTER_ID.fullmatch(chapter_id) and chapter_id != "000":
        return TocKind.CHAPTER, int(chapter_id)
    raise ValueError(f"not a canonical narrative chapter ID: {chapter_id!r}")


def is_narrative_chapter_id(name: str) -> bool:
    try:
        parse_narrative_chapter_id(name)
    except ValueError:
        return False
    return True


def chapter_id_from_selector(selector: str) -> str:
    """Resolve the operator selector forms shared by every stage to one canonical chapter ID."""
    normalized = selector.strip().lower()
    if normalized in {"prologue", "0", PROLOGUE_CHAPTER_ID}:
        return PROLOGUE_CHAPTER_ID
    if normalized in {"epilogue", EPILOGUE_CHAPTER_ID}:
        return EPILOGUE_CHAPTER_ID
    if normalized.isdigit():
        number = int(normalized)
    elif re.fullmatch(r"chapter-\d+", normalized):
        number = int(normalized.removeprefix("chapter-"))
    else:
        raise ValueError(f"invalid chapter selector {selector!r}")
    if not 1 <= number <= 999:
        raise ValueError(f"invalid chapter selector {selector!r}")
    return f"{number:03d}"


def book_workspace_of(relative_path: Path) -> str | None:
    """Recover the book workspace ID a persisted project-relative path belongs to, if any.

    Legacy `workspace/...` paths return None. Used so that an immutable record such as a snapshot
    manifest can name the workspace it was frozen from without a caller having to guess.
    """
    parts = relative_path.parts
    if len(parts) >= 3 and parts[0] == "workspace" and parts[1] == "books":
        return book_workspace_id(parts[2])
    return None


def chunk_directory_name(sequence: int) -> str:
    if not 1 <= sequence <= 999:
        raise ValueError("chunk sequence must be between 1 and 999")
    return f"{sequence:03d}"


def front_matter_directory_name(entry_id: str) -> str:
    if (
        not entry_id.startswith("front-matter-")
        or not entry_id.removeprefix("front-matter-").isdigit()
    ):
        raise ValueError("front matter entry ID must be front-matter- followed by digits")
    return entry_id


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """Derive paths rooted at a project; never create them.

    Without a `book`, every path resolves under the legacy `workspace/` root exactly as before.
    With a `book`, the same layout is rooted under `workspace/books/<book>/`, so two books with
    identical logical unit IDs (both have a `000-prologue`) can never share an artifact path.
    """

    project_root: Path
    book: str | None = None

    def __post_init__(self) -> None:
        if self.book is not None:
            book_workspace_id(self.book)

    @property
    def book_workspaces(self) -> Path:
        """The shared container of every book-scoped workspace; never a workspace itself."""
        return self.project_root / "workspace" / "books"

    @property
    def workspace(self) -> Path:
        if self.book is None:
            return self.project_root / "workspace"
        return self.book_workspaces / self.book

    @property
    def book_config(self) -> Path:
        """The project configuration describing the selected book."""
        if self.book is None:
            return self.project_root / "config" / "book.yaml"
        return self.project_root / "config" / "books" / f"{self.book}.yaml"

    @property
    def book_metadata(self) -> Path:
        return self.workspace / "metadata.yaml"

    @property
    def toc(self) -> Path:
        return self.workspace / "toc.yaml"

    @property
    def snapshot(self) -> Path:
        return self.workspace / "snapshot.yaml"

    @property
    def translation_snapshots(self) -> Path:
        return self.workspace / "snapshots" / "translation"

    @property
    def source_quality_audits(self) -> Path:
        return self.workspace / "audits" / "source-quality"

    @property
    def final_qa_audits(self) -> Path:
        return self.workspace / "audits" / "final-qa"

    @property
    def paragraph_alignment_audits(self) -> Path:
        return self.workspace / "audits" / "paragraph-alignment"

    def paragraph_alignment_audit(self, run_id: str) -> Path:
        """Derive the read-only paragraph alignment audit of one translation run."""
        return self.paragraph_alignment_audits / self.translation_run(run_id).name

    @property
    def semantic_coverage_audits(self) -> Path:
        return self.workspace / "audits" / "semantic-coverage"

    def semantic_coverage_audit(self, run_id: str) -> Path:
        """Derive the semantic coverage evaluations of one translation run."""
        return self.semantic_coverage_audits / self.translation_run(run_id).name

    def final_qa_audit(self, run_id: str) -> Path:
        """Derive the read-only final-QA diagnosis of one translation run."""
        return self.final_qa_audits / self.translation_run(run_id).name

    def final_qa_finalization(self, run_id: str, finalization_number: int) -> Path:
        """Derive the durable record of one finalization of that run."""
        if finalization_number < 1 or finalization_number > 999:
            raise ValueError("finalization number must be between 1 and 999")
        return self.final_qa_audit(run_id) / "finalizations" / f"{finalization_number:03d}"

    def translation_snapshot(self, snapshot_id: str) -> Path:
        if not snapshot_id or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in snapshot_id
        ):
            raise ValueError("snapshot ID must be filesystem-safe lowercase ASCII")
        return self.translation_snapshots / snapshot_id

    @property
    def terminology_remediation_plans(self) -> Path:
        return self.workspace / "terminology-remediation"

    def terminology_remediation_plan(self, plan_id: str) -> Path:
        if not re.fullmatch(r"trp-[0-9a-f]{24}", plan_id):
            raise ValueError("invalid terminology remediation plan ID")
        return self.terminology_remediation_plans / plan_id

    def terminology_remediation_lineage(self, plan_id: str) -> Path:
        """Immutable evidence a third party needs to re-verify a plan without the session."""
        return self.terminology_remediation_plan(plan_id) / "lineage"

    @property
    def frozen_resources(self) -> Path:
        return self.workspace / "resources"

    def frozen_resource_category(self, category: str) -> Path:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", category):
            raise ValueError("invalid frozen resource category")
        return self.frozen_resources / category

    def frozen_resource(self, category: str, content_sha256: str, suffix: str = ".yaml") -> Path:
        """Derive the durable content-addressed copy of one frozen translation resource."""
        if not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
            raise ValueError("invalid frozen resource content hash")
        if not re.fullmatch(r"\.[a-z0-9]+", suffix):
            raise ValueError("invalid frozen resource suffix")
        return self.frozen_resource_category(category) / f"{content_sha256}{suffix}"

    @property
    def chapter_title_artifacts(self) -> Path:
        """Chapter-title artifacts are directory-shaped frozen resources, one per identity."""
        return self.frozen_resource_category("chapter-titles")

    def chapter_title_artifact(self, artifact_id: str) -> Path:
        if not re.fullmatch(r"ct-[0-9a-f]{24}", artifact_id):
            raise ValueError("invalid chapter title artifact ID")
        return self.chapter_title_artifacts / artifact_id

    @property
    def terminology_resources(self) -> Path:
        return self.frozen_resource_category("terminology")

    def terminology_resource(self, content_sha256: str) -> Path:
        return self.frozen_resource("terminology", content_sha256)

    @property
    def open_question_resources(self) -> Path:
        return self.frozen_resource_category("open-questions")

    def open_question_resource(self, content_sha256: str) -> Path:
        return self.frozen_resource("open-questions", content_sha256)

    # Series-aware terminology: the derived book registry and the per-chunk reference packages
    # are book-scoped derived artifacts; the frozen categories hold their content-addressed copies.
    @property
    def book_terminology_directory(self) -> Path:
        return self.workspace / "terminology"

    @property
    def book_terminology_registry(self) -> Path:
        return self.book_terminology_directory / "registry.yaml"

    @property
    def book_terminology_exemptions(self) -> Path:
        """User-adjudicated occurrence exemptions; a snapshot freezes it when it exists."""
        return self.book_terminology_directory / "occurrence-exemptions.yaml"

    @property
    def reference_context_directory(self) -> Path:
        return self.workspace / "reference-context"

    @property
    def reference_context_index(self) -> Path:
        return self.reference_context_directory / "index.yaml"

    @property
    def reference_context_packages(self) -> Path:
        """Per-chunk reference packages are immutable and content-addressed from birth."""
        return self.frozen_resource_category("reference-context")

    def reference_context_package(self, content_sha256: str) -> Path:
        return self.frozen_resource("reference-context", content_sha256)

    @property
    def book_identity_migrations(self) -> Path:
        return self.workspace / "migrations" / "book-identity"

    def book_identity_migration(self, migration_id: str) -> Path:
        if not re.fullmatch(r"bim-[0-9a-f]{24}", migration_id):
            raise ValueError("invalid book identity migration ID")
        return self.book_identity_migrations / f"{migration_id}.yaml"

    @property
    def book_metadata_resources(self) -> Path:
        return self.frozen_resource_category("book-metadata")

    def book_metadata_resource(self, content_sha256: str) -> Path:
        return self.frozen_resource("book-metadata", content_sha256)

    @property
    def terminology_decision_corrections(self) -> Path:
        return self.workspace / "terminology-corrections"

    def terminology_decision_correction(self, correction_id: str) -> Path:
        if not re.fullmatch(r"tdc-[0-9a-f]{24}", correction_id):
            raise ValueError("invalid terminology decision correction ID")
        return self.terminology_decision_corrections / f"{correction_id}.yaml"

    @property
    def series_policy_amendments(self) -> Path:
        """Project-wide evidence of governed series-policy edits; a policy serves many books."""
        return self.project_root / "workspace" / "series-terminology-amendments"

    def series_policy_amendment(self, amendment_id: str) -> Path:
        if not re.fullmatch(r"spa-[0-9a-f]{24}", amendment_id):
            raise ValueError("invalid series policy amendment ID")
        return self.series_policy_amendments / f"{amendment_id}.yaml"

    @property
    def releases(self) -> Path:
        """The default home for assembled releases, which are derived and never canonical."""
        return self.workspace / "releases"

    @property
    def release_waivers(self) -> Path:
        """Operator-authorized release waivers, addressed by their own content."""
        return self.workspace / "release-waivers"

    def release_waiver(self, waiver_id: str) -> Path:
        if not re.fullmatch(r"rw-[0-9a-f]{24}", waiver_id):
            raise ValueError("invalid release waiver ID")
        return self.release_waivers / f"{waiver_id}.yaml"

    def release(self, release_id: str) -> Path:
        if not release_id or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in release_id
        ):
            raise ValueError("release ID must be filesystem-safe lowercase ASCII")
        return self.releases / release_id

    @property
    def translation_runs(self) -> Path:
        return self.workspace / "translation-runs"

    def translation_run(self, run_id: str) -> Path:
        if not re.fullmatch(r"tr-[0-9a-f]{12}-\d{3,}", run_id):
            raise ValueError("invalid translation run ID")
        return self.translation_runs / run_id

    @property
    def literary_runs(self) -> Path:
        """Immutable chapter-level literary derivatives, separate from translation runs."""
        return self.workspace / "literary-runs"

    def literary_run(self, literary_run_id: str) -> Path:
        if not re.fullmatch(r"lr-[0-9a-f]{24}", literary_run_id):
            raise ValueError("invalid literary run ID")
        return self.literary_runs / literary_run_id

    def prompt_package(self, run_id: str, chunk_id: str) -> Path:
        run = self.translation_run(run_id)
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", chunk_id):
            raise ValueError("invalid chunk ID")
        return run / "jobs" / chunk_id / "prompt-package"

    def translation_attempt(self, run_id: str, chunk_id: str, attempt_number: int) -> Path:
        if attempt_number < 1 or attempt_number > 999:
            raise ValueError("attempt number must be between 1 and 999")
        package = self.prompt_package(run_id, chunk_id)
        return package.parent / "attempts" / f"{attempt_number:03d}"

    def translation_execution_resolution(
        self, run_id: str, chunk_id: str, attempt_number: int
    ) -> Path:
        """Derive the immutable operator resolution record for one attempt slot."""
        if attempt_number < 1 or attempt_number > 999:
            raise ValueError("attempt number must be between 1 and 999")
        package = self.prompt_package(run_id, chunk_id)
        return package.parent / "resolutions" / f"{attempt_number:03d}"

    def translation_acceptance(self, run_id: str, chunk_id: str, attempt_number: int) -> Path:
        if attempt_number < 1 or attempt_number > 999:
            raise ValueError("attempt number must be between 1 and 999")
        package = self.prompt_package(run_id, chunk_id)
        return package.parent / "acceptance" / f"{attempt_number:03d}"

    def translation_auto_repair_decision(
        self, run_id: str, chunk_id: str, attempt_number: int
    ) -> Path:
        """Derive the authorization record for the repair attempt occupying one attempt slot."""
        if attempt_number < 2 or attempt_number > 999:
            raise ValueError("repair attempt number must be between 2 and 999")
        package = self.prompt_package(run_id, chunk_id)
        return package.parent / "auto-repair" / f"{attempt_number:03d}"

    def translation_provider_recovery_decision(
        self, run_id: str, chunk_id: str, attempt_number: int
    ) -> Path:
        """Derive the authorization record for the recovery attempt occupying one attempt slot."""
        if attempt_number < 2 or attempt_number > 999:
            raise ValueError("recovery attempt number must be between 2 and 999")
        package = self.prompt_package(run_id, chunk_id)
        return package.parent / "provider-recovery" / f"{attempt_number:03d}"

    def translation_repair_escalation(
        self, run_id: str, chunk_id: str, escalation_number: int
    ) -> Path:
        """Derive the bounded exceptional grant of extra repair attempts to one job."""
        if escalation_number < 1 or escalation_number > 999:
            raise ValueError("escalation number must be between 1 and 999")
        package = self.prompt_package(run_id, chunk_id)
        return package.parent / "repair-escalation" / f"{escalation_number:03d}"

    def translation_reevaluation(
        self, run_id: str, chunk_id: str, reevaluation_number: int
    ) -> Path:
        if reevaluation_number < 1 or reevaluation_number > 999:
            raise ValueError("re-evaluation number must be between 1 and 999")
        package = self.prompt_package(run_id, chunk_id)
        return package.parent / "reevaluations" / f"{reevaluation_number:03d}"

    def translation_reuse(self, run_id: str, chunk_id: str, reuse_number: int) -> Path:
        if reuse_number < 1 or reuse_number > 999:
            raise ValueError("reuse number must be between 1 and 999")
        return self.prompt_package(run_id, chunk_id).parent / "reuse" / f"{reuse_number:03d}"

    def translation_carry_forward(
        self, run_id: str, chunk_id: str, carry_forward_number: int
    ) -> Path:
        """Derive the record of one historical rejected output re-evaluated into this run."""
        if carry_forward_number < 1 or carry_forward_number > 999:
            raise ValueError("carry-forward number must be between 1 and 999")
        package = self.prompt_package(run_id, chunk_id)
        return package.parent / "carry-forward" / f"{carry_forward_number:03d}"

    def translation_structural_repair(self, run_id: str, chunk_id: str, repair_number: int) -> Path:
        """Derive the record of one provider-free structural repair of this job's translation."""
        if repair_number < 1 or repair_number > 999:
            raise ValueError("structural repair number must be between 1 and 999")
        package = self.prompt_package(run_id, chunk_id)
        return package.parent / "structural-repair" / f"{repair_number:03d}"

    @property
    def chapters(self) -> Path:
        return self.workspace / "chapters"

    def chapter(
        self, number: int | None = None, *, prologue: bool = False, epilogue: bool = False
    ) -> Path:
        return self.chapters / chapter_directory_name(number, prologue=prologue, epilogue=epilogue)

    def chapter_directory(self, chapter_id: str) -> Path:
        """Derive the directory of one narrative chapter from its canonical ID."""
        parse_narrative_chapter_id(chapter_id)
        return self.chapters / chapter_id

    def front_matter(self, entry_id: str) -> Path:
        return self.chapters / front_matter_directory_name(entry_id)

    def chapter_metadata(self, number: int | None = None, *, prologue: bool = False) -> Path:
        return self.chapter(number, prologue=prologue) / "metadata.yaml"

    def chapter_source(self, number: int | None = None, *, prologue: bool = False) -> Path:
        return self.chapter(number, prologue=prologue) / "source.md"

    def chapter_cleaned(self, number: int | None = None, *, prologue: bool = False) -> Path:
        return self.chapter(number, prologue=prologue) / "cleaned.md"

    def chapter_normalization_report(
        self, number: int | None = None, *, prologue: bool = False
    ) -> Path:
        return self.chapter(number, prologue=prologue) / "normalization-report.yaml"

    def chapter_chunks(self, number: int | None = None, *, prologue: bool = False) -> Path:
        return self.chapter(number, prologue=prologue) / "chunks"

    def chapter_chunking_report(self, number: int | None = None, *, prologue: bool = False) -> Path:
        return self.chapter(number, prologue=prologue) / "chunking-report.yaml"

    def chapter_review(self, number: int | None = None, *, prologue: bool = False) -> Path:
        return self.chapter(number, prologue=prologue) / "review"

    def chapter_final(self, number: int | None = None, *, prologue: bool = False) -> Path:
        return self.chapter(number, prologue=prologue) / "final.md"

    def chunk(self, chapter_number: int | None, sequence: int, *, prologue: bool = False) -> Path:
        return (
            self.chapter(chapter_number, prologue=prologue)
            / "chunks"
            / chunk_directory_name(sequence)
        )

    def chunk_metadata(
        self, chapter_number: int | None, sequence: int, *, prologue: bool = False
    ) -> Path:
        return self.chunk(chapter_number, sequence, prologue=prologue) / "metadata.yaml"

    def chunk_source(
        self, chapter_number: int | None, sequence: int, *, prologue: bool = False
    ) -> Path:
        return self.chunk(chapter_number, sequence, prologue=prologue) / "source.md"

    def chunk_context(
        self, chapter_number: int | None, sequence: int, *, prologue: bool = False
    ) -> Path:
        return self.chunk(chapter_number, sequence, prologue=prologue) / "context.md"

    def chunk_translation(
        self, chapter_number: int | None, sequence: int, *, prologue: bool = False
    ) -> Path:
        return self.chunk(chapter_number, sequence, prologue=prologue) / "translation.md"
