"""
Contract tests for opencrab.execution.workflow.WorkflowEngine.

Runs against a real SQLStore: SQLite on a tmp file always, and PostgreSQL
additionally when OPENCRAB_PG_TEST_URL is set (skipped otherwise).

Scope note: transition-rule enforcement (which status -> status moves are
legal) is explicitly out of scope for this test file -- it is a later
stage's contract change. These tests cover persistence side effects,
retrieval ordering, and input validation that already exists in the code.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from opencrab.execution.workflow import WorkflowEngine
from opencrab.stores.sql_store import SQLStore


def _pg_scoped_store(dsn: str, suffix: str):
    """Build a SQLStore whose (unqualified) DDL lands in a fresh, uuid-named
    PG schema rather than the shared `public` schema -- prevents concurrent
    pytest sessions from tripping over each other's CREATE/DROP TABLE.

    Mechanism: SQLStore/WorkflowEngine issue unqualified `CREATE TABLE` /
    `SELECT ... FROM workflow_runs` etc., so pointing every pooled
    connection's `search_path` at a schema that exists (and only that
    schema) makes all of that DDL/DML land there without touching
    WorkflowEngine or SQLStore. psycopg2/libpq honor a `-c search_path=...`
    passed via the `options` connect kwarg, and SQLAlchemy forwards
    unrecognised URL query params straight through to psycopg2.connect().
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


@pytest.fixture(params=["sqlite", "pg"])
def sql_store(request, tmp_path):
    if request.param == "sqlite":
        db_path = tmp_path / "workflow.db"
        store = SQLStore(f"sqlite:///{db_path}")
        assert store.available
        yield store
        return

    dsn = os.environ.get("OPENCRAB_PG_TEST_URL")
    if not dsn:
        pytest.skip("OPENCRAB_PG_TEST_URL 미설정 - PG 워크플로우 테스트 스킵")
    store, schema, admin_engine = _pg_scoped_store(dsn, "wf")
    if not store.available:
        _drop_pg_schema(admin_engine, schema)
        pytest.skip(f"PG 테스트 DB 접속 불가: {dsn!r}")
    yield store
    _drop_pg_schema(admin_engine, schema)


@pytest.fixture
def engine(sql_store):
    return WorkflowEngine(sql_store)


# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestWorkflowNormal:
    def test_create_run_persists_row_and_seed_log_entry(self, engine):
        result = engine.create_run("add_node", {"space": "subject", "id": "u1"}, subject_id="alice")

        assert result["status"] == "pending"
        assert result["run_id"].startswith("run_")
        assert result["receipt_id"].startswith("rcpt_")

        row = engine.get_run(result["run_id"])
        assert row is not None
        assert row["action_type"] == "add_node"
        assert row["status"] == "pending"
        assert row["subject_id"] == "alice"
        assert json.loads(row["payload_json"]) == {"space": "subject", "id": "u1"}
        assert row["receipt_id"] == result["receipt_id"]

        log = engine.get_log(result["run_id"])
        assert len(log) == 1
        assert log[0]["action_type"] == "add_node"
        assert log[0]["actor"] == "alice"
        assert json.loads(log[0]["input_json"]) == {"space": "subject", "id": "u1"}

    def test_get_run_returns_created_run(self, engine):
        created = engine.create_run("restrict_access", {"resource_id": "r1"})
        fetched = engine.get_run(created["run_id"])
        assert fetched["run_id"] == created["run_id"]
        assert fetched["action_type"] == "restrict_access"

    def test_advance_updates_row_and_appends_log(self, engine):
        created = engine.create_run("restrict_access", {"resource_id": "r1"})
        run_id = created["run_id"]

        result = engine.advance(run_id, "running", output={"note": "started"}, actor="bob")

        assert result["run_id"] == run_id
        assert result["status"] == "running"
        assert result["receipt_id"] != created["receipt_id"]

        row = engine.get_run(run_id)
        assert row["status"] == "running"

        log = engine.get_log(run_id)
        assert len(log) == 2
        assert log[1]["actor"] == "bob"
        assert json.loads(log[1]["output_json"]) == {"note": "started"}
        # action_type is looked up via subquery -- must match the run's type
        assert log[1]["action_type"] == "restrict_access"

    def test_get_log_returns_entries_in_chronological_order(self, engine):
        created = engine.create_run("add_edge", {})
        run_id = created["run_id"]
        engine.advance(run_id, "running")
        engine.advance(run_id, "completed")

        log = engine.get_log(run_id)
        assert len(log) == 3
        ids = [entry["id"] for entry in log]
        assert ids == sorted(ids)

    @pytest.mark.parametrize(
        "chain",
        [
            ["running", "completed"],
            ["running", "failed"],
            ["approved", "running"],
            ["approved", "rejected"],
            ["rejected"],
        ],
    )
    def test_advance_follows_legal_chain_from_pending(self, engine, chain):
        """Transition-legality itself is covered exhaustively by
        test_workflow_transitions.py; this only checks that a fresh
        (pending) run can walk each legal chain end to end."""
        created = engine.create_run("promote_claim", {})
        run_id = created["run_id"]
        for status in chain:
            result = engine.advance(run_id, status)
            assert result["status"] == status
            assert engine.get_run(run_id)["status"] == status


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestWorkflowError:
    def test_advance_rejects_status_outside_valid_set(self, engine):
        created = engine.create_run("add_node", {})
        with pytest.raises(ValueError, match="Invalid status"):
            engine.advance(created["run_id"], "not_a_real_status")

        # rejected before touching the row -- state must be unchanged
        assert engine.get_run(created["run_id"])["status"] == "pending"

    def test_advance_unknown_run_id_raises(self, engine):
        with pytest.raises(ValueError, match="not found"):
            engine.advance("run_does_not_exist", "running")

    def test_advance_unknown_run_id_does_not_append_log(self, engine):
        with pytest.raises(ValueError):
            engine.advance("run_does_not_exist", "running")
        assert engine.get_log("run_does_not_exist") == []


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestWorkflowEdge:
    def test_get_run_nonexistent_returns_none(self, engine):
        assert engine.get_run("run_nope") is None

    def test_get_log_nonexistent_returns_empty_list(self, engine):
        assert engine.get_log("run_nope") == []

    def test_list_runs_empty_store_returns_empty_list(self, engine):
        assert engine.list_runs() == []
