"""Regression guards for the authorized real-data pilot corpus in this repository.

These tests assert hashes, counts, and decision provenance only. They never read or compare book
prose, and they skip cleanly in a checkout that does not carry the private pilot resources.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from book_translator.book_identity import load_migrations
from book_translator.book_metadata import load_book_metadata, placeholder_fields
from book_translator.config import load_book_config
from book_translator.hashing import sha256_file
from book_translator.models import (
    OpenQuestionRegistry,
    QuestionStatus,
    SourceAnomalyRegistry,
    TerminologyRegistry,
    TranslationManifest,
)
from book_translator.source_anomalies import anomaly_identity
from book_translator.terminology.conflict_graph import detect_alias_cycles
from book_translator.terminology.lineage import (
    RegistryEventKind,
    explain_registry_lineage,
    registry_transitions,
)
from book_translator.terminology.question_workflow import load_corrections
from book_translator.terminology.registry import registry_identity
from book_translator.terminology.remediation_engine import load_applied_remediations
from book_translator.translation_snapshot import (
    ManifestIdentityFormat,
    persisted_manifest_identity,
    persisted_manifest_identity_format,
    plan_snapshot,
    validate_snapshot,
)

ROOT = Path(__file__).parents[1]
HISTORICAL_SNAPSHOT_ID = "translation-fa-e0bd2fda7a852ca6d42b3cff"
HISTORICAL_SNAPSHOT = ROOT / "workspace/snapshots/translation" / HISTORICAL_SNAPSHOT_ID
HISTORICAL_MANIFEST_SHA = "e0bd2fda7a852ca6d42b3cff9cf63193d106c060bc9e33317032a67693b8fe35"

# Exact bytes of the frozen historical snapshot, recorded before durable resource freezing existed.
HISTORICAL_FILE_HASHES = {
    "manifest.yaml": "8174eaf90447c4d2dd36b9fba61bf41beee56837a6550edabe411209c80930cd",
    "snapshot.yaml": "af88125f9243da2c13916fac14cbc0735d9887d8ec649d691d1ed0bee5db2ce9",
    "validation-report.yaml": "e6ef5c75501da9acb04f87d13226f325171df19b8c1baae625c900f92e9f2577",
}

EXPECTED_CHAPTERS = 57
EXPECTED_CHUNKS = 206

# The bytes each registry carried at migration time. Pinned so that a record rewritten to match a
# later state of the mutable registry fails here instead of passing silently.
HISTORICAL_MIGRATED_REGISTRY_HASHES = {
    "config/source-anomalies.yaml": (
        "a8f4929bf679f3b9177dbb0872ce29af443a8c04e5ae173adf9a8e21d61dabf9"
    ),
    "config/terminology-questions.yaml": (
        "93e2cd7c526c381dc7d6746c904320cdca5f67a6fd0a5c8c880f7e4c57f001f5"
    ),
    "config/terminology.yaml": ("b796823c16cb722e61598eb2565644c030303cc6df2018e17bf955015b21fbf9"),
}
HISTORICAL_MIGRATED_TERMINOLOGY_HASH = HISTORICAL_MIGRATED_REGISTRY_HASHES[
    "config/terminology.yaml"
]

# The governed remediation that moved the registry after the last decision correction, and the
# semantic identity it published as its result. Both are pinned so an ungoverned edit to the live
# registry fails here instead of being absorbed as "drift".
APPLIED_REMEDIATION_PLAN_ID = "trp-be33e0bf18466d1c124ed920"
CURRENT_TERMINOLOGY_SEMANTIC_SHA = (
    "6df76fe749e120e1f7cb9529a6cc0c4db88159175ac0be4ebcccac0027699ef0"
)

PILOT_RESOURCES = (
    ROOT / "config/terminology.yaml",
    ROOT / "config/terminology-questions.yaml",
    ROOT / "config/source-anomalies.yaml",
    ROOT / "input/book.pdf",
    ROOT / "workspace/metadata.yaml",
)

requires_pilot_corpus = pytest.mark.skipif(
    not all(path.is_file() for path in PILOT_RESOURCES),
    reason="the private pilot corpus is not present in this checkout",
)
requires_historical_snapshot = pytest.mark.skipif(
    not HISTORICAL_SNAPSHOT.is_dir(),
    reason="the historical pilot snapshot is not present in this checkout",
)


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@requires_historical_snapshot
def test_historical_snapshot_bytes_are_never_upgraded_in_place() -> None:
    persisted = {name: sha256_file(HISTORICAL_SNAPSHOT / name) for name in HISTORICAL_FILE_HASHES}
    raw_manifest = _load(HISTORICAL_SNAPSHOT / "manifest.yaml")
    manifest = TranslationManifest.model_validate(raw_manifest)
    snapshot_id, manifest_sha = persisted_manifest_identity(raw_manifest, manifest)

    assert persisted == HISTORICAL_FILE_HASHES
    assert snapshot_id == HISTORICAL_SNAPSHOT_ID
    assert manifest_sha == HISTORICAL_MANIFEST_SHA
    # It keeps its original contract: prompt/glossary/style were not durably frozen back then.
    assert (
        persisted_manifest_identity_format(raw_manifest)
        is ManifestIdentityFormat.FROZEN_TERMINOLOGY_V1
    )
    assert raw_manifest["prompt"]["frozen_file"] is None
    assert validate_snapshot(HISTORICAL_SNAPSHOT, project_root=ROOT).report.valid


@requires_pilot_corpus
def test_resolved_pilot_book_metadata_carries_no_placeholder_identity() -> None:
    metadata = load_book_metadata(ROOT)

    assert metadata is not None
    assert placeholder_fields(metadata) == ()
    assert metadata.title_en == "The Fires of Heaven"
    assert metadata.author == "Robert Jordan"
    assert metadata.series_name == "The Wheel of Time"
    assert metadata.series_index == 5
    assert metadata.source_language == "en"
    assert metadata.target_language == "fa"
    # The Persian title stays an explicit human decision rather than an invented value.
    assert metadata.title_fa == ""


@requires_pilot_corpus
@requires_historical_snapshot
def test_new_pilot_snapshot_is_distinct_and_preserves_chunk_lineage() -> None:
    historical = TranslationManifest.model_validate(_load(HISTORICAL_SNAPSHOT / "manifest.yaml"))
    plan = plan_snapshot(project_root=ROOT)
    manifest = plan.manifest

    assert plan.snapshot_id != HISTORICAL_SNAPSHOT_ID
    assert plan.manifest_sha256 != HISTORICAL_MANIFEST_SHA
    assert manifest.selection_mode == "full"
    assert len(manifest.chapters) == EXPECTED_CHAPTERS
    assert sum(chapter.chunk_count for chapter in manifest.chapters) == EXPECTED_CHUNKS
    # Real metadata changed identity; no normalized or chunk byte moved with it.
    assert [chapter.model_dump() for chapter in manifest.chapters] == [
        chapter.model_dump() for chapter in historical.chapters
    ]
    assert manifest.book.book_id == "the-fires-of-heaven-en-fa"
    assert manifest.book.metadata_file == Path("workspace/metadata.yaml")


@requires_pilot_corpus
def test_every_pilot_translation_resource_has_a_durable_frozen_reference() -> None:
    manifest = plan_snapshot(project_root=ROOT).manifest

    for name in (
        "prompt",
        "glossary",
        "style_guide",
        "terminology",
        "source_anomalies",
        "open_questions",
    ):
        reference = getattr(manifest, name)
        assert reference is not None, name
        assert reference.frozen_file is not None, name
        assert reference.sha256 == sha256_file(ROOT / reference.file), name
    assert manifest.book.frozen_metadata_file is not None


@requires_pilot_corpus
def test_open_question_registry_keeps_the_nonblocking_kinship_question() -> None:
    registry = OpenQuestionRegistry.model_validate(
        _load(ROOT / "config/terminology-questions.yaml")
    )
    manifest = plan_snapshot(project_root=ROOT).manifest

    question = next(item for item in registry.questions if item.source_term == "Aunt Resara")
    assert question.status is QuestionStatus.OPEN
    assert question.blocking is False
    assert manifest.open_questions is not None
    assert manifest.open_questions.sha256 == sha256_file(ROOT / "config/terminology-questions.yaml")


@requires_pilot_corpus
@pytest.mark.parametrize(
    ("source_term", "target_term", "decision_source", "decision_state", "locked"),
    [
        ("Aunt Resara", "خاله رسارا", "ai", "provisional", False),
        ("Ajah", "آجای", "user", "confirmed", True),
    ],
)
def test_terminology_decisions_survive_the_metadata_change(
    source_term: str, target_term: str, decision_source: str, decision_state: str, locked: bool
) -> None:
    registry = _load(ROOT / "config/terminology.yaml")
    candidates = {item["candidate_id"]: item["source_term"] for item in registry["candidates"]}
    decision = next(
        item
        for item in registry["decisions"]
        if candidates.get(item["candidate_id"]) == source_term
    )

    assert decision["target_term"] == target_term
    assert decision["decision_source"] == decision_source
    assert decision["decision_state"] == decision_state
    assert decision["locked"] is locked


CURRENT_AUTHORITATIVE_BOOK_ID = "the-fires-of-heaven-en-fa"
PLACEHOLDER_IDENTITY = (
    "example-fantasy-novel-en-fa",
    "Example Fantasy Novel",
    "رمان فانتزی نمونه",
)
CURRENT_AUTHORITATIVE_FILES = (
    Path("config/book.yaml"),
    Path("config/terminology.yaml"),
    Path("config/terminology-questions.yaml"),
    Path("config/source-anomalies.yaml"),
    Path("workspace/metadata.yaml"),
)


@requires_pilot_corpus
def test_every_current_authoritative_artifact_agrees_on_the_book_id() -> None:
    config = load_book_config(ROOT / "config/book.yaml")
    metadata = load_book_metadata(ROOT)
    registry = TerminologyRegistry.model_validate(_load(ROOT / "config/terminology.yaml"))
    questions = OpenQuestionRegistry.model_validate(
        _load(ROOT / "config/terminology-questions.yaml")
    )
    anomalies = SourceAnomalyRegistry.model_validate(_load(ROOT / "config/source-anomalies.yaml"))

    assert metadata is not None
    observed = {
        config.book_id,
        metadata.book_id,
        registry.book_id,
        questions.book_id,
        *{item.book_id for item in anomalies.anomalies},
    }
    assert observed == {CURRENT_AUTHORITATIVE_BOOK_ID}


@requires_pilot_corpus
@pytest.mark.parametrize("relative", CURRENT_AUTHORITATIVE_FILES)
def test_current_authoritative_state_carries_no_placeholder_identity(relative: Path) -> None:
    text = (ROOT / relative).read_text(encoding="utf-8")

    assert [marker for marker in PLACEHOLDER_IDENTITY if marker in text] == []


@requires_historical_snapshot
def test_historical_resources_intentionally_retain_the_previous_identity() -> None:
    """Frozen historical bytes are evidence, not drift, and are never migrated."""
    frozen = (
        ROOT
        / "workspace/resources/terminology"
        / ("487af81e70957984b1e8d7e3c37c718c8d477b3a60517ec05d10c0a9d4a3db70.yaml")
    )
    historical = TranslationManifest.model_validate(_load(HISTORICAL_SNAPSHOT / "manifest.yaml"))

    assert historical.book.book_id == "example-fantasy-novel-en-fa"
    assert sha256_file(frozen) == historical.terminology.sha256  # type: ignore[union-attr]
    assert "example-fantasy-novel-en-fa" in frozen.read_text(encoding="utf-8")


@requires_pilot_corpus
def test_book_identity_migration_lineage_is_recorded_and_auditable() -> None:
    records = load_migrations(ROOT)

    assert len(records) == 1
    record = records[0]
    assert record.previous_book_id == "example-fantasy-novel-en-fa"
    assert record.book_id == CURRENT_AUTHORITATIVE_BOOK_ID
    assert record.provenance == "canonical_book_metadata"
    assert record.semantic_content_unchanged
    assert record.book_metadata_sha256 == sha256_file(ROOT / record.book_metadata_file)
    assert [item.file.as_posix() for item in record.registries] == [
        "config/source-anomalies.yaml",
        "config/terminology-questions.yaml",
        "config/terminology.yaml",
    ]
    for item in record.registries:
        # The record is a historical claim about the bytes at migration time, not a promise that
        # the mutable registry stops there.  Its own hashes are what must never move.
        assert item.before_sha256 != item.after_sha256
        assert (ROOT / item.file).is_file()
        assert item.after_sha256 == HISTORICAL_MIGRATED_REGISTRY_HASHES[item.file.as_posix()]


@requires_pilot_corpus
def test_a_registry_untouched_since_the_migration_still_matches_its_recorded_bytes() -> None:
    record = load_migrations(ROOT)[0]
    unchanged = [
        item for item in record.registries if item.file.as_posix() != "config/terminology.yaml"
    ]

    assert [item.file.as_posix() for item in unchanged] == [
        "config/source-anomalies.yaml",
        "config/terminology-questions.yaml",
    ]
    for item in unchanged:
        assert sha256_file(ROOT / item.file) == item.after_sha256


@requires_pilot_corpus
def test_terminology_drift_since_the_migration_is_explained_by_governed_events() -> None:
    """Drift is acceptable only when governed lineage accounts for every step of it.

    Two independent facts are asserted here, and confusing them is what made the original guard
    wrong: the migration record still reports the historical hash it always did, *and* the live
    registry is the head of the governed chain that continues from it. The chain has more than one
    kind of writer — a decision correction restates one decision, an applied remediation plan moves
    many — so an explanation that considered only corrections would call a legitimately remediated
    registry ungoverned.
    """

    record = load_migrations(ROOT)[0]
    terminology = next(
        item for item in record.registries if item.file.as_posix() == "config/terminology.yaml"
    )
    registry = TerminologyRegistry.model_validate(_load(ROOT / "config/terminology.yaml"))
    corrections = load_corrections(ROOT)
    transitions = registry_transitions(
        corrections=corrections, remediations=load_applied_remediations(ROOT)
    )
    lineage = explain_registry_lineage(
        transitions,
        current_semantic_sha256=registry_identity(registry),
        since=record.created_at,
    )

    # The historical claim is intact and was not rewritten to match the current file.
    assert terminology.after_sha256 == HISTORICAL_MIGRATED_TERMINOLOGY_HASH
    assert sha256_file(ROOT / terminology.file) != terminology.after_sha256

    # Every step away from it is recorded, and the last recorded step is the live registry.
    assert lineage.explained is True
    assert lineage.head_event_id == APPLIED_REMEDIATION_PLAN_ID
    assert lineage.head_event_kind is RegistryEventKind.REMEDIATION_APPLICATION
    assert lineage.head_semantic_sha256 == CURRENT_TERMINOLOGY_SEMANTIC_SHA
    assert lineage.head_semantic_sha256 == registry_identity(registry)
    assert lineage.considered_event_ids == (
        "trp-90ae32ee2d14c3035f01bf7d",
        "tdc-18f94c4bca98e64281013a14",
        APPLIED_REMEDIATION_PLAN_ID,
    )

    # The remediation hands over from the correction exactly, with nothing ungoverned between them.
    window = {item.event_id: item for item in transitions}
    assert (
        window["tdc-18f94c4bca98e64281013a14"].semantic_sha256_after
        == window[APPLIED_REMEDIATION_PLAN_ID].semantic_sha256_before
    )
    # An earlier gap is reported rather than denied: the migration itself sits inside the window.
    assert lineage.contiguous is False

    # A correction published before the migration stays outside the window, and its own record is
    # untouched: the migration interrupts the chain rather than invalidating what precedes it.
    earlier = [item for item in corrections if item.created_at < record.created_at]
    assert [item.correction_id for item in earlier] == ["tdc-a5b0be196c114d4920020766"]
    assert earlier[0].source_term == "Aunt Resara"


@requires_pilot_corpus
def test_live_registry_health_matches_its_governed_post_remediation_state() -> None:
    """The live decision count is derived from the governed chain, not pinned as a bare number.

    Hard-coding the count is what made this guard stale the moment a governed remediation retired
    six decisions. What is durable is the arithmetic: the applied plan says what it started from and
    what it retired, and the live registry must be exactly that.
    """

    registry = TerminologyRegistry.model_validate(_load(ROOT / "config/terminology.yaml"))
    questions = OpenQuestionRegistry.model_validate(
        _load(ROOT / "config/terminology-questions.yaml")
    )
    plan = next(
        item
        for item in load_applied_remediations(ROOT)
        if item.plan_id == APPLIED_REMEDIATION_PLAN_ID
    )

    assert len(registry.decisions) == plan.decisions_before - plan.retirement_count
    assert plan.decisions_before == 894 and plan.retirement_count == 6
    # Rejecting a candidate withdraws its decision without deleting the evidence it was found on.
    assert len(registry.candidates) == plan.candidates_before == 1193
    assert plan.candidate_rejection_count == 6
    assert registry_identity(registry) == CURRENT_TERMINOLOGY_SEMANTIC_SHA
    assert registry_identity(registry) == plan.expected_semantic_sha256
    assert registry.version == plan.registry_version_after

    assert detect_alias_cycles(registry) == []
    assert len([item for item in questions.questions if item.status is QuestionStatus.OPEN]) == 1
    assert [item for item in questions.questions if item.blocking] == []


@requires_pilot_corpus
def test_every_retired_decision_is_absent_for_its_recorded_governed_reason() -> None:
    """Absence is only legitimate when a published operation accounts for it."""

    registry = TerminologyRegistry.model_validate(_load(ROOT / "config/terminology.yaml"))
    plan = next(
        item
        for item in load_applied_remediations(ROOT)
        if item.plan_id == APPLIED_REMEDIATION_PLAN_ID
    )
    retirements = [item.retirement for item in plan.operations if item.retirement is not None]
    decided = {
        candidate.source_term
        for candidate in registry.candidates
        if any(item.candidate_id == candidate.candidate_id for item in registry.decisions)
    }

    assert len(retirements) == plan.retirement_count
    for retirement in retirements:
        assert retirement.source_term not in decided
        assert retirement.reason


@requires_pilot_corpus
def test_locked_user_owned_decisions_survived_every_governed_operation() -> None:
    """Ownership is the one thing remediation may never overrule, so it is asserted separately."""

    registry = TerminologyRegistry.model_validate(_load(ROOT / "config/terminology.yaml"))
    candidates = {item.candidate_id: item for item in registry.candidates}
    locked = {
        candidates[item.candidate_id].source_term for item in registry.decisions if item.locked
    }
    plan = next(
        item
        for item in load_applied_remediations(ROOT)
        if item.plan_id == APPLIED_REMEDIATION_PLAN_ID
    )
    mutated = {
        item.source_term
        for item in plan.operations
        if item.retirement is not None
        or item.enforcement_change is not None
        or item.target_correction is not None
    }

    assert "Ajah" in locked
    assert locked & mutated == set()


@requires_pilot_corpus
def test_migrated_anomaly_identity_matches_its_own_derivation_rule() -> None:
    anomalies = SourceAnomalyRegistry.model_validate(_load(ROOT / "config/source-anomalies.yaml"))

    assert len(anomalies.anomalies) == 1
    anomaly = anomalies.anomalies[0]
    assert anomaly.anomaly_id == anomaly_identity(
        book_id=anomaly.book_id,
        chapter_id=anomaly.chapter_id,
        page_from=anomaly.page_from,
        page_to=anomaly.page_to,
        source_location=anomaly.source_location,
        category=anomaly.category,
        original_sha256=anomaly.original_sha256,
    )
    assert anomaly.status.value == "confirmed_source_defect"
    assert anomaly.blocking is False


# --------------------------------------------------------------------------------------------
# Paragraph alignment over the completed pilot run
# --------------------------------------------------------------------------------------------

COMPLETED_RUN_ID = "tr-9df726a15a52-001"
COMPLETED_RUN = ROOT / "workspace/translation-runs" / COMPLETED_RUN_ID

#: The chunks an independent read-only audit confirmed to carry paragraph-boundary drift. The
#: alignment policy was calibrated to select exactly these and nothing else on this corpus, so
#: this set is the regression guard on the policy, not a description of the book.
CONFIRMED_DRIFT_CHUNKS = {"011-003", "032-003", "040-003"}

requires_completed_run = pytest.mark.skipif(
    not COMPLETED_RUN.is_dir(),
    reason="the completed pilot translation run is not present in this checkout",
)


@requires_completed_run
def test_the_ratio_band_selects_exactly_the_confirmed_drift_chunks() -> None:
    from book_translator.paragraph_alignment.policy import PARAGRAPH_WORD_RATIO_OUT_OF_BAND
    from book_translator.paragraph_alignment.workflow import plan_alignment_audit

    plan = plan_alignment_audit(COMPLETED_RUN_ID, project_root=ROOT)

    assert plan.report.assessed_jobs == EXPECTED_CHUNKS
    assert plan.report.skipped_jobs == 0
    # The same 5,068 aligned pairs the earlier independent audit counted.
    assert plan.report.aligned_pairs == 5068
    flagged = {
        entry.chunk_id
        for entry in plan.report.entries
        if any(item.code == PARAGRAPH_WORD_RATIO_OUT_OF_BAND for item in entry.findings)
    }
    assert flagged == CONFIRMED_DRIFT_CHUNKS


@requires_completed_run
def test_every_alignment_finding_on_the_pilot_is_advisory_except_confirmed_drift() -> None:
    from book_translator.paragraph_alignment.workflow import plan_alignment_audit

    plan = plan_alignment_audit(COMPLETED_RUN_ID, project_root=ROOT)

    blocking = {entry.chunk_id for entry in plan.report.entries if entry.error_findings}
    assert blocking <= CONFIRMED_DRIFT_CHUNKS
    assert plan.report.finding_counts["adjacent_boundary_drift_suspected"] == 2


@requires_completed_run
def test_auditing_the_completed_run_mutates_nothing_it_reads() -> None:
    from book_translator.paragraph_alignment.workflow import plan_alignment_audit

    before = {
        path.relative_to(ROOT): sha256_file(path)
        for path in sorted(COMPLETED_RUN.rglob("*"))
        if path.is_file()
    }

    plan_alignment_audit(COMPLETED_RUN_ID, project_root=ROOT)

    after = {
        path.relative_to(ROOT): sha256_file(path)
        for path in sorted(COMPLETED_RUN.rglob("*"))
        if path.is_file()
    }
    assert after == before


@requires_completed_run
def test_the_completed_run_keeps_its_pre_contract_output_contract() -> None:
    from book_translator.translation_queue import load_run_summary

    state = load_run_summary(COMPLETED_RUN_ID, project_root=ROOT)

    # The run predates the numbered paragraph contract and must keep saying so, which is what
    # stops its packages, attempts, and reuse eligibility from being reinterpreted.
    assert state.run.output_contract_version is None
    assert state.run.accepted_count == EXPECTED_CHUNKS
