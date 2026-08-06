"""Shared model configuration."""

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    """Strict base for persisted, versioned contracts."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)
