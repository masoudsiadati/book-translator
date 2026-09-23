"""Contracts for read-only verification of a published release.

Verification is the gate between assembly and publication. It produces a *judgement* and nothing
else: it reads a release directory, proves or fails to prove that the release is complete,
internally consistent, immutable since publication, and traceable to the run that produced it, and
writes only the report it was asked for.

The report is designed to be re-derivable. Everything in it except ``created_at`` is a pure
function of the bytes on disk, and ``content_sha256`` covers exactly that part — so two runs of the
verifier over an unchanged release produce the same content address, and a differing one is
evidence that something moved.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from book_translator.models.base import ContractModel


class ReleaseCheck(StrEnum):
    """The seven things a release has to prove before it may be published."""

    MANIFEST = "manifest"
    BOOK_INTEGRITY = "book_integrity"
    CHAPTERS = "chapters"
    TRANSLATIONS = "translations"
    TITLES = "titles"
    TERMINOLOGY = "terminology"
    PROVENANCE = "provenance"


#: The order checks are reported in. It is the order a reader needs them: the manifest is what
#: every later check is measured against, so a manifest failure explains the rest.
RELEASE_CHECK_ORDER: tuple[ReleaseCheck, ...] = (
    ReleaseCheck.MANIFEST,
    ReleaseCheck.BOOK_INTEGRITY,
    ReleaseCheck.CHAPTERS,
    ReleaseCheck.TRANSLATIONS,
    ReleaseCheck.TITLES,
    ReleaseCheck.TERMINOLOGY,
    ReleaseCheck.PROVENANCE,
)


class ReleaseCheckStatus(StrEnum):
    """The outcome of one check.

    ``SKIPPED`` is not a quiet pass. It means the evidence a check needs was not reachable — the
    canonical workspace is absent, which is the ordinary case when verifying a release that has
    been copied somewhere else. A skipped check is reported as skipped everywhere it appears, and
    a release with skipped checks is verified only as far as the bytes in front of the verifier
    allow.
    """

    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"


class ReleaseVerificationStatus(StrEnum):
    """Whether the release may be published."""

    READY_FOR_PUBLICATION = "READY_FOR_PUBLICATION"
    NOT_READY_FOR_PUBLICATION = "NOT_READY_FOR_PUBLICATION"


class ReleaseVerificationFinding(ContractModel):
    """One discrepancy. It names a check, a subject, and a code — never book prose."""

    check: ReleaseCheck
    code: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    severity: Literal["warning", "error"]
    subject: str | None = None
    message: str = Field(min_length=1)


class ReleaseVerificationCheck(ContractModel):
    """The outcome of one check with the evidence it actually examined."""

    check: ReleaseCheck
    status: ReleaseCheckStatus
    examined: int = Field(ge=0)
    expected: int = Field(ge=0)
    detail: str = Field(min_length=1)
    finding_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def outcome_matches_its_findings(self) -> "ReleaseVerificationCheck":
        if sorted(self.finding_codes) != self.finding_codes:
            raise ValueError("finding codes must be ordered")
        if len(set(self.finding_codes)) != len(self.finding_codes):
            raise ValueError("finding codes must be distinct")
        if self.status is ReleaseCheckStatus.PASS and self.examined != self.expected:
            raise ValueError("a passing check must have examined everything it expected")
        return self


class ReleaseVerificationReport(ContractModel):
    """The complete, re-derivable verdict on one release directory."""

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    release_verification_engine_version: str = Field(
        pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
    )
    release_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    release_path: Path
    release_assembly_engine_version: str | None = None
    source_translation_run: str | None = Field(default=None, pattern=r"^tr-[0-9a-f]{12}-\d{3,}$")
    snapshot_id: str | None = None
    snapshot_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    release_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    book_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    terminology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    chapter_title_artifact_id: str | None = Field(default=None, pattern=r"^ct-[0-9a-f]{24}$")
    chapter_title_artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    files_verified: int = Field(ge=0)
    files_expected: int = Field(ge=0)
    chapters_verified: int = Field(ge=0)
    chapters_expected: int = Field(ge=0)
    chunks_verified: int = Field(ge=0)
    chunks_expected: int = Field(ge=0)
    titles_verified: int = Field(ge=0)
    titles_expected: int = Field(ge=0)
    workspace_reachable: bool
    checks: list[ReleaseVerificationCheck]
    findings: list[ReleaseVerificationFinding] = Field(default_factory=list)
    failed_checks: list[ReleaseCheck] = Field(default_factory=list)
    skipped_checks: list[ReleaseCheck] = Field(default_factory=list)
    status: ReleaseVerificationStatus
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def the_verdict_follows_the_checks(self) -> "ReleaseVerificationReport":
        if [check.check for check in self.checks] != list(RELEASE_CHECK_ORDER):
            raise ValueError("every check must be reported exactly once, in the canonical order")
        failed = [check.check for check in self.checks if check.status is ReleaseCheckStatus.FAIL]
        skipped = [
            check.check for check in self.checks if check.status is ReleaseCheckStatus.SKIPPED
        ]
        if self.failed_checks != failed or self.skipped_checks != skipped:
            raise ValueError("failed and skipped check lists must follow the checks themselves")
        blocking = any(finding.severity == "error" for finding in self.findings)
        ready = self.status is ReleaseVerificationStatus.READY_FOR_PUBLICATION
        if ready == (bool(failed) or blocking):
            raise ValueError(
                "a release is ready exactly when no check failed and no error finding stands"
            )
        by_check: dict[ReleaseCheck, set[str]] = {}
        for finding in self.findings:
            by_check.setdefault(finding.check, set()).add(finding.code)
        for check in self.checks:
            if set(check.finding_codes) != by_check.get(check.check, set()):
                raise ValueError(f"check {check.check.value} does not list its own findings")
        return self
