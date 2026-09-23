"""Contracts for the series terminology policy, reference-corpus lineage, and reference context.

Three kinds of data live here and must not be confused:

- the **series policy** is a governed decision layer (what the current book must or should use);
- the **reference-corpus configuration** pins immutable external evidence by identity;
- a **reference-context package** is retrieved evidence selected for one chunk of the current
  book, carrying provenance but never a decision.

The current book's own occurrence evidence is *not* modelled here: it is the existing
``TerminologyRegistry`` derived from the policy over the book's canonical chunks, so every
downstream consumer keeps reading the contract it already knows.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.terminology import TerminologyType, validate_authorized_target_forms

_SHA256 = r"^[0-9a-f]{64}$"
_SEMVER = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
_SLUG = r"^[a-z0-9][a-z0-9._-]*$"


class SeriesTermStatus(StrEnum):
    """The force a series decision carries for the current book."""

    #: Mandatory except for trivial grammatical adaptation.
    LOCKED = "LOCKED"
    #: Strongly preferred; immediate grammar or meaning may justify another form.
    STRONG_REFERENCE = "STRONG_REFERENCE"
    #: Unresolved historical alternatives; evidence only, never silently canonicalized.
    OBSERVED_VARIANTS = "OBSERVED_VARIANTS"


class SeriesProvenanceKind(StrEnum):
    """Why a term holds its status."""

    EXPLICIT_USER = "explicit_user"
    EXPLICIT_PROJECT = "explicit_project"
    PROMOTED_REFERENCE = "promoted_reference"
    #: Evidence recorded from a previously translated volume of this project; never a decision.
    PRIOR_VOLUME = "prior_volume"
    #: No decision could be justified; the record exists to hold observed variants.
    UNRESOLVED = "unresolved"


class ReferenceEvidenceSource(StrEnum):
    NARRATIVE = "narrative"
    PRINTED_GLOSSARY = "printed_glossary"
    PROJECT_GLOSSARY = "project_glossary"
    USER_DECISION = "user_decision"
    PRIOR_VOLUME_REGISTRY = "prior_volume_registry"


class ReferenceEvidenceQuality(StrEnum):
    CLEAN = "clean"
    OCR_SUSPECT = "ocr_suspect"
    UNCERTAIN = "uncertain"


class ObservedVariantSupport(ContractModel):
    """One independent piece of support for a target form, with its provenance."""

    corpus_id: str = Field(pattern=_SLUG)
    source: ReferenceEvidenceSource
    quality: ReferenceEvidenceQuality
    observation_count: int = Field(ge=0)
    reference_id: str | None = None
    observed_surfaces: dict[str, int] = Field(default_factory=dict)


class ObservedVariant(ContractModel):
    target_form: str = Field(min_length=1)
    support: list[ObservedVariantSupport] = Field(min_length=1)

    @property
    def clean(self) -> bool:
        return any(item.quality is ReferenceEvidenceQuality.CLEAN for item in self.support)

    @property
    def clean_observation_count(self) -> int:
        return sum(
            item.observation_count
            for item in self.support
            if item.quality is ReferenceEvidenceQuality.CLEAN
        )

    @property
    def corpus_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item.corpus_id for item in self.support}))


class SeriesTermProvenance(ContractModel):
    kind: SeriesProvenanceKind
    #: Where the decision came from: a registry decision id, a glossary path, a rule name.
    origin: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_summary: str = ""


class SeriesTermConflictKind(StrEnum):
    CORPUS_DISAGREES_WITH_EXPLICIT_DECISION = "corpus_disagrees_with_explicit_decision"
    PRIOR_VOLUME_DISAGREES = "prior_volume_disagrees"
    EXPLICIT_SOURCES_DISAGREE = "explicit_sources_disagree"


class SeriesTermConflict(ContractModel):
    kind: SeriesTermConflictKind
    detail: str = Field(min_length=1)
    competing_forms: list[str] = Field(default_factory=list)


class SeriesTerm(ContractModel):
    concept_id: str = Field(pattern=r"^sc-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    classification: TerminologyType
    status: SeriesTermStatus
    target_term: str | None = None
    #: Explicitly authorized further realizations of ``target_term``; projected unchanged onto the
    #: derived decision. Only a decided term can authorize one, and an empty list is omitted from
    #: serialization so existing policies keep their bytes and identity.
    allowed_target_forms: list[str] = Field(
        default_factory=list, exclude_if=lambda forms: not forms
    )
    provenance: SeriesTermProvenance
    variants: list[ObservedVariant] = Field(default_factory=list)
    conflicts: list[SeriesTermConflict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def status_and_provenance_agree(self) -> "SeriesTerm":
        if len(set(self.aliases)) != len(self.aliases) or self.source_term in self.aliases:
            raise ValueError("aliases must be unique and must not repeat the source term")
        decided = self.status in {SeriesTermStatus.LOCKED, SeriesTermStatus.STRONG_REFERENCE}
        if decided and not self.target_term:
            raise ValueError(f"a {self.status.value} term requires a target term")
        if not decided and self.target_term is not None:
            raise ValueError("an OBSERVED_VARIANTS term must not carry a target term")
        if not decided and not self.variants:
            raise ValueError("an OBSERVED_VARIANTS term must record at least one variant")
        if not decided and self.allowed_target_forms:
            raise ValueError("an OBSERVED_VARIANTS term must not authorize target forms")
        validate_authorized_target_forms(self.target_term, self.allowed_target_forms)
        kind = self.provenance.kind
        if self.status is SeriesTermStatus.LOCKED and kind not in {
            SeriesProvenanceKind.EXPLICIT_USER,
            SeriesProvenanceKind.EXPLICIT_PROJECT,
        }:
            raise ValueError("only an explicit user or project decision may be LOCKED")
        if self.status is SeriesTermStatus.STRONG_REFERENCE and kind not in {
            SeriesProvenanceKind.PROMOTED_REFERENCE,
            SeriesProvenanceKind.EXPLICIT_USER,
            SeriesProvenanceKind.EXPLICIT_PROJECT,
        }:
            raise ValueError("a STRONG_REFERENCE term requires promoted or explicit provenance")
        if self.status is SeriesTermStatus.OBSERVED_VARIANTS and kind not in {
            SeriesProvenanceKind.UNRESOLVED,
            SeriesProvenanceKind.PRIOR_VOLUME,
        }:
            raise ValueError(
                "an OBSERVED_VARIANTS term carries unresolved or prior-volume provenance"
            )
        forms = [variant.target_form for variant in self.variants]
        if len(set(forms)) != len(forms):
            raise ValueError("observed variants must record each target form once")
        return self


class ImportedPolicySource(ContractModel):
    """A governed input the policy import consumed, pinned by hash for provenance."""

    kind: Literal["registry", "glossary", "reference_corpus", "prior_volume_registry"]
    file: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256)


class SeriesTerminologyPolicy(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    registry_kind: Literal["series_terminology_policy"] = "series_terminology_policy"
    version: str = Field(pattern=_SEMVER)
    series_id: str = Field(pattern=_SLUG)
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    promotion_rule_version: str = Field(min_length=1)
    imported_sources: list[ImportedPolicySource] = Field(default_factory=list)
    terms: list[SeriesTerm] = Field(default_factory=list)

    @model_validator(mode="after")
    def terms_are_unique(self) -> "SeriesTerminologyPolicy":
        ids = [term.concept_id for term in self.terms]
        sources = [term.source_term for term in self.terms]
        if len(set(ids)) != len(ids):
            raise ValueError("series concept IDs must be unique")
        if len(set(sources)) != len(sources):
            raise ValueError("series source terms must be unique")
        return self


class SeriesTermAmendment(ContractModel):
    """One explicit decision a person states about an existing series term.

    Every field is the decision's *complete* intended state rather than a patch: aliases and
    authorized forms listed here replace the term's lists, so what the policy says afterwards is
    exactly what the request says and nothing is left to merge rules.
    """

    source_term: str = Field(min_length=1)
    status: Literal[SeriesTermStatus.LOCKED, SeriesTermStatus.STRONG_REFERENCE] = (
        SeriesTermStatus.LOCKED
    )
    target_term: str = Field(min_length=1)
    allowed_target_forms: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def lists_are_well_formed(self) -> "SeriesTermAmendment":
        if len(set(self.aliases)) != len(self.aliases) or self.source_term in self.aliases:
            raise ValueError("aliases must be unique and must not repeat the source term")
        validate_authorized_target_forms(self.target_term, self.allowed_target_forms)
        return self


class SeriesTermRetirement(ContractModel):
    """One decided term a person withdraws to evidence.

    Retirement is not deletion. The term keeps its concept, its spelling, and every observed
    variant the corpora attested; it simply stops stating a canonical decision, which is the only
    way one concept can end up with two rival renderings. It is the governed answer to a promotion
    that turned out to name something an explicit decision already governs.
    """

    source_term: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class SeriesPolicyAmendmentRequest(ContractModel):
    """An operator's batch of explicit series decisions, applied atomically or not at all."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    request_kind: Literal["series_policy_amendment_request"] = "series_policy_amendment_request"
    series_id: str = Field(pattern=_SLUG)
    #: The policy version the amended policy carries; it must be newer than the current one.
    resulting_policy_version: str = Field(pattern=_SEMVER)
    rationale: str = Field(min_length=1)
    terms: list[SeriesTermAmendment] = Field(min_length=1)
    #: Decided terms withdrawn to evidence. Omitted from serialization when empty so every
    #: amendment recorded before retirement existed keeps its bytes and its identity.
    retired_terms: list[SeriesTermRetirement] = Field(
        default_factory=list, exclude_if=lambda items: not items
    )

    @model_validator(mode="after")
    def terms_are_unique(self) -> "SeriesPolicyAmendmentRequest":
        sources = [term.source_term for term in self.terms]
        if len(set(sources)) != len(sources):
            raise ValueError("an amendment names each source term once")
        retired = [term.source_term for term in self.retired_terms]
        if len(set(retired)) != len(retired):
            raise ValueError("an amendment retires each source term once")
        return self


