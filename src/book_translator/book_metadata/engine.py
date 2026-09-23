"""Pure deterministic resolution of book metadata from local source evidence.

The rules here are generic bibliographic rules. No specific book, series, author, or publisher
is encoded, and a value is left unresolved rather than guessed when evidence is absent.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from book_translator.book_metadata.identity import derive_book_id, is_placeholder
from book_translator.models import BOOK_METADATA_FIELDS, BookMetadataProvenance

_ORDINALS: Mapping[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
}

_ORDINAL_VOLUME = re.compile(
    r"\bthe\s+(?P<ordinal>"
    + "|".join(_ORDINALS)
    + r")\s+(?:book|volume)\s+of\s+(?P<series>.+?)\s*$",
    re.IGNORECASE,
)
_NUMERIC_VOLUME = re.compile(
    r"\b(?:book|volume)\s+(?P<index>\d{1,2})\s+of\s+(?P<series>.+?)\s*$", re.IGNORECASE
)
_ISBN = re.compile(r"\bISBN(?:[-\s]?1[03])?\s*:?\s*(?P<isbn>[\dX][\dX\s-]{8,20})", re.IGNORECASE)
_PUBLISHER = re.compile(
    r"\bpublished\s+(?:in\s+\d{4}\s+)?(?:by|in\s+.+?\s+by)\s+(?P<publisher>.+?)\s*$",
    re.IGNORECASE,
)
_PUBLICATION_YEAR = re.compile(
    r"(?:©|\(c\)|\bcopyright\b)\s*(?:©\s*)?(?P<year>\d{4})", re.IGNORECASE
)
_EDITION = re.compile(
    r"\b(?P<edition>(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"\d+(?:st|nd|rd|th))(?:\s+\w+){0,2}\s+edition)\b",
    re.IGNORECASE,
)

# Bibliographic lines are short; anything longer is prose and is never inspected or persisted.
MAXIMUM_EVIDENCE_LINE_LENGTH = 120


@dataclass(frozen=True, slots=True)
class EvidenceLine:
    """One short candidate line of front or back matter with its 1-based PDF page."""

    page: int
    text: str


@dataclass(frozen=True, slots=True)
class PdfBookEvidence:
    """Local PDF evidence: embedded document information plus bounded matter lines."""

    page_count: int
    title: str | None = None
    author: str | None = None
    subject: str | None = None
    keywords: str | None = None
    matter_lines: tuple[EvidenceLine, ...] = ()


@dataclass(frozen=True, slots=True)
class ConfiguredBookEvidence:
    """Project configuration evidence, the lowest-precedence bibliographic source."""

    book_id: str
    title_en: str
    title_fa: str
    author: str
    source_language: str
    target_language: str
    series_name: str | None = None
    series_index: int | None = None


@dataclass(frozen=True, slots=True)
class OperatorAssertion:
    """An explicit operator-supplied value and the provenance that operator claims for it."""

    value: str
    provenance: BookMetadataProvenance


@dataclass(frozen=True, slots=True)
class ResolvedField:
    value: str | int | None
    provenance: BookMetadataProvenance
    evidence: str | None = None


@dataclass(frozen=True, slots=True)
class BookMetadataResolution:
    fields: Mapping[str, ResolvedField]
    warnings: tuple[str, ...] = ()

    def value(self, name: str) -> str | int | None:
        return self.fields[name].value

    def text(self, name: str) -> str | None:
        value = self.fields[name].value
        return value if isinstance(value, str) else None


def _unresolved(reason: str) -> ResolvedField:
    return ResolvedField(None, BookMetadataProvenance.UNRESOLVED, reason)


def _usable(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or is_placeholder(stripped):
        return None
    return stripped


def _assertion(assertions: Mapping[str, OperatorAssertion], name: str) -> ResolvedField | None:
    claimed = assertions.get(name)
    if claimed is None:
        return None
    return ResolvedField(claimed.value, claimed.provenance, "operator assertion")


def _corroboration(value: str, lines: tuple[EvidenceLine, ...]) -> str | None:
    folded = value.casefold()
    for line in lines:
        if line.text.casefold() == folded:
            return f"pdf:page-{line.page}"
    return None


def _resolve_title(
    pdf: PdfBookEvidence,
    config: ConfiguredBookEvidence,
    assertions: Mapping[str, OperatorAssertion],
) -> ResolvedField:
    asserted = _assertion(assertions, "title_en")
    if asserted is not None:
        return asserted
    title = _usable(pdf.title)
    if title is not None:
        corroborated = _corroboration(title, pdf.matter_lines)
        evidence = "pdf:docinfo.title"
        if corroborated is not None:
            evidence = f"{evidence}+{corroborated}"
        return ResolvedField(title, BookMetadataProvenance.PDF_METADATA, evidence)
    configured = _usable(config.title_en)
    if configured is not None:
        return ResolvedField(
            configured, BookMetadataProvenance.CONFIGURATION, "config/book.yaml:title_en"
        )
    return _unresolved("no PDF or configured English title")


def _resolve_author(
    pdf: PdfBookEvidence,
    config: ConfiguredBookEvidence,
    assertions: Mapping[str, OperatorAssertion],
) -> ResolvedField:
    asserted = _assertion(assertions, "author")
    if asserted is not None:
        return asserted
    author = _usable(pdf.author)
    if author is not None:
        corroborated = _corroboration(author, pdf.matter_lines)
        evidence = "pdf:docinfo.author"
        if corroborated is not None:
            evidence = f"{evidence}+{corroborated}"
        return ResolvedField(author, BookMetadataProvenance.PDF_METADATA, evidence)
    configured = _usable(config.author)
    if configured is not None:
        return ResolvedField(
            configured, BookMetadataProvenance.CONFIGURATION, "config/book.yaml:author"
        )
    return _unresolved("no PDF or configured author")


def _resolve_series(
    pdf: PdfBookEvidence,
    config: ConfiguredBookEvidence,
    assertions: Mapping[str, OperatorAssertion],
) -> tuple[ResolvedField, ResolvedField]:
    asserted_name = _assertion(assertions, "series_name")
    asserted_index = _assertion(assertions, "series_index")
    # A configured series is a recorded decision, so it is consulted before front matter and only
    # an explicit operator assertion outranks it; an absent one leaves the source rules untouched.
    configured_name = _usable(config.series_name)
    if configured_name is not None:
        asserted_name = asserted_name or ResolvedField(
            configured_name, BookMetadataProvenance.CONFIGURATION, "config/book.yaml:series_name"
        )
    if config.series_index is not None:
        asserted_index = asserted_index or ResolvedField(
            config.series_index,
            BookMetadataProvenance.CONFIGURATION,
            "config/book.yaml:series_index",
        )
    if asserted_name is not None and asserted_index is not None:
        return asserted_name, asserted_index
    for line in pdf.matter_lines:
        match = _ORDINAL_VOLUME.search(line.text) or _NUMERIC_VOLUME.search(line.text)
        if match is None:
            continue
        groups = match.groupdict()
        index = (
            _ORDINALS[groups["ordinal"].casefold()]
            if groups.get("ordinal")
            else int(groups["index"])
        )
        series = groups["series"].strip(" .,;:")
        if not series:
            continue
        evidence = f"pdf:page-{line.page}"
        return (
            asserted_name or ResolvedField(series, BookMetadataProvenance.FRONT_MATTER, evidence),
            asserted_index or ResolvedField(index, BookMetadataProvenance.FRONT_MATTER, evidence),
        )
    return (
        asserted_name or _unresolved("no series statement in front or back matter"),
        asserted_index or _unresolved("no volume statement in front or back matter"),
    )


def _normalized_isbn(raw: str) -> str | None:
    digits = re.sub(r"[\s-]", "", raw).upper()
    if len(digits) == 10 and re.fullmatch(r"\d{9}[\dX]", digits):
        total = sum(
            (10 - position) * (10 if character == "X" else int(character))
            for position, character in enumerate(digits)
        )
        return digits if total % 11 == 0 else None
    if len(digits) == 13 and digits.isdigit():
        total = sum(
            int(character) * (1 if position % 2 == 0 else 3)
            for position, character in enumerate(digits)
        )
        return digits if total % 10 == 0 else None
    return None


def _resolve_pattern(
    pdf: PdfBookEvidence,
    assertions: Mapping[str, OperatorAssertion],
    name: str,
    pattern: re.Pattern[str],
    group: str,
    reason: str,
) -> ResolvedField:
    asserted = _assertion(assertions, name)
    if asserted is not None:
        return asserted
    for line in pdf.matter_lines:
        match = pattern.search(line.text)
        if match is None:
            continue
        value: str | int | None = match.group(group).strip(" .,;:")
        if name == "isbn":
            value = _normalized_isbn(match.group(group))
        elif name == "publication_year":
            value = int(match.group(group))
        if value in (None, ""):
            continue
        return ResolvedField(value, BookMetadataProvenance.FRONT_MATTER, f"pdf:page-{line.page}")
    return _unresolved(reason)


def resolve_book_metadata(
    *,
    pdf: PdfBookEvidence,
    config: ConfiguredBookEvidence,
    assertions: Mapping[str, OperatorAssertion] | None = None,
) -> BookMetadataResolution:
    """Resolve every field from local evidence, preferring source over configuration."""
    claimed = dict(assertions or {})
    unknown = sorted(set(claimed) - set(BOOK_METADATA_FIELDS))
    if unknown:
        raise ValueError(f"unknown book metadata field: {', '.join(unknown)}")
    title = _resolve_title(pdf, config, claimed)
    author = _resolve_author(pdf, config, claimed)
    series_name, series_index = _resolve_series(pdf, config, claimed)
    fields: dict[str, ResolvedField] = {
        "title_en": title,
        "author": author,
        "series_name": series_name,
        "series_index": series_index,
        "isbn": _resolve_pattern(
            pdf, claimed, "isbn", _ISBN, "isbn", "no valid ISBN in front or back matter"
        ),
        "publisher": _resolve_pattern(
            pdf, claimed, "publisher", _PUBLISHER, "publisher", "no publisher imprint statement"
        ),
        "publication_year": _resolve_pattern(
            pdf,
            claimed,
            "publication_year",
            _PUBLICATION_YEAR,
            "year",
            "no copyright year statement",
        ),
        "edition": _resolve_pattern(
            pdf, claimed, "edition", _EDITION, "edition", "no edition statement"
        ),
        "source_language": ResolvedField(
            config.source_language,
            BookMetadataProvenance.CONFIGURATION,
            "config/book.yaml:source_language",
        ),
        "target_language": ResolvedField(
            config.target_language,
            BookMetadataProvenance.CONFIGURATION,
            "config/book.yaml:target_language",
        ),
    }
    configured_title_fa = _usable(config.title_fa)
    fields["title_fa"] = _assertion(claimed, "title_fa") or (
        ResolvedField(
            configured_title_fa, BookMetadataProvenance.CONFIGURATION, "config/book.yaml:title_fa"
        )
        if configured_title_fa is not None
        else _unresolved("target title is a human translation decision and was never recorded")
    )
    fields["book_id"] = _resolve_book_id(title, config, claimed)
    warnings = tuple(
        f"{name} is unresolved: {fields[name].evidence}"
        for name in BOOK_METADATA_FIELDS
        if fields[name].provenance is BookMetadataProvenance.UNRESOLVED
    )
    return BookMetadataResolution({name: fields[name] for name in BOOK_METADATA_FIELDS}, warnings)


def _resolve_book_id(
    title: ResolvedField,
    config: ConfiguredBookEvidence,
    assertions: Mapping[str, OperatorAssertion],
) -> ResolvedField:
    """Resolve the project identifier: a configured non-placeholder ID stays authoritative."""
    asserted = _assertion(assertions, "book_id")
    if asserted is not None:
        return asserted
    configured = _usable(config.book_id)
    if configured is not None:
        return ResolvedField(
            configured, BookMetadataProvenance.CONFIGURATION, "config/book.yaml:book_id"
        )
    if not isinstance(title.value, str):
        return _unresolved("book ID needs a resolved English title")
    return ResolvedField(
        derive_book_id(
            title_en=title.value,
            source_language=config.source_language,
            target_language=config.target_language,
        ),
        title.provenance,
        f"derived from title_en ({title.evidence})",
    )
