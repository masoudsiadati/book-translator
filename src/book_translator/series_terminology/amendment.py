"""Pure, all-or-nothing application of an explicit series-policy amendment.

``set-series-terminology`` records one explicit decision at a time and leaves no trace of what the
term said before. That is enough to author a policy and not enough to *govern* one that a frozen
snapshot already pins: a translation run answering the old policy can only be carried forward
under a new one if the move between them is on record. An amendment is that record's subject.

The rules are deliberately narrow. An amendment restates existing terms and never creates one, so
it cannot smuggle an ordinary word into the policy. It is refused whole — nothing is applied — if
any item would override an explicit user decision: a different target, a different force, a
withdrawn authorized form, a withdrawn alias, or an alias that another user decision already
owns. Everything else about a term (its observed variants, conflicts, and notes) is preserved by
the same explicit-user edit the single-term command uses.
"""

import hashlib
from dataclasses import dataclass

from book_translator.models import (
    SeriesPolicyAmendmentRequest,
    SeriesProvenanceKind,
    SeriesTerm,
    SeriesTermChange,
    SeriesTerminologyPolicy,
    SeriesTermProvenance,
    SeriesTermStatus,
)
from book_translator.series_terminology.identity import canonical_bytes
from book_translator.series_terminology.policy import (
    SeriesTerminologyError,
    find_term,
    set_explicit_term,
)

SERIES_POLICY_AMENDMENT_VERSION = "1.1.0"
AMENDMENT_ORIGIN = "amend-series-terminology"


class SeriesPolicyAmendmentConflict(SeriesTerminologyError):
    """Raised, before anything is applied, when an amendment would override a user decision."""


@dataclass(frozen=True, slots=True)
class AmendedPolicy:
    policy: SeriesTerminologyPolicy
    changes: tuple[SeriesTermChange, ...]


def _version_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def request_sha256(request: SeriesPolicyAmendmentRequest) -> str:
    return hashlib.sha256(canonical_bytes(request.model_dump(mode="json"))).hexdigest()


def amendment_identity(policy_sha256_before: str, request: SeriesPolicyAmendmentRequest) -> str:
    """Address one amendment by the exact policy it applies to and what it asks for."""
    payload = {
        "amendment_engine_version": SERIES_POLICY_AMENDMENT_VERSION,
        "policy_sha256_before": policy_sha256_before,
        "request_sha256": request_sha256(request),
    }
    return f"spa-{hashlib.sha256(canonical_bytes(payload)).hexdigest()[:24]}"


def _user_owned(term: SeriesTerm) -> bool:
    return term.provenance.kind is SeriesProvenanceKind.EXPLICIT_USER and term.status in {
        SeriesTermStatus.LOCKED,
        SeriesTermStatus.STRONG_REFERENCE,
    }


def amendment_conflicts(
    policy: SeriesTerminologyPolicy, request: SeriesPolicyAmendmentRequest
) -> tuple[str, ...]:
    """Every reason the amendment may not be applied, in request order; empty when it may."""
    conflicts: list[str] = []
    if request.series_id != policy.series_id:
        conflicts.append(
            f"amendment names series {request.series_id!r}, policy is {policy.series_id!r}"
        )
    if _version_tuple(request.resulting_policy_version) <= _version_tuple(policy.version):
        conflicts.append(
            f"resulting policy version {request.resulting_policy_version} is not newer than "
            f"{policy.version}"
        )
    amended_sources = {item.source_term for item in request.terms}
    for retirement in request.retired_terms:
        name = retirement.source_term
        if name in amended_sources:
            conflicts.append(f"{name!r}: both amended and retired by one amendment")
        retiring = find_term(policy, name)
        if retiring is None:
            conflicts.append(f"{name!r}: no such series term; amendments never add one")
            continue
        if _user_owned(retiring):
            conflicts.append(f"{name!r}: retires a user decision")
        if retiring.status is SeriesTermStatus.OBSERVED_VARIANTS:
            conflicts.append(f"{name!r}: holds no decision to retire")
        if not retiring.variants:
            conflicts.append(f"{name!r}: retirement keeps a term's evidence, and it has none")
    amended_ids: set[str] = set()
    for item in request.terms:
        existing = find_term(policy, item.source_term)
        if existing is None:
            conflicts.append(f"{item.source_term!r}: no such series term; amendments never add one")
            continue
        amended_ids.add(existing.concept_id)
        if not _user_owned(existing):
            continue
        if existing.target_term != item.target_term:
            conflicts.append(f"{item.source_term!r}: changes the target of a user decision")
        if existing.status is not item.status:
            conflicts.append(f"{item.source_term!r}: changes the force of a user decision")
        if set(existing.allowed_target_forms) - set(item.allowed_target_forms):
            conflicts.append(f"{item.source_term!r}: withdraws a form a user decision authorized")
        if set(existing.aliases) - set(item.aliases):
            conflicts.append(f"{item.source_term!r}: withdraws an alias of a user decision")
    owners = {
        spelling: term
        for term in policy.terms
        if _user_owned(term) and term.concept_id not in amended_ids
        for spelling in (term.source_term, *term.aliases)
    }
    for item in request.terms:
        for alias in item.aliases:
            owner = owners.get(alias)
            if owner is not None:
                conflicts.append(
                    f"{item.source_term!r}: alias {alias!r} is claimed by the user decision "
                    f"{owner.source_term!r}"
                )
    return tuple(conflicts)


