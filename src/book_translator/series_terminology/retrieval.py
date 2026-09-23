"""Integration of the external read-only cross-book retriever.

The retriever script and the corpora it opens are frozen external evidence. This module executes
the script from bytes whose hash the configuration pins, verifies every corpus hash before a
connection is opened, and converts the retriever's context into the project's typed reference
package, keeping clean evidence and provenance only.
"""

import hashlib
import sys
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import ValidationError

from book_translator.hashing import sha256_file
from book_translator.models import (
    SCHEMA_VERSION,
    ObservedVariantSupport,
    ReferenceContextPackage,
    ReferenceCorpusConfig,
    ReferenceCorpusIdentity,
    ReferenceEvidenceQuality,
    ReferenceEvidenceSource,
    ReferenceExclusions,
    ReferencePhrase,
    ReferenceProvenance,
    ReferenceRetrievalIdentity,
    ReferenceSegment,
    ReferenceStyleSample,
    ReferenceTargetForm,
    ReferenceTermEvidence,
    ReferenceVoiceSample,
)
from book_translator.series_terminology.corpus import SqliteCorpusReader
from book_translator.series_terminology.identity import REFERENCE_CONTEXT_CONTRACT_VERSION

#: Clip budgets keep a package bounded; the retriever's own prompt renderer uses the same limits.
SEGMENT_SOURCE_CHARS = 900
SEGMENT_TARGET_CHARS = 1100
STYLE_SOURCE_CHARS = 650
STYLE_TARGET_CHARS = 800
VOICE_CHARS = 320


class ReferenceRetrievalError(ValueError):
    """Raised when the external retriever or corpora cannot be trusted or reached."""


class ReferenceRetriever(Protocol):
    """What the workflow needs from a retriever; the adapter and test doubles provide it."""

    def context(self, text: str) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


def load_reference_corpus_config(path: Path) -> ReferenceCorpusConfig:
    try:
        return ReferenceCorpusConfig.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ReferenceRetrievalError(
            f"invalid reference corpus configuration {path}: {exc}"
        ) from exc


def _resolve(path: Path, project_root: Path) -> Path:
    return path if path.is_absolute() else project_root / path


def load_retriever_module(path: Path, *, expected_sha256: str) -> types.ModuleType:
    """Execute the retriever from verified bytes without writing bytecode beside it."""
    try:
        source = path.read_bytes()
    except OSError as exc:
        raise ReferenceRetrievalError(f"retriever script is unreadable: {path}") from exc
    actual = hashlib.sha256(source).hexdigest()
    if actual != expected_sha256:
        raise ReferenceRetrievalError(
            f"retriever script hash mismatch for {path}: expected {expected_sha256}, found {actual}"
        )
    module = types.ModuleType("book_translator_external_cross_book_retriever")
    module.__file__ = str(path)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    finally:
        sys.dont_write_bytecode = previous
    return module


def verify_reference_corpus(
    config: ReferenceCorpusConfig, *, project_root: Path
) -> ReferenceRetrievalIdentity:
    """Prove every configured external input still has the pinned identity."""
    retriever_path = _resolve(config.retriever.path, project_root)
    if not retriever_path.is_file():
        raise ReferenceRetrievalError(f"retriever script not found: {retriever_path}")
    retriever_hash = sha256_file(retriever_path)
    if retriever_hash != config.retriever.sha256:
        raise ReferenceRetrievalError("retriever script does not match its pinned hash")
    corpora = []
    for entry in config.corpora:
        corpus_path = _resolve(entry.path, project_root)
        if not corpus_path.is_file():
            raise ReferenceRetrievalError(f"reference corpus not found: {corpus_path}")
        actual = sha256_file(corpus_path)
        if actual != entry.sha256:
            raise ReferenceRetrievalError(
                f"reference corpus {entry.corpus_id} does not match its pinned hash"
            )
        manifest_hash = None
        if entry.freeze_manifest_file is not None:
            manifest_path = _resolve(entry.freeze_manifest_file, project_root)
            if not manifest_path.is_file():
                raise ReferenceRetrievalError(
                    f"freeze manifest not found for corpus {entry.corpus_id}: {manifest_path}"
                )
            manifest_hash = sha256_file(manifest_path)
            if manifest_hash != entry.freeze_manifest_sha256:
                raise ReferenceRetrievalError(
                    f"freeze manifest of corpus {entry.corpus_id} does not match its pinned hash"
                )
        corpora.append(
            ReferenceCorpusIdentity(
                corpus_id=entry.corpus_id,
                book_number=entry.book_number,
                label=entry.label,
                path=corpus_path.as_posix(),
                sha256=actual,
                corpus_version=entry.corpus_version,
                freeze_manifest_sha256=manifest_hash,
            )
        )
    return ReferenceRetrievalIdentity(
        retriever_path=retriever_path.as_posix(),
        retriever_sha256=retriever_hash,
        retriever_schema_version=config.retriever.schema_version,
        corpora=corpora,
        limits=config.limits,
    )


