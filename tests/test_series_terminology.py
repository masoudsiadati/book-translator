"""Terminology Architecture v2: series policy, book occurrences, reference retrieval, lineage.

Everything here is synthetic. The "corpora" are opaque files that only need stable hashes, the
retriever is a scripted module written into the temporary project, and every Persian string is an
invented form. The tests prove the three layers stay apart: historical evidence is never
rewritten, retrieval carries provenance and excludes anomalous material, and the policy decides.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from book_translator.cli import app
from book_translator.config import BookConfig, load_book_config
from book_translator.models import (
    ChapterMetadata,
    ChapterStatus,
    ChunkMetadata,
    ChunkStatus,
    DecisionSource,
    ObservedVariant,
    ObservedVariantSupport,
    PromptPackage,
    ReferenceContextIndex,
    ReferenceContextPackage,
    ReferenceCorpusConfig,
    ReferenceEvidenceQuality,
    ReferenceEvidenceSource,
    SeriesProvenanceKind,
    SeriesTermConflictKind,
    SeriesTerminologyPolicy,
    SeriesTermStatus,
    TerminologyEnforcement,
    TerminologyType,
    TocDocument,
    TocEntry,
    TocKind,
    TranslationManifest,
)
from book_translator.paths import ProjectPaths
from book_translator.prompt_packaging import (
    PromptPackageError,
    execute_prompt_packages,
    plan_prompt_packages,
    validate_prompt_package,
)
from book_translator.prompt_packaging.rendering import extract_framed_bytes
from book_translator.series_terminology import TERMINOLOGY_CONTEXT_CONTRACT_VERSION
from book_translator.series_terminology.bootstrap import (
    ExplicitDecision,
    PriorVolumeDecision,
    build_policy,
)
from book_translator.series_terminology.context import (
    build_effective_context,
    render_effective_context,
)
from book_translator.series_terminology.corpus import (
    CorpusConcept,
    CorpusForm,
    natural_source_forms,
)
from book_translator.series_terminology.derivation import ChunkText, derive_book_registry
from book_translator.series_terminology.identity import (
    canonical_policy_bytes,
    concept_identity,
    policy_identity,
)
from book_translator.series_terminology.policy import (
    SeriesTerminologyError,
    load_policy,
    save_policy,
    set_explicit_term,
)
from book_translator.series_terminology.promotion import promote
from book_translator.series_terminology.retrieval import (
    ReferenceRetrievalError,
    load_retriever_module,
    open_reference_retriever,
    package_from_context,
    verify_reference_corpus,
)
from book_translator.series_terminology.workflow import (
    execute_book_terminology,
    execute_reference_context,
    plan_book_terminology,
    plan_reference_context,
    resolve_terminology_lineage,
)
from book_translator.terminology.validation import validate_terminology
from book_translator.translation_queue.workflow import create_run, plan_run
from book_translator.translation_snapshot import (
    SnapshotError,
    create_snapshot,
    plan_snapshot,
    validate_snapshot,
)
from book_translator.translation_snapshot.identity import (
    ManifestIdentityFormat,
    canonical_manifest_bytes,
    persisted_manifest_identity_format,
)

NOW = datetime(2026, 5, 6, 7, 8, tzinfo=UTC)
BOOK = "beta"
runner = CliRunner()

# Invented target forms. None of these is a real translation.
FORM_AJAH_USER = "آجای"
FORM_AJAH_CORPUS = "آجاه"
FORM_SEDAI = "آیزسدای"
FORM_THOM_1 = "تام"
FORM_THOM_2 = "ثام"
FORM_WARDER = "نگهبان"
FORM_LAN = "لان"
FORM_LANFEAR = "لانفیر"
FORM_ELAYNE_PRIOR = "الین"
FORM_ANOMALOUS_TARGET = "متن-مرجع-ناهنجار"
FORM_CLEAN_TARGET = "متن-مرجع-پاک"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _support(
    corpus_id: str,
    source: ReferenceEvidenceSource,
    count: int,
    *,
    quality: ReferenceEvidenceQuality = ReferenceEvidenceQuality.CLEAN,
    reference_id: str = "TTF-B01-0001-01",
) -> ObservedVariantSupport:
    return ObservedVariantSupport(
        corpus_id=corpus_id,
        source=source,
        quality=quality,
        observation_count=count,
        reference_id=reference_id,
    )


def _form(
    corpus_id: str,
    target: str,
    source: ReferenceEvidenceSource,
    count: int,
    *,
    quality: ReferenceEvidenceQuality = ReferenceEvidenceQuality.CLEAN,
    reference_id: str = "TTF-B01-0001-01",
) -> CorpusForm:
    return CorpusForm(corpus_id, target, source, quality, count, reference_id)


NARRATIVE = ReferenceEvidenceSource.NARRATIVE
GLOSSARY = ReferenceEvidenceSource.PRINTED_GLOSSARY


def synthetic_corpus() -> list[CorpusConcept]:
    """Two synthetic corpora with agreement, disagreement, glossary-only, and short-name shapes."""
    return [
        CorpusConcept(
            "book1",
            "TC-B01-0001",
            "terminology",
            "Ajah",
            "organization",
            ("Ajah",),
            (_form("book1", FORM_AJAH_CORPUS, NARRATIVE, 40),),
        ),
        CorpusConcept(
            "book2",
            "TC-B02-0001",
            "terminology",
            "Ajah",
            "organization",
            ("Ajah",),
            (_form("book2", FORM_AJAH_CORPUS, NARRATIVE, 55),),
        ),
        CorpusConcept(
            "book1",
            "TC-B01-0002",
            "terminology",
            "Aes Sedai",
            "group_title",
            ("Aes Sedai",),
            (_form("book1", FORM_SEDAI, GLOSSARY, 1), _form("book1", FORM_SEDAI, NARRATIVE, 503)),
        ),
        CorpusConcept(
            "book2",
            "TC-B02-0002",
            "terminology",
            "Aes Sedai",
            "group_title",
            ("Aes Sedai",),
            (_form("book2", FORM_SEDAI, GLOSSARY, 1), _form("book2", FORM_SEDAI, NARRATIVE, 512)),
        ),
        CorpusConcept(
            "book1",
            "EF-B01-003-01",
            "entity",
            "Thom Merrilin",
            "person",
            ("Thom",),
            (_form("book1", FORM_THOM_1, NARRATIVE, 40),),
        ),
        CorpusConcept(
            "book2",
            "EF-B02-003-01",
            "entity",
            "Thom Merrilin",
            "person",
            ("Thom",),
            (_form("book2", FORM_THOM_2, NARRATIVE, 50),),
        ),
        CorpusConcept(
            "book1",
            "TC-B01-0004",
            "terminology",
            "Warder, the",
            "title",
            ("Warder",),
            (_form("book1", FORM_WARDER, GLOSSARY, 1),),
        ),
        CorpusConcept(
            "book2",
            "TC-B02-0004",
            "terminology",
            "Warder, the",
            "title",
            ("Warder",),
            (_form("book2", FORM_WARDER, GLOSSARY, 1),),
        ),
        CorpusConcept(
            "book1",
            "EF-B01-005-01",
            "entity",
            "Lan",
            "person",
            ("Lan",),
            (_form("book1", FORM_LAN, NARRATIVE, 90),),
        ),
        CorpusConcept(
            "book2",
            "EF-B02-005-01",
            "entity",
            "Lan",
            "person",
            ("Lan",),
            (_form("book2", FORM_LAN, NARRATIVE, 70),),
        ),
        CorpusConcept(
            "book1",
            "EF-B01-006-01",
            "entity",
            "Lanfear",
            "person",
            ("Lanfear",),
            (_form("book1", FORM_LANFEAR, NARRATIVE, 12),),
        ),
        CorpusConcept(
            "book2",
            "EF-B02-006-01",
            "entity",
            "Lanfear",
            "person",
            ("Lanfear",),
            (_form("book2", FORM_LANFEAR, NARRATIVE, 30),),
        ),
    ]


def synthetic_policy() -> SeriesTerminologyPolicy:
    policy, _ = build_policy(
        series_id="synthetic-series",
        source_language="en",
        target_language="fa",
        corpus_concepts=synthetic_corpus(),
        explicit=[
            ExplicitDecision(
                "Ajah",
                FORM_AJAH_USER,
                TerminologyType.ORGANIZATION,
                SeriesProvenanceKind.EXPLICIT_USER,
                "config/terminology.yaml#td-1",
                "user",
            )
        ],
        prior=[PriorVolumeDecision("Elayne", FORM_ELAYNE_PRIOR, TerminologyType.PERSON, "td-9", 7)],
    )
    return policy


def _term(policy: SeriesTerminologyPolicy, source: str) -> Any:
    return next(term for term in policy.terms if term.source_term == source)


# ------------------------------------------------------------------------------------------
# Policy construction and precedence
# ------------------------------------------------------------------------------------------


def test_explicit_user_term_outranks_conflicting_corpus_evidence_and_keeps_it() -> None:
    policy = synthetic_policy()
    ajah = _term(policy, "Ajah")
    assert ajah.status is SeriesTermStatus.LOCKED
    assert ajah.target_term == FORM_AJAH_USER
    assert ajah.provenance.kind is SeriesProvenanceKind.EXPLICIT_USER
    # The corpus form is neither erased nor promoted: it stays as observed evidence.
    assert [variant.target_form for variant in ajah.variants] == [FORM_AJAH_CORPUS]
    assert {support.corpus_id for support in ajah.variants[0].support} == {"book1", "book2"}
    assert [conflict.kind for conflict in ajah.conflicts] == [
        SeriesTermConflictKind.CORPUS_DISAGREES_WITH_EXPLICIT_DECISION
    ]
    assert ajah.conflicts[0].competing_forms == [FORM_AJAH_CORPUS]


def test_cross_source_agreement_promotes_and_disagreement_stays_observed() -> None:
    policy = synthetic_policy()
    sedai = _term(policy, "Aes Sedai")
    assert sedai.status is SeriesTermStatus.STRONG_REFERENCE
    assert sedai.target_term == FORM_SEDAI
    assert sedai.provenance.kind is SeriesProvenanceKind.PROMOTED_REFERENCE
    assert "independent clean sources agree" in sedai.provenance.evidence_summary
    thom = _term(policy, "Thom")
    assert thom.status is SeriesTermStatus.OBSERVED_VARIANTS
    assert thom.target_term is None
    assert sorted(variant.target_form for variant in thom.variants) == sorted(
        [FORM_THOM_1, FORM_THOM_2]
    )


def test_printed_glossary_evidence_is_never_promoted_to_a_prose_form() -> None:
    policy = synthetic_policy()
    warder = _term(policy, "Warder")
    assert warder.status is SeriesTermStatus.OBSERVED_VARIANTS
    assert "printed-glossary evidence only" in warder.provenance.evidence_summary
    # The dictionary-order headword was reduced to its natural form for matching only.
    assert natural_source_forms("Warder, the", lambda form: [("Warder",)]) == ("Warder",)
    assert natural_source_forms("Aes Sedai", lambda form: [("wrong",)]) == ("Aes Sedai",)


def test_promotion_is_not_raw_frequency() -> None:
    frequent_single_source = [
        ObservedVariant(target_form="x", support=[_support("book1", NARRATIVE, 900)])
    ]
    assert promote(frequent_single_source).status is SeriesTermStatus.OBSERVED_VARIANTS
    ocr_noise_rival = [
        ObservedVariant(
            target_form="x",
            support=[_support("book1", NARRATIVE, 300), _support("book2", NARRATIVE, 200)],
        ),
        ObservedVariant(
            target_form="y",
            support=[_support("book2", NARRATIVE, 1, quality=ReferenceEvidenceQuality.OCR_SUSPECT)],
        ),
    ]
    outcome = promote(ocr_noise_rival)
    assert outcome.status is SeriesTermStatus.STRONG_REFERENCE
    assert outcome.target_term == "x"


def test_prior_volume_decisions_are_evidence_never_decisions() -> None:
    policy = synthetic_policy()
    elayne = _term(policy, "Elayne")
    assert elayne.status is SeriesTermStatus.OBSERVED_VARIANTS
    assert elayne.provenance.kind is SeriesProvenanceKind.PRIOR_VOLUME
    assert elayne.variants[0].support[0].source is ReferenceEvidenceSource.PRIOR_VOLUME_REGISTRY


def test_policy_contract_rejects_inconsistent_status_and_provenance() -> None:
    policy = synthetic_policy()
    locked = _term(policy, "Ajah")
    with pytest.raises(ValueError, match="only an explicit user or project decision"):
        locked.model_copy(
            update={
                "provenance": locked.provenance.model_copy(
                    update={"kind": SeriesProvenanceKind.PROMOTED_REFERENCE}
                )
            }
        ).model_validate(
            locked.model_copy(
                update={
                    "provenance": locked.provenance.model_copy(
                        update={"kind": SeriesProvenanceKind.PROMOTED_REFERENCE}
                    )
                }
            ).model_dump()
        )
    with pytest.raises(ValueError, match="must not carry a target term"):
        _term(policy, "Thom").model_validate(
            {**_term(policy, "Thom").model_dump(), "target_term": FORM_THOM_1}
        )


def test_explicit_user_edit_preserves_history_and_survives_round_trip(tmp_path: Path) -> None:
    policy = synthetic_policy()
    edited = set_explicit_term(
        policy,
        source_term="Thom",
        target_term=FORM_THOM_2,
        classification=TerminologyType.PERSON,
        rationale="reviewed",
    )
    thom = _term(edited, "Thom")
    assert thom.status is SeriesTermStatus.LOCKED
    assert thom.provenance.kind is SeriesProvenanceKind.EXPLICIT_USER
    assert sorted(v.target_form for v in thom.variants) == sorted([FORM_THOM_1, FORM_THOM_2])
    assert any("superseded OBSERVED_VARIANTS" in note for note in thom.notes)
    path = tmp_path / "policy.yaml"
    save_policy(edited, path)
    assert canonical_policy_bytes(load_policy(path)) == canonical_policy_bytes(edited)
    assert policy_identity(load_policy(path)) == policy_identity(edited)
    with pytest.raises(SeriesTerminologyError):
        set_explicit_term(
            policy,
            source_term="Thom",
            target_term=FORM_THOM_2,
            classification=TerminologyType.PERSON,
            status=SeriesTermStatus.OBSERVED_VARIANTS,
            rationale="no",
        )


# ------------------------------------------------------------------------------------------
# Book occurrences and QA projection
# ------------------------------------------------------------------------------------------

CHUNKS = (
    ChunkText("001-001", "001", "The Aes Sedai of the Red Ajah met Thom.\n\nAjah again.\n", ""),
    ChunkText("002-001", "002", "Lanfear laughed at the Warder.\n\nElayne waited.\n", "Ajah\n"),
)


def test_derived_registry_carries_only_this_books_occurrence_identity() -> None:
    registry, summary = derive_book_registry(
        synthetic_policy(), book_id="beta-en-fa", chunks=CHUNKS
    )
    assert registry.book_id == "beta-en-fa"
    by_source = {candidate.source_term: candidate for candidate in registry.candidates}
    ajah = by_source["Ajah"]
    assert [(o.chapter_id, o.chunk_id, o.location, o.count) for o in ajah.occurrences] == [
        ("001", "001-001", "source", 2),
        ("002", "002-001", "context", 1),
    ]
    assert summary.chapters == 2 and summary.chunks == 2
    corpus_ids = {"TC-B01", "TC-B02", "EF-B01", "EF-B02", "TM-B0"}
    for candidate in registry.candidates:
        for occurrence in candidate.occurrences:
            assert not any((occurrence.chunk_id or "").startswith(prefix) for prefix in corpus_ids)
            assert occurrence.chunk_id in {"001-001", "002-001"}


def test_short_name_matching_never_infers_lan_from_lanfear() -> None:
    registry, _ = derive_book_registry(synthetic_policy(), book_id="beta-en-fa", chunks=CHUNKS)
    sources = {candidate.source_term for candidate in registry.candidates}
    assert "Lanfear" in sources
    assert "Lan" not in sources
    report = validate_terminology(
        "Lanfear laughed at the Warder.\n", f"{FORM_LANFEAR} خندید.\n", registry
    )
    assert report.valid and not report.findings


def test_projection_matches_policy_force_levels() -> None:
    registry, _ = derive_book_registry(synthetic_policy(), book_id="beta-en-fa", chunks=CHUNKS)
    candidates = {c.candidate_id: c for c in registry.candidates}
    decisions = {candidates[d.candidate_id].source_term: d for d in registry.decisions}
    assert decisions["Ajah"].locked and decisions["Ajah"].decision_source is DecisionSource.USER
    assert decisions["Ajah"].enforcement is TerminologyEnforcement.EXACT
    assert decisions["Ajah"].competing_variants == [FORM_AJAH_CORPUS]
    assert decisions["Aes Sedai"].enforcement is TerminologyEnforcement.CONTEXTUAL
    assert not decisions["Aes Sedai"].locked and not decisions["Aes Sedai"].competing_variants
    assert "Thom" not in decisions and "Warder" not in decisions and "Elayne" not in decisions


@pytest.mark.parametrize(
    ("translation", "codes", "valid"),
    [
        (f"{FORM_SEDAI} از {FORM_AJAH_USER} سرخ.\n\n{FORM_AJAH_USER} دوباره.\n", (), True),
        (
            f"{FORM_SEDAI} از {FORM_AJAH_CORPUS} سرخ.\n\n{FORM_AJAH_CORPUS} دوباره.\n",
            ("canonical_term_missing", "competing_variant"),
            False,
        ),
        (
            f"خواهران از {FORM_AJAH_USER} سرخ.\n\n{FORM_AJAH_USER} دوباره.\n",
            ("canonical_term_unverified",),
            True,
        ),
    ],
    ids=["conforming", "locked_violation_blocks", "strong_divergence_warns"],
)
def test_locked_blocks_strong_warns_observed_is_silent(
    translation: str, codes: tuple[str, ...], valid: bool
) -> None:
    registry, _ = derive_book_registry(synthetic_policy(), book_id="beta-en-fa", chunks=CHUNKS)
    report = validate_terminology(CHUNKS[0].source_text, translation, registry)
    assert report.valid is valid
    assert tuple(sorted({finding.code for finding in report.findings})) == tuple(sorted(codes))
    assert all(finding.source_term != "Thom" for finding in report.findings)
    for finding in report.findings:
        expected = (
            "error"
            if finding.code in {"canonical_term_missing", "competing_variant"}
            else "warning"
        )
        assert finding.severity == expected


def test_derivation_prefers_the_terms_own_book_spelling_over_an_alias() -> None:
    policy, _ = build_policy(
        series_id="s",
        source_language="en",
        target_language="fa",
        corpus_concepts=[
            CorpusConcept(
                "book1",
                "TC-B01-0001",
                "terminology",
                "Amyrlin Seat",
                "title",
                ("Amyrlin Seat", "Amyrlin"),
                (_form("book1", "x", NARRATIVE, 3),),
            ),
            CorpusConcept(
                "book1",
                "TC-B01-0002",
                "terminology",
                "Amyrlin",
                "title",
                ("Amyrlin",),
                (_form("book1", "y", NARRATIVE, 3),),
            ),
        ],
        explicit=[],
    )
    chunks = (ChunkText("001-001", "001", "The Amyrlin Seat. The Amyrlin. Amyrlin.\n"),)
    registry, _ = derive_book_registry(policy, book_id="b", chunks=chunks)
    sources = sorted(c.source_term for c in registry.candidates)
    assert sources == ["Amyrlin", "Amyrlin Seat"]
    seat = next(c for c in registry.candidates if c.source_term == "Amyrlin Seat")
    assert "Amyrlin" not in seat.aliases


def test_typographic_apostrophe_of_the_book_is_the_exact_source_term() -> None:
    policy, _ = build_policy(
        series_id="s",
        source_language="en",
        target_language="fa",
        corpus_concepts=[
            CorpusConcept(
                "book1",
                "EF-B01-001-01",
                "entity",
                "Rand al’Thor",
                "person",
                ("Rand al’Thor",),
                (_form("book1", "ر", NARRATIVE, 3),),
            ),
        ],
        explicit=[],
    )
    assert _term(policy, "Rand al'Thor").concept_id == concept_identity("Rand al’Thor", "en")
    chunks = (ChunkText("001-001", "001", "Rand al’Thor rode.\n"),)
    registry, _ = derive_book_registry(policy, book_id="b", chunks=chunks)
    assert [c.source_term for c in registry.candidates] == ["Rand al’Thor"]


# ------------------------------------------------------------------------------------------
# Effective context rendering
# ------------------------------------------------------------------------------------------


def _reference_context(text: str) -> dict[str, Any]:
    """A retriever-shaped context: clean and anomalous material side by side."""
    terms = []
    if "Ajah" in text:
        terms.append(
            {
                "source": "Ajah",
                "match_quality": "exact",
                "occurrences_in_passage": 2,
                "cross_book_observation": "identical_variant_sets",
                "concepts": [{"category": "organization"}],
                "observed_variants": [
                    {
                        "target": FORM_AJAH_CORPUS,
                        "all_diagnostic": False,
                        "evidence": [
                            {
                                "book": 1,
                                "origin": "narrative",
                                "ocr_suspect": False,
                                "diagnostic": False,
                                "observation_count": 40,
                                "target_form_id": "TTF-B01-0001-02",
                            },
                            {
                                "book": 2,
                                "origin": "narrative",
                                "ocr_suspect": False,
                                "diagnostic": False,
                                "observation_count": 55,
                                "target_form_id": "TTF-B02-0001-02",
                            },
                        ],
                    }
                ],
            }
        )
    if "Lanfear" in text:
        terms.append(
            {
                "source": "Lanfear",
                "match_quality": "exact",
                "occurrences_in_passage": 1,
                "cross_book_observation": "identical_variant_sets",
                "concepts": [{"category": "person"}],
                "observed_variants": [
                    {
                        "target": FORM_LANFEAR,
                        "all_diagnostic": False,
                        "evidence": [
                            {
                                "book": 2,
                                "origin": "narrative",
                                "ocr_suspect": True,
                                "diagnostic": False,
                                "observation_count": 30,
                                "target_form_id": "TTF-B02-0006-01",
                                "variant_note": "broken letter",
                            },
                        ],
                    }
                ],
            }
        )
    terms.append(
        {
            "source": "Light",
            "match_quality": "ambiguous_common_word",
            "occurrences_in_passage": 1,
            "cross_book_observation": "only_book_1",
            "concepts": [],
            "observed_variants": [
                {
                    "target": "نور",
                    "all_diagnostic": False,
                    "evidence": [
                        {
                            "book": 1,
                            "origin": "narrative",
                            "ocr_suspect": False,
                            "diagnostic": False,
                            "observation_count": 3,
                            "target_form_id": "TTF-B01-0009-01",
                        }
                    ],
                }
            ],
        }
    )
    return {
        "schema_version": "cross-book-retriever/1",
        "query": {"sha256": _sha(text.encode())},
        "terminology": terms,
        "entities": [],
        "translation_memory": [
            {
                "book": 1,
                "chapter": 3,
                "tm_id": "TM-B01-C03-0007",
                "status": "clean",
                "relevance_score": 0.8,
                "minimum_alignment_confidence": 0.95,
                "source_text": f"Clean source about {text.split()[0]}.",
                "target_text": FORM_CLEAN_TARGET,
                "diagnostic": False,
            },
            {
                "book": 2,
                "chapter": 4,
                "tm_id": "TM-B02-C04-0001",
                "status": "reference_only",
                "relevance_score": 0.9,
                "minimum_alignment_confidence": 0.4,
                "source_text": "Anomalous source.",
                "target_text": FORM_ANOMALOUS_TARGET,
                "diagnostic": True,
                "diagnostic_label": "REFERENCE-ONLY TM",
            },
        ],
        "phrases": [
            {
                "source": "Blood and ashes",
                "categories": ["curse"],
                "observed_variants": [
                    {
                        "target": "خون-و-خاکستر",
                        "all_diagnostic": False,
                        "evidence": [
                            {
                                "book": 1,
                                "origin": "narrative",
                                "ocr_suspect": False,
                                "diagnostic": False,
                                "observation_count": 34,
                                "phrase_id": "PH-B01-001",
                            }
                        ],
                    }
                ],
            }
        ],
        "style": [
            {
                "book": 2,
                "chapter": 1,
                "style_id": "STYLE-B02-001",
                "tags": ["dialogue"],
                "source_text": "Style source.",
                "target_text": "نمونه-سبک",
            }
        ],
        "character_voice": [
            {
                "character": "Thom Merrilin",
                "book": 1,
                "chapter": 2,
                "voice_id": "CV-B01-004",
                "attribution_tag": "Thom said",
                "english_dialogue": "Voice source.",
                "persian_dialogue": "نمونه-صدا",
            }
        ],
        "diagnostics": {
            "reference_only_tm": [{"tm_id": "TM-B02-C04-0001", "book": 2}],
            "anomalies": [{"anomaly_id": "ANOM-B02-001"}],
        },
        "warnings": ["ambiguous common-word matches: Light"],
    }


def _package(chunk: ChunkText) -> ReferenceContextPackage:
    from book_translator.models import (
        ReferenceCorpusIdentity,
        ReferenceRetrievalIdentity,
        ReferenceRetrievalLimits,
    )

    identity = ReferenceRetrievalIdentity(
        retriever_path="/external/retriever.py",
        retriever_sha256="a" * 64,
        retriever_schema_version="cross-book-retriever/1",
        corpora=[
            ReferenceCorpusIdentity(
                corpus_id="book1",
                book_number=1,
                label="Book 1",
                path="/x/1",
                sha256="b" * 64,
                corpus_version="V2.1",
            ),
            ReferenceCorpusIdentity(
                corpus_id="book2",
                book_number=2,
                label="Book 2",
                path="/x/2",
                sha256="c" * 64,
                corpus_version="V2.1",
            ),
        ],
        limits=ReferenceRetrievalLimits(),
    )
    return package_from_context(
        _reference_context(chunk.source_text),
        book_id="beta-en-fa",
        chapter_id=chunk.chapter_id,
        chunk_id=chunk.chunk_id,
        source_sha256=_sha(chunk.source_text.encode()),
        retrieval=identity,
    )


def test_retrieval_package_keeps_clean_evidence_with_provenance_and_drops_anomalies() -> None:
    package = _package(CHUNKS[0])
    assert [segment.provenance.reference_id for segment in package.translation_memory] == [
        "TM-B01-C03-0007"
    ]
    assert package.translation_memory[0].provenance.book_label == "Book 1"
    assert package.translation_memory[0].provenance.chapter == 3
    assert package.exclusions.reference_only_tm == 2 and package.exclusions.anomalies == 1
    assert package.exclusions.ambiguous_matches == 1
    assert [item.source_term for item in package.terminology] == ["Ajah"]
    support = package.terminology[0].forms[0].support
    assert [
        (s.corpus_id, s.source.value, s.quality.value, s.observation_count) for s in support
    ] == [
        ("book1", "narrative", "clean", 40),
        ("book2", "narrative", "clean", 55),
    ]
    dumped = json.dumps(package.model_dump(mode="json"), ensure_ascii=False)
    assert FORM_ANOMALOUS_TARGET not in dumped
    assert FORM_CLEAN_TARGET in dumped


def test_effective_context_separates_the_four_tiers_visibly() -> None:
    policy = synthetic_policy()
    context = build_effective_context(
        policy, source_text=CHUNKS[0].source_text, reference=_package(CHUNKS[0])
    )
    assert [t.source_term for t in context.locked] == ["Ajah"]
    assert [t.source_term for t in context.strong] == ["Aes Sedai"]
    assert [t.source_term for t in context.observed] == ["Thom"]
    rendered = render_effective_context(context)
    locked_at = rendered.index("## 1. LOCKED CONSISTENCY TERMS")
    strong_at = rendered.index("## 2. STRONG REFERENCE EVIDENCE")
    observed_at = rendered.index("## 3. OBSERVED VARIANTS")
    reference_at = rendered.index("## 4. REFERENCE CONTEXT")
    assert locked_at < strong_at < observed_at < reference_at
    assert f'"{FORM_AJAH_USER}"' in rendered[locked_at:strong_at]
    assert "MANDATORY" in rendered[locked_at:strong_at]
    assert "Do not re-transliteract" not in rendered
    assert "re-transliterate" in rendered[locked_at:strong_at]
    assert f'"{FORM_SEDAI}"' in rendered[strong_at:observed_at]
    assert "STRONGLY PREFERRED" in rendered[strong_at:observed_at]
    assert FORM_THOM_1 in rendered[observed_at:reference_at]
    assert FORM_THOM_2 in rendered[observed_at:reference_at]
    assert "EVIDENCE ONLY" in rendered[observed_at:reference_at]
    assert "Never copy a reference sentence" in rendered[reference_at:]
    # The corpus form of a locked term appears only as observed evidence, never as reference
    # terminology that could read like a second recommendation.
    assert "reference_terminology:\n  []" in rendered[reference_at:]
    assert FORM_ANOMALOUS_TARGET not in rendered
    assert "reference-only TM 2, anomalies 1" in rendered
    assert render_effective_context(context) == rendered


def test_context_rendering_marks_ocr_suspect_reference_forms() -> None:
    policy = synthetic_policy()
    context = build_effective_context(
        policy, source_text=CHUNKS[1].source_text, reference=_package(CHUNKS[1])
    )
    assert [t.source_term for t in context.strong] == ["Lanfear"]
    assert context.superseded_reference_terms == ("Lanfear",)
    assert "[ocr_suspect]" not in render_effective_context(context)
    ungoverned = policy.model_copy(update={"terms": []})
    rendered = render_effective_context(
        build_effective_context(
            ungoverned, source_text=CHUNKS[1].source_text, reference=_package(CHUNKS[1])
        )
    )
    assert "[ocr_suspect]" in rendered
    assert 'reference_terminology:\n  - source: "Lanfear"' in rendered


FORM_AJAHS_PRIOR = "آجاها"
FORM_RED = "سرخ"
FORM_BLACK = "سیاه"


def test_observed_variants_need_a_mention_in_the_passage_itself() -> None:
    policy = synthetic_policy()
    # Thom is OBSERVED_VARIANTS, Ajah is LOCKED, Aes Sedai is STRONG_REFERENCE. The context window
    # is untranslated text: evidence about a term that only occurs there cannot shape the output,
    # while the decided tiers keep honouring it exactly as before.
    context = build_effective_context(
        policy,
        source_text="Nobody spoke.\n",
        context_text="Thom greeted the Aes Sedai of the Ajah.\n",
    )
    assert [t.source_term for t in context.locked] == ["Ajah"]
    assert [t.source_term for t in context.strong] == ["Aes Sedai"]
    assert context.observed == ()
    in_passage = build_effective_context(
        policy, source_text="Thom spoke.\n", context_text="Thom greeted the Ajah.\n"
    )
    assert [t.source_term for t in in_passage.observed] == ["Thom"]
    assert TERMINOLOGY_CONTEXT_CONTRACT_VERSION == "series-terminology-context/1.1.0"
    assert context.contract_version == TERMINOLOGY_CONTEXT_CONTRACT_VERSION


def test_observed_form_claimed_by_a_decision_is_not_restated_as_evidence() -> None:
    policy, _ = build_policy(
        series_id="synthetic-series",
        source_language="en",
        target_language="fa",
        corpus_concepts=synthetic_corpus(),
        explicit=[
            ExplicitDecision(
                "Ajah",
                FORM_AJAH_USER,
                TerminologyType.ORGANIZATION,
                SeriesProvenanceKind.EXPLICIT_USER,
                "config/terminology.yaml#td-1",
                "user",
                aliases=("Ajahs",),
            )
        ],
        prior=[
            PriorVolumeDecision("Ajahs", FORM_AJAHS_PRIOR, TerminologyType.ORGANIZATION, "td-2", 3)
        ],
    )
    ajahs = _term(policy, "Ajahs")
    assert ajahs.status is SeriesTermStatus.OBSERVED_VARIANTS
    assert "Ajahs" in _term(policy, "Ajah").aliases

    context = build_effective_context(policy, source_text="The Ajahs gathered.\n")

    # The locked decision already claims this spelling through its alias, so the prior volume's
    # rendering is not offered beside it as if the question were open.
    assert [t.source_term for t in context.locked] == ["Ajah"]
    assert context.observed == ()
    rendered = render_effective_context(context)
    assert FORM_AJAHS_PRIOR not in rendered
    assert f'"{FORM_AJAH_USER}"' in rendered


def _policy_with_locked_compounds() -> SeriesTerminologyPolicy:
    corpus = synthetic_corpus() + [
        CorpusConcept(
            "book1",
            "TC-B01-0007",
            "terminology",
            "Red Ajah",
            "organization",
            ("Red Ajah",),
            (_form("book1", f"{FORM_AJAH_CORPUS} {FORM_RED}", NARRATIVE, 11),),
        ),
        CorpusConcept(
            "book2",
            "TC-B02-0007",
            "terminology",
            "Red Ajah",
            "organization",
            ("Red Ajah",),
            (_form("book2", f"{FORM_AJAH_CORPUS} {FORM_RED}", NARRATIVE, 19),),
        ),
        CorpusConcept(
            "book2",
            "TC-B02-0008",
            "terminology",
            "Black Ajah",
            "organization",
            ("Black Ajah",),
            (_form("book2", f"{FORM_AJAH_CORPUS} {FORM_BLACK}", NARRATIVE, 14),),
        ),
    ]
    policy, _ = build_policy(
        series_id="synthetic-series",
        source_language="en",
        target_language="fa",
        corpus_concepts=corpus,
        explicit=[
            ExplicitDecision(
                "Ajah",
                FORM_AJAH_USER,
                TerminologyType.ORGANIZATION,
                SeriesProvenanceKind.EXPLICIT_USER,
                "config/terminology.yaml#td-1",
                "user",
            )
        ],
        prior=[
            PriorVolumeDecision(
                "Red Ajah", f"{FORM_AJAH_USER} {FORM_RED}", TerminologyType.ORGANIZATION, "td-3", 8
            )
        ],
    )
    return policy


def test_observed_forms_lacking_a_locked_component_rendering_are_withheld() -> None:
    policy = _policy_with_locked_compounds()
    red = _term(policy, "Red Ajah")
    black = _term(policy, "Black Ajah")
    assert red.status is SeriesTermStatus.OBSERVED_VARIANTS
    assert black.status is SeriesTermStatus.OBSERVED_VARIANTS
    assert len(red.variants) == 2

    context = build_effective_context(policy, source_text="The Red Ajah watched the Black Ajah.\n")

    assert [t.source_term for t in context.locked] == ["Ajah"]
    # The corpus compound that spells the locked component another way is exactly what the lock
    # forbids and what terminology QA rejects; only the form honouring the lock stays evidence.
    assert [t.source_term for t in context.observed] == ["Red Ajah"]
    observed_red = context.observed[0]
    assert [v.target_form for v in observed_red.variants] == [f"{FORM_AJAH_USER} {FORM_RED}"]
    assert "1 form(s) withheld" in observed_red.provenance.evidence_summary
    assert "'Ajah'" in observed_red.provenance.evidence_summary
    rendered = render_effective_context(context)
    observed_at = rendered.index("## 3. OBSERVED VARIANTS")
    reference_at = rendered.index("## 4. REFERENCE CONTEXT")
    assert FORM_AJAH_CORPUS not in rendered[observed_at:reference_at]
    assert f"{FORM_AJAH_USER} {FORM_RED}" in rendered[observed_at:reference_at]
    assert "Black Ajah" not in rendered[observed_at:reference_at]
    # The policy itself is a view's input, never edited by rendering.
    assert len(_term(policy, "Red Ajah").variants) == 2
    assert len(_term(policy, "Black Ajah").variants) == 1


# ------------------------------------------------------------------------------------------
# External retriever integration (scripted module, opaque corpus files)
# ------------------------------------------------------------------------------------------

RETRIEVER_SOURCE = """
import json
from pathlib import Path

