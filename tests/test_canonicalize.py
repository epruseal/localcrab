"""
Contract tests for opencrab.ontology.canonicalize.CanonicalizeEngine.

CanonicalizeEngine is a thin orchestration layer over IdentityEngine (real
SQLite-backed here) plus an OntologyBuilder collaborator.

KNOWN GAP (documented, not silently pinned — see canonicalize.py's updated
docstrings and the report for this test wave): `merge_nodes`'s
`merge_properties` flag and its `builder` constructor argument are currently
inert — no property copy from alias to canonical node happens regardless of
the flag. The tests below assert only what merge_nodes actually contractually
does today (alias registration + receipt), and explicitly verify the builder
is not touched, rather than asserting a property-merge outcome one way or
the other.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opencrab.ontology.canonicalize import CanonicalizeEngine
from opencrab.ontology.identity import IdentityEngine
from opencrab.stores.sql_store import SQLStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def identity(tmp_path):
    sql = SQLStore(f"sqlite:///{tmp_path / 'canon.db'}")
    assert sql.available
    return IdentityEngine(sql), sql


@pytest.fixture
def builder():
    return MagicMock(name="builder")


@pytest.fixture
def engine(identity, builder):
    identity_engine, _sql = identity
    return CanonicalizeEngine(identity_engine, builder)


# ===========================================================================
# merge_nodes — 정상 (Normal)
# ===========================================================================

class TestMergeNodesNormal:
    def test_registers_alias_and_returns_receipt(self, engine, identity):
        identity_engine, _sql = identity
        result = engine.merge_nodes(
            "canon1", "alias1", "subject", "Agent", merged_by="tester"
        )

        assert result["canonical_id"] == "canon1"
        assert result["alias_id"] == "alias1"
        assert result["space"] == "subject"
        assert result["merged_by"] == "tester"
        assert result["receipt_id"].startswith("rcpt_")
        assert "receipt_ts" in result

        aliases = identity_engine.get_aliases("canon1")
        assert any(a["alias_id"] == "alias1" and a["alias_type"] == "merge" for a in aliases)

    def test_resolve_canonical_reflects_the_merge(self, engine, identity):
        identity_engine, _sql = identity
        engine.merge_nodes("canon1", "alias1", "subject", "Agent")
        assert identity_engine.resolve_canonical("alias1") == "canon1"


# ===========================================================================
# merge_nodes — 엣지 (Edge) / documented gap
# ===========================================================================

class TestMergeNodesEdges:
    def test_builder_is_never_invoked_regardless_of_merge_properties_flag(self, engine, builder):
        """Documents the current, intentional-for-now scope of merge_nodes:
        it is alias-registration-only. If a future change starts reading/
        writing node properties through `builder`, this test should be
        updated to assert the new contract instead of deleted blindly."""
        engine.merge_nodes("canon1", "alias1", "subject", "Agent", merge_properties=True)
        assert builder.method_calls == []

        engine.merge_nodes("canon2", "alias2", "subject", "Agent", merge_properties=False)
        assert builder.method_calls == []

    def test_repeated_merge_of_same_pair_is_idempotent(self, engine, identity):
        identity_engine, _sql = identity
        engine.merge_nodes("canon1", "alias1", "subject", "Agent")
        engine.merge_nodes("canon1", "alias1", "subject", "Agent")
        aliases = identity_engine.get_aliases("canon1")
        assert len(aliases) == 1


# ===========================================================================
# find_and_propose — 정상 (Normal)
# ===========================================================================

class TestFindAndProposeNormal:
    def test_proposes_candidates_for_each_match_above_threshold(self, engine, identity):
        _identity_engine, sql = identity
        sql.register_node("subject", "Agent", "alexlee_agent")
        sql.register_node("subject", "Agent", "bob_agent")

        result = engine.find_and_propose(
            "new_agent", "alexlee_agent", space="subject", threshold=0.3
        )

        assert result["node_id"] == "new_agent"
        assert result["candidates_found"] == 1
        assert len(result["proposals"]) == 1
        assert result["proposals"][0]["status"] == "pending"

    def test_no_matches_returns_empty_proposals_without_error(self, engine):
        result = engine.find_and_propose("new_agent", "totally_unique_name", space="subject")
        assert result == {"node_id": "new_agent", "candidates_found": 0, "proposals": []}


# ===========================================================================
# find_and_propose — 엣지 (Edge)
# ===========================================================================

class TestFindAndProposeEdges:
    def test_proposals_are_not_applied_automatically(self, engine, identity):
        """find_and_propose's own docstring promises 'None are applied
        automatically' -- verify no alias is created as a side effect."""
        identity_engine, sql = identity
        sql.register_node("subject", "Agent", "alexlee_agent")

        engine.find_and_propose("new_agent", "alexlee_agent", space="subject", threshold=0.3)

        assert identity_engine.list_pending_candidates() != []
        assert identity_engine.get_aliases("alexlee_agent") == []
        assert identity_engine.get_aliases("new_agent") == []


# ===========================================================================
# batch_find_and_propose — 정상 (Normal)
# ===========================================================================

class TestBatchFindAndProposeNormal:
    def test_runs_find_and_propose_for_every_node_in_batch(self, engine, identity):
        _identity_engine, sql = identity
        sql.register_node("subject", "Agent", "alexlee_agent")

        results = engine.batch_find_and_propose([
            {"node_id": "n1", "name": "alexlee_agent", "space": "subject"},
            {"node_id": "n2", "name": "no_match_at_all", "space": "subject"},
        ])

        assert [r["node_id"] for r in results] == ["n1", "n2"]
        assert results[0]["candidates_found"] == 1
        assert results[1]["candidates_found"] == 0

    def test_missing_name_key_defaults_to_node_id(self, engine, identity):
        """batch_find_and_propose's docstring: 'name' is documented as
        required per node dict, but the implementation falls back to
        node_id via node.get("name", node["node_id"]) -- confirm that
        fallback actually works rather than raising KeyError."""
        _identity_engine, sql = identity
        sql.register_node("subject", "Agent", "n2")

        results = engine.batch_find_and_propose([
            {"node_id": "n2_dup", "space": "subject"},
        ])
        assert results[0]["node_id"] == "n2_dup"
        # searched using name="n2_dup" (its own node_id, since no 'name' key)
        assert results[0]["candidates_found"] == 0


# ===========================================================================
# batch_find_and_propose — 오류 (Error)
# ===========================================================================

class TestBatchFindAndProposeErrors:
    def test_missing_node_id_key_raises_key_error(self, engine):
        """node_id is not optional -- both node.get("name", node["node_id"])
        and find_and_propose's own required node_id param need it."""
        with pytest.raises(KeyError):
            engine.batch_find_and_propose([{"name": "no_id_here"}])

    def test_empty_batch_returns_empty_list(self, engine):
        assert engine.batch_find_and_propose([]) == []