@dataclass(frozen=True, slots=True)
class CrossBookRetrieverAdapter:
    """The external retriever behind the project's protocol, opened only on verified corpora."""

    module: types.ModuleType
    retriever: Any
    config: ReferenceCorpusConfig
    identity: ReferenceRetrievalIdentity

    def context(self, text: str) -> Mapping[str, Any]:
        result: Mapping[str, Any] = self.retriever.context(
            text,
            limits=self.config.limits.model_dump(),
            include_uncertain=False,
            include_reference_only=False,
        )
        return result

    def corpus_reader(self) -> SqliteCorpusReader:
        databases = {
            entry.corpus_id: self.retriever.dbs[entry.book_number] for entry in self.config.corpora
        }
        derive: Callable[[str], list[tuple[str, ...]]] = self.module.derive_match_forms
        return SqliteCorpusReader(databases, derive_match_forms=derive)

    def close(self) -> None:
        self.retriever.close()


def open_reference_retriever(
    config: ReferenceCorpusConfig, *, project_root: Path
) -> CrossBookRetrieverAdapter:
    identity = verify_reference_corpus(config, project_root=project_root)
    module = load_retriever_module(
        _resolve(config.retriever.path, project_root), expected_sha256=config.retriever.sha256
    )
    schema_version = getattr(module, "SCHEMA_VERSION", None)
    if schema_version != config.retriever.schema_version:
        raise ReferenceRetrievalError(
            f"retriever schema version {schema_version!r} does not match the configured "
            f"{config.retriever.schema_version!r}"
        )
    books = tuple(getattr(module, "BOOKS", ()))
    configured = tuple(sorted(entry.book_number for entry in config.corpora))
    if tuple(sorted(books)) != configured:
        raise ReferenceRetrievalError(
            f"retriever serves books {books}, but the configuration pins {configured}"
        )
    db_paths = {
        entry.book_number: _resolve(entry.path, project_root).resolve() for entry in config.corpora
    }
    retriever = module.CrossBookRetriever(db_paths=db_paths)
    opened = {book: Path(db.path).resolve() for book, db in retriever.dbs.items()}
    if opened != db_paths:
        retriever.close()
        raise ReferenceRetrievalError(
            "retriever opened databases other than the configured corpora"
        )
    return CrossBookRetrieverAdapter(module, retriever, config, identity)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _corpus_id(book: Any) -> str:
    return f"book{int(book)}"


def _quality(row: Mapping[str, Any]) -> ReferenceEvidenceQuality:
    if row.get("diagnostic"):
        return ReferenceEvidenceQuality.UNCERTAIN
    if row.get("ocr_suspect"):
        return ReferenceEvidenceQuality.OCR_SUSPECT
    return ReferenceEvidenceQuality.CLEAN


def _surfaces(raw: Any) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    ranked = sorted(((str(k), int(v)) for k, v in raw.items()), key=lambda i: (-i[1], i[0]))
    return dict(ranked[:5])


def _forms(
    groups: list[Mapping[str, Any]], exclusions: dict[str, int]
) -> list[ReferenceTargetForm]:
    forms: list[ReferenceTargetForm] = []
    for group in groups:
        support: list[ObservedVariantSupport] = []
        for row in group.get("evidence", []):
            quality = _quality(row)
            if quality is ReferenceEvidenceQuality.UNCERTAIN:
                exclusions["uncertain_forms_withheld"] += 1
                continue
            if quality is ReferenceEvidenceQuality.OCR_SUSPECT:
                exclusions["ocr_suspect_forms_labelled"] += 1
            support.append(
                ObservedVariantSupport(
                    corpus_id=_corpus_id(row["book"]),
                    source=ReferenceEvidenceSource(str(row.get("origin", "narrative"))),
                    quality=quality,
                    observation_count=int(row.get("observation_count", 0)),
                    reference_id=str(row.get("target_form_id") or row.get("phrase_id") or ""),
                    observed_surfaces=_surfaces(row.get("observed_surfaces")),
                )
            )
        if support:
            forms.append(
                ReferenceTargetForm(
                    target_form=str(group["target"]),
                    support=sorted(support, key=lambda i: (i.corpus_id, i.source.value)),
                )
            )
    return forms


