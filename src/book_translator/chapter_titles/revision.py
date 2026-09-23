"""Pure derivation of a provider-free revision of a published chapter-title artifact.

A title artifact is addressed by its *inputs*, so a second run over the same snapshot lands on the
same address and collides with the published answer instead of quietly rivalling it. That is
right for translation and wrong for correction: correcting one heading must not mean paying for
fifty-seven fresh answers, and it must not overwrite the answer of record either.

A revision is therefore its own kind of artifact. It carries every title of its parent unchanged,
replaces exactly the ones a person named, and is addressed by the parent plus those replacements,
so the corrected set gets an address of its own and the parent stays exactly where it was. What a
provider produced is still recorded as the provider's; what a person decided is recorded as the
person's, beside the title it replaced.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from book_translator.chapter_titles.engine import ChapterTitleError
from book_translator.chapter_titles.identity import canonical_identity_bytes, title_content_address
from book_translator.models.chapter_title import (
    ChapterTitleDocument,
    ChapterTitleOrigin,
    ChapterTitleProvenance,
    ChapterTitleProvenanceRecord,
    ChapterTitleRecord,
)

#: The provider-free revision boundary. The translation engine and its prompt are untouched by it,
#: so both keep their own versions and every artifact they published keeps its address.
CHAPTER_TITLE_REVISION_VERSION = "1.0.0"

__all__ = [
    "CHAPTER_TITLE_REVISION_VERSION",
    "RevisedTitles",
    "TitleOverride",
    "revised_titles",
    "revision_identity",
]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TitleOverride:
    """One chapter whose Persian title a person replaced, and why."""

    chapter_id: str
    title_fa: str
    reason: str


def revision_identity(
    *, parent_artifact_id: str, parent_content_sha256: str, overrides: tuple[TitleOverride, ...]
) -> tuple[str, str]:
    """Address a revision by its parent and the exact replacements it makes."""
    payload = {
        "artifact_kind": "chapter-titles-revision",
        "revision_version": CHAPTER_TITLE_REVISION_VERSION,
        "parent_artifact_id": parent_artifact_id,
        "parent_content_sha256": parent_content_sha256,
        "overrides": [
            {"chapter_id": item.chapter_id, "title_fa": item.title_fa}
            for item in sorted(overrides, key=lambda item: item.chapter_id)
        ],
    }
    digest = hashlib.sha256(canonical_identity_bytes(payload)).hexdigest()
    return f"ct-{digest[:24]}", digest


@dataclass(frozen=True, slots=True)
class RevisedTitles:
    """The derived answer: the parent's titles with exactly the named ones replaced."""

    artifact_id: str
    artifact_digest: str
    content_sha256: str
    records: tuple[ChapterTitleRecord, ...]
    provenance: tuple[ChapterTitleProvenanceRecord, ...]
    overrides: tuple[TitleOverride, ...]

    @property
    def inherited_count(self) -> int:
        return sum(1 for record in self.records if record.origin is ChapterTitleOrigin.INHERITED)

    @property
    def override_count(self) -> int:
        """How many titles *this* revision replaced, which a chain's counts do not tell apart."""
        return len(self.overrides)

    @property
    def operator_title_count(self) -> int:
        """Every person-decided title in the resulting set, including ones carried from a chain."""
        return sum(
            1 for record in self.records if record.origin is ChapterTitleOrigin.OPERATOR_OVERRIDE
        )


