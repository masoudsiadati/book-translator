"""Pure policy deciding whether a provider-failed translation job may be re-executed.

An unattended run has to answer a question the executor deliberately does not: a provider failure
ended one attempt, so is a *new* attempt justified now? The executor's answer — the ``retryable``
flag on the immutable attempt — is a statement about that moment, made with only that moment's
evidence, and it stays true forever. An expired login was genuinely not retryable while it was
expired. It says nothing about a provider that has since been re-authenticated.

This module is where the later question is answered, and it is deliberately separate from four
neighbouring mechanisms it must never absorb:

* **Provider transport retry** happens inside one attempt, before it is published.
* **Queue retry** (``retry-translation-job``) reschedules a failure the executor already marked
  retryable, spends no separate budget, and is not a judgement about provider health.
* **QA auto-repair** acts on a provider *success* that mechanical QA rejected. It decides that a
  repair *action* is warranted; recovering a repair whose provider call failed replays that same
  action and is deliberately not a second one.
* **Operator retry** is a person deciding, and needs no policy at all.

The rule is a conjunction and every part fails closed:

* An ambiguous attempt is never replayed. Not knowing whether a request reached the provider is
  precisely the case the reservation and resolution boundary exists for, and guessing there can
  duplicate paid work or a published translation.
* Only failure families that are *about the provider's availability* are recoverable. A malformed
  response, a contract violation, a content refusal, a local fault, or an unclassified failure is
  ``operator_required`` however healthy the provider now looks: re-sending an identical request
  that the provider already answered badly is not recovery, it is repetition.
* Authentication and provider-unavailability are recoverable only against *fresh* evidence. The
  policy cannot obtain that evidence itself — it performs no I/O — so it returns
  ``preflight_required`` and refuses to conclude anything until the caller supplies a preflight
  result taken after the failure.
* The budget is the smaller of the recovery allowance and the job's remaining provider attempts,
  so this policy can only ever be more conservative than the queue's own accounting.
* A failure is not replayed before the run's own backoff has elapsed since it happened, and a
  rate-limited provider is given a longer rest than a dropped connection.

The same rule governs a failed *repair*, because the question is identical: a provider failed to
carry out a request, so may an unattended process send that request again? Only the request differs,
and a repair request is not frozen anywhere — it is derived from the rejected draft and the findings
that rejected it. So a repair recovery carries one extra conjunct: the caller must have re-derived
that request and proven it byte-identical to the one that failed. Recovering a repair whose request
cannot be reproduced would send a *different* repair under an authorization for the original, which
is the case this policy refuses hardest.

Nothing here performs I/O, reads a workspace, or contacts a provider; the caller supplies the
classified failure, the budgets, the failure time, the preflight evidence, and — for a repair — the
proof that the exact failed request was reconstructed.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from book_translator.models import (
    ProviderErrorCategory,
    ProviderFailureClass,
    ProviderRecoveryVerdict,
    TranslationExecutionKind,
)

PROVIDER_RECOVERY_POLICY_VERSION = "1.0.0"

#: Default recovery allowance: how many times an unattended process may re-enter one job that a
#: provider failure ended. It is deliberately small — recovery exists so a healthy provider is not
#: blocked by an old classification, not so a job can be retried indefinitely.
DEFAULT_MAX_PROVIDER_RECOVERY_ATTEMPTS = 2

# Every executor category maps to exactly one family. Categories are matched exhaustively rather
# than by prefix so a newly added category is a type error here instead of a silent `unknown`.
_FAILURE_CLASSES: dict[ProviderErrorCategory, ProviderFailureClass] = {
    ProviderErrorCategory.AUTHENTICATION: ProviderFailureClass.AUTHENTICATION,
    ProviderErrorCategory.AUTHORIZATION: ProviderFailureClass.AUTHENTICATION,
    ProviderErrorCategory.RATE_LIMIT: ProviderFailureClass.RATE_LIMIT,
    ProviderErrorCategory.TIMEOUT_PRE_SEND: ProviderFailureClass.TRANSIENT,
    ProviderErrorCategory.CONNECTION_PRE_SEND: ProviderFailureClass.TRANSIENT,
    ProviderErrorCategory.SERVER_ERROR: ProviderFailureClass.TRANSIENT,
    ProviderErrorCategory.TIMEOUT_AMBIGUOUS: ProviderFailureClass.AMBIGUOUS_EXECUTION,
    ProviderErrorCategory.CONNECTION_AMBIGUOUS: ProviderFailureClass.AMBIGUOUS_EXECUTION,
    ProviderErrorCategory.TIMEOUT: ProviderFailureClass.AMBIGUOUS_EXECUTION,
    ProviderErrorCategory.INTERRUPTED: ProviderFailureClass.AMBIGUOUS_EXECUTION,
    ProviderErrorCategory.LEASE_INVALID: ProviderFailureClass.AMBIGUOUS_EXECUTION,
    ProviderErrorCategory.INVALID_REQUEST: ProviderFailureClass.CONTRACT_VIOLATION,
    ProviderErrorCategory.UNSUPPORTED_MODEL_OR_PARAMETER: ProviderFailureClass.CONTRACT_VIOLATION,
    ProviderErrorCategory.CONTEXT_LENGTH: ProviderFailureClass.CONTRACT_VIOLATION,
    ProviderErrorCategory.PACKAGE_INVALID: ProviderFailureClass.CONTRACT_VIOLATION,
    ProviderErrorCategory.CLAIM_INVALID: ProviderFailureClass.CONTRACT_VIOLATION,
    ProviderErrorCategory.CONTENT_POLICY: ProviderFailureClass.CONTENT_POLICY,
    ProviderErrorCategory.MALFORMED_RESPONSE: ProviderFailureClass.MALFORMED_RESPONSE,
    ProviderErrorCategory.EMPTY_RESPONSE: ProviderFailureClass.MALFORMED_RESPONSE,
    ProviderErrorCategory.EMPTY_OUTPUT: ProviderFailureClass.MALFORMED_RESPONSE,
    ProviderErrorCategory.OUTPUT_TRUNCATED: ProviderFailureClass.MALFORMED_RESPONSE,
    ProviderErrorCategory.EXECUTABLE_NOT_FOUND: ProviderFailureClass.PROVIDER_UNAVAILABLE,
    ProviderErrorCategory.INVOCATION_ERROR: ProviderFailureClass.PROVIDER_UNAVAILABLE,
    ProviderErrorCategory.LOCAL_IO: ProviderFailureClass.LOCAL_ENVIRONMENT,
    # A bare nonzero exit is an undiagnosed failure wearing a specific-looking name. It is exactly
    # the case that must fail closed, so it is `unknown` rather than a guess at a family.
    ProviderErrorCategory.NONZERO_EXIT: ProviderFailureClass.UNKNOWN,
    ProviderErrorCategory.UNKNOWN: ProviderFailureClass.UNKNOWN,
}

#: Families whose recovery needs no provider re-evidence: nothing about them claims the provider
#: became unusable, only that this exchange did not complete.
_RECOVERABLE_WITHOUT_PREFLIGHT = frozenset(
    {ProviderFailureClass.TRANSIENT, ProviderFailureClass.RATE_LIMIT}
)

#: Families that assert the provider itself was unusable. Recovery is exactly the claim that this
#: is no longer true, so it must be demonstrated rather than assumed.
_PREFLIGHT_REQUIRED = frozenset(
    {ProviderFailureClass.AUTHENTICATION, ProviderFailureClass.PROVIDER_UNAVAILABLE}
)

#: How many multiples of the run's retry backoff must pass before a family may be replayed.
#:
#: The cooldown exists for families whose recovery rests on *time* having passed, because nothing
#: else can tell us the condition cleared: a rate limit is the extreme case, where replaying too
#: soon reproduces the failure by definition. Families that require a preflight are gated by proof
#: instead of by a timer, and a timer would add nothing to it — a login that a preflight has just
#: verified is not more verified sixty seconds later. Their bound is the recovery budget, and a
#: provider that passes preflight while still failing every call exhausts it in two attempts.
_COOLDOWN_MULTIPLIERS: dict[ProviderFailureClass, int] = {ProviderFailureClass.RATE_LIMIT: 5}


def classify_provider_failure(
    category: ProviderErrorCategory | None, *, ambiguous: bool
) -> ProviderFailureClass:
    """Place one historical failure in a recovery family, failing closed on anything unknown.

    A recorded ambiguity outranks the category: whatever the provider said, an attempt that may
    have been sent is an ambiguous execution and belongs to the resolution boundary.
    """
    if ambiguous:
        return ProviderFailureClass.AMBIGUOUS_EXECUTION
    if category is None:
        return ProviderFailureClass.UNKNOWN
    return _FAILURE_CLASSES.get(category, ProviderFailureClass.UNKNOWN)


def cooldown_seconds(failure_class: ProviderFailureClass, retry_backoff_seconds: int) -> int:
    """Return how long after the failure a recovery attempt may be spent."""
    if failure_class in _PREFLIGHT_REQUIRED:
        return 0
    return max(0, retry_backoff_seconds) * _COOLDOWN_MULTIPLIERS.get(failure_class, 1)


@dataclass(frozen=True, slots=True)
class ProviderRecoveryBudget:
    """How many further provider-recovery attempts the policy and the queue jointly permit."""

    max_provider_recovery_attempts: int
    provider_recovery_attempts_used: int
    max_provider_attempts: int
    provider_attempts_used: int

    @property
    def policy_remaining(self) -> int:
        return max(0, self.max_provider_recovery_attempts - self.provider_recovery_attempts_used)

    @property
    def provider_remaining(self) -> int:
        return max(0, self.max_provider_attempts - self.provider_attempts_used)

    @property
    def remaining(self) -> int:
        return min(self.policy_remaining, self.provider_remaining)

    @property
    def limiting_bound(self) -> str:
        return (
            "provider_recovery_attempts"
            if self.policy_remaining <= self.provider_remaining
            else "attempts"
        )


@dataclass(frozen=True, slots=True)
class PreflightEvidence:
    """What a preflight taken after the failure established about the provider.

    ``performed`` distinguishes "no preflight was run" from "a preflight ran and failed"; only the
    first leaves the question open.
    """

    performed: bool
    passed: bool
    authenticated: bool


#: The absence of provider re-evidence, which is not the same as evidence of a failure.
NO_PREFLIGHT = PreflightEvidence(performed=False, passed=False, authenticated=False)


@dataclass(frozen=True, slots=True)
class RepairReplayEvidence:
    """What the caller proved about reproducing the exact repair request that failed.

    ``lineage_verified`` answers "is this really a failed repair of a QA rejection that still
    stands, with every referenced artifact intact"; ``request_reproduced_exactly`` answers "does
    re-deriving the repair request today land on the same bytes the failed attempt recorded".
    ``blocking_reasons`` names whatever failed, so a refusal explains itself rather than reporting
    a bare boolean.
    """

    lineage_verified: bool
    request_reproduced_exactly: bool
    blocking_reasons: tuple[str, ...] = ()

    @property
    def replayable(self) -> bool:
        return (
            self.lineage_verified and self.request_reproduced_exactly and not self.blocking_reasons
        )


@dataclass(frozen=True, slots=True)
class ProviderRecoveryAssessment:
    """The policy's complete, explainable answer for one provider-failed job."""

    policy_version: str
    verdict: ProviderRecoveryVerdict
    failure_class: ProviderFailureClass
    budget: ProviderRecoveryBudget
    preflight_required: bool
    eligible_at: datetime
    blocking_reasons: tuple[str, ...]
    #: What kind of request would be re-sent. A recovery never changes it.
    execution_kind: TranslationExecutionKind = TranslationExecutionKind.TRANSLATION

    @property
    def recoverable(self) -> bool:
        return self.verdict is ProviderRecoveryVerdict.RECOVERABLE_NOW


