"""Bounded corpus evidence for terminology conflict components."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from book_translator.models import TerminologyRegistry
from book_translator.models.terminology_reconciliation import (
    ComponentEvidence,
    ConflictComponent,
    ConflictGraph,
    TermOccurrenceEvidence,
)
from book_translator.terminology.registry import legacy_glossary_decisions

CONTEXT_RADIUS = 48
MAX_CONTEXTS_PER_TERM = 2
MAX_NEARBY_ENTITIES = 8


class CorpusIndex:
    """A read-only in-memory index of chunk sources for bounded evidence lookup."""

    def __init__(self, chunks: tuple[tuple[str, str, str], ...]) -> None:
        self._chunks = chunks
        self._vocabulary_key: tuple[str, ...] | None = None
        self._chunk_term_index: list[set[str]] = []

    @property
    def chapter_count(self) -> int:
        return len({chapter_id for chapter_id, _, _ in self._chunks})

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    @classmethod
    def load(cls, workspace: Path) -> CorpusIndex:
        chunks: list[tuple[str, str, str]] = []
        chapters = workspace / "chapters"
        if not chapters.is_dir():
            return cls(())
        for chapter in sorted(chapters.iterdir()):
            source_directory = chapter / "chunks"
            if not source_directory.is_dir():
                continue
            for chunk in sorted(source_directory.iterdir()):
                source = chunk / "source.md"
                if source.is_file():
                    chunks.append(
                        (chapter.name, f"{chapter.name}-{chunk.name}", source.read_text("utf-8"))
                    )
        return cls(tuple(chunks))

    def occurrences(self, term: str) -> tuple[int, list[str], list[str], list[tuple[str, int]]]:
        """Return counts, chapters, bounded contexts, and match offsets for one form."""
        pattern = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)")
        total = 0
        chapters: list[str] = []
        chunks: list[str] = []
        contexts: list[str] = []
        offsets: list[tuple[str, int]] = []
        for chapter_id, chunk_id, text in self._chunks:
            matches = list(pattern.finditer(text))
            if not matches:
                continue
            total += len(matches)
            chapters.append(chapter_id)
            chunks.append(chunk_id)
            for match in matches[:1]:
                offsets.append((chunk_id, match.start()))
                if len(contexts) < MAX_CONTEXTS_PER_TERM:
                    contexts.append(_window(text, match.start(), match.end()))
        return total, sorted(set(chapters)), contexts, offsets

    def _chunk_terms(self, vocabulary: tuple[str, ...]) -> list[set[str]]:
        """Index which vocabulary forms occur in each chunk exactly once per corpus."""
        if self._vocabulary_key != vocabulary:
            index: list[set[str]] = []
            for _chapter_id, _chunk_id, text in self._chunks:
                present = {
                    other
                    for other in vocabulary
                    if re.search(r"(?<!\w)" + re.escape(other) + r"(?!\w)", text)
                }
                index.append(present)
            self._vocabulary_key = vocabulary
            self._chunk_term_index = index
        return self._chunk_term_index

    def nearby_terms(self, term: str, vocabulary: tuple[str, ...]) -> list[str]:
        """Return known terminology forms co-occurring with *term* in the same chunk."""
        pattern = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)")
        index = self._chunk_terms(vocabulary)
        counts: Counter[str] = Counter()
        for position, (_chapter_id, _chunk_id, text) in enumerate(self._chunks):
            if pattern.search(text) is None:
                continue
            counts.update(index[position] - {term})
        return [item for item, _ in counts.most_common(MAX_NEARBY_ENTITIES)]


def _window(text: str, start: int, end: int) -> str:
    """Return a short single-line window; never a full prose passage."""
    left = max(0, start - CONTEXT_RADIUS)
    right = min(len(text), end + CONTEXT_RADIUS)
    return " ".join(text[left:right].split())


def collect_component_evidence(
    registry: TerminologyRegistry,
    graph: ConflictGraph,
    component: ConflictComponent,
    corpus: CorpusIndex,
    glossary: Any,
    *,
    anomalies: dict[str, list[str]] | None = None,
    review_dispositions: dict[str, str] | None = None,
) -> ComponentEvidence:
    """Gather only the bounded evidence needed to reason about one family."""
    node_by_id = {item.node_id: item for item in graph.nodes}
    candidates = {item.candidate_id: item for item in registry.candidates}
    decisions = {item.candidate_id: item for item in registry.decisions}
    legacy = {source: target for source, target, _ in legacy_glossary_decisions(glossary)}

    forms: list[str] = []
    for node_id in component.node_ids:
        node = node_by_id[node_id]
        forms.append(node.source_term)
        forms.extend(node.aliases)
    forms = sorted(set(forms))
    vocabulary = tuple(
        sorted({item.source_term for item in registry.candidates if len(item.source_term) > 3})
    )

    terms: list[TermOccurrenceEvidence] = []
    for form in forms:
        count, chapters, contexts, offsets = corpus.occurrences(form)
        terms.append(
            TermOccurrenceEvidence(
                source_form=form,
                occurrence_count=count,
                chapter_count=len(chapters),
                chunk_count=len({chunk_id for chunk_id, _ in offsets}),
                chapters=chapters[:12],
                capitalized=form[:1].isupper(),
                always_capitalized=all(part[:1].isupper() for part in form.split() if part),
                representative_contexts=contexts,
                nearby_entities=corpus.nearby_terms(form, vocabulary) if count else [],
            )
        )

    related: dict[str, str] = {}
    for node_id in component.node_ids:
        node = node_by_id[node_id]
        if node.target_term is not None:
            related[node.source_term] = node.target_term
    user_owned = [
        candidates[item.candidate_id].source_term
        for item in registry.decisions
        if item.decision_id in component.decision_ids and item.decision_source.value == "user"
    ]
    locked = [
        candidates[item.candidate_id].source_term
        for item in registry.decisions
        if item.decision_id in component.decision_ids and item.locked
    ]
    anomaly_ids: list[str] = []
    for form in forms:
        anomaly_ids.extend((anomalies or {}).get(form, []))
    dispositions = {
        form: (review_dispositions or {})[form]
        for form in forms
        if form in (review_dispositions or {})
    }
    del decisions
    return ComponentEvidence(
        component_id=component.component_id,
        terms=terms,
        legacy_glossary_entries={form: legacy[form] for form in forms if form in legacy},
        user_owned_decisions=sorted(set(user_owned)),
        locked_decisions=sorted(set(locked)),
        related_canonical_terms=dict(sorted(related.items())),
        historical_review_dispositions=dict(sorted(dispositions.items())),
        source_anomaly_ids=sorted(set(anomaly_ids)),
        corpus_chapter_count=corpus.chapter_count,
        corpus_chunk_count=corpus.chunk_count,
    )


def load_source_anomaly_index(path: Path) -> dict[str, list[str]]:
    """Map an anomaly's exact evidence text to its stable identifier."""
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    index: dict[str, list[str]] = defaultdict(list)
    for entry in raw.get("anomalies", []) or []:
        anomaly_id = str(entry.get("anomaly_id", ""))
        evidence = entry.get("evidence") or {}
        text = evidence.get("source_text") if isinstance(evidence, dict) else None
        if anomaly_id and isinstance(text, str):
            index[text.strip()].append(anomaly_id)
    return dict(index)
