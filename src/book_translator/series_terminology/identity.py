"""Pure identities and versions for the series terminology layer."""

import hashlib
import json
import unicodedata
from typing import Any

from book_translator.models import (
    ReferenceContextIndex,
    ReferenceContextPackage,
    SeriesTerminologyPolicy,
)

#: Governs policy import, promotion, derivation, and context assembly as one engine.
SERIES_TERMINOLOGY_ENGINE_VERSION = "1.2.0"

#: The rendering contract of the length-framed ``terminology-policy`` prompt section.
TERMINOLOGY_CONTEXT_CONTRACT_VERSION = "series-terminology-context/1.1.0"

#: The persisted shape of a per-chunk reference-context package.
REFERENCE_CONTEXT_CONTRACT_VERSION = "reference-context/1.0.0"

#: The rule that promotes corpus evidence to ``STRONG_REFERENCE``; part of every policy.
PROMOTION_RULE_VERSION = "cross-source-agreement/1.0.0"

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "＇": "'"})


def canonical_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        + b"\n"
    )


def normalize_source_term(value: str) -> str:
    """Fold only documented typographic equivalents; never case, inflection, or spelling.

    The published corpora and the two books of this project disagree on apostrophe glyphs, so a
    series concept is keyed on the straight-apostrophe form. The glyph that actually occurs in a
    given book is restored by derivation, because exact matching downstream is byte-exact.
    """
    return " ".join(unicodedata.normalize("NFC", value).translate(_APOSTROPHES).split())


def typographic_variants(term: str) -> tuple[str, ...]:
    """Every apostrophe rendering a byte-exact matcher may need to look for."""
    straight = normalize_source_term(term)
    variants = [straight]
    if "'" in straight:
        variants.append(straight.replace("'", "’"))
    return tuple(variants)


def concept_identity(source_term: str, source_language: str) -> str:
    payload = {
        "source_language": source_language,
        "source_term": normalize_source_term(source_term),
    }
    return f"sc-{hashlib.sha256(canonical_bytes(payload)).hexdigest()[:24]}"


def canonical_policy_bytes(policy: SeriesTerminologyPolicy) -> bytes:
    payload = policy.model_dump(mode="json", exclude_none=False)
    payload["terms"] = sorted(payload["terms"], key=lambda item: item["concept_id"])
    return canonical_bytes(payload)


def policy_identity(policy: SeriesTerminologyPolicy) -> str:
    return hashlib.sha256(canonical_policy_bytes(policy)).hexdigest()


def package_identity(package: ReferenceContextPackage) -> str:
    return hashlib.sha256(
        canonical_bytes(package.model_dump(mode="json", exclude_none=False))
    ).hexdigest()


def index_identity(index: ReferenceContextIndex) -> str:
    return hashlib.sha256(
        canonical_bytes(index.model_dump(mode="json", exclude_none=False))
    ).hexdigest()
