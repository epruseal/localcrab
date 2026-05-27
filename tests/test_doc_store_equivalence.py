"""
동등성 테스트 — LocalSQLDocStore(SQLite)와 LocalDocStore(JSON)가
동일한 인터페이스 행동을 보이는지 검증한다.

두 스토어를 pytest.mark.parametrize로 동일한 테스트에 통과시킨다.

알려진 차이점 (의도된 동작):
  - LocalDocStore.upsert_source()는 text를 4096자로 잘린다.
    → source text 동등성 테스트는 짧은 텍스트로 제한한다.
  - LocalDocStore.log_event()는 None 반환, LocalSQLDocStore는 event_id(str) 반환.
    → 반환값 대신 저장된 이벤트 내용을 검증한다.
  - LocalDocStore의 audit_log 항목에는 'event_id' 키가 없다.
    → event_id 키 검사는 동등성 테스트에서 제외한다.
"""

from __future__ import annotations

import time

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sql_store(tmp_path):
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    return LocalSQLDocStore(str(tmp_path / "doc_store.db"))


@pytest.fixture
def json_store(tmp_path):
    from opencrab.stores.local_doc_store import LocalDocStore

    return LocalDocStore(str(tmp_path / "docs"))


@pytest.fixture(params=["sql_store", "json_store"])
def store(request):
    return request.getfixturevalue(request.param)


# ---------------------------------------------------------------------------
# 기본 upsert/get 동등성
# ---------------------------------------------------------------------------


class TestUpsertGetEquivalence:
    def test_upsert_get_node_equivalence(self, store):
        """upsert 후 get_node_doc() 결과가 올바른 값을 포함해야 한다."""
        store.upsert_node_doc("sp", "Person", "alice", {"name": "Alice", "age": 30})
        doc = store.get_node_doc("sp", "alice")

        assert doc is not None
        assert doc["space"] == "sp"
        assert doc["node_id"] == "alice"
        assert doc["node_type"] == "Person"
        assert doc["properties"]["name"] == "Alice"
        assert doc["properties"]["age"] == 30
        assert "updated_at" in doc

    def test_upsert_overwrite_equivalence(self, store):
        """두 번 upsert 시 최신값이 반영되어야 한다."""
        store.upsert_node_doc("sp", "T", "node1", {"v": 1})
        store.upsert_node_doc("sp", "T", "node1", {"v": 2})
        doc = store.get_node_doc("sp", "node1")

        assert doc is not None
        assert doc["properties"]["v"] == 2

    def test_get_missing_node_returns_none(self, store):
        """없는 노드를 조회하면 None을 반환해야 한다."""
        result = store.get_node_doc("sp", "does_not_exist")
        assert result is None

    def test_node_id_preserved(self, store):
        """node_id 필드가 저장 후에도 그대로 보존되어야 한다."""
        store.upsert_node_doc("sp", "T", "my_node_123", {})
        doc = store.get_node_doc("sp", "my_node_123")

        assert doc is not None
        assert doc["node_id"] == "my_node_123"

    def test_properties_preserved(self, store):
        """중첩된 properties dict가 그대로 보존되어야 한다."""
        props = {"key": "value", "num": 42, "list": [1, 2, 3], "nested": {"a": "b"}}
        store.upsert_node_doc("sp", "T", "n1", props)
        doc = store.get_node_doc("sp", "n1")

        assert doc is not None
        assert doc["properties"] == props


# ---------------------------------------------------------------------------
# list_nodes 동등성
# ---------------------------------------------------------------------------


class TestListNodesEquivalence:
    def test_list_nodes_no_filter(self, store):
        """space 필터 없이 전체 노드를 조회해야 한다."""
        store.upsert_node_doc("s1", "T", "a", {})
        store.upsert_node_doc("s1", "T", "b", {})
        store.upsert_node_doc("s2", "T", "c", {})

        result = store.list_nodes(limit=100)
        assert len(result) == 3

    def test_list_nodes_space_filter(self, store):
        """space 파라미터로 필터링이 올바르게 작동해야 한다."""
        store.upsert_node_doc("alpha", "T", "a1", {})
        store.upsert_node_doc("alpha", "T", "a2", {})
        store.upsert_node_doc("beta", "T", "b1", {})

        result = store.list_nodes(space="alpha", limit=100)
        assert len(result) == 2
        assert all(r["space"] == "alpha" for r in result)

    def test_list_nodes_limit(self, store):
        """limit 파라미터가 결과 수를 제한해야 한다."""
        for i in range(10):
            store.upsert_node_doc("sp", "T", f"node_{i}", {})

        result = store.list_nodes(limit=5)
        assert len(result) == 5

    def test_list_nodes_after_delete(self, store):
        """삭제 후 해당 노드가 목록에서 제거되어야 한다."""
        store.upsert_node_doc("sp", "T", "keep", {})
        store.upsert_node_doc("sp", "T", "remove", {})
        store.delete_node_doc("sp", "remove")

        result = store.list_nodes(limit=100)
        node_ids = [r["node_id"] for r in result]
        assert "keep" in node_ids
        assert "remove" not in node_ids


