"""Unit tests for opencrab/stores/_sql_dialect.py (SqlDialect) and the
DOC_STORE_SCHEMA it renders in _sql_doc_base.py.

These are pure string/behavior tests — no DB connection needed for the
dataclass logic itself, but the SQLite branch is additionally executed
against a real in-memory sqlite3 connection (render_ddl -> CREATE TABLE,
insert/upsert -> real writes) as end-to-end proof the generated SQL is not
just structurally plausible but actually valid SQLite syntax.
"""

from __future__ import annotations

import sqlite3

import pytest

from opencrab.stores._sql_dialect import (
    POSTGRES,
    SQLITE,
    Column,
    IndexSpec,
    SchemaSpec,
    TableSpec,
)
from opencrab.stores._sql_doc_base import DOC_STORE_SCHEMA

# ---------------------------------------------------------------------------
# now_expr / bind_value_for_timestamp / json_get
# ---------------------------------------------------------------------------


def test_now_expr_per_dialect():
    assert SQLITE.now_expr() == "datetime('now')"
    assert POSTGRES.now_expr() == "NOW()"


def test_bind_value_for_timestamp():
    from datetime import UTC, datetime

    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert SQLITE.bind_value_for_timestamp(dt) == dt.isoformat()
    assert POSTGRES.bind_value_for_timestamp(dt) is dt


def test_json_get_per_dialect():
    assert SQLITE.json_get("properties", "pack_id") == "json_extract(properties, '$.pack_id')"
    assert POSTGRES.json_get("properties", "pack_id") == "properties->>'pack_id'"


def test_json_index_expr_per_dialect():
    """PG needs an extra paren wrap for a functional index over an operator
    expression (``->>``); SQLite's json_extract() is already a function call
    so no extra wrap is needed — see _sql_graph_base.py's idx_nodes_pack."""
    assert SQLITE.json_index_expr("properties", "pack_id") == "json_extract(properties, '$.pack_id')"
    assert POSTGRES.json_index_expr("properties", "pack_id") == "(properties->>'pack_id')"


def test_list_packs_pg_json_get_is_parenthesized_in_concat():
    """Regression guard: _sql_graph_base.py's list_packs() builds
    `'dataset:' || {json_get(...)}` — on PG, `||` binds tighter than `->>`,
    so an unparenthesized `'dataset:' || properties->>'pack_id'` parses as
    `('dataset:' || properties) ->> 'pack_id'` and throws
    InvalidTextRepresentation at runtime (concatenating text with the raw
    jsonb column) instead of raising at review time. This bug shipped once
    (caught by the PG parity suite, not by any SQLite-only unit test, since
    SQLite's json_extract() is a function call with no such precedence
    trap) — this test pins the fix at the dialect/SQL-text level so it can't
    silently regress even without a live PG connection."""
    from opencrab.stores._sql_graph_base import _SqlGraphStoreBase

    class _CapturingPgDouble(_SqlGraphStoreBase):
        _dialect = POSTGRES

        def __init__(self) -> None:
            self._available = True
            self.captured_sql = ""

        def _table(self, name: str) -> str:
            return name

        def _fetch_all(self, sql, params):
            self.captured_sql = sql
            return []

        def _fetch_one(self, sql, params):
            raise NotImplementedError

        def _exec_write(self, sql, params):
            raise NotImplementedError

        def _exec_write_many(self, statements):
            raise NotImplementedError

        def _exec_write_batch(self, sql, params_list):
            raise NotImplementedError

        def _require_available(self) -> None:
            pass

    store = _CapturingPgDouble()
    store.list_packs()
    assert "'dataset:' || (properties->>'pack_id')" in store.captured_sql


# ---------------------------------------------------------------------------
# insert / upsert SQL text
# ---------------------------------------------------------------------------


def test_sqlite_insert_plain():
    """SQLite renders NAMED (:col) placeholders too (sqlite3 supports
    paramstyle "named" natively), not the qmark (?) the hand-written
    pre-refactor code uses — this lets _sql_doc_base.py pass one params dict
    to either backend. See module docstring's "WHAT IT DELIBERATELY DOES NOT
    COVER" section for why placeholder_style itself still says "qmark"."""
    sql = SQLITE.insert("audit_log", ["event_id", "event_type", "details"], json_columns=["details"])
    assert sql == (
        "INSERT INTO audit_log(event_id, event_type, details)\n"
        "VALUES (:event_id, :event_type, :details)"
    )
    assert "CAST" not in sql  # SQLite has no jsonb cast — JSON is plain TEXT


def test_postgres_insert_plain_casts_json_columns():
    sql = POSTGRES.insert(
        '"s1".audit_log', ["event_id", "event_type", "details"], json_columns=["details"]
    )
    assert sql == (
        'INSERT INTO "s1".audit_log(event_id, event_type, details)\n'
        "VALUES (:event_id, :event_type, CAST(:details AS jsonb))"
    )


