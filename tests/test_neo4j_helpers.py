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

from opencrab.common.graph_identity import (
    GraphReadCapabilityUnavailable,
    canonical_edge_digest,
    canonical_node_digest,
)
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
    def execute_write(callback):
        transaction = MagicMock(name="transaction")
        lock_result = MagicMock(name="lock-result")
        lock_result.single.return_value = {"epoch": 1}

        def run(query, *args, **kwargs):
            if "SET l.lock_epoch" in query:
                return lock_result
            if "SET l.owner_token=null" in query:
                return MagicMock(name="unlock-result")
            return mock_session.run(query, *args, **kwargs)

        transaction.run.side_effect = run
        return callback(transaction)

    mock_session.execute_write.side_effect = execute_write
    mock_session.run.reset_mock()
    store._schema_state = "target"
    return store, mock_driver, mock_session


def _make_unavailable_store() -> Neo4jStore:
    with patch("neo4j.GraphDatabase") as mock_gdb:
        mock_gdb.driver.side_effect = RuntimeError("no route to host")
        store = Neo4jStore("bolt://mock:7687", "neo4j", "pw")
    return store


def _reinitialise_with_edges(store: Neo4jStore, session: MagicMock, edges: list[dict]) -> None:
    node_props = {"id": "n1"}
    node_digest = canonical_node_digest("Person", None, node_props)
    node = {
        "node_id": "n1",
        "node_type": "Person",
        "node_digest": node_digest,
        "props": node_props,
        "labels": ["OpenCrabNode", "Person"],
    }
    required_constraints = [
        {"name": "opencrab_node_id_unique"},
        {"name": "opencrab_write_lock_name_unique"},
        {"name": "opencrab_migration_lock_name_unique"},
    ]

    def run(query: str, **_kwargs):
        if query == "SHOW CONSTRAINTS":
            return required_constraints
        if query == "SHOW INDEXES":
            return []
        if "MATCH (n:OpenCrabNode) RETURN" in query:
            return [node]
        if "MATCH (a)-[r]->(b)" in query:
            return edges
        if "any(label IN labels(n)" in query:
            return []
        if "OpenCrabWriteLock" in query:
            return [{"count": 1}]
        if "OpenCrabMigrationLock" in query:
            return [{"count": 1}]
        raise AssertionError(f"unexpected schema query: {query}")

    session.run.side_effect = run
    store._initialise_schema_state()


# ---------------------------------------------------------------------------
# Normal — cypher construction + parameter passing via mocked session
# ---------------------------------------------------------------------------


