"""Deterministic A/B artifact construction with the identity key kept separate."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from book_translator.models import (
    SCHEMA_VERSION,
    BlindComparisonAnswerKey,
    BlindComparisonArtifact,
    BlindComparisonKeyEntry,
    BlindComparisonPassage,
)

from .engine import text_sha256

BLIND_COMPARISON_CONTRACT_VERSION = "1.0.0"
BLIND_RUBRIC = [
    "روانی",
    "طبیعی بودن فارسی",
    "لحن رمان",
    "دیالوگ",
    "انسجام",
    "وفاداری ادراک‌شده",
    "ترجیح کلی",
]


@dataclass(frozen=True, slots=True)
class BlindChapterInput:
    chapter_id: str
    paragraph_ids: tuple[str, ...]
    v1_blocks: tuple[str, ...]
    literary_blocks: tuple[str, ...]
    changed_indexes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class BlindBuild:
    artifact: BlindComparisonArtifact
    key: BlindComparisonAnswerKey
    document: str


def _passage_id(literary_run_id: str, chapter_id: str, index: int) -> str:
    material = f"{literary_run_id}:{chapter_id}:{index}".encode()
    return f"passage-{hashlib.sha256(material).hexdigest()[:12]}"


def _selected_indexes(chapter: BlindChapterInput) -> tuple[int, ...]:
    """Choose at most two changed passages, distributed through the chapter."""
    indexes = chapter.changed_indexes or tuple(range(len(chapter.paragraph_ids)))
    if len(indexes) <= 2:
        return indexes
    return indexes[0], indexes[len(indexes) // 2]


def _a_is_literary(literary_run_id: str, passage_id: str) -> bool:
    return int(hashlib.sha256(f"{literary_run_id}:{passage_id}".encode()).hexdigest(), 16) % 2 == 0


def build_blind_comparison(
    literary_run_id: str,
    chapters: tuple[BlindChapterInput, ...],
    *,
    document_file: Path,
    answer_key_file: Path,
    created_at: datetime,
) -> BlindBuild:
    """Build opaque A/B passages; mapping appears only in the separately hashed key."""
    passages: list[BlindComparisonPassage] = []
    keys: list[BlindComparisonKeyEntry] = []
    document_parts = [
        "# مقایسهٔ کور ترجمه",
        "",
        "برای هر قطعه، نسخهٔ A و B را بدون حدس‌زدن منشأ آن‌ها مقایسه کنید.",
        "به هر معیار از ۱ تا ۱۰ امتیاز دهید و ترجیح کلی را جداگانه ثبت کنید.",
        "",
        "معیارها: " + "، ".join(BLIND_RUBRIC),
        "",
    ]
    for chapter in chapters:
        if not (
            len(chapter.paragraph_ids) == len(chapter.v1_blocks) == len(chapter.literary_blocks)
        ):
            raise ValueError("blind comparison chapter paragraphs are not positionally aligned")
        for index in _selected_indexes(chapter):
            passage_id = _passage_id(literary_run_id, chapter.chapter_id, index)
            v1 = chapter.v1_blocks[index]
            literary = chapter.literary_blocks[index]
            literary_first = _a_is_literary(literary_run_id, passage_id)
            version_a, version_b = (literary, v1) if literary_first else (v1, literary)
            label_a: Literal["v1_published", "v2_literary_edited"] = (
                "v2_literary_edited" if literary_first else "v1_published"
            )
            label_b: Literal["v1_published", "v2_literary_edited"] = (
                "v1_published" if literary_first else "v2_literary_edited"
            )
            passages.append(
                BlindComparisonPassage(
                    passage_id=passage_id,
                    chapter_id=chapter.chapter_id,
                    paragraph_ids=[chapter.paragraph_ids[index]],
                    version_a_sha256=text_sha256(version_a),
                    version_b_sha256=text_sha256(version_b),
                )
            )
            keys.append(
                BlindComparisonKeyEntry(
                    passage_id=passage_id,
                    version_a=label_a,
                    version_b=label_b,
                )
            )
            document_parts.extend(
                [
                    f"## {passage_id}",
                    "",
                    "### Version A",
                    "",
                    version_a,
                    "",
                    "### Version B",
                    "",
                    version_b,
                    "",
                    "| معیار | امتیاز A | امتیاز B |",
                    "| --- | ---: | ---: |",
                    *(f"| {rubric} |  |  |" for rubric in BLIND_RUBRIC[:-1]),
                    "",
                    "ترجیح کلی: A / B / برابر",
                    "",
                ]
            )
    document = "\n".join(document_parts).rstrip() + "\n"
    key = BlindComparisonAnswerKey(
        schema_version=SCHEMA_VERSION,
        contract_version=BLIND_COMPARISON_CONTRACT_VERSION,
        literary_run_id=literary_run_id,
        entries=keys,
    )
    from book_translator.provider_execution.artifacts import serialize_yaml

    key_bytes = serialize_yaml(key).encode("utf-8")
    artifact = BlindComparisonArtifact(
        schema_version=SCHEMA_VERSION,
        contract_version=BLIND_COMPARISON_CONTRACT_VERSION,
        literary_run_id=literary_run_id,
        rubric=BLIND_RUBRIC,  # type: ignore[arg-type]
        passages=passages,
        document_file=document_file,
        document_sha256=text_sha256(document),
        answer_key_file=answer_key_file,
        answer_key_sha256=hashlib.sha256(key_bytes).hexdigest(),
        created_at=created_at,
    )
    return BlindBuild(artifact=artifact, key=key, document=document)
