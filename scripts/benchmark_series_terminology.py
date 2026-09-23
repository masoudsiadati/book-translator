"""Benchmark the provider-free series terminology stages on synthetic material.

Measures registry derivation over a policy of realistic size, then effective-context assembly and
rendering for every chunk with a synthetic reference package, over three iterations. No corpus,
retriever, or provider is contacted; external retrieval is timed separately by the operator
because it is dominated by the storage the corpora live on.
"""

import hashlib
import statistics
import time
import tracemalloc

import yaml

from book_translator.models import (
    SCHEMA_VERSION,
    ObservedVariant,
    ObservedVariantSupport,
    ReferenceContextPackage,
    ReferenceCorpusIdentity,
    ReferenceEvidenceQuality,
    ReferenceEvidenceSource,
    ReferenceProvenance,
    ReferenceRetrievalIdentity,
    ReferenceRetrievalLimits,
    ReferenceSegment,
    ReferenceTargetForm,
    ReferenceTermEvidence,
    SeriesProvenanceKind,
    SeriesTerm,
    SeriesTerminologyPolicy,
    SeriesTermProvenance,
    SeriesTermStatus,
    TerminologyType,
)
from book_translator.series_terminology.context import (
    build_effective_context,
    render_effective_context,
)
from book_translator.series_terminology.derivation import ChunkText, derive_book_registry
from book_translator.series_terminology.identity import (
    PROMOTION_RULE_VERSION,
    REFERENCE_CONTEXT_CONTRACT_VERSION,
    concept_identity,
)
from book_translator.terminology.registry import canonical_registry_bytes

ITERATIONS = 3
TERM_COUNT = 1200
CHUNK_COUNT = 227
PARAGRAPHS_PER_CHUNK = 30


def _support(book: str, count: int) -> ObservedVariantSupport:
    return ObservedVariantSupport(
        corpus_id=book,
        source=ReferenceEvidenceSource.NARRATIVE,
        quality=ReferenceEvidenceQuality.CLEAN,
        observation_count=count,
        reference_id=f"TTF-{book}-0001-01",
    )


def synthetic_policy() -> SeriesTerminologyPolicy:
    terms = []
    for index in range(TERM_COUNT):
        source = f"Term{index:04d}"
        target = f"واژه{index:04d}"
        variants = [ObservedVariant(target_form=target, support=[_support("book1", 30)])]
        if index % 3 == 0:
            status, kind = SeriesTermStatus.LOCKED, SeriesProvenanceKind.EXPLICIT_PROJECT
            variants.append(
                ObservedVariant(target_form=target + "ب", support=[_support("book2", 12)])
            )
        elif index % 3 == 1:
            status, kind = (
                SeriesTermStatus.STRONG_REFERENCE,
                SeriesProvenanceKind.PROMOTED_REFERENCE,
            )
        else:
            status, kind = SeriesTermStatus.OBSERVED_VARIANTS, SeriesProvenanceKind.UNRESOLVED
            variants.append(
                ObservedVariant(target_form=target + "پ", support=[_support("book2", 25)])
            )
        terms.append(
            SeriesTerm(
                concept_id=concept_identity(source, "en"),
                source_term=source,
                classification=TerminologyType.PERSON if index % 2 else TerminologyType.PLACE,
                status=status,
                target_term=target if status is not SeriesTermStatus.OBSERVED_VARIANTS else None,
                provenance=SeriesTermProvenance(
                    kind=kind, origin="benchmark", rationale="synthetic", evidence_summary="x"
                ),
                variants=variants,
            )
        )
    return SeriesTerminologyPolicy(
        schema_version=SCHEMA_VERSION,
        version="1.0.0",
        series_id="synthetic-series",
        source_language="en",
        target_language="fa",
        promotion_rule_version=PROMOTION_RULE_VERSION,
        terms=terms,
    )


