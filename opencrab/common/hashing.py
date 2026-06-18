"""Hashing helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path


def file_sha256(path: Path) -> str:
    """SHA-256 hex digest of a file, read in 1 MiB chunks.

    Consolidates the identical ``_sha256``/``sha256_file`` helpers previously
    duplicated in media/, pack/, and scripts/.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
