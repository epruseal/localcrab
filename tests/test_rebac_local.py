"""
Tests for ReBACEngine using LocalGraphStore (local / SQLite-backed mode).

Verifies that the local-mode graph traversal correctly evaluates access
decisions without relying on run_cypher() (which is a no-op in local mode).
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from opencrab.ontology.rebac import (
    _SQL_LOOKUP_FAILED_REASON,
    _SQL_NON_BOOLEAN_REASON,
    ReBACEngine,
)
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.sql_store import SQLStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path) -> LocalGraphStore:
    return LocalGraphStore(str(tmp_path / "rebac_test.db"))


def _make_sql_stub() -> MagicMock:
    """Return a SQL store stub that is unavailable (skips SQL policy checks)."""
    stub = MagicMock()
    stub.available = False
    return stub


def _make_engine(store: LocalGraphStore) -> ReBACEngine:
    return ReBACEngine(neo4j=store, sql=_make_sql_stub())


# ---------------------------------------------------------------------------
# Direct access tests
# ---------------------------------------------------------------------------

class TestDirectAccess:
    def test_direct_owns_edge_grants_access(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.upsert_node("User", "user1", {"name": "Alice"})
        store.upsert_node("Resource", "res1", {"name": "MyDoc"})
        store.upsert_edge("User", "user1", "owns", "Resource", "res1")

        engine = _make_engine(store)
        decision = engine.check("user1", "view", "res1")

        assert decision.granted is True
        assert "owns" in decision.reason
        assert decision.subject_id == "user1"
        assert decision.resource_id == "res1"
        assert decision.permission == "view"

    def test_direct_can_edit_edge_grants_edit_permission(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.upsert_node("User", "user2", {"name": "Bob"})
        store.upsert_node("Resource", "res2", {"name": "Sheet"})
        store.upsert_edge("User", "user2", "can_edit", "Resource", "res2")

        engine = _make_engine(store)
        decision = engine.check("user2", "edit", "res2")

        assert decision.granted is True
        assert "can_edit" in decision.reason

    def test_direct_unrelated_edge_returns_deny(self, tmp_path: Path) -> None:
        """An edge with a relation type not in the permission mapping → deny."""
        store = _make_store(tmp_path)
        store.upsert_node("User", "user3", {"name": "Carol"})
        store.upsert_node("Resource", "res3", {"name": "File"})
        # "references" is not a valid permission-granting relation
        store.upsert_edge("User", "user3", "references", "Resource", "res3")

        engine = _make_engine(store)
        decision = engine.check("user3", "admin", "res3")

        assert decision.granted is False

    def test_no_edge_at_all_returns_deny(self, tmp_path: Path) -> None:
        """Nodes exist but no edge between them → default deny."""
        store = _make_store(tmp_path)
        store.upsert_node("User", "user4", {"name": "Dave"})
        store.upsert_node("Resource", "res4", {"name": "Notebook"})

        engine = _make_engine(store)
        decision = engine.check("user4", "view", "res4")

        assert decision.granted is False

    def test_direct_check_returns_none_for_wrong_resource(self, tmp_path: Path) -> None:
        """Edge points to a different resource → should not grant access."""
        store = _make_store(tmp_path)
        store.upsert_node("User", "user5", {"name": "Eve"})
        store.upsert_node("Resource", "res5a", {"name": "DocA"})
        store.upsert_node("Resource", "res5b", {"name": "DocB"})
        store.upsert_edge("User", "user5", "owns", "Resource", "res5a")

        engine = _make_engine(store)
        decision = engine.check("user5", "view", "res5b")

        assert decision.granted is False


# ---------------------------------------------------------------------------
# Transitive access tests
# ---------------------------------------------------------------------------

class TestTransitiveAccess:
    def test_member_of_group_with_can_view_grants_view(self, tmp_path: Path) -> None:
        """subject → (member_of) → group → (can_view) → resource → granted."""
        store = _make_store(tmp_path)
        store.upsert_node("User", "u_trans1", {"name": "Frank"})
        store.upsert_node("Team", "team1", {"name": "Editors"})
        store.upsert_node("Resource", "r_trans1", {"name": "Report"})

        store.upsert_edge("User", "u_trans1", "member_of", "Team", "team1")
        store.upsert_edge("Team", "team1", "can_view", "Resource", "r_trans1")

        engine = _make_engine(store)
        decision = engine.check("u_trans1", "view", "r_trans1")

        assert decision.granted is True
        assert "team1" in decision.reason
        assert "can_view" in decision.reason
        assert decision.path is not None
        assert "u_trans1" in decision.path
        assert "team1" in decision.path
        assert "r_trans1" in decision.path

    def test_manages_group_with_can_approve_grants_approve(self, tmp_path: Path) -> None:
        """subject → (manages) → group → (can_approve) → resource → granted."""
        store = _make_store(tmp_path)
        store.upsert_node("User", "u_trans2", {"name": "Grace"})
        store.upsert_node("Team", "team2", {"name": "Approvers"})
        store.upsert_node("Resource", "r_trans2", {"name": "Contract"})

        store.upsert_edge("User", "u_trans2", "manages", "Team", "team2")
        store.upsert_edge("Team", "team2", "can_approve", "Resource", "r_trans2")

        engine = _make_engine(store)
        decision = engine.check("u_trans2", "approve", "r_trans2")

        assert decision.granted is True
        assert "team2" in decision.reason

    def test_transitive_no_path_returns_deny(self, tmp_path: Path) -> None:
        """Group exists but has no permission edge to the resource → deny."""
        store = _make_store(tmp_path)
        store.upsert_node("User", "u_trans3", {"name": "Hank"})
        store.upsert_node("Team", "team3", {"name": "Readers"})
        store.upsert_node("Resource", "r_trans3", {"name": "Secret"})

        # subject is in the group but the group has no edge to the resource
        store.upsert_edge("User", "u_trans3", "member_of", "Team", "team3")

        engine = _make_engine(store)
        decision = engine.check("u_trans3", "view", "r_trans3")

        assert decision.granted is False

    def test_transitive_wrong_group_relation_returns_deny(self, tmp_path: Path) -> None:
        """subject → (references) → group (not member_of/manages) → no transitive grant."""
        store = _make_store(tmp_path)
        store.upsert_node("User", "u_trans4", {"name": "Iris"})
        store.upsert_node("Team", "team4", {"name": "Owners"})
        store.upsert_node("Resource", "r_trans4", {"name": "Data"})

        # "references" is not a group-membership relation; transitive check won't follow it
        store.upsert_edge("User", "u_trans4", "references", "Team", "team4")
        store.upsert_edge("Team", "team4", "owns", "Resource", "r_trans4")

        engine = _make_engine(store)
        decision = engine.check("u_trans4", "admin", "r_trans4")

        assert decision.granted is False


# ---------------------------------------------------------------------------
# Permission validation
# ---------------------------------------------------------------------------

class TestPermissionValidation:
    def test_invalid_permission_returns_deny(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        engine = _make_engine(store)
        decision = engine.check("user_x", "fly", "res_x")

        assert decision.granted is False
        assert "Invalid" in decision.reason or decision.reason  # some error message present


# ---------------------------------------------------------------------------
# SQL store failure tests (issue #78)
# ---------------------------------------------------------------------------
#
# ``check()`` must be a fail-closed authorization boundary. The graph path
# already swallows store errors; the SQL policy lookup did not, so a DB
# outage propagated as an exception. These tests fix three contracts:
#
# 1. any exception from the SQL availability check or ``check_policy`` yields
#    a DENY decision with the dedicated reason and one WARNING that names the
#    exception type but never its text (the text can carry a DSN);
# 2. the graph is not consulted after a SQL failure, because an explicit DENY
#    row that could not be read must not be overridden by a graph GRANT;
# 3. a ``check_policy`` return outside ``bool | None`` is DENY, not "no policy".


class _RaisingAvailable:
    """SQL stub whose ``available`` property itself raises."""

    @property
    def available(self) -> bool:
        raise RuntimeError("SECRET-DSN-MARKER available probe failed")

    def check_policy(self, *args: object) -> bool | None:  # pragma: no cover
        raise AssertionError("check_policy must not be reached")


def _grant_graph(tmp_path: Path) -> LocalGraphStore:
    """Graph with a direct owns edge: GRANT when SQL says 'no policy'."""
    store = _make_store(tmp_path)
    store.upsert_node("User", "u1", {"name": "U"})
    store.upsert_node("Resource", "r1", {"name": "R"})
    store.upsert_edge("User", "u1", "owns", "Resource", "r1")
    return store


def _spy_graph(store: LocalGraphStore) -> MagicMock:
    spy = MagicMock(wraps=store)
    spy.available = True
    return spy


def _failing_sql(exc: Exception) -> MagicMock:
    stub = MagicMock()
    stub.available = True
    stub.check_policy.side_effect = exc
    return stub


def _rebac_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "opencrab.ontology.rebac" and r.levelno == logging.WARNING
    ]


class TestSQLStoreFailure:
    def test_sql_lookup_exception_returns_deny_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = ReBACEngine(
            neo4j=_make_store(tmp_path), sql=_failing_sql(RuntimeError("connection closed"))
        )
        with caplog.at_level(logging.WARNING, logger="opencrab.ontology.rebac"):
            decision = engine.check("u1", "view", "r1")

        assert decision.granted is False
        assert decision.reason == _SQL_LOOKUP_FAILED_REASON
        warnings = _rebac_warnings(caplog)
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        for needle in ("RuntimeError", "subject=u1", "permission=view", "resource=r1"):
            assert needle in msg, msg

    def test_sql_lookup_exception_with_real_sqlite_store(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        sql = SQLStore(f"sqlite:///{tmp_path / 'policies.db'}")
        with sql._engine.begin() as conn:
            conn.execute(text("DROP TABLE rebac_policies"))
        # Fixture preconditions: the store still reports available, and the
        # lookup itself now raises a real driver error.
        assert sql.available is True
        with pytest.raises(OperationalError):
            sql.check_policy("u1", "view", "r1")

        engine = ReBACEngine(neo4j=_make_store(tmp_path), sql=sql)
        with caplog.at_level(logging.WARNING, logger="opencrab.ontology.rebac"):
            decision = engine.check("u1", "view", "r1")

        assert decision.granted is False
        assert decision.reason == _SQL_LOOKUP_FAILED_REASON
        warnings = _rebac_warnings(caplog)
        assert len(warnings) == 1
        assert "OperationalError" in warnings[0].getMessage()

    def test_sql_lookup_exception_does_not_fall_through_to_graph(
        self, tmp_path: Path
    ) -> None:
        graph = _grant_graph(tmp_path)
        # Control: with SQL saying "no policy" the graph grants.
        ok_sql = MagicMock()
        ok_sql.available = True
        ok_sql.check_policy.return_value = None
        assert ReBACEngine(neo4j=graph, sql=ok_sql).check("u1", "view", "r1").granted is True

        spy = _spy_graph(graph)
        engine = ReBACEngine(neo4j=spy, sql=_failing_sql(RuntimeError("db down")))
        decision = engine.check("u1", "view", "r1")

        assert decision.granted is False
        assert decision.reason == _SQL_LOOKUP_FAILED_REASON
        spy.find_neighbors.assert_not_called()

    def test_sql_lookup_exception_message_is_not_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        exc = RuntimeError("SECRET-DSN-MARKER postgresql://user:pw@host/db")
        engine = ReBACEngine(neo4j=_make_store(tmp_path), sql=_failing_sql(exc))
        with caplog.at_level(logging.DEBUG, logger="opencrab.ontology.rebac"):
            engine.check("u1", "view", "r1")

        warnings = _rebac_warnings(caplog)
        assert len(warnings) == 1
        assert "SECRET-DSN-MARKER" not in warnings[0].getMessage()
        assert "RuntimeError" in warnings[0].getMessage()
        # The full traceback stays reachable for operators at DEBUG only.
        debug = [
            r
            for r in caplog.records
            if r.name == "opencrab.ontology.rebac" and r.levelno == logging.DEBUG
        ]
        assert any(r.exc_info and r.exc_info[1] is exc for r in debug)

    def test_sql_available_exception_returns_deny(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        spy = _spy_graph(_grant_graph(tmp_path))
        engine = ReBACEngine(neo4j=spy, sql=_RaisingAvailable())
        with caplog.at_level(logging.WARNING, logger="opencrab.ontology.rebac"):
            decision = engine.check("u1", "view", "r1")

        assert decision.granted is False
        assert decision.reason == _SQL_LOOKUP_FAILED_REASON
        warnings = _rebac_warnings(caplog)
        assert len(warnings) == 1
        assert "SECRET-DSN-MARKER" not in warnings[0].getMessage()
        spy.find_neighbors.assert_not_called()

    @pytest.mark.parametrize("value", [1, 0, "yes", object()], ids=["int1", "int0", "str", "obj"])
    def test_sql_non_boolean_policy_value_denies_without_graph(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, value: object
    ) -> None:
        graph = _grant_graph(tmp_path)
        ok_sql = MagicMock()
        ok_sql.available = True
        ok_sql.check_policy.return_value = None
        assert ReBACEngine(neo4j=graph, sql=ok_sql).check("u1", "view", "r1").granted is True

        spy = _spy_graph(graph)
        bad_sql = MagicMock()
        bad_sql.available = True
        bad_sql.check_policy.return_value = value
        engine = ReBACEngine(neo4j=spy, sql=bad_sql)
        with caplog.at_level(logging.WARNING, logger="opencrab.ontology.rebac"):
            decision = engine.check("u1", "view", "r1")

        assert decision.granted is False
        assert decision.reason == _SQL_NON_BOOLEAN_REASON
        warnings = _rebac_warnings(caplog)
        assert len(warnings) == 1
        assert type(value).__name__ in warnings[0].getMessage()
        spy.find_neighbors.assert_not_called()

    def test_graph_exception_still_denies(self, tmp_path: Path) -> None:
        graph = MagicMock()
        graph.available = True
        graph.find_neighbors.side_effect = RuntimeError("graph down")
        engine = ReBACEngine(neo4j=graph, sql=_make_sql_stub())

        decision = engine.check("u1", "view", "r1")

        assert decision.granted is False
        assert "Default deny" in decision.reason

    @pytest.mark.parametrize(
        ("stored", "granted", "reason_fragment"),
        [(True, True, "Explicit GRANT"), (False, False, "Explicit DENY")],
        ids=["grant", "deny"],
    )
    def test_explicit_policy_paths_unchanged(
        self, tmp_path: Path, stored: bool, granted: bool, reason_fragment: str
    ) -> None:
        sql = MagicMock()
        sql.available = True
        sql.check_policy.return_value = stored
        engine = ReBACEngine(neo4j=_make_store(tmp_path), sql=sql)

        decision = engine.check("u1", "view", "r1")

        assert decision.granted is granted
        assert reason_fragment in decision.reason