class TestNeo4jStoreNormal:
    def test_connect_success_marks_available(self):
        store, mock_driver, mock_session = _make_connected_store()
        assert store.available is True
        assert store.schema_state == "target"

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
        props = {"id": "u1", "name": "Alice", "space": "s1"}
        digest = canonical_node_digest("User", "s1", props)
        mock_session.run.return_value.single.side_effect = [
            None,
            {"props": props, "node_type": "User", "node_digest": digest},
        ]

        result = store.upsert_node("User", "u1", {"name": "Alice"}, space_id="s1")

        assert result == props
        cypher, kwargs = mock_session.run.call_args[0][0], mock_session.run.call_args[1]
        assert "MERGE (n:OpenCrabNode {node_id: $node_id})" in cypher
        assert kwargs["node_id"] == "u1"
        assert kwargs["props"] == props

    def test_upsert_node_omits_space_when_not_given(self):
        store, _driver, mock_session = _make_connected_store()
        props = {"id": "u1"}
        digest = canonical_node_digest("User", None, props)
        mock_session.run.return_value.single.side_effect = [
            None,
            {"props": props, "node_type": "User", "node_digest": digest},
        ]

        store.upsert_node("User", "u1", {})

        kwargs = mock_session.run.call_args[1]
        assert kwargs["props"] == props

    def test_upsert_node_space_id_argument_wins_over_conflicting_props_space(self):
        """Issue #118 codex review [2]: upsert_node now goes through the
        shared _normalize_space (same function _sql_graph_base.py and
        kuzu_graph_store.py use), so all three backends must agree on which
        value wins when a caller passes conflicting ones. Neo4j already did
        this (unconditional overwrite) before the shared helper existed --
        this pins that the refactor preserved it exactly."""
        store, _driver, mock_session = _make_connected_store()
        props = {"id": "u1", "space": "evidence"}
        digest = canonical_node_digest("User", "evidence", props)
        mock_session.run.return_value.single.side_effect = [
            None,
            {"props": props, "node_type": "User", "node_digest": digest},
        ]

        store.upsert_node("User", "u1", {"space": "claim"}, space_id="evidence")

        kwargs = mock_session.run.call_args[1]
        assert kwargs["props"] == props

    def test_get_node_returns_none_when_no_record(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = None

        assert store.get_node("User", "missing") is None

    def test_get_node_builds_match_cypher_with_type_label(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"props": {"id": "u1"}}

        store.get_node("User", "u1")

        cypher = mock_session.run.call_args[0][0]
        assert "MATCH (n:OpenCrabNode {node_id: $id})" in cypher
        assert mock_session.run.call_args[1] == {"id": "u1", "node_type": "User"}

    def test_lookup_node_type_returns_label(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"lbl": "User"}

        assert store.lookup_node_type("u1") == "User"

    def test_lookup_node_type_none_when_not_found(self):
        # A genuine "no match": real Neo4j's single() returns None itself
        # when MATCH finds nothing -- it never hands back a record whose
        # field is None (that shape is the malformed case below, #162 v3
        # codex review: this fixture used to simulate the wrong thing).
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = None

        assert store.lookup_node_type("missing") is None

    def test_lookup_node_type_malformed_raises(self):
        # A row matched but its node_type came back null/empty -- a
        # data-integrity fault, not "not found". Must not be confused with
        # the absent case above (#162).
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"lbl": None}

        with pytest.raises(GraphReadCapabilityUnavailable):
            store.lookup_node_type("weird")

    @pytest.mark.parametrize("bad_label", [42, "has space", "1leadingdigit", "kebab-case"])
    def test_lookup_node_type_truthy_but_invalid_label_raises(self, bad_label):
        # A row matched with a NON-empty node_type that is still not a
        # legal label -- an int, or a string with illegal characters. The
        # bare "not label" check (#162 v1/v2) let this pass through as if
        # it were a real type, and OntologyBuilder.add_edge would forward
        # it to get_node/upsert_edge, which raise a raw TypeError/ValueError
        # there instead of the intended fail-closed graph-unavailable
        # receipt (#162 codex review round 6).
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"lbl": bad_label}

        with pytest.raises(GraphReadCapabilityUnavailable):
            store.lookup_node_type("weird")

    @pytest.mark.parametrize(
        "exc_type", [KeyError, TypeError, AttributeError, IndexError, ValueError, AssertionError]
    )
    def test_lookup_node_type_propagates_programming_errors(self, exc_type):
        # KeyError: RETURN ... AS lbl renamed but the read site not updated.
        # TypeError/AttributeError/IndexError/ValueError/AssertionError:
        # adapter or driver misuse. None of these are "store unavailable" --
        # they must surface as themselves so a bug is visible instead of
        # disguised (#162 codex review round 3/4, full denylist coverage
        # round 5).
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.side_effect = exc_type("boom")

        with pytest.raises(exc_type):
            store.lookup_node_type("u1")

    def test_lookup_node_type_wraps_other_errors_with_cause(self):
        store, _driver, mock_session = _make_connected_store()
        original = RuntimeError("driver connection reset")
        mock_session.run.side_effect = original

        with pytest.raises(GraphReadCapabilityUnavailable) as excinfo:
            store.lookup_node_type("u1")
        assert excinfo.value.__cause__ is original

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
        props = {"from_id": "u1", "relation": "OWNS", "to_id": "p1", "since": 2020}
        mock_session.run.return_value.single.side_effect = [
            {"from_type": "User", "to_type": "Project"},
            None,
            {"props": props},
        ]

        result = store.upsert_edge(
            "User", "u1", "OWNS", "Project", "p1", {"since": 2020}
        )

        assert result is True
        cypher = mock_session.run.call_args[0][0]
        kwargs = mock_session.run.call_args[1]
        assert "MERGE (a)-[r:`OWNS` {edge_key: $edge_key}]->(b)" in cypher
        assert "r.edge_digest=$edge_digest" in cypher
        assert kwargs["from_id"] == "u1"
        assert kwargs["to_id"] == "p1"
        assert kwargs["props"] == props

    def test_upsert_edge_default_created_timestamp_when_no_properties(self):
        store, _driver, mock_session = _make_connected_store()
        props = {"from_id": "u1", "relation": "OWNS", "to_id": "p1"}
        digest = canonical_edge_digest("u1", "OWNS", "p1", "User", "Project", props)
        mock_session.run.return_value.single.side_effect = [
            {"from_type": "User", "to_type": "Project"},
            None,
            {"props": props},
        ]

        store.upsert_edge("User", "u1", "OWNS", "Project", "p1")

        cypher = mock_session.run.call_args[0][0]
        assert "r.edge_digest=$edge_digest" in cypher
        assert mock_session.run.call_args[1]["edge_digest"] == digest

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
        assert "MATCH (n:OpenCrabNode)" in cypher
        assert "n.node_type=$node_type" in cypher

    def test_count_nodes_without_type_filter(self):
        store, _driver, mock_session = _make_connected_store()
        mock_session.run.return_value.single.return_value = {"cnt": 9}

        assert store.count_nodes() == 9
        cypher = mock_session.run.call_args[0][0]
        assert cypher == "MATCH (n:OpenCrabNode) RETURN count(n) AS cnt"

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

        assert store.schema_state == "target"
        mock_session.run.assert_not_called()


