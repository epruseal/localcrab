#!/usr/bin/env python3
"""Attribute pre-#146 pack data to the bootstrap owner (#146, execution 3
of #143's sequential-execution auth design).

Before #146 a "pack" had no registry row -- it existed purely because some
graph node's ``properties.pack_id`` matched a string. This script:

  1. Creates one ``packs`` registry row (owner=bootstrap local user,
     visibility=private) per DISTINCT pack_id already present in the graph
     store, using the EXACT pack_id (never suffixed -- suffixing is
     `pack_create`'s slug-negotiation behaviour for NEW packs; a migration
     must preserve the id already stamped on every existing row, or
     graph/doc/vector data would point at a pack_id the registry no longer
     recognises).
  2. Creates one catch-all "default" pack (also owned by the bootstrap
     user) and re-tags every graph node/edge, doc node/source row that has
     NO pack_id at all with it (#143 invariant 5: data outside a pack must
     not exist -- everything must resolve to *some* pack so read-path
     scoping, coming in #147, never silently hides legacy rows).

SCOPE (#146's time-box -- read before assuming a backend is covered):
  - Graph: only STORAGE_MODE=local (the SQLite ``graph.db`` file). This
    delegates to the EXISTING ``opencrab.ontology.pack_provenance.
    backfill_pack_ids`` (already exposed as ``opencrab packs
    backfill-pack-id``) rather than re-implementing pack_id backfill --
    that function already infers pack_id from any ``/packs/<id>/`` path
    pattern first, falling back to ``assume_pack_id`` (this script passes
    the default pack) only when inference finds nothing. pg/kuzu/docker
    graph backends are skipped with a warning: pack_provenance.py is
    SQLite-file-specific (same scope the existing CLI command already
    has), and re-implementing an equivalent for Cypher/PG is out of scope
    here.
  - Doc: SQL-backed (local/kuzu/pg) via each store's own
    ``_table``/``_fetch_one``/``_exec_write`` hooks (shared by
    LocalSQLDocStore and PgDocStore, see _sql_doc_base.py), or Mongo
    (docker) via a native ``update_many``.
  - Vector: BEST-EFFORT ONLY. Neither SqliteVecStore nor PgVectorStore
    exposes a "list every row" primitive (only ``get_by_id`` / KNN
    ``query``), so this script can only re-tag vector rows whose node_id
    it already knows about -- the ones it just found missing pack_id in
    the GRAPH store. A vector row with no graph/doc counterpart at all
    (only reachable via the legacy `text_as_node=False` ingest path, which
    has stamped pack_id on every write since #52) is not covered. Re-tags
    via delete+reinsert (``upsert_texts``, which RE-EMBEDS the text) since
    vec0 virtual tables do not support UPDATEing the partition-key column
    in place (see sqlite_vec_store.py's module docstring) and pgvector's
    upsert path is shared code with it.

SAFETY:
  - Defaults to dry-run. Pass --apply to write anything.
  - --apply requires --backup-to <dir> (local/kuzu SQLite files are copied
    there via the same online .backup() pattern as
    migrate_add_binary_quantization.py) unless --skip-backup is passed
    explicitly (the only option for pg/docker mode, where there is no
    single file this script can safely copy -- take a pg_dump/snapshot
    yourself first).
  - Every write is COUNTED before it runs (an expected-count assertion),
    and re-checked against the actual rowcount afterwards -- a mismatch
    aborts immediately rather than silently under- or over-writing.
  - Idempotent: rows already carrying a pack_id, and pack_ids already
    registered, are left untouched on a re-run.
  - Every run ends with a structured per-stage report (graph_backfill,
    registry_enumeration, docs_backfill, vector_backfill), each one of
    clean (nothing to do) / applied (found and, in --apply mode, wrote
    something) / skipped (backend out of SCOPE above) / failed (raised --
    the run stops there, later stages do not run). Exit code: 1 if any
    stage failed; else 3 if graph_backfill or docs_backfill was skipped
    (#147 must NOT deploy against a code-3 run -- one of the two backends
    #147's read-path scoping depends on was never even inspected);
    vector_backfill's skip does NOT gate the exit code -- it is
    best-effort FOREVER per the Vector SCOPE note above, regardless of
    store availability, never a completeness guarantee; else 0.

Usage:
    python scripts/migrate_pack_ownership.py                 # dry-run
    python scripts/migrate_pack_ownership.py --apply --backup-to /path/to/backups/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_PACK_ID = "default"
DEFAULT_PACK_TITLE = "Default pack (pre-#146 legacy data)"


# ---------------------------------------------------------------------------
# Registry bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_owner_id(sql: Any) -> str:
    from opencrab.auth import get_local_user

    principal = get_local_user(sql)
    if principal is None:
        print(
            "ERROR: no local user is bootstrapped. Run 'opencrab init' first "
            "-- it creates the local user this migration attributes legacy "
            "data to.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return principal.user_id


def _registered_pack_ids(sql: Any) -> set[str]:
    from sqlalchemy import text

    with sql._engine.connect() as conn:
        rows = conn.execute(text("SELECT pack_id FROM packs")).fetchall()
    return {r[0] for r in rows}


def _registered_pack_owners(sql: Any) -> dict[str, str]:
    """Every registered ``pack_id -> owner_id`` -- ``_registered_pack_ids``
    only reports which slugs are taken, not by whom; the foreign-owner
    overlap check in ``_register_graph_packs`` (#177 review round 2, gate
    W v3) needs the owner to tell "bootstrap re-run" (silently skip, same
    as before) from "someone else already holds this exact slug" (abort by
    default -- see that function's docstring)."""
    from sqlalchemy import text

    with sql._engine.connect() as conn:
        rows = conn.execute(text("SELECT pack_id, owner_id FROM packs")).fetchall()
    return {r[0]: r[1] for r in rows}


def _graph_edge_pack_ids(graph: Any) -> tuple[set[str], bool]:
    """Distinct pack_id found in ``graph_edges.properties`` (#146 M P1-2).

    Uses a raw ``SELECT properties`` + Python ``json.loads`` per row, NOT
    SQL ``json_extract`` -- ``json_extract`` raises on malformed JSON,
    while ``pack_provenance._process`` (the actual backfill) already
    tolerates malformed JSON via a ``json.loads`` try/except. Enumeration
    must use the same defense or it would disagree with the backfill about
    which edges even have a pack_id.

    Returns ``(pack_ids, enumerable)``. ``enumerable`` is False when
    ``graph`` has no ``_table``/``_fetch_all`` (a non-SQL-backed wrapper,
    e.g. Kuzu/Neo4j) -- re-implementing edge enumeration for a Cypher-style
    backend is #182 scope, not this migration's; callers must not treat an
    empty set as "no edge pack_ids" in that case.
    """
    if not (hasattr(graph, "_table") and hasattr(graph, "_fetch_all")):
        return set(), False
    table = graph._table("graph_edges")
    rows = graph._fetch_all(f"SELECT properties FROM {table}", {})  # noqa: S608
    pack_ids: set[str] = set()
    for (raw,) in rows:
        try:
            props = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            props = {}
        if isinstance(props, dict):
            pid = props.get("pack_id")
            if pid:
                pack_ids.add(str(pid))
    return pack_ids, True


def _register_graph_packs(
    sql: Any,
    graph: Any,
    owner_id: str,
    apply: bool,
    node_pack_map: dict[str, str] | None = None,
    ambiguous_nodes: dict[str, list[str]] | None = None,
    accept_foreign_owned_packs: bool = False,
) -> dict[str, Any]:
    """One registry row per distinct graph pack_id not already registered.

    Uses the exact pack_id via ``_insert_pack`` (no quiet-suffixing --
    see module docstring). Called AFTER the graph backfill step so a
    pack_id ``backfill_pack_ids`` recovered via path-inference (not just
    the ``assume_pack_id`` default) also gets a registry row -- a fresh
    ``graph.list_packs()`` call here sees whatever the backfill step just
    wrote (in --apply mode) or would have found unchanged (dry-run).

    Candidates are the UNION of node pack_ids (``graph.list_packs()``, a
    GROUP BY over ``graph_nodes``), edge pack_ids (``_graph_edge_pack_ids``,
    a raw ``graph_edges`` scan, #146 M P1-2), and -- #177 review round 2,
    "C v2" -- every pack_id ``node_pack_map``/``ambiguous_nodes`` predicts
    or resolves for the CALLER's node_ids. In dry-run mode this last part
    is load-bearing: ``graph.list_packs()`` only sees pack_ids already
    written to ``graph_nodes``, so a node whose pack_id would be
    PATH-INFERRED by the (not-yet-run, dry-run) backfill is otherwise
    invisible to this enumeration -- the caller's dry-run report would then
    under-count ``unregistered``. In --apply mode ``node_pack_map`` is the
    POST-backfill ground truth (see main()), so every value in it is
    already present in ``graph.list_packs()`` too and this union is a
    no-op there. Ambiguous node_ids contribute BOTH of their conflicting
    pack_ids (not just one) -- ``_backfill_vector`` excludes the node_id
    itself from any vector write, but ``_backfill_graph`` still writes a
    pack_id to EACH of that node_id's rows individually, so both values do
    land in the registry on a real apply.

    LIMITATION (documented in the dry-run report, not silently accepted):
    this only covers NODE path-inference (``_predict_node_pack_map``) --
    there is no equivalent predictor for EDGE path-inference, so a dry-run
    still cannot foresee a pack_id an edge would only acquire once the real
    backfill runs (#182 scope, same edge-inference gap ``_graph_edge_pack_ids``
    already documents for its OWN enumeration).

    An edge whose endpoints are unpacked or foreign-packed but whose OWN
    properties carry a pack_id would otherwise never reach the registry,
    and #147's read-path scoping would then make it vanish silently.

    Foreign-owner overlap guard (#177 review round 2, "B v2"): a graph
    pack_id ALREADY registered to someone other than ``owner_id`` is
    ambiguous after the fact -- it looks identical whether that row is a
    legitimate user pack (created after this migration first ran, no
    conflict) or a slug that got squatted before this migration ever ran
    (the legacy graph content now silently belongs to the squatter once
    this function just skips it as "already registered"). This function
    cannot tell those two apart, so it does not guess: by default it
    raises (dry-run AND --apply alike) so an operator decides.
    ``accept_foreign_owned_packs=True`` is that explicit operator decision
    -- it downgrades the raise to a printed warning and proceeds exactly
    as before (the foreign-owned pack_id is left alone, same as any other
    already-registered pack_id).
    """
    from opencrab.pack.ownership import _insert_pack

    if not getattr(graph, "available", False):
        print("  graph store unavailable -- skipping pack-id enumeration")
        return {
            "graph_distinct_packs": 0,
            "unregistered": 0,
            "created": 0,
            "edges_enumerable": True,
            "foreign_owned": [],
        }

    already_owners = _registered_pack_owners(sql)
    rows = graph.list_packs(min_nodes=1)
    node_meta = {r["pack_id"]: r for r in rows if r.get("pack_id")}
    edge_pack_ids, edges_enumerable = _graph_edge_pack_ids(graph)
    predicted_pack_ids: set[str] = set((node_pack_map or {}).values())
    for pids in (ambiguous_nodes or {}).values():
        predicted_pack_ids.update(pids)
    all_pack_ids = set(node_meta) | edge_pack_ids | predicted_pack_ids

    foreign_owned = sorted(
        pid
        for pid in all_pack_ids
        if pid in already_owners and already_owners[pid] != owner_id
    )
    if foreign_owned:
        detail = ", ".join(f"{pid!r} (owner={already_owners[pid]!r})" for pid in foreign_owned)
        if not accept_foreign_owned_packs:
            raise RuntimeError(
                f"{len(foreign_owned)} graph pack_id(s) already exist in the "
                f"graph store's content but are registered to a DIFFERENT "
                f"owner than the bootstrap owner {owner_id!r}: {detail}. "
                "This can happen if remote pack registration was opened "
                "before this migration ran (someone else claimed one of "
                "these exact slugs first) -- or, more benignly, a re-run "
                "colliding with a genuinely new user pack. This script "
                "cannot tell those apart automatically -- it needs a human "
                "to verify. If these rows are confirmed to be legitimate "
                "(e.g. a safe re-run, or the owner is expected to hold this "
                "legacy content), re-run with --accept-foreign-owned-packs "
                "to skip them and proceed with everything else unchanged."
            )
        print(
            f"  ! --accept-foreign-owned-packs: skipping {len(foreign_owned)} "
            f"graph pack_id(s) already registered to a different owner: {detail}"
        )

    candidates = sorted(pid for pid in all_pack_ids if pid not in already_owners)
    print(
        f"  graph distinct pack_id: {len(all_pack_ids)} total "
        f"(nodes={len(node_meta)}, edges={len(edge_pack_ids)}, "
        f"predicted={len(predicted_pack_ids)}), "
        f"{len(candidates)} not yet in the registry"
    )
    if not apply:
        print(
            "  note: edge-inferred pack_id (path-inference on graph_edges, "
            "no predictor exists for it -- see this function's docstring "
            "LIMITATION) is not visible to this dry-run report; it is only "
            "registered once a real --apply backfill has run."
        )
    created = 0
    if apply:
        for pid in candidates:
            meta = node_meta.get(pid)
            if _insert_pack(
                sql,
                pid,
                owner_id,
                (meta.get("sample_title") or None) if meta else None,
                (meta.get("sample_description") or None) if meta else None,
                None,
            ):
                created += 1
        if created != len(candidates):
            raise RuntimeError(
                f"expected to register {len(candidates)} graph pack_ids, "
                f"actually registered {created} -- a concurrent writer may "
                "have raced this script; re-run to confirm before trusting "
                "the registry."
            )
    return {
        "graph_distinct_packs": len(all_pack_ids),
        "unregistered": len(candidates),
        "created": created,
        "edges_enumerable": edges_enumerable,
        "foreign_owned": foreign_owned,
    }


def _ensure_default_pack(sql: Any, owner_id: str, apply: bool) -> tuple[str, bool]:
    """Returns ``(DEFAULT_PACK_ID, was_pending)`` -- ``was_pending`` is True
    when the default pack row did NOT already exist at the start of this
    call (dry-run or --apply alike), so callers can tell "nothing to do"
    from "found something" without re-querying.

    Raises ``RuntimeError`` -- unconditionally, dry-run and --apply alike,
    and regardless of whether there is any unattributed legacy data to
    migrate right now -- when a ``default`` row already exists but is owned
    by someone other than ``owner_id`` (#146 M P1-3). ``default`` is the
    reserved catch-all identity #147's read-path scoping and #148's
    pack-less-write default both resolve to; silently reusing whoever
    already squats that slug would hand them every unattributed legacy row
    (a privilege escalation), and even a "no legacy data today" run would
    leave the reserved-identity invariant broken for every pack-less write
    that comes after it. This migration never auto-renames or auto-steals
    the row -- an operator decision, not a script's, per SAFETY above.
    """
    from opencrab.pack.ownership import _insert_pack, get_pack

    existing = get_pack(sql, DEFAULT_PACK_ID)
    if existing is not None:
        if existing["owner_id"] != owner_id:
            raise RuntimeError(
                f"default pack {DEFAULT_PACK_ID!r} is already registered to "
                f"owner {existing['owner_id']!r}, not the bootstrap owner "
                f"{owner_id!r}. 'default' is the reserved catch-all identity "
                "every pack-less write resolves to (#147 read-path scoping, "
                "#148 pack-less-write default) -- reusing someone else's row "
                "here would hand them every unattributed legacy row. This "
                "aborts even when there is no legacy data to attribute right "
                "now, because the reserved-identity invariant must hold "
                "going forward regardless of today's row count. Resolve by "
                "renaming/transferring the squatting pack away from "
                f"{DEFAULT_PACK_ID!r}, or by confirming its current owner is "
                "genuinely meant to be the catch-all identity and "
                "re-bootstrapping the local user to match -- then re-run "
                "this script."
            )
        print(f"  default pack {DEFAULT_PACK_ID!r} already registered (owner={existing['owner_id']})")
        return DEFAULT_PACK_ID, False
    print(f"  default pack {DEFAULT_PACK_ID!r} not yet registered")
    if apply:
        if not _insert_pack(sql, DEFAULT_PACK_ID, owner_id, DEFAULT_PACK_TITLE, None, None):
            raise RuntimeError(
                f"default pack {DEFAULT_PACK_ID!r} was created by someone else "
                "between the check above and this insert -- re-run the script."
            )
    return DEFAULT_PACK_ID, True


# ---------------------------------------------------------------------------
# SQL-backed store backfill (graph_nodes/graph_edges/doc_nodes/doc_sources)
# ---------------------------------------------------------------------------


def _is_pg_dialect(store: Any) -> bool:
    from opencrab.stores._sql_dialect import POSTGRES

    return getattr(store, "_dialect", None) is POSTGRES


def _missing_and_set_sql(col: str, is_pg: bool) -> tuple[str, str]:
    if is_pg:
        missing = f"({col}->>'pack_id') IS NULL OR ({col}->>'pack_id') = ''"
        set_expr = f"COALESCE({col}, '{{}}'::jsonb) || jsonb_build_object('pack_id', :pid)"
    else:
        missing = (
            f"json_extract({col}, '$.pack_id') IS NULL "
            f"OR json_extract({col}, '$.pack_id') = ''"
        )
        set_expr = f"json_set(COALESCE({col}, '{{}}'), '$.pack_id', :pid)"
    return missing, set_expr


def _backfill_sql_table(
    store: Any,
    table_name: str,
    prop_col: str,
    id_col: str | None,
    default_pack_id: str,
    apply: bool,
) -> dict[str, Any]:
    """Bulk-backfill missing pack_id on one JSON-properties table via the
    store's own ``_table``/``_fetch_one``/``_fetch_all``/``_exec_write``
    hooks (shared by every SQL-backed graph/doc store -- see
    ``_sql_graph_base.py`` / ``_sql_doc_base.py``), so this works
    identically on SQLite and PostgreSQL without touching either store's
    connection machinery directly.

    ``id_col=None`` (graph_edges has no single-column identifier -- its PK
    is the 5-column tuple from_type/from_id/relation/to_type/to_id) skips
    collecting per-row ids and just counts; used by callers (vector
    backfill) that need the affected ids from OTHER tables only.
    """
    table = store._table(table_name)
    is_pg = _is_pg_dialect(store)
    missing_where, set_expr = _missing_and_set_sql(prop_col, is_pg)

    total = store._fetch_one(f"SELECT COUNT(*) FROM {table}", {})[0]  # noqa: S608
    if id_col:
        missing_ids = [
            r[0]
            for r in store._fetch_all(f"SELECT {id_col} FROM {table} WHERE {missing_where}", {})  # noqa: S608
        ]
        missing = len(missing_ids)
    else:
        missing_ids = []
        missing = store._fetch_one(f"SELECT COUNT(*) FROM {table} WHERE {missing_where}", {})[0]  # noqa: S608
    print(f"  {table_name}: total={total} missing_pack_id={missing}")

    updated = 0
    if apply and missing:
        updated = store._exec_write(
            f"UPDATE {table} SET {prop_col} = {set_expr} WHERE {missing_where}",  # noqa: S608
            {"pid": default_pack_id},
        )
        if updated != missing:
            raise RuntimeError(
                f"{table_name}: expected to backfill {missing} rows, actually "
                f"updated {updated} -- aborting rather than trusting a "
                "partial write."
            )
    return {"total": total, "missing": missing, "updated": updated, "missing_ids": missing_ids}


def _graph_missing_node_ids(graph: Any) -> list[str]:
    """node_ids currently missing pack_id in graph_nodes -- captured BEFORE
    the backfill runs, so the vector best-effort step (below) knows which
    ids to re-check. Local-mode-only (see ``_backfill_graph``)."""
    if not hasattr(graph, "_dialect"):
        return []
    is_pg = _is_pg_dialect(graph)
    missing_where, _ = _missing_and_set_sql("properties", is_pg)
    table = graph._table("graph_nodes")
    return [
        r[0]
        for r in graph._fetch_all(f"SELECT node_id FROM {table} WHERE {missing_where}", {})  # noqa: S608
    ]


def _local_graph_db_path(settings: Any) -> Path | None:
    """The local ``graph.db`` path, or ``None`` when out of the local-mode-
    only SCOPE this migration's SQLite-specific graph helpers share (a
    non-local ``storage_mode``, or the file not existing yet) -- shared by
    ``_backfill_graph`` and the node-pack-map builders below (#146 M P1-1)
    so the same guard isn't duplicated with a chance to drift."""
    if settings.storage_mode != "local":
        return None
    db_path = Path(settings.local_data_dir) / "graph.db"
    return db_path if db_path.exists() else None


def _backfill_graph(settings: Any, default_pack_id: str, apply: bool) -> dict[str, Any]:
    """Delegates to the EXISTING ``opencrab.ontology.pack_provenance.
    backfill_pack_ids`` (same function ``opencrab packs backfill-pack-id``
    already exposes) rather than re-implementing pack_id backfill -- see
    module docstring SCOPE for why this is local-mode-only."""
    from opencrab.ontology.pack_provenance import backfill_pack_ids

    db_path = _local_graph_db_path(settings)
    if db_path is None:
        if settings.storage_mode != "local":
            print(
                f"  storage_mode={settings.storage_mode!r} has no SQLite graph.db -- "
                "skipping graph node/edge pack_id backfill (see module docstring SCOPE)."
            )
        else:
            print(f"  {Path(settings.local_data_dir) / 'graph.db'} does not exist -- skipping.")
        return {"skipped": True}

    summary = backfill_pack_ids(db_path, assume_pack_id=default_pack_id, dry_run=not apply)
    print(f"  graph.db backfill_pack_ids: {summary}")
    return summary


def _split_ambiguous(
    seen: dict[str, set[str]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """The graph PK is ``(node_type, node_id)``, so several graph rows may
    legally share one node_id -- but the vector store's identity is node_id
    ALONE (``upsert_texts(ids=[node_id])``), so when those rows resolve to
    DIFFERENT pack_ids there is no single correct value a vector row could
    carry (#146 M P1-1 review round 2). Such node_ids are excluded from the
    map (= no vector upsert) and reported as ambiguous; agreeing duplicates
    collapse to their one shared value."""
    resolved: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    for node_id, packs in seen.items():
        if len(packs) == 1:
            resolved[node_id] = next(iter(packs))
        else:
            ambiguous[node_id] = sorted(packs)
    return resolved, ambiguous


def _predict_node_pack_map(
    db_path: Path, node_ids: list[str], assume_pack_id: str
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """dry-run prediction (#146 M P1-1): for each ``node_id`` (read at its
    CURRENT, pre-backfill state), resolve its would-be pack_id via
    ``pack_provenance.resolve_row_pack_id`` -- the SAME helper the real
    backfill (``_process``) uses, so a divergence between this prediction
    and what actually gets written is structurally impossible. A node_id
    that resolves to ``None`` (``skipped-non-dict``/``skipped-unresolvable``
    -- backfill_pack_ids could not attribute it either) is excluded: vector
    backfill must not write a guessed pack_id for a row the graph backfill
    itself left unattributed.
    """
    import sqlite3

    from opencrab.ontology.pack_provenance import resolve_row_pack_id

    if not node_ids:
        return {}, {}
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        placeholders = ", ".join("?" for _ in node_ids)
        cur = conn.cursor()
        cur.execute(
            f"SELECT node_id, properties FROM graph_nodes WHERE node_id IN ({placeholders})",  # noqa: S608
            node_ids,
        )
        seen: dict[str, set[str]] = {}
        for row in cur.fetchall():
            pack_id, _reason = resolve_row_pack_id(row["properties"], row, assume_pack_id)
            if pack_id is not None:
                seen.setdefault(row["node_id"], set()).add(pack_id)
        return _split_ambiguous(seen)
    finally:
        conn.close()


def _read_actual_node_pack_ids(
    db_path: Path, node_ids: list[str]
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """apply-path ground truth (#146 M P1-1): a direct re-query of
    ``graph_nodes`` for ``node_ids``' CURRENT pack_id, called AFTER
    ``backfill_pack_ids`` has run -- real measurement, not a
    ``resolve_row_pack_id`` prediction. A node_id still lacking pack_id
    (left unattributed by the backfill) is simply absent from the result,
    matching ``_predict_node_pack_map``'s own exclusion of such rows."""
    import json
    import sqlite3

    if not node_ids:
        return {}, {}
    conn = sqlite3.connect(db_path)
    try:
        placeholders = ", ".join("?" for _ in node_ids)
        cur = conn.cursor()
        cur.execute(
            f"SELECT node_id, properties FROM graph_nodes WHERE node_id IN ({placeholders})",  # noqa: S608
            node_ids,
        )
        seen: dict[str, set[str]] = {}
        for node_id, raw in cur.fetchall():
            try:
                props = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                props = {}
            if isinstance(props, dict) and props.get("pack_id"):
                seen.setdefault(node_id, set()).add(str(props["pack_id"]))
        return _split_ambiguous(seen)
    finally:
        conn.close()


def _backfill_doc(docs: Any, default_pack_id: str, apply: bool) -> dict[str, Any]:
    if not getattr(docs, "available", True):
        print("  doc store unavailable -- skipping.")
        return {"skipped": True}
    if hasattr(docs, "_dialect"):
        nodes = _backfill_sql_table(docs, "doc_nodes", "properties", "node_id", default_pack_id, apply)
        sources = _backfill_sql_table(docs, "doc_sources", "metadata", "source_id", default_pack_id, apply)
        return {"doc_nodes": nodes, "doc_sources": sources}
    if hasattr(docs, "_db"):
        return _backfill_mongo(docs._db, default_pack_id, apply)
    print("  doc store is neither SQL- nor Mongo-backed -- skipping.")
    return {"skipped": True}


def _backfill_mongo(db: Any, default_pack_id: str, apply: bool) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for collection, field_root in (("nodes", "properties"), ("sources", "metadata")):
        coll = db[collection]
        total = coll.estimated_document_count()
        missing_q = {
            "$or": [
                {f"{field_root}.pack_id": {"$exists": False}},
                {f"{field_root}.pack_id": None},
                {f"{field_root}.pack_id": ""},
            ]
        }
        missing = coll.count_documents(missing_q)
        print(f"  mongo.{collection}: total={total} missing_pack_id={missing}")
        updated = 0
        if apply and missing:
            result = coll.update_many(missing_q, {"$set": {f"{field_root}.pack_id": default_pack_id}})
            updated = result.modified_count
            if updated != missing:
                raise RuntimeError(
                    f"mongo.{collection}: expected to backfill {missing} docs, "
                    f"actually updated {updated} -- aborting."
                )
        results[collection] = {"total": total, "missing": missing, "updated": updated}
    return results


# ---------------------------------------------------------------------------
# Vector store — best-effort, keyed off graph node_ids found missing above
# ---------------------------------------------------------------------------


def _backfill_vector(
    vector: Any, node_ids: list[str], node_pack_map: dict[str, str], apply: bool
) -> dict[str, int]:
    """``node_pack_map`` (built by ``_predict_node_pack_map``/
    ``_read_actual_node_pack_ids``) supplies each node_id's ACTUAL graph
    pack_id (#146 M P1-1) -- a node that graph backfill path-inferred into
    ``pack-x`` must have its vector row tagged ``pack-x`` too, not a
    blanket ``default_pack_id``, or graph and vector end up in different
    packs for the same row. A node_id absent from the map (graph itself
    could not attribute a pack_id to it) is skipped entirely: no vector
    upsert is issued for it, since guessing here would only disagree with
    graph's own "unattributed" verdict.
    """
    if not (hasattr(vector, "get_by_id") and hasattr(vector, "upsert_texts")):
        print("  vector store has no get_by_id/upsert_texts -- skipping (out of scope, see module docstring).")
        return {"checked": 0, "missing": 0, "updated": 0}
    if not getattr(vector, "available", False):
        print("  vector store unavailable -- skipping.")
        return {"checked": 0, "missing": 0, "updated": 0}

    missing_ids: list[str] = []
    for node_id in node_ids:
        if node_id not in node_pack_map:
            continue
        doc = vector.get_by_id(node_id)
        if doc is None:
            continue
        meta = doc.get("metadata") or {}
        if not meta.get("pack_id"):
            missing_ids.append(node_id)
    print(f"  vector: checked {len(node_ids)} known ids, {len(missing_ids)} missing pack_id")

    updated = 0
    if apply and missing_ids:
        for node_id in missing_ids:
            doc = vector.get_by_id(node_id)
            if doc is None:
                continue
            meta = dict(doc.get("metadata") or {})
            meta["pack_id"] = node_pack_map[node_id]
            vector.upsert_texts([doc.get("document") or ""], [meta], [node_id])
            updated += 1
        if updated != len(missing_ids):
            raise RuntimeError(
                f"vector: expected to backfill {len(missing_ids)} rows, "
                f"actually updated {updated} -- aborting."
            )
    return {"checked": len(node_ids), "missing": len(missing_ids), "updated": updated}


# ---------------------------------------------------------------------------
# Backup (mandatory before --apply, local/kuzu SQLite only)
# ---------------------------------------------------------------------------


def _backup_sqlite_files(local_data_dir: str, backup_to: str) -> list[str]:
    import sqlite3

    dest_dir = Path(backup_to)
    dest_dir.mkdir(parents=True, exist_ok=True)
    backed_up = []
    for name in ("opencrab.db", "graph.db", "doc_store.db"):
        src = Path(local_data_dir) / name
        if not src.is_file():
            continue
        dst = dest_dir / name
        if dst.exists():
            print(f"! backup target already exists, refusing to overwrite: {dst}", file=sys.stderr)
            raise SystemExit(2)
        src_conn = sqlite3.connect(str(src))
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
        print(f"  backed up {src} -> {dst}")
        backed_up.append(str(dst))
    return backed_up


# ---------------------------------------------------------------------------
# Structured per-stage outcomes
# ---------------------------------------------------------------------------
#
# Every stage below ends in exactly one of four outcomes:
#   clean   -- nothing needed doing.
#   applied -- something needed doing (dry-run: would be written;
#              --apply: was written).
#   skipped -- the stage's backend is out of scope for this migration (see
#              module docstring SCOPE: non-local graph, an unavailable
#              store, or a vector store missing get_by_id/upsert_texts).
#   failed  -- the stage raised; main() catches it, stops running further
#              stages (an inconsistent partial state is worse than an
#              incomplete one), and returns 1.
#
# Exit code: every stage clean/applied -> 0; any stage skipped -> 3 (see
# module docstring SAFETY -- #147 must not deploy against a code 3 run,
# since it means some backend's data was never even inspected); any stage
# failed -> 1.


def _stage_outcome(stats: dict[str, Any], pending_keys: tuple[str, ...]) -> tuple[str, str]:
    """clean/applied/skipped for a stage whose stats dict already marks
    itself ``{"skipped": True}`` when out of scope (``_backfill_graph``,
    ``_backfill_doc``) -- ``pending_keys`` are the stats fields that count
    as "something needed doing"."""
    if stats.get("skipped"):
        return "skipped", "backend out of scope for this migration (see module docstring SCOPE)"
    pending = sum(int(stats.get(k) or 0) for k in pending_keys)
    if pending == 0:
        return "clean", "nothing to do"
    return "applied", f"{pending} item(s) needed backfilling"


def _docs_stage_outcome(stats: dict[str, Any]) -> tuple[str, str]:
    """Like ``_stage_outcome`` but for ``_backfill_doc``'s nested shape
    (``{"doc_nodes": {...}, "doc_sources": {...}}`` for SQL-backed stores,
    ``{"nodes": {...}, "sources": {...}}`` for Mongo -- both are dicts of
    sub-dicts carrying their own "missing" count)."""
    if stats.get("skipped"):
        return "skipped", "doc store unavailable or not SQL/Mongo-backed"
    pending = sum(
        int(sub.get("missing") or 0) for sub in stats.values() if isinstance(sub, dict)
    )
    if pending == 0:
        return "clean", "nothing to do"
    return "applied", f"{pending} row(s) needed backfilling"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Perform writes. Without this flag the script is dry-run.")
    parser.add_argument("--backup-to", default=None, help="Directory to copy local SQLite files into before writing (required with --apply unless --skip-backup).")
    parser.add_argument("--skip-backup", action="store_true", help="Explicitly skip the mandatory pre-migration backup (required for non-SQLite deployments, since this script cannot back those up itself).")
    parser.add_argument(
        "--accept-foreign-owned-packs",
        action="store_true",
        help=(
            "Explicit operator override (#177 review round 2 gate W v3): "
            "without it, a graph pack_id that already has registry content "
            "AND is registered to someone other than the bootstrap owner "
            "aborts the run (rc 1, dry-run and --apply alike) rather than "
            "silently skipping it -- see _register_graph_packs' docstring "
            "for why this can't be decided automatically. Pass this once "
            "you have confirmed the foreign-owned pack_id(s) are legitimate "
            "(e.g. a safe re-run after remote pack registration opened)."
        ),
    )
    args = parser.parse_args(argv)

    from opencrab.config import get_settings
    from opencrab.stores.factory import (
        make_doc_store,
        make_graph_store,
        make_sql_store,
        make_vector_store,
    )

    settings = get_settings()
    print(f"Mode: {'APPLY (writes)' if args.apply else 'DRY-RUN'}")
    print(f"storage_mode: {settings.storage_mode}")

    if args.apply:
        if args.backup_to:
            if not settings.is_local:
                print(
                    "! --backup-to only backs up local SQLite files; this "
                    f"deployment's storage_mode is {settings.storage_mode!r}. "
                    "Take your own snapshot, then re-run with --skip-backup.",
                    file=sys.stderr,
                )
                return 2
            print("Backing up local SQLite files...")
            _backup_sqlite_files(settings.local_data_dir, args.backup_to)
        elif args.skip_backup:
            print("! --skip-backup passed: proceeding WITHOUT a backup.")
        else:
            print(
                "! --apply requires --backup-to <dir> (local/kuzu SQLite) or "
                "--skip-backup (explicit override -- required for pg/docker, "
                "where you must take your own backup first). Refusing to "
                "run.",
                file=sys.stderr,
            )
            return 2

    sql = make_sql_store(settings)
    if not sql.available:
        print("ERROR: SQL store unavailable -- run 'opencrab init' first.", file=sys.stderr)
        return 2
    owner_id = _bootstrap_owner_id(sql)
    print(f"Bootstrap owner: {owner_id}")

    graph = make_graph_store(settings)
    docs = make_doc_store(settings)
    vector = make_vector_store(settings)

    # {stage_name: {"outcome": ..., "reason": ...}} -- populated as each
    # stage below runs, printed in full in the Summary regardless of where
    # a failure stops the sequence.
    stage_outcomes: dict[str, dict[str, str]] = {}

    try:
        print("\n1) Ensuring the default (catch-all) pack is registered...")
        default_pack_id, default_pending = _ensure_default_pack(sql, owner_id, args.apply)

        print("\n2) Backfilling graph rows with no pack_id...")
        # dict.fromkeys: dedupe while keeping order -- duplicate node_ids
        # (legal: the graph PK is (node_type, node_id)) must not trigger
        # duplicate vector upserts.
        node_ids_needing_check = list(dict.fromkeys(_graph_missing_node_ids(graph)))
        local_db_path = _local_graph_db_path(settings)
        # Predicted BEFORE the backfill writes anything -- resolve_row_pack_id
        # applied to each node's pre-backfill state (#146 M P1-1).
        node_pack_map, ambiguous_nodes = (
            _predict_node_pack_map(local_db_path, node_ids_needing_check, default_pack_id)
            if local_db_path is not None
            else ({}, {})
        )
        graph_stats = _backfill_graph(settings, default_pack_id, args.apply)
        if args.apply and local_db_path is not None:
            # Ground truth AFTER the write -- verifies the prediction above
            # actually matches what backfill_pack_ids wrote, and is what
            # vector backfill (step 5) uses from here on: real measurement
            # beats prediction whenever both exist.
            actual_pack_map, actual_ambiguous = _read_actual_node_pack_ids(
                local_db_path, node_ids_needing_check
            )
            diverged = {
                nid
                for nid in set(node_pack_map) | set(actual_pack_map)
                if node_pack_map.get(nid) != actual_pack_map.get(nid)
            }
            if diverged:
                print(
                    f"  WARNING: predicted vs actual pack_id diverged for "
                    f"{len(diverged)} node(s) -- resolve_row_pack_id/"
                    f"backfill_pack_ids contract mismatch: {sorted(diverged)}"
                )
            if set(ambiguous_nodes) != set(actual_ambiguous):
                print(
                    f"  WARNING: predicted vs actual AMBIGUOUS sets diverged "
                    f"(predicted {sorted(ambiguous_nodes)}, actual "
                    f"{sorted(actual_ambiguous)})"
                )
            node_pack_map, ambiguous_nodes = actual_pack_map, actual_ambiguous
        for nid, packs in sorted(ambiguous_nodes.items()):
            print(
                f"  WARNING: node_id {nid!r} maps to CONFLICTING pack_ids "
                f"{packs} across its graph rows -- the vector store's "
                f"identity is node_id alone, so no single value is correct; "
                f"excluded from vector backfill (vector_ambiguous)."
            )
        graph_outcome, graph_reason = _stage_outcome(
            graph_stats,
            ("nodes_inferred", "nodes_assumed", "edges_inferred", "edges_assumed"),
        )
        # Row-level skips (non-dict JSON properties etc. -- rows
        # backfill_pack_ids could NOT attribute) leave pack_id-less rows
        # behind, which violates invariant 5 exactly like a whole-stage
        # skip does.  Demote the stage so it gates the exit code; always
        # surface the counts in the summary.
        rows_skipped = int(graph_stats.get("nodes_skipped") or 0) + int(
            graph_stats.get("edges_skipped") or 0
        )
        if rows_skipped and graph_outcome != "skipped":
            graph_outcome = "skipped"
            graph_reason = (
                f"{rows_skipped} row(s) left unattributed "
                f"(nodes_skipped={graph_stats.get('nodes_skipped') or 0}, "
                f"edges_skipped={graph_stats.get('edges_skipped') or 0}) -- "
                f"was: {graph_reason}"
            )
        stage_outcomes["graph_backfill"] = {"outcome": graph_outcome, "reason": graph_reason}

        print("\n3) Registering graph pack_ids (including any this step's inference just found)...")
        registry_stats = _register_graph_packs(
            sql,
            graph,
            owner_id,
            args.apply,
            node_pack_map=node_pack_map,
            ambiguous_nodes=ambiguous_nodes,
            accept_foreign_owned_packs=args.accept_foreign_owned_packs,
        )
        graph_available_for_enum = getattr(graph, "available", False)
        registry_pending = default_pending + int(registry_stats.get("unregistered") or 0)
        if not graph_available_for_enum:
            # Unconditional: registering the default pack is NOT the same
            # thing as having enumerated the graph's pack_ids.  (An earlier
            # version reported "applied" here whenever default_pending was
            # set, which let a fresh registry + unavailable-wrapper run
            # exit 0 with the graph's packs never registered.)
            registry_outcome, registry_reason = (
                "skipped",
                "graph unavailable -- pack_id enumeration skipped (default-pack registration still ran)",
            )
        elif not registry_stats.get("edges_enumerable", True):
            registry_outcome, registry_reason = (
                "skipped",
                "graph_edges could not be scanned for pack_id on this backend "
                "(no _table/_fetch_all -- e.g. Kuzu/Neo4j, #182 scope) -- an "
                "edge-only pack_id may exist unregistered",
            )
        elif registry_pending == 0:
            registry_outcome, registry_reason = "clean", "nothing to do"
        else:
            registry_outcome, registry_reason = "applied", f"{registry_pending} row(s) needed registering"
        stage_outcomes["registry_enumeration"] = {"outcome": registry_outcome, "reason": registry_reason}

        print("\n4) Backfilling doc rows with no pack_id...")
        doc_stats = _backfill_doc(docs, default_pack_id, args.apply)
        stage_outcomes["docs_backfill"] = dict(
            zip(("outcome", "reason"), _docs_stage_outcome(doc_stats), strict=True)
        )

        print("\n5) Backfilling vector rows with no pack_id (best-effort)...")
        vector_in_scope = (
            hasattr(vector, "get_by_id")
            and hasattr(vector, "upsert_texts")
            and getattr(vector, "available", False)
        )
        vector_stats = _backfill_vector(vector, node_ids_needing_check, node_pack_map, args.apply)
        vector_stats["ambiguous"] = len(ambiguous_nodes)
        if ambiguous_nodes:
            print(f"  vector_ambiguous={len(ambiguous_nodes)} (see WARNINGs above)")
        if not vector_in_scope:
            vector_outcome, vector_reason = (
                "skipped",
                "vector store unavailable or missing get_by_id/upsert_texts (best-effort only, see module docstring SCOPE)",
            )
        else:
            vector_outcome, vector_reason = _stage_outcome(vector_stats, ("missing",))
        stage_outcomes["vector_backfill"] = {"outcome": vector_outcome, "reason": vector_reason}
    except Exception as exc:
        # Whichever named stage above didn't get an entry yet is the one
        # that failed -- fill it in explicitly rather than leaving it
        # implicit, so the summary always accounts for all four stages.
        for name in ("graph_backfill", "registry_enumeration", "docs_backfill", "vector_backfill"):
            stage_outcomes.setdefault(name, {"outcome": "failed", "reason": str(exc)})
        print(f"\n! stage failed: {exc}", file=sys.stderr)
        print("\nSummary (incomplete -- a stage failed, later stages did not run):")
        for name, info in stage_outcomes.items():
            print(f"  {name}: {info['outcome']} ({info['reason']})")
        return 1

    print("\nSummary:")
    for name, info in stage_outcomes.items():
        print(f"  {name}: {info['outcome']} ({info['reason']})")
    if not args.apply:
        print("\nDry-run. Re-run with --apply (and --backup-to/--skip-backup) to perform writes.")

    # graph_backfill, docs_backfill AND registry_enumeration gate the exit
    # code -- stages whose SCOPE limits mean #147's read-path scoping would
    # be built on incompletely-inspected (or never-registered) data.
    # registry_enumeration gates in its own right: its skip is NOT always
    # accompanied by a graph_backfill skip -- a readable graph.db with an
    # unavailable graph *wrapper* backfills "clean" while enumeration is
    # skipped, leaving the graph's packs unregistered.  vector_backfill
    # alone stays out: it is documented as best-effort FOREVER (module
    # docstring SCOPE) regardless of store availability -- it was never a
    # completeness guarantee, so its skip doesn't gate deployment.
    gating = {
        stage_outcomes[n]["outcome"]
        for n in ("graph_backfill", "docs_backfill", "registry_enumeration")
    }
    if "skipped" in gating:
        print(
            "\n! graph_backfill, docs_backfill and/or registry_enumeration "
            "was skipped (out of scope) -- exit code 3. #147 must not "
            "deploy against a code-3 run: some backend's data was never "
            "even inspected or registered.",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
