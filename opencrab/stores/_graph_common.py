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
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Node-property fields ``HybridQuery.keyword_search()``
# (opencrab/ontology/query.py) matches a keyword against. Shared by every
# graph-store backend's ``search_nodes()`` (_sql_graph_base.py,
# kuzu_graph_store.py) AND by query.py itself, so the field list can't drift
# between backends the way the old inline duplicate risked (issue #86).
KEYWORD_SEARCH_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "text",
    "title",
    "label",
    "summary",
)


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


def _space_passes(props: dict[str, Any], space_set: set[str] | None) -> bool:
    """Strict space-membership check (issue #52).

    Unlike the pack filter, there is no "include unspaced" escape hatch here
    — this mirrors the BM25 (``bm25.py``'s ``doc.get("space") not in
    spaces``) and vector (``sqlite_vec_store.py``) legs, which both treat a
    missing/foreign space as a hard exclude. ``props["space"]`` is expected
    to already be folded in via ``_merge_space`` for the SQL/Kuzu backends
    (native on Neo4jStore).
    """
    if not space_set:
        return True
    return props.get("space") in space_set


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


def _valid_space(value: Any) -> str | None:
    """The only value shape ``_space_passes`` (below) actually treats as a
    usable space identifier: a non-empty ``str`` (it does ``props.get(
    "space") in space_set``, a set of caller-supplied strings -- anything
    else, e.g. an int/dict/list/bool, can never be in that set regardless of
    its value). Used by both ``_normalize_space`` and ``_merge_space`` so a
    value the WRITE side considers "real" is exactly the same one the READ
    side does -- issue #118 codex review [3]: before this, both used bare
    Python truthiness on ``props.get("space")`` directly, which agreed on
    falsy-vs-truthy but not on TYPE (a truthy non-string, e.g. ``{"a": 1}``
    or ``5``, would have been treated as "a real space" by one code path's
    truthiness check while never matching anything at the actual filtering
    layer) -- and it keeps ``_normalize_space`` from ever writing a
    non-string value into the ``space_id`` TEXT column, which risked a PG
    bind-type error even where SQLite would have silently tolerated it.
    """
    return value if isinstance(value, str) and value else None


def _normalize_space(props: dict[str, Any], space_id: str | None) -> tuple[dict[str, Any], str | None]:
    """Reconcile a node's two space representations BEFORE a write lands
    (issue #118), so ``space_id`` (the column SQL/Cypher predicates filter
    on) and ``props["space"]`` can never diverge for any node written
    through ``upsert_node``/``upsert_nodes_batch`` again. Shared by
    ``_sql_graph_base.py`` (SQLite + PG), ``kuzu_graph_store.py``, and
    ``neo4j_store.py`` -- issue #118 codex review [2]: before this, Neo4j's
    own inline ``if space_id: props["space"] = space_id`` (explicit argument
    wins) and this function's precedence (JSON wins, matching ``_merge_space``
    below) picked DIFFERENT winners for the same conflicting input, so the
    same ``upsert_node(..., properties={"space": "B"}, space_id="A")`` call
    landed as space "B" on SQL/Kuzu but space "A" on Neo4j -- a cross-backend
    inconsistency worse than the single-store divergence this function
    exists to close. All three backends now call this one function, so they
    cannot drift again.

    PRECEDENCE: the explicit ``space_id`` ARGUMENT wins when it is a valid,
    truthy string -- not ``props["space"]``. This is the reverse of
    ``_merge_space``'s read-time fallback (still JSON-wins, see below) and
    is a deliberate choice, not an oversight: an explicit ``space=`` the
    caller passed to ``upsert_node`` is the least-surprising authority --
    silently letting an incidental key in an arbitrary ``properties`` dict
    override it is not. It is also what Neo4j already did (the actual
    precedent named in the issue), so this makes SQL/Kuzu match Neo4j
    instead of the other way around. Re-measured live 2026-08-06 (see PR
    description) across every combination the two values can disagree on
    (not just "both present and different" -- also JSON-truthy/column-NULL,
    the case codex review [1] pointed out the first measurement missed): 0
    of 252,604 graph_nodes actually diverge either way, so flipping the
    precedence does not touch any existing data.
    tests/test_graph_protocol_contract.py::TestExportCarriesSpace's
    ``test_explicit_props_space_is_not_overwritten_by_column`` pinned the
    OLD precedence and was renamed/flipped to
    ``test_explicit_space_id_argument_overwrites_props_space`` alongside
    this change (same file's ``TestSingleNodeReadsCarrySpace`` had a second,
    ``get_node``-level copy of the same old pin, flipped too).

    WARNING, NOT SILENT DISCARD (codex review [2]): when both sides are
    valid, truthy, and DIFFERENT, the losing value is not just dropped --
    it's logged, since a caller passing conflicting values for the same
    node is itself a sign of a bug somewhere upstream worth surfacing.

    Returns ``(props, space_id)`` -- ``props`` is the input dict unchanged
    (same object) unless a value needs folding in (matching ``_merge_space``'s
    own mutation contract); ``space_id`` is the effective value to persist,
    ALWAYS either ``None`` or a non-empty ``str`` (never a passthrough of a
    caller's invalid, e.g. non-string, argument -- see ``_valid_space``).

    SCOPE: this closes the divergence for every write going through
    ``upsert_node``/``upsert_nodes_batch``/Neo4jStore's own ``upsert_node``
    from here on. It does NOT retroactively fix rows already divergent
    before this shipped (measured 0 live, so nothing to backfill), and it
    does NOT cover writers that bypass these methods entirely -- two
    scripts (``scripts/build_nemotron_personas_korea_pack.py``,
    ``scripts/migrate_sqlite_to_pg.py``) INSERT into ``graph_nodes``
    directly (codex review [4]/[7]); left as-is per that review (a
    follow-up, not this fix's scope), noted here so this docstring does not
    overstate the invariant's reach.
    # ponytail: SQL/Cypher predicates (find_neighbors, export_nodes,
    # count_exported_nodes, _scan_space_matching) still filter on the raw
    # space_id column, not an effective-space expression -- a row written
    # out-of-band (the two scripts above, a direct DB edit, a restored
    # backup) could still be divergent and still starve a LIMIT-bound query
    # the way issue #118 describes. Upgrade path if that ever measures
    # nonzero again: either re-run the backfill query above, or push
    # COALESCE(NULLIF(json_extract(properties,'$.space'),''), space_id) into
    # the predicate (measured cost on this store's live 250k-row table: an
    # indexed `space_id = ?` SEARCH at ~2.5ms became a full `SCAN` at
    # ~439ms -- 173x -- so pair it with a second index on the JSON
    # expression, not a bare COALESCE wrap, if it's ever needed).
    """
    sid = _valid_space(space_id)
    pspace = _valid_space(props.get("space"))
    if sid and pspace and sid != pspace:
        logger.warning(
            "upsert_node: space_id=%r and properties['space']=%r disagree for "
            "node_id=%r; space_id (the explicit argument) wins.",
            sid, pspace, props.get("id"),
        )
    if sid:
        if pspace == sid:
            return props, sid
        return {**props, "space": sid}, sid
    if pspace:
        return props, pspace
    return props, None