SCHEMA_VERSION = "cross-book-retriever/1"
BOOKS = (1, 2)
DB_PATHS = {1: Path("/external/book1.sqlite3"), 2: Path("/external/book2.sqlite3")}


def derive_match_forms(form):
    return [tuple(form.replace(",", "").split())]


class _DB:
    def __init__(self, book, path):
        self.book = book
        self.path = path

    def rows(self, sql, params=()):
        return []


class CrossBookRetriever:
    def __init__(self, db_paths=None):
        paths = db_paths or DB_PATHS
        self.dbs = {book: _DB(book, Path(paths[book])) for book in BOOKS}
        self.calls = []
        self.closed = False

    def context(self, text, limits=None, include_uncertain=False, include_reference_only=False):
        self.calls.append((text, include_uncertain, include_reference_only))
        marker = Path(__file__).with_name("context.json")
        return json.loads(marker.read_text(encoding="utf-8"))[text]

    def close(self):
        self.closed = True
"""


def _write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _metadata(root: Path, resource: Path, kind: str, version: str = "1.0.0") -> None:
    _write_yaml(
        resource.with_suffix(".metadata.yaml"),
        {
            "schema_version": "1.0",
            "resource_kind": kind,
            "resource_file": resource.relative_to(root).as_posix(),
            "version": version,
        },
    )


def _external_corpora(root: Path, chunks: tuple[ChunkText, ...]) -> tuple[Path, dict[str, str]]:
    external = root / "external"
    external.mkdir(exist_ok=True)
    (external / "book1.sqlite3").write_bytes(b"synthetic corpus one")
    (external / "book2.sqlite3").write_bytes(b"synthetic corpus two")
    (external / "book1-manifest.json").write_text('{"corpus_version": "V2.1"}', encoding="utf-8")
    (external / "retriever.py").write_text(RETRIEVER_SOURCE, encoding="utf-8")
    (external / "context.json").write_text(
        json.dumps({chunk.source_text: _reference_context(chunk.source_text) for chunk in chunks}),
        encoding="utf-8",
    )
    hashes = {path.name: _sha(path.read_bytes()) for path in external.iterdir()}
    return external, hashes


def _corpus_config(external: Path, hashes: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "config_kind": "reference_corpus",
        "series_id": "synthetic-series",
        "retriever": {
            "path": (external / "retriever.py").as_posix(),
            "sha256": hashes["retriever.py"],
            "schema_version": "cross-book-retriever/1",
        },
        "corpora": [
            {
                "corpus_id": "book1",
                "book_number": 1,
                "label": "Book 1",
                "path": (external / "book1.sqlite3").as_posix(),
                "sha256": hashes["book1.sqlite3"],
                "corpus_version": "V2.1",
                "freeze_manifest_file": (external / "book1-manifest.json").as_posix(),
                "freeze_manifest_sha256": hashes["book1-manifest.json"],
            },
            {
                "corpus_id": "book2",
                "book_number": 2,
                "label": "Book 2",
                "path": (external / "book2.sqlite3").as_posix(),
                "sha256": hashes["book2.sqlite3"],
                "corpus_version": "V2.1",
            },
        ],
        "excluded_paths": [(external / "legacy.sqlite3").as_posix()],
        "limits": {
            "term_limit": 15,
            "entity_limit": 15,
            "tm_limit": 8,
            "phrase_limit": 6,
            "style_limit": 3,
            "voice_limit": 4,
        },
        "include_uncertain": False,
        "include_reference_only": False,
    }


def _chapter(root: Path, book: str, chunk: ChunkText, number: int) -> None:
    paths = ProjectPaths(root, book)
    directory = paths.chapter_directory(chunk.chapter_id)
    chunk_dir = directory / "chunks" / "001"
    chunk_dir.mkdir(parents=True)
    cleaned = chunk.source_text
    (directory / "cleaned.md").write_text(cleaned, encoding="utf-8")
    (chunk_dir / "source.md").write_text(chunk.source_text, encoding="utf-8")
    (chunk_dir / "context.md").write_text(chunk.context_text, encoding="utf-8")
    cleaned_hash = _sha(cleaned.encode())
    from book_translator.document_chunking.paragraphs import parse_document

    paragraphs = parse_document(cleaned)
    words = sum(item.word_count for item in paragraphs)
    _write_yaml(
        chunk_dir / "metadata.yaml",
        ChunkMetadata(
            schema_version="1.0",
            chunk_id=chunk.chunk_id,
            chapter_id=chunk.chapter_id,
            sequence=1,
            source_file=(chunk_dir / "source.md").relative_to(root),
            source_sha256=cleaned_hash,
            context_file=(chunk_dir / "context.md").relative_to(root),
            context_sha256=_sha(chunk.context_text.encode()),
            context_word_count=len(chunk.context_text.split()),
            translation_file=(chunk_dir / "translation.md").relative_to(root),
            source_word_count=len(chunk.source_text.split()),
            source_paragraph_count=len(paragraphs),
            status=ChunkStatus.PENDING,
            chunk_engine_version="1.0.0",
            cleaned_source_sha256=cleaned_hash,
            created_at=NOW,
            updated_at=NOW,
        ),
    )
    _write_yaml(
        directory / "metadata.yaml",
        ChapterMetadata(
            schema_version="1.0",
            chapter_id=chunk.chapter_id,
            kind=TocKind.CHAPTER,
            number=number,
            title_en=f"Chapter {number}",
            source_start_page=number,
            source_end_page=number,
            source_file=(directory / "source.md").relative_to(root),
            source_sha256="a" * 64,
            cleaned_file=(directory / "cleaned.md").relative_to(root),
            cleaned_sha256=cleaned_hash,
            cleaned_word_count=words,
            cleaned_paragraph_count=len(paragraphs),
            chunk_count=1,
            status=ChapterStatus.CHUNKED,
            extraction_version="1.0",
            normalization_version="1.0",
            chunk_engine_version="1.0.0",
            created_at=NOW,
            updated_at=NOW,
        ),
    )
    _write_yaml(
        directory / "chunking-report.yaml",
        {
            "chapter_id": chunk.chapter_id,
            "cleaned_sha256": cleaned_hash,
            "chunk_engine_version": "1.0.0",
            "chunk_count": 1,
            "conservation_validation": True,
            "reconstructed_sha256": cleaned_hash,
        },
    )


def series_project(root: Path, *, chunks: tuple[ChunkText, ...] = CHUNKS) -> Path:
    """A legacy first book beside a book-scoped second book that uses the series policy."""
    (root / "config").mkdir()
    (root / "input").mkdir()
    for stem, kind, suffix, content in (
        (
            "translation-prompt",
            "prompt",
            ".md",
            "Chapter {{ chapter_number }} part {{ part_number }}\nGlossary: {{ glossary }}\n"
            "Context: {{ previous_context }}\nSource: {{ source_text }}\n",
        ),
        (
            "glossary",
            "glossary",
            ".yaml",
            "terms: {}\ncharacters: {}\nplaces: {}\ngroups: {}\nunresolved_terms: []\n",
        ),
        ("style-guide", "style_guide", ".md", "Synthetic style guide.\n"),
    ):
        resource = root / f"config/{stem}{suffix}"
        resource.write_text(content, encoding="utf-8")
        _metadata(root, resource, kind)
    # The first book's registry lives at the legacy root and must never reach the second book.
    (root / "config/terminology.yaml").write_text(
        "schema_version: '1.0'\nregistry_kind: terminology\nversion: 1.0.0\n"
        "book_id: alpha-book-en-fa\nsource_language: en\ntarget_language: fa\n"
        "candidates: []\ndecisions: []\n",
        encoding="utf-8",
    )
    _metadata(root, root / "config/terminology.yaml", "terminology")
    external, hashes = _external_corpora(root, chunks)
    series = root / "config/series/synthetic"
    _write_yaml(series / "reference-corpus.yaml", _corpus_config(external, hashes))
    _metadata(root, series / "reference-corpus.yaml", "reference_corpus")
    save_policy(synthetic_policy(), series / "terminology-policy.yaml")
    _metadata(root, series / "terminology-policy.yaml", "series_terminology")
    pdf = root / f"input/{BOOK}.pdf"
    pdf.write_bytes(b"synthetic-pdf-beta")
    (root / f"config/books/{BOOK}.yaml").parent.mkdir()
    (root / f"config/books/{BOOK}.yaml").write_text(
        f"""book_id: {BOOK}-book-en-fa
