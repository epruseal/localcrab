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

    def test_on_node_write_wrapper_never_raises_on_broken_engine(self):
        """Callers (mcp/tools.py) call on_* wrappers and ignore the return
        value entirely — the outage must stay invisible to them."""
        store = SQLStore("sqlite:///:memory:")
        hooks = BillingHooks(store)
        hooks._sql._engine = _BrokenEngine()

        # Must not raise.
        hooks.on_node_write("t1", "u1", "space", "User")
        hooks.on_query("t1", "u1", "some question")


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
