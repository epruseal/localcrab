"""
Contract tests for opencrab.execution.approvals.ApprovalEngine.

Runs against a real SQLStore: SQLite on a tmp file always, and PostgreSQL
additionally when OPENCRAB_PG_TEST_URL is set (skipped otherwise).
"""

from __future__ import annotations

import os

import pytest

from opencrab.execution.approvals import ApprovalEngine
from opencrab.stores.sql_store import SQLStore


@pytest.fixture(params=["sqlite", "pg"])
def sql_store(request, tmp_path):
    if request.param == "sqlite":
        db_path = tmp_path / "approvals.db"
        store = SQLStore(f"sqlite:///{db_path}")
        assert store.available
        yield store
        return

    dsn = os.environ.get("OPENCRAB_PG_TEST_URL")
    if not dsn:
        pytest.skip("OPENCRAB_PG_TEST_URL 미설정 - PG 승인큐 테스트 스킵")
    store = SQLStore(dsn)
    if not store.available:
        pytest.skip(f"PG 테스트 DB 접속 불가: {dsn!r}")
    yield store
    from sqlalchemy import text

    with store._engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS approval_queue"))


@pytest.fixture
def engine(sql_store):
    return ApprovalEngine(sql_store)


# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestApprovalsNormal:
    def test_request_creates_pending_row(self, engine):
        result = engine.request("restrict_access", "alice", {"resource_id": "r1"})

        assert result["approval_id"].startswith("appr_")
        assert result["status"] == "pending"

        row = engine.get(result["approval_id"])
        assert row is not None
        assert row["status"] == "pending"
        assert row["subject_id"] == "alice"
        assert row["action_type"] == "restrict_access"

    def test_request_appears_in_list_pending(self, engine):
        created = engine.request("restrict_access", "alice", {})
        pending = engine.list_pending()
        assert any(p["approval_id"] == created["approval_id"] for p in pending)

    def test_resolve_approve_updates_row(self, engine):
        created = engine.request("restrict_access", "alice", {})
        result = engine.resolve(
            created["approval_id"], "approved", reviewer_id="bob", note="looks fine"
        )

        assert result["status"] == "approved"
        assert result["reviewer_id"] == "bob"

        row = engine.get(created["approval_id"])
        assert row["status"] == "approved"
        assert row["reviewer_id"] == "bob"
        assert row["review_note"] == "looks fine"
        assert row["resolved_at"] is not None

    def test_resolve_reject_updates_row(self, engine):
        created = engine.request("restrict_access", "alice", {})
        result = engine.resolve(created["approval_id"], "rejected", reviewer_id="bob")

        assert result["status"] == "rejected"
        assert engine.get(created["approval_id"])["status"] == "rejected"

    def test_list_pending_ordering_is_oldest_first(self, engine):
        first = engine.request("restrict_access", "alice", {})
        second = engine.request("restrict_access", "alice", {})
        third = engine.request("restrict_access", "alice", {})

        pending_ids = [p["approval_id"] for p in engine.list_pending()]
        assert pending_ids == [first["approval_id"], second["approval_id"], third["approval_id"]]

    def test_list_pending_excludes_resolved(self, engine):
        keep = engine.request("restrict_access", "alice", {})
        resolved = engine.request("restrict_access", "alice", {})
        engine.resolve(resolved["approval_id"], "approved")

        pending_ids = [p["approval_id"] for p in engine.list_pending()]
        assert pending_ids == [keep["approval_id"]]


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestApprovalsError:
    def test_resolve_invalid_decision_raises(self, engine):
        created = engine.request("restrict_access", "alice", {})
        with pytest.raises(ValueError, match="approved.*rejected"):
            engine.resolve(created["approval_id"], "maybe")

        # rejected before touching the row -- state must be unchanged
        assert engine.get(created["approval_id"])["status"] == "pending"

    def test_double_resolve_second_call_fails_cleanly(self, engine):
        created = engine.request("restrict_access", "alice", {})
        engine.resolve(created["approval_id"], "approved", reviewer_id="bob")

        with pytest.raises(ValueError, match="not found or already resolved"):
            engine.resolve(created["approval_id"], "rejected", reviewer_id="carol")

        # the guard (`AND status='pending'`) must have prevented the second
        # write from overwriting the first resolution
        row = engine.get(created["approval_id"])
        assert row["status"] == "approved"
        assert row["reviewer_id"] == "bob"


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestApprovalsEdge:
    def test_resolve_nonexistent_id_raises(self, engine):
        with pytest.raises(ValueError, match="not found or already resolved"):
            engine.resolve("appr_does_not_exist", "approved")

    def test_get_nonexistent_returns_none(self, engine):
        assert engine.get("appr_does_not_exist") is None

    def test_list_pending_empty_queue(self, engine):
        assert engine.list_pending() == []