def test_sqlite_upsert_on_conflict_do_update():
    """SQLite's upsert uses ON CONFLICT DO UPDATE, same as PG — NOT INSERT OR
    REPLACE, which would allocate a new rowid on every conflict (delete +
    reinsert) and destabilize no-ORDER-BY scan order across re-upserts of an
    already-seen key. See module docstring's "ROWID STABILITY"."""
    sql = SQLITE.upsert(
        "doc_nodes",
        ["space", "node_id", "node_type", "properties", "updated_at"],
        conflict_cols=["space", "node_id"],
        update_cols=["node_type", "properties", "updated_at"],
        json_columns=["properties"],
    )
    assert sql.startswith("INSERT INTO doc_nodes(")
    assert "OR REPLACE" not in sql
    assert "ON CONFLICT (space, node_id) DO UPDATE SET" in sql
    assert "node_type = EXCLUDED.node_type" in sql
    assert "properties = EXCLUDED.properties" in sql
    assert "updated_at = EXCLUDED.updated_at" in sql
    assert "?" not in sql  # named placeholders, not qmark — see test_sqlite_insert_plain
    for col in ("space", "node_id", "node_type", "properties", "updated_at"):
        assert f":{col}" in sql


def test_postgres_upsert_on_conflict_do_update():
    sql = POSTGRES.upsert(
        '"s1".doc_nodes',
        ["space", "node_id", "node_type", "properties", "updated_at"],
        conflict_cols=["space", "node_id"],
        update_cols=["node_type", "properties", "updated_at"],
        json_columns=["properties"],
    )
    assert sql.startswith('INSERT INTO "s1".doc_nodes(')
    assert "CAST(:properties AS jsonb)" in sql
    assert ":properties" in sql and ":space" in sql
    assert "ON CONFLICT (space, node_id) DO UPDATE SET" in sql
    assert "node_type = EXCLUDED.node_type" in sql
    assert "properties = EXCLUDED.properties" in sql
    assert "updated_at = EXCLUDED.updated_at" in sql
    # conflict columns themselves must not appear in the SET clause
    assert "space = EXCLUDED.space" not in sql
    assert "node_id = EXCLUDED.node_id" not in sql


def test_postgres_upsert_single_column_conflict():
    sql = POSTGRES.upsert(
        '"s1".doc_sources',
        ["source_id", "text", "metadata", "ingested_at"],
        conflict_cols=["source_id"],
        update_cols=["text", "metadata", "ingested_at"],
        json_columns=["metadata"],
    )
    assert "ON CONFLICT (source_id) DO UPDATE SET" in sql
    assert "CAST(:metadata AS jsonb)" in sql
    assert ":text" in sql and ":ingested_at" in sql


# ---------------------------------------------------------------------------
# render_ddl — structural checks + real SQLite execution
# ---------------------------------------------------------------------------


def test_render_ddl_sqlite_statement_count_and_order():
    stmts = SQLITE.render_ddl(DOC_STORE_SCHEMA)
    # 3 tables + 2 indexes (idx_doc_nodes_updated, idx_audit_ts) = 5 statements
    assert len(stmts) == 5
    assert stmts[0].startswith("CREATE TABLE IF NOT EXISTS doc_nodes")
    assert stmts[1].startswith("CREATE INDEX IF NOT EXISTS idx_doc_nodes_updated")
    assert stmts[2].startswith("CREATE TABLE IF NOT EXISTS doc_sources")
    assert stmts[3].startswith("CREATE TABLE IF NOT EXISTS audit_log")
    assert stmts[4].startswith("CREATE INDEX IF NOT EXISTS idx_audit_ts")


def test_render_ddl_sqlite_types_and_defaults():
    stmts = SQLITE.render_ddl(DOC_STORE_SCHEMA)
    doc_nodes_ddl = stmts[0]
    assert "properties TEXT NOT NULL DEFAULT '{}'" in doc_nodes_ddl
    assert "node_type TEXT NOT NULL DEFAULT ''" in doc_nodes_ddl
    assert "updated_at TEXT NOT NULL" in doc_nodes_ddl
    assert "PRIMARY KEY (space, node_id)" in doc_nodes_ddl

    doc_sources_ddl = stmts[2]
    assert "source_id TEXT PRIMARY KEY" in doc_sources_ddl

    audit_log_ddl = stmts[3]
    assert "event_id TEXT PRIMARY KEY" in audit_log_ddl
    # subject_id is nullable: no NOT NULL, no DEFAULT
    assert "subject_id TEXT," in audit_log_ddl or "subject_id TEXT\n" in audit_log_ddl
    assert "subject_id TEXT NOT NULL" not in audit_log_ddl


def test_render_ddl_postgres_jsonb_and_timestamptz_and_schema_prefix():
    stmts = POSTGRES.render_ddl(DOC_STORE_SCHEMA, schema_name="tenant1")
    doc_nodes_ddl = stmts[0]
    assert 'CREATE TABLE IF NOT EXISTS "tenant1".doc_nodes' in doc_nodes_ddl
    assert "properties JSONB NOT NULL DEFAULT '{}'::jsonb" in doc_nodes_ddl
    assert "updated_at TIMESTAMPTZ NOT NULL" in doc_nodes_ddl

    idx_ddl = stmts[1]
    assert 'ON "tenant1".doc_nodes(updated_at)' in idx_ddl


