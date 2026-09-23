"""Contracts for the durable terminology Open Question Registry.

An open question records a semantic uncertainty that a translation choice cannot settle on its
own — for example whether an English kinship title is maternal or paternal in a language that
marks the distinction.  Questions are deliberately decoupled from the decision they annotate: the
decision keeps the pipeline moving with a provisional target while the question stays open, so an
unresolved meaning never blocks translation unless it is explicitly marked blocking.
"""

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel


class QuestionCategory(StrEnum):
    """A general, term-agnostic classification of what is unresolved."""

    KINSHIP_SIDE = "kinship_side"
    GENDER = "gender"
    IDENTITY = "identity"
    TITLE_MEANING = "title_meaning"
    TRANSLITERATION = "transliteration"
    WORLD_TERM_SEMANTICS = "world_term_semantics"
    PROPER_NOUN_CLASSIFICATION = "proper_noun_classification"
    OTHER = "other"


class QuestionStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


class ResearchState(StrEnum):
    """How far one research channel has been taken for a question.

    ``unavailable`` records that a channel could not be consulted (no network, no other volumes)
    rather than silently leaving it ``not_checked``; readiness must never require it.
    """

    NOT_CHECKED = "not_checked"
    CHECKED_INCONCLUSIVE = "checked_inconclusive"
    CHECKED_RESOLVED = "checked_resolved"
    UNAVAILABLE = "unavailable"


class EvidenceProvenance(StrEnum):
    """Where the evidence behind a question or its resolution came from."""

    CURRENT_BOOK = "current_book"
    WEB = "web"
    OTHER_VOLUMES = "other_volumes"
    MODEL_PRIOR_KNOWLEDGE = "model_prior_knowledge"
    USER = "user"


class EvidenceStatus(StrEnum):
    """Whether the corpus evidence gathered so far can decide the question."""

    NOT_GATHERED = "not_gathered"
    INCONCLUSIVE = "inconclusive"
    SUFFICIENT = "sufficient"
    CONTRADICTORY = "contradictory"


class QuestionResearch(ContractModel):
    """The research ladder for one question: current book, then web, then other volumes."""

    current_book: ResearchState = ResearchState.NOT_CHECKED
    web: ResearchState = ResearchState.NOT_CHECKED
    other_volumes: ResearchState = ResearchState.NOT_CHECKED
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_research(self) -> "QuestionResearch":
        if len(self.notes) != len(set(self.notes)):
            raise ValueError("research notes must be unique")
        return self


class QuestionResolution(ContractModel):
    """The eventual answer, recorded only when a question actually closes."""

    resolved_target: str = Field(min_length=1)
    provenance: EvidenceProvenance
    rationale: str = Field(min_length=1)
    evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resolved_at: AwareDatetime
    supersedes_target: str | None = None


