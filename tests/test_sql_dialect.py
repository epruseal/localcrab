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
    json_valid_expr,
    reset_json5_valid_cache_for_testing,
    sqlite_json5_valid_supported,
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

        def _is_malformed_json_error(self, exc: Exception) -> bool:
            # issue #415: PG's jsonb rejects malformed JSON at write time --
            # structurally unreachable on PG, mirrors pg_graph_store.py.
            return False

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
    # 3 tables + 3 indexes (idx_doc_nodes_updated, idx_doc_nodes_updated_tiebreak
    # — #63 tie-break composite index, codex P2 — and idx_audit_ts) = 6 statements
    assert len(stmts) == 6
    assert stmts[0].startswith("CREATE TABLE IF NOT EXISTS doc_nodes")
    assert stmts[1].startswith("CREATE INDEX IF NOT EXISTS idx_doc_nodes_updated")
    assert stmts[2].startswith("CREATE INDEX IF NOT EXISTS idx_doc_nodes_updated_tiebreak")
    assert stmts[3].startswith("CREATE TABLE IF NOT EXISTS doc_sources")
    assert stmts[4].startswith("CREATE TABLE IF NOT EXISTS audit_log")
    assert stmts[5].startswith("CREATE INDEX IF NOT EXISTS idx_audit_ts")


def test_render_ddl_sqlite_types_and_defaults():
    stmts = SQLITE.render_ddl(DOC_STORE_SCHEMA)
    doc_nodes_ddl = stmts[0]
    assert "properties TEXT NOT NULL DEFAULT '{}'" in doc_nodes_ddl
    assert "node_type TEXT NOT NULL DEFAULT ''" in doc_nodes_ddl
    assert "updated_at TEXT NOT NULL" in doc_nodes_ddl
    assert "PRIMARY KEY (space, node_id)" in doc_nodes_ddl

    doc_sources_ddl = stmts[3]
    assert "source_id TEXT PRIMARY KEY" in doc_sources_ddl

    audit_log_ddl = stmts[4]
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


# ---------------------------------------------------------------------------
# issue #415: json_valid_expr / json_get_safe / json_truthy_text malformed-row
# guards, and the JSON5 (json_valid(x,3)) feature-detection cache.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_json5_cache():
    """The JSON5 support flag is a process-wide cache (SQLite capability is a
    property of the linked libsqlite3, not of any one connection). Reset it
    around every test in this module so tests that force a simulated
    on/off state never leak into an unrelated test's result."""
    reset_json5_valid_cache_for_testing()
    yield
    reset_json5_valid_cache_for_testing()


def _malformed_properties_db() -> sqlite3.Connection:
    """A minimal in-memory table seeded with one well-formed row and one
    syntactically malformed JSON row -- the exact issue #415 shape."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id TEXT, properties TEXT)")
    conn.execute("INSERT INTO t VALUES ('good', '{\"pack_id\": \"P\"}')")
    conn.execute("INSERT INTO t VALUES ('bad', 'not valid json {')")
    conn.commit()
    return conn


def test_sqlite_json5_valid_supported_returns_a_real_bool_and_caches():
    """RED/GREEN is meaningless here (no prior behavior to break) -- this
    pins the actual detection result against a real in-memory connection and
    confirms the process-wide cache does not re-probe on a second call.
    ``sqlite3.Connection`` is a C extension type whose methods cannot be
    monkeypatched on the instance, so a thin wrapper counts calls instead."""

    class _CountingConn:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real
            self.calls: list[str] = []

        def execute(self, sql, *a, **kw):
            self.calls.append(sql)
            return self._real.execute(sql, *a, **kw)

    conn = _CountingConn(sqlite3.connect(":memory:"))
    first = sqlite_json5_valid_supported(conn)
    assert isinstance(first, bool)
    second = sqlite_json5_valid_supported(conn)
    assert second == first
    assert len(conn.calls) == 1, f"probe must run at most once per process: {conn.calls}"


def test_json_valid_expr_uses_json_valid_3_when_supported(monkeypatch):
    """Simulated support branch (rev.4, 4-0): forces the cache to report
    support without depending on this machine's actual SQLite build."""
    import opencrab.stores._sql_dialect as dialect_mod

    monkeypatch.setattr(dialect_mod, "_JSON5_VALID_SUPPORTED", True)
    assert json_valid_expr("properties") == "json_valid(properties, 3)"


