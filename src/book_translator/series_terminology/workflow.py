"""File I/O orchestration for the series terminology layer.

Three governed operations live here beside a read-only lineage resolver:

- ``plan_policy_import`` / ``execute_policy_import`` build the series policy from explicit
  project decisions and corpus evidence;
- ``plan_book_terminology`` / ``execute_book_terminology`` derive the book's own registry;
- ``plan_reference_context`` / ``execute_reference_context`` retrieve one immutable reference
  package per chunk;
- ``resolve_terminology_lineage`` decides, without fallbacks, which terminology inputs a
  book-scoped snapshot may freeze, and proves the derived artifacts are current.
"""

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.config import BookConfig, ProjectDataError, load_book_config
from book_translator.hashing import sha256_file
from book_translator.models import (
    SCHEMA_VERSION,
    ChunkMetadata,
    ImportedPolicySource,
    ReferenceContextIndex,
    ReferenceContextIndexEntry,
    ReferenceContextPackage,
    ReferenceCorpusConfig,
    ReferenceRetrievalIdentity,
    SeriesPolicyAmendmentRecord,
    SeriesPolicyAmendmentRequest,
    SeriesProvenanceKind,
    SeriesTerminologyPolicy,
    SnapshotResourceKind,
    TerminologyRegistry,
    TocDocument,
    VersionedResourceMetadata,
)
from book_translator.paths import NARRATIVE_KINDS, ProjectPaths, narrative_chapter_id
from book_translator.series_terminology.amendment import (
    SERIES_POLICY_AMENDMENT_VERSION,
    amendment_identity,
    apply_amendment,
    request_sha256,
)
from book_translator.series_terminology.bootstrap import (
    ExplicitDecision,
    PolicyImportReport,
    PriorVolumeDecision,
    build_policy,
    explicit_decisions_from_glossary,
    explicit_decisions_from_registry,
    prior_volume_decisions,
)
from book_translator.series_terminology.corpus import CorpusConcept, CorpusReader
from book_translator.series_terminology.derivation import (
    ChunkText,
    DerivationSummary,
    derive_book_registry,
)
from book_translator.series_terminology.identity import (
    REFERENCE_CONTEXT_CONTRACT_VERSION,
    TERMINOLOGY_CONTEXT_CONTRACT_VERSION,
    index_identity,
    package_identity,
    policy_identity,
)
from book_translator.series_terminology.policy import (
    SeriesTerminologyError,
    load_policy,
    serialized_policy,
)
from book_translator.series_terminology.retrieval import (
    ReferenceRetrievalError,
    ReferenceRetriever,
    load_reference_corpus_config,
    open_reference_retriever,
    package_from_context,
    verify_reference_corpus,
)
from book_translator.terminology.registry import (
    canonical_registry_bytes,
    load_registry,
    save_registry,
)

LineageKind = Literal["none", "registry", "series"]


def _serialize(model: Any) -> str:
    rendered = yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise SeriesTerminologyError("could not serialize a series terminology artifact")
    return rendered


def _load_model[ModelT](path: Path, model: type[ModelT]) -> ModelT:
    try:
        return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))  # type: ignore[attr-defined,no-any-return]
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise SeriesTerminologyError(f"invalid artifact {path}: {exc}") from exc


def _project_paths(project_root: Path, book: str | None) -> ProjectPaths:
    try:
        return ProjectPaths(project_root, book)
    except ValueError as exc:
        raise SeriesTerminologyError(str(exc)) from exc


def _write_metadata(resource: Path, kind: SnapshotResourceKind, version: str, root: Path) -> None:
    metadata = VersionedResourceMetadata(
        schema_version=SCHEMA_VERSION,
        resource_kind=kind,
        resource_file=Path(resource.relative_to(root).as_posix()),
        version=version,
    )
    atomic_write_text(resource.with_suffix(".metadata.yaml"), _serialize(metadata), overwrite=True)


def _resource_version(resource: Path, kind: SnapshotResourceKind) -> str:
    metadata = _load_model(resource.with_suffix(".metadata.yaml"), VersionedResourceMetadata)
    if metadata.resource_kind is not kind:
        raise SeriesTerminologyError(f"resource kind mismatch for {resource}")
    return metadata.version


