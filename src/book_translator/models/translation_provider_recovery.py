"""Durable provenance for bounded recovery of provider-failed translation jobs.

A failed attempt records what the provider did and how the executor classified it *at that
moment*. That classification is history and stays byte-identical forever: an attempt marked
``retryable: false`` was not retryable when it was made, and no later event changes that.

What a later event *can* change is whether a **new** attempt is justified. Provider health is a
property of the provider, not of the historical attempt, so re-established health is new evidence
supporting a new decision rather than a reinterpretation of the old one. This contract is that new
decision: the failure class the historical attempt fell into, the fresh preflight that re-proved
the provider, the bounded budget the authorization was drawn against, and the attempt slot it
authorizes.

Only an authorization is persisted, and it is written *before* the recovered attempt is executed,
so an interrupted recovery still explains the attempt slot it consumed. What the recovered attempt
then did belongs to the attempt artifact and its acceptance sidecar; duplicating it here is how
evidence starts disagreeing with itself.
"""

import re
from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel
from book_translator.models.provider_execution import (
    ProviderErrorCategory,
    ProviderName,
    TranslationExecutionKind,
)
from book_translator.models.translation_worker import TranslationAcceptanceOrigin


class ProviderFailureClass(StrEnum):
    """Provider-neutral family a historical failure belongs to.

    The executor's ``ProviderErrorCategory`` is deliberately fine-grained and partly adapter
    shaped. Recovery reasons about families instead, because the question it answers — may a new
    attempt be spent, and what has to be re-proved first — has the same answer for every category
    inside a family and must keep having it when a new adapter adds categories.
    """

    #: Nothing durable was wrong: a pre-send timeout, a dropped connection, a provider 5xx.
    TRANSIENT = "transient"
    #: The provider refused because of usage pacing, not because the request was wrong.
    RATE_LIMIT = "rate_limit"
    #: The credential or session was rejected; recovery requires re-proving the login.
    AUTHENTICATION = "authentication"
    #: The provider implementation itself was missing or unusable locally.
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    #: The provider answered, but the answer did not satisfy the output contract.
    MALFORMED_RESPONSE = "malformed_response"
    #: The request, the package, or the claim violated a contract the provider enforces.
    CONTRACT_VIOLATION = "contract_violation"
    #: The provider declined the content itself.
    CONTENT_POLICY = "content_policy"
    #: The failure was local — disk, interruption — and is not a statement about the provider.
    LOCAL_ENVIRONMENT = "local_environment"
    #: It is not known whether the request reached the provider.
    AMBIGUOUS_EXECUTION = "ambiguous_execution"
    #: The failure was never classified; nothing may be inferred from it.
    UNKNOWN = "unknown"


class ProviderRecoveryVerdict(StrEnum):
    """The recovery policy's complete answer for one provider-failed job."""

    #: Health is established, the budget allows it, and the cooldown has elapsed.
    RECOVERABLE_NOW = "recoverable_now"
    #: Recoverable in principle, but the failure is too recent to replay yet.
    WAIT_FOR_BACKOFF = "wait_for_backoff"
    #: Undecidable until a fresh provider preflight supplies current health evidence.
    PREFLIGHT_REQUIRED = "preflight_required"
    #: A person must decide; automatic recovery is refused.
    OPERATOR_REQUIRED = "operator_required"
    #: Replaying could duplicate work that may already have reached the provider.
    UNSAFE_TO_REPLAY = "unsafe_to_replay"
    #: The recovery budget or the job's provider-attempt budget is spent.
    EXHAUSTED = "exhausted"


class ProviderRecoveryInitiator(StrEnum):
    """Who decided that a recovery attempt should be spent."""

    AUTOMATIC = "automatic"
    OPERATOR = "operator"


class ProviderReplaySafety(StrEnum):
    """Why replaying the failed attempt slot cannot duplicate or overwrite anything."""

    #: The attempt failed conclusively: not ambiguous, no stored output, no outstanding reservation.
    FAILURE_IS_CONCLUSIVE = "failure_is_conclusive"
    #: An explicit operator reservation resolution settled what the slot did.
    RESERVATION_RESOLVED = "reservation_resolved"
    #: Replay safety could not be established; never persisted.
    UNSAFE = "unsafe"


# Structural preflight evidence only. A value is a short token such as `yes`, `claude.ai`, `max`,
# or `2.1.235`; the long opaque run a credential would produce cannot satisfy both bounds.
_PREFLIGHT_VALUE = re.compile(r"^[A-Za-z0-9 ._:/+-]{1,64}$")
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9_-]{25,}")
_SECRET_MARKERS = ("sk-", "bearer ", "authorization:", "password")


