"""
LocalGraphStore 확장 메서드 테스트.
run_cypher() no-op을 대체하는 SQLite 네이티브 메서드들의 동작 검증.
"""
import pytest
from opencrab.stores.local_graph_store import LocalGraphStore


@pytest.fixture
def store(tmp_path):
    s = LocalGraphStore(str(tmp_path / "test.db"))
    yield s
    s.close()


class TestGetNodeById:
    def test_returns_node_with_type(self, store):
        store.upsert_node("Lever", "lev-1", {"name": "test"})
        result = store.get_node_by_id("lev-1")
        assert result is not None
        assert result["node_type"] == "Lever"
        assert result["id"] == "lev-1"

    def test_returns_none_for_missing(self, store):
        assert store.get_node_by_id("not-exist") is None


class TestFindByRelations:
    def test_outgoing_relation_filter(self, store):
        store.upsert_node("Lever", "lev-1", {})
        store.upsert_node("Outcome", "out-1", {})
        store.upsert_node("Outcome", "out-2", {})
        store.upsert_edge("Lever", "lev-1", "raises", "Outcome", "out-1")
        store.upsert_edge("Lever", "lev-1", "affects", "Outcome", "out-2")

        result = store.find_by_relations("lev-1", ["raises"], direction="out")
        assert len(result) == 1
        assert result[0]["relation_type"] == "raises"

    def test_empty_relations_returns_empty(self, store):
        assert store.find_by_relations("lev-1", [], direction="out") == []

    def test_incoming_direction(self, store):
        store.upsert_node("A", "a1", {})
        store.upsert_node("B", "b1", {})
        store.upsert_edge("A", "a1", "links", "B", "b1")

        result = store.find_by_relations("b1", ["links"], direction="in")
        assert len(result) == 1
        assert result[0]["properties"]["id"] == "a1"


class TestFindPath:
    def test_direct_path(self, store):
        store.upsert_node("A", "n1", {})
        store.upsert_node("B", "n2", {})
        store.upsert_edge("A", "n1", "connects", "B", "n2")

        path = store.find_path("n1", "n2")
        assert len(path) >= 1
        assert path[-1]["node"]["id"] == "n2"

    def test_no_path_returns_empty(self, store):
        store.upsert_node("A", "isolated1", {})
        store.upsert_node("B", "isolated2", {})
        path = store.find_path("isolated1", "isolated2")
        assert path == []


class TestExportNodesEdges:
    def test_export_nodes_all(self, store):
        store.upsert_node("Person", "p1", {"name": "Alice"})
        store.upsert_node("Person", "p2", {"name": "Bob"})

        rows = store.export_nodes()
        assert len(rows) == 2
        assert all("props" in r and "labels" in r for r in rows)

    def test_export_nodes_pack_filter(self, store):
        store.upsert_node("T", "n1", {"pack_id": "pack-A"})
        store.upsert_node("T", "n2", {"pack_id": "pack-B"})

        rows = store.export_nodes(pack_id="pack-A")
        assert len(rows) == 1
        assert rows[0]["props"]["pack_id"] == "pack-A"

    def test_export_edges_all(self, store):
        store.upsert_node("A", "a1", {})
        store.upsert_node("B", "b1", {})
        store.upsert_edge("A", "a1", "rel", "B", "b1")

        rows = store.export_edges()
        assert len(rows) == 1
        assert rows[0]["relation"] == "rel"
