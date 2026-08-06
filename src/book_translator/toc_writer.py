"""Deterministic, atomic YAML persistence for normalized TOCs."""

from pathlib import Path
from typing import Any, cast

import yaml

from book_translator.atomic_write import atomic_write_text
from book_translator.models import TocDocument


def serialize_toc(document: TocDocument) -> str:
    data: dict[str, Any] = document.model_dump(mode="json", exclude_none=True)
    return cast(
        str, yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    )


def write_toc(document: TocDocument, output_path: Path, *, force: bool = False) -> None:
    atomic_write_text(output_path, serialize_toc(document), overwrite=force)
