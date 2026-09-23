"""Pure chapter assembly, section planning, diff measurement, and mechanical governance."""

import hashlib
import json
from dataclasses import dataclass
from difflib import SequenceMatcher

from book_translator.models import (
    LiteraryDiffClass,
    LiteraryEditorResponse,
    LiteraryEditReason,
    LiteraryTerminologyDifference,
    TerminologyRegistry,
    TerminologyValidationReport,
    TranslationAlignmentReport,
    TranslationQAReport,
)
from book_translator.paragraph_alignment import MARKED, assess_alignment, build_alignment_report
from book_translator.paragraph_alignment.policy import policy_for
from book_translator.paragraph_markers import marker_for, parse_marked_document
from book_translator.terminology import validation as qa_validation
from book_translator.terminology.enforcement import resolve_enforcement
from book_translator.terminology.lexical import (
    count_canonical_realizations,
    count_morphological_realizations,
)
from book_translator.terminology.registry import (
    applicable_decisions,
    authorized_forms_payload,
    canonically_matched_decisions,
)

from .policy import DEFAULT_EDIT_POLICY, LiteraryEditPolicy, classify_diff


class LiteraryEditError(ValueError):
    """Raised when a chapter cannot be edited without violating its frozen inputs."""


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChapterParagraph:
    paragraph_id: str
    paragraph_index: int
    chunk_id: str
    chunk_paragraph_index: int
    source_text: str
    faithful_text: str

    @property
    def chunk_paragraph_id(self) -> str:
        return f"P{self.chunk_paragraph_index:04d}"


@dataclass(frozen=True, slots=True)
class ChapterSection:
    section_number: int
    visible_indexes: tuple[int, ...]
    editable_indexes: tuple[int, ...]

    @property
    def label(self) -> str:
        return f"section-{self.section_number:03d}"


@dataclass(frozen=True, slots=True)
class ParagraphDiff:
    character_delta: int
    word_delta: int
    character_change_ratio: float
    diff_class: LiteraryDiffClass


@dataclass(frozen=True, slots=True)
class MechanicalAssessment:
    marker_identity_preserved: bool
    structural_report: TranslationQAReport
    alignment_report: TranslationAlignmentReport
    terminology_report: TerminologyValidationReport | None
    paragraph_refusals: dict[int, tuple[str, ...]]


def canonical_document(blocks: tuple[str, ...] | list[str]) -> str:
    if not blocks or any(not block.strip() for block in blocks):
        raise LiteraryEditError("a literary chapter requires nonempty paragraph blocks")
    return "\n\n".join(blocks) + "\n"


def marked_paragraphs(paragraphs: tuple[ChapterParagraph, ...], *, source: bool) -> str:
    """Render chapter-global identities without renumbering a section's overlap."""
    return (
        "\n\n".join(
            f"{marker_for(item.paragraph_index)}\n"
            f"{item.source_text if source else item.faithful_text}"
            for item in paragraphs
        )
        + "\n"
    )


def marked_values(paragraphs: tuple[ChapterParagraph, ...], values: list[str]) -> str:
    if len(paragraphs) != len(values):
        raise LiteraryEditError("marked candidate values do not cover the chapter")
    return (
        "\n\n".join(
            f"{marker_for(item.paragraph_index)}\n{text}"
            for item, text in zip(paragraphs, values, strict=True)
        )
        + "\n"
    )


def _request_characters(paragraphs: tuple[ChapterParagraph, ...]) -> int:
    return sum(len(item.source_text) + len(item.faithful_text) + 24 for item in paragraphs)


