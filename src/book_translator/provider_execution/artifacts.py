"""Deterministic private attempt serialization and validation helpers."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from book_translator.atomic_write import atomic_write_text
from book_translator.provider_execution.errors import ExecutorError


def canonical_json(data: dict[str, object]) -> bytes:
    return (
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def serialize_yaml(model: BaseModel) -> str:
    value = yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=False),
        allow_unicode=True,
        sort_keys=False,
    )
    if not isinstance(value, str):
        raise ExecutorError("could not serialize provider attempt metadata")
    return value


def load_yaml[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ExecutorError(f"invalid provider attempt artifact {path}: {exc}") from exc


def private_write(path: Path, content: bytes, *, overwrite: bool = False) -> None:
    try:
        atomic_write_text(path, content.decode("utf-8"), overwrite=overwrite)
        os.chmod(path, 0o600)
    except (OSError, UnicodeError) as exc:
        raise ExecutorError(f"could not publish private provider artifact {path.name}") from exc


def project_relative(path: Path, project_root: Path) -> Path:
    try:
        return Path(path.resolve().relative_to(project_root.resolve()).as_posix())
    except ValueError as exc:
        raise ExecutorError("provider artifact path must be project-relative") from exc


def sanitized_request_payload(
    *,
    request_id: str,
    idempotency_key: str,
    provider: str,
    model: str | None,
    timeout_seconds: int,
    max_output_tokens: int,
    sdk_retry_count: int,
    reasoning_effort: str | None,
    system_file: Path,
    system_sha256: str,
    user_file: Path,
    user_sha256: str,
    output_contract: str,
    metadata: dict[str, str],
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "provider": provider,
        "model": model,
        "timeout_seconds": timeout_seconds,
        "max_output_tokens": max_output_tokens,
        "sdk_retry_count": sdk_retry_count,
        "reasoning_effort": reasoning_effort,
        "system_message": {"file": system_file.as_posix(), "sha256": system_sha256},
        "user_message": {"file": user_file.as_posix(), "sha256": user_sha256},
        "output_contract": output_contract,
        "metadata": metadata,
    }


def safe_json_data(value: Any) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ExecutorError("provider response representation must be a mapping")
    return {str(key): item for key, item in value.items()}