def _retire_term(
    policy: SeriesTerminologyPolicy, term: SeriesTerm, rationale: str, amendment_id: str
) -> tuple[SeriesTerminologyPolicy, SeriesTerm]:
    """Withdraw a decision to evidence, keeping the concept, its spelling, and every variant.

    The result is an ordinary ``OBSERVED_VARIANTS`` record, which is the contract's own way of
    saying "the corpora attested this and nobody decided it". The decision that was withdrawn is
    written into the notes so the policy still says what it used to claim, and the amendment id in
    the provenance origin says who withdrew it and under which record.
    """
    retired = SeriesTerm(
        concept_id=term.concept_id,
        source_term=term.source_term,
        aliases=list(term.aliases),
        classification=term.classification,
        status=SeriesTermStatus.OBSERVED_VARIANTS,
        target_term=None,
        allowed_target_forms=[],
        provenance=SeriesTermProvenance(
            kind=SeriesProvenanceKind.UNRESOLVED,
            origin=f"{AMENDMENT_ORIGIN}:{amendment_id}",
            rationale=rationale,
        ),
        variants=list(term.variants),
        conflicts=list(term.conflicts),
        notes=[
            *term.notes,
            f"retired {term.status.value} ({term.provenance.kind.value}) target "
            f"{term.target_term!r} to evidence by amendment {amendment_id}",
        ],
    )
    remaining = [item for item in policy.terms if item.concept_id != term.concept_id]
    return policy.model_copy(update={"terms": [*remaining, retired]}), retired


def _change(before: SeriesTerm, after: SeriesTerm) -> SeriesTermChange:
    return SeriesTermChange(
        concept_id=after.concept_id,
        source_term=after.source_term,
        status_before=before.status,
        status_after=after.status,
        target_before=before.target_term,
        target_after=after.target_term or "",
        provenance_before=before.provenance.kind,
        provenance_after=after.provenance.kind,
        aliases_added=sorted(set(after.aliases) - set(before.aliases)),
        aliases_removed=sorted(set(before.aliases) - set(after.aliases)),
        forms_added=sorted(set(after.allowed_target_forms) - set(before.allowed_target_forms)),
        forms_removed=sorted(set(before.allowed_target_forms) - set(after.allowed_target_forms)),
    )


def apply_amendment(
    policy: SeriesTerminologyPolicy,
    request: SeriesPolicyAmendmentRequest,
    *,
    amendment_id: str,
) -> AmendedPolicy:
    """Apply every item or none; the result is revalidated as a whole policy."""
    conflicts = amendment_conflicts(policy, request)
    if conflicts:
        raise SeriesPolicyAmendmentConflict(
            "amendment refused, nothing applied: " + "; ".join(conflicts)
        )
    amended = policy
    changes: list[SeriesTermChange] = []
    for retirement in request.retired_terms:
        retiring = find_term(amended, retirement.source_term)
        assert retiring is not None
        amended, retired = _retire_term(amended, retiring, retirement.rationale, amendment_id)
        changes.append(_change(retiring, retired))
    for item in request.terms:
        before = find_term(amended, item.source_term)
        assert before is not None
        amended = set_explicit_term(
            amended,
            source_term=before.source_term,
            target_term=item.target_term,
            classification=before.classification,
            status=item.status,
            rationale=item.rationale,
            aliases=tuple(item.aliases),
            allowed_target_forms=tuple(item.allowed_target_forms),
            origin=f"{AMENDMENT_ORIGIN}:{amendment_id}",
        )
        after = find_term(amended, item.source_term)
        assert after is not None
        changes.append(_change(before, after))
    revalidated = SeriesTerminologyPolicy.model_validate(
        amended.model_copy(update={"version": request.resulting_policy_version}).model_dump(
            mode="json"
        )
    )
    return AmendedPolicy(revalidated, tuple(changes))
