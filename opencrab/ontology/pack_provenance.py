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

from opencrab.locking import write_lock

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


def _normalize_props(props_raw: Any) -> dict[str, Any] | None:
    """Normalize a properties/metadata column value to a ``dict``, or
    ``None`` if it is valid-but-not-an-object (#146 P1(b), PR #177 review
    round 3 v3 결함 7 / v5 결함 8).

    Accepts either a JSON **string** (what every SQLite-backed column --
    ``graph_nodes``/``graph_edges``/``doc_nodes``/``doc_sources`` -- always
    stores) or an **already-parsed ``dict``** (what PostgreSQL's JSONB
    columns hand back via psycopg2, and what every Mongo document field
    already is natively). Malformed JSON is treated the same as "empty" --
    ``{}`` -- matching this module's long-standing tolerance for corrupt
    rows; only a value that parses/normalizes to something that is not a
    ``dict`` at all (a bare JSON string, number, or array) returns ``None``.

    Extracted as its own function so ``resolve_row_pack_id`` (below, the
    ``graph_nodes``/``graph_edges``/doc self-inference path) and
    ``scripts/migrate_pack_ownership.py``'s ``_graph_twin_pack_map`` (which
    reads RAW ``_fetch_all`` rows straight off a graph store's own
    ``properties`` column, bypassing ``resolve_row_pack_id`` entirely for
    its ``actual=True`` ground-truth branch) share ONE normalization
    routine -- duplicating this dict-or-string handling in both places
    would give SQLite vs. PostgreSQL JSONB a chance to silently disagree
    between the two call sites.
    """
    if isinstance(props_raw, dict):
        return props_raw
    try:
        props = json.loads(props_raw) if props_raw else {}
    except (TypeError, ValueError):
        props = {}
    return props if isinstance(props, dict) else None


def resolve_row_pack_id(
    props_raw: Any, row: Any, assume_pack_id: str | None
) -> tuple[str | None, str]:
    """Decide the pack_id one ``graph_nodes``/``graph_edges``-shaped row
    should carry, using the EXACT precedence ``_process`` (below) has
    always applied. Extracted as a module-level function (#146 M P1-1) so
    the migration script's dry-run prediction and its post-apply
    verification call this SAME logic instead of re-deriving it -- a
    divergence between graph pack_id assignment and vector pack_id
    assignment becomes structurally impossible.

    ``props_raw`` may be a JSON **string** (SQLite storage -- always) OR an
    already-parsed **dict** (PostgreSQL JSONB via psycopg2, or a Mongo
    document's field, both handed to this same helper by #146 P1(b)'s doc
    backfill) -- see ``_normalize_props`` above for the exact contract.
    Both dry-run prediction and --apply's own writes go through this one
    normalization, so a divergence between the two modes over a dict vs.
    string input is structurally impossible.

    ``row`` must support ``row.keys()`` and ``row[key]`` (a ``sqlite3.Row``
    from the same ``SELECT <key_cols>, properties FROM <table>`` query
    ``_process`` runs, or an equivalent mapping-like object -- the doc
    backfill passes a plain ``{"node_id": ...}``/``{"source_id": ...}``
    dict, which supports the same ``.keys()``/``[key]`` protocol) -- only
    its ``*_id``-suffixed columns are consulted, as a last-resort inference
    source after ``props``'s own source_path/source_id/id keys.

    Returns ``(pack_id, reason)``:
      - ``(existing, "existing")`` -- ``props`` already carries a pack_id.
      - ``(inferred, "inferred")`` -- found via ``infer_pack_id_from_path``
        on props' source_path/source_id/id, or a ``row`` column ending in
        ``_id``.
      - ``(assume_pack_id, "assumed")`` -- nothing inferable; ``assume_pack_id``
        was given.
      - ``(None, "skipped-non-dict")`` -- ``props_raw`` normalized to a
        non-dict (malformed JSON is treated as ``{}``, which IS a dict --
        this only fires when the column holds valid JSON that isn't an
        object, e.g. a bare string or array).
      - ``(None, "skipped-unresolvable")`` -- valid dict, no pack_id,
        nothing inferable, and no ``assume_pack_id`` given.
    """
    props = _normalize_props(props_raw)
    if props is None:
        return None, "skipped-non-dict"

    existing = props.get("pack_id")
    if existing:
        return str(existing), "existing"

    for candidate_key in ("source_path", "source_id", "id"):
        value = props.get(candidate_key)
        if value:
            inferred = infer_pack_id_from_path(str(value))
            if inferred:
                return inferred, "inferred"

    for key in row.keys():
        if key.endswith("_id"):
            inferred = infer_pack_id_from_path(str(row[key]))
            if inferred:
                return inferred, "inferred"

    if assume_pack_id:
        return assume_pack_id, "assumed"
    return None, "skipped-unresolvable"


def _backfill_pack_ids_unlocked(
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
                pack_id, reason = resolve_row_pack_id(row["properties"], row, assume_pack_id)
                if reason == "existing":
                    continue
                if reason in ("skipped-non-dict", "skipped-unresolvable"):
                    summary[f"{table.split('_')[1]}_skipped"] += 1
                    continue
                # reason is "inferred" or "assumed" -- re-parse to get the
                # full props dict to write back (resolve_row_pack_id only
                # returns the decided pack_id, not the dict it came from).
                try:
                    props = json.loads(row["properties"]) if row["properties"] else {}
                except (TypeError, ValueError):
                    props = {}
                props["pack_id"] = pack_id
                summary[f"{table.split('_')[1]}_{reason}"] += 1
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


def backfill_pack_ids(
    db_path: str | Path,
    *,
    assume_pack_id: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Back-fill pack IDs under the lock for the database being changed."""
    if dry_run:
        return _backfill_pack_ids_unlocked(
            db_path, assume_pack_id=assume_pack_id, dry_run=True
        )
    with write_lock(str(Path(db_path).resolve().parent)):
        return _backfill_pack_ids_unlocked(
            db_path, assume_pack_id=assume_pack_id, dry_run=False
        )
