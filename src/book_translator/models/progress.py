"""Legacy progress and future pipeline run status contracts."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class PipelineRunStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"


class Progress(BaseModel):
    """The existing state/progress.json contract; retained without migration."""

    model_config = ConfigDict(extra="forbid")

    detected_chapters: list[int]
    translated_chapters: list[int]
    approved_chapters: list[int]
    failed_chunks: list[str]