def _merge_space(props: dict[str, Any], space_id: Any) -> dict[str, Any]:
    """Fold the graph store's ``space_id`` column into a node's properties.

    The SQL and Kuzu backends keep ``space`` in a dedicated column while
    ``upsert_node`` only injects ``id`` into ``properties`` -- so a node's space
    reaches ``properties`` solely when the caller happened to put it there.
    Neo4jStore, by contrast, writes ``props["space"] = space_id`` on upsert, so
    its reads carry the space natively and need no folding.

    Consumers read the space from props (e.g. ``_resolve_space`` in
    opencrab/pack/neo4j_export.py, which the protocol's export docstring names
    as the shape's consumer), so on the SQL/Kuzu backends they silently saw
    nothing: measured 2026-07-29 on a live store, 206,817 of 248,304 nodes
    (83.3%) had no ``space`` key, which dropped 88% of the candidates in the
    BM25 space filter (``ontology/query.py``), emptied the space field in
    ``ontology_list_nodes`` and pack export, and made the graph API fall back to
    "concept".

    Restoring it at read time is exact: on that same store the column was
    non-NULL for all 248,304 rows and never disagreed with ``props["space"]``
    where both were present.

    Precedence: a **truthy, string** ``props["space"]`` wins, so an explicit
    caller-supplied value survives. A falsy one (``""``/``None``) OR a
    non-string one (e.g. an int/dict a caller wrote directly, bypassing
    ``upsert_node``) is treated as absent and the column fills it -- an
    empty or wrong-shaped space is not a meaningful value, and leaving it
    would keep exactly the breakage this exists to fix. (issue #118 codex
    review [3]: this used to be a bare ``props.get("space")`` truthiness
    check, which agreed with ``_normalize_space``'s on falsy-vs-truthy but
    not on type -- ``_valid_space`` is now the single shared definition of
    "a real space value" both functions use, so a write-time and read-time
    judgement of the same row can no longer disagree just because one
    checked the type and the other didn't.)

    NOTE this is the OPPOSITE precedence from ``_normalize_space`` (which
    makes the ``space_id`` argument win) -- deliberate, not a leftover: this
    function's job is to be a read-time SAFETY NET for rows this fix cannot
    retroactively touch (written before it shipped, or by a script that
    bypasses ``upsert_node`` -- see ``_normalize_space``'s docstring
    "SCOPE"), so it keeps favoring whatever a human is most likely to have
    intentionally put in the JSON by hand. For every row written through
    ``upsert_node``/``upsert_nodes_batch`` after this fix, ``space_id`` and
    ``props["space"]`` are identical by construction, so this function's
    choice of winner never actually matters for them either way.

    Returns the input dict unchanged (same object) when there is nothing to
    fold; otherwise a new dict. The input is never mutated, so callers sharing a
    props dict cannot be polluted through this.
    """
    if not space_id or _valid_space(props.get("space")):
        return props
    return {**props, "space": space_id}
