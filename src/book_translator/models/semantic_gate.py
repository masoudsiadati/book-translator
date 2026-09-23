"""Persisted contracts for the production semantic gate and bounded semantic revision.

The gate adds a fourth acceptance dimension beside structural, terminology, and alignment
acceptance, and it is recorded as its own state rather than folded into ``acceptance_status``.
Mechanical acceptance says a translation is well-formed; semantic acceptance says it means what
the source means, and a job can hold the first without the second. Nothing here infers one from
the other, and the absence of a semantic state means *not judged*, never *passed*.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.paragraph_alignment import TranslationAlignmentReport
from book_translator.models.semantic_qa import (
    MAX_EVIDENCE_CHARACTERS,
    MAX_EXPLANATION_CHARACTERS,
    SemanticFinding,
    SemanticFindingCode,
    SemanticJudgeRequestRecord,
    SemanticParagraphOutcome,
    SemanticSeverity,
    SemanticVerdict,
)
from book_translator.models.terminology import TerminologyValidationReport, TranslationQAReport


class SemanticGateStatus(StrEnum):
    """The governed semantic state of one job. Absent means not judged."""

    PASS = "pass"
    REVISE = "revise"
    INCONCLUSIVE = "inconclusive"
    #: A bounded terminal state: the budget or the second look is spent and the paragraph is
    #: still not proven faithful. It is never silently accepted and never silently escalated.
    UNRESOLVED = "unresolved"


class SemanticRevisionKind(StrEnum):
    """How a revision candidate was produced."""

    #: Whitespace-only re-segmentation of displaced content; no provider, no lexical change.
    DETERMINISTIC_RESEGMENTATION = "deterministic_resegmentation"
    #: A provider revising exactly the named defects inside an explicit window.
    PROVIDER_REVISION = "provider_revision"


class SemanticRevisionWindow(ContractModel):
    """The explicit, bounded paragraph range a revision was allowed to change."""

    first_index: int = Field(ge=1)
    last_index: int = Field(ge=1)
    reason: Literal["single_paragraph", "boundary_displacement"]

    @model_validator(mode="after")
    def window_is_ordered(self) -> "SemanticRevisionWindow":
        if self.last_index < self.first_index:
            raise ValueError("a revision window must not end before it starts")
        if self.reason == "single_paragraph" and self.first_index != self.last_index:
            raise ValueError("a single-paragraph window spans exactly one paragraph")
        return self

    @property
    def indexes(self) -> tuple[int, ...]:
        return tuple(range(self.first_index, self.last_index + 1))


class SemanticRevisionBounds(ContractModel):
    """Deterministic measurements of what a revision changed, recorded so it can be refused.

    Every field is a count or a digest. ``changed_paragraph_indexes`` must be a subset of the
    window; anything else is a hard failure the orchestrator refuses before adoption, and the
    record of the refusal keeps these numbers so the failure is explainable afterwards.
    """

    paragraphs_in_window: int = Field(ge=1)
    changed_paragraph_indexes: list[int] = Field(default_factory=list)
    unchanged_outside_window: bool
    marker_identity_preserved: bool
    character_delta: int
    word_delta: int
    outside_window_sha256_before: str = Field(pattern=r"^[0-9a-f]{64}$")
    outside_window_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminology_blocking_before: list[str] = Field(default_factory=list)
    terminology_blocking_after: list[str] = Field(default_factory=list)
    lexical_content_preserved: bool | None = None

    @model_validator(mode="after")
    def outside_hashes_agree_with_the_flag(self) -> "SemanticRevisionBounds":
        agree = self.outside_window_sha256_before == self.outside_window_sha256_after
        if agree != self.unchanged_outside_window:
            raise ValueError("outside-window digests must agree with the unchanged flag")
        return self


class OperatorDefectStatement(ContractModel):
    """One defect a person identified in an already-accepted paragraph.

    The judge's vocabulary is reused deliberately: the reviser is told what kind of defect to
    correct in exactly the terms it already understands. What differs is who claims it, which is
    why the claim is recorded as the operator's rather than dressed up as a judge verdict.
    """

    code: SemanticFindingCode
    severity: SemanticSeverity
    #: The source fragment the defect is about, bounded exactly as judge evidence is bounded.
    source_evidence: str = Field(min_length=1, max_length=MAX_EVIDENCE_CHARACTERS)
    reason: str = Field(min_length=1, max_length=MAX_EXPLANATION_CHARACTERS)
    decided_by: Literal["user"] = "user"


class RevisedParagraphIdentity(ContractModel):
    """The one paragraph a targeted revision was allowed to touch, bound by hash."""

    paragraph_id: str = Field(pattern=r"^P\d{4}$")
    paragraph_index: int = Field(ge=1)
    source_paragraph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_paragraph_sha256_before: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: Absent when the revision was refused and no new paragraph exists.
    target_paragraph_sha256_after: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )


class SemanticRevisionVerification(ContractModel):
    """The independent judgement of the revised paragraph that adoption rests on."""

    judge_engine_version: str
    judge_prompt_version: str
    judge_schema_version: str
    policy_version: str
    outcome: SemanticParagraphOutcome
    request: SemanticJudgeRequestRecord


class TranslationSemanticRevisionEvidence(ContractModel):
    """Immutable record of one bounded semantic revision attempt, adopted or refused.

    An adopted revision supersedes the job's effective translation the way a structural repair
    does, and it names what it superseded so the supersession is checkable. A refused revision
    is recorded too: it spent budget, and a budget spent on nothing must still be accountable.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    revision_engine_version: str
    revision_prompt_version: str | None = None
    revision_policy_version: str
    displacement_classifier_version: str | None = None
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    job_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    revision_number: int = Field(ge=1)
    kind: SemanticRevisionKind
    provider_contacted: bool
    window: SemanticRevisionWindow
    triggering_findings: list[SemanticFinding] = Field(default_factory=list)
    superseded_translation_file: Path
    superseded_translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: The semantic judgement a gate revision answers. An operator revision answers a person,
    #: not a judgement, and carries neither; both are omitted rather than null there.
    superseded_semantic_file: Path | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    superseded_semantic_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )
    source_file: Path
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_file: Path | None = None
    translation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    terminology_file: Path | None = None
    terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request: SemanticJudgeRequestRecord | None = None
    bounds: SemanticRevisionBounds | None = None
    marker_findings: list[str] = Field(default_factory=list)
    terminology_report: TerminologyValidationReport | None = None
    structural_report: TranslationQAReport | None = None
    alignment_report: TranslationAlignmentReport | None = None
    adopted: bool
    refusals: list[str] = Field(default_factory=list)
    #: Who identified the defect. Omitted for the gate's own revisions, which are judge-driven.
    defect_origin: Literal["judge", "operator"] = Field(
        default="judge", exclude_if=lambda value: value == "judge"
    )
    #: The operator's statement, the paragraph it named, and the snapshot it was judged under.
    operator_defect: OperatorDefectStatement | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    revised_paragraph: RevisedParagraphIdentity | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    snapshot_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    manifest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )
    #: The occurrence exemptions that shaped the terminology this revision was held to.
    occurrence_exemption_ids: list[str] = Field(
        default_factory=list, exclude_if=lambda values: not values
    )
    #: The independent re-judgement of the revised paragraph; an operator revision is adopted
    #: only on PASS, so its absence on an adopted operator revision is a contract violation.
    verification: SemanticRevisionVerification | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    created_at: AwareDatetime

    @model_validator(mode="after")
    def adoption_is_earned(self) -> "TranslationSemanticRevisionEvidence":
        if self.adopted:
            if self.refusals:
                raise ValueError("an adopted revision carries no refusals")
            if self.translation_file is None or self.translation_sha256 is None:
                raise ValueError("an adopted revision must publish its translation")
            if self.bounds is None or not self.bounds.unchanged_outside_window:
                raise ValueError(
                    "an adopted revision must prove it changed nothing outside its window"
                )
            if not self.bounds.marker_identity_preserved:
                raise ValueError("an adopted revision must preserve every marker identity")
            if (
                self.terminology_report is None
                or self.structural_report is None
                or self.alignment_report is None
            ):
                raise ValueError("an adopted revision must carry every mechanical QA report")
        elif not self.refusals:
            raise ValueError("a refused revision must say why")
        if (self.kind is SemanticRevisionKind.PROVIDER_REVISION) != self.provider_contacted:
            raise ValueError("provider contact must match the revision kind")
        if (self.terminology_file is None) != (self.terminology_sha256 is None):
            raise ValueError("terminology file and hash must be present together")
        semantic = (self.superseded_semantic_file, self.superseded_semantic_sha256)
        if (semantic[0] is None) != (semantic[1] is None):
            raise ValueError("superseded semantic evidence needs its file and its hash together")
        operator = self.defect_origin == "operator"
        if operator and semantic[0] is not None:
            raise ValueError("an operator revision supersedes no semantic judgement")
        if not operator and semantic[0] is None:
            raise ValueError("a judge-driven revision names the judgement it answers")
        if operator != (self.operator_defect is not None):
            raise ValueError("an operator revision is the one that carries an operator statement")
        if operator:
            if self.window.reason != "single_paragraph":
                raise ValueError("an operator revision revises exactly one paragraph")
            if self.revised_paragraph is None:
                raise ValueError("an operator revision must name the paragraph it was given")
            if self.revised_paragraph.paragraph_index != self.window.first_index:
                raise ValueError("the named paragraph must be the revision window")
            if self.adopted:
                if self.verification is None:
                    raise ValueError("an adopted operator revision must carry its verification")
                if self.verification.outcome.policy_outcome is not SemanticVerdict.PASS:
                    raise ValueError("an adopted operator revision requires a PASS verification")
                if self.verification.outcome.paragraph_index != self.window.first_index:
                    raise ValueError("the verification must judge the revised paragraph")
                if self.revised_paragraph.target_paragraph_sha256_after is None:
                    raise ValueError("an adopted revision records the paragraph it produced")
        elif self.verification is not None or self.revised_paragraph is not None:
            raise ValueError("judge-driven revisions carry no operator paragraph record")
        return self


