"""Deterministic release identity: the content address of an assembled release."""

import hashlib
import json

from book_translator.models.release import ReleaseArtifactReference


def release_content_address(artifacts: tuple[ReleaseArtifactReference, ...]) -> str:
    """Hash the release's file inventory, ignoring when it was assembled.

    The manifest carries a timestamp and therefore cannot address itself. This address is over
    the sorted ``(path, sha256)`` pairs of everything the manifest indexes, so re-exporting the
    same run to the same release ID reproduces it exactly — which is what makes "this release is
    the same book" a checkable statement rather than a claim about the clock.
    """
    payload = [
        [artifact.file.as_posix(), artifact.sha256]
        for artifact in sorted(artifacts, key=lambda item: item.file.as_posix())
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