def _validated(
    document: ChapterTitleDocument, overrides: Mapping[str, TitleOverride]
) -> tuple[TitleOverride, ...]:
    known = {record.chapter_id: record for record in document.chapters}
    if not overrides:
        raise ChapterTitleError("a revision must replace at least one title")
    resolved: list[TitleOverride] = []
    for chapter_id, override in sorted(overrides.items()):
        record = known.get(chapter_id)
        if record is None:
            raise ChapterTitleError(f"chapter {chapter_id} is not in the parent artifact")
        title = override.title_fa
        if title != title.strip() or not title or "\n" in title or "\r" in title:
            raise ChapterTitleError(f"chapter {chapter_id}: a title is one trimmed line")
        if title == record.title_en:
            raise ChapterTitleError(f"chapter {chapter_id}: a title must differ from its source")
        if title == record.title_fa:
            raise ChapterTitleError(
                f"chapter {chapter_id}: the replacement equals the published title"
            )
        if not override.reason.strip():
            raise ChapterTitleError(f"chapter {chapter_id}: an override records why it was made")
        resolved.append(TitleOverride(chapter_id, title, override.reason.strip()))
    return tuple(resolved)


def revised_titles(
    *,
    document: ChapterTitleDocument,
    provenance: ChapterTitleProvenance,
    overrides: Mapping[str, TitleOverride],
) -> RevisedTitles:
    """Derive the revised title set and its history from a parent artifact and the overrides."""
    if document.artifact_id != provenance.artifact_id:
        raise ChapterTitleError("parent document and provenance describe different artifacts")
    applied = _validated(document, overrides)
    by_chapter = {item.chapter_id: item for item in applied}
    history = {record.chapter_id: record for record in provenance.titles}
    records: list[ChapterTitleRecord] = []
    histories: list[ChapterTitleProvenanceRecord] = []
    for record in document.chapters:
        parent_history = history.get(record.chapter_id)
        if parent_history is None:
            raise ChapterTitleError(
                f"the parent artifact has no history for chapter {record.chapter_id}"
            )
        override = by_chapter.get(record.chapter_id)
        if override is None:
            # Carried byte for byte. A title a provider produced becomes an inherited title and
            # keeps saying whose answer it was; a title a person already decided in an earlier
            # revision stays that person's decision, with the title it replaced still named. What
            # changes either way is only which artifact it was carried from.
            carried_origin = (
                ChapterTitleOrigin.OPERATOR_OVERRIDE
                if record.origin is ChapterTitleOrigin.OPERATOR_OVERRIDE
                else ChapterTitleOrigin.INHERITED
            )
            records.append(record.model_copy(update={"origin": carried_origin}))
            histories.append(
                parent_history.model_copy(
                    update={
                        "origin": carried_origin,
                        "parent_artifact_id": document.artifact_id,
                    }
                )
            )
            continue
        records.append(
            record.model_copy(
                update={
                    "title_fa": override.title_fa,
                    "translation_sha256": _sha256(override.title_fa),
                    "origin": ChapterTitleOrigin.OPERATOR_OVERRIDE,
                }
            )
        )
        histories.append(
            parent_history.model_copy(
                update={
                    "title_fa": override.title_fa,
                    "translation_sha256": _sha256(override.title_fa),
                    "origin": ChapterTitleOrigin.OPERATOR_OVERRIDE,
                    "parent_artifact_id": document.artifact_id,
                    "superseded_title_fa": record.title_fa,
                    "superseded_translation_sha256": record.translation_sha256,
                    "override_reason": override.reason,
                    "decided_by": "user",
                    # The person did not call anybody, and did not inherit the call that produced
                    # the title they replaced.
                    "provider": None,
                    "provider_adapter_version": None,
                    "model": None,
                    "provider_response_id": None,
                    "request_sha256": None,
                    "translated_at": None,
                    "deduplicated_from_chapter_id": None,
                }
            )
        )
    artifact_id, digest = revision_identity(
        parent_artifact_id=document.artifact_id,
        parent_content_sha256=document.content_sha256,
        overrides=applied,
    )
    content = title_content_address(
        tuple((item.chapter_id, item.title_en, item.title_fa) for item in records)
    )
    return RevisedTitles(
        artifact_id=artifact_id,
        artifact_digest=digest,
        content_sha256=content,
        records=tuple(records),
        provenance=tuple(
            item.model_copy(update={"chapter_sequence": item.chapter_sequence})
            for item in histories
        ),
        overrides=applied,
    )
