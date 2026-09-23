"""Provider-free chapter-title QA.

Every check here is a pure function of the snapshot's chapters, the produced titles, and the
frozen terminology registry. Nothing is contacted, nothing is written, and no finding carries a
title's text — a finding names a chapter and a code, which is all an operator needs to act.
"""

from book_translator.chapter_titles.engine import (
    MAX_TITLE_CHARACTERS,
    is_target_script,
    latin_word_ratio,
    title_sha256,
)
from book_translator.models import SCHEMA_VERSION, TerminologyRegistry
from book_translator.models.chapter_title import (
    ChapterTitleQAFinding,
    ChapterTitleQAReport,
    ChapterTitleRecord,
)
from book_translator.terminology.registry import canonically_matched_decisions
from book_translator.terminology.validation import validate_terminology

CHAPTER_TITLE_QA_VERSION = "1.1.0"

#: Above this share of Latin words a "Persian" title is mostly untranslated English.
LATIN_WORD_WARNING_RATIO = 0.5


def _finding(
    code: str,
    severity: str,
    message: str,
    *,
    chapter_id: str | None = None,
    candidate: str | None = None,
) -> ChapterTitleQAFinding:
    if severity not in {"warning", "error"}:  # pragma: no cover - guarded by the caller
        raise ValueError("severity must be warning or error")
    return ChapterTitleQAFinding(
        code=code,
        severity="error" if severity == "error" else "warning",
        chapter_id=chapter_id,
        candidate_id=candidate,
        message=message,
    )


def validate_chapter_titles(
    *,
    expected: tuple[tuple[str, str], ...],
    records: tuple[ChapterTitleRecord, ...],
    registry: TerminologyRegistry,
) -> ChapterTitleQAReport:
    """Judge one complete set of chapter titles against its snapshot and frozen terminology.

    ``expected`` is ``(chapter_id, title_en)`` in snapshot order — the chapters a release will ask
    for. A title set that does not cover exactly those chapters is refused, because a release that
    silently falls back to English for one chapter is the failure this stage exists to prevent.
    """
    findings: list[ChapterTitleQAFinding] = []
    by_chapter = {record.chapter_id: record for record in records}
    expected_titles = dict(expected)
    checked_decisions = 0

    for chapter_id, title_en in expected:
        record = by_chapter.get(chapter_id)
        if record is None:
            findings.append(
                _finding(
                    "missing_title",
                    "error",
                    "the snapshot has this chapter and the title artifact does not",
                    chapter_id=chapter_id,
                )
            )
            continue
        if record.title_en != title_en:
            findings.append(
                _finding(
                    "source_title_drift",
                    "error",
                    "the recorded source title differs from the snapshot's",
                    chapter_id=chapter_id,
                )
            )

    for record in records:
        chapter_id = record.chapter_id
        if chapter_id not in expected_titles:
            findings.append(
                _finding(
                    "unknown_chapter",
                    "error",
                    "the title artifact carries a chapter the snapshot never froze",
                    chapter_id=chapter_id,
                )
            )
            continue
        if not record.title_fa.strip():
            findings.append(
                _finding(
                    "empty_translation",
                    "error",
                    "the target-language title is empty",
                    chapter_id=chapter_id,
                )
            )
            continue
        if record.source_sha256 != title_sha256(record.title_en):
            findings.append(
                _finding(
                    "source_hash_mismatch",
                    "error",
                    "the recorded source hash does not address the recorded source title",
                    chapter_id=chapter_id,
                )
            )
        if record.translation_sha256 != title_sha256(record.title_fa):
            findings.append(
                _finding(
                    "translation_hash_mismatch",
                    "error",
                    "the recorded translation hash does not address the recorded title",
                    chapter_id=chapter_id,
                )
            )
        try:
            record.title_fa.encode("utf-8").decode("utf-8")
        except UnicodeError:  # pragma: no cover - a str is always encodable
            findings.append(
                _finding(
                    "invalid_utf8",
                    "error",
                    "the target-language title is not valid UTF-8",
                    chapter_id=chapter_id,
                )
            )
        if record.title_fa == record.title_en:
            findings.append(
                _finding(
                    "untranslated_title",
                    "error",
                    "the target-language title is identical to the source title",
                    chapter_id=chapter_id,
                )
            )
        if not is_target_script(record.title_fa):
            findings.append(
                _finding(
                    "not_target_script",
                    "error",
                    "the target-language title carries no Persian script",
                    chapter_id=chapter_id,
                )
            )
        if len(record.title_fa) > MAX_TITLE_CHARACTERS:
            findings.append(
                _finding(
                    "title_too_long",
                    "error",
                    f"the target-language title exceeds {MAX_TITLE_CHARACTERS} characters",
                    chapter_id=chapter_id,
                )
            )
        if latin_word_ratio(record.title_fa) > LATIN_WORD_WARNING_RATIO:
            findings.append(
                _finding(
                    "high_latin_word_share",
                    "warning",
                    "the target-language title is mostly Latin words",
                    chapter_id=chapter_id,
                )
            )
        # Terminology is judged by the installed validator over the title pair, so a chapter title
        # is held to exactly the canonical decisions the book's prose is held to.
        matched = canonically_matched_decisions(registry, record.title_en)
        checked_decisions += len(matched)
        report = validate_terminology(record.title_en, record.title_fa, registry)
        for finding in report.findings:
            if finding.severity != "error":
                continue
            findings.append(
                _finding(
                    "terminology_not_preserved",
                    "error",
                    f"canonical terminology was not preserved ({finding.code})",
                    chapter_id=chapter_id,
                    candidate=finding.candidate_id,
                )
            )

    # One English title must never receive two different Persian titles.
    by_source: dict[str, tuple[str, str]] = {}
    for record in records:
        previous = by_source.get(record.title_en)
        if previous is None:
            by_source[record.title_en] = (record.chapter_id, record.title_fa)
        elif previous[1] != record.title_fa:
            findings.append(
                _finding(
                    "inconsistent_duplicate_title",
                    "error",
                    f"this English title is rendered differently in chapter {previous[0]}",
                    chapter_id=record.chapter_id,
                )
            )

    ordered = sorted(
        findings, key=lambda item: (item.chapter_id or "", item.code, item.candidate_id or "")
    )
    return ChapterTitleQAReport(
        schema_version=SCHEMA_VERSION,
        chapter_title_qa_version=CHAPTER_TITLE_QA_VERSION,
        valid=not any(item.severity == "error" for item in ordered),
        checked_chapters=len(expected),
        checked_terminology_decisions=checked_decisions,
        findings=ordered,
    )
