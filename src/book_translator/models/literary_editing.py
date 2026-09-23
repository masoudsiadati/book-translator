"""Persisted contracts for chapter-level Persian literary editing.

The literary layer consumes a semantically accepted translation and publishes a new immutable
derivative.  It never upgrades fidelity by assertion: changed paragraphs require independent
semantic evidence and an edit that cannot earn that evidence falls back to its byte-exact input.
Provider-authored prose is limited to the edited paragraphs and bounded continuity observations;
there is no field in any contract for chain-of-thought or self-certified fidelity.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.paragraph_alignment import TranslationAlignmentReport
from book_translator.models.semantic_qa import SemanticParagraphOutcome
from book_translator.models.terminology import TerminologyValidationReport, TranslationQAReport


class LiteraryEditReason(StrEnum):
    """Closed, non-explanatory vocabulary for the kind of prose edit made."""

    LITERAL_SYNTAX = "literal_syntax"
    AWKWARD_WORDING = "awkward_wording"
    DIALOGUE_NATURALNESS = "dialogue_naturalness"
    FLOW = "flow"
    REPETITION = "repetition"
    PRONOUN_CLARITY = "pronoun_clarity"
    REGISTER = "register"
    RHYTHM = "rhythm"
    PUNCTUATION = "punctuation"
    NO_CHANGE = "no_change"


class LiteraryDiffClass(StrEnum):
    """Governance class for the breadth of one attempted paragraph edit."""

    NORMAL = "normal_edit"
    BROAD = "broad_edit"
    SUSPICIOUS = "suspicious_rewrite"


class LiteraryCallRole(StrEnum):
    """Provider roles whose cost and provenance must remain separate."""

    EDITOR = "literary_editor"
    SEMANTIC_REJUDGE = "semantic_rejudge"
    CORRECTION = "literary_correction"
    CONTINUITY = "continuity_reviewer"


class LiteraryChapterStatus(StrEnum):
    COMPLETE = "complete"
    COMPLETE_WITH_FALLBACKS = "complete_with_fallbacks"


class LiteraryParagraphFidelity(StrEnum):
    """How the final literary paragraph earned its fidelity status."""

    REUSED = "reused_semantic_evidence"
    FRESH_PASS = "fresh_semantic_pass"
    FALLBACK = "literary_edit_fallback"


class ContinuityFindingCode(StrEnum):
    """Closed vocabulary for a read-only chapter continuity review."""

    UNNATURAL_PERSIAN = "unnatural_persian"
    NARRATOR_REGISTER = "inconsistent_narrator_register"
    CHARACTER_VOICE = "inconsistent_character_voice"
    DIALOGUE_TONE = "dialogue_tone_inconsistency"
    PRONOUN_REFERENCE = "pronoun_reference_ambiguity"
    TRANSLATIONESE = "repeated_translationese"
    TRANSITION = "awkward_paragraph_transition"
    TERMINOLOGY = "terminology_inconsistency"
    STYLE_DISCONTINUITY = "local_stylistic_discontinuity"


class LiteraryEditedParagraph(ContractModel):
    """One editor output paragraph, still subject to independent fidelity verification."""

    paragraph_id: str = Field(pattern=r"^P\d{4}$")
    edited_text: str = Field(min_length=1)
    changed: bool
    reasons: list[LiteraryEditReason] = Field(default_factory=list, max_length=9)
    changed_flag_normalized: bool = False

    @model_validator(mode="after")
    def reasons_match_changed_flag(self) -> "LiteraryEditedParagraph":
        unique = set(self.reasons)
        if len(unique) != len(self.reasons):
            raise ValueError("literary edit reasons must be unique")
        if self.changed and LiteraryEditReason.NO_CHANGE in unique:
            raise ValueError("a changed paragraph cannot carry no_change")
        if not self.changed and unique not in (set(), {LiteraryEditReason.NO_CHANGE}):
            raise ValueError("an unchanged paragraph may carry only no_change")
        return self


class GovernedMultiParagraphEditRequest(ContractModel):
    """An explicit refusal to move content silently across paragraph identities."""

    paragraph_ids: list[str] = Field(min_length=2)
    reason: Literal[
        "cross_paragraph_syntax",
        "dialogue_ownership",
        "reference_continuity",
        "other_governed_restructure",
    ]

    @model_validator(mode="after")
    def identities_are_unique(self) -> "GovernedMultiParagraphEditRequest":
        if len(self.paragraph_ids) != len(set(self.paragraph_ids)):
            raise ValueError("a governed multi-paragraph request must name unique identities")
        return self


class LiteraryEditorResponse(ContractModel):
    """Strict provider response; it deliberately contains no fidelity verdict."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    editor_schema_version: str
    paragraphs: list[LiteraryEditedParagraph] = Field(min_length=1)
    multi_paragraph_edit_requests: list[GovernedMultiParagraphEditRequest] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def identities_are_unique(self) -> "LiteraryEditorResponse":
        identities = [item.paragraph_id for item in self.paragraphs]
        if len(identities) != len(set(identities)):
            raise ValueError("the editor may return each paragraph identity exactly once")
        return self


