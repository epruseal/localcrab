"""
Contract tests for Neo4jStore helper/query-construction paths using a
mocked neo4j driver/session — no live Neo4j.

``Neo4jStore._build_neighbors_cypher`` (the pack-filter cypher builder) is
already covered by tests/test_graph_pack_filter.py (T11 tests) — this file
does not duplicate those cases. It focuses on: the mocked session/driver
plumbing (run_cypher parameter passing, upsert/get/delete node & edge cypher
shape, find_path shape, count_nodes/ping), the "unavailable" raise contract,
and driver-exception propagation. Cross-backend path semantics for
find_path are explicitly out of scope for this stage.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencrab.stores.neo4j_store import Neo4jStore


def _make_connected_store() -> tuple[Neo4jStore, MagicMock, MagicMock]:
    """Build a Neo4jStore whose ``_connect`` succeeds against a mocked driver."""
    mock_session = MagicMock(name="session")
    mock_driver = MagicMock(name="driver")
    mock_driver.session.return_value.__enter__.return_value = mock_session
    mock_driver.session.return_value.__exit__.return_value = False

    with patch("neo4j.GraphDatabase") as mock_gdb:
        mock_gdb.driver.return_value = mock_driver
        store = Neo4jStore("bolt://mock:7687", "neo4j", "pw")
    return store, mock_driver, mock_session


def _make_unavailable_store() -> Neo4jStore:
    with patch("neo4j.GraphDatabase") as mock_gdb:
        mock_gdb.driver.side_effect = RuntimeError("no route to host")
        store = Neo4jStore("bolt://mock:7687", "neo4j", "pw")
    return store


# ---------------------------------------------------------------------------
# Normal — cypher construction + parameter passing via mocked session
# ---------------------------------------------------------------------------


class TestNeo4jStoreNormal:
    def test_connect_success_marks_available(self):
        store, mock_driver, mock_session = _make_connected_store()
        assert store.available is True
        mock_session.run.assert_called_with("RETURN 1")

    def test_ping_true_when_available(self):
        store, _driver, _session = _make_connected_store()
        assert store.ping() is True

    def test_run_cypher_passes_params_and_maps_records(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value = [{"a": 1}, {"a": 2}]

        result = store.run_cypher("MATCH (n) WHERE n.id = $id RETURN n", {"id": "x"})

        mock_session.run.assert_called_with(
            "MATCH (n) WHERE n.id = $id RETURN n", id="x"
        )
        assert result == [{"a": 1}, {"a": 2}]

    def test_run_cypher_defaults_params_to_empty_dict(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value = []

        store.run_cypher("RETURN 1")

        mock_session.run.assert_called_with("RETURN 1")

    def test_upsert_node_merges_and_returns_properties(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {
            "props": {"id": "u1", "name": "Alice"}
        }

        result = store.upsert_node("User", "u1", {"name": "Alice"}, space_id="s1")

        assert result == {"id": "u1", "name": "Alice"}
        cypher, kwargs = mock_session.run.call_args[0][0], mock_session.run.call_args[1]
        assert "MERGE (n:OpenCrabNode:User {id: $id})" in cypher
        assert kwargs["id"] == "u1"
        assert kwargs["name"] == "Alice"
        assert kwargs["space"] == "s1"

    def test_upsert_node_omits_space_when_not_given(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"props": {"id": "u1"}}

        store.upsert_node("User", "u1", {})

        kwargs = mock_session.run.call_args[1]
        assert "space" not in kwargs

    def test_upsert_node_space_id_argument_wins_over_conflicting_props_space(self):
        """Issue #118 codex review [2]: upsert_node now goes through the
        shared _normalize_space (same function _sql_graph_base.py and
        kuzu_graph_store.py use), so all three backends must agree on which
        value wins when a caller passes conflicting ones. Neo4j already did
        this (unconditional overwrite) before the shared helper existed --
        this pins that the refactor preserved it exactly."""
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"props": {"id": "u1"}}

        store.upsert_node("User", "u1", {"space": "claim"}, space_id="evidence")

        kwargs = mock_session.run.call_args[1]
        assert kwargs["space"] == "evidence"

    def test_get_node_returns_none_when_no_record(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = None

        assert store.get_node("User", "missing") is None

    def test_get_node_builds_match_cypher_with_type_label(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"props": {"id": "u1"}}

        store.get_node("User", "u1")

        cypher = mock_session.run.call_args[0][0]
        assert "MATCH (n:User {id: $id})" in cypher
        assert mock_session.run.call_args[1] == {"id": "u1"}

    def test_lookup_node_type_returns_label(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"lbl": "User"}

        assert store.lookup_node_type("u1") == "User"

    def test_lookup_node_type_none_when_not_found(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"lbl": None}

        assert store.lookup_node_type("missing") is None

    def test_delete_node_true_when_count_positive(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"cnt": 1}

        assert store.delete_node("User", "u1") is True
        cypher = mock_session.run.call_args[0][0]
        assert "DETACH DELETE n" in cypher

    def test_delete_node_false_when_count_zero(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"cnt": 0}

        assert store.delete_node("User", "missing") is False

    def test_upsert_edge_builds_merge_with_properties(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"r": {}}

        result = store.upsert_edge(
            "User", "u1", "OWNS", "Project", "p1", {"since": 2020}
        )

        assert result is True
        cypher = mock_session.run.call_args[0][0]
        kwargs = mock_session.run.call_args[1]
        assert "MERGE (a)-[r:OWNS]->(b)" in cypher
        assert "r.since = $since" in cypher
        assert kwargs == {"from_id": "u1", "to_id": "p1", "since": 2020}

    def test_upsert_edge_default_created_timestamp_when_no_properties(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"r": {}}

        store.upsert_edge("User", "u1", "OWNS", "Project", "p1")

        cypher = mock_session.run.call_args[0][0]
        assert "r.created = timestamp()" in cypher

    def test_upsert_edge_false_when_no_record(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = None

        assert store.upsert_edge("User", "u1", "OWNS", "Project", "p1") is False

    def test_find_path_maps_nodes_and_relations(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {
            "node_props": [{"id": "a"}, {"id": "b"}],
            "rel_types": ["OWNS"],
        }

        result = store.find_path("a", "b", max_depth=3)

        assert result == [
            {"node": {"id": "a"}, "relation": "OWNS"},
            {"node": {"id": "b"}, "relation": ""},
        ]
        cypher = mock_session.run.call_args[0][0]
        assert "*1..3" in cypher

    def test_find_path_empty_when_no_record(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = None

        assert store.find_path("a", "b") == []

    def test_count_nodes_with_type_filter(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"cnt": 5}

        assert store.count_nodes("User") == 5
        cypher = mock_session.run.call_args[0][0]
        assert "MATCH (n:User)" in cypher

    def test_count_nodes_without_type_filter(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"cnt": 9}

        assert store.count_nodes() == 9
        cypher = mock_session.run.call_args[0][0]
        assert cypher == "MATCH (n) RETURN count(n) AS cnt"

    def test_find_neighbors_maps_records_via_build_neighbors_cypher(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value = [
            {
                "props": {"id": "b"},
                "labels": ["Claim"],
                "relationship_types": ["RELATED_TO"],
                "depth": 1,
            }
        ]

        result = store.find_neighbors("a", direction="out", depth=1, limit=10)

        assert result == [
            {
                "properties": {"id": "b"},
                "labels": ["Claim"],
                "relationship_types": ["RELATED_TO"],
                "relation_type": "RELATED_TO",
                "depth": 1,
            }
        ]
        # Parameter passing must match _build_neighbors_cypher's own contract.
        kwargs = mock_session.run.call_args[1]
        assert kwargs == {"id": "a", "limit": 10}

    def test_ensure_constraints_runs_one_per_node_type(self):
        store, _driver, mock_session = _make_connected_store()
        with patch(
            "opencrab.grammar.manifest.all_node_types",
            return_value=["User", "Project"],
        ):
            store.ensure_constraints()

        calls = [c.args[0] for c in mock_session.run.call_args_list]
        assert any("FOR (n:User)" in c for c in calls)
        assert any("FOR (n:Project)" in c for c in calls)


# ---------------------------------------------------------------------------
# Error — unavailable raise contract + driver exception propagation
# ---------------------------------------------------------------------------


class TestNeo4jStoreErrors:
    def test_connect_failure_leaves_store_unavailable(self):
        store = _make_unavailable_store()
        assert store.available is False

    def test_ping_false_when_unavailable(self):
        store = _make_unavailable_store()
        assert store.ping() is False

    @pytest.mark.parametrize(
        "call",
        [
            lambda s: s.upsert_node("User", "u1", {}),
            lambda s: s.get_node("User", "u1"),
            lambda s: s.delete_node("User", "u1"),
            lambda s: s.upsert_edge("User", "u1", "OWNS", "Project", "p1"),
            lambda s: s.run_cypher("RETURN 1"),
            lambda s: s.find_neighbors("u1"),
            lambda s: s.find_path("a", "b"),
            lambda s: s.count_nodes(),
        ],
    )
    def test_raises_runtime_error_when_unavailable(self, call):
        store = _make_unavailable_store()
        with pytest.raises(RuntimeError, match="not available"):
            call(store)

    def test_lookup_node_type_returns_none_when_unavailable(self):
        # Deliberately lenient: used as a best-effort resolution helper by
        # OntologyBuilder, so it degrades to None instead of raising.
        store = _make_unavailable_store()
        assert store.lookup_node_type("u1") is None

    def test_ensure_constraints_warns_and_returns_when_unavailable(self):
        # Deliberately lenient bootstrap operation: warns instead of raising.
        store = _make_unavailable_store()
        store.ensure_constraints()  # should not raise

    def test_driver_exception_propagates_from_run_cypher(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.side_effect = RuntimeError("connection reset")

        with pytest.raises(RuntimeError, match="connection reset"):
            store.run_cypher("RETURN 1")

    def test_driver_exception_propagates_from_upsert_node(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.side_effect = RuntimeError("connection reset")

        with pytest.raises(RuntimeError, match="connection reset"):
            store.upsert_node("User", "u1", {})

    def test_ping_false_when_session_raises(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.side_effect = RuntimeError("boom")

        assert store.ping() is False


# ---------------------------------------------------------------------------
# Edge — empty relations, depth boundary values
# ---------------------------------------------------------------------------


class TestNeo4jStoreEdgeCases:
    def test_find_neighbors_empty_result_when_no_matches(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value = []

        assert store.find_neighbors("a") == []

    def test_upsert_edge_empty_properties_dict_uses_default_timestamp(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"r": {}}

        store.upsert_edge("User", "u1", "OWNS", "Project", "p1", {})

        cypher = mock_session.run.call_args[0][0]
        assert "r.created = timestamp()" in cypher

    def test_build_neighbors_cypher_depth_zero_boundary(self):
        # depth=0 is a boundary not covered by test_graph_pack_filter.py's
        # T11 suite (which exercises depth=1, 2, "3", and injection cases).
        cypher, params = Neo4jStore._build_neighbors_cypher(
            node_id="x", direction="both", depth=0, limit=5,
            pack_ids=None, include_unpackaged=False,
        )
        assert "*1..0]" in cypher
        assert params == {"id": "x", "limit": 5}

    def test_build_neighbors_cypher_negative_depth_not_validated(self):
        # NOTE: only non-numeric depth strings are rejected (via int()
        # raising ValueError, covered by test_graph_pack_filter.py's
        # injection test). A negative int passes int() cleanly and is
        # embedded as-is, producing a range like "*1..-1]" that Neo4j
        # itself would reject at query time. Pinning the actual (currently
        # unguarded) behavior here rather than an assumed validation.
        cypher, params = Neo4jStore._build_neighbors_cypher(
            node_id="x", direction="both", depth=-1, limit=5,
            pack_ids=None, include_unpackaged=False,
        )
        assert "*1..-1]" in cypher
        assert params == {"id": "x", "limit": 5}

    def test_run_cypher_empty_result_list(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value = []

        assert store.run_cypher("MATCH (n) RETURN n") == []


def test_find_neighbors_empty_scope_returns_nothing_without_querying() -> None:
    """#147: on Neo4j this short-circuit is the ONLY thing enforcing an
    empty read scope.

    The SQL and Kuzu backends filter every visited node again in Python
    (``_node_passes``), so removing their short-circuit still yields no
    rows. Neo4j returns Cypher records straight through, and
    ``_build_neighbors_cypher`` folds an empty ``pack_ids`` into "no pack
    clause at all" -- so without this guard a principal who may read no
    pack would traverse the whole graph. Asserting the session is never
    opened pins the guard itself rather than a downstream effect that does
    not exist here.
    """
    store, _driver, session = _make_connected_store()
    # _make_connected_store's own connectivity probe already ran a query.
    session.run.reset_mock()

    assert store.find_neighbors("n1", pack_ids=[]) == []
    session.run.assert_not_called()


def test_build_neighbors_cypher_has_no_pack_clause_for_an_empty_scope() -> None:
    """Why the guard above cannot be dropped: the builder itself cannot
    express "match nothing". This is the failure mode, pinned so nobody
    concludes the builder is defensive on its own."""
    store, _driver, _session = _make_connected_store()

    cypher, params = store._build_neighbors_cypher(
        "n1", "both", 1, 5, pack_ids=[], include_unpackaged=False, spaces=None
    )
    assert "pack_id" not in cypher
    assert "pack_ids" not in params

