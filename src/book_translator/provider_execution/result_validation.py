"""Pure extraction and structural checks for provider translation results.

The work is split in two so the executor can hold on to what a provider actually said even when
it is refused.  :func:`candidate_output` decides whether a result carries exactly one trustworthy
answer and normalizes it; :func:`validate_candidate` decides whether that answer is a translation
under the job's output contract.  A candidate that fails the second step is still the provider's
answer, and the executor preserves it as a private diagnostic artifact; a result that fails the
first step has no answer worth preserving.  :func:`extract_translation` composes the two.
"""

import re
from dataclasses import dataclass

from book_translator.models import ProviderErrorCategory
from book_translator.paragraph_markers import (
    PARAGRAPH_MARKER_CONTRACT_VERSION,
    MarkerFinding,
    MarkerParseResult,
    marker_token,
    parse_marked_document,
    source_paragraphs,
)
from book_translator.prompt_packaging.identity import uses_paragraph_markers
from book_translator.provider_execution.errors import (
    ErrorClassification,
    MarkerContractDiagnostics,
    ProviderExecutionError,
)
from book_translator.provider_execution.protocol import ProviderResult


@dataclass(frozen=True, slots=True)
class ValidatedTranslation:
    content: bytes
    warnings: tuple[str, ...]


def _normalized_output(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def _prose_free(finding: MarkerFinding) -> MarkerFinding:
    """Reduce a finding's observed value to a bare marker token.

    The parser quotes the offending line for a malformed marker, and that line may carry prose
    beside the token.  Diagnostics leave the private attempt directory (attempt metadata, queue
    error messages, operator tables), so only the token survives.
    """
    if finding.observed_marker is None:
        return finding
    return MarkerFinding(
        code=finding.code,
        severity=finding.severity,
        expected_marker=finding.expected_marker,
        observed_marker=marker_token(finding.observed_marker),
        source_paragraph_index=finding.source_paragraph_index,
        message=finding.message,
    )


def _marker_violation(parsed: MarkerParseResult) -> ProviderExecutionError:
    """Classify a broken numbered paragraph contract.

    It is retryable and safe to replay because the failure is about the shape of the answer, not
    about its content or about anything that happened at the transport: the same request asked
    again is exactly the right remedy.  It is never ambiguous, because the response arrived and
    was read; what it is not is a translation, so no translation artifact is published for it.
    The structural findings travel with the error so the attempt can say which marker failed
    without anyone opening the rejected prose.
    """
    codes = tuple(finding.code for finding in parsed.findings)
    return ProviderExecutionError(
        ErrorClassification(
            ProviderErrorCategory.MALFORMED_RESPONSE,
            retryable=True,
            ambiguous=False,
            safe_automatic_retry=True,
            message=(
                "provider output violated the numbered paragraph contract: "
                + ", ".join(sorted(set(codes)))
            ),
        ),
        marker_diagnostics=MarkerContractDiagnostics(
            contract_version=PARAGRAPH_MARKER_CONTRACT_VERSION,
            expected_marker_count=parsed.expected_count,
            observed_marker_count=len(parsed.observed_markers),
            findings=tuple(_prose_free(finding) for finding in parsed.findings),
        ),
    )


def _source_echo() -> ProviderExecutionError:
    return ProviderExecutionError(
        ErrorClassification(
            ProviderErrorCategory.MALFORMED_RESPONSE,
            retryable=False,
            ambiguous=False,
            safe_automatic_retry=False,
            message="provider output exactly echoed the source",
        )
    )


def candidate_output(result: ProviderResult) -> str:
    """Return the one normalized answer a completed result carries, or refuse the result.

    A truncated result, an empty one, or one with several competing outputs has no candidate a
    diagnostic could trust, and each is refused here with its own classification.  What comes
    back is LF-normalized with exactly one trailing newline and is otherwise the provider's text
    byte for byte; it has not yet been judged as a translation.
    """
    if result.status != "completed" or result.incomplete_reason:
        raise ProviderExecutionError(
            ErrorClassification(
                ProviderErrorCategory.OUTPUT_TRUNCATED,
                retryable=True,
                ambiguous=False,
                safe_automatic_retry=False,
                message="provider response was incomplete or truncated",
            )
        )
    outputs = tuple(text for text in result.text_outputs if text.strip())
    if not outputs:
        raise ProviderExecutionError(
            ErrorClassification(
                ProviderErrorCategory.EMPTY_RESPONSE,
                retryable=True,
                ambiguous=False,
                safe_automatic_retry=False,
                message="provider response contained no translation text",
            )
        )
    if len(outputs) != 1:
        raise ProviderExecutionError(
            ErrorClassification(
                ProviderErrorCategory.MALFORMED_RESPONSE,
                retryable=False,
                ambiguous=False,
                safe_automatic_retry=False,
                message="provider response contained multiple conflicting text outputs",
            )
        )
    return _normalized_output(outputs[0])


def validate_candidate(
    text: str, source_bytes: bytes, *, output_contract: str | None = None
) -> ValidatedTranslation:
    """Judge one normalized candidate as a translation of *source_bytes* under the contract.

    Every refusal raised here is about a candidate that arrived whole, which is what makes the
    candidate worth keeping as a diagnostic.  Marker-contract refusals carry their structural
    findings on the error; no refusal carries the candidate's prose.
    """
    encoded = text.encode("utf-8")
    if encoded == source_bytes or text.strip() == source_bytes.decode("utf-8").strip():
        raise _source_echo()
    lowered = text.lower()
    if "<framed-data " in lowered or "<translation-job>" in lowered:
        raise ProviderExecutionError(
            ErrorClassification(
                ProviderErrorCategory.MALFORMED_RESPONSE,
                retryable=False,
                ambiguous=False,
                safe_automatic_retry=False,
                message="provider output reproduced prompt-package framing",
            )
        )
    if re.search(r"\b(error|refusal|cannot comply)\b", lowered[:300]):
        raise ProviderExecutionError(
            ErrorClassification(
                ProviderErrorCategory.CONTENT_POLICY,
                retryable=False,
                ambiguous=False,
                safe_automatic_retry=False,
                message="provider output appears to contain a refusal or error envelope",
            )
        )
    warnings: list[str] = []
    if uses_paragraph_markers(output_contract):
        # Marker conformance is decided before anything else is measured, and the marker-free
        # reconstruction replaces the raw output from here on: the accepted translation is prose,
        # and marker syntax must never reach an artifact, a release, or a reader.
        expected = len(source_paragraphs(source_bytes.decode("utf-8")))
        parsed = parse_marked_document(text, expected_count=expected)
        if not parsed.valid:
            raise _marker_violation(parsed)
        text = parsed.text
        encoded = text.encode("utf-8")
        if encoded == source_bytes or text.strip() == source_bytes.decode("utf-8").strip():
            # The echo check above saw the *marked* output, which can never equal the frozen
            # source; a provider that simply copied the source back under its own markers has to
            # be caught here, after the markers come off.
            raise _source_echo()
    paragraph_total = max(1, len(source_bytes.decode("utf-8").strip().split("\n\n")))
    output_paragraphs = max(1, len(text.strip().split("\n\n")))
    if output_paragraphs < max(1, paragraph_total // 2):
        warnings.append("translation paragraph count is substantially below source paragraph count")
    ascii_words = re.findall(r"\b[A-Za-z]{3,}\b", text)
    all_words = text.split()
    if all_words and len(ascii_words) / len(all_words) > 0.35:
        warnings.append("translation has a high conservative English-word proportion")
    return ValidatedTranslation(encoded, tuple(warnings))


def extract_translation(
    result: ProviderResult, source_bytes: bytes, *, output_contract: str | None = None
) -> ValidatedTranslation:
    """Extract and validate a translation from *result* in one step."""
    return validate_candidate(
        candidate_output(result), source_bytes, output_contract=output_contract
    )
