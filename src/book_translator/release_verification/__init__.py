"""Deterministic read-only release verification.

Verification is the gate between assembly and publication. It answers one question — may this
release directory be published — and it answers it from the bytes on disk rather than from any
claim the assembly made about itself.

The stage is the narrowest in the project on purpose: no provider import and no path to one, no
write outside the report an operator explicitly asks for, no mutation of any translation run,
snapshot, job, attempt, acceptance record, or registry, and no clock dependency beyond the
report's own timestamp — which is excluded from the report's content address so that two runs over
an unchanged release are comparable byte for byte.
"""

from book_translator.models.release_verification import (
    RELEASE_CHECK_ORDER,
    ReleaseCheck,
    ReleaseCheckStatus,
    ReleaseVerificationCheck,
    ReleaseVerificationFinding,
    ReleaseVerificationReport,
    ReleaseVerificationStatus,
)
from book_translator.release_verification.engine import (
    RELEASE_VERIFICATION_ENGINE_VERSION,
    ReleaseVerificationError,
    check_book,
    check_chapters,
    check_manifest,
    check_provenance,
    check_terminology,
    check_titles,
    check_translations,
    report_content_address,
)
from book_translator.release_verification.workflow import (
    ReleaseVerification,
    serialize_verification,
    verify_release_artifact,
    write_verification_report,
)

__all__ = [
    "RELEASE_CHECK_ORDER",
    "RELEASE_VERIFICATION_ENGINE_VERSION",
    "ReleaseCheck",
    "ReleaseCheckStatus",
    "ReleaseVerification",
    "ReleaseVerificationCheck",
    "ReleaseVerificationError",
    "ReleaseVerificationFinding",
    "ReleaseVerificationReport",
    "ReleaseVerificationStatus",
    "check_book",
    "check_chapters",
    "check_manifest",
    "check_provenance",
    "check_terminology",
    "check_titles",
    "check_translations",
    "report_content_address",
    "serialize_verification",
    "verify_release_artifact",
    "write_verification_report",
]
