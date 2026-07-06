"""
Unit tests for opencrab.execution._sql — the shared DDL-ensure / now-expr /
dialect-detection helper adopted by workflow.py, approvals.py, and
billing/hooks.py.

Runs against a real SQLStore: SQLite on a tmp file always, and PostgreSQL
additionally when OPENCRAB_PG_TEST_URL is set (skipped otherwise), matching
the pattern in test_execution_workflow.py.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from opencrab.execution._sql import dialect_for, ensure_tables, is_sqlite, now_expr
from opencrab.stores._sql_dialect import POSTGRES, SQLITE
from opencrab.stores.sql_store import SQLStore


@pytest.fixture(params=["sqlite", "pg"])
def sql_store(request, tmp_path):
    if request.param == "sqlite":
        db_path = tmp_path / "sql_helper.db"
        store = SQLStore(f"sqlite:///{db_path}")
        assert store.available
        yield store
        return

    dsn = os.environ.get("OPENCRAB_PG_TEST_URL")
    if not dsn:
        pytest.skip("OPENCRAB_PG_TEST_URL 미설정 - PG _sql 헬퍼 테스트 스킵")
    # 공유 public 대신 uuid 스키마 격리 (A2 패턴) — 병렬 세션 간 probe 테이블 경합 방지.
    import uuid

    from sqlalchemy import create_engine

    schema = f"t{uuid.uuid4().hex[:12]}_sqlh"
    admin = create_engine(dsn)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    sep = "&" if "?" in dsn else "?"
    store = SQLStore(f"{dsn}{sep}options=-csearch_path%3D{schema}")
    if not store.available:
        pytest.skip(f"PG 테스트 DB 접속 불가: {dsn!r}")
    yield store
    store._engine.dispose()
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    admin.dispose()


# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestSqlHelperNormal:
    def test_dialect_for_matches_store_backend(self, sql_store):
        expected = SQLITE if sql_store._is_sqlite else POSTGRES
        assert dialect_for(sql_store) is expected

    def test_is_sqlite_matches_store_flag(self, sql_store):
        assert is_sqlite(sql_store) == sql_store._is_sqlite

    def test_now_expr_matches_dialect_literal(self, sql_store):
        expected = "datetime('now')" if sql_store._is_sqlite else "NOW()"
        assert now_expr(sql_store) == expected

    def test_ensure_tables_picks_matching_dialect_list_and_creates_table(self, sql_store):
        ddl_sqlite = ["CREATE TABLE IF NOT EXISTS _sql_helper_probe (id INTEGER PRIMARY KEY)"]
        ddl_pg = ["CREATE TABLE IF NOT EXISTS _sql_helper_probe (id SERIAL PRIMARY KEY)"]

        ensure_tables(sql_store, ddl_sqlite, ddl_pg)

        with sql_store._engine.connect() as conn:
            conn.execute(text("SELECT * FROM _sql_helper_probe"))  # does not raise

    def test_ensure_tables_is_idempotent(self, sql_store):
        ddl_sqlite = ["CREATE TABLE IF NOT EXISTS _sql_helper_probe (id INTEGER PRIMARY KEY)"]
        ddl_pg = ["CREATE TABLE IF NOT EXISTS _sql_helper_probe (id SERIAL PRIMARY KEY)"]

        ensure_tables(sql_store, ddl_sqlite, ddl_pg)
        ensure_tables(sql_store, ddl_sqlite, ddl_pg)  # second call must not raise


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestSqlHelperError:
    def test_ensure_tables_propagates_invalid_sql(self, sql_store):
        bad_ddl = ["THIS IS NOT VALID SQL"]

        with pytest.raises((OperationalError, ProgrammingError)):
            ensure_tables(sql_store, bad_ddl, bad_ddl)


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestSqlHelperEdge:
    def test_ensure_tables_empty_list_is_a_no_op(self, sql_store):
        ensure_tables(sql_store, [], [])  # must not raise

    def test_ensure_tables_runs_multiple_statements_in_order(self, sql_store):
        ddl_sqlite = [
            "CREATE TABLE IF NOT EXISTS _sql_helper_probe (id INTEGER PRIMARY KEY)",
            "CREATE INDEX IF NOT EXISTS idx_sql_helper_probe ON _sql_helper_probe (id)",
        ]
        ddl_pg = [
            "CREATE TABLE IF NOT EXISTS _sql_helper_probe (id SERIAL PRIMARY KEY)",
            "CREATE INDEX IF NOT EXISTS idx_sql_helper_probe ON _sql_helper_probe (id)",
        ]

        ensure_tables(sql_store, ddl_sqlite, ddl_pg)

        with sql_store._engine.connect() as conn:
            conn.execute(text("SELECT * FROM _sql_helper_probe"))  # does not raise