title_en: Beta Book
title_fa: کتاب بتا
author: Author
source_language: en
target_language: fa
pdf_path: input/{BOOK}.pdf
chunk_target_words: 10
chunk_max_words: 20
previous_context_words: 2
series_terminology_policy: config/series/synthetic/terminology-policy.yaml
reference_corpus: config/series/synthetic/reference-corpus.yaml
output_format: epub3
""",
        encoding="utf-8",
    )
    entries = []
    for number, chunk in enumerate(chunks, start=1):
        entries.append(
            TocEntry(
                entry_id=f"chapter-{chunk.chapter_id}",
                level=1,
                kind=TocKind.CHAPTER,
                number=number,
                title_en=f"Chapter {number}",
                start_page=number,
                end_page=number,
                source_toc_title=f"Chapter {number}",
            )
        )
        _chapter(root, BOOK, chunk, number)
    _write_yaml(
        ProjectPaths(root, BOOK).toc,
        TocDocument(
            schema_version="1.0",
            source="embedded",
            source_pdf=Path(f"input/{BOOK}.pdf"),
            source_pdf_sha256=_sha(pdf.read_bytes()),
            pdf_page_count=len(chunks),
            entry_count=len(chunks),
            entries=entries,
        ),
    )
    return series


def _prepare(root: Path) -> None:
    execute_book_terminology(plan_book_terminology(project_root=root, book=BOOK))
    execute_reference_context(plan_reference_context(project_root=root, book=BOOK))


def test_retriever_is_loaded_from_pinned_bytes_and_corpora_stay_untouched(tmp_path: Path) -> None:
    series_project(tmp_path)
    config = ReferenceCorpusConfig.model_validate(
        yaml.safe_load((tmp_path / "config/series/synthetic/reference-corpus.yaml").read_text())
    )
    before = {path.name: _sha(path.read_bytes()) for path in (tmp_path / "external").iterdir()}
    adapter = open_reference_retriever(config, project_root=tmp_path)
    try:
        assert sorted(adapter.retriever.dbs) == [1, 2]
        assert {b: Path(db.path).name for b, db in adapter.retriever.dbs.items()} == {
            1: "book1.sqlite3",
            2: "book2.sqlite3",
        }
        context = adapter.context(CHUNKS[0].source_text)
        assert adapter.retriever.calls == [(CHUNKS[0].source_text, False, False)]
        assert context["terminology"][0]["source"] == "Ajah"
    finally:
        adapter.close()
    assert adapter.retriever.closed
    after = {path.name: _sha(path.read_bytes()) for path in (tmp_path / "external").iterdir()}
    assert after == before
    assert not list((tmp_path / "external").glob("__pycache__"))
    identity = adapter.identity
    assert [c.corpus_id for c in identity.corpora] == ["book1", "book2"]
    assert identity.corpora[0].freeze_manifest_sha256 == before["book1-manifest.json"]


def test_drifted_retriever_or_corpus_is_refused(tmp_path: Path) -> None:
    series_project(tmp_path)
    config = ReferenceCorpusConfig.model_validate(
        yaml.safe_load((tmp_path / "config/series/synthetic/reference-corpus.yaml").read_text())
    )
    (tmp_path / "external/book2.sqlite3").write_bytes(b"drifted")
    with pytest.raises(ReferenceRetrievalError, match="book2 does not match"):
        verify_reference_corpus(config, project_root=tmp_path)
    with pytest.raises(ReferenceRetrievalError, match="hash mismatch"):
        load_retriever_module(tmp_path / "external/retriever.py", expected_sha256="0" * 64)


def test_books_beyond_the_configured_corpora_are_never_loaded(tmp_path: Path) -> None:
    series_project(tmp_path)
    retriever = tmp_path / "external/retriever.py"
    retriever.write_text(
        RETRIEVER_SOURCE.replace("BOOKS = (1, 2)", "BOOKS = (1, 2, 3)"), encoding="utf-8"
    )
    raw = yaml.safe_load((tmp_path / "config/series/synthetic/reference-corpus.yaml").read_text())
    raw["retriever"]["sha256"] = _sha(retriever.read_bytes())
    config = ReferenceCorpusConfig.model_validate(raw)
    with pytest.raises(ReferenceRetrievalError, match="serves books"):
        open_reference_retriever(config, project_root=tmp_path)
    with pytest.raises(ValueError):
        ReferenceCorpusConfig.model_validate({**raw, "include_reference_only": True})


def test_the_real_reference_corpus_configuration_pins_only_books_one_and_two() -> None:
    config = ReferenceCorpusConfig.model_validate(
        yaml.safe_load(
            Path("config/series/wheel-of-time/reference-corpus.yaml").read_text(encoding="utf-8")
        )
    )
    assert [(c.corpus_id, c.book_number, c.corpus_version) for c in config.corpora] == [
        ("book1", 1, "V2.1"),
        ("book2", 2, "V2.1"),
    ]
    assert all(
        "book1-reference-v2" in c.path.name or "book2-reference-v2" in c.path.name
        for c in config.corpora
    )
    assert any(path.name == "book-reference.sqlite3" for path in config.excluded_paths)
    assert config.include_uncertain is False and config.include_reference_only is False
    book6 = load_book_config(Path("config/books/lord-of-chaos.yaml"))
    assert book6.series_terminology_policy is not None and book6.terminology is None


# ------------------------------------------------------------------------------------------
# Book-scoped lineage, snapshots, and prompt packages
# ------------------------------------------------------------------------------------------


def test_two_chunks_receive_relevant_and_different_reference_packages(tmp_path: Path) -> None:
    series_project(tmp_path)
    plan = plan_reference_context(project_root=tmp_path, book=BOOK)
    assert plan.pending == ("001-001", "002-001")
    result = execute_reference_context(plan)
    assert result.retrieved == 2 and result.reused == 0
    index = ReferenceContextIndex.model_validate(yaml.safe_load(result.index_path.read_text()))
    packages = {
        entry.chunk_id: ReferenceContextPackage.model_validate(
            yaml.safe_load((tmp_path / entry.package_file).read_text(encoding="utf-8"))
        )
        for entry in index.entries
    }
    assert [t.source_term for t in packages["001-001"].terminology] == ["Ajah"]
    assert [t.source_term for t in packages["002-001"].terminology] == ["Lanfear"]
    assert packages["001-001"].source_sha256 == _sha(CHUNKS[0].source_text.encode())
    assert {e.package_sha256 for e in index.entries} == {
        _sha((tmp_path / e.package_file).read_bytes()) for e in index.entries
    }
    # A rerun retrieves nothing and reuses every immutable package.
    rerun = plan_reference_context(project_root=tmp_path, book=BOOK)
    assert rerun.pending == () and sorted(rerun.reusable) == ["001-001", "002-001"]
    assert execute_reference_context(rerun).retrieved == 0


def test_book_scoped_workspace_never_falls_back_to_the_first_books_registry(tmp_path: Path) -> None:
    series_project(tmp_path)
    config = load_book_config(tmp_path / f"config/books/{BOOK}.yaml")
    plain = config.model_copy(update={"series_terminology_policy": None, "reference_corpus": None})
    lineage = resolve_terminology_lineage(project_root=tmp_path, book=BOOK, config=plain)
    assert lineage.kind == "none" and lineage.terminology is None
    (tmp_path / f"config/books/{BOOK}.yaml").write_text(
        (tmp_path / f"config/books/{BOOK}.yaml")
        .read_text(encoding="utf-8")
        .replace("series_terminology_policy: config/series/synthetic/terminology-policy.yaml\n", "")
        .replace("reference_corpus: config/series/synthetic/reference-corpus.yaml\n", ""),
        encoding="utf-8",
    )
    plan = plan_snapshot(project_root=tmp_path, book=BOOK)
    assert plan.manifest.terminology is None
    assert plan.manifest.series_terminology is None
    with pytest.raises(ValueError, match="configured together"):
        BookConfig.model_validate({**config.model_dump(mode="json"), "reference_corpus": None})
    with pytest.raises(ValueError, match="cannot name both"):
        BookConfig.model_validate(
            {**config.model_dump(mode="json"), "terminology": "config/terminology.yaml"}
        )


def test_snapshot_fails_closed_until_the_derived_artifacts_are_current(tmp_path: Path) -> None:
    series_project(tmp_path)
    with pytest.raises(SnapshotError, match="derived book terminology registry is missing"):
        plan_snapshot(project_root=tmp_path, book=BOOK)
    execute_book_terminology(plan_book_terminology(project_root=tmp_path, book=BOOK))
    with pytest.raises(SnapshotError, match="reference context index is missing"):
        plan_snapshot(project_root=tmp_path, book=BOOK)
    execute_reference_context(plan_reference_context(project_root=tmp_path, book=BOOK))
    plan = plan_snapshot(project_root=tmp_path, book=BOOK)
    assert plan.manifest.series_terminology is not None
    # A policy edit makes the derived registry stale, and the planner refuses rather than reads.
    policy_path = tmp_path / "config/series/synthetic/terminology-policy.yaml"
    save_policy(
        set_explicit_term(
            load_policy(policy_path),
            source_term="Thom",
            target_term=FORM_THOM_2,
            classification=TerminologyType.PERSON,
            rationale="settled",
        ),
        policy_path,
    )
    with pytest.raises(SnapshotError, match="stale against the series policy"):
        plan_snapshot(project_root=tmp_path, book=BOOK)


def test_snapshot_freezes_series_policy_and_corpus_identities(tmp_path: Path) -> None:
    series_project(tmp_path)
    _prepare(tmp_path)
    plan = plan_snapshot(project_root=tmp_path, book=BOOK)
    manifest = plan.manifest
    assert manifest.series_terminology is not None and manifest.reference_corpus is not None
    assert manifest.reference_context is not None
    assert manifest.terminology_context_contract_version == TERMINOLOGY_CONTEXT_CONTRACT_VERSION
    assert manifest.terminology is not None
    assert (
        manifest.terminology.file.as_posix() == f"workspace/books/{BOOK}/terminology/registry.yaml"
    )
    corpora = manifest.reference_corpus.corpora
    assert [(c.corpus_id, c.corpus_version) for c in corpora] == [
        ("book1", "V2.1"),
        ("book2", "V2.1"),
    ]
    assert corpora[0].sha256 == _sha(b"synthetic corpus one")
    assert corpora[1].sha256 == _sha(b"synthetic corpus two")
    assert manifest.reference_corpus.retriever_sha256 == _sha(
        (tmp_path / "external/retriever.py").read_bytes()
    )
    assert manifest.series_terminology.sha256 == _sha(
        (tmp_path / "config/series/synthetic/terminology-policy.yaml").read_bytes()
    )
    payload = manifest.model_dump(mode="json")
    assert (
        persisted_manifest_identity_format(payload) is ManifestIdentityFormat.SERIES_TERMINOLOGY_V1
    )
    result = create_snapshot(plan, now=lambda: NOW)
    paths = ProjectPaths(tmp_path, BOOK)
    assert (
        paths.frozen_resource_category("series-terminology")
        / f"{manifest.series_terminology.sha256}.yaml"
    ).is_file()
    assert (
        paths.frozen_resource_category("reference-corpus")
        / f"{manifest.reference_corpus.sha256}.yaml"
    ).is_file()
    assert (
        paths.frozen_resource_category("reference-context-index")
        / f"{manifest.reference_context.sha256}.yaml"
    ).is_file()
    validation = validate_snapshot(result.snapshot_id, project_root=tmp_path, book=BOOK)
    assert validation.report.valid
    assert "frozen_series_terminology_resource" in validation.report.checks_performed
    assert "frozen_reference_context_packages" in validation.report.checks_performed
    assert validation.lineage_current
    # The snapshot record mirrors the versions and the manifest rejects a partial series block.
    assert (
        validation.snapshot is not None
        and validation.snapshot.series_terminology_version == "1.0.0"
    )
    with pytest.raises(ValueError, match="together"):
        TranslationManifest.model_validate({**payload, "reference_context": None})


def test_snapshot_validation_detects_changed_live_policy_without_invalidating_history(
    tmp_path: Path,
) -> None:
    series_project(tmp_path)
    _prepare(tmp_path)
    result = create_snapshot(plan_snapshot(project_root=tmp_path, book=BOOK), now=lambda: NOW)
    policy_path = tmp_path / "config/series/synthetic/terminology-policy.yaml"
    save_policy(
        set_explicit_term(
            load_policy(policy_path),
            source_term="Thom",
            target_term=FORM_THOM_2,
            classification=TerminologyType.PERSON,
            rationale="settled",
        ),
        policy_path,
    )
    validation = validate_snapshot(result.snapshot_id, project_root=tmp_path, book=BOOK)
    assert validation.report.valid
    assert not validation.lineage_current
    assert any("not current" in warning for warning in validation.report.warnings)
    # Corrupting a frozen package is corruption, not history.
    manifest = TranslationManifest.model_validate(
        yaml.safe_load((result.path / "manifest.yaml").read_text(encoding="utf-8"))
    )
    assert manifest.reference_context is not None and manifest.reference_context.frozen_file
    index = ReferenceContextIndex.model_validate(
        yaml.safe_load((tmp_path / manifest.reference_context.frozen_file).read_text())
    )
    (tmp_path / index.entries[0].package_file).write_text("tampered\n", encoding="utf-8")
    broken = validate_snapshot(result.snapshot_id, project_root=tmp_path, book=BOOK)
    assert not broken.report.valid
    assert any("reference package" in error for error in broken.report.errors)


def test_prompt_package_renders_the_policy_section_and_stays_valid_after_policy_edits(
    tmp_path: Path,
) -> None:
    series_project(tmp_path)
    _prepare(tmp_path)
    snapshot = create_snapshot(plan_snapshot(project_root=tmp_path, book=BOOK), now=lambda: NOW)
    run = create_run(plan_run(snapshot.path, project_root=tmp_path, now=lambda: NOW))
    plan = plan_prompt_packages(run.path, project_root=tmp_path, now=lambda: NOW)
    execute_prompt_packages(plan)
    first = next(item for item in plan.items if item.chunk.chunk_id == "001-001")
    second = next(item for item in plan.items if item.chunk.chunk_id == "002-001")
    user_text = (first.package_directory / "user.md").read_text(encoding="utf-8")
    section = extract_framed_bytes(user_text, "terminology-policy").decode("utf-8")
    assert "## 1. LOCKED CONSISTENCY TERMS" in section and FORM_AJAH_USER in section
    assert "## 4. REFERENCE CONTEXT" in section and FORM_CLEAN_TARGET in section
    assert FORM_ANOMALOUS_TARGET not in user_text
    glossary = extract_framed_bytes(user_text, "glossary-data").decode("utf-8")
    assert "Relevant canonical terminology" not in glossary
    other = extract_framed_bytes(
        (second.package_directory / "user.md").read_text(encoding="utf-8"), "terminology-policy"
    ).decode("utf-8")
    # Chunk two names Lanfear in its source and Ajah only in its comprehension context.
    assert other != section and FORM_LANFEAR in other and FORM_LANFEAR not in section
    package = PromptPackage.model_validate(
        yaml.safe_load((first.package_directory / "package.yaml").read_text(encoding="utf-8"))
    )
    assert package.series_terminology is not None and package.reference_context is not None
    assert package.terminology_context_contract_version == TERMINOLOGY_CONTEXT_CONTRACT_VERSION
    assert package.reference_context.sha256 != second.package.reference_context.sha256  # type: ignore[union-attr]
    assert validate_prompt_package(run.path, first.job.job_id, project_root=tmp_path).valid
    # Live policy changes afterwards do not touch the frozen package or its validity.
    policy_path = tmp_path / "config/series/synthetic/terminology-policy.yaml"
    save_policy(
        set_explicit_term(
            load_policy(policy_path),
            source_term="Aes Sedai",
            target_term="x",
            classification=TerminologyType.TITLE,
            rationale="changed later",
        ),
        policy_path,
    )
    # The frozen package is immutable and its stored validation stands; the snapshot has become
    # historical for new work, so a re-render is refused rather than silently changed.
    assert (first.package_directory / "user.md").read_text(encoding="utf-8") == user_text
    stored = yaml.safe_load(
        (first.package_directory / "validation-report.yaml").read_text(encoding="utf-8")
    )
    assert stored["valid"] and stored["package_id"] == package.package_id
    with pytest.raises(PromptPackageError, match="not valid"):
        plan_prompt_packages(run.path, project_root=tmp_path, force=True, now=lambda: NOW)


def test_legacy_manifest_identity_is_unchanged_by_the_series_fields() -> None:
    from tests.test_translation_snapshot import sample_manifest

    manifest = sample_manifest()
    payload = json.loads(canonical_manifest_bytes(manifest))
    assert not any(key.startswith("series") or key.startswith("reference") for key in payload)
    assert "terminology_context_contract_version" not in payload
    assert persisted_manifest_identity_format(manifest.model_dump(mode="json")) is not (
        ManifestIdentityFormat.SERIES_TERMINOLOGY_V1
    )


def test_cli_commands_are_registered_and_dry_runs_write_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    series_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    registry = ProjectPaths(tmp_path, BOOK).book_terminology_registry
    result = runner.invoke(app, ["derive-book-terminology", "--book", BOOK])
    assert result.exit_code == 0, result.output
    assert not registry.exists()
    result = runner.invoke(app, ["prepare-reference-context", "--book", BOOK])
    assert result.exit_code == 0, result.output
    assert not ProjectPaths(tmp_path, BOOK).reference_context_index.exists()
    result = runner.invoke(app, ["validate-series-terminology", "--book", BOOK])
    assert result.exit_code == 1
    assert "derived book terminology registry is missing" in result.output
    result = runner.invoke(app, ["derive-book-terminology", "--book", BOOK, "--apply"])
    assert result.exit_code == 0, result.output
    assert registry.is_file()
    result = runner.invoke(app, ["prepare-reference-context", "--book", BOOK, "--apply"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["validate-series-terminology", "--book", BOOK])
    assert result.exit_code == 0, result.output
    output = tmp_path / "context.txt"
    result = runner.invoke(
        app, ["show-terminology-context", "001-001", "--book", BOOK, "--output", output.as_posix()]
    )
    assert result.exit_code == 0, result.output
    assert "LOCKED CONSISTENCY TERMS" in output.read_text(encoding="utf-8")
    assert FORM_CLEAN_TARGET not in result.output
    assert f"Ajah → {FORM_AJAH_USER}" in result.output
