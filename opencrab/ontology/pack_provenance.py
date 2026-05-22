"""
Pack provenance helpers.

Unified pack_id inference for vector / BM25 / graph results so the same
rule is applied across all retrieval paths. The single entry point is
``infer_pack_id(item)`` — every retrieval site should call it instead of
re-implementing the lookup.

Inference order:
  1. item["metadata"]["pack_id"]
  2. item["properties"]["pack_id"]
  3. /packs/<id>/ pattern found in any of:
        item["metadata"]["source_path"]
        item["properties"]["source_path"]
        item["source_path"]
        item["source_id"]
        item["node_id"]
        item["id"]
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_PACK_RE = re.compile(r"/packs/([^/]+)/")


def infer_pack_id_from_path(path: str | Path) -> str | None:
    """Return ``<id>`` from a path like ``.../packs/<id>/stage/...``."""
    if not path:
        return None
    text = str(path)
    match = _PACK_RE.search(text.replace("\\", "/"))
    if match:
        return match.group(1)

    # Tolerate inputs missing a leading slash (e.g. "packs/<id>/stage/...").
    parts = Path(text).parts
    if "packs" in parts:
        idx = parts.index("packs")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def _string_pack_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return infer_pack_id_from_path(text)


def infer_pack_id(item: dict[str, Any] | None) -> str | None:
    """Return the pack_id for a result item, or ``None`` if not derivable.

    The function never raises; unexpected types simply return ``None``.
    """
    if not item:
        return None

    metadata = item.get("metadata") if isinstance(item, dict) else None
    if isinstance(metadata, dict):
        pid = metadata.get("pack_id")
        if pid:
            return str(pid)

    properties = item.get("properties") if isinstance(item, dict) else None
    if isinstance(properties, dict):
        pid = properties.get("pack_id")
        if pid:
            return str(pid)

    candidates: list[Any] = []
    if isinstance(metadata, dict):
        candidates.append(metadata.get("source_path"))
        candidates.append(metadata.get("source_id"))
    if isinstance(properties, dict):
        candidates.append(properties.get("source_path"))
        candidates.append(properties.get("source_id"))
    for key in ("source_path", "source_id", "node_id", "id"):
        candidates.append(item.get(key))

    for value in candidates:
        pid = _string_pack_id(value)
        if pid:
            return pid

    return None


def matches_pack_filter(
    item: dict[str, Any] | None,
    pack_ids: list[str] | tuple[str, ...] | set[str] | None,
    include_unpackaged: bool = False,
) -> bool:
    """Return True if ``item`` survives a pack_id filter.

    - If ``pack_ids`` is empty/None, always pass.
    - Otherwise, the inferred pack_id must be in the set.
    - Items with no inferable pack_id only pass when ``include_unpackaged`` is True.
    """
    if not pack_ids:
        return True
    allowed = set(pack_ids)
    pid = infer_pack_id(item)
    if pid is None:
        return bool(include_unpackaged)
    return pid in allowed