def load_book_chunks(project_root: Path, book: str | None) -> tuple[ChunkText, ...]:
    """Every narrative chunk of the book with its hash-verified source and context text."""
    paths = _project_paths(project_root, book)
    toc = _load_model(paths.toc, TocDocument)
    chunks: list[ChunkText] = []
    for entry in toc.entries:
        if entry.kind not in NARRATIVE_KINDS:
            continue
        chapter_id = narrative_chapter_id(entry.kind, entry.number)
        chunks_root = paths.chapter_directory(chapter_id) / "chunks"
        if not chunks_root.is_dir():
            raise SeriesTerminologyError(f"chapter {chapter_id} has no chunks")
        for directory in sorted(path for path in chunks_root.iterdir() if path.is_dir()):
            metadata = _load_model(directory / "metadata.yaml", ChunkMetadata)
            source_path = project_root / metadata.source_file
            context_path = project_root / metadata.context_file
            source = source_path.read_bytes()
            context = context_path.read_bytes()
            if hashlib.sha256(source).hexdigest() != metadata.source_sha256:
                raise SeriesTerminologyError(f"stale chunk source for {metadata.chunk_id}")
            if hashlib.sha256(context).hexdigest() != metadata.context_sha256:
                raise SeriesTerminologyError(f"stale chunk context for {metadata.chunk_id}")
            chunks.append(
                ChunkText(
                    chunk_id=metadata.chunk_id,
                    chapter_id=chapter_id,
                    source_text=source.decode("utf-8"),
                    context_text=context.decode("utf-8"),
                )
            )
    if not chunks:
        raise SeriesTerminologyError("the book has no narrative chunks")
    return tuple(chunks)


# --------------------------------------------------------------------------------------------
# Policy import
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyImportPlan:
    project_root: Path
    policy_path: Path
    version: str
    policy: SeriesTerminologyPolicy
    report: PolicyImportReport
    action: Literal["create", "update", "skip"]


def _explicit_from_existing(policy: SeriesTerminologyPolicy) -> tuple[ExplicitDecision, ...]:
    """Decisions a person recorded on the policy itself survive a re-import."""
    return tuple(
        ExplicitDecision(
            source_term=term.source_term,
            target_term=term.target_term or "",
            classification=term.classification,
            kind=SeriesProvenanceKind.EXPLICIT_USER,
            origin=term.provenance.origin,
            rationale=term.provenance.rationale,
            aliases=tuple(term.aliases),
        )
        for term in policy.terms
        if term.provenance.kind is SeriesProvenanceKind.EXPLICIT_USER
        and term.provenance.origin == "set-series-terminology"
    )


