"""Persisted contracts for the provider-backed Semantic Coverage Judge.

Three contracts live here and they answer different questions. ``SemanticJudgeResponse`` is what a
provider must return and is validated strictly before anything else happens with it.
``SemanticQAEvidence`` is the immutable record of one judged unit, hash-linked to the exact source
and translated bytes it judged. ``SemanticCoverageReport`` is the run-level evaluation summary.

None of them carries a prompt, a raw response, or a chain of thought. A finding carries a short
source-local evidence fragment because a later repair stage has to know *what* was omitted, and
that fragment is the only book-derived text any of these records holds; the artifacts are written
owner-only and are ignored by Git for exactly that reason.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel


class SemanticVerdict(StrEnum):
    """What the judge concluded about one paragraph."""

    PASS = "PASS"
    REVISE = "REVISE"
    INCONCLUSIVE = "INCONCLUSIVE"


class SemanticSeverity(StrEnum):
    """How much a defect costs a reader."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SemanticFindingCode(StrEnum):
    """The closed vocabulary of fidelity defects the judge may report.

    It is closed on purpose. An open vocabulary would let a provider invent a category nothing
    downstream can classify, and the acceptance policy would then have to guess how serious an
    unknown code is. A code outside this set fails the response.
    """

    MEANING_OMITTED = "meaning_omitted"
    MEANING_ADDED = "meaning_added"
    FACTUAL_CHANGE = "factual_change"
    ACTION_CHANGED = "action_changed"
    NEGATION_CHANGED = "negation_changed"
    MODALITY_CHANGED = "modality_changed"
    NUMBER_CHANGED = "number_changed"
    ENTITY_RELATION_CHANGED = "entity_relation_changed"
    DIALOGUE_CONTENT_CHANGED = "dialogue_content_changed"
    SPEAKER_ATTRIBUTION_RISK = "speaker_attribution_risk"
    TEMPORAL_RELATION_CHANGED = "temporal_relation_changed"
    CAUSAL_RELATION_CHANGED = "causal_relation_changed"


#: Bounds on judge-authored text. They keep an evidence file a record of *what* was found rather
#: than a second copy of the book, and they make a provider that starts quoting paragraphs fail
#: the contract instead of quietly filling the workspace with prose.
MAX_EVIDENCE_CHARACTERS = 160
MAX_EXPLANATION_CHARACTERS = 300


class SemanticFinding(ContractModel):
    """One fidelity defect the judge claims, with the evidence a repair stage would need."""

    code: SemanticFindingCode
    severity: SemanticSeverity
    source_evidence: str = Field(min_length=1, max_length=MAX_EVIDENCE_CHARACTERS)
    explanation: str = Field(min_length=1, max_length=MAX_EXPLANATION_CHARACTERS)
    confidence: float = Field(ge=0.0, le=1.0)