def assess_provider_recovery(
    *,
    failure_class: ProviderFailureClass,
    budget: ProviderRecoveryBudget,
    failed_at: datetime,
    retry_backoff_seconds: int,
    now: datetime,
    preflight: PreflightEvidence | None = None,
    execution_kind: TranslationExecutionKind = TranslationExecutionKind.TRANSLATION,
    repair: RepairReplayEvidence | None = None,
) -> ProviderRecoveryAssessment:
    """Decide whether one provider failure may be answered with a new automatic attempt.

    ``execution_kind`` names what would be re-sent. Recovering a repair requires ``repair``
    evidence, and refusing to decide without it is the point: a repair request has no frozen
    package behind it, so an assessment that did not see the reconstruction proof would be
    authorizing an unknown request.
    """
    if (execution_kind is TranslationExecutionKind.REPAIR) != (repair is not None):
        raise ValueError("repair replay evidence is required exactly for a repair recovery")
    evidence = preflight or NO_PREFLIGHT
    preflight_required = failure_class in _PREFLIGHT_REQUIRED
    eligible_at = failed_at + timedelta(
        seconds=cooldown_seconds(failure_class, retry_backoff_seconds)
    )

    def answer(verdict: ProviderRecoveryVerdict, *reasons: str) -> ProviderRecoveryAssessment:
        return ProviderRecoveryAssessment(
            PROVIDER_RECOVERY_POLICY_VERSION,
            verdict,
            failure_class,
            budget,
            preflight_required,
            eligible_at,
            reasons,
            execution_kind,
        )

    if failure_class is ProviderFailureClass.AMBIGUOUS_EXECUTION:
        return answer(
            ProviderRecoveryVerdict.UNSAFE_TO_REPLAY,
            "execution_may_have_reached_the_provider",
        )
    if repair is not None and not repair.replayable:
        # Checked before the failure family, because a request that cannot be reproduced is not
        # made safer by a benign failure class: the provider would receive something else.
        return answer(
            ProviderRecoveryVerdict.OPERATOR_REQUIRED,
            *(repair.blocking_reasons or ("repair_request_not_reproducible",)),
        )
    if failure_class not in _RECOVERABLE_WITHOUT_PREFLIGHT | _PREFLIGHT_REQUIRED:
        return answer(
            ProviderRecoveryVerdict.OPERATOR_REQUIRED,
            f"failure_class_not_automatically_recoverable:{failure_class.value}",
        )
    if budget.remaining < 1:
        return answer(ProviderRecoveryVerdict.EXHAUSTED, f"{budget.limiting_bound}_exhausted")
    if preflight_required and not evidence.performed:
        return answer(
            ProviderRecoveryVerdict.PREFLIGHT_REQUIRED,
            f"provider_health_unproven:{failure_class.value}",
        )
    if evidence.performed and not evidence.passed:
        return answer(ProviderRecoveryVerdict.OPERATOR_REQUIRED, "provider_preflight_failed")
    if (
        failure_class is ProviderFailureClass.AUTHENTICATION
        and evidence.performed
        and not evidence.authenticated
    ):
        return answer(ProviderRecoveryVerdict.OPERATOR_REQUIRED, "provider_not_authenticated")
    if now < eligible_at:
        return answer(ProviderRecoveryVerdict.WAIT_FOR_BACKOFF, "failure_cooldown_has_not_elapsed")
    return answer(ProviderRecoveryVerdict.RECOVERABLE_NOW)
