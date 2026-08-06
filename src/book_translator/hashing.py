"""Byte-exact hashing utilities for artifact provenance."""

import hashlib
from pathlib import Path


def sha256_file(path: Path, *, block_size: int = 1024 * 1024) -> str:
    """Stream a file and return its lowercase hexadecimal SHA-256 digest."""
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(block_size):
            digest.update(block)
    return digest.hexdigest()