def _term_items(
    items: list[Mapping[str, Any]], kind: str, exclusions: dict[str, int]
) -> list[ReferenceTermEvidence]:
    found = []
    for item in items:
        if item.get("match_quality") != "exact":
            exclusions["ambiguous_matches"] += 1
            continue
        forms = _forms(list(item.get("observed_variants", [])), exclusions)
        if not forms:
            continue
        categories = (
            sorted({str(c["category"]) for c in item.get("concepts", [])})
            if kind == "terminology"
            else [str(t) for t in item.get("entity_types", [])]
        )
        found.append(
            ReferenceTermEvidence(
                source_term=str(item["source"]),
                kind=kind,  # type: ignore[arg-type]
                categories=categories,
                occurrences_in_passage=max(1, int(item.get("occurrences_in_passage", 1))),
                cross_book_observation=str(item.get("cross_book_observation", "unknown")),
                forms=forms,
            )
        )
    return found


def _provenance(row: Mapping[str, Any], reference_key: str) -> ReferenceProvenance:
    return ReferenceProvenance(
        corpus_id=_corpus_id(row["book"]),
        book_label=f"Book {int(row['book'])}",
        chapter=int(row["chapter"]) if row.get("chapter") is not None else None,
        source=ReferenceEvidenceSource.NARRATIVE,
        quality=ReferenceEvidenceQuality.CLEAN,
        reference_id=str(row[reference_key]),
    )


def package_from_context(
    context: Mapping[str, Any],
    *,
    book_id: str,
    chapter_id: str,
    chunk_id: str,
    source_sha256: str,
    retrieval: ReferenceRetrievalIdentity,
) -> ReferenceContextPackage:
    """Convert a retriever context into the typed package, carrying clean evidence only."""
    exclusions: dict[str, int] = {
        "reference_only_tm": 0,
        "anomalies": 0,
        "uncertain_forms_withheld": 0,
        "ambiguous_matches": 0,
        "ocr_suspect_forms_labelled": 0,
    }
    diagnostics = context.get("diagnostics", {})
    exclusions["reference_only_tm"] = len(diagnostics.get("reference_only_tm", []))
    exclusions["anomalies"] = len(diagnostics.get("anomalies", []))
    segments = []
    for row in context.get("translation_memory", []):
        if row.get("diagnostic") or row.get("status") != "clean":
            exclusions["reference_only_tm"] += 1
            continue
        segments.append(
            ReferenceSegment(
                provenance=_provenance(row, "tm_id"),
                relevance_score=float(row.get("relevance_score", 0.0)),
                minimum_alignment_confidence=float(row.get("minimum_alignment_confidence", 0.0)),
                source_text=_clip(row["source_text"], SEGMENT_SOURCE_CHARS),
                target_text=_clip(row["target_text"], SEGMENT_TARGET_CHARS),
            )
        )
    phrases = []
    for item in context.get("phrases", []):
        forms = _forms(list(item.get("observed_variants", [])), exclusions)
        if forms:
            phrases.append(
                ReferencePhrase(
                    source_phrase=str(item["source"]),
                    categories=[str(c) for c in item.get("categories", [])],
                    forms=forms,
                )
            )
    voice = [
        ReferenceVoiceSample(
            character=str(row["character"]),
            provenance=_provenance(row, "voice_id"),
            attribution=str(row.get("attribution_tag") or ""),
            source_dialogue=_clip(row["english_dialogue"], VOICE_CHARS),
            target_dialogue=_clip(row["persian_dialogue"], VOICE_CHARS),
        )
        for row in context.get("character_voice", [])
        if row.get("english_dialogue") and row.get("persian_dialogue")
    ]
    style = [
        ReferenceStyleSample(
            provenance=_provenance(row, "style_id"),
            tags=[str(t) for t in row.get("tags", [])],
            source_text=_clip(row["source_text"], STYLE_SOURCE_CHARS),
            target_text=_clip(row["target_text"], STYLE_TARGET_CHARS),
        )
        for row in context.get("style", [])
        if row.get("source_text") and row.get("target_text")
    ]
    terminology = _term_items(list(context.get("terminology", [])), "terminology", exclusions)
    entities = _term_items(list(context.get("entities", [])), "entity", exclusions)
    return ReferenceContextPackage(
        schema_version=SCHEMA_VERSION,
        contract_version=REFERENCE_CONTEXT_CONTRACT_VERSION,
        book_id=book_id,
        chapter_id=chapter_id,
        chunk_id=chunk_id,
        source_sha256=source_sha256,
        retrieval=retrieval,
        terminology=terminology,
        entities=entities,
        translation_memory=segments,
        phrases=phrases,
        character_voice=voice,
        style=style,
        exclusions=ReferenceExclusions(**exclusions),
        warnings=[str(w) for w in context.get("warnings", [])],
    )
