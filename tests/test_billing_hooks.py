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

    def test_broken_engine_failure_increments_emit_failure_count(self):
        """#105: emit_failure_count is a second, log-independent route to
        observe a lost billing event (see BillingHooks.__init__)."""
        store = SQLStore("sqlite:///:memory:")
        hooks = BillingHooks(store)
        hooks._sql._engine = _BrokenEngine()

        assert hooks.emit_failure_count == 0
        hooks.emit("query", metadata={"question": "hi"})
        assert hooks.emit_failure_count == 1
        hooks.emit("query", metadata={"question": "hi again"})
        assert hooks.emit_failure_count == 2


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
# #105: emit() must retry a REAL "database is locked" (not treat it as
# terminal on the first attempt like every other exception), and must not
# retry anything else. All three tests below use a throwaway file-backed
# scratch DB in tmp_path -- never a real project database -- and a second
# raw sqlite3 connection to genuinely hold SQLite's write lock, so the
# "database is locked" hit is the real DBAPI error, not a mock standing in
# for it.
# ---------------------------------------------------------------------------


class TestEmitLockRetry:
    @staticmethod
    def _scratch_hooks(tmp_path, *, sqlite_timeout: float) -> tuple[BillingHooks, str]:
        """A BillingHooks whose engine has a short sqlite busy-timeout, so a
        real lock raises "database is locked" quickly instead of sqlite3's
        own internal busy-handler silently absorbing the wait -- this test
        is about emit()'s OWN retry loop, layered on top of whatever DBAPI
        busy-timeout sql_store.py configures (issue #105's other fix)."""
        from sqlalchemy import create_engine

        db_path = str(tmp_path / "billing_lock_scratch.db")
        store = SQLStore(f"sqlite:///{db_path}")
        store._engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"timeout": sqlite_timeout, "check_same_thread": False},
        )
        hooks = BillingHooks(store)
        return hooks, db_path

    def test_fails_without_retry_then_succeeds_with_retry_once_lock_clears(self, tmp_path, monkeypatch):
        """Requirement 1 (issue #105 test plan): with retries disabled, a
        held write lock makes emit() fail. With retries enabled (the actual
        fix), the identical contention succeeds once the lock is released
        inside the retry window."""
        import sqlite3
        import threading
        import time

        import opencrab.billing.hooks as hooks_module

        hooks, db_path = self._scratch_hooks(tmp_path, sqlite_timeout=0.02)

        # --- (a) no retry: a held lock is a terminal failure -------------
        blocker = sqlite3.connect(db_path, timeout=1)
        blocker.execute("BEGIN IMMEDIATE")  # reserves the write lock, no commit yet
        monkeypatch.setattr(hooks_module, "_MAX_LOCK_RETRIES", 0)
        try:
            result_no_retry = hooks.emit("query", metadata={"question": "no-retry"})
        finally:
            blocker.rollback()
            blocker.close()

        assert result_no_retry["ok"] is False
        assert "locked" in result_no_retry["error"].lower()

        # --- (b) with retry: same contention, but the lock clears midway -
        monkeypatch.undo()  # restore real _MAX_LOCK_RETRIES for part (b)
        blocker2 = sqlite3.connect(db_path, timeout=1, check_same_thread=False)
        blocker2.execute("BEGIN IMMEDIATE")

        def release_after_delay() -> None:
            time.sleep(0.12)  # well under the retry loop's ~0.3s total backoff budget
            blocker2.commit()
            blocker2.close()

        releaser = threading.Thread(target=release_after_delay)
        releaser.start()
        try:
            result_with_retry = hooks.emit("query", metadata={"question": "with-retry"})
        finally:
            releaser.join()

        assert result_with_retry["ok"] is True
        assert result_with_retry["event_id"].startswith("evt_")

    def test_non_lock_exception_returns_immediately_no_backoff_loop(self, tmp_path, monkeypatch):
        """Requirement 2: an exception that retrying can never fix (here: a
        missing table, standing in for e.g. a schema error) must return on
        the first attempt -- time.sleep must never be called."""
        import time

        hooks, _ = self._scratch_hooks(tmp_path, sqlite_timeout=0.02)
        sleep_calls: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

        # Drop the table emit() writes to -> "no such table" is NOT a lock
        # error, so _is_lock_error() must reject it and emit() must not loop.
        from sqlalchemy import text

        with hooks._sql._engine.begin() as conn:
            conn.execute(text("DROP TABLE billing_events"))

        result = hooks.emit("query", metadata={"question": "schema-broken"})

        assert result["ok"] is False
        assert "locked" not in result["error"].lower()
        assert sleep_calls == []  # proves no backoff loop was entered


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