def plan_sections(
    paragraphs: tuple[ChapterParagraph, ...],
    *,
    policy: LiteraryEditPolicy = DEFAULT_EDIT_POLICY,
) -> tuple[ChapterSection, ...]:
    """Prefer a whole chapter; otherwise produce overlapping, uniquely owned core sections."""
    if not paragraphs:
        raise LiteraryEditError("cannot section an empty chapter")
    if _request_characters(paragraphs) <= policy.max_request_characters:
        indexes = tuple(range(len(paragraphs)))
        return (ChapterSection(1, indexes, indexes),)

    cores: list[tuple[int, ...]] = []
    current: list[int] = []
    size = 0
    for index, item in enumerate(paragraphs):
        item_size = len(item.source_text) + len(item.faithful_text) + 24
        if current and size + item_size > policy.max_request_characters:
            cores.append(tuple(current))
            current = []
            size = 0
        current.append(index)
        size += item_size
    if current:
        cores.append(tuple(current))
    if any(not core for core in cores):
        raise LiteraryEditError("section planner produced an empty core")
    if len(cores) > 1 and len(cores[-1]) < policy.minimum_section_core_paragraphs:
        previous = list(cores[-2])
        tail = list(cores[-1])
        move = policy.minimum_section_core_paragraphs - len(tail)
        if len(previous) > move:
            tail = previous[-move:] + tail
            previous = previous[:-move]
            cores[-2] = tuple(previous)
            cores[-1] = tuple(tail)

    overlap = policy.section_overlap_paragraphs
    sections: list[ChapterSection] = []
    for number, core in enumerate(cores, 1):
        first = max(0, core[0] - overlap)
        last = min(len(paragraphs), core[-1] + overlap + 1)
        sections.append(ChapterSection(number, tuple(range(first, last)), core))
    owned = [index for section in sections for index in section.editable_indexes]
    if owned != list(range(len(paragraphs))):
        raise LiteraryEditError("section cores do not tile the chapter exactly")
    return tuple(sections)


def merge_editor_responses(
    paragraphs: tuple[ChapterParagraph, ...],
    sections: tuple[ChapterSection, ...],
    responses: tuple[LiteraryEditorResponse, ...],
) -> tuple[list[str], dict[str, tuple[LiteraryEditReason, ...]], frozenset[str]]:
    """Merge only section cores; overlaps are context and can never race for ownership."""
    if len(sections) != len(responses):
        raise LiteraryEditError("each section must have exactly one editor response")
    values = [item.faithful_text for item in paragraphs]
    reasons: dict[str, tuple[LiteraryEditReason, ...]] = {}
    governed: set[str] = set()
    seen: set[int] = set()
    for section, response in zip(sections, responses, strict=True):
        if len(response.paragraphs) != len(section.editable_indexes):
            raise LiteraryEditError("editor response does not cover its section core")
        for index, edited in zip(section.editable_indexes, response.paragraphs, strict=True):
            if index in seen or edited.paragraph_id != paragraphs[index].paragraph_id:
                raise LiteraryEditError("deterministic section merge found an identity conflict")
            seen.add(index)
            values[index] = edited.edited_text
            reasons[edited.paragraph_id] = tuple(edited.reasons)
        for request in response.multi_paragraph_edit_requests:
            governed.update(request.paragraph_ids)
    if seen != set(range(len(paragraphs))):
        raise LiteraryEditError("editor responses do not tile the chapter")
    for index, item in enumerate(paragraphs):
        if item.paragraph_id in governed:
            values[index] = item.faithful_text
    return values, reasons, frozenset(governed)


def measure_diff(
    faithful: str,
    edited: str,
    *,
    policy: LiteraryEditPolicy = DEFAULT_EDIT_POLICY,
) -> ParagraphDiff:
    ratio = 1.0 - SequenceMatcher(None, faithful, edited, autojunk=False).ratio()
    word_delta = len(edited.split()) - len(faithful.split())
    return ParagraphDiff(
        character_delta=len(edited) - len(faithful),
        word_delta=word_delta,
        character_change_ratio=round(ratio, 6),
        diff_class=classify_diff(ratio, word_delta, len(faithful.split()), policy=policy),
    )


def terminology_json(registry: TerminologyRegistry | None, source_text: str) -> str:
    if registry is None:
        return "[]"
    payload = []
    for candidate, decision in applicable_decisions(registry, source_text):
        payload.append(
            {
                "candidate_id": candidate.candidate_id,
                "decision_id": decision.decision_id,
                "classification": candidate.classification.value,
                "source_term": candidate.source_term,
                "source_aliases": candidate.aliases,
                "canonical_target": decision.target_term,
                **authorized_forms_payload(decision.allowed_target_forms),
                "locked": decision.locked,
                "required": decision.required,
                "enforcement": resolve_enforcement(candidate, decision).value,
                "contextual_notes": decision.contextual_notes,
            }
        )
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _canonical_count(
    text: str, forms: tuple[str, ...], enforcement: str, singulars: tuple[str, ...] = ()
) -> int:
    """Count every authorized realization, through the same matcher terminology QA uses.

    Counting the preferred target alone would report a faithful paragraph that correctly used an
    authorized form as having lost its canonical, and an edit that swapped one authorized form for
    another as a terminology change.
    """
    if enforcement == "morphological":
        return count_morphological_realizations(text, forms, target_language="fa")
    return count_canonical_realizations(text, forms, target_language="fa", singulars=singulars)


