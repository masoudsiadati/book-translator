"""Deterministic, provider-free full-book source-quality audit."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.document_chunking.paragraphs import parse_document, render_paragraph_texts
from book_translator.hashing import sha256_file
from book_translator.models import SCHEMA_VERSION, ChapterMetadata, ChunkMetadata
from book_translator.models.source_anomaly import SourceAnomalyCategory, SourceAnomalyRegistry
from book_translator.models.source_audit import (
    AuditCategory,
    AuditConfidence,
    AuditResolutionStatus,
    AuditSeverity,
    AuditSourceLayer,
    SourceAuditFinding,
    SourceAuditIdentity,
    SourceAuditReport,
)
from book_translator.normalization.markers import parse_source
from book_translator.paths import ProjectPaths, chapter_id_from_selector, is_narrative_chapter_id
from book_translator.source_anomalies import load_source_anomalies

AUDIT_ENGINE_VERSION = "1.4.0"
DETECTION_ENGINE_VERSION = "1.0.0"
INTERRUPTED_TOKEN_DETECTOR_VERSION = "1.1.0"
JOINED_TOKEN_DETECTOR_VERSION = "1.1.0"
_CAMEL_JOIN = re.compile(r"(?<![A-Z])[a-z]{2,}[A-Z][A-Za-z]{1,}")
_ARTICLE_JOIN = re.compile(r"(?:A|An|The)[A-Z][a-z]{2,}")
_MODAL_JOIN = re.compile(r"(could|would|should)([a-z]{5,})")
_TOKEN_EDGE_PUNCTUATION = '"“”‘’«»()[]{}.,!?;:'
_TERMINAL = re.compile(r"[.!?](?:[\"”’»)]*)$")
_HEADING = re.compile(r"^(?:#{1,6}\s+|chapter\b|prologue\b|epilogue\b)", re.I)
_SCENE = frozenset({"***", "* * *", "# # #", "— — —"})
_OPEN_QUOTES = {'"': '"', "“": "”", "‘": "’", "«": "»"}
_SHORT_COMPLETE_WORDS = frozenset(
    {
        "a",
        "an",
        "as",
        "at",
        "be",
        "by",
        "do",
        "go",
        "he",
        "her",
        "him",
        "i",
        "if",
        "in",
        "is",
        "it",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "out",
        "say",
        "she",
        "so",
        "the",
        "to",
        "up",
        "us",
        "was",
        "we",
        "who",
        "why",
        "yes",
        "you",
    }
)


class SourceAuditError(ValueError):
    """Raised when canonical inputs cannot be audited safely."""


@dataclass(frozen=True, slots=True)
class AuditSelection:
    report: SourceAuditReport
    project_root: Path
    book: str | None = None


@dataclass(frozen=True, slots=True)
class FindingInspection:
    finding: SourceAuditFinding
    source_left: str | None
    source_right: str | None
    normalized_representation: str
    chunk_representation: str


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(items: list[str]) -> str:
    return hashlib.sha256(_canonical(items)).hexdigest()


def _finding(
    *,
    chapter_id: str,
    category: AuditCategory,
    severity: AuditSeverity,
    confidence: AuditConfidence,
    layer: AuditSourceLayer,
    message: str,
    action: str,
    chunk_id: str | None = None,
    page_from: int | None = None,
    page_to: int | None = None,
    evidence: dict[str, str | int | bool | None] | None = None,
    detector_version: str = DETECTION_ENGINE_VERSION,
) -> SourceAuditFinding:
    evidence = evidence or {}
    semantic = {
        "engine": detector_version,
        "chapter": chapter_id,
        "chunk": chunk_id,
        "page_from": page_from,
        "page_to": page_to,
        "category": category.value,
        "severity": severity.value,
        "confidence": confidence.value,
        "layer": layer.value,
        "evidence": evidence,
    }
    finding_id = "sqf-" + hashlib.sha256(_canonical(semantic)).hexdigest()[:24]
    return SourceAuditFinding(
        finding_id=finding_id,
        chapter_id=chapter_id,
        chunk_id=chunk_id,
        page_from=page_from,
        page_to=page_to,
        category=category,
        severity=severity,
        confidence=confidence,
        source_layer=layer,
        resolution_status=AuditResolutionStatus.NOT_APPLICABLE_DOWNSTREAM,
        blocking=False,
        message=message,
        evidence=evidence,
        suggested_action=action,
    )


_UPSTREAM_CATEGORIES = frozenset(
    {
        AuditCategory.PAGE_BOUNDARY_CONTINUATION,
        AuditCategory.SPLIT_TOKEN,
        AuditCategory.DIALOGUE_BOUNDARY,
        AuditCategory.JOINED_TOKEN,
        AuditCategory.DUPLICATION,
    }
)


def _blocking(status: AuditResolutionStatus, finding: SourceAuditFinding) -> bool:
    if finding.category is AuditCategory.CONSERVATION:
        return status is AuditResolutionStatus.UNRESOLVED
    return status is AuditResolutionStatus.UNRESOLVED and (
        finding.confidence is AuditConfidence.HIGH
    )


def _with_resolution(
    finding: SourceAuditFinding, status: AuditResolutionStatus
) -> SourceAuditFinding:
    return finding.model_copy(
        update={"resolution_status": status, "blocking": _blocking(status, finding)}
    )


def _match_registered_anomalies(
    findings: list[SourceAuditFinding], registry: SourceAnomalyRegistry, source_hash: str
) -> list[SourceAuditFinding]:
    matched: list[SourceAuditFinding] = []
    for finding in findings:
        anomaly = next(
            (
                item
                for item in registry.anomalies
                if item.chapter_id == finding.chapter_id
                and item.page_from == finding.page_from
                and item.page_to == (finding.page_to or finding.page_from)
                and item.source_file_sha256 == source_hash
                and (
                    (
                        item.category is SourceAnomalyCategory.TRUNCATED_TOKEN
                        and finding.category is AuditCategory.SPLIT_TOKEN
                        and str(finding.evidence.get("token", "")) in item.original_text
                    )
                    or (
                        item.category is SourceAnomalyCategory.DUPLICATED_TEXT
                        and finding.category is AuditCategory.DUPLICATION
                    )
                    or (
                        item.category
                        in {
                            SourceAnomalyCategory.MISSING_TEXT,
                            SourceAnomalyCategory.GARBLED_TEXT,
                            SourceAnomalyCategory.UNKNOWN,
                        }
                        and finding.category is AuditCategory.CONSERVATION
                    )
                )
            ),
            None,
        )
        if anomaly is None:
            matched.append(finding)
            continue
        matched.append(
            finding.model_copy(
                update={
                    "source_layer": AuditSourceLayer.MANUAL_REVIEW,
                    "resolution_status": AuditResolutionStatus.KNOWN_SOURCE_ANOMALY,
                    "blocking": anomaly.blocking,
                    "anomaly_id": anomaly.anomaly_id,
                    "suggested_action": (
                        "Preserve source bytes and carry the registered anomaly forward."
                    ),
                }
            )
        )
    return matched


def _normalization_join_keys(report: object) -> set[tuple[int, int]]:
    if not isinstance(report, dict) or not isinstance(report.get("changes"), list):
        return set()
    return {
        (change["source_page"], change["block_index"])
        for change in report["changes"]
        if isinstance(change, dict)
        and change.get("change_type") == "page_boundary_continuation_join"
        and isinstance(change.get("source_page"), int)
        and isinstance(change.get("block_index"), int)
    }


def _resolve_upstream_findings(
    findings: list[SourceAuditFinding],
    *,
    cleaned: str | None,
    normalization_valid: bool,
    normalization_report: object = None,
) -> list[SourceAuditFinding]:
    joins = _normalization_join_keys(normalization_report)
    resolved: list[SourceAuditFinding] = []
    for finding in findings:
        if finding.resolution_status is AuditResolutionStatus.KNOWN_SOURCE_ANOMALY:
            resolved.append(finding)
            continue
        if finding.category not in _UPSTREAM_CATEGORIES:
            resolved.append(finding)
            continue
        if cleaned is None or not normalization_valid:
            resolved.append(_with_resolution(finding, AuditResolutionStatus.CANNOT_EVALUATE))
            continue
        right_block = finding.evidence.get("right_block")
        if (
            finding.page_to is not None
            and isinstance(right_block, int)
            and (finding.page_to, right_block) in joins
        ):
            resolved.append(
                _with_resolution(finding, AuditResolutionStatus.RESOLVED_BY_NORMALIZATION)
            )
            continue
        token = finding.evidence.get("token")
        if finding.category is AuditCategory.JOINED_TOKEN and isinstance(token, str):
            status = (
                AuditResolutionStatus.UNRESOLVED
                if token in cleaned
                else AuditResolutionStatus.RESOLVED_BY_NORMALIZATION
            )
            resolved.append(_with_resolution(finding, status))
            continue
        resolved.append(_with_resolution(finding, AuditResolutionStatus.UNRESOLVED))
    return resolved


def _unmatched_quote(text: str) -> bool:
    if text.count('"') % 2:
        return True
    return any(
        text.count(opener) > text.count(closer)
        for opener, closer in _OPEN_QUOTES.items()
        if opener != closer
    )


def _edge(text: str, first: bool) -> str:
    words = text.split()
    return (words[0] if first else words[-1]).strip('"“”‘’«»()[]{}.,!?;:') if words else ""


def _interrupted_speaker_transition(left: str, right: str) -> bool:
    """Recognize interrupted dialogue followed by a structurally new speaker block."""
    stripped_left = left.rstrip()
    stripped_right = right.lstrip()
    return stripped_left.endswith(('—"', "—”", "—’", "—»", '–"', "–”", "–’", "–»")) and (
        stripped_right[:1] in _OPEN_QUOTES
        or re.match(
            r"^[A-Z][^.!?]{0,80}\b(?:said|asked|replied|answered|broke|cut)\b", stripped_right
        )
        is not None
    )


def _truncated_interruption_token(left: str, right: str) -> str | None:
    """Return an uncommon short token cut off immediately before an interruption."""
    if not _interrupted_speaker_transition(left, right):
        return None
    match = re.search(r"\b([a-z]{2,3})\s+[—–][\"”’»]$", left.rstrip())
    if match is None or match.group(1).casefold() in _SHORT_COMPLETE_WORDS:
        return None
    return match.group(1)


_JOINED_TOKEN_MESSAGES = {
    "case_boundary": "Token has a suspicious lowercase-to-uppercase join.",
    "article": "Token joins a capitalized article to a capitalized word.",
    "modal": "Token joins a modal verb to a word attested elsewhere in the chapter.",
}


def _block_tokens(compact: str) -> list[str]:
    return re.findall(r"\b[^\s]+\b", compact)


def _chapter_vocabulary(compact_blocks: list[str]) -> frozenset[str]:
    """Collect every standalone word form the chapter itself attests, case-folded."""
    return frozenset(
        token.strip(_TOKEN_EDGE_PUNCTUATION).casefold()
        for compact in compact_blocks
        for token in _block_tokens(compact)
    )


def _joined_token_kind(bare: str, vocabulary: frozenset[str]) -> str | None:
    """Classify a token as a join defect, or return None for an ordinary word.

    A case boundary or a capitalized article is evidence on its own. A modal verb
    prefix is not: ordinary lexemes such as agent nouns and their inflections begin
    with the same letters, so the remainder must be attested as a standalone word
    in the same chapter before the token counts as two words missing a space.
    """
    if _CAMEL_JOIN.fullmatch(bare):
        return "case_boundary"
    if _ARTICLE_JOIN.fullmatch(bare):
        return "article"
    modal = _MODAL_JOIN.fullmatch(bare)
    if modal is not None and modal.group(2) in vocabulary:
        return "modal"
    return None


def _source_findings(chapter_id: str, source: str) -> tuple[list[SourceAuditFinding], int, int]:
    document, _ = parse_source(source)
    findings: list[SourceAuditFinding] = []
    seen_blocks: dict[str, tuple[int, int]] = {}
    vocabulary = _chapter_vocabulary(
        [" ".join(block.original_text.split()) for block in document.blocks]
    )
    for block in document.blocks:
        compact = " ".join(block.original_text.split())
        if not compact:
            findings.append(
                _finding(
                    chapter_id=chapter_id,
                    category=AuditCategory.CONSERVATION,
                    severity=AuditSeverity.ERROR,
                    confidence=AuditConfidence.HIGH,
                    layer=AuditSourceLayer.EXTRACTION,
                    message="Extraction contains an empty block.",
                    action="Inspect and correct through the extraction workflow.",
                    page_from=block.source_page,
                    evidence={"block": block.block_index},
                )
            )
        block_hash = hashlib.sha256(compact.encode()).hexdigest()
        prior = seen_blocks.get(block_hash)
        if prior and len(compact) >= 80:
            findings.append(
                _finding(
                    chapter_id=chapter_id,
                    category=AuditCategory.DUPLICATION,
                    severity=AuditSeverity.ERROR,
                    confidence=AuditConfidence.HIGH,
                    layer=AuditSourceLayer.EXTRACTION,
                    message="An extraction block is duplicated.",
                    action="Verify the page parser; use an approved extraction migration.",
                    page_from=prior[0],
                    page_to=block.source_page,
                    evidence={
                        "first_block": prior[1],
                        "second_block": block.block_index,
                        "block_sha256": block_hash,
                    },
                )
            )
        else:
            seen_blocks[block_hash] = (block.source_page, block.block_index)
        for token_index, token in enumerate(_block_tokens(compact)):
            bare = token.strip(_TOKEN_EDGE_PUNCTUATION)
            join_kind = _joined_token_kind(bare, vocabulary)
            if join_kind is not None:
                findings.append(
                    _finding(
                        chapter_id=chapter_id,
                        category=AuditCategory.JOINED_TOKEN,
                        severity=AuditSeverity.WARNING,
                        confidence=AuditConfidence.HIGH,
                        layer=AuditSourceLayer.EXTRACTION,
                        message=_JOINED_TOKEN_MESSAGES[join_kind],
                        action="Inspect the immutable extraction token and its PDF boundary.",
                        page_from=block.source_page,
                        evidence={
                            "block": block.block_index,
                            "token_index": token_index,
                            "token": bare,
                            "join_kind": join_kind,
                        },
                        detector_version=JOINED_TOKEN_DETECTOR_VERSION,
                    )
                )
    for previous, following in zip(document.blocks, document.blocks[1:], strict=False):
        if previous.source_page == following.source_page:
            continue
        left = previous.original_text.rstrip()
        right = following.original_text.lstrip()
        if not left or not right or left in _SCENE or right in _SCENE or _HEADING.match(right):
            continue
        lower_start = right[0].islower()
        nonterminal = _TERMINAL.search(left) is None and not left.endswith(("—", "–", "...", "…"))
        quote_open = _unmatched_quote(left)
        speaker_transition = _interrupted_speaker_transition(left, right)
        truncated_interruption = _truncated_interruption_token(left, right)
        left_token, right_token = _edge(left, False), _edge(right, True)
        fragment = (len(left_token) == 1 and left_token.casefold() not in {"a", "i"}) or (
            len(right_token) == 1 and right_token.casefold() not in {"a", "i"}
        )
        possessive = right_token.endswith(("'s", "’s")) and right_token[:1].isupper()
        if truncated_interruption is not None:
            findings.append(
                _finding(
                    chapter_id=chapter_id,
                    category=AuditCategory.SPLIT_TOKEN,
                    severity=AuditSeverity.WARNING,
                    confidence=AuditConfidence.HIGH,
                    layer=AuditSourceLayer.EXTRACTION,
                    message=(
                        "An interrupted dialogue edge contains an uncommon short token fragment."
                    ),
                    action="Inspect the immutable extraction edge against the source PDF.",
                    page_from=previous.source_page,
                    page_to=following.source_page,
                    evidence={
                        "left_block": previous.block_index,
                        "right_block": following.block_index,
                        "token": truncated_interruption,
                        "speaker_transition": True,
                    },
                    detector_version=INTERRUPTED_TOKEN_DETECTOR_VERSION,
                )
            )
        if nonterminal and (possessive or fragment) and not speaker_transition:
            confidence = AuditConfidence.HIGH
            findings.append(
                _finding(
                    chapter_id=chapter_id,
                    category=AuditCategory.PAGE_BOUNDARY_CONTINUATION,
                    severity=AuditSeverity.WARNING,
                    confidence=confidence,
                    layer=AuditSourceLayer.NORMALIZATION,
                    message="Canonical page boundary has strong continuation signals.",
                    action="Review normalization here; do not edit extraction in place.",
                    page_from=previous.source_page,
                    page_to=following.source_page,
                    evidence={
                        "left_block": previous.block_index,
                        "right_block": following.block_index,
                        "nonterminal": nonterminal,
                        "lowercase_start": lower_start,
                        "unmatched_dialogue": quote_open,
                        "fragment_edge": fragment,
                        "possessive_continuation": possessive,
                    },
                )
            )
            if fragment:
                findings.append(
                    _finding(
                        chapter_id=chapter_id,
                        category=AuditCategory.SPLIT_TOKEN,
                        severity=AuditSeverity.WARNING,
                        confidence=AuditConfidence.HIGH,
                        layer=AuditSourceLayer.EXTRACTION,
                        message="A page boundary appears to split a token fragment.",
                        action="Inspect the immutable extraction edge against the source PDF.",
                        page_from=previous.source_page,
                        page_to=following.source_page,
                        evidence={
                            "left_block": previous.block_index,
                            "right_block": following.block_index,
                            "left_edge_length": len(left_token),
                            "right_edge_length": len(right_token),
                        },
                    )
                )
        if nonterminal and quote_open and lower_start:
            findings.append(
                _finding(
                    chapter_id=chapter_id,
                    category=AuditCategory.DIALOGUE_BOUNDARY,
                    severity=AuditSeverity.WARNING,
                    confidence=AuditConfidence.MEDIUM,
                    layer=AuditSourceLayer.NORMALIZATION,
                    message="Open dialogue appears to continue across a separated page block.",
                    action="Review the normalization dialogue safeguard for this boundary.",
                    page_from=previous.source_page,
                    page_to=following.source_page,
                    evidence={
                        "left_block": previous.block_index,
                        "right_block": following.block_index,
                    },
                )
            )
    return findings, len(document.page_transitions), len(document.blocks)


def _cleaned_findings(chapter_id: str, cleaned: str) -> list[SourceAuditFinding]:
    paragraphs = parse_document(cleaned)
    findings: list[SourceAuditFinding] = []
    if not paragraphs or not _HEADING.match(paragraphs[0].text.strip()):
        findings.append(
            _finding(
                chapter_id=chapter_id,
                category=AuditCategory.HEADING_STRUCTURE,
                severity=AuditSeverity.WARNING,
                confidence=AuditConfidence.MEDIUM,
                layer=AuditSourceLayer.NORMALIZATION,
                message="Chapter does not begin with a recognizable structural heading.",
                action="Compare chapter metadata and normalized heading structure.",
                evidence={"paragraph": 1},
            )
        )
    heading_keys: set[str] = set()
    for paragraph in paragraphs[:5]:
        text = paragraph.text.strip()
        if _HEADING.match(text):
            key = text.casefold()
            if key in heading_keys:
                findings.append(
                    _finding(
                        chapter_id=chapter_id,
                        category=AuditCategory.HEADING_STRUCTURE,
                        severity=AuditSeverity.WARNING,
                        confidence=AuditConfidence.HIGH,
                        layer=AuditSourceLayer.NORMALIZATION,
                        message="A chapter heading is duplicated.",
                        action="Review normalization heading output.",
                        evidence={
                            "paragraph": paragraph.sequence,
                            "heading_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        },
                    )
                )
            heading_keys.add(key)
    for previous, following in zip(paragraphs, paragraphs[1:], strict=False):
        right = following.text.lstrip()
        left = previous.text.rstrip()
        if (
            right
            and right[0].islower()
            and _TERMINAL.search(left) is None
            and left not in _SCENE
            and right not in _SCENE
            and not _unmatched_quote(left)
            and not left.endswith(("—", "–", "...", "…"))
        ):
            findings.append(
                _finding(
                    chapter_id=chapter_id,
                    category=AuditCategory.PARAGRAPH_BOUNDARY,
                    severity=AuditSeverity.WARNING,
                    confidence=AuditConfidence.MEDIUM,
                    layer=AuditSourceLayer.NORMALIZATION,
                    message="A lowercase paragraph appears to continue a nonterminal predecessor.",
                    action="Review this boundary against canonical page provenance.",
                    evidence={
                        "left_paragraph": previous.sequence,
                        "right_paragraph": following.sequence,
                    },
                )
            )
    return findings


def _chunk_findings(
    chapter_id: str, cleaned: str, chapter_dir: Path, metadata: ChapterMetadata
) -> tuple[list[SourceAuditFinding], int, list[str]]:
    findings: list[SourceAuditFinding] = []
    chunk_texts: list[str] = []
    lineages: list[str] = []
    chunk_dirs = (
        sorted((chapter_dir / "chunks").iterdir()) if (chapter_dir / "chunks").is_dir() else []
    )
    for directory in chunk_dirs:
        if not directory.is_dir() or not directory.name.isdigit():
            continue
        try:
            chunk_meta = ChunkMetadata.model_validate(
                yaml.safe_load((directory / "metadata.yaml").read_text(encoding="utf-8"))
            )
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise SourceAuditError(f"invalid chunk metadata {directory}: {exc}") from exc
        source_path = directory / "source.md"
        text = source_path.read_text(encoding="utf-8")
        actual = sha256_file(source_path)
        lineages.append(actual)
        if actual != chunk_meta.source_sha256:
            findings.append(
                _finding(
                    chapter_id=chapter_id,
                    chunk_id=chunk_meta.chunk_id,
                    category=AuditCategory.CONSERVATION,
                    severity=AuditSeverity.ERROR,
                    confidence=AuditConfidence.MEDIUM,
                    layer=AuditSourceLayer.CHUNKING,
                    message="Chunk source hash does not match metadata.",
                    action="Do not snapshot; diagnose chunk artifact integrity.",
                    evidence={
                        "sequence": chunk_meta.sequence,
                        "expected_sha256": chunk_meta.source_sha256,
                        "actual_sha256": actual,
                    },
                )
            )
        chunk_texts.append(text)
    for index, (left, right) in enumerate(zip(chunk_texts, chunk_texts[1:], strict=False), start=1):
        lp = parse_document(left)[-1].text.rstrip()
        rp = parse_document(right)[0].text.lstrip()
        duplicate = lp == rp and len(lp) >= 40
        continuation = bool(rp and rp[0].islower() and _TERMINAL.search(lp) is None)
        if duplicate or continuation or (_unmatched_quote(lp) and rp[:1].islower()):
            findings.append(
                _finding(
                    chapter_id=chapter_id,
                    chunk_id=f"{chapter_id}-{index + 1:03d}",
                    category=AuditCategory.CHUNK_BOUNDARY,
                    severity=AuditSeverity.ERROR if duplicate else AuditSeverity.WARNING,
                    confidence=AuditConfidence.HIGH
                    if duplicate or continuation
                    else AuditConfidence.MEDIUM,
                    layer=AuditSourceLayer.CHUNKING,
                    message="Adjacent chunks have a suspicious logical boundary.",
                    action="Review planning; use the explicit mutable workflow.",
                    evidence={
                        "left_chunk_sequence": index,
                        "right_chunk_sequence": index + 1,
                        "duplicate_paragraph": duplicate,
                        "lowercase_continuation": continuation,
                        "unmatched_dialogue": _unmatched_quote(lp),
                    },
                )
            )
    reconstructed_paragraphs = tuple(
        paragraph.text for chunk_text in chunk_texts for paragraph in parse_document(chunk_text)
    )
    reconstructed = (
        render_paragraph_texts(reconstructed_paragraphs) if reconstructed_paragraphs else ""
    )
    if reconstructed != cleaned:
        findings.append(
            _finding(
                chapter_id=chapter_id,
                category=AuditCategory.CONSERVATION,
                severity=AuditSeverity.ERROR,
                confidence=AuditConfidence.HIGH,
                layer=AuditSourceLayer.CHUNKING,
                message="Chunk concatenation does not reconstruct canonical cleaned text.",
                action="Do not snapshot; diagnose omitted, duplicated, or reordered chunk text.",
                evidence={
                    "cleaned_sha256": hashlib.sha256(cleaned.encode()).hexdigest(),
                    "reconstructed_sha256": hashlib.sha256(reconstructed.encode()).hexdigest(),
                    "expected_chunks": metadata.chunk_count,
                    "actual_chunks": len(chunk_texts),
                },
            )
        )
    return findings, len(chunk_texts), lineages


def audit_source_quality(
    project_root: Path, *, chapter: str | None = None, book: str | None = None
) -> AuditSelection:
    root = project_root.resolve()
    try:
        chapters_root = ProjectPaths(root, book).chapters
    except ValueError as exc:
        raise SourceAuditError(str(exc)) from exc
    if not chapters_root.is_dir():
        raise SourceAuditError(f"chapter workspace not found: {chapters_root}")
    directories = [
        p for p in sorted(chapters_root.iterdir()) if p.is_dir() and is_narrative_chapter_id(p.name)
    ]
    if chapter:
        try:
            normalized = chapter_id_from_selector(chapter)
        except ValueError:
            normalized = chapter
        directories = [p for p in directories if p.name == normalized]
        if not directories:
            raise SourceAuditError(f"selected chapter workspace not found: {chapter}")
    findings: list[SourceAuditFinding] = []
    pages = blocks = chunks = 0
    extraction_lineage: list[str] = []
    normalization_lineage: list[str] = []
    chunk_lineage: list[str] = []
    normalization_versions: set[str] = set()
    chunk_versions: set[str] = set()
    missing_normalization: set[str] = set()
    missing_chunks: set[str] = set()
    structurally_complete: set[str] = set()
    registry_path = root / "config/source-anomalies.yaml"
    registry = (
        load_source_anomalies(registry_path)
        if registry_path.is_file()
        else SourceAnomalyRegistry(schema_version="1.0", registry_version="1.0.0")
    )
    for directory in directories:
        chapter_finding_start = len(findings)
        try:
            metadata = ChapterMetadata.model_validate(
                yaml.safe_load((directory / "metadata.yaml").read_text(encoding="utf-8"))
            )
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise SourceAuditError(f"invalid chapter metadata {directory}: {exc}") from exc
        source_path, cleaned_path = directory / "source.md", directory / "cleaned.md"
        source = source_path.read_text(encoding="utf-8")
        source_hash = sha256_file(source_path)
        extraction_lineage.append(source_hash)
        normalization_versions.add(metadata.normalization_version)
        chunk_versions.add(metadata.chunk_engine_version)
        if source_hash != metadata.source_sha256:
            findings.append(
                _finding(
                    chapter_id=metadata.chapter_id,
                    category=AuditCategory.CONSERVATION,
                    severity=AuditSeverity.ERROR,
                    confidence=AuditConfidence.HIGH,
                    layer=AuditSourceLayer.MANUAL_REVIEW,
                    message="Immutable source hash does not match canonical metadata.",
                    action="Do not snapshot; diagnose lineage mismatch before any repair.",
                    evidence={"source_match": False},
                )
            )
        source_findings, page_count, block_count = _source_findings(metadata.chapter_id, source)
        source_findings = _match_registered_anomalies(source_findings, registry, source_hash)
        findings.extend(source_findings)
        pages += page_count
        blocks += block_count
        if not cleaned_path.is_file():
            missing_normalization.add(metadata.chapter_id)
            missing_chunks.add(metadata.chapter_id)
            findings[chapter_finding_start:] = _resolve_upstream_findings(
                findings[chapter_finding_start:],
                cleaned=None,
                normalization_valid=False,
            )
            findings.append(
                _with_resolution(
                    _finding(
                        chapter_id=metadata.chapter_id,
                        category=AuditCategory.CONSERVATION,
                        severity=AuditSeverity.ERROR,
                        confidence=AuditConfidence.HIGH,
                        layer=AuditSourceLayer.NORMALIZATION,
                        message="Canonical cleaned chapter artifact is missing.",
                        action="Normalize the chapter before creating a translation snapshot.",
                        evidence={"cleaned_file_present": False},
                    ),
                    AuditResolutionStatus.UNRESOLVED,
                )
            )
            continue
        cleaned = cleaned_path.read_text(encoding="utf-8")
        cleaned_hash = sha256_file(cleaned_path)
        normalization_lineage.append(cleaned_hash)
        normalization_valid = cleaned_hash == metadata.cleaned_sha256
        if not normalization_valid:
            findings.append(
                _with_resolution(
                    _finding(
                        chapter_id=metadata.chapter_id,
                        category=AuditCategory.CONSERVATION,
                        severity=AuditSeverity.ERROR,
                        confidence=AuditConfidence.HIGH,
                        layer=AuditSourceLayer.NORMALIZATION,
                        message="Cleaned artifact hash does not match canonical metadata.",
                        action="Do not snapshot; diagnose normalization lineage mismatch.",
                        evidence={"cleaned_match": False},
                    ),
                    AuditResolutionStatus.UNRESOLVED,
                )
            )
        normalization_report_path = directory / "normalization-report.yaml"
        normalization_report: object = None
        if normalization_report_path.is_file():
            normalization_lineage.append(sha256_file(normalization_report_path))
            normalization_report = yaml.safe_load(
                normalization_report_path.read_text(encoding="utf-8")
            )
            report_valid = isinstance(normalization_report, dict) and (
                normalization_report.get("source_sha256") == source_hash
                and normalization_report.get("cleaned_sha256") == cleaned_hash
            )
            normalization_valid = normalization_valid and report_valid
            if not report_valid:
                findings.append(
                    _with_resolution(
                        _finding(
                            chapter_id=metadata.chapter_id,
                            category=AuditCategory.CONSERVATION,
                            severity=AuditSeverity.ERROR,
                            confidence=AuditConfidence.HIGH,
                            layer=AuditSourceLayer.NORMALIZATION,
                            message="Normalization report lineage mismatches chapter artifacts.",
                            action="Do not snapshot; diagnose the normalization report lineage.",
                            evidence={"normalization_report_matches": False},
                        ),
                        AuditResolutionStatus.UNRESOLVED,
                    )
                )
        else:
            normalization_valid = False
        source_count = len(source_findings)
        findings[chapter_finding_start : chapter_finding_start + source_count] = (
            _resolve_upstream_findings(
                findings[chapter_finding_start : chapter_finding_start + source_count],
                cleaned=cleaned,
                normalization_valid=normalization_valid,
                normalization_report=normalization_report,
            )
        )
        findings.extend(
            _with_resolution(item, AuditResolutionStatus.UNRESOLVED)
            for item in _cleaned_findings(metadata.chapter_id, cleaned)
        )
        chunk_findings, chunk_count, hashes = _chunk_findings(
            metadata.chapter_id, cleaned, directory, metadata
        )
        findings.extend(
            _with_resolution(item, AuditResolutionStatus.UNRESOLVED) for item in chunk_findings
        )
        chunks += chunk_count
        chunk_lineage.extend(hashes)
        chunk_report_path = directory / "chunking-report.yaml"
        chunk_valid = chunk_count > 0
        if chunk_count == 0:
            missing_chunks.add(metadata.chapter_id)
        if chunk_report_path.is_file():
            chunk_lineage.append(sha256_file(chunk_report_path))
            chunk_report = yaml.safe_load(chunk_report_path.read_text(encoding="utf-8"))
            report_valid = isinstance(chunk_report, dict) and (
                chunk_report.get("cleaned_sha256") == cleaned_hash
                and chunk_report.get("chunk_count") == chunk_count
                and chunk_report.get("conservation_validation") is True
            )
            chunk_valid = chunk_valid and report_valid and not chunk_findings
            if not report_valid:
                findings.append(
                    _with_resolution(
                        _finding(
                            chapter_id=metadata.chapter_id,
                            category=AuditCategory.CONSERVATION,
                            severity=AuditSeverity.ERROR,
                            confidence=AuditConfidence.HIGH,
                            layer=AuditSourceLayer.CHUNKING,
                            message="Chunk report lineage or conservation claim is invalid.",
                            action="Do not snapshot; diagnose the chunking report.",
                            evidence={"chunk_report_matches": False},
                        ),
                        AuditResolutionStatus.UNRESOLVED,
                    )
                )
        else:
            chunk_valid = False
            missing_chunks.add(metadata.chapter_id)
        if normalization_valid and chunk_valid:
            structurally_complete.add(metadata.chapter_id)
    findings = [
        _with_resolution(item, AuditResolutionStatus.UNRESOLVED)
        if item.resolution_status is AuditResolutionStatus.NOT_APPLICABLE_DOWNSTREAM
        and item.category is AuditCategory.CONSERVATION
        else item
        for item in findings
    ]
    findings.sort(
        key=lambda f: (
            f.chapter_id,
            f.category.value,
            f.page_from or 0,
            f.chunk_id or "",
            f.finding_id,
        )
    )
    identity = SourceAuditIdentity(
        audit_engine_version=AUDIT_ENGINE_VERSION,
        extraction_lineage_sha256=_digest(extraction_lineage),
        normalization_lineage_sha256=_digest(normalization_lineage),
        chunk_lineage_sha256=_digest(chunk_lineage),
        normalization_versions=sorted(normalization_versions),
        chunk_engine_versions=sorted(chunk_versions),
    )
    semantic = identity.model_dump(mode="json") | {"chapters": [p.name for p in directories]}
    audit_id = "source-quality-" + hashlib.sha256(_canonical(semantic)).hexdigest()[:24]
    report = SourceAuditReport(
        schema_version=SCHEMA_VERSION,
        audit_id=audit_id,
        identity=identity,
        chapters_scanned=len(directories),
        pages_inspected=pages,
        blocks_inspected=blocks,
        chunks_inspected=chunks,
        blocking_findings=sum(item.blocking for item in findings),
        unresolved_high_confidence_findings=sum(
            item.confidence is AuditConfidence.HIGH
            and item.resolution_status is AuditResolutionStatus.UNRESOLVED
            for item in findings
        ),
        resolved_high_confidence_findings=sum(
            item.confidence is AuditConfidence.HIGH
            and item.resolution_status
            in {
                AuditResolutionStatus.RESOLVED_BY_NORMALIZATION,
                AuditResolutionStatus.RESOLVED_BY_CHUNKING,
            }
            for item in findings
        ),
        chapters_missing_normalization=len(missing_normalization),
        chapters_missing_chunks=len(missing_chunks),
        chapters_translation_ready=sum(
            chapter_id in structurally_complete
            and not any(item.chapter_id == chapter_id and item.blocking for item in findings)
            for chapter_id in (directory.name for directory in directories)
        ),
        findings=findings,
    )
    return AuditSelection(report=report, project_root=root, book=book)


def filter_findings(
    report: SourceAuditReport,
    *,
    confidence: AuditConfidence | None = None,
    category: AuditCategory | None = None,
    blocking_only: bool = False,
) -> SourceAuditReport:
    findings = [
        f
        for f in report.findings
        if (confidence is None or f.confidence is confidence)
        and (category is None or f.category is category)
        and (not blocking_only or f.blocking)
    ]
    return report.model_copy(update={"findings": findings})


def _excerpt(text: str, *, tail: bool = False, limit: int = 96) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return ("…" + compact[-limit:]) if tail else (compact[:limit] + "…")


def inspect_finding(
    project_root: Path, finding_id: str, *, book: str | None = None
) -> FindingInspection:
    selection = audit_source_quality(project_root, book=book)
    finding = next(
        (item for item in selection.report.findings if item.finding_id == finding_id), None
    )
    if finding is None:
        raise SourceAuditError(f"source-quality finding not found: {finding_id}")
    chapter = ProjectPaths(selection.project_root, selection.book).chapters / finding.chapter_id
    source = (chapter / "source.md").read_text(encoding="utf-8")
    document, _ = parse_source(source)
    by_index = {block.block_index: block for block in document.blocks}
    left_index = finding.evidence.get("left_block")
    right_index = finding.evidence.get("right_block")
    left = by_index.get(left_index) if isinstance(left_index, int) else None
    right = by_index.get(right_index) if isinstance(right_index, int) else None
    normalized = (
        "current normalization joined the exact source boundary"
        if finding.resolution_status is AuditResolutionStatus.RESOLVED_BY_NORMALIZATION
        else "current normalization preserves or does not prove repair of this structure"
        if finding.resolution_status is AuditResolutionStatus.UNRESOLVED
        else "canonical normalized artifact is unavailable or invalid"
    )
    chunks = sorted((chapter / "chunks").glob("[0-9][0-9][0-9]/source.md"))
    chunk_representation = (
        f"{len(chunks)} current chunk source artifact(s); conservation evaluated by audit"
        if chunks
        else "canonical chunks unavailable"
    )
    return FindingInspection(
        finding=finding,
        source_left=_excerpt(left.original_text, tail=True) if left else None,
        source_right=_excerpt(right.original_text) if right else None,
        normalized_representation=normalized,
        chunk_representation=chunk_representation,
    )


def serialize_report(report: SourceAuditReport, *, as_json: bool = False) -> str:
    data = report.model_dump(mode="json", exclude_none=False)
    if as_json:
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    if not isinstance(rendered, str):
        raise SourceAuditError("could not serialize audit report")
    return rendered


def persist_report(selection: AuditSelection, output: Path | None = None) -> Path:
    report = selection.report
    directory = (
        output
        or ProjectPaths(selection.project_root, selection.book).source_quality_audits
        / report.audit_id
    )
    if directory.exists():
        existing = directory / "report.yaml"
        if existing.is_file() and existing.read_text(encoding="utf-8") == serialize_report(report):
            return directory
        raise SourceAuditError(f"audit output already exists with different content: {directory}")
    directory.mkdir(parents=True)
    atomic_write_text(directory / "report.yaml", serialize_report(report))
    findings = yaml.safe_dump(
        [f.model_dump(mode="json", exclude_none=False) for f in report.findings],
        allow_unicode=True,
        sort_keys=False,
    )
    atomic_write_text(directory / "findings.yaml", findings)
    return directory