# ---------------------------------------------------------------------------
# delete 동등성
# ---------------------------------------------------------------------------


class TestDeleteEquivalence:
    def test_delete_existing_returns_true(self, store):
        """존재하는 노드를 삭제하면 True를 반환해야 한다."""
        store.upsert_node_doc("sp", "T", "to_delete", {})
        result = store.delete_node_doc("sp", "to_delete")
        assert result is True

    def test_delete_missing_returns_false(self, store):
        """존재하지 않는 노드를 삭제하면 False를 반환해야 한다."""
        result = store.delete_node_doc("sp", "nonexistent")
        assert result is False


# ---------------------------------------------------------------------------
# source 동등성
# ---------------------------------------------------------------------------


class TestSourceEquivalence:
    def test_upsert_get_source_equivalence(self, store):
        """source upsert 후 get_source()로 동일한 데이터를 읽어야 한다.

        NOTE: LocalDocStore는 text를 4096자로 잘리므로 짧은 텍스트를 사용한다.
        """
        short_text = "Hello, world!"
        store.upsert_source("src1", short_text, {"user": "alice"})
        src = store.get_source("src1")

        assert src is not None
        assert src["source_id"] == "src1"
        assert src["text"] == short_text
        assert src["metadata"]["user"] == "alice"
        assert "ingested_at" in src

    def test_list_sources_limit(self, store):
        """list_sources()의 limit 파라미터가 올바르게 작동해야 한다."""
        for i in range(5):
            store.upsert_source(f"s{i}", f"text {i}", {})

        result = store.list_sources(limit=3)
        assert len(result) == 3

    def test_get_missing_source_returns_none(self, store):
        """존재하지 않는 source를 조회하면 None을 반환해야 한다."""
        result = store.get_source("nonexistent_source")
        assert result is None


# ---------------------------------------------------------------------------
# audit log 동등성
# ---------------------------------------------------------------------------


class TestAuditLogEquivalence:
    def test_log_event_stored(self, store):
        """log_event() 호출 후 get_audit_log()로 이벤트를 조회할 수 있어야 한다."""
        store.log_event("create", "u1", {"node": "n1"})
        log = store.get_audit_log(limit=10)

        assert len(log) == 1
        entry = log[0]
        assert entry["event_type"] == "create"
        assert entry["subject_id"] == "u1"
        assert entry["details"]["node"] == "n1"
        assert "timestamp" in entry

    def test_audit_log_sorted_desc(self, store):
        """get_audit_log()는 timestamp 내림차순으로 정렬되어야 한다."""
        for i in range(3):
            store.log_event("ev", None, {"i": i})
            time.sleep(0.01)  # 타임스탬프가 구분되도록

        log = store.get_audit_log(limit=10)
        timestamps = [e["timestamp"] for e in log]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_audit_log_filter_by_type(self, store):
        """event_type 필터링이 올바르게 작동해야 한다."""
        store.log_event("login", "u1", {})
        store.log_event("logout", "u1", {})
        store.log_event("login", "u2", {})

        result = store.get_audit_log(limit=100, event_type="login")
        assert len(result) == 2
        assert all(e["event_type"] == "login" for e in result)


# ---------------------------------------------------------------------------
# stats 동등성
# ---------------------------------------------------------------------------


class TestStatsEquivalence:
    def test_collection_stats_counts_nodes(self, store):
        """collection_stats()가 upsert된 노드 수를 정확히 반환해야 한다."""
        store.upsert_node_doc("sp", "T", "n1", {})
        store.upsert_node_doc("sp", "T", "n2", {})
        store.upsert_node_doc("sp", "T", "n3", {})

        stats = store.collection_stats()
        assert stats["nodes"] == 3

    def test_collection_stats_counts_sources(self, store):
        """collection_stats()가 upsert된 source 수를 정확히 반환해야 한다."""
        store.upsert_source("s1", "text", {})
        store.upsert_source("s2", "text", {})

        stats = store.collection_stats()
        assert stats["sources"] == 2

    def test_collection_stats_counts_audit_log(self, store):
        """collection_stats()가 audit log 항목 수를 정확히 반환해야 한다."""
        store.log_event("ev1", None, {})
        store.log_event("ev2", None, {})

        stats = store.collection_stats()
        assert stats["audit_log"] == 2

    def test_collection_stats_empty_store(self, store):
        """빈 스토어의 collection_stats()는 0을 반환해야 한다."""
        stats = store.collection_stats()
        assert stats["nodes"] == 0
        assert stats["sources"] == 0
        assert stats["audit_log"] == 0
