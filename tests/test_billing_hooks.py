"""
Contract tests for opencrab.billing.hooks.BillingHooks.

Focus: emit() must never raise (billing is fire-and-forget from the
caller's perspective — an outage here must not break ontology writes),
but its return value must truthfully reflect whether the event was
persisted. Before this fix, emit() swallowed INSERT failures and still
returned a success-shaped dict (including a phantom event_id), so callers
had no way to detect a persistence failure.
"""

from __future__ import annotations

import os
import uuid

import pytest

from opencrab.billing.hooks import BillingHooks
from opencrab.stores.sql_store import SQLStore

PG_URL = os.environ.get("OPENCRAB_PG_TEST_URL")


def _pg_scoped_store(dsn: str, suffix: str):
    """Build a SQLStore whose (unqualified) DDL lands in a fresh, uuid-named
    PG schema rather than the shared `public` schema -- prevents concurrent
    pytest sessions from tripping over each other's CREATE/DROP TABLE.

    Mechanism: pointing every pooled connection's `search_path` at a schema
    that exists (and only that schema) makes BillingHooks/SQLStore's
    unqualified DDL/DML land there without touching production code.
    psycopg2/libpq honor a `-c search_path=...` passed via the `options`
    connect kwarg, and SQLAlchemy forwards unrecognised URL query params
    straight through to psycopg2.connect().
    """
    from sqlalchemy import create_engine, text

    schema = f"t{uuid.uuid4().hex[:12]}_{suffix}"
    admin_engine = create_engine(dsn)
    with admin_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    sep = "&" if "?" in dsn else "?"
    scoped_dsn = f"{dsn}{sep}options=-csearch_path%3D{schema}"
    store = SQLStore(scoped_dsn)
    return store, schema, admin_engine


def _drop_pg_schema(admin_engine, schema: str) -> None:
    from sqlalchemy import text

    with admin_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    admin_engine.dispose()


class _BrokenEngine:
    """Stand-in for a closed/broken SQLAlchemy engine."""

    def begin(self):
        raise RuntimeError("engine is closed")

    def connect(self):
        raise RuntimeError("engine is closed")


class _UnstringableMeta:
    """Raises on __str__ so json.dumps(default=str) fails mid-serialization."""

    def __str__(self):
        raise ValueError("cannot stringify")


# ---------------------------------------------------------------------------
# Normal: success path (sqlite), row actually persisted
# ---------------------------------------------------------------------------


class TestEmitSuccess:
    def test_emit_success_returns_ok_true_and_row_is_actually_inserted(self):
        from sqlalchemy import text

        store = SQLStore("sqlite:///:memory:")
        hooks = BillingHooks(store)

        result = hooks.emit("node_write", tenant_id="t1", metadata={"space": "s", "node_type": "User"})

        assert result["ok"] is True
        assert result["event_id"].startswith("evt_")
        assert result["event_type"] == "node_write"

        # Verify independently of get_usage/list_events — direct SELECT.
        with store._engine.connect() as conn:
            row = conn.execute(
                text("SELECT event_id, tenant_id, event_type FROM billing_events WHERE event_id = :eid"),
                {"eid": result["event_id"]},
            ).fetchone()
        assert row is not None
        assert row[0] == result["event_id"]
        assert row[1] == "t1"
        assert row[2] == "node_write"


# ---------------------------------------------------------------------------
# Error: emit() must not raise, and must not lie about persistence
# ---------------------------------------------------------------------------


