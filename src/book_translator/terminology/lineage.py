"""Pure explanation of the live terminology registry from its governed writers.

An immutable lineage record — a book identity migration, a frozen resource, a snapshot — pins the
registry hash that was true when it was written, and later governed edits legitimately move the live
registry away from it. Auditing that drift by comparing the two hashes proves nothing except that
time passed. What must hold instead is that every step away from the recorded state is itself
recorded, which is what this module explains.

More than one governed writer may move the registry. A decision correction restates a single
canonical decision; an applied remediation plan retires, retargets, or relaxes many at once. Both
publish the semantic identity they moved the registry *from* and *to*, so both are the same kind of
evidence and belong in one chain. Modelling only one of them was the original defect here: it made a
legitimately remediated registry look ungoverned.
"""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from book_translator.models import TerminologyDecisionCorrection, TerminologyRemediationPlan


class RegistryLineageError(ValueError):
    """Raised when a governed record cannot be read as a registry transition."""


class RegistryEventKind(StrEnum):
    """Which governed writer produced one registry transition."""

    DECISION_CORRECTION = "decision_correction"
    REMEDIATION_APPLICATION = "remediation_application"


@dataclass(frozen=True, slots=True)
class RegistryTransition:
    """One governed step between two semantic registry identities."""

    event_id: str
    event_kind: RegistryEventKind
    created_at: datetime
    semantic_sha256_before: str
    semantic_sha256_after: str


@dataclass(frozen=True, slots=True)
class RegistryLineage:
    """Whether the live registry identity is the head of the governed transition chain."""

    considered_event_ids: tuple[str, ...]
    head_event_id: str | None
    head_event_kind: RegistryEventKind | None
    head_semantic_sha256: str | None
    current_semantic_sha256: str
    head_matches_current: bool
    contiguous: bool
    explained: bool


def correction_transition(correction: TerminologyDecisionCorrection) -> RegistryTransition:
    return RegistryTransition(
        event_id=correction.correction_id,
        event_kind=RegistryEventKind.DECISION_CORRECTION,
        created_at=correction.created_at,
        semantic_sha256_before=correction.registry_sha256_before,
        semantic_sha256_after=correction.registry_sha256_after,
    )


def remediation_transition(plan: TerminologyRemediationPlan) -> RegistryTransition:
    """Read an *applied* plan as a transition.

    A plan's expected identity is only a claim until the plan is applied; `execute_remediation_plan`
    refuses to publish unless the applied registry actually hashes to it, so a published plan is
    evidence of the move rather than a proposal for one.
    """
    if plan.expected_semantic_sha256 is None:
        raise RegistryLineageError(
            f"remediation plan {plan.plan_id} projects no resulting registry identity"
        )
    return RegistryTransition(
        event_id=plan.plan_id,
        event_kind=RegistryEventKind.REMEDIATION_APPLICATION,
        created_at=plan.created_at,
        semantic_sha256_before=plan.source_registry_semantic_sha256,
        semantic_sha256_after=plan.expected_semantic_sha256,
    )


def registry_transitions(
    *,
    corrections: Iterable[TerminologyDecisionCorrection] = (),
    remediations: Iterable[TerminologyRemediationPlan] = (),
) -> tuple[RegistryTransition, ...]:
    """Merge every governed writer's records into one chronologically ordered chain."""
    transitions = [correction_transition(item) for item in corrections]
    transitions.extend(remediation_transition(item) for item in remediations)
    return tuple(sorted(transitions, key=lambda item: (item.created_at, item.event_id)))


def explain_registry_lineage(
    transitions: Iterable[RegistryTransition],
    *,
    current_semantic_sha256: str,
    since: datetime | None = None,
) -> RegistryLineage:
    """Explain the terminology registry's current identity from its governed transitions.

    *since* restricts the window to transitions published after some earlier lineage record, which
    is what makes the answer meaningful: an event older than that record is already accounted for by
    the record itself. ``contiguous`` is reported separately from ``explained`` because a window may
    legitimately have gaps — another governed writer, such as the identity migration itself, may sit
    between two events — while the head must still be the live registry.
    """
    window = tuple(
        sorted(
            (item for item in transitions if since is None or item.created_at > since),
            key=lambda item: (item.created_at, item.event_id),
        )
    )
    head = window[-1] if window else None
    matches = head is not None and head.semantic_sha256_after == current_semantic_sha256
    contiguous = all(
        earlier.semantic_sha256_after == later.semantic_sha256_before
        for earlier, later in zip(window, window[1:], strict=False)
    )
    return RegistryLineage(
        considered_event_ids=tuple(item.event_id for item in window),
        head_event_id=head.event_id if head is not None else None,
        head_event_kind=head.event_kind if head is not None else None,
        head_semantic_sha256=head.semantic_sha256_after if head is not None else None,
        current_semantic_sha256=current_semantic_sha256,
        head_matches_current=matches,
        contiguous=contiguous,
        explained=matches,
    )


@dataclass(frozen=True, slots=True)
class RegistryDrift:
    """Whether one recorded registry identity reaches another only through governed steps."""

    from_semantic_sha256: str
    to_semantic_sha256: str
    path: tuple[RegistryTransition, ...]
    explained: bool


def explain_registry_drift(
    transitions: Iterable[RegistryTransition],
    *,
    from_semantic_sha256: str,
    to_semantic_sha256: str,
) -> RegistryDrift:
    """Explain the move between two registry identities as a chain of governed transitions.

    This answers a different question from :func:`explain_registry_lineage`, which asks whether the
    *live* registry is the head of a time window. Here both endpoints are already fixed — typically
    two frozen registries pinned by two snapshots — so a time window is the wrong instrument: the
    honest proof is a contiguous path whose every step is a published governed record. An
    unreachable endpoint means some edit was never recorded, which is exactly the case that must
    not be waived.

    Search is breadth-first over transitions ordered by ``(created_at, event_id)``, so the shortest
    governed explanation is returned and the result is deterministic.
    """
    ordered = sorted(transitions, key=lambda item: (item.created_at, item.event_id))
    if from_semantic_sha256 == to_semantic_sha256:
        return RegistryDrift(from_semantic_sha256, to_semantic_sha256, (), True)
    outgoing: dict[str, list[RegistryTransition]] = {}
    for transition in ordered:
        outgoing.setdefault(transition.semantic_sha256_before, []).append(transition)
    queue: deque[tuple[str, tuple[RegistryTransition, ...]]] = deque([(from_semantic_sha256, ())])
    reached = {from_semantic_sha256}
    while queue:
        identity, path = queue.popleft()
        for transition in outgoing.get(identity, ()):
            successor = transition.semantic_sha256_after
            if successor in reached:
                continue
            extended = (*path, transition)
            if successor == to_semantic_sha256:
                return RegistryDrift(from_semantic_sha256, to_semantic_sha256, extended, True)
            reached.add(successor)
            queue.append((successor, extended))
    return RegistryDrift(from_semantic_sha256, to_semantic_sha256, (), False)
