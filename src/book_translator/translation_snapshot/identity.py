"""Pure content-addressed translation-manifest identity."""

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from book_translator.models import TranslationManifest

SNAPSHOT_ENGINE_VERSION = "1.2.0"
SNAPSHOT_DIGEST_LENGTH = 24


class ManifestIdentityFormat(StrEnum):
    """Supported immutable manifest identity representations."""

    LEGACY_V1 = "legacy-v1"
    FROZEN_TERMINOLOGY_V1 = "frozen-terminology-v1"
    FROZEN_RESOURCES_V1 = "frozen-resources-v1"
    #: Adds the series terminology lineage block; hashed exactly as written.
    SERIES_TERMINOLOGY_V1 = "series-terminology-v1"


#: Fields the series lineage added. A manifest without a series policy omits them from identity
#: so every earlier identity representation keeps producing the same digest.
SERIES_LINEAGE_FIELDS = (
    "series_terminology",
    "reference_corpus",
    "reference_context",
    "terminology_context_contract_version",
)


def _without_absent_series_lineage(payload: Mapping[str, Any]) -> dict[str, Any]:
    canonical = dict(payload)
    if canonical.get("series_terminology") is None:
        for field in SERIES_LINEAGE_FIELDS:
            canonical.pop(field, None)
    return canonical


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def persisted_manifest_identity_format(
    payload: Mapping[str, Any],
) -> ManifestIdentityFormat:
    """Select identity rules from fields present in the immutable representation."""
    if payload.get("series_terminology") is not None:
        return ManifestIdentityFormat.SERIES_TERMINOLOGY_V1
    prompt = payload.get("prompt")
    if isinstance(prompt, Mapping) and prompt.get("frozen_file") is not None:
        # Every mutable translation input is durably frozen; the payload is hashed as written.
        return ManifestIdentityFormat.FROZEN_RESOURCES_V1
    terminology = payload.get("terminology")
    if isinstance(terminology, Mapping) and "frozen_file" in terminology:
        return ManifestIdentityFormat.FROZEN_TERMINOLOGY_V1
    return ManifestIdentityFormat.LEGACY_V1


def canonical_manifest_bytes(manifest: TranslationManifest) -> bytes:
    """Serialize a newly created manifest with the current identity format."""
    payload = _without_absent_series_lineage(manifest.model_dump(mode="json", exclude_none=False))
    if manifest.prompt.frozen_file is not None:
        return _canonical_json_bytes(payload)
    if payload.get("source_anomalies") is None:
        payload.pop("source_anomalies", None)
    # Preserve the version-1 identity of historical manifests that predate terminology resources.
    if payload.get("terminology") is None:
        payload.pop("terminology", None)
    elif payload["terminology"].get("frozen_file") is None:
        # Preserve manifests written before the additive frozen-resource reference.
        payload["terminology"].pop("frozen_file", None)
    return _canonical_json_bytes(payload)


def canonical_persisted_manifest_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialize the validated field-present payload of an immutable manifest.

    Legacy identity was defined over the manifest fields that existed at publication
    time.  In particular, model defaults added by newer application versions must not
    be injected before hashing.
    """
    identity_format = persisted_manifest_identity_format(payload)
    if identity_format is ManifestIdentityFormat.SERIES_TERMINOLOGY_V1:
        return _canonical_json_bytes(payload)
    canonical_payload = _without_absent_series_lineage(payload)
    if identity_format is ManifestIdentityFormat.FROZEN_RESOURCES_V1:
        return _canonical_json_bytes(canonical_payload)
    if payload.get("source_anomalies") is None:
        canonical_payload.pop("source_anomalies", None)
    if identity_format is ManifestIdentityFormat.LEGACY_V1:
        terminology = canonical_payload.get("terminology")
        if terminology is None:
            canonical_payload.pop("terminology", None)
        elif isinstance(terminology, Mapping) and terminology.get("frozen_file") is None:
            canonical_terminology = dict(terminology)
            canonical_terminology.pop("frozen_file", None)
            canonical_payload["terminology"] = canonical_terminology
    return _canonical_json_bytes(canonical_payload)


def manifest_identity(manifest: TranslationManifest) -> tuple[str, str]:
    digest = hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
    snapshot_id = (
        f"translation-{manifest.book.target_language.lower()}-{digest[:SNAPSHOT_DIGEST_LENGTH]}"
    )
    return snapshot_id, digest


def persisted_manifest_identity(
    payload: Mapping[str, Any], manifest: TranslationManifest
) -> tuple[str, str]:
    """Derive identity using the supported format of a persisted manifest."""
    digest = hashlib.sha256(canonical_persisted_manifest_bytes(payload)).hexdigest()
    snapshot_id = (
        f"translation-{manifest.book.target_language.lower()}-{digest[:SNAPSHOT_DIGEST_LENGTH]}"
    )
    return snapshot_id, digest
