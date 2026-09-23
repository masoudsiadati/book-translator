"""Read-only enumeration of terminology and entity evidence from the reference corpora.

The reader never interprets evidence: it returns every stored target form with its origin,
quality flags, and counts exactly as recorded, keyed by the English forms the corpus attests. The
SQL here runs over the connections the external retriever already opened read-only, so no second
access path to the frozen databases exists.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from book_translator.models import (
    ReferenceEvidenceQuality,
    ReferenceEvidenceSource,
    TerminologyType,
)

_GLOSSARY_CONVENTION = (",", ";", "(", ":")


class RowSource(Protocol):
    """The subset of the retriever's per-book database handle the reader depends on."""

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class CorpusForm:
    corpus_id: str
    target_form: str
    source: ReferenceEvidenceSource
    quality: ReferenceEvidenceQuality
    observation_count: int
    reference_id: str
    observed_surfaces: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class CorpusConcept:
    corpus_id: str
    concept_id: str
    kind: str
    label: str
    category: str
    source_forms: tuple[str, ...]
    forms: tuple[CorpusForm, ...]


class CorpusReader(Protocol):
    def concepts(self) -> tuple[CorpusConcept, ...]: ...


#: Corpus categories mapped onto the project's terminology vocabulary. Unknown categories fall
#: back to ``story_term`` so a new corpus category can never silently become a person or place.
_CATEGORY_TYPES: dict[str, TerminologyType] = {
    "person": TerminologyType.PERSON,
    "place": TerminologyType.PLACE,
    "region": TerminologyType.PLACE,
    "place_term": TerminologyType.PLACE,
    "nation": TerminologyType.PEOPLE,
    "people": TerminologyType.PEOPLE,
    "group": TerminologyType.PEOPLE,
    "organization": TerminologyType.ORGANIZATION,
    "organization_type": TerminologyType.ORGANIZATION,
    "organization_or_people": TerminologyType.ORGANIZATION,
    "title": TerminologyType.TITLE,
    "group_title": TerminologyType.TITLE,
    "epithet": TerminologyType.TITLE,
    "artifact": TerminologyType.ARTIFACT,
    "artifact_term": TerminologyType.ARTIFACT,
    "named_object": TerminologyType.ARTIFACT,
    "cultural_object": TerminologyType.ARTIFACT,
    "historical_event": TerminologyType.HISTORICAL_EVENT,
    "historical_era": TerminologyType.HISTORICAL_EVENT,
    "creature": TerminologyType.INVENTED,
    "cosmology": TerminologyType.INVENTED,
    "magic_term": TerminologyType.INVENTED,
    "old_tongue": TerminologyType.INVENTED,
    "prophecy": TerminologyType.INVENTED,
    "legend": TerminologyType.INVENTED,
}


def classification_for(category: str) -> TerminologyType:
    return _CATEGORY_TYPES.get(category, TerminologyType.STORY_TERM)


def _surfaces(raw: Any) -> tuple[tuple[str, int], ...]:
    try:
        decoded = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return ()
    if not isinstance(decoded, dict):
        return ()
    items = sorted(
        ((str(surface), int(count)) for surface, count in decoded.items()),
        key=lambda item: (-item[1], item[0]),
    )
    return tuple(items[:5])


def _quality(*, extraction_status: str | None, ocr_suspect: Any) -> ReferenceEvidenceQuality:
    if extraction_status not in (None, "clean"):
        return ReferenceEvidenceQuality.UNCERTAIN
    if ocr_suspect:
        return ReferenceEvidenceQuality.OCR_SUSPECT
    return ReferenceEvidenceQuality.CLEAN


def natural_source_forms(
    form: str, derive_match_forms: Callable[[str], list[tuple[str, ...]]]
) -> tuple[str, ...]:
    """Return the English forms a stored headword can be matched under, in natural word order.

    A narrative form is already natural and is kept byte-exact. A printed-glossary headword uses
    dictionary conventions ("al'Thor, Rand"), which the retriever's own derivation reverses; those
    derived forms are token joins and therefore lose hyphens, which is acceptable for a headword
    but is why a form without any convention marker is never rewritten.
    """
    if not any(marker in form for marker in _GLOSSARY_CONVENTION):
        return (form.strip(),)
    return tuple(" ".join(tokens) for tokens in derive_match_forms(form) if tokens)