class OpenQuestion(ContractModel):
    """One durable, deterministically identified terminology question."""

    question_id: str = Field(pattern=r"^tq-[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    decision_id: str | None = Field(default=None, pattern=r"^td-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    provisional_target: str | None = None
    category: QuestionCategory
    question: str = Field(min_length=1)
    status: QuestionStatus = QuestionStatus.OPEN
    blocking: bool = False
    evidence_status: EvidenceStatus = EvidenceStatus.NOT_GATHERED
    chapters: list[str] = Field(default_factory=list)
    chunks: list[str] = Field(default_factory=list)
    research: QuestionResearch = Field(default_factory=QuestionResearch)
    provenance: EvidenceProvenance = EvidenceProvenance.CURRENT_BOOK
    evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resolution: QuestionResolution | None = None
    created_at: AwareDatetime
    superseded_by: str | None = Field(default=None, pattern=r"^tq-[0-9a-f]{24}$")

    @model_validator(mode="after")
    def validate_question(self) -> "OpenQuestion":
        if self.status is QuestionStatus.RESOLVED and self.resolution is None:
            raise ValueError("a resolved question must record its resolution")
        if self.status is not QuestionStatus.RESOLVED and self.resolution is not None:
            raise ValueError("only a resolved question may carry a resolution")
        if self.status is QuestionStatus.SUPERSEDED and self.superseded_by is None:
            raise ValueError("a superseded question must name the question that replaced it")
        if self.superseded_by == self.question_id:
            raise ValueError("a question must not supersede itself")
        if len(self.chapters) != len(set(self.chapters)):
            raise ValueError("question chapters must be unique")
        if len(self.chunks) != len(set(self.chunks)):
            raise ValueError("question chunks must be unique")
        return self


class OpenQuestionRegistry(ContractModel):
    """The committed, deterministically ordered set of terminology questions."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    registry_kind: Literal["terminology_questions"] = "terminology_questions"
    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    book_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    questions: list[OpenQuestion] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_registry(self) -> "OpenQuestionRegistry":
        ids = [item.question_id for item in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question IDs must be unique")
        known = set(ids)
        for item in self.questions:
            if item.superseded_by is not None and item.superseded_by not in known:
                raise ValueError("a superseding question must exist in the same registry")
        open_keys = [
            (item.candidate_id, item.category)
            for item in self.questions
            if item.status is QuestionStatus.OPEN
        ]
        if len(open_keys) != len(set(open_keys)):
            raise ValueError("a candidate may hold at most one open question per category")
        return self


class TerminologyDecisionCorrection(ContractModel):
    """Durable evidence that a canonical decision record was corrected in place.

    The registry only ever holds the active decision, so a correction that changes provenance —
    for example an AI choice mistakenly recorded as a locked user preference — would otherwise
    leave no trace.  This record preserves the superseded fields verbatim.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    correction_id: str = Field(pattern=r"^tdc-[0-9a-f]{24}$")
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    decision_id: str = Field(pattern=r"^td-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    previous_target_term: str = Field(min_length=1)
    previous_decision_source: str = Field(min_length=1)
    previous_decision_state: str = Field(min_length=1)
    previous_locked: bool
    corrected_target_term: str = Field(min_length=1)
    corrected_decision_source: str = Field(min_length=1)
    corrected_decision_state: str = Field(min_length=1)
    corrected_locked: bool
    reason: str = Field(min_length=1)
    question_id: str | None = Field(default=None, pattern=r"^tq-[0-9a-f]{24}$")
    registry_sha256_before: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_correction(self) -> "TerminologyDecisionCorrection":
        unchanged = (
            self.previous_target_term == self.corrected_target_term
            and self.previous_decision_source == self.corrected_decision_source
            and self.previous_decision_state == self.corrected_decision_state
            and self.previous_locked == self.corrected_locked
        )
        if unchanged:
            raise ValueError("a correction must change at least one recorded decision fact")
        if self.registry_sha256_before == self.registry_sha256_after:
            raise ValueError("a correction must move the registry to a new identity")
        return self


class TerminologyReadinessReport(ContractModel):
    """Read-only translation readiness split by decision state and question severity."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    questions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed_decisions: int = Field(ge=0)
    provisional_decisions: int = Field(ge=0)
    total_decisions: int = Field(ge=0)
    open_questions: int = Field(ge=0)
    open_blocking_questions: int = Field(ge=0)
    open_non_blocking_questions: int = Field(ge=0)
    resolved_questions: int = Field(ge=0)
    superseded_questions: int = Field(ge=0)
    provisional_without_question: list[str] = Field(default_factory=list)
    ready: bool
    blocking_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_readiness(self) -> "TerminologyReadinessReport":
        if self.confirmed_decisions + self.provisional_decisions != self.total_decisions:
            raise ValueError("confirmed and provisional decisions must sum to the total")
        if self.open_blocking_questions + self.open_non_blocking_questions != self.open_questions:
            raise ValueError("blocking and non-blocking questions must sum to the open total")
        if self.ready is bool(self.blocking_reasons):
            raise ValueError("readiness must agree with the recorded blocking reasons")
        return self


class ImpactedChunk(ContractModel):
    """One chunk whose frozen source actually contains the affected source form."""

    chunk_id: str = Field(min_length=1)
    chapter_id: str = Field(min_length=1)
    occurrence_count: int = Field(ge=1)


class ImpactedSnapshot(ContractModel):
    """One snapshot that froze the terminology registry carrying the affected decision."""

    snapshot_id: str = Field(min_length=1)
    terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    open_questions_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    froze_affected_decision: bool
    chunk_count: int = Field(ge=0)


class ImpactedTranslation(ContractModel):
    """One recorded translation whose acceptance may depend on the affected decision."""

    run_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    requires_reevaluation: bool


class TerminologyImpactReport(ContractModel):
    """Read-only blast radius of changing one terminology decision."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    source_term: str = Field(min_length=1)
    current_target_term: str | None = None
    current_decision_state: str | None = None
    proposed_target_term: str | None = None
    question_id: str | None = Field(default=None, pattern=r"^tq-[0-9a-f]{24}$")
    impacted_chunks: list[ImpactedChunk] = Field(default_factory=list)
    impacted_snapshots: list[ImpactedSnapshot] = Field(default_factory=list)
    impacted_translations: list[ImpactedTranslation] = Field(default_factory=list)
    accepted_requiring_reevaluation: int = Field(ge=0)
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_impact(self) -> "TerminologyImpactReport":
        expected = sum(1 for item in self.impacted_translations if item.requires_reevaluation)
        if expected != self.accepted_requiring_reevaluation:
            raise ValueError("re-evaluation count must match the recorded translations")
        return self
