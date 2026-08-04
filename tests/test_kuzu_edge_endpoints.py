"""KuzuGraphStore의 edge endpoint / anchor description 투영 계약.

LocalGraphStore(_sql_graph_base)와 동일한 계약을 Kuzu 백엔드가 지키는지 고정한다.
direction="both"를 두 번의 directed 쿼리로 나눈 구현이 방향 정보를 정확히
싣고, 한쪽 방향이 limit을 독식하지 않는지가 핵심이다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("ladybug")


@pytest.fixture
def store(tmp_path: Path):
    from opencrab.stores.kuzu_graph_store import KuzuGraphStore

    s = KuzuGraphStore(db_path=str(tmp_path / "edge_endpoints_kuzu"))
    yield s
    s.close()


def test_endpoints_carry_true_edge_direction(store) -> None:
    store.upsert_node("Document", "doc1", {"pack_id": "p"})
    store.upsert_node("TextUnit", "tu1", {"pack_id": "p"})
    store.upsert_node("Claim", "claim1", {"pack_id": "p"})
    store.upsert_edge("Document", "doc1", "contains", "TextUnit", "tu1")
    store.upsert_edge("TextUnit", "tu1", "supports", "Claim", "claim1")

    rows = store.find_neighbors("tu1", direction="both", depth=1, limit=10)
    by_id = {r["properties"]["id"]: r for r in rows}

    # in-edge: 이웃이 source, anchor가 target
    assert (by_id["doc1"]["from_id"], by_id["doc1"]["to_id"]) == ("doc1", "tu1")
    # out-edge: anchor가 source
    assert (by_id["claim1"]["from_id"], by_id["claim1"]["to_id"]) == ("tu1", "claim1")


def test_depth_two_endpoints_are_not_the_anchor(store) -> None:
    store.upsert_node("Document", "doc1", {"pack_id": "p"})
    store.upsert_node("TextUnit", "tu1", {"pack_id": "p"})
    store.upsert_node("Claim", "claim1", {"pack_id": "p"})
    store.upsert_edge("Document", "doc1", "contains", "TextUnit", "tu1")
    store.upsert_edge("TextUnit", "tu1", "supports", "Claim", "claim1")

    rows = store.find_neighbors("doc1", direction="both", depth=2, limit=10)
    hop2 = next(r for r in rows if r["properties"]["id"] == "claim1")
    assert (hop2["from_id"], hop2["to_id"]) == ("tu1", "claim1")
    assert "doc1" not in (hop2["from_id"], hop2["to_id"])


def test_out_fanout_does_not_starve_in_edges(store) -> None:
    """허브 노드의 out-이웃이 limit을 독식하면 in-이웃이 영원히 안 나온다."""
    store.upsert_node("Claim", "hub", {"pack_id": "p"})
    store.upsert_node("Claim", "parent", {"pack_id": "p"})
    store.upsert_edge("Claim", "parent", "RELATED_TO", "Claim", "hub")
    for i in range(10):
        store.upsert_node("Claim", f"child{i}", {"pack_id": "p"})
        store.upsert_edge("Claim", "hub", "RELATED_TO", "Claim", f"child{i}")

    rows = store.find_neighbors("hub", direction="both", depth=1, limit=4)
    ids = {r["properties"]["id"] for r in rows}
    assert len(rows) == 4
    assert "parent" in ids


def test_single_direction_is_unaffected(store) -> None:
    store.upsert_node("Claim", "a", {"pack_id": "p"})
    for i in range(5):
        store.upsert_node("Claim", f"b{i}", {"pack_id": "p"})
        store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", f"b{i}")

    rows = store.find_neighbors("a", direction="out", depth=1, limit=3)
    assert len(rows) == 3
    assert all(r["from_id"] == "a" for r in rows)


def test_list_packs_projects_anchor_description(store) -> None:
    store.upsert_node(
        "Dataset",
        "dataset:packA",
        {"pack_id": "packA", "title": "My Pack", "description": "About pack A"},
    )
    store.upsert_node("Item", "i1", {"pack_id": "packA"})
    store.upsert_node("Item", "i2", {"pack_id": "packB", "description": "노드 설명"})

    packs = {p["pack_id"]: p for p in store.list_packs(1)}
    assert packs["packA"]["sample_description"] == "About pack A"
    # anchor가 없으면 노드 단위 폴백 없이 빈 문자열
    assert packs["packB"]["sample_description"] == ""