def test_json_valid_expr_falls_back_to_json_valid_1_when_unsupported(monkeypatch):
    """Simulated unsupported branch: forces the probe itself to raise
    OperationalError (as it would on SQLite < 3.45), independent of whether
    this CI's actual SQLite build supports JSON5 or not."""
    import opencrab.stores._sql_dialect as dialect_mod

    reset_json5_valid_cache_for_testing()

    class _RaisingConn:
        def execute(self, sql):
            raise sqlite3.OperationalError("unrecognized token")

    monkeypatch.setattr(dialect_mod, "_JSON5_VALID_SUPPORTED", None)
    assert sqlite_json5_valid_supported(_RaisingConn()) is False
    assert json_valid_expr("properties") == "json_valid(properties)"


def test_json_get_safe_postgres_is_identical_to_json_get():
    assert POSTGRES.json_get_safe("properties", "pack_id") == POSTGRES.json_get(
        "properties", "pack_id"
    )


def test_json_get_safe_real_connection_null_for_malformed_row_not_exception():
    """RED: the bare json_extract this replaces raises OperationalError for
    the ENTIRE query the instant it touches the malformed row (issue #415's
    core symptom) -- reproduced first so the GREEN assertion below is
    provably a fix, not a no-op."""
    conn = _malformed_properties_db()
    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        conn.execute(f"SELECT id, {SQLITE.json_get('properties', 'pack_id')} FROM t").fetchall()

    # GREEN: json_get_safe survives the same query and NULLs out only the
    # malformed row.
    expr = SQLITE.json_get_safe("properties", "pack_id")
    rows = dict(conn.execute(f"SELECT id, {expr} FROM t").fetchall())
    assert rows == {"good": "P", "bad": None}
    conn.close()


def test_json_truthy_text_guard_excludes_malformed_row_real_connection():
    """RED/GREEN pair for the ``json_truthy_text`` SQLite branch's new outer
    ``json_valid`` guard (4-A)."""
    conn = _malformed_properties_db()
    inner_only = (
        "CASE json_type(properties, '$.pack_id')"
        " WHEN 'text' THEN NULLIF(json_extract(properties, '$.pack_id'), '')"
        " ELSE NULL END"
    )
    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        conn.execute(f"SELECT id, {inner_only} FROM t").fetchall()

    guarded = SQLITE.json_truthy_text("properties", "pack_id")
    rows = dict(conn.execute(f"SELECT id, {guarded} FROM t").fetchall())
    assert rows == {"good": "P", "bad": None}
    conn.close()


def test_json_truthy_text_json5_style_row_is_not_excluded_when_simulated_on(monkeypatch):
    """JSON5 대조군(리드 재정 — 함수별 행동 시험 요구): `json_truthy_text`가
    JSON5 지원 시뮬레이션 하에서 트레일링 콤마 행을 진짜 손상과 혼동하지
    않는지 이 함수 자체의 렌더링으로 직접 확인한다. `json_valid_expr` 단위
    시험만으로는 이 함수가 나중에 `json_valid_expr(col)` 대신 `json_valid(col)`
    을 직접 박는 식으로 바뀌어도 잡지 못한다 — 이 시험은 그 회귀를 이 함수의
    실제 SQL 렌더링으로 잡는다."""
    if not sqlite_json5_valid_supported():
        pytest.skip("this SQLite build has no JSON5 support to simulate positively")

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id TEXT, properties TEXT)")
    conn.execute("INSERT INTO t VALUES ('json5', '{\"pack_id\": \"P\",}')")  # trailing comma
    conn.execute("INSERT INTO t VALUES ('good', '{\"pack_id\": \"Q\"}')")
    conn.execute("INSERT INTO t VALUES ('bad', 'not valid json {')")
    conn.commit()

    import opencrab.stores._sql_dialect as dialect_mod
    monkeypatch.setattr(dialect_mod, "_JSON5_VALID_SUPPORTED", True)
    expr = SQLITE.json_truthy_text("properties", "pack_id")
    rows = dict(conn.execute(f"SELECT id, {expr} FROM t").fetchall())
    assert rows["json5"] == "P", "JSON5 행은 정상 행처럼 값이 나와야 한다"
    assert rows["good"] == "Q"
    assert rows["bad"] is None, "진짜 손상 행은 여전히 배제돼야 한다"
    conn.close()