class LiteraryCorrectionResponse(ContractModel):
    """A bounded retry that may answer only the paragraphs rejected by semantic QA."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    correction_schema_version: str
    paragraphs: list[LiteraryEditedParagraph] = Field(min_length=1)

    @model_validator(mode="after")
    def identities_are_unique_and_changed(self) -> "LiteraryCorrectionResponse":
        identities = [item.paragraph_id for item in self.paragraphs]
        if len(identities) != len(set(identities)):
            raise ValueError("a correction may answer each paragraph identity exactly once")
        if any(not item.changed for item in self.paragraphs):
            raise ValueError("a correction response must return corrected paragraphs")
        return self


class ContinuityFinding(ContractModel):
    code: ContinuityFindingCode
    severity: Literal["advisory", "warning"]
    paragraph_ids: list[str] = Field(min_length=1, max_length=4)
    observation: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def identities_are_unique(self) -> "ContinuityFinding":
        if len(self.paragraph_ids) != len(set(self.paragraph_ids)):
            raise ValueError("a continuity finding must name unique paragraph identities")
        return self


class ContinuityReviewResponse(ContractModel):
    """Read-only output: there is structurally nowhere to return rewritten text."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    continuity_schema_version: str
    findings: list[ContinuityFinding] = Field(default_factory=list, max_length=100)


class PersianLiteraryStyleContract(ContractModel):
    """Generic, versioned Persian prose rules; book-specific voice is not stored here."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    contract_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    target_language: Literal["fa"] = "fa"
    literary_naturalness: list[str] = Field(min_length=1)
    dialogue_register: list[str] = Field(min_length=1)
    punctuation: list[str] = Field(min_length=1)
    quotation_style: list[str] = Field(min_length=1)
    orthography_and_zwnj: list[str] = Field(min_length=1)
    english_syntax: list[str] = Field(min_length=1)
    narrator_consistency: list[str] = Field(min_length=1)
    proper_names: list[str] = Field(min_length=1)
    terminology_flexibility: list[str] = Field(min_length=1)
    inner_monologue: list[str] = Field(min_length=1)
    emphasis: list[str] = Field(min_length=1)
    prohibitions: list[str] = Field(min_length=1)


class ChapterLiteraryGuidance(ContractModel):
    chapter_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    focus: list[str] = Field(default_factory=list, max_length=8)
    negative_controls: list[str] = Field(default_factory=list, max_length=4)


class BookLiteraryProfile(ContractModel):
    """Optional book-scoped style and pilot guidance, frozen independently of generic style."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    profile_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    book_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    prose_profile: list[str] = Field(default_factory=list, max_length=12)
    chapter_guidance: list[ChapterLiteraryGuidance] = Field(default_factory=list)

    @model_validator(mode="after")
    def chapter_guidance_is_unique(self) -> "BookLiteraryProfile":
        identifiers = [item.chapter_id for item in self.chapter_guidance]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("book literary profile chapter guidance must be unique")
        return self