def test_render_ddl_postgres_no_schema_name_omits_prefix():
    stmts = POSTGRES.render_ddl(DOC_STORE_SCHEMA)
    assert stmts[0].startswith("CREATE TABLE IF NOT EXISTS doc_nodes")


def test_render_ddl_sqlite_ignores_schema_name():
    with_schema = SQLITE.render_ddl(DOC_STORE_SCHEMA, schema_name="ignored")
    without_schema = SQLITE.render_ddl(DOC_STORE_SCHEMA)
    assert with_schema == without_schema


def test_render_ddl_sqlite_executes_against_real_connection():
    """Strongest check: the SQLite DDL is not just plausible-looking text —
    it actually creates the tables/indexes in a real sqlite3 database, and
    the generated insert/upsert SQL actually writes and overwrites rows."""
    conn = sqlite3.connect(":memory:")
    try:
        for stmt in SQLITE.render_ddl(DOC_STORE_SCHEMA):
            conn.execute(stmt)
        conn.commit()

        upsert_sql = SQLITE.upsert(
            "doc_nodes",
            ["space", "node_id", "node_type", "properties", "updated_at"],
            conflict_cols=["space", "node_id"],
            update_cols=["node_type", "properties", "updated_at"],
            json_columns=["properties"],
        )
        params1 = {
            "space": "s1", "node_id": "n1", "node_type": "Doc",
            "properties": '{"a": 1}', "updated_at": "2026-01-01T00:00:00+00:00",
        }
        conn.execute(upsert_sql, params1)
        conn.commit()
        row = conn.execute(
            "SELECT space, node_id, node_type, properties, updated_at FROM doc_nodes"
        ).fetchone()
        assert row == ("s1", "n1", "Doc", '{"a": 1}', "2026-01-01T00:00:00+00:00")

        # upsert-conflict overwrite: same PK, new payload wins
        params2 = {**params1, "properties": '{"a": 2}', "updated_at": "2026-01-02T00:00:00+00:00"}
        conn.execute(upsert_sql, params2)
        conn.commit()
        row2 = conn.execute(
            "SELECT properties, updated_at FROM doc_nodes WHERE space='s1' AND node_id='n1'"
        ).fetchall()
        assert len(row2) == 1  # no duplicate row
        assert row2[0] == ('{"a": 2}', "2026-01-02T00:00:00+00:00")

        insert_sql = SQLITE.insert(
            "audit_log",
            ["event_id", "event_type", "subject_id", "details", "timestamp"],
            json_columns=["details"],
        )
        conn.execute(insert_sql, {
            "event_id": "e1", "event_type": "ingest", "subject_id": "n1",
            "details": "{}", "timestamp": "2026-01-01T00:00:00+00:00",
        })
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_composite_pk_vs_single_pk_rendering():
    spec = SchemaSpec(
        tables=(
            TableSpec(
                name="composite_t",
                columns=(Column("a", "text"), Column("b", "text")),
                primary_key=("a", "b"),
            ),
            TableSpec(
                name="single_t",
                columns=(Column("id", "text"),),
                primary_key=("id",),
            ),
        ),
        indexes=(),
    )
    sqlite_stmts = SQLITE.render_ddl(spec)
    assert "PRIMARY KEY (a, b)" in sqlite_stmts[0]
    assert "id TEXT PRIMARY KEY" in sqlite_stmts[1]
    assert "PRIMARY KEY (id)" not in sqlite_stmts[1]


def test_empty_json_columns_no_cast_postgres():
    sql = POSTGRES.insert("t", ["a", "b"])
    assert "CAST" not in sql
    assert sql == "INSERT INTO t(a, b)\nVALUES (:a, :b)"


def test_dialect_is_frozen():
    with pytest.raises(Exception):
        SQLITE.name = "postgres"  # type: ignore[misc]


def test_render_ddl_json_key_index():
    """IndexSpec.json_key renders via json_index_expr instead of a static
    ``expr`` string (Stage 6b addition for graph-store's idx_nodes_pack)."""
    spec = SchemaSpec(
        tables=(
            TableSpec(
                name="t",
                columns=(Column("id", "text"), Column("properties", "json", default="{}")),
                primary_key=("id",),
            ),
        ),
        indexes=(IndexSpec("idx_t_pack", "t", json_key=("properties", "pack_id")),),
    )
    sqlite_stmts = SQLITE.render_ddl(spec)
    idx_sqlite = next(s for s in sqlite_stmts if "idx_t_pack" in s)
    assert idx_sqlite == "CREATE INDEX IF NOT EXISTS idx_t_pack ON t(json_extract(properties, '$.pack_id'))"

    pg_stmts = POSTGRES.render_ddl(spec, schema_name="s1")
    idx_pg = next(s for s in pg_stmts if "idx_t_pack" in s)
    assert idx_pg == 'CREATE INDEX IF NOT EXISTS idx_t_pack ON "s1".t((properties->>\'pack_id\'))'