def test_json_truthy_text_json5_style_row_is_a_known_limitation_when_simulated_off(monkeypatch):
    """알려진 한계 대조군(`json_truthy_text` 쪽): 1-인자 엄격 폴백에서는
    JSON5 행도 진짜 손상과 구분되지 않고 배제된다 -- 4-0 문서화 내용과
    일치하는지 이 함수 자체로 재확인한다."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id TEXT, properties TEXT)")
    conn.execute("INSERT INTO t VALUES ('json5', '{\"pack_id\": \"P\",}')")  # trailing comma
    conn.execute("INSERT INTO t VALUES ('good', '{\"pack_id\": \"Q\"}')")
    conn.commit()

    import opencrab.stores._sql_dialect as dialect_mod
    monkeypatch.setattr(dialect_mod, "_JSON5_VALID_SUPPORTED", False)
    expr = SQLITE.json_truthy_text("properties", "pack_id")
    rows = dict(conn.execute(f"SELECT id, {expr} FROM t").fetchall())
    assert rows["good"] == "Q"
    assert rows["json5"] is None, "알려진 한계: 폴백에서는 JSON5 행도 배제된다"
    conn.close()


def test_genuinely_malformed_row_excluded_when_json5_support_is_simulated_off(monkeypatch):
    """No-regression check (rev.4, 4-0 introduction): a truly broken row must
    be NULLed out by json_get_safe under the 1-arg strict fallback, which
    every SQLite build supports -- always exercised, no skip needed."""
    import opencrab.stores._sql_dialect as dialect_mod

    conn = _malformed_properties_db()
    monkeypatch.setattr(dialect_mod, "_JSON5_VALID_SUPPORTED", False)
    expr = SQLITE.json_get_safe("properties", "pack_id")
    rows = dict(conn.execute(f"SELECT id, {expr} FROM t").fetchall())
    assert rows["bad"] is None, "genuinely malformed row leaked through (supported=False)"
    assert rows["good"] == "P"
    conn.close()


def test_genuinely_malformed_row_excluded_when_json5_support_is_simulated_on(monkeypatch):
    """No-regression check (rev.4, 4-0 introduction), JSON5-permissive branch:
    only meaningful when this build's json_valid genuinely accepts the 2-arg
    form -- forcing True on a build that lacks it renders SQL this build
    cannot execute at all (an arity mismatch is a capability gap, not a
    behavior difference under test), so that case is skipped rather than
    forced. A prior version of this test forced both branches unconditionally
    in one function and would raise OperationalError (not an assertion
    failure) on any SQLite build lacking the 2-arg json_valid overload."""
    import opencrab.stores._sql_dialect as dialect_mod

    if not sqlite_json5_valid_supported():
        pytest.skip("this SQLite build has no JSON5 2-arg json_valid to simulate positively")

    conn = _malformed_properties_db()
    monkeypatch.setattr(dialect_mod, "_JSON5_VALID_SUPPORTED", True)
    expr = SQLITE.json_get_safe("properties", "pack_id")
    rows = dict(conn.execute(f"SELECT id, {expr} FROM t").fetchall())
    assert rows["bad"] is None, "genuinely malformed row leaked through (supported=True)"
    assert rows["good"] == "P"
    conn.close()


def test_json5_style_row_is_not_excluded_when_json5_support_is_simulated_on(monkeypatch):
    """Positive control group (rev.4, 4-0/1): a JSON5-flavored-but-parseable
    row (trailing comma) must NOT be treated as malformed when json_valid's
    2-arg JSON5 mode is in effect -- distinguishing "SQLite's json_extract
    can parse it" (true) from "strict RFC8259 json_valid(x) says invalid"
    (also true, and would be a false positive if used alone)."""
    import opencrab.stores._sql_dialect as dialect_mod

    # This machine's actual json_valid(x, 3) support decides whether the
    # trailing-comma probe row below is even parseable by json_extract in
    # the first place (SQLite's own JSON5 lenience, independent of our
    # json_valid_expr helper) -- skip the positive branch if this build
    # can't parse JSON5 text at all, since then there is nothing to admit.
    if not sqlite_json5_valid_supported():
        pytest.skip("this SQLite build has no JSON5 support to simulate positively")

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id TEXT, properties TEXT)")
    conn.execute("INSERT INTO t VALUES ('json5', '{\"pack_id\": \"P\",}')")  # trailing comma
    conn.execute("INSERT INTO t VALUES ('good', '{\"pack_id\": \"Q\"}')")
    conn.execute("INSERT INTO t VALUES ('bad', 'not valid json {')")
    conn.commit()

    monkeypatch.setattr(dialect_mod, "_JSON5_VALID_SUPPORTED", True)
    expr = SQLITE.json_get_safe("properties", "pack_id")
    rows = dict(conn.execute(f"SELECT id, {expr} FROM t").fetchall())
    assert rows["json5"] == "P", "JSON5-style row must be admitted like a normal row, not excluded"
    assert rows["good"] == "Q"
    assert rows["bad"] is None, "a genuinely malformed row must still be excluded"
    conn.close()


def test_json5_style_row_is_a_known_limitation_when_json5_support_is_simulated_off(monkeypatch):
    """Documented fallback limitation (4-0): with the 1-arg strict
    ``json_valid(x)`` fallback, a JSON5-style row is indistinguishable from
    a genuinely malformed one and gets excluded too -- confirmed here as an
    intentional, documented gap, not silently discovered later."""
    import opencrab.stores._sql_dialect as dialect_mod

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id TEXT, properties TEXT)")
    conn.execute("INSERT INTO t VALUES ('json5', '{\"pack_id\": \"P\",}')")  # trailing comma
    conn.execute("INSERT INTO t VALUES ('good', '{\"pack_id\": \"Q\"}')")
    conn.commit()

    monkeypatch.setattr(dialect_mod, "_JSON5_VALID_SUPPORTED", False)
    expr = SQLITE.json_get_safe("properties", "pack_id")
    rows = dict(conn.execute(f"SELECT id, {expr} FROM t").fetchall())
    assert rows["good"] == "Q"
    assert rows["json5"] is None, (
        "known limitation: strict-mode fallback cannot tell JSON5 apart from"
        " genuinely malformed JSON -- if this now passes, the fallback"
        " behavior changed and 4-0's documented gap needs updating too"
    )
    conn.close()


def test_json_str_in_raw_extraction_survives_malformed_row_in_select_list_position():
    """issue #415 design.md §8 row 4 (rev.4 정정, 리드 재정 3번): codex 2라운드가
    재현한 대로, ``_json_str_in``의 ``raw`` 값 추출은 WHERE-불리언 위치뿐 아니라
    SELECT 리스트 위치에서도 안전해야 한다. RED: 가드 없는 바닥 추출을 SELECT
    리스트에 두면 손상 행에서 그대로 죽는다. GREEN 판정은 정확한 반환값이 아니라
    **그 행이 결과 집합에서 빠지는지**로 한다 -- 실측(SQLite 3진 논리)상 손상
    행은 ``type_check=0``, ``raw=NULL``이라 ``0 AND NULL = 0``(falsy지만 NULL은
    아니다)이기 때문이다."""
    from opencrab.stores._sql_doc_base import _json_str_in

    conn = _malformed_properties_db()

    # RED: bare unguarded extraction (pre-#415 shape) in a SELECT-list
    # position -- codex 2라운드가 실측으로 재현한 정확한 실패 위치.
    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        conn.execute(
            f"SELECT id, {SQLITE.json_get('properties', 'pack_id')} FROM t"
        ).fetchall()

    # GREEN (SELECT-list position): _json_str_in's own raw extraction
    # (json_get_safe internally) survives the identical position/data.
    safe_extract = SQLITE.json_get_safe("properties", "pack_id")
    rows = dict(conn.execute(f"SELECT id, {safe_extract} FROM t").fetchall())
    assert rows == {"good": "P", "bad": None}

    # GREEN (WHERE-boolean position, rev.4 GREEN criterion): the malformed
    # row is excluded from the result set -- not asserted via its combined
    # expression's truth value, but via row presence/absence.
    frag, transform = _json_str_in(SQLITE, "properties", "pack_id", ":packs")
    where_rows = conn.execute(
        f"SELECT id FROM t WHERE {frag}", {"packs": transform(["P"])}
    ).fetchall()
    assert {r[0] for r in where_rows} == {"good"}

    # GREEN (SELECT-list position, this test's actual claim -- codex 3라운드
    # P2 지적 수정): unlike the block above, which only proves
    # the underlying building block json_get_safe survives there, this
    # executes _json_str_in's OWN returned fragment in a SELECT list.
    # A mutation reverting _json_str_in's internal json_get_safe back to
    # bare json_get previously kept this test green because nothing here
    # ran _json_str_in's real frag outside a WHERE clause. Checked against
    # both selectivity values so a mutation that flips the membership test
    # itself (not just the malformed-row guard) is also caught.
    select_rows_match = dict(
        conn.execute(
            f"SELECT id, {frag} FROM t", {"packs": transform(["P"])}
        ).fetchall()
    )
    assert select_rows_match == {"good": 1, "bad": 0}

    select_rows_no_match = dict(
        conn.execute(
            f"SELECT id, {frag} FROM t", {"packs": transform(["Q"])}
        ).fetchall()
    )
    assert select_rows_no_match == {"good": 0, "bad": 0}

    conn.close()