class SeriesTermChange(ContractModel):
    """What one amendment did to one term, stated as sets so a reader never diffs text."""

    concept_id: str = Field(pattern=r"^sc-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    status_before: SeriesTermStatus
    status_after: SeriesTermStatus
    target_before: str | None = None
    target_after: str
    provenance_before: SeriesProvenanceKind
    provenance_after: SeriesProvenanceKind
    aliases_added: list[str] = Field(default_factory=list)
    aliases_removed: list[str] = Field(default_factory=list)
    forms_added: list[str] = Field(default_factory=list)
    forms_removed: list[str] = Field(default_factory=list)


class SeriesPolicyAmendmentRecord(ContractModel):
    """Immutable evidence that a series policy moved from one exact file identity to another.

    A frozen snapshot pins the policy bytes that were true when it was taken; a later governed
    edit legitimately moves the live policy away from them. This record is what makes that move
    *explained*: a chain of records whose hashes connect two frozen policies is the proof a
    cross-snapshot migration needs before it may waive a changed terminology projection.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    record_kind: Literal["series_policy_amendment"] = "series_policy_amendment"
    amendment_engine_version: str = Field(pattern=_SEMVER)
    amendment_id: str = Field(pattern=r"^spa-[0-9a-f]{24}$")
    series_id: str = Field(pattern=_SLUG)
    policy_file: Path
    policy_sha256_before: str = Field(pattern=_SHA256)
    policy_sha256_after: str = Field(pattern=_SHA256)
    policy_identity_before: str = Field(pattern=_SHA256)
    policy_identity_after: str = Field(pattern=_SHA256)
    policy_version_before: str = Field(pattern=_SEMVER)
    policy_version_after: str = Field(pattern=_SEMVER)
    request: SeriesPolicyAmendmentRequest
    request_sha256: str = Field(pattern=_SHA256)
    changes: list[SeriesTermChange] = Field(min_length=1)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def the_policy_moved(self) -> "SeriesPolicyAmendmentRecord":
        if self.policy_sha256_before == self.policy_sha256_after:
            raise ValueError("an amendment must move the policy to a new identity")
        # Retirements are applied before amendments, so the record reads in that order.
        expected = [term.source_term for term in self.request.retired_terms]
        expected += [term.source_term for term in self.request.terms]
        if [change.source_term for change in self.changes] != expected:
            raise ValueError(
                "an amendment records exactly one change per retired and per requested term"
            )
        return self


class ReferenceRetrievalLimits(ContractModel):
    term_limit: int = Field(default=15, ge=0)
    entity_limit: int = Field(default=15, ge=0)
    tm_limit: int = Field(default=8, ge=0)
    phrase_limit: int = Field(default=6, ge=0)
    style_limit: int = Field(default=3, ge=0)
    voice_limit: int = Field(default=4, ge=0)


class ReferenceCorpusEntry(ContractModel):
    corpus_id: str = Field(pattern=_SLUG)
    book_number: int = Field(ge=1)
    label: str = Field(min_length=1)
    #: May be absolute: the corpus is external, frozen evidence rather than a project artifact.
    path: Path
    sha256: str = Field(pattern=_SHA256)
    corpus_version: str = Field(min_length=1)
    freeze_manifest_file: Path | None = None
    freeze_manifest_sha256: str | None = Field(default=None, pattern=_SHA256)

    @model_validator(mode="after")
    def freeze_manifest_is_complete(self) -> "ReferenceCorpusEntry":
        if (self.freeze_manifest_file is None) != (self.freeze_manifest_sha256 is None):
            raise ValueError("a freeze manifest reference requires both its path and its hash")
        return self


class ReferenceRetrieverReference(ContractModel):
    path: Path
    sha256: str = Field(pattern=_SHA256)
    schema_version: str = Field(min_length=1)


class ReferenceCorpusConfig(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    config_kind: Literal["reference_corpus"] = "reference_corpus"
    series_id: str = Field(pattern=_SLUG)
    retriever: ReferenceRetrieverReference
    corpora: list[ReferenceCorpusEntry] = Field(min_length=1)
    #: Databases deliberately not loaded; recorded so the exclusion is visible and auditable.
    excluded_paths: list[Path] = Field(default_factory=list)
    limits: ReferenceRetrievalLimits = Field(default_factory=ReferenceRetrievalLimits)
    #: Both must stay false for translation context: anomalous material is diagnostic only.
    include_uncertain: Literal[False] = False
    include_reference_only: Literal[False] = False

    @model_validator(mode="after")
    def corpora_are_distinct(self) -> "ReferenceCorpusConfig":
        ids = [entry.corpus_id for entry in self.corpora]
        numbers = [entry.book_number for entry in self.corpora]
        if len(set(ids)) != len(ids) or len(set(numbers)) != len(numbers):
            raise ValueError("reference corpora must have distinct ids and book numbers")
        return self


class ReferenceCorpusIdentity(ContractModel):
    """The verified identity of one external corpus at retrieval time."""

    corpus_id: str = Field(pattern=_SLUG)
    book_number: int = Field(ge=1)
    label: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256)
    corpus_version: str = Field(min_length=1)
    freeze_manifest_sha256: str | None = Field(default=None, pattern=_SHA256)


class ReferenceRetrievalIdentity(ContractModel):
    retriever_path: str = Field(min_length=1)
    retriever_sha256: str = Field(pattern=_SHA256)
    retriever_schema_version: str = Field(min_length=1)
    corpora: list[ReferenceCorpusIdentity] = Field(min_length=1)
    limits: ReferenceRetrievalLimits
    include_uncertain: Literal[False] = False
    include_reference_only: Literal[False] = False


class ReferenceProvenance(ContractModel):
    corpus_id: str = Field(pattern=_SLUG)
    book_label: str = Field(min_length=1)
    chapter: int | None = Field(default=None, ge=0)
    source: ReferenceEvidenceSource
    quality: ReferenceEvidenceQuality
    reference_id: str = Field(min_length=1)


class ReferenceTargetForm(ContractModel):
    target_form: str = Field(min_length=1)
    support: list[ObservedVariantSupport] = Field(min_length=1)


class ReferenceTermEvidence(ContractModel):
    source_term: str = Field(min_length=1)
    kind: Literal["terminology", "entity"]
    categories: list[str] = Field(default_factory=list)
    occurrences_in_passage: int = Field(ge=1)
    cross_book_observation: str = Field(min_length=1)
    forms: list[ReferenceTargetForm] = Field(min_length=1)


class ReferenceSegment(ContractModel):
    """One clean bilingual translation-memory segment: analogy, never text to copy."""

    provenance: ReferenceProvenance
    relevance_score: float = Field(ge=0.0)
    minimum_alignment_confidence: float = Field(ge=0.0, le=1.0)
    source_text: str = Field(min_length=1)
    target_text: str = Field(min_length=1)


class ReferencePhrase(ContractModel):
    source_phrase: str = Field(min_length=1)
    categories: list[str] = Field(default_factory=list)
    forms: list[ReferenceTargetForm] = Field(min_length=1)


class ReferenceVoiceSample(ContractModel):
    character: str = Field(min_length=1)
    provenance: ReferenceProvenance
    attribution: str = ""
    source_dialogue: str = Field(min_length=1)
    target_dialogue: str = Field(min_length=1)


class ReferenceStyleSample(ContractModel):
    provenance: ReferenceProvenance
    tags: list[str] = Field(default_factory=list)
    source_text: str = Field(min_length=1)
    target_text: str = Field(min_length=1)


class ReferenceExclusions(ContractModel):
    """What retrieval saw and refused to carry into translation context."""

    reference_only_tm: int = Field(default=0, ge=0)
    anomalies: int = Field(default=0, ge=0)
    uncertain_forms_withheld: int = Field(default=0, ge=0)
    ambiguous_matches: int = Field(default=0, ge=0)
    ocr_suspect_forms_labelled: int = Field(default=0, ge=0)


class ReferenceContextPackage(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    package_kind: Literal["reference_context"] = "reference_context"
    contract_version: str = Field(min_length=1)
    book_id: str = Field(pattern=_SLUG)
    chapter_id: str = Field(pattern=_SLUG)
    chunk_id: str = Field(pattern=_SLUG)
    source_sha256: str = Field(pattern=_SHA256)
    retrieval: ReferenceRetrievalIdentity
    terminology: list[ReferenceTermEvidence] = Field(default_factory=list)
    entities: list[ReferenceTermEvidence] = Field(default_factory=list)
    translation_memory: list[ReferenceSegment] = Field(default_factory=list)
    phrases: list[ReferencePhrase] = Field(default_factory=list)
    character_voice: list[ReferenceVoiceSample] = Field(default_factory=list)
    style: list[ReferenceStyleSample] = Field(default_factory=list)
    exclusions: ReferenceExclusions = Field(default_factory=ReferenceExclusions)
    warnings: list[str] = Field(default_factory=list)


class ReferenceContextIndexEntry(ContractModel):
    chunk_id: str = Field(pattern=_SLUG)
    chapter_id: str = Field(pattern=_SLUG)
    source_sha256: str = Field(pattern=_SHA256)
    package_file: Path
    package_sha256: str = Field(pattern=_SHA256)


class ReferenceContextIndex(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    index_kind: Literal["reference_context_index"] = "reference_context_index"
    contract_version: str = Field(min_length=1)
    book_id: str = Field(pattern=_SLUG)
    retrieval: ReferenceRetrievalIdentity
    entries: list[ReferenceContextIndexEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def chunks_are_unique(self) -> "ReferenceContextIndex":
        ids = [entry.chunk_id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("reference context index chunk IDs must be unique")
        return self