class SqliteCorpusReader:
    def __init__(
        self,
        databases: Mapping[str, RowSource],
        *,
        derive_match_forms: Callable[[str], list[tuple[str, ...]]],
    ) -> None:
        self._databases = dict(databases)
        self._derive = derive_match_forms

    def concepts(self) -> tuple[CorpusConcept, ...]:
        found: list[CorpusConcept] = []
        for corpus_id in sorted(self._databases):
            database = self._databases[corpus_id]
            found.extend(self._term_concepts(corpus_id, database))
            found.extend(self._entity_concepts(corpus_id, database))
        return tuple(found)

    def _term_concepts(self, corpus_id: str, database: RowSource) -> list[CorpusConcept]:
        concepts = []
        for concept in database.rows(
            "SELECT concept_id, label, category FROM term_concepts ORDER BY concept_id"
        ):
            concept_id = str(concept["concept_id"])
            source_forms: list[str] = []
            for row in database.rows(
                "SELECT form FROM term_source_forms WHERE concept_id=? ORDER BY source_form_id",
                (concept_id,),
            ):
                for natural in natural_source_forms(str(row["form"]), self._derive):
                    if natural and natural not in source_forms:
                        source_forms.append(natural)
            for natural in natural_source_forms(str(concept["label"]), self._derive):
                if natural and natural not in source_forms:
                    source_forms.append(natural)
            forms = tuple(
                CorpusForm(
                    corpus_id=corpus_id,
                    target_form=str(row["form"]),
                    source=ReferenceEvidenceSource(str(row["origin"])),
                    quality=_quality(
                        extraction_status=row["extraction_status"], ocr_suspect=row["ocr_suspect"]
                    ),
                    observation_count=int(row["observation_count"]),
                    reference_id=str(row["target_form_id"]),
                    observed_surfaces=_surfaces(row["observed_surfaces"]),
                )
                for row in database.rows(
                    "SELECT target_form_id, form, origin, extraction_status, ocr_suspect, "
                    "observation_count, observed_surfaces FROM term_target_forms "
                    "WHERE concept_id=? ORDER BY target_form_id",
                    (concept_id,),
                )
                if str(row["form"]).strip()
            )
            if source_forms and forms:
                concepts.append(
                    CorpusConcept(
                        corpus_id=corpus_id,
                        concept_id=concept_id,
                        kind="terminology",
                        label=str(concept["label"]),
                        category=str(concept["category"]),
                        source_forms=tuple(source_forms),
                        forms=forms,
                    )
                )
        return concepts

    def _entity_concepts(self, corpus_id: str, database: RowSource) -> list[CorpusConcept]:
        # Every entity *form* is its own concept: a surname, a short name, and a title form each
        # carry their own target evidence in the corpus, and each is matched as its own source term.
        concepts = []
        for row in database.rows(
            "SELECT f.form_id, f.entity_id, f.source_form, f.form_type, e.label, e.entity_type "
            "FROM entity_forms f JOIN entities e USING(entity_id) ORDER BY f.form_id"
        ):
            form_id = str(row["form_id"])
            forms = tuple(
                CorpusForm(
                    corpus_id=corpus_id,
                    target_form=str(target["form"]),
                    source=ReferenceEvidenceSource.NARRATIVE,
                    quality=_quality(extraction_status=None, ocr_suspect=target["ocr_suspect"]),
                    observation_count=int(target["observation_count"]),
                    reference_id=str(target["target_form_id"]),
                    observed_surfaces=_surfaces(target["observed_surfaces"]),
                )
                for target in database.rows(
                    "SELECT target_form_id, form, ocr_suspect, observation_count, "
                    "observed_surfaces FROM entity_target_forms WHERE form_id=? "
                    "ORDER BY target_form_id",
                    (form_id,),
                )
                if str(target["form"]).strip()
            )
            source_form = str(row["source_form"]).strip()
            if source_form and forms:
                concepts.append(
                    CorpusConcept(
                        corpus_id=corpus_id,
                        concept_id=form_id,
                        kind="entity",
                        label=str(row["label"]),
                        category=str(row["entity_type"]),
                        source_forms=(source_form,),
                        forms=forms,
                    )
                )
        return concepts