class SemanticGateJobRecord(ContractModel):
    """What the gate did to one job, in counts."""

    job_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    initial_status: SemanticGateStatus | None = None
    final_status: SemanticGateStatus | None = None
    judge_calls: int = Field(ge=0)
    second_looks: int = Field(ge=0)
    revisions_attempted: int = Field(ge=0)
    revisions_adopted: int = Field(ge=0)
    resegmentations: int = Field(ge=0)
    paragraphs_judged: int = Field(ge=0)
    unresolved_paragraphs: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)


class SemanticGateReport(ContractModel):
    """One invocation of the production semantic gate over a run."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    gate_engine_version: str
    gate_policy_version: str
    judge_prompt_version: str
    judge_schema_version: str
    judge_policy_version: str
    second_look_policy_version: str
    revision_prompt_version: str
    revision_policy_version: str
    displacement_classifier_version: str
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    provider: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    models_returned: list[str] = Field(default_factory=list)
    dry_run: bool
    jobs_selected: int = Field(ge=0)
    jobs_processed: int = Field(ge=0)
    judge_calls: int = Field(ge=0)
    reviser_calls: int = Field(ge=0)
    second_looks: int = Field(ge=0)
    resegmentations: int = Field(ge=0)
    revisions_adopted: int = Field(ge=0)
    revisions_refused: int = Field(ge=0)
    status_counts: dict[str, int] = Field(default_factory=dict)
    jobs: list[SemanticGateJobRecord] = Field(default_factory=list)
    created_at: AwareDatetime
