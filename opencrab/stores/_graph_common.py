"""
Shared pack-filter predicates + JSONB coercion + identifier validation for the
graph and doc stores.

VERBATIM-IDENTICAL SOURCES (diffed byte-for-byte before extraction — no
inter-copy differences found, no parameterisation needed):
    - ``_node_pack_id`` / ``_node_passes`` / ``_edge_passes``:
      opencrab/stores/local_graph_store.py (~30-75) and
      opencrab/stores/pg_graph_store.py (~74-107).
    - ``_as_dict``: opencrab/stores/pg_graph_store.py (~110) and
      opencrab/stores/pg_doc_store.py (~79).
    - ``IDENT_RE``: the identical ``^[A-Za-z_][A-Za-z0-9_]*$`` pattern
      previously duplicated as pg_graph_store._SCHEMA_IDENT_RE,
      pg_doc_store._SCHEMA_IDENT_RE, pg_vector_store._IDENT_RE, and
      sqlite_vec_store._IDENT_RE.

C3 UPDATE: opencrab/stores/kuzu_graph_store.py originally re-implemented the
    pack-filter 3-rule policy INLINE in ``find_neighbors()`` /
    ``_find_neighbors_1hop()``, on the assumption that its Cypher result rows
    didn't carry a standalone node/edge properties dict shaped for these
    helpers. Verification found ``_parse()`` (opencrab/stores/_json.py)
    already returns a plain dict for both, so no restructuring was needed —
    the module now imports and calls ``_node_passes``/``_edge_passes``
    directly. This also fixed two real divergences the inline copy had from
    the policy below: it compared a node's raw ``pack_id`` (no ``str()``
    cast, so a numeric pack_id failed a string ``pack_ids`` filter) and
    treated a falsy pack_id (e.g. ``""``) as a real foreign pack_id (always
    excluded) instead of "no pack_id" (governed by ``include_unpackaged``).
    See tests/test_store_seams_misc.py::TestKuzuPackFilterEquivalence.
"""

from __future__ import annotations

import json
import re
from typing import Any

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _node_pack_id(props: dict[str, Any]) -> str | None:
    """Top-level lookup mirroring the unified provenance helper.

    Imported lazily to avoid an import cycle (pack_provenance imports nothing
    from opencrab.stores, but keep this file dependency-light).
    """
    pid = props.get("pack_id") if isinstance(props, dict) else None
    if pid:
        return str(pid)
    return None


def _node_passes(
    props: dict[str, Any],
    pack_set: set[str] | None,
    include_unpackaged: bool,
) -> bool:
    if not pack_set:
        return True
    pid = _node_pack_id(props)
    if pid is None:
        return include_unpackaged
    return pid in pack_set


def _edge_passes(
    edge_props: dict[str, Any],
    src_passes: bool,
    dst_passes: bool,
    pack_set: set[str] | None,
) -> bool:
    """Apply the agreed edge filter rules.

    Rules (see plan §4):
      1. edge.pack_id in pack_set        -> pass (endpoints still must pass)
      2. edge.pack_id not in pack_set    -> always exclude
      3. edge has no pack_id             -> only pass when both endpoints pass
    """
    if not pack_set:
        return True
    edge_pid = _node_pack_id(edge_props) if isinstance(edge_props, dict) else None
    if edge_pid is not None:
        if edge_pid not in pack_set:
            return False
        return src_passes and dst_passes
    return src_passes and dst_passes


def _as_dict(value: Any) -> dict[str, Any]:
    """psycopg2 auto-decodes JSONB into dict/list; tolerate str/None too."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _merge_space(props: dict[str, Any], space_id: Any) -> dict[str, Any]:
    """Fold the graph store's ``space_id`` column into a node's properties.

    The SQL and Kuzu backends keep ``space`` in a dedicated column while
    ``upsert_node`` only injects ``id`` into ``properties`` -- so a node's space
    reaches ``properties`` solely when the caller happened to put it there. The
    GraphStore protocol (see ``_graph_protocol.export_nodes``) nonetheless
    documents the export shape as ``{"props": dict, "labels": [str]}`` with the
    space carried inside ``props``, which is how Neo4j behaves (space is a real
    property there). Every props-only consumer therefore mis-reads SQL/Kuzu
    exports: measured 2026-07-29 on a live store, 206,817 of 248,304 nodes
    (83.3%) had no ``space`` key, which silently dropped 88% of the candidates
    in the BM25 space filter (``ontology/query.py``), emptied the space field in
    ``ontology_list_nodes`` and pack export, and made the graph API fall back to
    "concept".

    Restoring it at read time is exact: on that same store the column was
    non-NULL for all 248,304 rows and never disagreed with ``props["space"]``
    where both existed.

    A ``space`` already present in ``props`` always wins -- the column is a
    fallback, never an override, so an explicit caller-supplied value survives.
    """
    if not space_id or props.get("space"):
        return props
    return {**props, "space": space_id}