class TestEmitFailure:
    def test_emit_failure_returns_ok_false_no_exception_no_phantom_event_id(self):
        store = SQLStore("sqlite:///:memory:")
        hooks = BillingHooks(store)
        hooks._sql._engine = _BrokenEngine()  # simulate closed/broken engine

        result = hooks.emit("query", metadata={"question": "hi"})

        assert result["ok"] is False
        assert "error" in result
        assert "event_id" not in result

    def test_on_node_write_and_on_query_never_raise_and_report_ok_false(self):
        """#105: on_node_write/on_query used to return None, which made it
        structurally impossible for ANY caller to notice a failed persist —
        they now return emit()'s dict like the other three wrappers below,
        so this pins that they still never raise, and that ok=False actually
        comes through instead of being swallowed by the wrapper."""
        store = SQLStore("sqlite:///:memory:")
        hooks = BillingHooks(store)
        hooks._sql._engine = _BrokenEngine()

        assert hooks.on_node_write("t1", "u1", "space", "User")["ok"] is False
        assert hooks.on_query("t1", "u1", "some question")["ok"] is False

    def test_66_wired_wrappers_never_raise_and_report_ok_false(self):
        """The three wrappers newly wired for #66 return emit()'s dict so
        callers CAN notice a failure — this pins that they still never
        raise, and that ok=False actually comes through instead of being
        swallowed by the wrapper."""
        store = SQLStore("sqlite:///:memory:")
        hooks = BillingHooks(store)
        hooks._sql._engine = _BrokenEngine()

        assert hooks.on_edge_write("t1", "u1", "owns")["ok"] is False
        assert hooks.on_ingest("t1", "u1", "src1")["ok"] is False
        assert hooks.on_harness_apply("t1", "u1", "pkg1", 3)["ok"] is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEmitEdgeCases:
    def test_metadata_serialization_falls_back_to_str_when_json_dumps_fails(self):
        store = SQLStore("sqlite:///:memory:")
        hooks = BillingHooks(store)

        result = hooks.emit("query", metadata={"weird": _UnstringableMeta()})

        # default=str raising mid-dumps is caught; falls back to str(metadata)
        # (dict.__str__ uses repr() on values, which does not call our
        # raising __str__), so the insert still succeeds.
        assert result["ok"] is True

    @pytest.mark.skipif(not PG_URL, reason="OPENCRAB_PG_TEST_URL not set — PG billing test skipped")
    def test_emit_pg_jsonb_cast_path(self):
        tenant = f"pg-billing-{uuid.uuid4().hex[:8]}"
        store, schema, admin_engine = _pg_scoped_store(PG_URL, "bill")
        if not store.available:
            _drop_pg_schema(admin_engine, schema)
            pytest.skip(f"PG 테스트 DB 접속 불가: {PG_URL!r}")
        try:
            hooks = BillingHooks(store)

            result = hooks.emit("ingest", tenant_id=tenant, metadata={"source_id": "doc-1", "n": 3})

            assert result["ok"] is True
            events = hooks.list_events(tenant_id=tenant, limit=5)
            assert any(e["event_id"] == result["event_id"] for e in events)
        finally:
            _drop_pg_schema(admin_engine, schema)

    def test_get_usage_aggregates_multiple_events_by_type(self):
        store = SQLStore("sqlite:///:memory:")
        hooks = BillingHooks(store)
        tenant = "usage-tenant"

        hooks.emit("node_write", tenant_id=tenant)
        hooks.emit("node_write", tenant_id=tenant)
        hooks.emit("edge_write", tenant_id=tenant)

        usage = hooks.get_usage(tenant_id=tenant)

        assert usage["total"] == 3
        assert usage["by_type"] == {"node_write": 2, "edge_write": 1}

    def test_list_events_respects_limit(self):
        store = SQLStore("sqlite:///:memory:")
        hooks = BillingHooks(store)
        tenant = "limit-tenant"

        for _ in range(5):
            hooks.emit("query", tenant_id=tenant)

        events = hooks.list_events(tenant_id=tenant, limit=2)

        assert len(events) == 2


# ---------------------------------------------------------------------------
# #105: the actual reason this fix exists. An earlier version of this fix
# retried the billing insert with backoff when it shared opencrab.db's file
# with write.lock'd writers -- that only shrank the failure window and slept
# synchronously in the request path (opencrab/mcp/http_app.py's async
# handler would have blocked on it). The real fix is that billing_events no
# longer shares that file at all (see make_billing_sql_store), so there is
# no contention to retry around in the first place. Both tests below use
# only a throwaway tmp_path scratch DB -- never a live database -- and a
# second raw sqlite3 connection genuinely holding SQLite's write lock (not
# mocked), held open for the whole call with no release/timing dependency.
# ---------------------------------------------------------------------------