class SemanticParagraphVerdict(ContractModel):
    """The judge's answer for exactly one paragraph identity."""

    paragraph_id: str = Field(pattern=r"^P\d{4}$")
    verdict: SemanticVerdict
    confidence: float = Field(ge=0.0, le=1.0)
    findings: list[SemanticFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def verdict_matches_its_findings(self) -> "SemanticParagraphVerdict":
        if self.verdict is SemanticVerdict.REVISE and not self.findings:
            raise ValueError("a REVISE verdict must name at least one finding")
        if self.verdict is SemanticVerdict.PASS and self.findings:
            raise ValueError("a PASS verdict must carry no findings")
        return self


class SemanticJudgeResponse(ContractModel):
    """The complete structured answer to one judge request."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    judge_schema_version: str
    verdicts: list[SemanticParagraphVerdict] = Field(min_length=1)

    @model_validator(mode="after")
    def paragraph_identities_are_unique(self) -> "SemanticJudgeResponse":
        identifiers = [item.paragraph_id for item in self.verdicts]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("a judge response may answer each paragraph at most once")
        return self


class SemanticParagraphOutcome(ContractModel):
    """One paragraph's judged verdict after the acceptance policy has read it.

    The judge's verdict and the policy's outcome are recorded separately and never collapsed. A
    judge that reports a defect the policy considers immaterial is not wrong, and a record that
    overwrote its answer would make the policy impossible to recalibrate against real evidence.
    """

    paragraph_id: str = Field(pattern=r"^P\d{4}$")
    paragraph_index: int = Field(ge=1)
    judge_verdict: SemanticVerdict
    policy_outcome: SemanticVerdict
    judge_confidence: float = Field(ge=0.0, le=1.0)
    source_word_count: int = Field(ge=0)
    target_word_count: int = Field(ge=0)
    material_findings: list[SemanticFinding] = Field(default_factory=list)
    advisory_findings: list[SemanticFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def outcome_follows_from_findings(self) -> "SemanticParagraphOutcome":
        if self.policy_outcome is SemanticVerdict.REVISE and not self.material_findings:
            raise ValueError("a REVISE outcome requires at least one material finding")
        if self.policy_outcome is SemanticVerdict.PASS and self.material_findings:
            raise ValueError("a PASS outcome cannot carry a material finding")
        return self


class SemanticJudgeRequestRecord(ContractModel):
    """Everything about one provider call except what was said.

    Message digests stand in for the messages themselves, which is what lets a later reader prove
    that a recorded verdict answers a specific request without the request being retained.
    """

    request_id: str = Field(pattern=r"^sjq-[0-9a-f]{24}$")
    batch_size: int = Field(ge=1)
    repetition: int = Field(ge=1)
    provider: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    model_requested: str | None = None
    model_returned: str | None = None
    system_message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    user_message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    latency_milliseconds: int = Field(ge=0)


class SemanticQAEvidence(ContractModel):
    """Immutable semantic coverage evidence for one chunk under one evaluation.

    It never claims anything about acceptance. A translation the judge asks to revise stays
    exactly as accepted as it was; this record is evidence a later governed stage may act on, and
    on its own it changes nothing.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    judge_engine_version: str
    judge_prompt_version: str
    judge_schema_version: str
    policy_version: str
    provider_contacted: Literal[True] = True
    mutated_acceptance: Literal[False] = False
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    job_id: str = Field(min_length=1)
    chapter_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    evaluation_number: int = Field(ge=1)
    repetition: int = Field(ge=1)
    #: Which look this is and how much context it saw. A first look sees one neighbour on each
    #: side; a second look is the one bounded wider-context re-ask an INCONCLUSIVE paragraph is
    #: granted. Defaults describe every record written before second looks existed.
    look: Literal["first", "second"] = "first"
    context_radius: int = Field(default=1, ge=0)
    context_policy_version: str | None = None
    #: How paragraph identity was established for this chunk, recorded rather than assumed.
    identity_mode: Literal["marked", "legacy_positional"]
    source_file: Path
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation_file: Path
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminology_file: Path | None = None
    terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paragraph_count: int = Field(ge=1)
    judged_paragraphs: int = Field(ge=0)
    pass_count: int = Field(ge=0)
    revise_count: int = Field(ge=0)
    inconclusive_count: int = Field(ge=0)
    requests: list[SemanticJudgeRequestRecord] = Field(default_factory=list)
    outcomes: list[SemanticParagraphOutcome] = Field(default_factory=list)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def counters_match_the_outcomes(self) -> "SemanticQAEvidence":
        if len(self.outcomes) != self.judged_paragraphs:
            raise ValueError("judged paragraph count must equal the recorded outcomes")
        counts = {verdict: 0 for verdict in SemanticVerdict}
        for outcome in self.outcomes:
            counts[outcome.policy_outcome] += 1
        expected = (
            counts[SemanticVerdict.PASS],
            counts[SemanticVerdict.REVISE],
            counts[SemanticVerdict.INCONCLUSIVE],
        )
        if (self.pass_count, self.revise_count, self.inconclusive_count) != expected:
            raise ValueError("recorded verdict counters do not match the outcomes")
        if len({outcome.paragraph_id for outcome in self.outcomes}) != len(self.outcomes):
            raise ValueError("each paragraph may appear at most once in one evidence record")
        if (self.terminology_file is None) != (self.terminology_sha256 is None):
            raise ValueError("terminology file and hash must be present together")
        return self


class SemanticCoverageReport(ContractModel):
    """The run-level summary of one semantic coverage evaluation."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    judge_engine_version: str
    judge_prompt_version: str
    judge_schema_version: str
    policy_version: str
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    snapshot_id: str = Field(min_length=1)
    evaluation_number: int = Field(ge=1)
    request_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    model_requested: str | None = None
    models_returned: list[str] = Field(default_factory=list)
    batch_size: int = Field(ge=1)
    repetitions: int = Field(ge=1)
    mutated_acceptance: Literal[False] = False
    chunks_selected: int = Field(ge=0)
    chunks_judged: int = Field(ge=0)
    chunks_skipped: int = Field(ge=0)
    paragraphs_judged: int = Field(ge=0)
    judge_calls: int = Field(ge=0)
    failed_calls: int = Field(ge=0)
    pass_count: int = Field(ge=0)
    revise_count: int = Field(ge=0)
    inconclusive_count: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    finding_counts: dict[str, int] = Field(default_factory=dict)
    severity_counts: dict[str, int] = Field(default_factory=dict)
    skipped: list[str] = Field(default_factory=list)
    created_at: AwareDatetime