class TestNeo4jSchemaInventory:
    @staticmethod
    def _edge_row(store: Neo4jStore, *, edge_key: str | None = None) -> dict:
        props = {"from_id": "n1", "relation": "KNOWS", "to_id": "n1"}
        key = edge_key or store._edge_key("n1", "KNOWS", "n1")
        digest = canonical_edge_digest("n1", "KNOWS", "n1", "Person", "Person", props)
        props = {**props, "edge_key": key, "edge_digest": digest, "from_type": "Person", "to_type": "Person"}
        return {
            "from_id": "n1", "from_type": "Person", "from_labels": ["OpenCrabNode", "Person"],
            "to_id": "n1", "to_type": "Person", "to_labels": ["OpenCrabNode", "Person"],
            "rel_type": "KNOWS", "relation": "KNOWS", "stored_from_id": "n1", "stored_to_id": "n1",
            "edge_key": key, "edge_digest": digest, "props": props,
        }

    def test_duplicate_relationship_identity_fails_closed_at_startup(self):
        store, _driver, session = _make_connected_store()
        edge = self._edge_row(store)

        _reinitialise_with_edges(store, session, [edge, dict(edge)])

        assert store.schema_state == "partial_or_unknown"

    def test_relationship_edge_key_drift_fails_closed_at_startup(self):
        store, _driver, session = _make_connected_store()
        edge = self._edge_row(store, edge_key="wrong-edge-key")

        _reinitialise_with_edges(store, session, [edge])

        assert store.schema_state == "partial_or_unknown"


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

    def test_lookup_node_type_raises_when_unavailable(self):
        # #162: an unavailable store cannot tell "node absent" from "store
        # down" -- it must raise instead of degrading to None, so
        # OntologyBuilder.add_edge can refuse the write instead of guessing
        # a default type.
        store = _make_unavailable_store()
        with pytest.raises(GraphReadCapabilityUnavailable):
            store.lookup_node_type("u1")

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

    def test_upsert_edge_empty_properties_dict_uses_canonical_identity(self):
        store, _driver, mock_session = _make_connected_store()
        props = {"from_id": "u1", "relation": "OWNS", "to_id": "p1"}
        digest = canonical_edge_digest("u1", "OWNS", "p1", "User", "Project", props)
        mock_session.run.return_value.single.side_effect = [
            {"from_type": "User", "to_type": "Project"},
            None,
            {"props": props},
        ]

        store.upsert_edge("User", "u1", "OWNS", "Project", "p1", {})

        cypher = mock_session.run.call_args[0][0]
        assert "r.edge_digest=$edge_digest" in cypher
        assert mock_session.run.call_args[1]["edge_digest"] == digest

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


