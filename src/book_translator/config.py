from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ProjectDataError(ValueError):
    """Raised when a project data file cannot be loaded or validated."""


# These providers inherit a model from the user's own authenticated CLI session, so an empty
# allow-list means "keep whatever that session already selects" rather than "no model is allowed".
CLI_SESSION_PROVIDERS = frozenset({"codex-cli", "claude-code"})


class BookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book_id: str = Field(min_length=1)
    title_en: str = Field(min_length=1)
    # Blank is valid: a Persian book title is a human translation decision, never a placeholder.
    title_fa: str = ""
    author: str = Field(min_length=1)
    #: A persisted series decision is a human bibliographic call, like `title_fa`; when set it is
    #: resolved with `configuration` provenance and outranks front-matter inference.
    series_name: str | None = Field(default=None, min_length=1)
    series_index: int | None = Field(default=None, ge=1)
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    pdf_path: Path | None = None
    epub_path: Path | None = None
    chunk_target_words: int = Field(gt=0)
    chunk_max_words: int = Field(gt=0)
    previous_context_words: int = Field(ge=0)
    translation_prompt: Path = Path("config/translation-prompt.md")
    glossary: Path = Path("config/glossary.yaml")
    style_guide: Path = Path("config/style-guide.md")
    terminology: Path | None = None
    terminology_questions: Path | None = None
    #: Series-aware terminology lineage. A book-scoped workspace that names a series policy
    #: derives its own terminology registry from it and never reads another book's registry.
    series_terminology_policy: Path | None = None
    reference_corpus: Path | None = None
    terminology_acceptance_policy: Literal["manual", "high-confidence", "automatic"] = "manual"
    terminology_confidence_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    terminology_review_required: bool = False
    translation_provider_models: dict[str, list[str]] = Field(
        default_factory=lambda: {"fake": ["fake-v1"]}
    )
    openai_reasoning_models: list[str] = Field(default_factory=list)
    provider_timeout_seconds: int = Field(default=600, gt=0)
    provider_max_output_tokens: int = Field(default=12000, gt=0)
    provider_sdk_retry_count: int = Field(default=0, ge=0)
    provider_store_raw_response: bool = True
    #: Whether a newly created translation run adopts the numbered paragraph output contract.
    #: It is read only when a run is created; the contract is then frozen on the run itself, so
    #: changing this setting can never reinterpret an existing run's packages or attempts.
    paragraph_marker_contract: bool = True
    output_format: Literal["epub3"]

    @model_validator(mode="after")
    def target_must_not_exceed_maximum(self) -> "BookConfig":
        if (self.pdf_path is None) == (self.epub_path is None):
            raise ValueError("exactly one of pdf_path or epub_path must be configured")
        if self.chunk_target_words > self.chunk_max_words:
            raise ValueError("chunk_target_words must not exceed chunk_max_words")
        if self.previous_context_words >= self.chunk_max_words:
            raise ValueError("previous_context_words must be less than chunk_max_words")
        if (self.series_terminology_policy is None) != (self.reference_corpus is None):
            raise ValueError(
                "series_terminology_policy and reference_corpus must be configured together"
            )
        if self.series_terminology_policy is not None and self.terminology is not None:
            raise ValueError(
                "a book cannot name both a series terminology policy and an explicit terminology "
                "registry; the registry is derived from the policy"
            )
        if any(not provider for provider in self.translation_provider_models):
            raise ValueError("configured translation provider names must not be empty")
        if any(
            not models and provider not in CLI_SESSION_PROVIDERS
            for provider, models in self.translation_provider_models.items()
        ):
            raise ValueError(
                "configured translation providers must allow at least one model, except the "
                f"authenticated CLI session providers {sorted(CLI_SESSION_PROVIDERS)}"
            )
        return self

    @property
    def source_path(self) -> Path:
        source = self.pdf_path if self.pdf_path is not None else self.epub_path
        if source is None:  # Validation makes this unreachable; retained for type narrowing.
            raise ValueError("book source path is not configured")
        return source

    @property
    def source_format(self) -> Literal["pdf", "epub"]:
        return "pdf" if self.pdf_path is not None else "epub"


def require_pdf_path(config: BookConfig) -> Path:
    if config.pdf_path is None:
        raise ProjectDataError("book is configured with an EPUB source, not a PDF source")
    return config.pdf_path


def require_epub_path(config: BookConfig) -> Path:
    if config.epub_path is None:
        raise ProjectDataError("book is configured with a PDF source, not an EPUB source")
    return config.epub_path


def load_book_config(path: Path = Path("config/book.yaml")) -> BookConfig:
    try:
        with path.open(encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except FileNotFoundError as exc:
        raise ProjectDataError(f"configuration file not found: {path}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ProjectDataError(f"could not read configuration file {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ProjectDataError(f"configuration file {path} must contain a YAML mapping")

    try:
        return BookConfig.model_validate(raw)
    except ValidationError as exc:
        raise ProjectDataError(f"invalid configuration in {path}: {exc}") from exc
