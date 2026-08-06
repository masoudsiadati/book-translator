"""Regenerate committed JSON Schemas from the canonical Pydantic models."""

import json
from pathlib import Path

from book_translator.models import (
    BookWorkspaceMetadata,
    ChapterMetadata,
    ChunkMetadata,
    ExtractionSnapshot,
    TocDocument,
)

SCHEMAS = {
    "book-metadata.schema.json": BookWorkspaceMetadata,
    "toc.schema.json": TocDocument,
    "chapter-metadata.schema.json": ChapterMetadata,
    "chunk-metadata.schema.json": ChunkMetadata,
    "snapshot.schema.json": ExtractionSnapshot,
}


def main() -> None:
    output_directory = Path(__file__).resolve().parents[1] / "schemas"
    output_directory.mkdir(exist_ok=True)
    for filename, model in SCHEMAS.items():
        destination = output_directory / filename
        serialized = json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n"
        destination.write_text(serialized, encoding="utf-8")


if __name__ == "__main__":
    main()
