#!/usr/bin/env python3
"""1:1 migrate the 4 local SQLite stores (graph/doc/sql/vector) into a
PostgreSQL PG-unified target (STORAGE_MODE=pg).

WHAT THIS DOES: row-for-row copy, widening TEXT-JSON columns to JSONB and
TEXT timestamps to TIMESTAMPTZ. NO re-embedding — the vector table's raw
float32 vectors are copied as-is from the source sqlite-vec (vec0) table via
``vec_to_json(embedding)`` (a lossless text serialisation Postgres accepts
directly as a ``vector`` literal). If the source vec0 table has an
``embedding_bit`` column (VECTOR_ANN=binary backfill,
scripts/migrate_add_binary_quantization.py), it is IGNORED — PgVectorStore
has no binary 2-stage path (its HNSW index already serves global search at
p95 6.44ms, see docs/pgvector-migration-plan.md "WHY NOT BINARY 2-STAGE").

SOURCE SAFETY: unlike migrate_add_binary_quantization.py (which rebuilds the
source table in place and therefore REQUIRES --backup-to), this script only
ever opens the source SQLite files with plain SELECT statements — it never
issues a single write against them. The originals are guaranteed unmodified
by construction; there is deliberately no --backup-to flag here. Instead,
pass --verify to have the script assert (exit non-zero on mismatch) that the
target holds what the source did after loading. SQL-store tables with a natural
key are checked by key rather than by count, because upserting into a target
that already held rows of its own makes it a superset — a count comparison
would call that a failure, while a genuine drop offset by a pre-existing row
would pass. Everything else is still compared by count.

IDEMPOTENCY: re-running is safe, but how differs by table.

SQL-store tables with a natural key upsert every row on every run
(``ON CONFLICT DO UPDATE``). They are deliberately NOT skipped when the target
row count already equals the source count: equal counts do not mean equal rows,
and a target initialised independently with one different user would otherwise
keep its own row while the source user is never copied — silently preserving
the wrong credentials. ``impact_records``/``lever_simulations`` have no natural
key (SERIAL id only, not FK-referenced elsewhere) so they cannot upsert; for
those two a *partial* target (count > 0 but != source count) is left untouched
and reported rather than blindly re-inserted, which would duplicate history.
Operators should inspect and clear them manually before re-running in that edge
case.

The graph/doc/vector migrations still skip on equal counts. Their rows are not
credentials, and reworking them is outside this script's SQL-store scope.

SCHEMA CREATION: this script does not hand-roll target DDL for graph/doc —
it imports and instantiates PGGraphStore/PgDocStore (their constructors run
ensure-schema idempotently). For the vector table it deliberately does NOT
construct PgVectorStore before the bulk load (that would build the HNSW
index against an empty table and then maintain it incrementally on every
INSERT — much slower at scale than a bulk load followed by one index build).
Instead it replicates just the base-table DDL inline, bulk-loads, and only
THEN constructs PgVectorStore — whose ensure-schema idempotently creates the
HNSW index post-load, honouring the `maintenance_work_mem`/
`max_parallel_maintenance_workers` preflight settings already encoded in
pg_vector_store.py (this script does not duplicate those settings).

Usage:
    python scripts/migrate_sqlite_to_pg.py \\
        --data-dir /path/to/localcrab/data --pg-url postgresql://... \\
        [--only graph,doc,sql,vector] [--limit 1000] [--batch 1000] \\
        [--dry-run] [--verify]

    # Individual file overrides (e.g. ad-hoc backup copies with non-default
    # names) take precedence over --data-dir:
    python scripts/migrate_sqlite_to_pg.py --pg-url postgresql://... \\
        --vector-db /path/vectors-20260702-pre-ann.db \\
        --graph-db /path/graph-pgbench.db \\
        --doc-db /path/doc_store-pgbench.db --only graph,doc,vector --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from typing import Any

import _migration_tables as mt

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_ident(name: str, what: str) -> str:
    """Table/schema names are f-string-interpolated into DDL/DML below (same
    pattern as pg_vector_store.py's _IDENT_RE) — reject anything that is not
    a plain identifier before it ever reaches SQL."""
    if not _IDENT_RE.match(name):
        raise ValueError(f"Unsafe {what}: {name!r}")
    return name


# ---------------------------------------------------------------------------
# Source (SQLite) readers
# ---------------------------------------------------------------------------


def _sqlite_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _vec_sqlite_conn(db_path: str) -> sqlite3.Connection:
    import sqlite_vec

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _fetch_batches(
    conn: sqlite3.Connection, sql: str, batch: int, limit: int | None
) -> Iterator[list[sqlite3.Row]]:
    if limit is not None:
        sql = f"{sql} LIMIT {int(limit)}"
    cur = conn.execute(sql)
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return
        yield rows


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _pg_count(pg_conn: Any, text: Any, table: str) -> int:
    try:
        row = pg_conn.execute(text(f"SELECT count(*) FROM {table}")).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# #144/#151 fail-closed guard — SQLStore may own a table that migrate_sql's
# SQL_TABLE_SPECS (scripts/_migration_tables.py) does not cover, e.g. a future
# 9th table added there and forgotten here. A run that completes without it
# silently drops that table's data while still printing RESULT: PASS —
# --verify would not catch it either, since it only re-checks tables already
# present in migrate_sql's own count map. Detect this BEFORE any work happens
# (dry-run included) and require an explicit opt-in to proceed. The set is
# derived (mt.unmigrated_tables), not a literal, so it stays empty as long as
# SQL_TABLE_SPECS matches what SQLStore actually creates.
# ---------------------------------------------------------------------------


def _sqlite_table_names(db_path: str) -> set[str]:
    """Real (non-internal) table names in a SQLite file."""
    conn = _sqlite_conn(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows if not r[0].startswith("sqlite_")}


# ---------------------------------------------------------------------------
# Per-store migration
# ---------------------------------------------------------------------------


def migrate_graph(
    db_path: str, engine: Any, schema: str, batch: int, limit: int | None, dry_run: bool
) -> dict[str, int]:
    from sqlalchemy import text

    from opencrab.stores.pg_graph_store import PGGraphStore

    if not os.path.exists(db_path):
        print(f"# [graph] source not found, skipping: {db_path}")
        return {}

    src = _sqlite_conn(db_path)
    src_nodes = _count(src, "graph_nodes")
    src_edges = _count(src, "graph_edges")
    print(f"# [graph] source rows: nodes={src_nodes} edges={src_edges}")

    if dry_run:
        print(f"# [graph] dry-run: would upsert into \"{schema}\".graph_nodes/graph_edges")
        src.close()
        return {"graph_nodes": src_nodes, "graph_edges": src_edges}

    store = PGGraphStore(engine, schema=schema)  # idempotent ensure-schema
    if not store.available:
        raise RuntimeError("PGGraphStore is not available (connection failed).")
    t = f'"{schema}"'

    with engine.begin() as conn:
        existing = _pg_count(conn, text, f"{t}.graph_nodes")
    copied_nodes = 0
    if existing == src_nodes and src_nodes > 0:
        print(f"# [graph] graph_nodes already has {existing} rows == source — skip.")
    else:
        node_sql = text(
            f"INSERT INTO {t}.graph_nodes (node_type, node_id, space_id, properties) "
            "VALUES (:node_type, :node_id, :space_id, (:properties)::jsonb) "
            "ON CONFLICT (node_type, node_id) DO UPDATE SET "
            "space_id = EXCLUDED.space_id, properties = EXCLUDED.properties"
        )
        for rows in _fetch_batches(src, "SELECT node_type, node_id, space_id, properties FROM graph_nodes", batch, limit):
            params = [dict(r) for r in rows]
            with engine.begin() as conn:
                conn.execute(node_sql, params)
            copied_nodes += len(params)
        print(f"#   graph_nodes: {copied_nodes} copied")

    with engine.begin() as conn:
        existing_e = _pg_count(conn, text, f"{t}.graph_edges")
    copied_edges = 0
    if existing_e == src_edges and src_edges > 0:
        print(f"# [graph] graph_edges already has {existing_e} rows == source — skip.")
    else:
        edge_sql = text(
            f"INSERT INTO {t}.graph_edges (from_type, from_id, relation, to_type, to_id, properties) "
            "VALUES (:from_type, :from_id, :relation, :to_type, :to_id, (:properties)::jsonb) "
            "ON CONFLICT (from_type, from_id, relation, to_type, to_id) DO UPDATE SET "
            "properties = EXCLUDED.properties"
        )
        for rows in _fetch_batches(
            src,
            "SELECT from_type, from_id, relation, to_type, to_id, properties FROM graph_edges",
            batch,
            limit,
        ):
            params = [dict(r) for r in rows]
            with engine.begin() as conn:
                conn.execute(edge_sql, params)
            copied_edges += len(params)
        print(f"#   graph_edges: {copied_edges} copied")

    src.close()
    store.close()
    return {"graph_nodes": src_nodes, "graph_edges": src_edges}


def migrate_doc(
    db_path: str, engine: Any, schema: str, batch: int, limit: int | None, dry_run: bool
) -> dict[str, int]:
    from sqlalchemy import text

    from opencrab.stores.pg_doc_store import PgDocStore

    if not os.path.exists(db_path):
        print(f"# [doc] source not found, skipping: {db_path}")
        return {}

    src = _sqlite_conn(db_path)
    counts = {
        "doc_nodes": _count(src, "doc_nodes"),
        "doc_sources": _count(src, "doc_sources"),
        "audit_log": _count(src, "audit_log"),
    }
    print(f"# [doc] source rows: {counts}")

    if dry_run:
        print(f"# [doc] dry-run: would upsert into \"{schema}\".doc_nodes/doc_sources/audit_log")
        src.close()
        return counts

    store = PgDocStore(engine, schema=schema)  # idempotent ensure-schema
    if not store.available:
        raise RuntimeError("PgDocStore is not available (connection failed).")
    t = f'"{schema}"'

    def _copy(table: str, cols: list[str], conflict_key: str, src_sql: str) -> int:
        with engine.begin() as conn:
            existing = _pg_count(conn, text, f"{t}.{table}")
        if existing == counts[table] and counts[table] > 0:
            print(f"# [doc] {table} already has {existing} rows == source — skip.")
            return 0
        casted = ", ".join(
            f"(:{c})::jsonb" if c in ("properties", "metadata", "details")
            else f"(:{c})::timestamptz" if c in ("updated_at", "ingested_at", "timestamp")
            else f":{c}"
            for c in cols
        )
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in conflict_key.split(","))
        sql = text(
            f"INSERT INTO {t}.{table} ({', '.join(cols)}) VALUES ({casted}) "
            f"ON CONFLICT ({conflict_key}) DO UPDATE SET {updates}"
        )
        copied = 0
        for rows in _fetch_batches(src, src_sql, batch, limit):
            params = [dict(r) for r in rows]
            with engine.begin() as conn:
                conn.execute(sql, params)
            copied += len(params)
        print(f"#   {table}: {copied} copied")
        return copied

    _copy(
        "doc_nodes",
        ["space", "node_id", "node_type", "properties", "updated_at"],
        "space, node_id",
        "SELECT space, node_id, node_type, properties, updated_at FROM doc_nodes",
    )
    _copy(
        "doc_sources",
        ["source_id", "text", "metadata", "ingested_at"],
        "source_id",
        "SELECT source_id, text, metadata, ingested_at FROM doc_sources",
    )
    _copy(
        "audit_log",
        ["event_id", "event_type", "subject_id", "details", "timestamp"],
        "event_id",
        "SELECT event_id, event_type, subject_id, details, timestamp FROM audit_log",
    )

    src.close()
    store.close()
    return counts


def _row_key(spec: mt.SqlTableSpec, row: sqlite3.Row) -> str | None:
    """Natural-key value for an error message, or None for the two tables that
    have no conflict_key — safe_error_text only accepts a declared
    conflict_key value in ``key=``, so there is nothing safe to pass there."""
    if spec.conflict_key is None:
        return None
    return ",".join(str(row[c]) for c in spec.conflict_key)


def _missing_source_keys(
    sql_db_path: str, engine: Any, text: Any, spec: mt.SqlTableSpec
) -> set[tuple]:
    """Source natural keys the target does not have.

    Compared as tuples, not as a joined string: ``('a,b', 'c')`` and
    ``('a', 'b,c')`` would collide and hide a real gap. Both key sets are read
    whole and diffed in Python rather than probing key by key -- one statement
    instead of one per row, and SQL equality would not match NULLs if a key
    column ever became nullable. Every conflict_key column is VARCHAR on
    PostgreSQL and TEXT on SQLite, so both sides come back as ``str``.
    """
    cols = ", ".join(spec.conflict_key or ())
    src = _sqlite_conn(sql_db_path)
    try:
        src_keys = {tuple(r) for r in src.execute(f"SELECT {cols} FROM {spec.name}")}
    finally:
        src.close()
    with engine.begin() as conn:
        dst_keys = {tuple(r) for r in conn.execute(text(f"SELECT {cols} FROM {spec.name}"))}
    return src_keys - dst_keys


def check_target_only_auth(engine: Any, sql_db_path: str) -> None:
    """Refuse a target holding credentials the source does not, before any write.

    Called from ``main()`` ahead of every per-store migration, not just from
    ``migrate_sql``: with the default ``--only`` the graph and doc stores are
    copied first, so a refusal raised inside ``migrate_sql`` would arrive after
    two target stores had already been modified -- and unlike the reverse
    direction, nothing here backs the target up.

    A target table that does not exist yet cannot hold anything, so its absence
    is simply not a finding.
    """
    from sqlalchemy import inspect, text

    if not os.path.exists(sql_db_path):
        return
    insp = inspect(engine)
    src = _sqlite_conn(sql_db_path)
    try:
        source_tables = _sqlite_table_names(sql_db_path)
        found: dict[str, list[str]] = {}
        for table in mt.AUTH_CREDENTIAL_TABLES:
            if not insp.has_table(table):
                continue
            spec = mt.SPEC_BY_NAME[table]
            assert spec.conflict_key is not None
            col = ", ".join(spec.conflict_key)
            src_keys: set[tuple] = set()
            if table in source_tables:
                src_keys = {tuple(r) for r in src.execute(f"SELECT {col} FROM {table}")}
            with engine.begin() as conn:
                dst_keys = {tuple(r) for r in conn.execute(text(f"SELECT {col} FROM {table}"))}
            extra = dst_keys - src_keys
            if extra:
                found[table] = [",".join(str(v) for v in k) for k in extra]
    finally:
        src.close()
    if found:
        raise mt.TargetOnlyAuthError(mt.target_only_report(found))


def _target_only_auth(engine: Any, text: Any, src: Any, source_tables: set[str]) -> dict[str, list[str]]:
    """Credential keys the PostgreSQL target has that the source does not.

    No local-identity exemption on this side: the target is a PostgreSQL
    deployment, not the source installation's own identity store, so an
    ``is_local`` user sitting there is exactly the credential this refuses to
    silently keep -- its tokens would authenticate as a local principal.

    ``users``/``api_tokens`` are checked even when the source lacks the table
    entirely; a source that does not know a table cannot vouch for rows in it.
    """
    found: dict[str, list[str]] = {}
    for table in mt.AUTH_CREDENTIAL_TABLES:
        key = mt.SPEC_BY_NAME[table].conflict_key
        assert key is not None
        col = ", ".join(key)
        src_keys: set[tuple] = set()
        if table in source_tables:
            src_keys = {tuple(r) for r in src.execute(f"SELECT {col} FROM {table}")}
        with engine.begin() as conn:
            dst_keys = {tuple(r) for r in conn.execute(text(f"SELECT {col} FROM {table}"))}
        extra = dst_keys - src_keys
        if extra:
            found[table] = [",".join(str(v) for v in k) for k in extra]
    return found


def migrate_sql(
    db_path: str,
    engine: Any,
    pg_url: str,
    batch: int,
    limit: int | None,
    dry_run: bool,
    allow_target_only_auth: bool = False,
) -> dict[str, int | None]:
    from sqlalchemy import text

    from opencrab.stores.sql_store import SQLStore

    if not os.path.exists(db_path):
        print(f"# [sql] source not found, skipping: {db_path}")
        return {}

    src = _sqlite_conn(db_path)
    source_tables = _sqlite_table_names(db_path)
    # None means "absent from source", not zero rows — keeps it out of the
    # int-count comparisons below (_pg_count etc.) and lets --verify report
    # [SKIP] instead of a false MISMATCH for a pre-#144 source.
    counts: dict[str, int | None] = {
        spec.name: _count(src, spec.name) if spec.name in source_tables else None
        for spec in mt.SQL_TABLE_SPECS
    }
    present_specs = [spec for spec in mt.SQL_TABLE_SPECS if counts[spec.name] is not None]
    absent = [spec.name for spec in mt.SQL_TABLE_SPECS if counts[spec.name] is None]
    print(f"# [sql] source rows: {counts}")
    if absent:
        print(f"# [sql] absent in source, not copied: {absent}")

    if dry_run:
        # No target engine exists in dry-run, so only the source side of the
        # column contract (resolve_columns rule 1) can be checked here — the
        # target-catalogue rules (2-4) need SQLStore's ensure-schema, which
        # only runs below, past this early return.
        for spec in present_specs:
            src_cols = set(mt.sqlite_columns(src, spec.name)) - spec.exclude_columns
            missing = spec.required_columns - src_cols
            if missing:
                raise mt.MigrationError(
                    f"source table {spec.name}: required column(s) {sorted(missing)} missing"
                )
        names = ",".join(spec.name for spec in present_specs)
        print(f"# [sql] dry-run: would insert into public.{{{names}}} (SQLStore DDL)")
        print("# [sql] dry-run: target schema not validated (no target engine in dry-run)")
        src.close()
        return counts

    # NOTE: use the caller-supplied DSN, not str(engine.url) — SQLAlchemy masks
    # the password as "***" when an Engine URL is stringified, which would make
    # SQLStore fail authentication.
    store = SQLStore(url=pg_url)  # idempotent ensure-schema (own engine; PG DDL)
    if not store.available:
        raise mt.MigrationError("SQLStore is not available (connection failed).")

    # Before the first INSERT, like the #144 guard and the corruption scan: a
    # target holding credentials the source never had would otherwise be left
    # working, with every source key present so the key comparison passes.
    if not allow_target_only_auth:
        target_only = _target_only_auth(engine, text, src, source_tables)
        if target_only:
            raise mt.TargetOnlyAuthError(mt.target_only_report(target_only))

    # Column lists and boolean/timestamp membership are derived once per table
    # here and reused for both the pre-scan and the copy below — deriving them
    # twice would let the two stages silently disagree. This also does the
    # schema validation (resolve_columns rules 1-4) for every present table
    # before any row is scanned or written. Reading the PG catalogue requires
    # SQLStore's ensure-schema above to have already run, or a fresh target
    # would fail rule 2 (no columns yet) and see zero boolean/timestamp
    # columns.
    copy_cols: dict[str, list[str]] = {}
    bool_cols: dict[str, set[str]] = {}
    ts_cols: dict[str, set[str]] = {}
    for spec in present_specs:
        src_cols = mt.sqlite_columns(src, spec.name)
        dst_cols, booleans, timestamps = mt.pg_typed_columns(engine, spec.name)
        cols = mt.resolve_columns(spec, src_cols, dst_cols)
        copy_cols[spec.name] = cols
        bool_cols[spec.name] = booleans & set(cols)
        ts_cols[spec.name] = timestamps & set(cols)

    # Full pre-scan of every boolean/timestamp value, over every present
    # table, before the first INSERT anywhere below — a corrupted value in the
    # last table must still abort with zero rows written to any sql-store
    # table (graph/doc already ran earlier in main() and are outside this
    # guarantee). A source write between this scan and the copy loop below
    # (TOCTOU) would weaken that guarantee, but the copy loop's own strict
    # conversion still refuses a newly-corrupted row rather than admitting it.
    for spec in present_specs:
        bools, timestamps = bool_cols[spec.name], ts_cols[spec.name]
        if not bools and not timestamps:
            continue
        cols = copy_cols[spec.name]
        src_sql = f"SELECT {', '.join(cols)} FROM {spec.name}"
        # --limit caps the scan the same as the copy below, but LIMIT without
        # ORDER BY does not guarantee the same row set between two separate
        # queries — acceptable because --limit is a smoke-test aid only.
        for rows in _fetch_batches(src, src_sql, batch, limit):
            for row in rows:
                key = _row_key(spec, row)
                for col in bools:
                    mt.to_pg_bool(row[col], table=spec.name, column=col, key=key)
                for col in timestamps:
                    mt.check_sqlite_timestamp(row[col], table=spec.name, column=col, key=key)

    def _copy_natural_key(spec: mt.SqlTableSpec) -> int:
        table = spec.name
        cols = copy_cols[table]
        bools = bool_cols[table]
        conflict_key = ", ".join(spec.conflict_key) if spec.conflict_key else None
        with engine.begin() as conn:
            existing = _pg_count(conn, text, table)
        if conflict_key is None and existing == counts[table] and counts[table] > 0:
            print(f"# [sql] {table} already has {existing} rows == source — skip.")
            return 0
        if existing > 0 and existing != counts[table] and conflict_key is None:
            print(
                f"! [sql] {table} has {existing} rows (partial, no natural key to upsert on); "
                f"source has {counts[table]}. Leaving untouched — inspect manually."
            )
            return 0
        placeholders = ", ".join(f":{c}" for c in cols)
        sql_str = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        if conflict_key:
            updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in spec.conflict_key)
            sql_str += f" ON CONFLICT ({conflict_key}) DO UPDATE SET {updates}"
        sql = text(sql_str)
        tz_sql = text("SET LOCAL TIME ZONE 'UTC'")
        src_sql = f"SELECT {', '.join(cols)} FROM {table}"
        copied = 0
        for rows in _fetch_batches(src, src_sql, batch, limit):
            params = []
            for row in rows:
                p = dict(row)
                for col in bools:
                    p[col] = mt.to_pg_bool(p[col], table=table, column=col, key=_row_key(spec, row))
                params.append(p)
            try:
                with engine.begin() as conn:
                    # SET LOCAL, not SET: it reverts at transaction end, so it
                    # cannot leak the TZ interpretation onto this pooled
                    # connection when a later batch (or migrate_doc/
                    # migrate_vector, sharing the same engine) reuses it.
                    conn.execute(tz_sql)
                    conn.execute(sql, params)
            except Exception as exc:  # noqa: BLE001 -- must not leak row values (design section 7)
                # executemany failures cannot be attributed to a single row,
                # so key= is intentionally omitted here.
                print(f"! [sql] {table}: {mt.safe_error_text(exc, table=table)}")
                raise
            copied += len(params)
        print(f"#   {table}: {copied} copied")
        return copied

    for spec in present_specs:
        _copy_natural_key(spec)

    src.close()
    # SQLStore has no close(); dispose its private engine directly (the store
    # was created here solely to run its idempotent PG ensure-schema).
    if getattr(store, "_engine", None) is not None:
        store._engine.dispose()
    return counts


def migrate_vector(
    db_path: str,
    engine: Any,
    src_table: str,
    dst_table: str,
    dim: int,
    ef_search: int,
    batch: int,
    limit: int | None,
    dry_run: bool,
) -> dict[str, int]:
    from sqlalchemy import text

    if not os.path.exists(db_path):
        print(f"# [vector] source not found, skipping: {db_path}")
        return {}

    _check_ident(src_table, "vector-src-table")
    _check_ident(dst_table, "vector-dst-table")
    src = _vec_sqlite_conn(db_path)
    src_count = _count(src, src_table)
    print(f"# [vector] source '{src_table}' rows: {src_count} (dim={dim})")

    if dry_run:
        print(f"# [vector] dry-run: would upsert into public.{dst_table} (raw float copy, no re-embed)")
        src.close()
        return {dst_table: src_count}

    # ---- base table only (no HNSW yet) — see module docstring rationale ----
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {dst_table} ("
                "node_id TEXT PRIMARY KEY, pack_id TEXT, "
                f"embedding vector({dim}) NOT NULL, document TEXT, metadata JSONB)"
            )
        )
        conn.execute(
            text(f"CREATE INDEX IF NOT EXISTS {dst_table}_pack_id_idx ON {dst_table} (pack_id)")
        )
        existing = _pg_count(conn, text, dst_table)

    copied = 0
    if existing == src_count and src_count > 0:
        print(f"# [vector] {dst_table} already has {existing} rows == source — skip bulk load.")
    else:
        insert_sql = (
            f"INSERT INTO {dst_table} (node_id, pack_id, embedding, document, metadata) "
            "VALUES %s ON CONFLICT (node_id) DO UPDATE SET "
            "pack_id = EXCLUDED.pack_id, embedding = EXCLUDED.embedding, "
            "document = EXCLUDED.document, metadata = EXCLUDED.metadata"
        )
        raw = engine.raw_connection()
        try:
            from psycopg2.extras import execute_values

            t0 = time.perf_counter()
            cur = raw.cursor()
            for rows in _fetch_batches(
                src,
                f"SELECT node_id, pack_id, vec_to_json(embedding) AS embedding, document, metadata"
                f" FROM {src_table}",
                batch,
                limit,
            ):
                values = [
                    (r["node_id"], r["pack_id"], r["embedding"], r["document"], r["metadata"] or "{}")
                    for r in rows
                ]
                execute_values(
                    cur,
                    insert_sql,
                    values,
                    template="(%s, %s, %s::vector, %s, %s::jsonb)",
                )
                raw.commit()
                copied += len(values)
                if copied % 5000 < batch:
                    rate = copied / max(time.perf_counter() - t0, 1e-6)
                    print(f"#   {dst_table}: {copied}/{src_count} ({rate:.0f} rows/s)")
            cur.close()
        finally:
            raw.close()
        print(f"#   {dst_table}: {copied} copied total")

    # ---- NOW build the HNSW index (post-load) via PgVectorStore ensure-schema ----
    def _unused_ef(_texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding_function should not be invoked during migration.")

    from opencrab.stores.pg_vector_store import PgVectorStore

    store = PgVectorStore(
        engine,
        embedding_function=_unused_ef,
        dim=dim,
        collection_name=dst_table,
        ef_search=ef_search,
    )
    if not store.available:
        raise RuntimeError("PgVectorStore is not available (connection failed) after bulk load.")
    store.close()

    src.close()
    return {dst_table: src_count}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=None, help="dir with graph.db/doc_store.db/opencrab.db/vectors.db")
    ap.add_argument("--graph-db", default=None, help="override graph.db path")
    ap.add_argument("--doc-db", default=None, help="override doc_store.db path")
    ap.add_argument("--sql-db", default=None, help="override opencrab.db path")
    ap.add_argument("--vector-db", default=None, help="override vectors.db path")
    ap.add_argument("--pg-url", default=None, help="target PostgreSQL DSN (default: settings.postgres_url)")
    ap.add_argument("--pg-schema", default="public", help="graph/doc target schema (default: public)")
    ap.add_argument(
        "--vector-src-table", default=None, help="source vec0 table (default: settings.vector_collection)"
    )
    ap.add_argument(
        "--vector-dst-table", default=None, help="target pgvector table (default: settings.embed_collection)"
    )
    ap.add_argument("--only", default="graph,doc,sql,vector", help="comma list of stores to migrate")
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--limit", type=int, default=None, help="cap rows copied per table (smoke-test aid)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true", help="assert PG row counts == source after run")
    ap.add_argument(
        "--allow-target-only-auth",
        action="store_true",
        help="proceed even though the target holds users/api_tokens rows the "
        "source does not; without this flag such a target aborts the run, so a "
        "credential the source does not know about cannot silently keep working",
    )
    ap.add_argument(
        "--allow-unmigrated",
        action="store_true",
        help="proceed even though the source has SQLStore-owned table(s) this "
        "migration does not copy (excluded tables are listed in the final "
        "summary); without this flag such a source aborts the run",
    )
    args = ap.parse_args()

    from opencrab.config import get_settings

    settings = get_settings()
    data_dir = args.data_dir or settings.local_data_dir
    graph_db = args.graph_db or os.path.join(data_dir, "graph.db")
    doc_db = args.doc_db or os.path.join(data_dir, "doc_store.db")
    sql_db = args.sql_db or os.path.join(data_dir, "opencrab.db")
    vector_db = args.vector_db or os.path.join(data_dir, settings.vector_db_file)
    pg_url = args.pg_url or settings.postgres_url
    vector_src_table = args.vector_src_table or settings.vector_collection
    vector_dst_table = args.vector_dst_table or settings.embed_collection
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    _check_ident(args.pg_schema, "pg-schema")

    print(f"# data-dir : {data_dir}")
    print(f"# pg-url   : {pg_url}")
    print(f"# only     : {sorted(only)}")
    print(f"# dry-run  : {args.dry_run}")
    if args.dry_run:
        # The target-only credential guard needs the target catalogue, which a
        # dry run never opens -- its PASS does not cover that check.
        print("# note     : dry-run does not check for target-only credentials")

    unmigrated: list[str] = []
    if "sql" in only and os.path.exists(sql_db):
        unmigrated = mt.unmigrated_tables(_sqlite_table_names(sql_db))
        if unmigrated and not args.allow_unmigrated:
            print(
                f"! source has SQLStore table(s) {unmigrated} that this "
                "migration does not copy — completing would silently drop "
                "that data while still reporting success. Pass "
                "--allow-unmigrated to proceed anyway (excluded tables will "
                "be listed in the final summary)."
            )
            return 2
        if unmigrated:
            print(f"# [sql] --allow-unmigrated: excluding {unmigrated} from migration")

    from sqlalchemy import create_engine

    engine = create_engine(pg_url, pool_pre_ping=True, hide_parameters=True) if not args.dry_run else None

    if engine is not None and "sql" in only and not args.allow_target_only_auth:
        try:
            check_target_only_auth(engine, sql_db)
        except mt.TargetOnlyAuthError as exc:
            print(f"! {mt.safe_error_text(exc)}")
            return 2
        except Exception as exc:
            # Not swallowed: a guard that cannot complete leaves the question
            # unanswered, and continuing would let migrate_graph/migrate_doc
            # write before migrate_sql re-runs the check -- exactly the partial
            # write this hoist exists to prevent. Failing here costs nothing,
            # since an unreachable target would fail on the next statement too.
            print(f"! credential guard could not run: {mt.safe_error_text(exc)}")
            return 1

    source_counts: dict[str, dict[str, int | None]] = {}
    try:
        if "graph" in only:
            source_counts["graph"] = migrate_graph(
                graph_db, engine, args.pg_schema, args.batch, args.limit, args.dry_run
            )
        if "doc" in only:
            source_counts["doc"] = migrate_doc(
                doc_db, engine, args.pg_schema, args.batch, args.limit, args.dry_run
            )
        if "sql" in only:
            source_counts["sql"] = migrate_sql(
                sql_db,
                engine,
                pg_url,
                args.batch,
                args.limit,
                args.dry_run,
                args.allow_target_only_auth,
            )
        if "vector" in only:
            source_counts["vector"] = migrate_vector(
                vector_db,
                engine,
                vector_src_table,
                vector_dst_table,
                settings.embed_dim,
                settings.pg_ef_search,
                args.batch,
                args.limit,
                args.dry_run,
            )
    except mt.TargetOnlyAuthError as exc:
        # Same exit code as the #144 guard: both refuse before doing any work,
        # and both need an operator decision rather than a bug report.
        print(f"! {mt.safe_error_text(exc)}")
        return 2
    except Exception as exc:
        # Constraint names and SQLSTATE only: a driver message can quote the
        # row that failed, and one of those rows is a token hash.
        print(f"! migration failed: {mt.safe_error_text(exc)}")
        print("! the driver's own message is withheld — reproduce against the source to see it")
        return 1

    excluded_note = f" (excluded: {unmigrated})" if unmigrated else ""

    if args.dry_run:
        print(f"RESULT: PASS (dry-run, no writes){excluded_note}")
        return 0

    if args.verify:
        from sqlalchemy import text

        print("# --verify: comparing source vs PG row counts")
        mismatches = []
        with engine.begin() as conn:
            for group, tables in source_counts.items():
                for table, src_n in tables.items():
                    if group in ("graph",):
                        full_table = f'"{args.pg_schema}".{table}'
                    elif group == "doc":
                        full_table = f'"{args.pg_schema}".{table}'
                    else:
                        full_table = table
                    pg_n = _pg_count(conn, text, full_table)
                    if src_n is None:
                        # Absent from the source (pre-#144 or similar) is not
                        # a mismatch -- see _migration_tables docs.
                        print(f"#   {group}.{table}: source=absent pg={pg_n} [SKIP]")
                        if pg_n > 0:
                            print(
                                f"!   {group}.{table}: source is absent but the "
                                f"target already has {pg_n} row(s)"
                            )
                        continue
                    spec = mt.SPEC_BY_NAME.get(table) if group == "sql" else None
                    if spec is not None and spec.conflict_key:
                        # Upserting into a target that held rows of its own
                        # makes it a superset, so the count is informational
                        # here and the key set is what decides.
                        gap = _missing_source_keys(sql_db, engine, text, spec)
                        status = "OK" if not gap else "MISMATCH"
                        print(
                            f"#   {group}.{table}: source={src_n} pg={pg_n} "
                            f"missing_keys={len(gap)} [{status}]"
                        )
                        if gap:
                            mismatches.append((group, table, src_n, pg_n))
                        continue
                    status = "OK" if pg_n == src_n else "MISMATCH"
                    print(f"#   {group}.{table}: source={src_n} pg={pg_n} [{status}]")
                    if pg_n != src_n:
                        mismatches.append((group, table, src_n, pg_n))
        if mismatches:
            print(f"RESULT: FAIL ({len(mismatches)} mismatches)")
            return 5
        print(f"RESULT: PASS (source rows are all present in the target){excluded_note}")
        return 0

    print(f"RESULT: PASS (migration complete; pass --verify to assert row-count parity){excluded_note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
