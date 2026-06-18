"""Stable, content-addressed identifiers shared across the codebase.

The single canonical form for derived ids: SHA-256 of a deterministic JSON
encoding (sorted keys, unicode preserved), truncated to 16 hex chars, prefixed
as ``{prefix}:{digest}``. This consolidates the helpers that had diverged —
``pack.neo4j_export._sha_id`` already used this form, while the obsidian
importer used SHA-1 + dash + raw-string encoding.

``crabharness.dedupe._compute_id`` is intentionally NOT migrated here: it is a
separate, local-only id namespace (``.seen.json`` dedup keys) that is never
cross-referenced with graph entity ids, and crabharness must stay importable
without opencrab. Unifying it would change every key and re-surface every seen
item as unseen, for no benefit.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Deterministic JSON encoding used for hashing and stable serialisation.

    Sorted keys, unicode kept (``ensure_ascii=False``), non-serialisable values
    coerced via ``str``. Byte-identical to the former
    ``pack.neo4j_export._stable_json``.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def stable_id(prefix: str, value: Any) -> str:
    """Return ``{prefix}:{16-hex}`` SHA-256 of the canonical-JSON encoding."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"
