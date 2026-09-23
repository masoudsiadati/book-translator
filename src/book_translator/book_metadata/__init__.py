"""Book Metadata Resolution Engine public API."""

from book_translator.book_metadata.engine import (
    BookMetadataResolution,
    ConfiguredBookEvidence,
    EvidenceLine,
    OperatorAssertion,
    PdfBookEvidence,
    ResolvedField,
    resolve_book_metadata,
)
from book_translator.book_metadata.evidence import collect_pdf_evidence
from book_translator.book_metadata.identity import (
    BOOK_METADATA_ENGINE_VERSION,
    PLACEHOLDER_TOKENS,
    BookIdentityError,
    derive_book_id,
    is_placeholder,
    slugify,
)
from book_translator.book_metadata.workflow import (
    IDENTITY_FIELDS,
    BookMetadataError,
    BookMetadataPlan,
    execute_book_metadata,
    load_book_metadata,
    placeholder_fields,
    plan_book_metadata,
    serialize_book_metadata,
)

__all__ = [
    "BOOK_METADATA_ENGINE_VERSION",
    "IDENTITY_FIELDS",
    "PLACEHOLDER_TOKENS",
    "BookIdentityError",
    "BookMetadataError",
    "BookMetadataPlan",
    "BookMetadataResolution",
    "ConfiguredBookEvidence",
    "EvidenceLine",
    "OperatorAssertion",
    "PdfBookEvidence",
    "ResolvedField",
    "collect_pdf_evidence",
    "derive_book_id",
    "execute_book_metadata",
    "is_placeholder",
    "load_book_metadata",
    "placeholder_fields",
    "plan_book_metadata",
    "resolve_book_metadata",
    "serialize_book_metadata",
    "slugify",
]
