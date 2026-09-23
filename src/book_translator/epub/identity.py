"""Deterministic identity for one built EPUB.

The EPUB is addressed by its own internal contents — the ordered ``(path, sha256)`` pairs of every
entry in the archive. That is a different thing from the hash of the ``.epub`` file, which the
workflow also records: the archive hash proves the container bytes, the content address proves the
book inside it, and the two answer different questions.
"""

import hashlib
import json


def epub_content_address(entries: tuple[tuple[str, str], ...]) -> str:
    """Hash the archive's file inventory: ``(archive path, sha256)`` pairs, sorted."""
    payload = [[path, digest] for path, digest in sorted(entries)]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def epub_id(content_sha256: str) -> str:
    """Return the short content-addressed identity of one built EPUB."""
    if len(content_sha256) != 64:
        raise ValueError("an EPUB identity needs a full SHA-256 content address")
    return f"epub-{content_sha256}"