def _reject_secret_material(label: str, value: str | None) -> str | None:
    """Return a structural preflight value, refusing anything that could carry a credential."""
    if value is None:
        return None
    if not _PREFLIGHT_VALUE.fullmatch(value) or _OPAQUE_RUN.search(value):
        raise ValueError(f"preflight {label} must be a short structural value")
    if any(marker in value.lower() for marker in _SECRET_MARKERS):
        raise ValueError(f"preflight {label} must not carry credential material")
    return value


class ProviderPreflightDetail(ContractModel):
    """One structural name/value pair reported by a provider preflight."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    value: str

    @model_validator(mode="after")
    def value_is_structural(self) -> "ProviderPreflightDetail":
        _reject_secret_material(self.name, self.value)
        return self


class ProviderPreflightRecord(ContractModel):
    """Durable evidence of one provider preflight, carrying no credential material.

    The preflight itself establishes local readiness without a model call, so this record proves
    the provider was reachable and logged in — never *how* it was logged in.
    """

    provider: ProviderName
    provider_preflight_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    provider_implementation: str = Field(min_length=1)
    performed_at: AwareDatetime
    supported: bool
    passed: bool
    authenticated: bool
    auth_method: str | None = None
    executable: str | None = None
    executable_version: str | None = None
    api_key_used: bool = False
    failure_category: ProviderErrorCategory | None = None
    failure_message: str | None = None
    details: list[ProviderPreflightDetail] = Field(default_factory=list)

    @model_validator(mode="after")
    def evidence_is_consistent_and_secret_free(self) -> "ProviderPreflightRecord":
        if self.api_key_used:
            raise ValueError("recovery preflight evidence must never record an API key path")
        for label, value in (
            ("auth_method", self.auth_method),
            ("executable", self.executable),
            ("executable_version", self.executable_version),
            ("provider_implementation", self.provider_implementation),
        ):
            _reject_secret_material(label, value)
        if self.passed:
            if self.failure_category is not None or self.failure_message is not None:
                raise ValueError("a passing preflight must not record a failure")
        elif self.failure_category is None:
            raise ValueError("a failing preflight must record why it failed")
        if not self.passed and self.authenticated:
            raise ValueError("a failing preflight cannot establish authentication")
        return self


class TranslationRepairRecoveryLineage(ContractModel):
    """Proof that a recovered repair reproduces the exact repair the provider failed to deliver.

    A translation recovery only has to identify the job; the frozen chunk and the rendered prompt
    package already define what will be sent. A repair has no package: its request is *derived*
    from the rejected draft and the QA findings that rejected it, so recovering one is only safe
    if that derivation can be shown to land on the same bytes it landed on before. This block is
    that showing, and every field in it is a hash or an integer — the derived request itself
    contains book prose and is never recorded here.

    The lineage deliberately points at the **repair root** — the QA-rejected translation attempt
    the repair answers — and not at the failed repair attempt. A failed repair produced no
    translation, so nothing can be repaired *from* it; treating the numerically previous attempt
    as the repair target would silently redefine what the provider is being asked to fix.
    """

    #: Always `repair`: a recovery preserves the kind of request it recovers, and this field is
    #: also the resulting attempt's `execution_kind`.
    recovered_execution_kind: TranslationExecutionKind
    #: The QA-rejected translation attempt every repair in this chain answers, and therefore the
    #: resulting attempt's `repaired_from_attempt`.
    repair_root_attempt: int = Field(ge=1)
    #: The repair attempt whose provider call failed. It is history, not a repair target.
    failed_repair_attempt: int = Field(ge=2)
    #: The auto-repair authorization that spent the QA repair decision being replayed. Absent
    #: when an operator drove the failed repair, because an operator decision needs no policy
    #: record.
    repair_decision_file: Path | None = None
    repair_decision_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    #: The effective mechanical rejection: which QA evidence, and which findings, caused the
    #: repair that is being recovered.
    rejection_origin: TranslationAcceptanceOrigin
    rejection_evidence_file: Path
    rejection_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    triggering_codes: list[str] = Field(min_length=1)
    #: The rejected draft itself.
    rejected_attempt_file: Path
    rejected_attempt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rejected_translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: Frozen inputs the repair request is derived from.
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    style_guide_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    #: The repair request identity, as rebuilt now and as the failed attempt recorded it.
    repair_prompt_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    output_contract: str = Field(min_length=1)
    repair_request_system_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_request_user_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_request_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_reproduced_exactly: bool
    #: The QA repair *decisions* charged to this job, which a recovery deliberately does not add
    #: to: QA chose the repair action once and the provider merely failed to carry it out.
    repair_decisions_used: int = Field(ge=1)
    max_repair_attempts: int = Field(ge=1)

    @model_validator(mode="after")
    def replay_is_proven(self) -> "TranslationRepairRecoveryLineage":
        if self.recovered_execution_kind is not TranslationExecutionKind.REPAIR:
            raise ValueError("repair recovery lineage describes a repair, never a translation")
        if self.failed_repair_attempt <= self.repair_root_attempt:
            raise ValueError("a failed repair must follow the attempt it repaired")
        if not self.request_reproduced_exactly:
            raise ValueError("an authorized repair recovery requires an exactly reproduced request")
        if (self.repair_decision_file is None) != (self.repair_decision_sha256 is None):
            raise ValueError("a referenced repair decision requires its hash")
        return self


class TranslationProviderRecoveryDecision(ContractModel):
    """Immutable authorization for exactly one bounded provider-recovery attempt.

    It is neither a transport retry, a queue retry, nor a QA repair: it authorizes re-entering a
    job that a provider failure ended, on the strength of provider health established *after*
    that failure.
    """

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    provider_recovery_engine_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    provider_recovery_policy_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    run_id: str = Field(pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    job_id: str
    initiator: ProviderRecoveryInitiator
    verdict: ProviderRecoveryVerdict
    provider: ProviderName
    failure_class: ProviderFailureClass
    failed_attempt_number: int = Field(ge=1)
    failed_attempt_id: str
    failed_attempt_file: Path
    failed_attempt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    failed_at: AwareDatetime
    historical_error_category: ProviderErrorCategory | None = None
    historical_retryable: bool
    historical_ambiguous: bool
    replay_safety: ProviderReplaySafety
    #: What kind of request the recovered attempt will carry. A recovery never changes the kind
    #: of work it recovers, so this is both the failed attempt's kind and the resulting one's.
    recovered_execution_kind: TranslationExecutionKind = TranslationExecutionKind.TRANSLATION
    #: Present exactly when a failed *repair* is being recovered.
    repair: TranslationRepairRecoveryLineage | None = None
    provider_recovery_attempts_used: int = Field(ge=0)
    max_provider_recovery_attempts: int = Field(ge=1)
    remaining_provider_recovery_attempts: int = Field(ge=1)
    provider_attempts_used: int = Field(ge=1)
    max_provider_attempts: int = Field(ge=1)
    preflight_required: bool
    preflight: ProviderPreflightRecord | None = None
    authorized_attempt_number: int = Field(ge=2)
    retry_eligible_at: AwareDatetime
    created_at: AwareDatetime

    @model_validator(mode="after")
    def authorization_is_self_evident(self) -> "TranslationProviderRecoveryDecision":
        if self.verdict is not ProviderRecoveryVerdict.RECOVERABLE_NOW:
            raise ValueError("only an authorized recovery decision is persisted")
        if self.failure_class in {
            ProviderFailureClass.AMBIGUOUS_EXECUTION,
            ProviderFailureClass.UNKNOWN,
        }:
            raise ValueError("an unsafe or unclassified failure is never automatically recovered")
        if self.historical_ambiguous:
            raise ValueError("an ambiguous attempt is resolved, never recovered")
        if self.replay_safety is ProviderReplaySafety.UNSAFE:
            raise ValueError("an authorized recovery requires established replay safety")
        if self.authorized_attempt_number <= self.failed_attempt_number:
            raise ValueError("a recovery attempt must follow the attempt it recovers")
        if self.provider_recovery_attempts_used >= self.max_provider_recovery_attempts:
            raise ValueError("an authorized recovery requires remaining recovery budget")
        if self.provider_attempts_used >= self.max_provider_attempts:
            raise ValueError("an authorized recovery requires remaining provider budget")
        if self.preflight_required and self.preflight is None:
            raise ValueError("this failure class requires recorded preflight evidence")
        if self.preflight is not None:
            if not self.preflight.passed:
                raise ValueError("an authorized recovery requires a passing preflight")
            if self.preflight.provider is not self.provider:
                raise ValueError("preflight evidence must describe the recovered provider")
            if self.preflight.performed_at > self.created_at:
                raise ValueError("preflight evidence must precede the decision it supports")
            if (
                self.failure_class is ProviderFailureClass.AUTHENTICATION
                and not self.preflight.authenticated
            ):
                raise ValueError("recovering an authentication failure requires proven login")
        if self.retry_eligible_at < self.failed_at:
            raise ValueError("a recovery attempt cannot become eligible before the failure")
        is_repair = self.recovered_execution_kind is TranslationExecutionKind.REPAIR
        if is_repair != (self.repair is not None):
            raise ValueError("repair lineage is recorded exactly when a repair is recovered")
        if self.repair is not None:
            if self.repair.failed_repair_attempt != self.failed_attempt_number:
                raise ValueError("repair lineage must describe the failed attempt it recovers")
            if self.repair.recovered_execution_kind is not self.recovered_execution_kind:
                raise ValueError("repair lineage must agree with the recovered execution kind")
        return self
