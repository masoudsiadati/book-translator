from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ProjectDataError(ValueError):
    """Raised when a project data file cannot be loaded or validated."""


class BookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book_id: str = Field(min_length=1)
    title_en: str = Field(min_length=1)
    title_fa: str = Field(min_length=1)
    author: str = Field(min_length=1)
    source_language: str = Field(min_length=2)
    target_language: str = Field(min_length=2)
    pdf_path: Path
    chunk_target_words: int = Field(gt=0)
    chunk_max_words: int = Field(gt=0)
    previous_context_words: int = Field(ge=0)
    output_format: Literal["epub3"]

    @model_validator(mode="after")
    def target_must_not_exceed_maximum(self) -> "BookConfig":
        if self.chunk_target_words > self.chunk_max_words:
            raise ValueError("chunk_target_words must not exceed chunk_max_words")
        if self.previous_context_words >= self.chunk_max_words:
            raise ValueError("previous_context_words must be less than chunk_max_words")
        return self


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