class LiteraryProviderCall(ContractModel):
    """Prose-free, role-separated provider provenance and cost evidence."""

    role: LiteraryCallRole
    request_id: str = Field(pattern=r"^(leq|lcq|crq|sjq)-[0-9a-f]{24}$")
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
    prompt_version: str
    schema_version_used: str
    policy_version: str
    status: Literal["completed", "output_rejected"] = "completed"
    error_code: str | None = None
    reused_from_literary_run_id: str | None = Field(default=None, pattern=r"^lr-[0-9a-f]{24}$")

    @model_validator(mode="after")
    def rejection_has_a_code(self) -> "LiteraryProviderCall":
        if (self.status == "output_rejected") != (self.error_code is not None):
            raise ValueError("only a rejected provider output carries an error code")
        return self


class LiteraryEvidenceReference(ContractModel):
    kind: Literal[
        "existing_semantic_gate",
        "literary_semantic_rejudge",
        "literary_semantic_second_look",
    ]
    file: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LiterarySemanticParagraphEvidence(ContractModel):
    paragraph_id: str = Field(pattern=r"^P\d{4}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: SemanticParagraphOutcome


class LiterarySemanticEvidence(ContractModel):
    """One immutable post-literary judge call, hash-linked at paragraph granularity."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    literary_run_id: str = Field(pattern=r"^lr-[0-9a-f]{24}$")
    input_translation_run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    chapter_id: str = Field(min_length=1)
    evidence_number: int = Field(ge=1)
    correction_round: int = Field(ge=0)
    look: Literal["first", "second", "suspicious"]
    candidate_chapter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    call: LiteraryProviderCall
    paragraphs: list[LiterarySemanticParagraphEvidence] = Field(min_length=1)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def call_and_identities_are_valid(self) -> "LiterarySemanticEvidence":
        if self.call.role is not LiteraryCallRole.SEMANTIC_REJUDGE:
            raise ValueError("literary semantic evidence requires the semantic rejudge role")
        identities = [item.paragraph_id for item in self.paragraphs]
        if len(identities) != len(set(identities)):
            raise ValueError("literary semantic evidence identities must be unique")
        return self


class LiteraryTerminologyDifference(ContractModel):
    candidate_id: str = Field(pattern=r"^tc-[0-9a-f]{24}$")
    decision_id: str = Field(pattern=r"^td-[0-9a-f]{24}$")
    enforcement: str
    before_count: int = Field(ge=0)
    after_count: int = Field(ge=0)


class LiteraryParagraphRecord(ContractModel):
    """Governance and fidelity lineage for one final chapter paragraph."""

    paragraph_id: str = Field(pattern=r"^P\d{4}$")
    paragraph_index: int = Field(ge=1)
    chunk_id: str = Field(min_length=1)
    chunk_paragraph_id: str = Field(pattern=r"^P\d{4}$")
    chunk_paragraph_index: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    faithful_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    final_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed: bool
    attempted_change: bool
    reasons: list[LiteraryEditReason] = Field(default_factory=list)
    diff_class: LiteraryDiffClass | None = None
    character_delta: int = 0
    word_delta: int = 0
    character_change_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    fidelity: LiteraryParagraphFidelity
    correction_rounds: int = Field(default=0, ge=0)
    fallback_reason: str | None = None
    semantic_evidence: list[LiteraryEvidenceReference] = Field(min_length=1)

    @model_validator(mode="after")
    def final_state_is_consistent(self) -> "LiteraryParagraphRecord":
        if not self.changed and self.final_sha256 != self.faithful_sha256:
            raise ValueError("an unchanged literary paragraph must be byte-identical")
        if self.fidelity is LiteraryParagraphFidelity.FALLBACK and (
            self.final_sha256 != self.faithful_sha256 or self.fallback_reason is None
        ):
            raise ValueError("a literary fallback must restore faithful bytes and name why")
        if self.changed and self.fidelity is not LiteraryParagraphFidelity.FRESH_PASS:
            raise ValueError("a changed final paragraph requires a fresh semantic pass")
        if not self.changed and self.fidelity is LiteraryParagraphFidelity.FRESH_PASS:
            raise ValueError("a fresh semantic pass is reserved for changed final paragraphs")
        return self


class LiteraryTextLayerReference(ContractModel):
    layer: Literal["v1_published", "v2_fidelity_only", "v2_literary_edited"]
    files: list[Path] = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LiteraryChapterArtifact(ContractModel):
    """Counts-and-hashes chapter record beside the private final literary text."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    literary_engine_version: str
    editor_prompt_version: str
    editor_schema_version: str
    style_contract_version: str
    edit_policy_version: str
    correction_prompt_version: str
    correction_policy_version: str
    continuity_prompt_version: str
    continuity_schema_version: str
    chapter_id: str = Field(min_length=1)
    status: LiteraryChapterStatus
    paragraph_count: int = Field(ge=1)
    changed_paragraph_count: int = Field(ge=0)
    changed_paragraph_ids: list[str] = Field(default_factory=list)
    attempted_changed_paragraph_count: int = Field(ge=0)
    character_delta: int
    word_delta: int
    changed_percentage: float = Field(ge=0.0, le=100.0)
    broad_edit_count: int = Field(ge=0)
    suspicious_rewrite_count: int = Field(ge=0)
    marker_identity_preserved: bool
    unchanged_paragraph_hashes: dict[str, str] = Field(default_factory=dict)
    terminology_differences: list[LiteraryTerminologyDifference] = Field(default_factory=list)
    edit_category_counts: dict[str, int] = Field(default_factory=dict)
    semantic_regressions_caught: int = Field(ge=0)
    semantic_regressions_corrected: int = Field(ge=0)
    inconclusive_count: int = Field(ge=0)
    fallback_count: int = Field(ge=0)
    paragraphs: list[LiteraryParagraphRecord] = Field(min_length=1)
    structural_report: TranslationQAReport
    alignment_report: TranslationAlignmentReport
    terminology_report: TerminologyValidationReport | None = None
    continuity_findings: list[ContinuityFinding] = Field(default_factory=list)
    provider_calls: list[LiteraryProviderCall] = Field(default_factory=list)
    text_layers: list[LiteraryTextLayerReference] = Field(min_length=2)
    output_file: Path
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reused_from_literary_run_id: str | None = Field(default=None, pattern=r"^lr-[0-9a-f]{24}$")
    reused_chapter_artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def counters_and_layers_are_consistent(self) -> "LiteraryChapterArtifact":
        if len(self.paragraphs) != self.paragraph_count:
            raise ValueError("chapter paragraph records must cover the chapter exactly")
        changed = [item.paragraph_id for item in self.paragraphs if item.changed]
        if changed != self.changed_paragraph_ids or len(changed) != self.changed_paragraph_count:
            raise ValueError("changed paragraph counters must match paragraph records")
        if self.fallback_count != sum(
            item.fidelity is LiteraryParagraphFidelity.FALLBACK for item in self.paragraphs
        ):
            raise ValueError("fallback count must match paragraph records")
        layers = [item.layer for item in self.text_layers]
        if len(layers) != len(set(layers)):
            raise ValueError("chapter text layers must be unique")
        if "v2_fidelity_only" not in layers or "v2_literary_edited" not in layers:
            raise ValueError("chapter evidence requires fidelity-only and literary text layers")
        if (self.reused_from_literary_run_id is None) != (
            self.reused_chapter_artifact_sha256 is None
        ):
            raise ValueError("literary chapter reuse requires both source run and artifact hash")
        return self


class LiteraryRunStatus(StrEnum):
    PROCESSING = "processing"
    COMPLETE = "complete"
    COMPLETE_WITH_FALLBACKS = "complete_with_fallbacks"


class LiteraryRunArtifact(ContractModel):
    """Top-level immutable literary pilot summary and cost accounting."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    literary_run_id: str = Field(pattern=r"^lr-[0-9a-f]{24}$")
    literary_engine_version: str
    editor_prompt_version: str
    editor_schema_version: str
    style_contract_version: str
    edit_policy_version: str
    correction_prompt_version: str
    correction_policy_version: str
    continuity_prompt_version: str
    continuity_schema_version: str
    blind_comparison_contract_version: str
    input_translation_run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    input_snapshot_id: str = Field(min_length=1)
    historical_translation_run_id: str | None = Field(
        default=None, pattern=r"^tr-[0-9a-f]{12}-\d{3,}$"
    )
    historical_release: Path | None = None
    reuse_source_literary_run_id: str | None = Field(default=None, pattern=r"^lr-[0-9a-f]{24}$")
    style_contract_file: Path
    style_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    book_profile_file: Path | None = None
    book_profile_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider: str = Field(min_length=1)
    model_requested: str | None = None
    status: LiteraryRunStatus
    chapter_ids: list[str] = Field(min_length=1)
    chapters: list[LiteraryChapterArtifact] = Field(default_factory=list)
    literary_editor_calls: int = Field(ge=0)
    semantic_rejudge_calls: int = Field(ge=0)
    literary_correction_calls: int = Field(ge=0)
    continuity_reviewer_calls: int = Field(ge=0)
    input_tokens_by_role: dict[str, int] = Field(default_factory=dict)
    output_tokens_by_role: dict[str, int] = Field(default_factory=dict)
    latency_milliseconds_by_role: dict[str, int] = Field(default_factory=dict)
    models_returned_by_role: dict[str, list[str]] = Field(default_factory=dict)
    paragraph_count: int = Field(ge=0)
    changed_paragraph_count: int = Field(ge=0)
    changed_paragraph_percentage: float = Field(ge=0.0, le=100.0)
    semantic_regression_count: int = Field(ge=0)
    semantic_regression_rate: float = Field(ge=0.0, le=1.0)
    fallback_count: int = Field(ge=0)
    fallback_rate: float = Field(ge=0.0, le=1.0)
    created_at: AwareDatetime
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def profile_reference_is_complete(self) -> "LiteraryRunArtifact":
        if (self.book_profile_file is None) != (self.book_profile_sha256 is None):
            raise ValueError("book profile file and hash must be present together")
        if self.status is LiteraryRunStatus.PROCESSING and self.completed_at is not None:
            raise ValueError("a processing literary run cannot have a completion time")
        if self.status is not LiteraryRunStatus.PROCESSING and self.completed_at is None:
            raise ValueError("a completed literary run requires a completion time")
        return self


class BlindComparisonPassage(ContractModel):
    passage_id: str = Field(pattern=r"^passage-[0-9a-f]{12}$")
    chapter_id: str = Field(min_length=1)
    paragraph_ids: list[str] = Field(min_length=1)
    version_a_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    version_b_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BlindComparisonArtifact(ContractModel):
    """Prose-free manifest for the human-facing A/B document."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    contract_version: str
    literary_run_id: str = Field(pattern=r"^lr-[0-9a-f]{24}$")
    labels: Literal["Version A / Version B"] = "Version A / Version B"
    identity_revealed: Literal[False] = False
    rubric: list[
        Literal[
            "روانی",
            "طبیعی بودن فارسی",
            "لحن رمان",
            "دیالوگ",
            "انسجام",
            "وفاداری ادراک‌شده",
            "ترجیح کلی",
        ]
    ]
    passages: list[BlindComparisonPassage] = Field(min_length=1)
    document_file: Path
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    answer_key_file: Path
    answer_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime


class BlindComparisonKeyEntry(ContractModel):
    passage_id: str = Field(pattern=r"^passage-[0-9a-f]{12}$")
    version_a: Literal["v1_published", "v2_literary_edited"]
    version_b: Literal["v1_published", "v2_literary_edited"]

    @model_validator(mode="after")
    def versions_are_distinct(self) -> "BlindComparisonKeyEntry":
        if self.version_a == self.version_b:
            raise ValueError("blind comparison versions must be distinct")
        return self


class BlindComparisonAnswerKey(ContractModel):
    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    contract_version: str
    literary_run_id: str = Field(pattern=r"^lr-[0-9a-f]{24}$")
    entries: list[BlindComparisonKeyEntry] = Field(min_length=1)
