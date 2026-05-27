"""
Tests for ReBACEngine using LocalGraphStore (local / SQLite-backed mode).

Verifies that the local-mode graph traversal correctly evaluates access
decisions without relying on run_cypher() (which is a no-op in local mode).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencrab.ontology.rebac import AccessDecision, ReBACEngine
from opencrab.stores.local_graph_store import LocalGraphStore


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
