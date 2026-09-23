"""Provider-neutral structured terminology analysis protocol and deterministic fake."""

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from book_translator.models import (
    TerminologyCandidate,
    TerminologyDiscoveryResponse,
    TerminologyProposalResponse,
    TerminologyRegistry,
    TerminologyReviewResponse,
)


@dataclass(frozen=True, slots=True)
class TerminologyDiscoveryRequest:
    request_id: str
    system_message: str
    user_message: str
    source_language: str
    target_language: str
    chapter_id: str
    chunk_id: str


@dataclass(frozen=True, slots=True)
class TerminologyProposalRequest:
    request_id: str
    system_message: str
    user_message: str
    source_language: str
    target_language: str
    candidates: tuple[TerminologyCandidate, ...]
    registry: TerminologyRegistry


@dataclass(frozen=True, slots=True)
class TerminologyReviewRequest:
    request_id: str
    system_message: str
    user_message: str
    source_language: str
    target_language: str
    proposal_ids: tuple[str, ...]


class TerminologyAnalysisProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    def discover(self, request: TerminologyDiscoveryRequest) -> Any: ...

    def propose(self, request: TerminologyProposalRequest) -> Any: ...


class TerminologyReviewProvider(Protocol):
    """Independent review-only provider boundary."""

    @property
    def provider_name(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    def review(self, request: TerminologyReviewRequest) -> Any: ...


class TerminologyProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.ambiguous = ambiguous


class FakeTerminologyProvider:
    """Offline typed fixture provider; it never derives or translates book prose."""

    def __init__(
        self,
        scenario: str = "success",
        *,
        discovery_response: TerminologyDiscoveryResponse | None = None,
        proposal_response: TerminologyProposalResponse | None = None,
        review_response: TerminologyReviewResponse | None = None,
    ) -> None:
        self.scenario = scenario
        self.discovery_response = discovery_response or TerminologyDiscoveryResponse(
            schema_version="1.0", candidates=[]
        )
        self.proposal_response = proposal_response or TerminologyProposalResponse(
            schema_version="1.0", proposals=[]
        )
        self.review_response = review_response or TerminologyReviewResponse(
            schema_version="1.0", reviews=[]
        )

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def adapter_version(self) -> str:
        return "1.0.0"

    def _failure(self) -> None:
        if self.scenario == "retryable_failure":
            raise TerminologyProviderError("synthetic retryable failure", retryable=True)
        if self.scenario == "non_retryable_failure":
            raise TerminologyProviderError("synthetic permanent failure", retryable=False)
        if self.scenario == "ambiguous_failure":
            raise TerminologyProviderError(
                "synthetic ambiguous failure", retryable=False, ambiguous=True
            )

    def discover(self, request: TerminologyDiscoveryRequest) -> Any:
        self._failure()
        if self.scenario == "malformed":
            return {"schema_version": "1.0", "candidates": "invalid"}
        if self.scenario == "duplicate_candidates":
            values = list(self.discovery_response.candidates)
            return self.discovery_response.model_copy(update={"candidates": values + values})
        if self.scenario not in {"success", "conflicting_proposals"}:
            raise ValueError(f"unknown fake terminology scenario: {self.scenario}")
        return self.discovery_response

    def propose(self, request: TerminologyProposalRequest) -> Any:
        self._failure()
        if self.scenario == "malformed":
            return {"schema_version": "1.0", "proposals": [{"invalid": True}]}
        if self.scenario == "conflicting_proposals":
            values = list(self.proposal_response.proposals)
            if values:
                conflict = values[0].model_copy(
                    update={
                        "proposal_id": "tp-" + "f" * 24,
                        "proposed_target": values[0].proposed_target + "-conflict",
                    }
                )
                return self.proposal_response.model_copy(update={"proposals": [*values, conflict]})
        if self.scenario not in {"success", "duplicate_candidates", "conflicting_proposals"}:
            raise ValueError(f"unknown fake terminology scenario: {self.scenario}")
        return self.proposal_response

    def review(self, request: TerminologyReviewRequest) -> Any:
        self._failure()
        if self.scenario == "malformed":
            return {"schema_version": "1.0", "reviews": [{"invalid": True}]}
        values = list(self.review_response.reviews)
        if self.scenario == "missing_review":
            return self.review_response.model_copy(update={"reviews": values[:-1]})
        if self.scenario == "duplicate_review" and values:
            return self.review_response.model_copy(update={"reviews": [*values, values[0]]})
        if self.scenario == "conflicting_review" and values:
            conflict = values[0].model_copy(
                update={
                    "review_id": "tr-" + "f" * 24,
                    "disposition": "reject",
                    "reviewed_target": None,
                }
            )
            return self.review_response.model_copy(update={"reviews": [*values, conflict]})
        if self.scenario not in {
            "success",
            "missing_review",
            "duplicate_review",
            "conflicting_review",
        }:
            raise ValueError(f"unknown fake terminology scenario: {self.scenario}")
        return self.review_response


class CodexCliTerminologyProvider:
    """Authenticated local Codex adapter returning JSON for subsequent strict validation."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        model: str | None = None,
        timeout_seconds: int = 600,
    ) -> None:
        self.executable = executable
        self.model = model
        self.timeout_seconds = timeout_seconds

    @property
    def provider_name(self) -> str:
        return "codex-cli"

    @property
    def adapter_version(self) -> str:
        return "1.0.0"

    def _execute(self, system: str, user: str) -> Any:
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.upper().endswith("_API_KEY")
            and name.upper() not in {"OPENAI_BASE_URL", "OPENAI_API_BASE", "AZURE_OPENAI_ENDPOINT"}
        }
        with tempfile.TemporaryDirectory(prefix="book-translator-terminology-") as temporary:
            root = Path(temporary)
            output = root / "final-message.json"
            command = [
                self.executable,
                "exec",
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--color",
                "never",
                "--output-last-message",
                str(output),
                "--cd",
                str(root),
                "--skip-git-repo-check",
            ]
            if self.model is not None:
                command.extend(("--model", self.model))
            command.append("-")
            prompt = (
                f"<system-instructions>\n{system}\n</system-instructions>\n"
                f"<user-data>\n{user}\n</user-data>\n"
            )
            try:
                completed = subprocess.run(
                    command,
                    input=prompt.encode("utf-8"),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=root,
                    env=environment,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise TerminologyProviderError(
                    "Codex terminology analysis timed out", retryable=False, ambiguous=True
                ) from exc
            except FileNotFoundError as exc:
                raise TerminologyProviderError(
                    "Codex CLI executable was not found", retryable=False
                ) from exc
            except OSError as exc:
                raise TerminologyProviderError(
                    "Codex CLI terminology process could not be invoked", retryable=False
                ) from exc
            if completed.returncode != 0:
                raise TerminologyProviderError(
                    f"Codex CLI exited unsuccessfully ({completed.returncode})", retryable=False
                )
            try:
                return json.loads(output.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise TerminologyProviderError(
                    "Codex CLI returned malformed structured terminology output", retryable=False
                ) from exc

    def discover(self, request: TerminologyDiscoveryRequest) -> Any:
        return self._execute(request.system_message, request.user_message)

    def propose(self, request: TerminologyProposalRequest) -> Any:
        return self._execute(request.system_message, request.user_message)

    def review(self, request: TerminologyReviewRequest) -> Any:
        return self._execute(request.system_message, request.user_message)
