"""Pure compatibility policy for re-evaluating a historically QA-rejected provider output.

Accepted-translation reuse (:mod:`book_translator.translation_reuse_policy`) can demand byte
identity of the rendered request, because a translation that already passed QA has nothing to
gain from a changed one. The outputs this policy governs are the opposite case: they are worth
revisiting *precisely because* the terminology QA rules moved after the provider produced them,
so requiring an identical rendered request would refuse every candidate the workflow exists for.

Arbitrary prompt drift is still refused. The rule is structural rather than string-specific:

* The **request contract** — source, context, languages, system message, and output contract —
  must be identical. Nothing in this policy can waive that.
* The rendered user message is compared per framed section, since the framed sections are the
  only channel through which a snapshot resource reaches a provider. Every section except the
  resource projection (``glossary-data``) is part of the contract above; a change in one of them
  is a changed prompt, not a changed lookup table.
* A changed resource projection is eligible only when it is *attributable and governed*: every
  other package resource is byte identical, the terminology resource is the one that moved, that
  move is a contiguous chain of published governed registry edits, and no locked or user-owned
  canonical decision reaching this job changed inside it.
* Policy 1.1.0 admits the series-policy lineage beside the registry lineage. There the resource
  projection is the length-framed ``terminology-policy`` section, the moving resources are the
  frozen series policy and the registry derived from it, and "governed" means a contiguous chain of
  published policy amendments. The reference-context package the section also renders and the
  context rendering contract are part of the request and must be identical.

Nothing here performs I/O. The caller supplies both prompt packages, the per-section digests it
read out of the two rendered user messages, the historical attempt's output contract, and the
terminology-lineage facts it proved from the project's governed records.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from book_translator.models import (
    HistoricalCompatibilityTier,
    PromptPackage,
    SeriesTerm,
    SeriesTerminologyPolicy,
    SeriesTermStatus,
)
from book_translator.series_terminology.context import term_mentioned
from book_translator.series_terminology.derivation import count_occurrences
from book_translator.series_terminology.identity import typographic_variants

#: 1.2.0 admits one more eligible tier, ``occurrence_exemption_drift``: a request that differs
#: from the historical one only because user-adjudicated occurrence exemptions withheld decisions
#: from it, proven by rendering the new request without them and reproducing the historical
#: user message byte for byte. Every verdict 1.1.0 reached is unchanged.
HISTORICAL_REEVALUATION_POLICY_VERSION = "1.2.0"

#: Framed sections that carry the request contract itself rather than a resource lookup.
CONTRACT_FRAMED_SECTIONS = frozenset(
    {"translation-prompt-rules", "style-guide", "previous-context", "source-text"}
)
#: The single framed section a governed registry move is allowed to change.
RESOURCE_PROJECTION_SECTION = "glossary-data"
#: The framed section through which a frozen series policy reaches a provider.
SERIES_PROJECTION_SECTION = "terminology-policy"
RESOURCE_PROJECTION_SECTIONS = frozenset({RESOURCE_PROJECTION_SECTION, SERIES_PROJECTION_SECTION})
#: Resources a governed terminology move is allowed to change; everything else must be identical.
_MOVABLE_RESOURCE_PREFIXES = ("terminology_", "series_terminology_")

EXACT_RENDERED_REQUEST_RATIONALE = (
    "historical request identity proven: the request contract and both rendered message digests "
    "are identical, so the historical output answers exactly the request this run would send"
)
GOVERNED_RESOURCE_DRIFT_RATIONALE = (
    "historical request differs only in the rendered resource projection, and that difference is "
    "attributable to a terminology move explained end to end by published governed registry "
    "edits that changed no locked or user-owned canonical decision reaching this job"
)


@dataclass(frozen=True, slots=True)
class TerminologyDriftFacts:
    """What the caller proved about the terminology move behind a changed request."""

    from_semantic_sha256: str
    to_semantic_sha256: str
    governed: bool
    transition_ids: tuple[str, ...] = ()
    locked_decision_drift: tuple[str, ...] = ()
    lineage_kind: str = "registry"
    waived_term_changes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HistoricalCompatibility:
    """Verdict for one historical/new prompt package pair under this policy."""

    tier: HistoricalCompatibilityTier
    blocking_reasons: tuple[str, ...]
    verified_identity_checks: tuple[str, ...]
    changed_framed_sections: tuple[str, ...] = ()
    observed_resource_drift: tuple[str, ...] = ()
    rationale: str = ""
    occurrence_exemption_ids: tuple[str, ...] = ()
    counterfactual_user_message_sha256: str | None = None

    @property
    def eligible(self) -> bool:
        return self.tier.eligible and not self.blocking_reasons


def _request_contract(
    old: PromptPackage, new: PromptPackage, historical_output_contract: str
) -> dict[str, bool]:
    """Equalities that define the request contract; none of them is ever waivable."""
    return {
        "source_hash": old.chunk_source_sha256 == new.chunk_source_sha256,
        "context_hash": old.context_sha256 == new.context_sha256,
        "source_language": old.source_language == new.source_language,
        "target_language": old.target_language == new.target_language,
        "system_message_hash": old.system_message.sha256 == new.system_message.sha256,
        "output_contract": old.output_contract_version == new.output_contract_version,
        "historical_output_contract": historical_output_contract == new.output_contract_version,
    }


def _resource_equality(old: PromptPackage, new: PromptPackage) -> dict[str, bool]:
    """Whole-resource equalities, split so terminology can be reasoned about on its own."""

    def reference(package: PromptPackage, name: str) -> tuple[str | None, str | None]:
        value = getattr(package, name)
        return (None, None) if value is None else (value.sha256, value.version)

    checks: dict[str, bool] = {}
    for name in (
        "terminology",
        "prompt",
        "glossary",
        "style_guide",
        "source_anomalies",
        "series_terminology",
        "reference_context",
    ):
        old_reference, new_reference = reference(old, name), reference(new, name)
        label = "style" if name == "style_guide" else name
        checks[f"{label}_hash"] = old_reference[0] == new_reference[0]
        checks[f"{label}_version"] = old_reference[1] == new_reference[1]
    checks["terminology_context_contract"] = (
        old.terminology_context_contract_version == new.terminology_context_contract_version
    )
    checks["renderer_version"] = old.renderer_version == new.renderer_version
    checks["chapter_chunk_count"] = old.total_chapter_chunks == new.total_chapter_chunks
    return checks


def changed_framed_sections(
    old_sections: Mapping[str, str], new_sections: Mapping[str, str]
) -> tuple[str, ...]:
    """Name the framed sections whose effective content differs, without disclosing content."""
    return tuple(
        name
        for name in sorted(set(old_sections) | set(new_sections))
        if old_sections.get(name) != new_sections.get(name)
    )


def _label(section: str) -> str:
    return f"effective_{section.replace('-', '_')}_changed"


def evaluate_historical_compatibility(
    old: PromptPackage,
    new: PromptPackage,
    *,
    old_sections: Mapping[str, str],
    new_sections: Mapping[str, str],
    historical_output_contract: str,
    terminology_drift: TerminologyDriftFacts,
) -> HistoricalCompatibility:
    """Classify a historical package against a new one for rejected-output re-evaluation."""
    contract = _request_contract(old, new, historical_output_contract)
    verified = tuple(name for name, equal in contract.items() if equal)
    resources = _resource_equality(old, new)
    drift = tuple(f"{name}_mismatch" for name, equal in resources.items() if not equal)
    contract_reasons = tuple(f"{name}_mismatch" for name, equal in contract.items() if not equal)
    if contract_reasons:
        return HistoricalCompatibility(
            tier=HistoricalCompatibilityTier.CONTRACT_CHANGED,
            blocking_reasons=contract_reasons,
            verified_identity_checks=verified,
            observed_resource_drift=drift,
        )

    sections = changed_framed_sections(old_sections, new_sections)
    identical_request = old.user_message.sha256 == new.user_message.sha256
    if identical_request:
        if sections:
            return HistoricalCompatibility(
                tier=HistoricalCompatibilityTier.UNSAFE_DRIFT,
                blocking_reasons=("rendered_projection_inconsistent",),
                verified_identity_checks=(*verified, "user_message_hash"),
                changed_framed_sections=tuple(_label(name) for name in sections),
                observed_resource_drift=drift,
            )
        return HistoricalCompatibility(
            tier=HistoricalCompatibilityTier.EXACT_RENDERED_REQUEST,
            blocking_reasons=(),
            verified_identity_checks=(*verified, "user_message_hash"),
            observed_resource_drift=drift,
            rationale=EXACT_RENDERED_REQUEST_RATIONALE,
        )

    labelled = tuple(_label(name) for name in sections)
    if not sections:
        return HistoricalCompatibility(
            tier=HistoricalCompatibilityTier.UNSAFE_DRIFT,
            blocking_reasons=("rendered_projection_inconsistent",),
            verified_identity_checks=verified,
            observed_resource_drift=drift,
        )
    contract_sections = tuple(name for name in sections if name in CONTRACT_FRAMED_SECTIONS)
    if contract_sections:
        return HistoricalCompatibility(
            tier=HistoricalCompatibilityTier.CONTRACT_CHANGED,
            blocking_reasons=tuple(_label(name) for name in contract_sections),
            verified_identity_checks=verified,
            changed_framed_sections=labelled,
            observed_resource_drift=drift,
        )
    unknown = tuple(name for name in sections if name not in RESOURCE_PROJECTION_SECTIONS)
    reasons: tuple[str, ...] = tuple(
        f"unrecognized_framed_section_changed:{name}" for name in unknown
    )
    if SERIES_PROJECTION_SECTION in sections and resources["series_terminology_hash"]:
        # A series section can only move with the series policy it renders.
        reasons += ("series_projection_change_unattributable",)
    ungoverned = tuple(
        reason for reason in drift if not reason.startswith(_MOVABLE_RESOURCE_PREFIXES)
    )
    if ungoverned:
        reasons += ("ungoverned_resource_drift", *ungoverned)
    if resources["terminology_hash"]:
        # The projection moved without the only resource allowed to move it, so nothing in the
        # governed record can explain the change.
        reasons += ("resource_projection_change_unattributable",)
    if not terminology_drift.governed:
        reasons += ("terminology_drift_not_governed",)
    if terminology_drift.locked_decision_drift:
        reasons += ("locked_terminology_drift", *terminology_drift.locked_decision_drift)
    if reasons:
        return HistoricalCompatibility(
            tier=HistoricalCompatibilityTier.UNSAFE_DRIFT,
            blocking_reasons=reasons,
            verified_identity_checks=verified,
            changed_framed_sections=labelled,
            observed_resource_drift=drift,
        )
    return HistoricalCompatibility(
        tier=HistoricalCompatibilityTier.GOVERNED_RESOURCE_DRIFT,
        blocking_reasons=(),
        verified_identity_checks=verified,
        changed_framed_sections=labelled,
        observed_resource_drift=drift,
        rationale=GOVERNED_RESOURCE_DRIFT_RATIONALE,
    )


OCCURRENCE_EXEMPTION_DRIFT_RATIONALE = (
    "historical request differs only because user-adjudicated occurrence exemptions withheld "
    "decisions from the new request: rendering the new request without them reproduces the "
    "historical user message exactly, so the historical output answers every other instruction "
    "this run would send"
)


def evaluate_exemption_compatibility(
    old: PromptPackage,
    new: PromptPackage,
    *,
    old_sections: Mapping[str, str],
    new_sections: Mapping[str, str],
    historical_output_contract: str,
    counterfactual_user_message_sha256: str,
    exemption_ids: tuple[str, ...],
) -> HistoricalCompatibility:
    """Classify a request that moved while occurrence exemptions reach the job.

    The only admissible explanation is the exemptions themselves: with them withheld, the new
    request must be the historical request exactly. Anything else — a terminology move on top of
    the exemptions included — is refused rather than explained twice.
    """
    contract = _request_contract(old, new, historical_output_contract)
    verified = tuple(name for name, equal in contract.items() if equal)
    drift = tuple(
        f"{name}_mismatch" for name, equal in _resource_equality(old, new).items() if not equal
    )
    contract_reasons = tuple(f"{name}_mismatch" for name, equal in contract.items() if not equal)
    if contract_reasons:
        return HistoricalCompatibility(
            tier=HistoricalCompatibilityTier.CONTRACT_CHANGED,
            blocking_reasons=contract_reasons,
            verified_identity_checks=verified,
            observed_resource_drift=drift,
        )
    sections = changed_framed_sections(old_sections, new_sections)
    labelled = tuple(_label(name) for name in sections)
    reasons: tuple[str, ...] = ()
    if not exemption_ids:
        reasons += ("occurrence_exemptions_absent",)
    if old.user_message.sha256 == new.user_message.sha256:
        reasons += ("rendered_request_unchanged",)
    if counterfactual_user_message_sha256 != old.user_message.sha256:
        reasons += ("occurrence_exemptions_not_whole_difference",)
    contract_sections = tuple(name for name in sections if name in CONTRACT_FRAMED_SECTIONS)
    if contract_sections:
        reasons += tuple(_label(name) for name in contract_sections)
    if reasons:
        return HistoricalCompatibility(
            tier=HistoricalCompatibilityTier.UNSAFE_DRIFT,
            blocking_reasons=reasons,
            verified_identity_checks=verified,
            changed_framed_sections=labelled,
            observed_resource_drift=drift,
        )
    return HistoricalCompatibility(
        tier=HistoricalCompatibilityTier.OCCURRENCE_EXEMPTION_DRIFT,
        blocking_reasons=(),
        verified_identity_checks=(*verified, "counterfactual_user_message_hash"),
        changed_framed_sections=labelled,
        observed_resource_drift=drift,
        rationale=OCCURRENCE_EXEMPTION_DRIFT_RATIONALE,
        occurrence_exemption_ids=exemption_ids,
        counterfactual_user_message_sha256=counterfactual_user_message_sha256,
    )


_DECIDED = frozenset({SeriesTermStatus.LOCKED, SeriesTermStatus.STRONG_REFERENCE})


@dataclass(frozen=True, slots=True)
class SeriesTermDrift:
    """Per-job verdict on the series-policy terms whose change reaches the job."""

    blocking_reasons: tuple[str, ...]
    waived_concepts: tuple[str, ...]


def _occurs(spelling: str, source_text: str, context_text: str) -> bool:
    return any(
        count_occurrences(variant, source_text) or count_occurrences(variant, context_text)
        for variant in typographic_variants(spelling)
    )


def _still_authorized(
    spelling: str, withdrawn_target: str, owner_id: str, policy: SeriesTerminologyPolicy
) -> bool:
    """Whether another decided term now owns *spelling* and authorizes the withdrawn target."""
    return any(
        term.concept_id != owner_id
        and term.status in _DECIDED
        and spelling in (term.source_term, *term.aliases)
        and withdrawn_target in (term.target_term, *term.allowed_target_forms)
        for term in policy.terms
    )


def _term_reasons(
    before: SeriesTerm,
    after: SeriesTerm,
    new_policy: SeriesTerminologyPolicy,
    source_text: str,
    context_text: str,
) -> tuple[str, ...]:
    concept = after.concept_id
    reasons: list[str] = []
    if before.status is not after.status or before.target_term != after.target_term:
        reasons.append(f"series_decision_changed:{concept}")
    if before.classification is not after.classification:
        reasons.append(f"series_classification_changed:{concept}")
    if before.variants != after.variants or before.conflicts != after.conflicts:
        reasons.append(f"series_evidence_changed:{concept}")
    if set(before.allowed_target_forms) - set(after.allowed_target_forms):
        reasons.append(f"series_authorized_form_withdrawn:{concept}")
    if any(
        _occurs(alias, source_text, context_text)
        for alias in set(after.aliases) - set(before.aliases)
    ):
        # The historical request never told the provider this spelling was the term.
        reasons.append(f"series_alias_added_reaches_job:{concept}")
    for alias in sorted(set(before.aliases) - set(after.aliases)):
        if not _occurs(alias, source_text, context_text):
            continue
        if before.target_term is None or not _still_authorized(
            alias, before.target_term, concept, new_policy
        ):
            # The historical request instructed a rendering for this spelling that the new policy
            # withdraws and nothing still authorizes; whether the provider obeyed it is invisible
            # to every mechanical check, so no carry-forward may rest on it.
            reasons.append(f"series_withdrawn_instruction_reaches_job:{concept}")
            break
    return tuple(reasons)


def evaluate_series_term_drift(
    old_policy: SeriesTerminologyPolicy,
    new_policy: SeriesTerminologyPolicy,
    *,
    source_text: str,
    context_text: str,
) -> SeriesTermDrift:
    """Classify every changed series term reaching one job as waivable or blocking.

    A change is waivable only when it cannot have altered what the historical request asked for:
    the preferred target and force are unchanged, authorized forms only widened, no alias the job
    contains was added, and every withdrawn alias the job contains is now owned by another decided
    term that still authorizes the target the historical request named for it. Current QA then
    judges the output under the new policy, so a widened authorization is honored and a narrowed
    one is enforced. Anything else — including a term added or removed — blocks.
    """
    old_terms = {term.concept_id: term for term in old_policy.terms}
    new_terms = {term.concept_id: term for term in new_policy.terms}
    blocking: list[str] = []
    waived: list[str] = []
    for concept in sorted(set(old_terms) | set(new_terms)):
        before, after = old_terms.get(concept), new_terms.get(concept)
        if before == after:
            continue
        if not any(
            term_mentioned(term, source_text, context_text)
            for term in (before, after)
            if term is not None
        ):
            continue
        if before is None or after is None:
            blocking.append(f"series_term_added_or_removed:{concept}")
            continue
        reasons = _term_reasons(before, after, new_policy, source_text, context_text)
        if reasons:
            blocking.extend(reasons)
        else:
            waived.append(concept)
    return SeriesTermDrift(tuple(blocking), tuple(waived))
