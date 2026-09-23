"""Pure reuse compatibility policy over effective provider-facing job inputs.

Reuse compatibility used to be a conjunction of whole-resource hash equalities. That rule is
unsound in both directions. It is too strict because a frozen resource is global while the
renderer projects only the part of it that a particular chunk needs, so a resource edit that
never reaches a job still invalidated that job. It is also incomplete, because not every
rendered input is recorded as a package resource reference (the open-question registry is
rendered but never referenced), so resource equality never proved request equality anyway.

The policy here compares *effective job inputs* instead. Every snapshot resource reaches a
provider through exactly one channel: the deterministically rendered system and user messages.
When both rendered message digests are equal, and the source, context, languages, and output
contract are equal, the provider-facing request that produced the historical translation is
byte identical to the request the new run would send. Resource-level drift is then observed,
recorded, and waived rather than treated as a blocking difference.

Nothing here performs I/O; the caller supplies the two prompt packages and, for divergence
classification, the per-section digests recorded inside the rendered user messages.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from book_translator.models import PromptPackage, ReuseCompatibilityTier

REUSE_COMPATIBILITY_POLICY_VERSION = "2.0.0"

EXACT_RENDERED_REQUEST_RATIONALE = (
    "effective provider-facing request identity proven: source, context, languages, output "
    "contract, and both rendered message digests are identical, so recorded resource drift "
    "did not reach this job's request"
)


@dataclass(frozen=True, slots=True)
class RequestCompatibility:
    """Verdict for one historical/new prompt package pair."""

    tier: ReuseCompatibilityTier
    identity_reasons: tuple[str, ...]
    verified_identity_checks: tuple[str, ...]
    resource_drift_reasons: tuple[str, ...]

    @property
    def exact(self) -> bool:
        return self.tier is ReuseCompatibilityTier.EXACT_RENDERED_REQUEST


def _request_identity(old: PromptPackage, new: PromptPackage) -> dict[str, bool]:
    """Equalities that together define one effective provider-facing request."""
    return {
        "source_hash": old.chunk_source_sha256 == new.chunk_source_sha256,
        "context_hash": old.context_sha256 == new.context_sha256,
        "source_language": old.source_language == new.source_language,
        "target_language": old.target_language == new.target_language,
        "system_message_hash": old.system_message.sha256 == new.system_message.sha256,
        "user_message_hash": old.user_message.sha256 == new.user_message.sha256,
        "output_contract": old.output_contract_version == new.output_contract_version,
    }


def _resource_drift(old: PromptPackage, new: PromptPackage) -> dict[str, bool]:
    """Whole-resource equalities that only reach a provider through the rendered messages."""

    def reference(package: PromptPackage, name: str) -> tuple[str | None, str | None]:
        value = getattr(package, name)
        return (None, None) if value is None else (value.sha256, value.version)

    checks: dict[str, bool] = {}
    for name in ("terminology", "prompt", "glossary", "style_guide", "source_anomalies"):
        old_reference, new_reference = reference(old, name), reference(new, name)
        label = "style" if name == "style_guide" else name
        checks[f"{label}_hash"] = old_reference[0] == new_reference[0]
        checks[f"{label}_version"] = old_reference[1] == new_reference[1]
    checks["renderer_version"] = old.renderer_version == new.renderer_version
    checks["chapter_chunk_count"] = old.total_chapter_chunks == new.total_chapter_chunks
    return checks


def evaluate_request_compatibility(old: PromptPackage, new: PromptPackage) -> RequestCompatibility:
    """Classify a historical package against a new one by effective-request identity."""
    identity = _request_identity(old, new)
    drift = _resource_drift(old, new)
    identity_reasons = tuple(f"{name}_mismatch" for name, equal in identity.items() if not equal)
    tier = (
        ReuseCompatibilityTier.INCOMPATIBLE
        if identity_reasons
        else ReuseCompatibilityTier.EXACT_RENDERED_REQUEST
    )
    return RequestCompatibility(
        tier=tier,
        identity_reasons=identity_reasons,
        verified_identity_checks=tuple(name for name, equal in identity.items() if equal),
        resource_drift_reasons=tuple(
            f"{name}_mismatch" for name, equal in drift.items() if not equal
        ),
    )


def classify_rendered_divergence(
    old_sections: Mapping[str, str], new_sections: Mapping[str, str]
) -> tuple[str, ...]:
    """Name the framed user-message sections whose effective content changed.

    This is the generic per-job effective-projection mechanism: the terminology, glossary,
    style-guide, prompt-rules, context, and source projections each occupy their own framed
    section with a content digest, so a changed request can be attributed without inspecting
    or disclosing any framed content.
    """
    return tuple(
        f"effective_{name.replace('-', '_')}_changed"
        for name in sorted(set(old_sections) | set(new_sections))
        if old_sections.get(name) != new_sections.get(name)
    )
