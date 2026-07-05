"""
Stage 5 (B1/B2) unified-behavior contract tests — EXPECTED RED until the
E-agents implement the new contracts. Do not xfail these: a genuine failure
here is the proof the old per-backend divergence still exists; the E-agents
turn each assertion green by editing the corresponding store.

B1 find_path(from_id, to_id, max_depth) unifies to: "max_depth = maximum
number of HOPS, out-edges only" (this is KuzuGraphStore's existing
semantics — kuzu_graph_store.py:375 already checks
``len(path) >= max_depth``, and its BFS only follows ``-[e]->``).
Divergence being closed:
  - local_graph_store.py:393 / pg_graph_store.py:526 check
    ``len(path) >= max_depth * 2`` -- i.e. they allow up to 2x the named
    depth in hops.
  - neo4j_store.py:347-349 uses ``shortestPath((a)-[*1..max_depth]-(b))``
    with NO arrow before ``(b)`` -- an undirected pattern -- while the other
    three backends only ever follow outgoing edges.

B2 delete_node(node_type, node_id) -> bool unifies to: "True iff the node
itself was deleted" (this is Neo4jStore's existing semantics --
neo4j_store.py:177-185 returns ``count(n) > 0`` from a DETACH DELETE).
Divergence being closed:
  - local_graph_store.py / pg_graph_store.py return the *edge*-delete
    rowcount (a reused-cursor-rowcount accident), so a node with zero
    incident edges deletes silently but returns False -- see
    pg_graph_store.py's "QUIRK PRESERVED ON PURPOSE" docstring.
  - kuzu_graph_store.py:162-172 returns True unconditionally whenever no
    exception was raised, even when the node never existed (a Cypher
    DETACH DELETE on zero matches is not an error).
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock, patch

import pytest

from opencrab.stores.local_graph_store import LocalGraphStore

PG_URL = os.environ.get("OPENCRAB_PG_TEST_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="OPENCRAB_PG_TEST_URL not set -- PG contract tests skipped"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def local_store(tmp_path):
    store = LocalGraphStore(str(tmp_path / "graph.db"))
    yield store
    store.close()


@pytest.fixture
def pg_engine():
    if not PG_URL:
        yield None
        return
    from sqlalchemy import create_engine

    engine = create_engine(PG_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_store(pg_engine):
    if not PG_URL:
        pytest.skip("OPENCRAB_PG_TEST_URL not set -- PG contract tests skipped")
    from sqlalchemy import text

    from opencrab.stores.pg_graph_store import PGGraphStore

    schema = f"t{uuid.uuid4().hex[:12]}_c"
    store = PGGraphStore(pg_engine, schema=schema)
    yield store
    with pg_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.fixture
def kuzu_store(tmp_path):
    pytest.importorskip("ladybug")
    from opencrab.stores.kuzu_graph_store import KuzuGraphStore

    store = KuzuGraphStore(db_path=str(tmp_path / "kuzu_db"))
    yield store
    store.close()


def _make_neo4j_store():
    """Build a Neo4jStore whose ``_connect`` succeeds against a mocked driver
    (same pattern as tests/test_neo4j_helpers.py -- no live Neo4j needed)."""
    from opencrab.stores.neo4j_store import Neo4jStore

    mock_session = MagicMock(name="session")
    mock_driver = MagicMock(name="driver")
    mock_driver.session.return_value.__enter__.return_value = mock_session
    mock_driver.session.return_value.__exit__.return_value = False

    with patch("neo4j.GraphDatabase") as mock_gdb:
        mock_gdb.driver.return_value = mock_driver
        store = Neo4jStore("bolt://mock:7687", "neo4j", "pw")
    return store, mock_session


def _make_chain(store, length: int, prefix: str = "n") -> None:
    """Create a directed chain prefix0 -> prefix1 -> ... -> prefix{length}."""
    for i in range(length + 1):
        store.upsert_node("Item", f"{prefix}{i}", {})
    for i in range(length):
        store.upsert_edge("Item", f"{prefix}{i}", "next", "Item", f"{prefix}{i + 1}")


# ---------------------------------------------------------------------------
# B1 -- find_path: max_depth hops, out-edges only
# ---------------------------------------------------------------------------


class TestFindPathLocal:
    def test_path_of_exactly_max_depth_hops_is_found(self, local_store):
        _make_chain(local_store, 4)
        path = local_store.find_path("n0", "n4", max_depth=4)
        assert [step["relation"] for step in path] == ["next"] * 4
        assert path[-1]["node"]["id"] == "n4"

    def test_path_requiring_max_depth_plus_one_hops_not_found(self, local_store):
        """RED today: local allows up to 2*max_depth hops, so a 5-hop chain
        is (wrongly) found with max_depth=4."""
        _make_chain(local_store, 5)
        assert local_store.find_path("n0", "n5", max_depth=4) == []

    def test_single_hop(self, local_store):
        _make_chain(local_store, 1)
        path = local_store.find_path("n0", "n1", max_depth=4)
        assert path == [{"node": {"id": "n1"}, "relation": "next"}]

    def test_no_path(self, local_store):
        local_store.upsert_node("Item", "a", {})
        local_store.upsert_node("Item", "b", {})
        assert local_store.find_path("a", "b", max_depth=4) == []

    def test_unknown_src_or_dst_returns_empty(self, local_store):
        _make_chain(local_store, 2)
        assert local_store.find_path("does_not_exist", "n2", max_depth=4) == []
        assert local_store.find_path("n0", "does_not_exist", max_depth=4) == []

    def test_reverse_edge_not_reachable_out_only(self, local_store):
        """B1 is out-edges only: a<-b must NOT be reachable as a->b."""
        local_store.upsert_node("Item", "a", {})
        local_store.upsert_node("Item", "b", {})
        local_store.upsert_edge("Item", "b", "next", "Item", "a")
        assert local_store.find_path("a", "b", max_depth=4) == []


@requires_pg
class TestFindPathPg:
    def test_path_of_exactly_max_depth_hops_is_found(self, pg_store):
        _make_chain(pg_store, 4)
        path = pg_store.find_path("n0", "n4", max_depth=4)
        assert [step["relation"] for step in path] == ["next"] * 4
        assert path[-1]["node"]["id"] == "n4"

    def test_path_requiring_max_depth_plus_one_hops_not_found(self, pg_store):
        """RED today: pg allows up to 2*max_depth hops, same as local."""
        _make_chain(pg_store, 5)
        assert pg_store.find_path("n0", "n5", max_depth=4) == []

    def test_single_hop(self, pg_store):
        _make_chain(pg_store, 1)
        path = pg_store.find_path("n0", "n1", max_depth=4)
        assert path == [{"node": {"id": "n1"}, "relation": "next"}]

    def test_no_path(self, pg_store):
        pg_store.upsert_node("Item", "a", {})
        pg_store.upsert_node("Item", "b", {})
        assert pg_store.find_path("a", "b", max_depth=4) == []

    def test_unknown_src_or_dst_returns_empty(self, pg_store):
        _make_chain(pg_store, 2)
        assert pg_store.find_path("does_not_exist", "n2", max_depth=4) == []
        assert pg_store.find_path("n0", "does_not_exist", max_depth=4) == []


class TestFindPathKuzu:
    """Kuzu is already the reference semantics for B1 -- these pin the
    contract rather than expect RED, so a regression here is a real bug."""

    def test_path_of_exactly_max_depth_hops_is_found(self, kuzu_store):
        _make_chain(kuzu_store, 4)
        path = kuzu_store.find_path("n0", "n4", max_depth=4)
        assert [step["relation"] for step in path] == ["next"] * 4

    def test_path_requiring_max_depth_plus_one_hops_not_found(self, kuzu_store):
        _make_chain(kuzu_store, 5)
        assert kuzu_store.find_path("n0", "n5", max_depth=4) == []

    def test_no_path(self, kuzu_store):
        kuzu_store.upsert_node("Item", "a", {})
        kuzu_store.upsert_node("Item", "b", {})
        assert kuzu_store.find_path("a", "b", max_depth=4) == []

    def test_unknown_src_or_dst_returns_empty(self, kuzu_store):
        _make_chain(kuzu_store, 2)
        assert kuzu_store.find_path("does_not_exist", "n2", max_depth=4) == []
        assert kuzu_store.find_path("n0", "does_not_exist", max_depth=4) == []


class TestFindPathNeo4j:
    def test_cypher_pattern_is_directed_not_undirected(self):
        """RED today: neo4j_store.py builds an undirected shortestPath
        pattern (``-[*1..N]-(b)``, no arrow). The unified out-edges-only
        contract requires a directed pattern (``-[*1..N]->(b)``)."""
        store, mock_session = _make_neo4j_store()
        mock_session.run.return_value.single.return_value = None

        store.find_path("a", "b", max_depth=4)

        cypher = mock_session.run.call_args[0][0]
        assert "]->(b" in cypher, f"expected a directed pattern, got: {cypher!r}"

    def test_max_depth_is_hop_bound_in_cypher(self):
        store, mock_session = _make_neo4j_store()
        mock_session.run.return_value.single.return_value = None

        store.find_path("a", "b", max_depth=3)

        cypher = mock_session.run.call_args[0][0]
        assert "*1..3" in cypher

    def test_no_record_returns_empty(self):
        store, mock_session = _make_neo4j_store()
        mock_session.run.return_value.single.return_value = None
        assert store.find_path("a", "b") == []


# ---------------------------------------------------------------------------
# B2 -- delete_node: True iff the node itself was deleted
# ---------------------------------------------------------------------------


class TestDeleteNodeLocal:
    def test_node_with_edges_deleted_true(self, local_store):
        local_store.upsert_node("Item", "a", {})
        local_store.upsert_node("Item", "b", {})
        local_store.upsert_edge("Item", "a", "next", "Item", "b")
        assert local_store.delete_node("Item", "a") is True

    def test_nonexistent_node_false(self, local_store):
        assert local_store.delete_node("Item", "never_existed") is False

    def test_node_with_zero_edges_deleted_true(self, local_store):
        """RED today: LocalGraphStore's delete_node returns the *edge*-DELETE
        rowcount, so a node with no incident edges deletes silently but
        returns False."""
        local_store.upsert_node("Item", "lonely", {})
        assert local_store.delete_node("Item", "lonely") is True
        assert local_store.get_node("Item", "lonely") is None


@requires_pg
class TestDeleteNodePg:
    def test_node_with_edges_deleted_true(self, pg_store):
        pg_store.upsert_node("Item", "a", {})
        pg_store.upsert_node("Item", "b", {})
        pg_store.upsert_edge("Item", "a", "next", "Item", "b")
        assert pg_store.delete_node("Item", "a") is True

    def test_nonexistent_node_false(self, pg_store):
        assert pg_store.delete_node("Item", "never_existed") is False

    def test_node_with_zero_edges_deleted_true(self, pg_store):
        """RED today: same QUIRK as local (PGGraphStore.delete_node's
        docstring names it "QUIRK PRESERVED ON PURPOSE")."""
        pg_store.upsert_node("Item", "lonely", {})
        assert pg_store.delete_node("Item", "lonely") is True
        assert pg_store.get_node("Item", "lonely") is None


class TestDeleteNodeKuzu:
    def test_node_with_edges_deleted_true(self, kuzu_store):
        kuzu_store.upsert_node("Item", "a", {})
        kuzu_store.upsert_node("Item", "b", {})
        kuzu_store.upsert_edge("Item", "a", "next", "Item", "b")
        assert kuzu_store.delete_node("Item", "a") is True

    def test_node_with_zero_edges_deleted_true(self, kuzu_store):
        kuzu_store.upsert_node("Item", "lonely", {})
        assert kuzu_store.delete_node("Item", "lonely") is True

    def test_nonexistent_node_false(self, kuzu_store):
        """RED today: KuzuGraphStore.delete_node returns True whenever no
        exception was raised -- a DETACH DELETE matching zero nodes does not
        raise, so a nonexistent node currently (incorrectly) returns True."""
        assert kuzu_store.delete_node("Item", "never_existed") is False


class TestDeleteNodeTypeMismatch:
    """delete_node(node_type, node_id) must match on the (node_type, node_id)
    PAIR, not node_id alone — a wrong node_type must be a no-op (return
    False, node stays). local/pg/neo4j already do this (WHERE .../Cypher
    label checks both); kuzu_graph_store.py's Cypher (``MATCH (n:OntologyNode
    {node_id: $id}) DETACH DELETE n``) matches by node_id ONLY, so a wrong
    node_type still deletes the node — Stage 6b's inherited RED case (its
    fix belongs to a kuzu-specific lane / F4, not the two SQL stores this
    stage migrates)."""

    def test_local_wrong_type_is_noop(self, local_store):
        local_store.upsert_node("Item", "a", {})
        assert local_store.delete_node("WrongType", "a") is False
        assert local_store.get_node_by_id("a") is not None

    @requires_pg
    def test_pg_wrong_type_is_noop(self, pg_store):
        pg_store.upsert_node("Item", "a", {})
        assert pg_store.delete_node("WrongType", "a") is False
        assert pg_store.get_node_by_id("a") is not None

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "kuzu_graph_store.py's delete_node Cypher matches by node_id "
            "only, ignoring node_type -- deleting with a wrong node_type "
            "still removes the node. Fix: add node_type to the MATCH/WHERE "
            "clause (owned by a dedicated kuzu lane, not Stage 6b's two SQL "
            "graph stores)."
        ),
    )
    def test_kuzu_wrong_type_is_noop(self, kuzu_store):
        kuzu_store.upsert_node("Item", "a", {})
        assert kuzu_store.delete_node("WrongType", "a") is False
        assert kuzu_store.get_node_by_id("a") is not None


class TestDeleteNodeNeo4j:
    """Neo4j is already the reference semantics for B2 -- pin, not RED."""

    def test_node_with_edges_deleted_true(self):
        store, mock_session = _make_neo4j_store()
        mock_session.run.return_value.single.return_value = {"cnt": 1}
        assert store.delete_node("Item", "a") is True

    def test_nonexistent_node_false(self):
        store, mock_session = _make_neo4j_store()
        mock_session.run.return_value.single.return_value = {"cnt": 0}
        assert store.delete_node("Item", "missing") is False
