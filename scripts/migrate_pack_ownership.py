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
    (docker) via a native ``update_many``. A ``doc_nodes`` row missing
    pack_id is backfilled with priority graph-twin(exact) ->
    graph-twin(fallback) -> self path-inference -> ``default`` (#146 P1(b),
    PR #177 review round 3) -- it does NOT unconditionally default the way
    an earlier version of this script did. GRAPH-TWIN CONSISTENCY (a doc
    row landing in the SAME pack_id its graph_nodes counterpart resolved
    to) is only GUARANTEED in a mode where the graph stage above is itself
    in SCOPE (STORAGE_MODE=local, SQLite graph.db) -- in every other mode
    (pg/kuzu/docker) there is no SQLite graph.db for the twin lookup to
    read, ``_backfill_graph`` already skips there, and that skip already
    demotes ``graph_backfill`` to rc 3 (see SAFETY below), which #147 is
    already documented to refuse to deploy against. A doc row still left
    without ANY resolvable pack_id (its graph twin, if any, is ambiguous
    across spaces, or its own properties/metadata is valid JSON but not an
    object) is excluded from the write and reported -- never silently
    defaulted -- and demotes ``docs_backfill`` to ``skipped`` (rc 3) in its
    own right, same discipline as the graph stage's row-level skips below.
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

# Sentinel distinguishing "key absent" from "key present with a None/falsy
# value" (#146 P1(b), PR #177 review round 4 R4-C) -- ``dict.get(k) or {}``
# collapses both, which is exactly the bug: a present-but-non-dict Mongo
# field (empty list, non-empty list/string, or JSON null) must be EXCLUDED,
# while an absent key must still fall through to the resolver (a dotted
# ``$set`` creates the nested document fine when the key never existed).
_MISSING = object()


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


def _register_pack_id_candidates(
    sql: Any,
    owner_id: str,
    apply: bool,
    all_pack_ids: set[str],
    already_owners: dict[str, str],
    accept_foreign_owned_packs: bool,
    *,
    context: str,
    meta_by_pack_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Foreign-owner gate + ``_insert_pack`` registration loop, extracted
    out of the original ``_register_graph_packs`` (#146 M / #177 review
    round 2 "B v2"/"W v3") so it can be shared verbatim by the DOC-derived
    preflight pass (step 3.5, R5-B, PR #177 review round 5) -- design v2's
    explicit requirement is "same function, same rules, no new
    registration path": a pack_id's PROVENANCE (graph vs. document) must
    not change whether an already-foreign-owned slug aborts the run, nor
    how a new one gets inserted.

    ``context`` (e.g. ``"graph"`` / ``"document-derived"``) only affects
    log/error text describing which provenance the caller is reporting
    about -- the gate and insert logic themselves are provenance-agnostic.

    Foreign-owner overlap guard: a pack_id in ``all_pack_ids`` ALREADY
    registered to someone other than ``owner_id`` is ambiguous after the
    fact -- it looks identical whether that row is a legitimate user pack
    (created after this migration first ran, no conflict) or a slug that
    got squatted before this migration ever ran (the legacy content now
    silently belongs to the squatter once this function just skips it as
    "already registered"). This function cannot tell those two apart, so
    it does not guess: by default it raises (dry-run AND --apply alike) so
    an operator decides. ``accept_foreign_owned_packs=True`` is that
    explicit operator decision -- it downgrades the raise to a printed
    warning and proceeds exactly as before (the foreign-owned pack_id is
    left alone, same as any other already-registered pack_id).
    """
    from opencrab.pack.ownership import _insert_pack

    foreign_owned = sorted(
        pid for pid in all_pack_ids if pid in already_owners and already_owners[pid] != owner_id
    )
    if foreign_owned:
        detail = ", ".join(f"{pid!r} (owner={already_owners[pid]!r})" for pid in foreign_owned)
        if not accept_foreign_owned_packs:
            raise RuntimeError(
                f"{len(foreign_owned)} {context} pack_id(s) already exist in "
                f"{context} content but are registered to a DIFFERENT owner "
                f"than the bootstrap owner {owner_id!r}: {detail}. This can "
                "happen if remote pack registration was opened before this "
                "migration ran (someone else claimed one of these exact "
                "slugs first) -- or, more benignly, a re-run colliding with "
                "a genuinely new user pack. This script cannot tell those "
                "apart automatically -- it needs a human to verify. If "
                "these rows are confirmed to be legitimate (e.g. a safe "
                "re-run, or the owner is expected to hold this legacy "
                "content), re-run with --accept-foreign-owned-packs to skip "
                "them and proceed with everything else unchanged."
            )
        print(
            f"  ! --accept-foreign-owned-packs: skipping {len(foreign_owned)} "
            f"{context} pack_id(s) already registered to a different owner: {detail}"
        )

    candidates = sorted(pid for pid in all_pack_ids if pid not in already_owners)
    created = 0
    if apply:
        for pid in candidates:
            meta = (meta_by_pack_id or {}).get(pid)
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
                f"expected to register {len(candidates)} {context} pack_ids, "
                f"actually registered {created} -- a concurrent writer may "
                "have raced this script; re-run to confirm before trusting "
                "the registry."
            )
    return {"candidates": candidates, "foreign_owned": foreign_owned, "created": created}


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

    print(
        f"  graph distinct pack_id: {len(all_pack_ids)} total "
        f"(nodes={len(node_meta)}, edges={len(edge_pack_ids)}, "
        f"predicted={len(predicted_pack_ids)}), "
        f"{sum(1 for pid in all_pack_ids if pid not in already_owners)} not yet in the registry"
    )
    if not apply:
        print(
            "  note: edge-inferred pack_id (path-inference on graph_edges, "
            "no predictor exists for it -- see this function's docstring "
            "LIMITATION) is not visible to this dry-run report; it is only "
            "registered once a real --apply backfill has run."
        )
    result = _register_pack_id_candidates(
        sql,
        owner_id,
        apply,
        all_pack_ids,
        already_owners,
        accept_foreign_owned_packs,
        context="graph",
        meta_by_pack_id=node_meta,
    )
    return {
        "graph_distinct_packs": len(all_pack_ids),
        "unregistered": len(result["candidates"]),
        "created": result["created"],
        "edges_enumerable": edges_enumerable,
        "foreign_owned": result["foreign_owned"],
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


def _chunked(items: list[Any], size: int) -> list[list[Any]]:
    """Split ``items`` into ``size``-sized slices, preserving order -- shared
    by the graph-twin lookup and the PK-based group UPDATE below (#146
    P1(b)) so neither trips a backend's bound-parameter limit on a large
    migration."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _in_clause(prefix: str, values: list[str]) -> tuple[str, dict[str, Any]]:
    """Manually expand an ``IN (...)`` list into numbered named
    placeholders (same technique as ``_SqlGraphStoreBase._in_placeholders``,
    duplicated here because this script calls store hooks directly rather
    than importing a store-internal helper)."""
    names = [f"{prefix}{i}" for i in range(len(values))]
    return ", ".join(f":{n}" for n in names), dict(zip(names, values, strict=True))


def _pk_predicate(
    pk_cols: tuple[str, ...], rows_pk: list[tuple[Any, ...]], prefix: str
) -> tuple[str, dict[str, Any]]:
    """Build a doc table's REAL-PK predicate for a group of rows headed to
    the same target pack_id (#146 P1(b), PR #177 review round 2 v2 결함 6)
    -- deliberately never a shared ``node_id IN (...)``, which would also
    match an EXCLUDED row sharing that same node_id under a different
    ``(space, node_id)`` key (or a different ``doc_sources`` row that
    happens to share nothing at all -- ``source_id`` IS the whole PK there,
    so this degenerates to a plain ``IN`` for that table)."""
    if len(pk_cols) == 1:
        placeholders, params = _in_clause(prefix, [str(r[0]) for r in rows_pk])
        return f"{pk_cols[0]} IN ({placeholders})", params
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for i, values in enumerate(rows_pk):
        col_clauses = []
        for col, val in zip(pk_cols, values, strict=True):
            key = f"{prefix}{col}{i}"
            col_clauses.append(f"{col}=:{key}")
            params[key] = val
        clauses.append("(" + " AND ".join(col_clauses) + ")")
    return " OR ".join(clauses), params


_DOC_BACKFILL_CHUNK_SIZE = 200


def _backfill_doc_table(
    store: Any,
    table_name: str,
    prop_col: str,
    pk_cols: tuple[str, ...],
    id_col: str,
    default_pack_id: str,
    apply: bool,
    *,
    twin_exact: dict[tuple[str, str], str] | None = None,
    twin_fallback: dict[str, str] | None = None,
    twin_ambiguous: dict[tuple[str, str], list[str]] | None = None,
    twin_fallback_ambiguous: set[str] | None = None,
) -> dict[str, Any]:
    """Bulk-backfill missing pack_id on ONE doc-store JSON-properties table
    (``doc_nodes``/``doc_sources``) via the store's own
    ``_table``/``_fetch_one``/``_fetch_all``/``_exec_write`` hooks (shared
    by LocalSQLDocStore and PgDocStore, see ``_sql_doc_base.py``).

    Replaces the earlier ``_backfill_sql_table``'s unconditional "every
    missing row -> ``default_pack_id``" write (#146 P1(b), PR #177 review
    round 3): each row's target pack_id is resolved individually, with
    priority graph-twin(exact) -> graph-twin(fallback) -> self
    path-inference -> ``default_pack_id``.

    ``twin_exact``/``twin_fallback``/``twin_ambiguous``/``twin_fallback_ambiguous``
    come from ``_graph_twin_pack_map`` and are ``None`` for ``doc_sources``
    (it is not a graph node twin -- a legacy ``text_as_node=False`` row --
    see module docstring SCOPE; only self path-inference -> default applies
    to it). When given, a row's twin lookup key is ``(space, node_id)``
    (``space``/``node_id`` are always the first two of ``pk_cols`` for
    ``doc_nodes``); ``twin_ambiguous`` hitting that key EXCLUDES the row
    (no single graph pack_id is correct for it) rather than falling through
    to self-inference or default -- writing a guess over a row whose own
    graph twin disagrees with itself would be worse than leaving it alone.

    Lookup order (#146 P1(b), PR #177 review round 4 R4-B): exact
    ambiguous -> EXCLUDE; exact hit -> apply; only when exact MISSES ->
    fallback ambiguous -> EXCLUDE; fallback hit -> apply; self path-
    inference; default. ``twin_fallback_ambiguous`` is a bare ``node_id``
    set (not keyed by ``(space, node_id)`` like ``twin_ambiguous`` --
    blank-``space_id`` graph rows that disagree on a shared node_id have no
    real space to key on) built by ``_graph_twin_pack_map`` from
    blank-``space_id`` graph rows that disagree with each other on a shared
    node_id; a doc row whose EXACT key misses but whose node_id is in this
    set is excluded rather than silently inheriting one of the disagreeing
    fallback values (or falling through to self-inference/default, which
    would be an equally arbitrary guess). It is consulted ONLY on an exact
    miss -- an exact hit already resolves the row correctly regardless of
    what unrelated blank-space rows disagree about, since the exact key's
    ``space`` component is more specific than the fallback's node_id-alone
    key.

    A row whose own ``prop_col`` is valid JSON but not an object
    (``resolve_row_pack_id``'s ``"skipped-non-dict"``) is likewise EXCLUDED,
    never defaulted -- same "don't guess over an unreadable row" reasoning.
    Both exclusion classes are counted in the returned ``excluded`` total
    and reported so ``_docs_stage_outcome`` can demote this stage (a row
    still missing pack_id after this call violates invariant 5 exactly like
    the graph stage's own row-level skips do).

    Every actual write is grouped by TARGET pack_id and predicated on the
    table's REAL primary key -- see ``_pk_predicate`` -- chunked at
    ``_DOC_BACKFILL_CHUNK_SIZE`` rows per UPDATE. Each chunk's expected
    matching row count is re-``SELECT COUNT(*)``'d with the identical
    predicate immediately before its UPDATE and compared against the
    UPDATE's own rowcount; a mismatch aborts (``RuntimeError``) rather than
    trusting a partial write, same discipline every other write in this
    script already follows.
    """
    from opencrab.ontology.pack_provenance import resolve_row_pack_id

    check_twin = twin_exact is not None
    table = store._table(table_name)
    is_pg = _is_pg_dialect(store)
    missing_where, set_expr = _missing_and_set_sql(prop_col, is_pg)

    total = store._fetch_one(f"SELECT COUNT(*) FROM {table}", {})[0]  # noqa: S608
    select_cols = ", ".join((*pk_cols, prop_col))
    rows = store._fetch_all(
        f"SELECT {select_cols} FROM {table} WHERE {missing_where}", {}  # noqa: S608
    )
    missing = len(rows)

    by_pack: dict[str, list[tuple[Any, ...]]] = {}
    excluded: dict[str, int] = {
        "ambiguous-twin": 0,
        "ambiguous-twin-fallback": 0,
        "non-dict": 0,
    }
    resolution_counts: dict[str, int] = {}
    for row in rows:
        pk_values = tuple(row[i] for i in range(len(pk_cols)))
        prop_raw = row[len(pk_cols)]
        id_value = pk_values[-1]  # node_id for doc_nodes, source_id for doc_sources
        space_value = pk_values[0] if len(pk_cols) > 1 else None

        pack_id: str | None = None
        reason = ""
        if check_twin:
            twin_key = (space_value, id_value)
            if twin_key in (twin_ambiguous or {}):
                excluded["ambiguous-twin"] += 1
                continue
            exact = (twin_exact or {}).get(twin_key)
            if exact:
                pack_id, reason = exact, "graph-twin-exact"
            elif id_value in (twin_fallback_ambiguous or set()):
                # Exact missed AND the blank-space fallback disagrees with
                # itself for this node_id -- neither side gives a single
                # correct answer, so exclude rather than fall through to
                # self-inference/default (#146 P1(b), R4-B).
                excluded["ambiguous-twin-fallback"] += 1
                continue
            else:
                fb = (twin_fallback or {}).get(id_value)
                if fb:
                    pack_id, reason = fb, "graph-twin-fallback"
        if pack_id is None:
            inferred, infer_reason = resolve_row_pack_id(prop_raw, {id_col: id_value}, None)
            if infer_reason == "skipped-non-dict":
                excluded["non-dict"] += 1
                continue
            if inferred:
                pack_id, reason = inferred, "self-inferred"
        if pack_id is None:
            pack_id, reason = default_pack_id, "default"

        by_pack.setdefault(pack_id, []).append(pk_values)
        resolution_counts[reason] = resolution_counts.get(reason, 0) + 1

    by_pack_summary = ", ".join(f"{p}: {len(r)}" for p, r in sorted(by_pack.items()))
    print(
        f"  {table_name}: total={total} missing_pack_id={missing} "
        f"by_pack={{{by_pack_summary}}} excluded={excluded} resolution={resolution_counts}"
    )

    updated = 0
    if apply:
        for pack_id, pk_rows in by_pack.items():
            for chunk in _chunked(pk_rows, _DOC_BACKFILL_CHUNK_SIZE):
                pred, pred_params = _pk_predicate(pk_cols, chunk, "bf")
                # missing_where is an unparenthesized "A IS NULL OR A = ''"
                # -- AND binds tighter than OR in SQL, so combining it bare
                # with "AND (pred)" would parse as "(A IS NULL) OR (A = ''
                # AND pred)", letting ANY row with a NULL properties.pack_id
                # match regardless of the PK predicate (silently reopening
                # v2 결함 6 despite the PK-based predicate above). Both
                # halves of missing_where must be grouped first.
                where = f"({missing_where}) AND ({pred})"
                expected = store._fetch_one(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}", pred_params  # noqa: S608
                )[0]
                if expected != len(chunk):
                    raise RuntimeError(
                        f"{table_name}: expected {len(chunk)} row(s) matching this "
                        f"chunk's PK predicate, found {expected} -- aborting rather "
                        "than trusting a partial write (a concurrent writer may have "
                        "changed a row's pack_id between selection and update)."
                    )
                params = {**pred_params, "pid": pack_id}
                rc = store._exec_write(
                    f"UPDATE {table} SET {prop_col} = {set_expr} WHERE {where}",  # noqa: S608
                    params,
                )
                if rc != len(chunk):
                    raise RuntimeError(
                        f"{table_name}: expected to backfill {len(chunk)} row(s) "
                        f"(pack_id={pack_id!r}), actually updated {rc} -- aborting "
                        "rather than trusting a partial write."
                    )
                updated += rc

    return {
        "total": total,
        "missing": missing,
        "updated": updated,
        "excluded": sum(excluded.values()),
        "excluded_breakdown": excluded,
        "by_pack": {pid: len(pk_rows) for pid, pk_rows in by_pack.items()},
    }


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


_GRAPH_TWIN_CHUNK_SIZE = 500


def _graph_twin_pack_map(
    graph: Any,
    node_ids: list[str],
    assume_pack_id: str | None,
    *,
    actual: bool,
) -> tuple[
    dict[tuple[str, str], str],
    dict[str, str],
    dict[tuple[str, str], list[str]],
    set[str],
]:
    """Resolve each ``doc_nodes`` row's GRAPH TWIN pack_id (#146 P1(b), PR
    #177 review round 3): for every node_id a doc row was found MISSING
    pack_id on, look up that SAME node_id's ``graph_nodes`` row(s) and read
    (``actual=True``, an apply-time re-query AFTER the graph backfill has
    already written something) or predict (``actual=False``, a dry-run
    resolution via the SAME ``resolve_row_pack_id`` the real graph backfill
    itself uses) what pack_id it carries.

    ``node_ids`` is the DOC-side missing-id set, NOT the graph-side missing
    set -- a graph node that ALREADY has a pack_id (nothing for the graph
    backfill to do) but whose doc twin is still bare is exactly half of the
    inconsistency this closes; it would be invisible if this only looked at
    graph's own missing rows.

    Keyed by ``(space_id, node_id)`` -- the graph PK is ``(node_type,
    node_id)``, so one node_id can legally carry DIFFERENT pack_ids across
    types/spaces (PR #177 review round 2 v2 결함 5's exact scenario: a
    ``(TypeA, shared, space=concept, pack-a)`` + ``(TypeB, shared,
    space=evidence, pack-b)`` pair is NOT ambiguous for a doc row keyed
    ``(concept, shared)`` -- it resolves cleanly to ``pack-a``; only rows
    sharing the SAME ``(space_id, node_id)`` with different pack_ids are
    truly ambiguous). ``exact_map`` only carries entries where every graph
    row sharing that ``(space_id, node_id)`` pair agrees; disagreement is
    reported via ``ambiguous`` (reusing ``_split_ambiguous`` -- its
    ``dict[str, ...]`` type hint is written for the vector-side str key,
    but the function itself is a plain key->set(values) splitter with no
    str-specific behavior, so a tuple key works identically) and excluded
    from both maps. ``fallback_map`` is a node_id-ONLY aggregate (used when
    a doc row's graph twin has no/blank ``space_id``, so the exact key can
    never match a real doc ``space``) and only carries a value when every
    graph row sharing that BARE node_id agrees too.

    ``fallback_ambiguous`` (#146 P1(b), PR #177 review round 4 R4-B) is the
    node_id-ONLY counterpart of ``ambiguous`` for the fallback path: several
    blank-``space_id`` graph rows sharing one node_id but resolving to
    DIFFERENT pack_ids. The plain dict-comprehension that builds
    ``fallback_map`` silently DROPS such a node_id (``len(packs) == 1``
    excludes it) -- correct for ``fallback_map`` itself, but that drop was
    previously invisible to the caller: ``fallback_map`` is keyed by bare
    node_id while ``ambiguous`` is keyed by ``(space_id, node_id)``, so a
    doc row's ``(document_space, node_id)`` lookup never collided with
    either dict and the row silently fell through to self-inference or
    ``default`` instead of being excluded. Reported SEPARATELY from
    ``ambiguous`` (not merged into one dict) because the two live in
    different key spaces -- merging them would force every caller to branch
    on key shape again, recreating the exact "key doesn't match, silently
    passes" bug this closes.

    ``actual=True`` and ``actual=False`` share ONE normalization path for
    the properties column (``pack_provenance._normalize_props``, via
    ``resolve_row_pack_id`` for the dry-run branch and directly for the
    apply-time branch) -- v5 결함 8: the apply-time branch reads RAW
    ``_fetch_all`` rows, where SQLite hands back a JSON **string** and
    PostgreSQL's JSONB decodes straight to a **dict**; duplicating that
    dict-or-string handling in a second place here (instead of importing
    the shared helper) would give the two modes a chance to silently
    disagree about a PG-only row.

    Returns ``({}, {}, {}, set())`` when ``graph`` exposes no ``_table``/
    ``_fetch_all`` hooks (Kuzu/Neo4j) -- that backend's SCOPE gap already
    forces ``graph_backfill`` to ``skipped`` (rc 3, see module docstring
    SAFETY), so callers must not treat an empty map here as "no twins
    exist" (see module docstring's new SCOPE note on doc/graph twin
    consistency).
    """
    if not node_ids or not (hasattr(graph, "_table") and hasattr(graph, "_fetch_all")):
        return {}, {}, {}, set()

    from opencrab.ontology.pack_provenance import _normalize_props, resolve_row_pack_id

    table = graph._table("graph_nodes")
    exact_seen: dict[tuple[str, str], set[str]] = {}
    fallback_seen: dict[str, set[str]] = {}
    for chunk in _chunked(sorted(set(node_ids)), _GRAPH_TWIN_CHUNK_SIZE):
        placeholders, params = _in_clause("twn", chunk)
        rows = graph._fetch_all(
            f"SELECT node_id, space_id, properties FROM {table} WHERE node_id IN ({placeholders})",  # noqa: S608
            params,
        )
        for node_id, space_id, props_raw in rows:
            if actual:
                props = _normalize_props(props_raw)
                pack_id = str(props["pack_id"]) if props and props.get("pack_id") else None
            else:
                pack_id, _reason = resolve_row_pack_id(
                    props_raw, {"node_id": node_id}, assume_pack_id
                )
            if not pack_id:
                continue
            exact_seen.setdefault((space_id, node_id), set()).add(pack_id)
            # ONLY blank-space_id rows feed the node_id-only fallback. A row
            # that HAS a space_id can always be matched exactly, so letting
            # it into the fallback would attribute a doc row in space X to a
            # graph node living in space Y purely because they share a
            # node_id -- they are different rows, not twins (doc_nodes' PK is
            # (space, node_id)), and that is the same wrong-pack/wrong-
            # visibility outcome this whole fix exists to prevent.
            if not space_id:
                fallback_seen.setdefault(node_id, set()).add(pack_id)

    exact_map, ambiguous = _split_ambiguous(exact_seen)  # type: ignore[arg-type]
    fallback_map: dict[str, str] = {}
    fallback_ambiguous: set[str] = set()
    for nid, packs in fallback_seen.items():
        if len(packs) == 1:
            fallback_map[nid] = next(iter(packs))
        else:
            fallback_ambiguous.add(nid)
    return exact_map, fallback_map, ambiguous, fallback_ambiguous


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


def _doc_missing_node_ids(docs: Any) -> list[str]:
    """node_ids currently missing pack_id in ``doc_nodes`` -- feeds the
    graph-twin lookup (#146 P1(b)) BEFORE ``_backfill_doc_table`` does its
    own (identically-predicated) SELECT of the same rows. A row gaining a
    pack_id between this call and that one (a concurrent writer) is not a
    new risk: ``_backfill_doc_table``'s own rowcount-assertion discipline
    already guards every write it performs, regardless of what this
    pre-pass saw."""
    table = docs._table("doc_nodes")
    is_pg = _is_pg_dialect(docs)
    missing_where, _ = _missing_and_set_sql("properties", is_pg)
    rows = docs._fetch_all(f"SELECT node_id FROM {table} WHERE {missing_where}", {})  # noqa: S608
    return [r[0] for r in rows]


def _backfill_doc(docs: Any, graph: Any, default_pack_id: str, apply: bool) -> dict[str, Any]:
    """``graph`` (#146 P1(b)) supplies the graph-twin lookup
    ``_graph_twin_pack_map`` needs for ``doc_nodes`` -- ``doc_sources`` gets
    no twin map (it is not a graph node twin, see module docstring SCOPE).
    ``actual=apply``: by the time this stage runs (main()'s step 4, AFTER
    step 2's graph backfill), a real ``--apply`` run has ALREADY written the
    graph's pack_ids for real, so the twin lookup must read that ACTUAL
    state; a dry-run left the graph untouched, so the twin lookup must
    PREDICT via the same ``resolve_row_pack_id`` the graph stage itself
    would use -- exactly the same apply/dry-run split ``_predict_node_pack_map``
    / ``_read_actual_node_pack_ids`` already use for the vector stage below.
    """
    if not getattr(docs, "available", True):
        print("  doc store unavailable -- skipping.")
        return {"skipped": True}
    if hasattr(docs, "_dialect"):
        missing_node_ids = _doc_missing_node_ids(docs)
        twin_exact, twin_fallback, twin_ambiguous, twin_fallback_ambiguous = _graph_twin_pack_map(
            graph, missing_node_ids, default_pack_id, actual=apply
        )
        nodes = _backfill_doc_table(
            docs,
            "doc_nodes",
            "properties",
            ("space", "node_id"),
            "node_id",
            default_pack_id,
            apply,
            twin_exact=twin_exact,
            twin_fallback=twin_fallback,
            twin_ambiguous=twin_ambiguous,
            twin_fallback_ambiguous=twin_fallback_ambiguous,
        )
        sources = _backfill_doc_table(
            docs, "doc_sources", "metadata", ("source_id",), "source_id", default_pack_id, apply
        )
        return {"doc_nodes": nodes, "doc_sources": sources}
    if hasattr(docs, "_db"):
        return _backfill_mongo(docs._db, default_pack_id, apply)
    print("  doc store is neither SQL- nor Mongo-backed -- skipping.")
    return {"skipped": True}


def _backfill_mongo(db: Any, default_pack_id: str, apply: bool) -> dict[str, Any]:
    """#146 P1(b): applies the SAME existing -> inferred -> assumed
    priority ``resolve_row_pack_id`` gives ``graph_nodes``/``graph_edges``
    to every missing Mongo document's own ``properties``/``metadata``,
    instead of the earlier version's unconditional
    ``$set: {...pack_id: default_pack_id}``. Mongo (docker mode) has no
    SQLite graph.db for a twin lookup (module docstring SCOPE) -- only the
    row's own path-inference, then default, exactly the doc self-inference
    step the SQL-backed path also falls through to.

    Type judgment happens BEFORE ``resolve_row_pack_id`` is ever called
    (#146 P1(b), PR #177 review round 4 R4-C): Mongo fields are NATIVE BSON
    values, not JSON strings like every SQLite-backed column, so
    ``resolve_row_pack_id``'s own non-dict detection (which normalizes via
    ``json.loads``) never fires the way it does for SQL rows -- a native
    Python list or str raises inside ``json.loads`` and is silently
    swallowed to ``{}`` (a valid, empty dict) by ``_normalize_props``'s
    malformed-JSON tolerance, so the row is treated as "no pack_id hint"
    and ends up ``assumed`` -> ``default_pack_id`` instead of excluded.
    ``["bad"]``/``"bad"`` would therefore still reach a dotted ``$set``
    under the OLDER "trust the reason string" design -- this function
    checks ``isinstance(raw, dict)`` itself first so it never asks
    ``resolve_row_pack_id`` to judge a type it cannot see correctly:

      - the field key is ABSENT (``_MISSING``) -- NOT excluded; a dotted
        ``$set`` happily creates the nested document, so this still goes
        through the resolver (-> self path-inference -> default), exactly
        as before. Excluding an absent key would be over-exclusion.
      - the field is PRESENT and a ``dict`` -- goes through the resolver
        normally.
      - the field is PRESENT and NOT a ``dict`` (``None``, ``""``, ``[]``,
        a non-empty list/string, ...) -- EXCLUDED, no resolver call, no
        ``$set`` attempted. This also covers the documented MongoDB
        failure mode of a dotted ``$set`` under a JSON ``null`` field
        (``{"properties": null}`` -> "Cannot create field 'pack_id' in
        element {properties: null}"), which the old code would have
        walked straight into.

    A resolver-returned ``None`` (the true ``skipped-non-dict``/
    ``skipped-unresolvable`` case -- unreachable here in practice since
    ``assume_pack_id=default_pack_id`` is always given, kept as a
    defensive second net) is also counted as excluded rather than silently
    dropped.

    Each collection's result dict carries its own ``excluded`` count (like
    ``_backfill_doc_table``'s SQL-backed sub-dicts already do) so
    ``_docs_stage_outcome``'s existing demote-to-``skipped`` rule (-> rc 3)
    reaches Mongo too -- a row left without ANY pack_id violates invariant
    5 the same way regardless of which backend it lives in.
    """
    from opencrab.ontology.pack_provenance import resolve_row_pack_id

    results: dict[str, Any] = {}
    for collection, field_root, id_field in (
        ("nodes", "properties", "node_id"),
        ("sources", "metadata", "source_id"),
    ):
        coll = db[collection]
        total = coll.estimated_document_count()
        missing_q = {
            "$or": [
                {f"{field_root}.pack_id": {"$exists": False}},
                {f"{field_root}.pack_id": None},
                {f"{field_root}.pack_id": ""},
            ]
        }
        missing_docs = list(coll.find(missing_q))
        missing = len(missing_docs)
        by_pack: dict[str, list[Any]] = {}
        excluded = 0
        for doc in missing_docs:
            raw = doc.get(field_root, _MISSING)
            if raw is not _MISSING and not isinstance(raw, dict):
                excluded += 1
                continue
            field = raw if raw is not _MISSING else {}
            id_value = doc.get(id_field, "")
            pack_id, _reason = resolve_row_pack_id(field, {id_field: id_value}, default_pack_id)
            if pack_id is None:
                excluded += 1
                continue
            by_pack.setdefault(pack_id, []).append(doc["_id"])
        by_pack_summary = ", ".join(f"{p}: {len(ids)}" for p, ids in sorted(by_pack.items()))
        print(
            f"  mongo.{collection}: total={total} missing_pack_id={missing} "
            f"by_pack={{{by_pack_summary}}} excluded={excluded}"
        )
        updated = 0
        if apply:
            for pack_id, ids in by_pack.items():
                result = coll.update_many(
                    {"_id": {"$in": ids}}, {"$set": {f"{field_root}.pack_id": pack_id}}
                )
                if result.modified_count != len(ids):
                    raise RuntimeError(
                        f"mongo.{collection}: expected to backfill {len(ids)} docs "
                        f"(pack_id={pack_id!r}), actually updated {result.modified_count} "
                        "-- aborting."
                    )
                updated += result.modified_count
        results[collection] = {
            "total": total,
            "missing": missing,
            "updated": updated,
            "excluded": excluded,
            # R5-B (PR #177 review round 5): same shape
            # _backfill_doc_table's SQL-backed sub-dicts already carry --
            # ``_register_doc_packs``'s step-3.5 preflight (main()) reads
            # this to preview which pack_ids a dry-run of this function
            # would assign, BEFORE step 4 writes anything for real.
            "by_pack": {pid: len(ids) for pid, ids in by_pack.items()},
        }
    return results


# ---------------------------------------------------------------------------
# R5-B (PR #177 review round 5 P1): document-derived pack_id registration
# preflight (main()'s step 3.5) -- registers a pack_id that ONLY exists in
# doc storage (no graph content of its own, so step 3's graph-only
# enumeration never sees it) BEFORE step 4 writes it onto any doc row. See
# module docstring / fix design R5-B for the bug this closes.
# ---------------------------------------------------------------------------


def _sql_table_existing_pack_ids(store: Any, table_name: str, prop_col: str) -> tuple[set[str], int]:
    """(a) of ``_register_doc_packs``'s collection, ONE SQL-backed doc
    table: every DISTINCT pack_id already present (non-missing) in
    ``prop_col`` right now -- NOT the resolver's predicted assignment for a
    still-missing row (that is (b), see ``_doc_predicted_pack_ids``).

    This is what gives a resumed re-run self-healing: an interrupted prior
    ``--apply`` run may have already stamped a pack_id on some rows without
    this migration ever having registered it (a plain re-scan of MISSING
    rows would never see those rows again -- they are not missing).

    Returns ``(valid_pack_ids, malformed_count)`` -- a present pack_id
    value that is not a non-empty JSON **string** (a number, list, object,
    bool, or ``null`` some other process wrote) is malformed and excluded,
    counted separately so the caller can warn rather than silently register
    it as-is or silently drop it.
    """
    from opencrab.ontology.pack_provenance import _normalize_props

    is_pg = _is_pg_dialect(store)
    missing_where, _ = _missing_and_set_sql(prop_col, is_pg)
    table = store._table(table_name)
    rows = store._fetch_all(
        f"SELECT {prop_col} FROM {table} WHERE NOT ({missing_where})", {}  # noqa: S608
    )
    pack_ids: set[str] = set()
    malformed = 0
    for (raw,) in rows:
        props = _normalize_props(raw) or {}
        pid = props.get("pack_id")
        if isinstance(pid, str) and pid:
            pack_ids.add(pid)
        else:
            malformed += 1
    return pack_ids, malformed


def _mongo_existing_pack_ids(db: Any) -> tuple[set[str], int]:
    """(a) of ``_register_doc_packs``'s collection, Mongo (docker mode):
    every DISTINCT pack_id already present in either collection's own
    field, across BOTH ``nodes``/``properties`` and ``sources``/
    ``metadata`` -- see ``_sql_table_existing_pack_ids`` for the SQL-backed
    counterpart and why this matters."""
    pack_ids: set[str] = set()
    malformed = 0
    for collection, field_root in (("nodes", "properties"), ("sources", "metadata")):
        coll = db[collection]
        existing_q = {
            "$and": [
                {f"{field_root}.pack_id": {"$exists": True}},
                {f"{field_root}.pack_id": {"$ne": None}},
                {f"{field_root}.pack_id": {"$ne": ""}},
            ]
        }
        for doc in coll.find(existing_q):
            field = doc.get(field_root)
            pid = field.get("pack_id") if isinstance(field, dict) else None
            if isinstance(pid, str) and pid:
                pack_ids.add(pid)
            else:
                malformed += 1
    return pack_ids, malformed


def _doc_existing_pack_ids(docs: Any) -> tuple[set[str], int]:
    """(a) of ``_register_doc_packs``'s collection, dispatched across
    whichever doc backend ``docs`` is (SQL-backed local/pg, Mongo, or
    unavailable/unknown -- same dispatch ``_backfill_doc`` itself uses)."""
    if not getattr(docs, "available", True):
        return set(), 0
    if hasattr(docs, "_dialect"):
        pack_ids: set[str] = set()
        malformed = 0
        for table_name, prop_col in (("doc_nodes", "properties"), ("doc_sources", "metadata")):
            pids, mal = _sql_table_existing_pack_ids(docs, table_name, prop_col)
            pack_ids |= pids
            malformed += mal
        return pack_ids, malformed
    if hasattr(docs, "_db"):
        return _mongo_existing_pack_ids(docs._db)
    return set(), 0


def _doc_predicted_pack_ids(stats: dict[str, Any]) -> tuple[set[str], int]:
    """(b) of ``_register_doc_packs``'s collection: every pack_id key of a
    ``_backfill_doc(..., apply=False)`` preview's ``by_pack`` sub-dicts.
    Works uniformly for the SQL-backed ``{"doc_nodes": {...}, "doc_sources":
    {...}}`` shape and the Mongo ``{"nodes": {...}, "sources": {...}}``
    shape -- both now carry ``by_pack`` (see ``_backfill_doc_table`` and
    ``_backfill_mongo``). ``{"skipped": True}`` (doc store unavailable, or
    neither SQL- nor Mongo-backed) contributes nothing.

    A key here being malformed should be structurally impossible --
    everything that populates a ``by_pack`` dict already stores a resolved
    non-empty ``str`` (a graph-twin value, a self-inferred value, or the
    ``default_pack_id`` constant) -- this check is a defensive second net
    matching this function's ``(a)`` counterpart, not an expected path.
    """
    if stats.get("skipped"):
        return set(), 0
    pack_ids: set[str] = set()
    malformed = 0
    for sub in stats.values():
        if not isinstance(sub, dict):
            continue
        for pid in sub.get("by_pack", {}):
            if isinstance(pid, str) and pid:
                pack_ids.add(pid)
            else:
                malformed += 1
    return pack_ids, malformed


def _register_doc_packs(
    sql: Any,
    docs: Any,
    graph: Any,
    owner_id: str,
    default_pack_id: str,
    apply: bool,
    accept_foreign_owned_packs: bool,
) -> dict[str, Any]:
    """R5-B (PR #177 review round 5 P1) preflight: register every
    DOCUMENT-derived pack_id BEFORE step 4 (main()) writes a single doc
    row.

    THE BUG THIS CLOSES: without this step, a ``doc_nodes``/``doc_sources``
    row that step 4's OWN self path-inference (or a value already stamped
    by an interrupted prior run) resolves to a pack_id with NO graph
    content of its own is invisible to step 3's graph-only enumeration
    (``_register_graph_packs`` only ever looks at ``graph_nodes``/
    ``graph_edges``/predicted graph twins). The migration would then exit 0
    having attributed real doc content to a pack_id the registry never
    heard of -- #147's registry-based read authorization can then never
    expose that content. This is a bug THIS PR introduced: before doc
    self-path-inference existed, a doc row missing pack_id always fell
    through to ``default_pack_id``, which step 1 always registers.

    READ-ONLY against doc storage: the only write this function performs
    is to the ``packs`` registry (via ``_register_pack_id_candidates``,
    the SAME gate+insert function step 3 uses -- no second registration
    code path). Nothing is written to ``doc_nodes``/``doc_sources``/Mongo
    here, so an abort from the foreign-owner gate below leaves every doc
    row provably untouched -- that is WHY this runs before step 4, not
    after.

    Collection = union of:
      (a) ``_doc_existing_pack_ids``: every DISTINCT pack_id already
          present (non-missing) in doc storage today -- gives a resumed
          re-run self-healing (see that function's docstring).
      (b) ``_doc_predicted_pack_ids`` of a forced ``_backfill_doc(...,
          apply=False)`` preview -- the SAME resolver step 4 will use for
          real, so this prediction and step 4's actual assignment cannot
          structurally diverge (same discipline as ``_predict_node_pack_map``
          / ``_read_actual_node_pack_ids`` already apply on the graph/vector
          side).

    Malformed pack_id values (not a non-empty string) found in either half
    are excluded from registration and reported via ``malformed_excluded``
    -- never registered as-is, never silently dropped without a trace.
    """
    doc_ids, existing_malformed = _doc_existing_pack_ids(docs)
    preview = _backfill_doc(docs, graph, default_pack_id, apply=False)
    predicted_ids, predicted_malformed = _doc_predicted_pack_ids(preview)
    malformed = existing_malformed + predicted_malformed
    all_pack_ids = doc_ids | predicted_ids

    if malformed:
        print(
            f"  ! {malformed} document-derived pack_id value(s) were "
            "malformed (present but not a non-empty string) -- excluded "
            "from registration, see _register_doc_packs' docstring."
        )
    already_owners = _registered_pack_owners(sql)
    print(
        f"  doc-derived distinct pack_id: {len(all_pack_ids)} total "
        f"(already-present={len(doc_ids)}, predicted={len(predicted_ids)}), "
        f"{sum(1 for pid in all_pack_ids if pid not in already_owners)} not yet in the registry"
    )
    result = _register_pack_id_candidates(
        sql,
        owner_id,
        apply,
        all_pack_ids,
        already_owners,
        accept_foreign_owned_packs,
        context="document-derived",
    )
    return {
        "doc_distinct_packs": len(all_pack_ids),
        "unregistered": len(result["candidates"]),
        "created": result["created"],
        "foreign_owned": result["foreign_owned"],
        "malformed_excluded": malformed,
    }


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
    sub-dicts carrying their own "missing" count).

    #146 P1(b): a sub-dict carrying a non-zero ``excluded`` count (BOTH
    ``_backfill_doc_table``'s SQL-backed sub-dicts -- an ``(space, node_id)``
    the graph-twin map reports ambiguous on either the exact or the
    blank-space fallback key, or a row whose own properties/metadata is
    valid JSON but not an object -- AND, since PR #177 review round 4
    R4-C, ``_backfill_mongo``'s sub-dicts too -- a document whose
    properties/metadata field is present but a native non-dict BSON value)
    demotes this stage to ``skipped`` regardless of ``missing`` -- those
    rows are left WITHOUT any pack_id by design (never guessed-and-
    defaulted, see ``_backfill_doc_table``'s and ``_backfill_mongo``'s
    docstrings), which violates invariant 5 the exact same way the graph
    stage's own row-level ``nodes_skipped``/``edges_skipped`` demotion
    (``main()``) does, so it must gate the exit code too."""
    if stats.get("skipped"):
        return "skipped", "doc store unavailable or not SQL/Mongo-backed"
    subs = [sub for sub in stats.values() if isinstance(sub, dict)]
    pending = sum(int(sub.get("missing") or 0) for sub in subs)
    excluded = sum(int(sub.get("excluded") or 0) for sub in subs)
    if excluded:
        return (
            "skipped",
            f"{excluded} row(s) left unattributed after resolution (ambiguous "
            "graph twin or non-dict properties/metadata) -- was: "
            f"{'clean' if pending == 0 else f'{pending} row(s) needed backfilling'}",
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

        # 3.5) R5-B (PR #177 review round 5 P1): register every DOC-derived
        # pack_id too -- BEFORE step 4 writes a single doc row -- so a
        # pack_id step 4's OWN self-path-inference (or graph-twin lookup, or
        # a value an interrupted prior run already stamped) would assign,
        # but that has NO graph content of its own (invisible to step 3's
        # graph-only enumeration above), still gets a registry row before
        # any content is attributed to it. See _register_doc_packs'
        # docstring. Folded into "registry_enumeration" rather than a new
        # stage (module docstring SAFETY documents exactly four stages) --
        # this is the SAME responsibility ("has every real pack_id reached
        # the registry") executed in two passes over two provenances.
        print("\n3.5) Registering document-derived pack_ids (preflight before doc backfill writes)...")
        try:
            doc_registry_stats = _register_doc_packs(
                sql,
                docs,
                graph,
                owner_id,
                default_pack_id,
                args.apply,
                args.accept_foreign_owned_packs,
            )
        except Exception as exc:
            # The outer `except Exception as exc:` below uses setdefault,
            # which would otherwise PRESERVE whatever step 3 just recorded
            # above (e.g. "clean"/"applied") and hide this failure entirely.
            # Must overwrite explicitly (design v2 R5-B point 5).
            stage_outcomes["registry_enumeration"] = {
                "outcome": "failed",
                "reason": f"document-derived pack_id registration (step 3.5) failed: {exc}",
            }
            raise

        doc_unregistered = int(doc_registry_stats.get("unregistered") or 0)
        if registry_outcome == "skipped":
            # A graph-side scope gap (unavailable graph/edges) is unrelated
            # to doc-derived registration progress -- stays authoritative
            # regardless of what step 3.5 just did.
            pass
        else:
            graph_registry_pending = registry_pending
            registry_pending += doc_unregistered
            if registry_pending == 0:
                registry_outcome, registry_reason = "clean", "nothing to do"
            else:
                registry_outcome, registry_reason = (
                    "applied",
                    f"{registry_pending} row(s) needed registering "
                    f"(graph-derived={graph_registry_pending}, "
                    f"doc-derived={doc_unregistered})",
                )
        stage_outcomes["registry_enumeration"] = {"outcome": registry_outcome, "reason": registry_reason}

        print("\n4) Backfilling doc rows with no pack_id...")
        doc_stats = _backfill_doc(docs, graph, default_pack_id, args.apply)
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
