"""
Pack provenance helpers.

Unified pack_id inference for vector / BM25 / graph results so the same
rule is applied across all retrieval paths. The single entry point is
``infer_pack_id(item)`` — every retrieval site should call it instead of
re-implementing the lookup.

Inference order:
  1. item["metadata"]["pack_id"]
  2. item["properties"]["pack_id"]
  3. item["pack_id"]
  4. /packs/<id>/ pattern found in any of:
        item["metadata"]["source_path"]
        item["properties"]["source_path"]
        item["source_path"]
        item["source_id"]
        item["node_id"]
        item["id"]
"""

from __future__ import annotations

import json
import re
import sqlite3
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

    pid = item.get("pack_id") if isinstance(item, dict) else None
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


def resolve_backfill_dry_run(
    apply_changes: bool, dry_run: bool | None
) -> tuple[bool, str | None]:
    """Reconcile ``packs backfill-pack-id``'s ``--apply``/``--dry-run``/
    ``--no-dry-run`` flags into a single effective dry-run bool.

    Returns ``(effective_dry_run, warning_or_None)``. Default (neither flag)
    is dry-run; ``--apply`` alone applies; contradictory combinations honour
    the safer (dry-run) choice and surface a warning.
    """
    if apply_changes and dry_run is True:
        return True, "warning: both --apply and --dry-run given; honouring --dry-run."
    if apply_changes:
        # dry_run is None or False -> explicit "do it"
        return False, None
    if dry_run is False:
        return True, "warning: --no-dry-run given without --apply; staying in dry-run."
    return True, None


def backfill_pack_ids(
    db_path: str | Path,
    *,
    assume_pack_id: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Back-fill ``properties.pack_id`` on ``graph_nodes``/``graph_edges``
    rows in the local SQLite graph store at ``db_path``.

    Infers pack_id from any ``/packs/<id>/`` path stored in
    ``properties.source_path`` / ``source_id`` / ``id``, falling back to the
    row's own type/id key columns. ``assume_pack_id`` fills any entry that
    still can't be inferred. Rows that already carry a ``pack_id`` are left
    untouched. With ``dry_run=True`` (the default) no row is written; the
    returned summary still reflects what *would* change.
    """
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "nodes_inferred": 0,
        "nodes_assumed": 0,
        "nodes_skipped": 0,
        "edges_inferred": 0,
        "edges_assumed": 0,
        "edges_skipped": 0,
    }

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        def _process(table: str, key_cols: tuple[str, ...]) -> None:
            cur.execute(f"SELECT {', '.join(key_cols)}, properties FROM {table}")
            rows = cur.fetchall()
            for row in rows:
                try:
                    props = json.loads(row["properties"]) if row["properties"] else {}
                except (TypeError, ValueError):
                    props = {}
                if not isinstance(props, dict):
                    summary[f"{table.split('_')[1]}_skipped"] += 1
                    continue
                if props.get("pack_id"):
                    continue
                inferred: str | None = None
                for candidate_key in ("source_path", "source_id", "id"):
                    value = props.get(candidate_key)
                    if value:
                        inferred = infer_pack_id_from_path(str(value))
                        if inferred:
                            break
                if not inferred:
                    # node_id column from the row itself
                    for key in key_cols:
                        if key.endswith("_id"):
                            inferred = infer_pack_id_from_path(str(row[key]))
                            if inferred:
                                break
                if inferred:
                    props["pack_id"] = inferred
                    summary[f"{table.split('_')[1]}_inferred"] += 1
                elif assume_pack_id:
                    props["pack_id"] = assume_pack_id
                    summary[f"{table.split('_')[1]}_assumed"] += 1
                else:
                    summary[f"{table.split('_')[1]}_skipped"] += 1
                    continue
                if not dry_run:
                    set_clauses = " AND ".join(f"{c}=?" for c in key_cols)
                    values = [json.dumps(props)] + [row[c] for c in key_cols]
                    cur.execute(
                        f"UPDATE {table} SET properties=? WHERE {set_clauses}",
                        values,
                    )

        _process("graph_nodes", ("node_type", "node_id"))
        _process("graph_edges", ("from_type", "from_id", "relation", "to_type", "to_id"))

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    return summary