class TestScopedReadPredicates:
    """#147: the three scoped read methods, whose Cypher no other test runs.

    On a docker (Neo4j) deployment these are the ONLY authorization point
    behind `ontology_list_nodes`, `ontology_list_edges`, `ontology_get_node`,
    `GET /api/nodes` and `GET /api/edges` -- the handlers pass their results
    into the response without re-filtering. `tests/test_pack_neo4j_export.py`
    uses a fake store, so it never executes these bodies either. Mock-session
    assertions are weaker than a live query (they cannot show the Cypher is
    semantically right) but they do pin that the pack clause is applied and
    the parameter bound, which is what silent removal looks like.
    """

    def test_export_nodes_scoped_filters_on_pack_id(self) -> None:
        store, _driver, session = _make_connected_store()
        session.run.return_value = []

        store.export_nodes_scoped(["p1", "p2"], limit=10)

        cypher = session.run.call_args[0][0]
        params = session.run.call_args[1]
        assert "n.pack_id IN $pack_ids" in cypher
        assert params["pack_ids"] == ["p1", "p2"]

    def test_export_edges_scoped_requires_both_endpoints_and_the_edge(self) -> None:
        store, _driver, session = _make_connected_store()
        session.run.return_value = []

        store.export_edges_scoped(["p1"], limit=10)

        cypher = session.run.call_args[0][0]
        # Both endpoints AND the edge's own pack: an edge row carries both
        # endpoints' full properties, so one unreadable endpoint discloses
        # that node. OR-any-endpoint is the pack-export rule, not this one.
        assert "a.pack_id IN $pack_ids" in cypher
        assert "b.pack_id IN $pack_ids" in cypher
        assert "r.pack_id IS NULL OR r.pack_id IN $pack_ids" in cypher

    def test_get_node_by_id_scoped_filters_on_pack_id(self) -> None:
        store, _driver, session = _make_connected_store()
        session.run.return_value.single.return_value = None

        store.get_node_by_id_scoped("n1", ["p1"])

        cypher = session.run.call_args[0][0]
        params = session.run.call_args[1]
        assert "n.pack_id IN $pack_ids" in cypher
        assert params["pack_ids"] == ["p1"]

    def test_empty_scope_never_reaches_the_session(self) -> None:
        store, _driver, session = _make_connected_store()
        session.run.reset_mock()

        assert store.export_nodes_scoped([], limit=10) == []
        assert store.export_edges_scoped([], limit=10) == []
        assert store.count_exported_nodes_scoped([]) == 0
        assert store.get_node_by_id_scoped("n1", []) is None
        session.run.assert_not_called()



class TestScopedRelationLookup:
    """`find_by_relations_scoped`, which `lever_simulate` now relies on alone.

    That handler dropped its Python post-filter when this method arrived, so
    on a docker deployment these three Cypher clauses are the whole of the
    authorization for `ontology_lever_simulate` -- nothing below re-checks.
    Same mock-session strength as `TestScopedReadPredicates`: it cannot show
    the Cypher is semantically right, but it does show each clause is still
    applied and the parameters bound.
    """

    def test_constrains_anchor_endpoint_and_edge(self) -> None:
        store, _driver, session = _make_connected_store()
        session.run.return_value = []

        store.find_by_relations_scoped("lev-1", ["raises"], ["p1"], "out", 20)

        cypher = session.run.call_args[0][0]
        params = session.run.call_args[1]
        assert "a.pack_id IN $pack_ids" in cypher
        assert "b.pack_id IN $pack_ids" in cypher
        assert "r.pack_id IS NULL OR r.pack_id IN $pack_ids" in cypher
        assert params["pack_ids"] == ["p1"]
        assert params["relations"] == ["raises"]
        # The relation-type filter must be in the query, not applied after.
        assert "type(r) IN $relations" in cypher

    def test_empty_scope_or_relations_never_reaches_the_session(self) -> None:
        store, _driver, session = _make_connected_store()
        session.run.reset_mock()

        assert store.find_by_relations_scoped("lev-1", ["raises"], [], "out", 20) == []
        assert store.find_by_relations_scoped("lev-1", [], ["p1"], "out", 20) == []
        session.run.assert_not_called()

    def test_count_exported_nodes_scoped_filters_on_pack_id(self) -> None:
        """`total` is a separate query from the page: an unscoped count leaks
        how much data other users hold even when their rows are withheld."""
        store, _driver, session = _make_connected_store()
        session.run.return_value.single.return_value = None

        store.count_exported_nodes_scoped(["p1"])

        cypher = session.run.call_args[0][0]
        params = session.run.call_args[1]
        assert "n.pack_id IN $pack_ids" in cypher
        assert params["pack_ids"] == ["p1"]

    def test_list_pack_ids_enumerates_edges_too(self) -> None:
        """An edge may carry a pack_id no node has; the startup guard has to
        see it or that pack starts unregistered and its edges vanish.

        Asserts on the RESULT, not just on the query text: a mutation that
        keeps the edge query but drops its rows would satisfy a text-only
        check while the guard went blind to edge-only packs.
        """
        store, _driver, session = _make_connected_store()

        def _rows(cypher, **_kw):
            if "-[r]->" in cypher:
                return [{"pid": "p-edge-only"}]
            return [{"pid": "p-node"}]

        session.run.side_effect = _rows

        assert store.list_pack_ids() == {"p-node", "p-edge-only"}

        queries = [c[0][0] for c in session.run.call_args_list]
        assert any("MATCH (n:OpenCrabNode)" in q and "n.pack_id" in q for q in queries)
        assert any("-[r]->" in q and "r.pack_id" in q for q in queries)
