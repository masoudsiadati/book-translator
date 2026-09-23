"""Pure, byte-minimal YAML scalar rewriting and semantic reduction for identity migration.

Re-serializing a registry would rewrite every byte of hand-maintained files and make "only the
identity changed" unprovable. Instead exactly the addressed scalar lines are rewritten, and the
parsed semantic payload is compared before and after.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

BOOK_IDENTITY_MIGRATION_VERSION = "1.0.0"

_SCALAR_LINE = re.compile(
    r"^(?P<prefix>[ \t]*(?:-[ \t]+)?)"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*):"
    r"(?P<space>[ \t]*)"
    # The value is non-greedy so trailing whitespace stays in `trailing` and is preserved verbatim.
    r"(?P<quote>['\"]?)(?P<value>[^'\"]*?)(?P=quote)"
    r"(?P<trailing>[ \t]*)$"
)


class BookIdentityRewriteError(ValueError):
    """Raised when a targeted identity rewrite cannot be applied provably."""


@dataclass(frozen=True, slots=True)
class ScalarRewrite:
    """Replace the value of one YAML scalar key, but only where it holds the expected value."""

    key: str
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class RewriteResult:
    text: str
    changed_lines: tuple[int, ...]
    counts: tuple[int, ...]


def apply_scalar_rewrites(text: str, rewrites: Sequence[ScalarRewrite]) -> RewriteResult:
    """Rewrite addressed scalar values, preserving indentation, quoting, and every other byte."""
    if not rewrites:
        raise BookIdentityRewriteError("at least one rewrite is required")
    if any(rewrite.before == rewrite.after for rewrite in rewrites):
        raise BookIdentityRewriteError("a rewrite must change its value")
    counts = [0] * len(rewrites)
    changed: list[int] = []
    output: list[str] = []
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        body = line.rstrip("\n")
        ending = line[len(body) :]
        match = _SCALAR_LINE.fullmatch(body)
        replacement: str | None = None
        if match is not None:
            for index, rewrite in enumerate(rewrites):
                if match["key"] == rewrite.key and match["value"] == rewrite.before:
                    quote = match["quote"]
                    replacement = (
                        f"{match['prefix']}{match['key']}:{match['space']}"
                        f"{quote}{rewrite.after}{quote}{match['trailing']}"
                    )
                    counts[index] += 1
                    break
        if replacement is None:
            output.append(line)
            continue
        changed.append(number)
        output.append(replacement + ending)
    unmatched = sorted({rewrites[index].key for index, count in enumerate(counts) if count == 0})
    if unmatched:
        raise BookIdentityRewriteError(f"expected scalar was not found for: {', '.join(unmatched)}")
    return RewriteResult("".join(output), tuple(changed), tuple(counts))


def strip_paths(payload: Any, paths: Sequence[Sequence[str]]) -> Any:
    """Deep-copy a payload with each addressed key removed; `*` matches every sequence element."""
    if isinstance(payload, Mapping):
        copied = {key: strip_paths(value, _descend(paths, key)) for key, value in payload.items()}
        for path in paths:
            if len(path) == 1:
                copied.pop(path[0], None)
        return copied
    if isinstance(payload, list):
        return [strip_paths(item, _descend(paths, "*")) for item in payload]
    return payload


def _descend(paths: Sequence[Sequence[str]], key: str) -> list[Sequence[str]]:
    return [path[1:] for path in paths if len(path) > 1 and path[0] in {key, "*"}]


def semantic_identity(payload: Any, paths: Sequence[Sequence[str]]) -> str:
    """Hash the canonical payload with identity fields removed, ignoring key order."""
    reduced = strip_paths(payload, paths)
    canonical = json.dumps(
        reduced, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def migration_identity(
    *, previous_book_id: str, book_id: str, registries: Sequence[tuple[str, str]]
) -> str:
    """Derive a content-addressed migration ID from the identities and exact source bytes."""
    payload = {
        "book_id": book_id,
        "previous_book_id": previous_book_id,
        "registries": [
            {"before_sha256": before, "file": file} for file, before in sorted(registries)
        ],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return f"bim-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]}"
