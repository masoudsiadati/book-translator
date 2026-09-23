"""Pure content identity for rendered prompt packages."""

import hashlib
import json
from typing import Any

#: 1.3.0 adds the numbered paragraph contract as a *selectable* output contract.  A run that does
#: not select it renders byte-identically to 1.2.0, which is what keeps existing packages,
#: attempts, and cross-run reuse eligibility untouched by this renderer version.
#: 1.4.0 adds the length-framed ``terminology-policy`` section for snapshots that froze a series
#: terminology policy.  A snapshot without one renders byte-identically to 1.3.0.
PROMPT_PACKAGE_RENDERER_VERSION = "1.5.0"

#: The original contract: translated prose, paragraph identity carried only by blank lines.
OUTPUT_CONTRACT_VERSION = "persian-translation-text/1.0.0"

#: The numbered paragraph contract: every source paragraph is sent under an explicit marker and
#: every marker must come back exactly once, in order, carrying that paragraph and nothing else.
MARKED_OUTPUT_CONTRACT_VERSION = "persian-translation-marked-paragraphs/1.0.0"

#: The contract new runs are created against unless an operator selects otherwise.
DEFAULT_OUTPUT_CONTRACT_VERSION = MARKED_OUTPUT_CONTRACT_VERSION

SUPPORTED_OUTPUT_CONTRACTS = frozenset({OUTPUT_CONTRACT_VERSION, MARKED_OUTPUT_CONTRACT_VERSION})


def uses_paragraph_markers(output_contract_version: str | None) -> bool:
    """Report whether an output contract requires numbered paragraph markers.

    ``None`` is the pre-contract default recorded by every run created before the contract
    existed, and it means the original unmarked contract.
    """
    return output_contract_version == MARKED_OUTPUT_CONTRACT_VERSION


def canonical_package_identity_bytes(payload: dict[str, Any]) -> bytes:
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


def package_identity(payload: dict[str, Any]) -> tuple[str, str]:
    digest = hashlib.sha256(canonical_package_identity_bytes(payload)).hexdigest()
    return f"pp-{digest[:24]}", digest