def plan_policy_import(
    *,
    project_root: Path,
    policy_path: Path,
    series_id: str,
    source_language: str,
    target_language: str,
    corpus_concepts: Iterable[CorpusConcept],
    corpus_sources: Iterable[ImportedPolicySource] = (),
    explicit_registry: Path | None = None,
    glossary: Path | None = None,
    prior_volume_registry: Path | None = None,
) -> PolicyImportPlan:
    project_root = project_root.resolve()
    policy_file = project_root / policy_path
    explicit: list[ExplicitDecision] = []
    sources: list[ImportedPolicySource] = list(corpus_sources)
    if explicit_registry is not None:
        registry_file = project_root / explicit_registry
        explicit.extend(
            explicit_decisions_from_registry(
                load_registry(registry_file), origin=explicit_registry.as_posix()
            )
        )
        sources.append(
            ImportedPolicySource(
                kind="registry",
                file=explicit_registry.as_posix(),
                sha256=sha256_file(registry_file),
            )
        )
    if glossary is not None:
        glossary_file = project_root / glossary
        raw = yaml.safe_load(glossary_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SeriesTerminologyError("the project glossary must be a mapping")
        explicit.extend(explicit_decisions_from_glossary(raw, origin=glossary.as_posix()))
        sources.append(
            ImportedPolicySource(
                kind="glossary", file=glossary.as_posix(), sha256=sha256_file(glossary_file)
            )
        )
    prior: tuple[PriorVolumeDecision, ...] = ()
    if prior_volume_registry is not None:
        prior_file = project_root / prior_volume_registry
        prior = prior_volume_decisions(load_registry(prior_file))
        sources.append(
            ImportedPolicySource(
                kind="prior_volume_registry",
                file=prior_volume_registry.as_posix(),
                sha256=sha256_file(prior_file),
            )
        )
    existing = load_policy(policy_file) if policy_file.is_file() else None
    if existing is not None:
        explicit.extend(_explicit_from_existing(existing))
    policy, report = build_policy(
        series_id=series_id,
        source_language=source_language,
        target_language=target_language,
        corpus_concepts=corpus_concepts,
        explicit=explicit,
        prior=prior,
    )
    version = existing.version if existing is not None else "1.0.0"
    policy = policy.model_copy(update={"imported_sources": sources, "version": version})
    action: Literal["create", "update", "skip"] = "create"
    if existing is not None:
        action = "skip" if serialized_policy(existing) == serialized_policy(policy) else "update"
    return PolicyImportPlan(project_root, policy_file, version, policy, report, action)


def execute_policy_import(plan: PolicyImportPlan) -> PolicyImportPlan:
    if plan.action != "skip":
        plan.policy_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(plan.policy_path, serialized_policy(plan.policy), overwrite=True)
    _write_metadata(
        plan.policy_path, SnapshotResourceKind.SERIES_TERMINOLOGY, plan.version, plan.project_root
    )
    return plan


# --------------------------------------------------------------------------------------------
# Book registry derivation
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BookTerminologyPlan:
    project_root: Path
    registry_path: Path
    registry: TerminologyRegistry
    summary: DerivationSummary
    policy_version: str
    action: Literal["create", "update", "skip"]


def _series_config(project_root: Path, book: str | None) -> tuple[ProjectPaths, BookConfig]:
    paths = _project_paths(project_root, book)
    try:
        config = load_book_config(paths.book_config)
    except ProjectDataError as exc:
        raise SeriesTerminologyError(str(exc)) from exc
    if config.series_terminology_policy is None or config.reference_corpus is None:
        raise SeriesTerminologyError(
            f"{paths.book_config.relative_to(project_root).as_posix()} names no series "
            "terminology policy and reference corpus"
        )
    return paths, config


def plan_book_terminology(*, project_root: Path, book: str | None = None) -> BookTerminologyPlan:
    project_root = project_root.resolve()
    paths, config = _series_config(project_root, book)
    policy_path = project_root / (config.series_terminology_policy or Path())
    policy = load_policy(policy_path)
    policy_version = _resource_version(policy_path, SnapshotResourceKind.SERIES_TERMINOLOGY)
    registry, summary = derive_book_registry(
        policy, book_id=config.book_id, chunks=load_book_chunks(project_root, book)
    )
    registry_path = paths.book_terminology_registry
    action: Literal["create", "update", "skip"] = "create"
    if registry_path.is_file():
        existing = load_registry(registry_path)
        action = (
            "skip"
            if canonical_registry_bytes(existing) == canonical_registry_bytes(registry)
            else "update"
        )
    return BookTerminologyPlan(
        project_root, registry_path, registry, summary, policy_version, action
    )


def execute_book_terminology(plan: BookTerminologyPlan) -> BookTerminologyPlan:
    if plan.action != "skip":
        plan.registry_path.parent.mkdir(parents=True, exist_ok=True)
        save_registry(plan.registry_path, plan.registry)
    _write_metadata(
        plan.registry_path, SnapshotResourceKind.TERMINOLOGY, plan.policy_version, plan.project_root
    )
    return plan


# --------------------------------------------------------------------------------------------
# Reference context retrieval
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReferenceContextPlan:
    project_root: Path
    book: str | None
    book_id: str
    index_path: Path
    config: ReferenceCorpusConfig
    identity: ReferenceRetrievalIdentity
    version: str
    chunks: tuple[ChunkText, ...]
    reusable: dict[str, ReferenceContextIndexEntry]
    pending: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReferenceContextResult:
    index_path: Path
    retrieved: int
    reused: int
    index_sha256: str
    warnings: tuple[str, ...] = field(default=())


def _load_index(path: Path) -> ReferenceContextIndex | None:
    return _load_model(path, ReferenceContextIndex) if path.is_file() else None


def plan_reference_context(
    *,
    project_root: Path,
    book: str | None = None,
    chapters: Iterable[str] | None = None,
    force: bool = False,
) -> ReferenceContextPlan:
    project_root = project_root.resolve()
    paths, config = _series_config(project_root, book)
    corpus_path = project_root / (config.reference_corpus or Path())
    corpus_config = load_reference_corpus_config(corpus_path)
    version = _resource_version(corpus_path, SnapshotResourceKind.REFERENCE_CORPUS)
    identity = verify_reference_corpus(corpus_config, project_root=project_root)
    chunks = load_book_chunks(project_root, book)
    selected = set(chapters) if chapters is not None else None
    existing = _load_index(paths.reference_context_index)
    reusable: dict[str, ReferenceContextIndexEntry] = {}
    if existing is not None and not force and existing.retrieval == identity:
        by_chunk = {chunk.chunk_id: chunk for chunk in chunks}
        for entry in existing.entries:
            chunk = by_chunk.get(entry.chunk_id)
            package = project_root / entry.package_file
            if (
                chunk is not None
                and hashlib.sha256(chunk.source_text.encode("utf-8")).hexdigest()
                == entry.source_sha256
                and package.is_file()
                and sha256_file(package) == entry.package_sha256
            ):
                reusable[entry.chunk_id] = entry
    pending = tuple(
        chunk.chunk_id
        for chunk in chunks
        if chunk.chunk_id not in reusable and (selected is None or chunk.chapter_id in selected)
    )
    return ReferenceContextPlan(
        project_root=project_root,
        book=book,
        book_id=config.book_id,
        index_path=paths.reference_context_index,
        config=corpus_config,
        identity=identity,
        version=version,
        chunks=chunks,
        reusable=reusable,
        pending=pending,
    )


def _publish_package(package: ReferenceContextPackage, paths: ProjectPaths, root: Path) -> Path:
    rendered = _serialize(package)
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    destination = paths.reference_context_package(digest)
    if destination.exists():
        if sha256_file(destination) != digest:
            raise SeriesTerminologyError(f"conflicting reference package exists: {destination}")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(destination, rendered)
    if sha256_file(destination) != digest:
        raise SeriesTerminologyError("reference package publication failed")
    return Path(destination.relative_to(root).as_posix())


def _write_index(plan: ReferenceContextPlan, entries: dict[str, ReferenceContextIndexEntry]) -> str:
    ordered = [entries[chunk.chunk_id] for chunk in plan.chunks if chunk.chunk_id in entries]
    index = ReferenceContextIndex(
        schema_version=SCHEMA_VERSION,
        contract_version=REFERENCE_CONTEXT_CONTRACT_VERSION,
        book_id=plan.book_id,
        retrieval=plan.identity,
        entries=ordered,
    )
    plan.index_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(plan.index_path, _serialize(index), overwrite=True)
    _write_metadata(
        plan.index_path, SnapshotResourceKind.REFERENCE_CONTEXT, plan.version, plan.project_root
    )
    return index_identity(index)


def execute_reference_context(
    plan: ReferenceContextPlan,
    *,
    retriever: ReferenceRetriever | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> ReferenceContextResult:
    """Retrieve every pending chunk, publishing packages immutably and the index incrementally."""
    paths = _project_paths(plan.project_root, plan.book)
    entries = dict(plan.reusable)
    owned = retriever is None
    active: ReferenceRetriever = (
        retriever
        if retriever is not None
        else open_reference_retriever(plan.config, project_root=plan.project_root)
    )
    warnings: list[str] = []
    retrieved = 0
    by_chunk = {chunk.chunk_id: chunk for chunk in plan.chunks}
    try:
        for position, chunk_id in enumerate(plan.pending, start=1):
            chunk = by_chunk[chunk_id]
            source_hash = hashlib.sha256(chunk.source_text.encode("utf-8")).hexdigest()
            try:
                context = active.context(chunk.source_text)
            except Exception as exc:  # the retriever is external code; wrap it, never swallow it
                raise ReferenceRetrievalError(
                    f"retrieval failed for chunk {chunk_id}: {exc}"
                ) from exc
            package = package_from_context(
                context,
                book_id=plan.book_id,
                chapter_id=chunk.chapter_id,
                chunk_id=chunk_id,
                source_sha256=source_hash,
                retrieval=plan.identity,
            )
            package_file = _publish_package(package, paths, plan.project_root)
            entries[chunk_id] = ReferenceContextIndexEntry(
                chunk_id=chunk_id,
                chapter_id=chunk.chapter_id,
                source_sha256=source_hash,
                package_file=package_file,
                package_sha256=sha256_file(plan.project_root / package_file),
            )
            if package.warnings:
                warnings.append(f"{chunk_id}: {len(package.warnings)} retrieval warnings")
            retrieved += 1
            # The index is republished after every chunk so an interrupted batch resumes from
            # the last published package rather than from nothing.
            _write_index(plan, entries)
            if progress is not None:
                progress(chunk_id, position, len(plan.pending))
    finally:
        if owned:
            active.close()
    index_hash = _write_index(plan, entries)
    return ReferenceContextResult(
        index_path=plan.index_path,
        retrieved=retrieved,
        reused=len(plan.reusable),
        index_sha256=index_hash,
        warnings=tuple(warnings),
    )


def package_identity_of(package: ReferenceContextPackage) -> str:
    return package_identity(package)


def load_reference_package(
    project_root: Path, book: str | None, chunk_id: str
) -> ReferenceContextPackage | None:
    """The current index's package for one chunk, hash-verified, or None when absent."""
    project_root = project_root.resolve()
    paths = _project_paths(project_root, book)
    index = _load_index(paths.reference_context_index)
    if index is None:
        return None
    entry = next((item for item in index.entries if item.chunk_id == chunk_id), None)
    if entry is None:
        return None
    package_path = project_root / entry.package_file
    if not package_path.is_file() or sha256_file(package_path) != entry.package_sha256:
        raise SeriesTerminologyError(
            f"reference package for chunk {chunk_id} is missing or corrupt"
        )
    return _load_model(package_path, ReferenceContextPackage)


def open_corpus_reader(
    config: ReferenceCorpusConfig, *, project_root: Path
) -> tuple[CorpusReader, Callable[[], None], tuple[ImportedPolicySource, ...]]:
    """Open the verified corpora for policy import; the caller closes what it opened."""
    adapter = open_reference_retriever(config, project_root=project_root)
    sources = tuple(
        ImportedPolicySource(kind="reference_corpus", file=corpus.path, sha256=corpus.sha256)
        for corpus in adapter.identity.corpora
    )
    return adapter.corpus_reader(), adapter.close, sources


# --------------------------------------------------------------------------------------------
# Lineage resolution for snapshot planning
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TerminologyLineage:
    """Which terminology inputs a snapshot may freeze, resolved without implicit fallbacks."""

    kind: LineageKind
    terminology: Path | None = None
    open_questions: Path | None = None
    series_policy: Path | None = None
    reference_corpus: Path | None = None
    reference_context_index: Path | None = None
    retrieval: ReferenceRetrievalIdentity | None = None
    context_contract_version: str | None = None


def resolve_terminology_lineage(
    *, project_root: Path, book: str | None, config: BookConfig
) -> TerminologyLineage:
    """Decide the terminology lineage of a book-scoped workspace and prove it is current.

    Fails closed: a book with a series policy must have a derived registry that is the exact
    projection of that policy over the current chunks, and a reference-context index that covers
    every chunk with a package hashing as recorded.
    """
    project_root = project_root.resolve()
    paths = _project_paths(project_root, book)
    if config.series_terminology_policy is None:
        return TerminologyLineage(
            kind="registry" if config.terminology is not None else "none",
            terminology=config.terminology,
            open_questions=config.terminology_questions,
        )
    policy_path = project_root / config.series_terminology_policy
    if not policy_path.is_file():
        raise SeriesTerminologyError(f"series terminology policy not found: {policy_path}")
    policy = load_policy(policy_path)
    corpus_path = project_root / (config.reference_corpus or Path())
    corpus_config = load_reference_corpus_config(corpus_path)
    chunks = load_book_chunks(project_root, book)
    registry_path = paths.book_terminology_registry
    if not registry_path.is_file():
        raise SeriesTerminologyError(
            "the derived book terminology registry is missing; run derive-book-terminology"
        )
    expected, _ = derive_book_registry(policy, book_id=config.book_id, chunks=chunks)
    if canonical_registry_bytes(load_registry(registry_path)) != canonical_registry_bytes(expected):
        raise SeriesTerminologyError(
            "the derived book terminology registry is stale against the series policy; "
            "run derive-book-terminology"
        )
    index = _load_index(paths.reference_context_index)
    if index is None:
        raise SeriesTerminologyError(
            "the reference context index is missing; run prepare-reference-context"
        )
    identity = verify_reference_corpus(corpus_config, project_root=project_root)
    if index.retrieval != identity:
        raise SeriesTerminologyError(
            "the reference context index was retrieved from different corpus identities; "
            "run prepare-reference-context"
        )
    entries = {entry.chunk_id: entry for entry in index.entries}
    for chunk in chunks:
        entry = entries.get(chunk.chunk_id)
        digest = hashlib.sha256(chunk.source_text.encode("utf-8")).hexdigest()
        if entry is None or entry.source_sha256 != digest:
            raise SeriesTerminologyError(
                f"reference context for chunk {chunk.chunk_id} is missing or stale; "
                "run prepare-reference-context"
            )
        package = project_root / entry.package_file
        if not package.is_file() or sha256_file(package) != entry.package_sha256:
            raise SeriesTerminologyError(
                f"reference package for chunk {chunk.chunk_id} is missing or corrupt"
            )
    return TerminologyLineage(
        kind="series",
        terminology=Path(registry_path.relative_to(project_root).as_posix()),
        open_questions=config.terminology_questions,
        series_policy=config.series_terminology_policy,
        reference_corpus=config.reference_corpus,
        reference_context_index=Path(
            paths.reference_context_index.relative_to(project_root).as_posix()
        ),
        retrieval=identity,
        context_contract_version=TERMINOLOGY_CONTEXT_CONTRACT_VERSION,
    )


# --------------------------------------------------------------------------------------------
# Governed policy amendment
# --------------------------------------------------------------------------------------------


AmendmentAction = Literal["apply", "complete", "skip"]


@dataclass(frozen=True, slots=True)
class PolicyAmendmentPlan:
    """A fully derived amendment; ``execute_policy_amendment`` only re-checks and writes it.

    ``apply`` publishes the record and then the policy; ``complete`` finishes an interrupted
    application whose record exists but whose policy was never replaced; ``skip`` means the
    policy already carries this exact amendment.
    """

    project_root: Path
    policy_path: Path
    record_path: Path
    record: SeriesPolicyAmendmentRecord
    policy: SeriesTerminologyPolicy
    policy_bytes: bytes
    action: AmendmentAction


def load_policy_amendments(project_root: Path) -> tuple[SeriesPolicyAmendmentRecord, ...]:
    directory = ProjectPaths(project_root.resolve()).series_policy_amendments
    if not directory.is_dir():
        return ()
    records = tuple(
        _load_model(path, SeriesPolicyAmendmentRecord)
        for path in sorted(directory.glob("spa-*.yaml"))
    )
    for record, path in zip(records, sorted(directory.glob("spa-*.yaml")), strict=True):
        if path.stem != record.amendment_id:
            raise SeriesTerminologyError(f"series policy amendment {path.name} names another ID")
    return records


def explain_policy_move(
    records: Iterable[SeriesPolicyAmendmentRecord],
    *,
    from_sha256: str,
    to_sha256: str,
    series_id: str,
) -> tuple[SeriesPolicyAmendmentRecord, ...] | None:
    """Return the contiguous chain of amendments leading from one policy file to another.

    ``()`` means the two files are identical; ``None`` means no chain of published amendments
    connects them, which is an unexplained move however small the difference is. The search is
    breadth-first over records sorted by identity, so the chain it returns is deterministic.
    """
    if from_sha256 == to_sha256:
        return ()
    edges: dict[str, list[SeriesPolicyAmendmentRecord]] = {}
    for record in sorted(records, key=lambda item: item.amendment_id):
        if record.series_id == series_id:
            edges.setdefault(record.policy_sha256_before, []).append(record)
    frontier: list[tuple[str, tuple[SeriesPolicyAmendmentRecord, ...]]] = [(from_sha256, ())]
    seen = {from_sha256}
    while frontier:
        following: list[tuple[str, tuple[SeriesPolicyAmendmentRecord, ...]]] = []
        for sha256, path in frontier:
            for record in edges.get(sha256, ()):
                chain = (*path, record)
                if record.policy_sha256_after == to_sha256:
                    return chain
                if record.policy_sha256_after not in seen:
                    seen.add(record.policy_sha256_after)
                    following.append((record.policy_sha256_after, chain))
        frontier = following
    return None


def plan_policy_amendment(
    *,
    project_root: Path,
    request_path: Path,
    book: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> PolicyAmendmentPlan:
    project_root = project_root.resolve()
    _, config = _series_config(project_root, book)
    policy_path = project_root / (config.series_terminology_policy or Path())
    request = _load_model(request_path, SeriesPolicyAmendmentRequest)
    current_bytes = policy_path.read_bytes()
    current_sha256 = hashlib.sha256(current_bytes).hexdigest()
    requested = request_sha256(request)
    paths = ProjectPaths(project_root)
    for existing in load_policy_amendments(project_root):
        if existing.request_sha256 != requested or existing.series_id != request.series_id:
            continue
        record_path = paths.series_policy_amendment(existing.amendment_id)
        if current_sha256 == existing.policy_sha256_after:
            policy = load_policy(policy_path)
            return PolicyAmendmentPlan(
                project_root, policy_path, record_path, existing, policy, current_bytes, "skip"
            )
        if current_sha256 == existing.policy_sha256_before:
            amended = apply_amendment(
                load_policy(policy_path), request, amendment_id=existing.amendment_id
            )
            amended_bytes = serialized_policy(amended.policy).encode("utf-8")
            if hashlib.sha256(amended_bytes).hexdigest() != existing.policy_sha256_after:
                raise SeriesTerminologyError(
                    "a published amendment no longer re-derives the policy it recorded"
                )
            return PolicyAmendmentPlan(
                project_root,
                policy_path,
                record_path,
                existing,
                amended.policy,
                amended_bytes,
                "complete",
            )
    policy = load_policy(policy_path)
    amendment_id = amendment_identity(current_sha256, request)
    amended = apply_amendment(policy, request, amendment_id=amendment_id)
    amended_bytes = serialized_policy(amended.policy).encode("utf-8")
    record = SeriesPolicyAmendmentRecord(
        schema_version=SCHEMA_VERSION,
        amendment_engine_version=SERIES_POLICY_AMENDMENT_VERSION,
        amendment_id=amendment_id,
        series_id=policy.series_id,
        policy_file=Path(policy_path.relative_to(project_root).as_posix()),
        policy_sha256_before=current_sha256,
        policy_sha256_after=hashlib.sha256(amended_bytes).hexdigest(),
        policy_identity_before=policy_identity(policy),
        policy_identity_after=policy_identity(amended.policy),
        policy_version_before=policy.version,
        policy_version_after=amended.policy.version,
        request=request,
        request_sha256=requested,
        changes=list(amended.changes),
        created_at=(now or (lambda: datetime.now(UTC)))(),
    )
    return PolicyAmendmentPlan(
        project_root,
        policy_path,
        paths.series_policy_amendment(amendment_id),
        record,
        amended.policy,
        amended_bytes,
        "apply",
    )


def execute_policy_amendment(plan: PolicyAmendmentPlan) -> PolicyAmendmentPlan:
    """Publish the record, then the policy, re-checking the policy bytes it was planned against.

    The record goes first so that no policy identity ever exists without the evidence explaining
    it; an interruption between the two writes leaves a record whose "before" still matches the
    live policy, which the next plan recognizes and completes.
    """
    if plan.action == "skip":
        return plan
    current = hashlib.sha256(plan.policy_path.read_bytes()).hexdigest()
    if current != plan.record.policy_sha256_before:
        raise SeriesTerminologyError("the series policy changed after the amendment was planned")
    if plan.record_path.exists():
        persisted = _load_model(plan.record_path, SeriesPolicyAmendmentRecord)
        if persisted.model_dump(exclude={"created_at"}) != plan.record.model_dump(
            exclude={"created_at"}
        ):
            raise SeriesTerminologyError("a different amendment record already uses this ID")
    else:
        plan.record_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(plan.record_path, _serialize(plan.record))
    atomic_write_text(plan.policy_path, plan.policy_bytes.decode("utf-8"), overwrite=True)
    if hashlib.sha256(plan.policy_path.read_bytes()).hexdigest() != plan.record.policy_sha256_after:
        raise SeriesTerminologyError("the published policy does not hash as the amendment recorded")
    _write_metadata(
        plan.policy_path,
        SnapshotResourceKind.SERIES_TERMINOLOGY,
        plan.record.policy_version_after,
        plan.project_root,
    )
    return plan