def terminology_differences(
    source_text: str,
    faithful_text: str,
    edited_text: str,
    registry: TerminologyRegistry | None,
) -> list[LiteraryTerminologyDifference]:
    if registry is None:
        return []
    differences: list[LiteraryTerminologyDifference] = []
    for candidate, decision in canonically_matched_decisions(registry, source_text):
        enforcement = resolve_enforcement(candidate, decision).value
        forms = qa_validation.authorized_realizations(decision)
        singulars = qa_validation.number_realizations(decision)
        before = _canonical_count(faithful_text, forms, enforcement, singulars)
        after = _canonical_count(edited_text, forms, enforcement, singulars)
        if before != after:
            differences.append(
                LiteraryTerminologyDifference(
                    candidate_id=candidate.candidate_id,
                    decision_id=decision.decision_id,
                    enforcement=enforcement,
                    before_count=before,
                    after_count=after,
                )
            )
    return differences


def assess_mechanical_candidate(
    paragraphs: tuple[ChapterParagraph, ...],
    candidate_values: list[str],
    *,
    registry: TerminologyRegistry | None,
) -> MechanicalAssessment:
    """Run marker, structural, alignment, and terminology QA and localize hard refusals."""
    source_text = canonical_document([item.source_text for item in paragraphs])
    faithful_text = canonical_document([item.faithful_text for item in paragraphs])
    candidate_text = canonical_document(candidate_values)
    marked = marked_values(paragraphs, candidate_values)
    marker_result = parse_marked_document(marked, expected_count=len(paragraphs))
    structural = qa_validation.validate_translation_qa(
        source_text, candidate_text, target_language="fa"
    )
    alignment_assessment = assess_alignment(
        source_text,
        candidate_text,
        policy=policy_for("fa"),
        contract_mode=MARKED,
        marker_findings=marker_result.findings,
    )
    alignment = build_alignment_report(
        alignment_assessment, source_text=source_text, translation_text=candidate_text
    )
    terminology = (
        qa_validation.validate_terminology(source_text, candidate_text, registry)
        if registry is not None
        else None
    )
    refusals: dict[int, set[str]] = {}
    for finding in alignment.findings:
        if finding.severity != "error":
            continue
        indexes = (finding.source_paragraph_index, finding.related_paragraph_index)
        for index in indexes:
            if index is not None:
                refusals.setdefault(index, set()).add(finding.code)
    if not structural.valid:
        for index in range(1, len(paragraphs) + 1):
            refusals.setdefault(index, set()).add("structural_qa_failed")
    if registry is not None:
        for index, (paragraph, candidate) in enumerate(
            zip(paragraphs, candidate_values, strict=True), 1
        ):
            if candidate == paragraph.faithful_text:
                continue
            before = qa_validation.validate_terminology(
                paragraph.source_text, paragraph.faithful_text, registry
            )
            after = qa_validation.validate_terminology(paragraph.source_text, candidate, registry)
            before_codes = {item.code for item in before.findings if item.severity == "error"}
            after_codes = {item.code for item in after.findings if item.severity == "error"}
            for code in sorted(after_codes - before_codes):
                refusals.setdefault(index, set()).add(f"terminology_regression:{code}")
    # A whole-chapter terminology failure that was already present in the faithful input is not
    # attributed to the editor. New local errors above are the only automatic fallback trigger.
    _ = faithful_text
    return MechanicalAssessment(
        marker_identity_preserved=marker_result.valid,
        structural_report=structural,
        alignment_report=alignment,
        terminology_report=terminology,
        paragraph_refusals={key: tuple(sorted(value)) for key, value in refusals.items()},
    )