def synthetic_chunks() -> tuple[ChunkText, ...]:
    chunks = []
    for index in range(CHUNK_COUNT):
        chapter = f"{index // 4 + 1:03d}"
        paragraphs = [
            f"Paragraph {p} mentions Term{(index * 7 + p * 13) % TERM_COUNT:04d} and "
            f"Term{(index * 11 + p) % TERM_COUNT:04d} while walking the road."
            for p in range(PARAGRAPHS_PER_CHUNK)
        ]
        chunks.append(
            ChunkText(
                chunk_id=f"{chapter}-{index % 4 + 1:03d}",
                chapter_id=chapter,
                source_text="\n\n".join(paragraphs) + "\n",
                context_text=f"Term{(index * 5) % TERM_COUNT:04d} was there.\n",
            )
        )
    return tuple(chunks)


def synthetic_package(chunk: ChunkText) -> ReferenceContextPackage:
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
            )
        ],
        limits=ReferenceRetrievalLimits(),
    )
    provenance = ReferenceProvenance(
        corpus_id="book1",
        book_label="Book 1",
        chapter=3,
        source=ReferenceEvidenceSource.NARRATIVE,
        quality=ReferenceEvidenceQuality.CLEAN,
        reference_id="TM-B01-C03-0001",
    )
    return ReferenceContextPackage(
        schema_version=SCHEMA_VERSION,
        contract_version=REFERENCE_CONTEXT_CONTRACT_VERSION,
        book_id="synthetic-en-fa",
        chapter_id=chunk.chapter_id,
        chunk_id=chunk.chunk_id,
        source_sha256=hashlib.sha256(chunk.source_text.encode("utf-8")).hexdigest(),
        retrieval=identity,
        terminology=[
            ReferenceTermEvidence(
                source_term=f"Term{(i * 7) % TERM_COUNT:04d}",
                kind="terminology",
                categories=["person"],
                occurrences_in_passage=1,
                cross_book_observation="identical_variant_sets",
                forms=[
                    ReferenceTargetForm(target_form=f"واژه{i:04d}", support=[_support("book1", 4)])
                ],
            )
            for i in range(15)
        ],
        translation_memory=[
            ReferenceSegment(
                provenance=provenance,
                relevance_score=0.5,
                minimum_alignment_confidence=0.9,
                source_text=f"Synthetic reference sentence number {i}.",
                target_text=f"جملهٔ مرجع ساختگی شمارهٔ {i}.",
            )
            for i in range(8)
        ],
    )


def run_once() -> tuple[float, float, str]:
    policy = synthetic_policy()
    chunks = synthetic_chunks()
    packages = {chunk.chunk_id: synthetic_package(chunk) for chunk in chunks}
    start = time.perf_counter()
    registry, _ = derive_book_registry(policy, book_id="synthetic-en-fa", chunks=chunks)
    derivation = time.perf_counter() - start
    digest = hashlib.sha256(canonical_registry_bytes(registry))
    start = time.perf_counter()
    for chunk in chunks:
        rendered = render_effective_context(
            build_effective_context(
                policy,
                source_text=chunk.source_text,
                context_text=chunk.context_text,
                reference=packages[chunk.chunk_id],
            )
        )
        digest.update(rendered.encode("utf-8"))
    rendering = time.perf_counter() - start
    return derivation, rendering, digest.hexdigest()


def main() -> None:
    derivations: list[float] = []
    renderings: list[float] = []
    digests: set[str] = set()
    tracemalloc.start()
    for _ in range(ITERATIONS):
        derivation, rendering, digest = run_once()
        derivations.append(derivation)
        renderings.append(rendering)
        digests.add(digest)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if len(digests) != 1:
        raise SystemExit("series terminology benchmark was nondeterministic")
    report = {
        "iterations": ITERATIONS,
        "policy_terms": TERM_COUNT,
        "chunks": CHUNK_COUNT,
        "derivation_seconds": [round(value, 6) for value in derivations],
        "derivation_median_seconds": round(statistics.median(derivations), 6),
        "context_render_seconds": [round(value, 6) for value in renderings],
        "context_render_median_seconds": round(statistics.median(renderings), 6),
        "best_chunks_per_second": round(CHUNK_COUNT / min(renderings), 2),
        "peak_traced_bytes": peak,
        "deterministic_sha256": digests.pop(),
    }
    print(yaml.safe_dump(report, sort_keys=False), end="")


if __name__ == "__main__":
    main()