class TestBillingNotBlockedByWriteLockHolder:
    @staticmethod
    def _hold_write_lock(db_path):
        """Open a second, real sqlite3 connection and reserve the write
        lock the way a long write.lock-held handler (e.g. a bulk
        pack_ingest via ontology_add_node, writes=True) would while it
        works -- BEGIN IMMEDIATE reserves the lock immediately, and the
        INSERT plus the deliberate absence of a commit keeps it held for as
        long as the caller wants (here: the whole test body)."""
        import sqlite3

        conn = sqlite3.connect(str(db_path), timeout=1)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO ontology_nodes (space, node_type, node_id) VALUES ('s', 't', 'n1')")
        return conn

    def test_billing_insert_succeeds_immediately_while_opencrab_db_write_lock_is_held(self, tmp_path):
        """The fixed shape: billing lives in its own file (billing.db), so a
        writer holding opencrab.db's write lock for the ENTIRE test (never
        released) does not block, slow down, or fail the billing insert."""
        import time

        from opencrab.config import Settings
        from opencrab.stores.factory import make_billing_sql_store, make_sql_store

        settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(tmp_path))
        sql = make_sql_store(settings)  # creates opencrab.db + its tables
        billing_store, migrate_from = make_billing_sql_store(settings, sql)
        hooks = BillingHooks(billing_store, migrate_from=migrate_from)
        assert billing_store is not sql  # sanity: really is a separate file

        blocker = self._hold_write_lock(tmp_path / "opencrab.db")
        try:
            start = time.monotonic()
            result = hooks.emit("query", metadata={"question": "contended"})
            elapsed = time.monotonic() - start
        finally:
            blocker.rollback()
            blocker.close()

        assert result["ok"] is True
        assert elapsed < 1.0  # no lock wait, no retry backoff -- effectively instant

    def test_negative_control_the_old_shared_file_shape_really_did_block(self, tmp_path):
        """Proves the contention this fix removes was real: BillingHooks
        wired the pre-#105 way (sharing `sql`'s own file/store, exactly what
        make_billing_sql_store now avoids) DOES fail under the identical
        held lock. Not exercised by the fixed path above -- this is what it
        would look like without this fix."""
        from sqlalchemy import create_engine

        from opencrab.config import Settings
        from opencrab.stores.factory import make_sql_store

        settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(tmp_path))
        sql = make_sql_store(settings)
        # Short busy-timeout so this negative control fails fast instead of
        # waiting out the real 5s DBAPI default -- the failure mode, not its
        # timing, is what's under test.
        sql._engine = create_engine(
            f"sqlite:///{tmp_path / 'opencrab.db'}",
            connect_args={"timeout": 0.05, "check_same_thread": False},
        )
        hooks_sharing_file = BillingHooks(sql)  # the pre-#105 wiring: no separation

        blocker = self._hold_write_lock(tmp_path / "opencrab.db")
        try:
            result = hooks_sharing_file.emit("query", metadata={"question": "contended"})
        finally:
            blocker.rollback()
            blocker.close()

        assert result["ok"] is False
        assert "locked" in result["error"].lower()


# ---------------------------------------------------------------------------
# #66: on_edge_write / on_ingest / on_harness_apply had zero callers
# repo-wide (on_promotion had zero callers AND no tool to call it from —
# promotion_promote was already deleted as dead MCP-tool code, see
# tests/test_mcp.py::test_tools_list_not_empty). This pins the resolution:
# on_promotion is gone, the other three are documented and wired at their
# respective MCP handlers (graph.py/pack.py/harness.py).
# ---------------------------------------------------------------------------


class TestHookSurfaceMatchesDocs:
    def test_on_promotion_hook_was_deleted_not_wired(self):
        """No tool ever called it, and none exists to call it from — deleting
        the hook (not wiring it) is the correct resolution, see hooks.py's
        module docstring."""
        assert not hasattr(BillingHooks, "on_promotion")

    def test_documented_event_types_match_the_on_star_wrapper_methods(self):
        """The module docstring's "Billable event types" list must stay in
        sync with the actual on_* wrapper methods — this is exactly the kind
        of drift that let 4 of 6 documented-but-unwired hooks go unnoticed."""
        import inspect

        import opencrab.billing.hooks as hooks_module

        on_star_methods = {
            name[len("on_"):]
            for name, fn in vars(BillingHooks).items()
            if name.startswith("on_") and inspect.isfunction(fn)
        }
        assert on_star_methods == {"node_write", "edge_write", "query", "ingest", "harness_apply"}

        docstring = hooks_module.__doc__ or ""
        table = docstring.split("Billable event types:")[1].split("Each event stores:")[0]
        table_lines = [ln.strip() for ln in table.strip().splitlines()]
        documented_types = {ln.split()[0] for ln in table_lines if ln}
        assert documented_types == on_star_methods, (
            f"docstring table {documented_types} != on_* methods {on_star_methods}"
        )
