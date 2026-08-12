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


def _register_graph_packs(sql: Any, graph: Any, owner_id: str, apply: bool) -> dict[str, int]:
    """One registry row per distinct graph pack_id not already registered.

    Uses the exact pack_id via ``_insert_pack`` (no quiet-suffixing --
    see module docstring). Called AFTER the graph backfill step so a
    pack_id ``backfill_pack_ids`` recovered via path-inference (not just
    the ``assume_pack_id`` default) also gets a registry row -- a fresh
    ``graph.list_packs()`` call here sees whatever the backfill step just
    wrote (in --apply mode) or would have found unchanged (dry-run)."""
    from opencrab.packs.registry import _insert_pack

    if not getattr(graph, "available", False):
        print("  graph store unavailable -- skipping pack-id enumeration")
        return {"graph_distinct_packs": 0, "unregistered": 0, "created": 0}

    already = _registered_pack_ids(sql)
    rows = graph.list_packs(min_nodes=1)
    candidates = [r for r in rows if r.get("pack_id") and r["pack_id"] not in already]
    print(
        f"  graph distinct pack_id: {len(rows)} total, "
        f"{len(candidates)} not yet in the registry"
    )
    created = 0
    if apply:
        for r in candidates:
            if _insert_pack(
                sql,
                r["pack_id"],
                owner_id,
                r.get("sample_title") or None,
                r.get("sample_description") or None,
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
    return {"graph_distinct_packs": len(rows), "unregistered": len(candidates), "created": created}


def _ensure_default_pack(sql: Any, owner_id: str, apply: bool) -> tuple[str, bool]:
    """Returns ``(DEFAULT_PACK_ID, was_pending)`` -- ``was_pending`` is True
    when the default pack row did NOT already exist at the start of this
    call (dry-run or --apply alike), so callers can tell "nothing to do"
    from "found something" without re-querying."""
    from opencrab.packs.registry import _insert_pack, get_pack

    existing = get_pack(sql, DEFAULT_PACK_ID)
    if existing is not None:
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


def _backfill_graph(settings: Any, default_pack_id: str, apply: bool) -> dict[str, Any]:
    """Delegates to the EXISTING ``opencrab.ontology.pack_provenance.
    backfill_pack_ids`` (same function ``opencrab packs backfill-pack-id``
    already exposes) rather than re-implementing pack_id backfill -- see
    module docstring SCOPE for why this is local-mode-only."""
    from opencrab.ontology.pack_provenance import backfill_pack_ids

    if settings.storage_mode != "local":
        print(
            f"  storage_mode={settings.storage_mode!r} has no SQLite graph.db -- "
            "skipping graph node/edge pack_id backfill (see module docstring SCOPE)."
        )
        return {"skipped": True}

    db_path = Path(settings.local_data_dir) / "graph.db"
    if not db_path.exists():
        print(f"  {db_path} does not exist -- skipping.")
        return {"skipped": True}

    summary = backfill_pack_ids(db_path, assume_pack_id=default_pack_id, dry_run=not apply)
    print(f"  graph.db backfill_pack_ids: {summary}")
    return summary


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


def _backfill_vector(vector: Any, node_ids: list[str], default_pack_id: str, apply: bool) -> dict[str, int]:
    if not (hasattr(vector, "get_by_id") and hasattr(vector, "upsert_texts")):
        print("  vector store has no get_by_id/upsert_texts -- skipping (out of scope, see module docstring).")
        return {"checked": 0, "missing": 0, "updated": 0}
    if not getattr(vector, "available", False):
        print("  vector store unavailable -- skipping.")
        return {"checked": 0, "missing": 0, "updated": 0}

    missing_ids: list[str] = []
    for node_id in node_ids:
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
            meta["pack_id"] = default_pack_id
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
        node_ids_needing_check = _graph_missing_node_ids(graph)
        graph_stats = _backfill_graph(settings, default_pack_id, args.apply)
        stage_outcomes["graph_backfill"] = dict(
            zip(
                ("outcome", "reason"),
                _stage_outcome(
                    graph_stats,
                    ("nodes_inferred", "nodes_assumed", "edges_inferred", "edges_assumed"),
                ),
                strict=True,
            )
        )

        print("\n3) Registering graph pack_ids (including any this step's inference just found)...")
        registry_stats = _register_graph_packs(sql, graph, owner_id, args.apply)
        graph_available_for_enum = getattr(graph, "available", False)
        registry_pending = default_pending + int(registry_stats.get("unregistered") or 0)
        if not graph_available_for_enum and not default_pending:
            registry_outcome, registry_reason = (
                "skipped",
                "graph unavailable -- pack_id enumeration skipped (default-pack registration still ran)",
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
        vector_stats = _backfill_vector(vector, node_ids_needing_check, default_pack_id, args.apply)
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

    # Only graph_backfill/docs_backfill gate the exit code -- those are the
    # two stages whose SCOPE limits (non-local storage_mode, a doc store
    # that's neither SQL- nor Mongo-backed) mean #147's read-path scoping
    # would be built on incompletely-inspected data. vector_backfill is
    # documented as best-effort FOREVER (module docstring SCOPE) regardless
    # of store availability -- it was never a completeness guarantee, so its
    # skip doesn't gate deployment. registry_enumeration's own "skipped" (a
    # symptom of the same graph unavailability) is already covered by
    # graph_backfill's skip in that same run.
    gating = {stage_outcomes[n]["outcome"] for n in ("graph_backfill", "docs_backfill")}
    if "skipped" in gating:
        print(
            "\n! graph_backfill and/or docs_backfill was skipped (out of "
            "scope) -- exit code 3. #147 must not deploy against a code-3 "
            "run: some backend's data was never even inspected.",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
