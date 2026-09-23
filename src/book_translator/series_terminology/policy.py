"""Series policy persistence and the governed explicit-user edit."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.models import (
    SCHEMA_VERSION,
    ObservedVariant,
    SeriesProvenanceKind,
    SeriesTerm,
    SeriesTerminologyPolicy,
    SeriesTermProvenance,
    SeriesTermStatus,
    TerminologyType,
)
from book_translator.series_terminology.identity import (
    PROMOTION_RULE_VERSION,
    concept_identity,
    normalize_source_term,
)


class SeriesTerminologyError(ValueError):
    """Raised when the series policy or its inputs violate their contract."""


def empty_policy(
    *, series_id: str, source_language: str, target_language: str
) -> SeriesTerminologyPolicy:
    return SeriesTerminologyPolicy(
        schema_version=SCHEMA_VERSION,
        version="1.0.0",
        series_id=series_id,
        source_language=source_language,
        target_language=target_language,
        promotion_rule_version=PROMOTION_RULE_VERSION,
    )


def load_policy(path: Path) -> SeriesTerminologyPolicy:
    try:
        return SeriesTerminologyPolicy.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise SeriesTerminologyError(f"invalid series terminology policy at {path}: {exc}") from exc


def serialized_policy(policy: SeriesTerminologyPolicy) -> str:
    ordered = policy.model_copy(
        update={"terms": sorted(policy.terms, key=lambda item: item.concept_id)}
    )
    rendered = yaml.safe_dump(
        ordered.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if not isinstance(rendered, str):
        raise SeriesTerminologyError("could not serialize the series terminology policy")
    return rendered


def save_policy(policy: SeriesTerminologyPolicy, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, serialized_policy(policy), overwrite=True)


def find_term(policy: SeriesTerminologyPolicy, source_term: str) -> SeriesTerm | None:
    concept_id = concept_identity(source_term, policy.source_language)
    return next((term for term in policy.terms if term.concept_id == concept_id), None)


def set_explicit_term(
    policy: SeriesTerminologyPolicy,
    *,
    source_term: str,
    target_term: str,
    classification: TerminologyType,
    status: SeriesTermStatus = SeriesTermStatus.LOCKED,
    rationale: str,
    aliases: tuple[str, ...] = (),
    allowed_target_forms: tuple[str, ...] = (),
    origin: str = "set-series-terminology",
) -> SeriesTerminologyPolicy:
    """Record an explicit user decision, preserving every observed variant already known.

    The historical evidence attached to the concept is kept exactly; only the decision layer
    changes, and the provenance says a person made it.
    """
    if status is SeriesTermStatus.OBSERVED_VARIANTS:
        raise SeriesTerminologyError("an explicit user decision must be LOCKED or STRONG_REFERENCE")
    forms = sorted({form.strip() for form in allowed_target_forms})
    if "" in forms or target_term in forms:
        raise SeriesTerminologyError(
            "an authorized target form must be non-empty and differ from the target term"
        )
    normalized = normalize_source_term(source_term)
    existing = find_term(policy, normalized)
    variants: list[ObservedVariant] = list(existing.variants) if existing is not None else []
    conflicts = list(existing.conflicts) if existing is not None else []
    notes = list(existing.notes) if existing is not None else []
    if existing is not None and existing.provenance.kind is not SeriesProvenanceKind.EXPLICIT_USER:
        notes.append(
            f"superseded {existing.status.value} ({existing.provenance.kind.value}) "
            f"target {existing.target_term!r} by explicit user decision"
        )
    term = SeriesTerm(
        concept_id=concept_identity(normalized, policy.source_language),
        source_term=normalized,
        aliases=sorted({normalize_source_term(alias) for alias in aliases} - {normalized}),
        classification=classification,
        status=status,
        target_term=target_term,
        allowed_target_forms=forms,
        provenance=SeriesTermProvenance(
            kind=SeriesProvenanceKind.EXPLICIT_USER,
            origin=origin,
            rationale=rationale,
        ),
        variants=variants,
        conflicts=conflicts,
        notes=notes,
    )
    remaining = [item for item in policy.terms if item.concept_id != term.concept_id]
    return policy.model_copy(update={"terms": [*remaining, term]})
