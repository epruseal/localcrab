"""검색 결과 canonical ID 보강 계약.

핵심 불변식 두 가지:
  1. exact 조회만 한다. 못 찾으면 unresolved로 표시하고 유사 id로 치환하지 않는다.
  2. 기존 키(node_id/metadata/graph_context)는 값도 형태도 건드리지 않는다.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencrab.services.canonical_ids import enrich


class FakeGraph:
    """node_id -> props 매핑만 갖는 최소 store 스텁."""

    def __init__(self, nodes: dict[str, dict], available: bool = True):
        self.available = available
        self._nodes = nodes
        self.calls: list[str] = []

    def get_node_by_id(self, node_id):
        self.calls.append(node_id)
        return self._nodes.get(node_id)


TEXT_UNIT = {
    "id": "claude/tdm/abc",
    "pack_id": "claude",
    "node_type": "TextUnit",
    "space": "evidence",
    "text": "본문",
}
DOCUMENT = {
    "id": "resource:session:1",
    "pack_id": "openclaw",
    "node_type": "Document",
    "space": "resource",
}


# ---------------------------------------------------------------------------
# 노드 canonical
# ---------------------------------------------------------------------------


def test_existing_node_resolves_to_store_values():
    graph = FakeGraph({"claude/tdm/abc": TEXT_UNIT})
    results = [{"source": "vector", "node_id": "claude/tdm/abc", "metadata": {}}]
    enrich(graph, results)
    assert results[0]["canonical"] == {
        "resolved": True,
        "node_id": "claude/tdm/abc",
        "node_type": "TextUnit",
        "space": "evidence",
        "pack_id": "claude",
        "document_id": None,
        "unresolved_fields": ["document_id"],
    }


def test_missing_node_is_unresolved_not_substituted():
    """store에 유사한 id가 있어도 절대 치환하지 않는다."""
    graph = FakeGraph({"claude/tdm/abc": TEXT_UNIT})
    results = [{"source": "vector", "node_id": "claude/tdm/ab", "metadata": {}}]
    enrich(graph, results)
    assert results[0]["canonical"] == {
        "resolved": False,
        "reason": "node_not_found",
        "requested_node_id": "claude/tdm/ab",
    }
    assert results[0]["node_id"] == "claude/tdm/ab"
    # 조회는 요청된 id 하나뿐 — 유사 id 탐색을 하지 않는다.
    assert graph.calls == ["claude/tdm/ab"]


@pytest.mark.parametrize("empty", [None, ""])
def test_missing_node_id_is_reported(empty):
    graph = FakeGraph({})
    results = [{"source": "bm25", "node_id": empty}]
    enrich(graph, results)
    assert results[0]["canonical"] == {"resolved": False, "reason": "missing_node_id"}
    assert graph.calls == []


def test_document_id_comes_from_props_only():
    node = {**TEXT_UNIT, "source_id": "resource:session:1", "source": "/tmp/x.md"}
    graph = FakeGraph({"claude/tdm/abc": node})
    results = [{"node_id": "claude/tdm/abc"}]
    enrich(graph, results)
    canonical = results[0]["canonical"]
    assert canonical["document_id"] == "resource:session:1"
    assert "unresolved_fields" not in canonical


def test_file_path_source_is_not_promoted_to_document_id():
    node = {**TEXT_UNIT, "source": "/home/asdf/docs/x.md"}
    graph = FakeGraph({"claude/tdm/abc": node})
    results = [{"node_id": "claude/tdm/abc"}]
    enrich(graph, results)
    assert results[0]["canonical"]["document_id"] is None
    assert results[0]["canonical"]["unresolved_fields"] == ["document_id"]


def test_missing_pack_id_is_flagged():
    node = {"id": "legacy", "node_type": "Concept", "space": "concept"}
    graph = FakeGraph({"legacy": node})
    results = [{"node_id": "legacy"}]
    enrich(graph, results)
    assert results[0]["canonical"]["pack_id"] is None
    assert "pack_id" in results[0]["canonical"]["unresolved_fields"]


# ---------------------------------------------------------------------------
# edge canonical
# ---------------------------------------------------------------------------


def _graph_result(depth, endpoints=None, anchor="anchor:1"):
    context = {
        "anchor_id": anchor,
        "labels": ["TextUnit"],
        "relation_type": "contains",
        "relationship_types": ["contains"],
        "depth": depth,
    }
    if endpoints:
        context["edge_endpoints"] = endpoints
    return {"source": "graph", "node_id": "claude/tdm/abc", "graph_context": context}


def test_edge_endpoints_resolve_source_relation_target():
    graph = FakeGraph({"claude/tdm/abc": TEXT_UNIT, "resource:session:1": DOCUMENT})
    item = _graph_result(
        1, {"from_id": "resource:session:1", "to_id": "claude/tdm/abc"}
    )
    enrich(graph, [item])
    edge = item["graph_context"]["edge"]
    assert edge["resolved"] is True
    assert edge["relation"] == "contains"
    assert edge["source"]["node_id"] == "resource:session:1"
    assert edge["source"]["node_type"] == "Document"
    assert edge["target"]["node_id"] == "claude/tdm/abc"
    assert edge["target"]["pack_id"] == "claude"


def test_edge_at_depth_two_does_not_use_anchor_as_source():
    """depth 2에서는 anchor가 edge의 source가 아니다."""
    graph = FakeGraph({"claude/tdm/abc": TEXT_UNIT, "resource:session:1": DOCUMENT})
    item = _graph_result(
        2,
        {"from_id": "resource:session:1", "to_id": "claude/tdm/abc"},
        anchor="some:other:anchor",
    )
    enrich(graph, [item])
    edge = item["graph_context"]["edge"]
    assert edge["source"]["node_id"] == "resource:session:1"
    assert edge["source"]["node_id"] != item["graph_context"]["anchor_id"]


def test_edge_without_store_endpoints_is_unresolved():
    graph = FakeGraph({"claude/tdm/abc": TEXT_UNIT})
    item = _graph_result(1)
    enrich(graph, [item])
    assert item["graph_context"]["edge"] == {
        "resolved": False,
        "reason": "edge_endpoints_unavailable",
    }


def test_edge_with_dangling_endpoint_is_unresolved():
    graph = FakeGraph({"claude/tdm/abc": TEXT_UNIT})
    item = _graph_result(1, {"from_id": "ghost:1", "to_id": "claude/tdm/abc"})
    enrich(graph, [item])
    edge = item["graph_context"]["edge"]
    assert edge["resolved"] is False
    assert edge["reason"] == "endpoint_not_found"
    assert edge["source_id"] == "ghost:1"


def test_resolve_edges_false_skips_edge_block():
    graph = FakeGraph({"claude/tdm/abc": TEXT_UNIT})
    item = _graph_result(1, {"from_id": "claude/tdm/abc", "to_id": "claude/tdm/abc"})
    enrich(graph, [item], resolve_edges=False)
    assert "edge" not in item["graph_context"]
    assert item["canonical"]["resolved"] is True


# ---------------------------------------------------------------------------
# 호환/성능/degrade
# ---------------------------------------------------------------------------


def test_existing_keys_are_untouched():
    graph = FakeGraph({"claude/tdm/abc": TEXT_UNIT})
    item = _graph_result(1, {"from_id": "claude/tdm/abc", "to_id": "claude/tdm/abc"})
    before = {
        "source": item["source"],
        "node_id": item["node_id"],
        "anchor_id": item["graph_context"]["anchor_id"],
        "relation_type": item["graph_context"]["relation_type"],
        "depth": item["graph_context"]["depth"],
    }
    enrich(graph, [item])
    assert item["source"] == before["source"]
    assert item["node_id"] == before["node_id"]
    assert item["graph_context"]["anchor_id"] == before["anchor_id"]
    assert item["graph_context"]["relation_type"] == before["relation_type"]
    assert item["graph_context"]["depth"] == before["depth"]


def test_lookups_are_memoised_per_call():
    graph = FakeGraph({"claude/tdm/abc": TEXT_UNIT, "resource:session:1": DOCUMENT})
    items = [
        _graph_result(1, {"from_id": "resource:session:1", "to_id": "claude/tdm/abc"})
        for _ in range(5)
    ]
    enrich(graph, items)
    assert sorted(set(graph.calls)) == ["claude/tdm/abc", "resource:session:1"]
    assert len(graph.calls) == 2


def test_unavailable_graph_is_a_noop():
    graph = FakeGraph({}, available=False)
    results = [{"node_id": "claude/tdm/abc"}]
    enrich(graph, results)
    assert results == [{"node_id": "claude/tdm/abc"}]
    assert graph.calls == []


def test_store_exception_degrades_to_unresolved():
    graph = MagicMock()
    graph.available = True
    graph.get_node_by_id.side_effect = RuntimeError("store down")
    results = [{"node_id": "claude/tdm/abc"}]
    enrich(graph, results)
    assert results[0]["canonical"]["reason"] == "node_not_found"


# ---------------------------------------------------------------------------
# 실 store 연동: find_neighbors가 edge endpoint를 실제로 준다
# ---------------------------------------------------------------------------


def test_local_store_find_neighbors_reports_edge_endpoints(tmp_path: Path):
    from opencrab.stores.local_graph_store import LocalGraphStore

    store = LocalGraphStore(str(tmp_path / "graph.db"))
    try:
        store.upsert_node("Document", "doc1", {"pack_id": "p"}, space_id="resource")
        store.upsert_node("TextUnit", "tu1", {"pack_id": "p"}, space_id="evidence")
        store.upsert_node("Claim", "claim1", {"pack_id": "p"}, space_id="concept")
        store.upsert_edge("Document", "doc1", "contains", "TextUnit", "tu1")
        store.upsert_edge("TextUnit", "tu1", "supports", "Claim", "claim1")

        hops = store.find_neighbors("tu1", direction="both", depth=1, limit=10)
        by_id = {h["properties"]["id"]: h for h in hops}

        # in-edge: 이웃이 source, anchor가 target
        assert by_id["doc1"]["from_id"] == "doc1"
        assert by_id["doc1"]["to_id"] == "tu1"
        # out-edge: anchor가 source
        assert by_id["claim1"]["from_id"] == "tu1"
        assert by_id["claim1"]["to_id"] == "claim1"

        # depth 2: anchor(doc1)는 두 번째 hop edge의 endpoint가 아니다
        deep = store.find_neighbors("doc1", direction="both", depth=2, limit=10)
        hop2 = next(h for h in deep if h["properties"]["id"] == "claim1")
        assert hop2["from_id"] == "tu1"
        assert hop2["to_id"] == "claim1"
        assert "doc1" not in (hop2["from_id"], hop2["to_id"])
    finally:
        store.close()
