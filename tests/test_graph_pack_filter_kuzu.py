"""T6 pack-filter regression tests for KuzuGraphStore.

Mirrors test_graph_pack_filter.py (LocalGraphStore) to confirm that
KuzuGraphStore's Python-side pack filtering behaves identically.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def store(tmp_path: Path):
    from opencrab.stores.kuzu_graph_store import KuzuGraphStore

    s = KuzuGraphStore(db_path=str(tmp_path / "pack_filter_kuzu"))
    yield s
    s.close()


def test_t6k_node_strict_excludes_foreign_pack(store) -> None:
    store.upsert_node("Claim", "a", {"pack_id": "A", "name": "anchor"})
    store.upsert_node("Claim", "b", {"pack_id": "A", "name": "neighbour"})
    store.upsert_node("Claim", "c", {"pack_id": "B", "name": "foreign"})
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {"pack_id": "A"})
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "c", {"pack_id": "B"})

    rows = store.find_neighbors("a", direction="both", depth=1, pack_ids=["A"])
    ids = {r["properties"]["id"] for r in rows}
    assert ids == {"b"}


def test_t6k_node_strict_excludes_unpackaged_by_default(store) -> None:
    store.upsert_node("Claim", "a", {"pack_id": "A"})
    store.upsert_node("Claim", "b", {})  # legacy, no pack_id
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {})

    rows = store.find_neighbors("a", direction="both", depth=1, pack_ids=["A"])
    assert rows == []


def test_t6k_include_unpackaged_allows_legacy_node(store) -> None:
    store.upsert_node("Claim", "a", {"pack_id": "A"})
    store.upsert_node("Claim", "b", {})  # legacy, no pack_id
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {})

    rows = store.find_neighbors(
        "a", direction="both", depth=1,
        pack_ids=["A"], include_unpackaged=True,
    )
    ids = {r["properties"]["id"] for r in rows}
    assert ids == {"b"}


def test_t6k_edge_pack_id_in_set_passes(store) -> None:
    store.upsert_node("Claim", "a", {"pack_id": "A"})
    store.upsert_node("Claim", "b", {"pack_id": "A"})
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {"pack_id": "A"})

    rows = store.find_neighbors("a", pack_ids=["A"])
    assert {r["properties"]["id"] for r in rows} == {"b"}


def test_t6k_edge_pack_id_foreign_always_excluded(store) -> None:
    store.upsert_node("Claim", "a", {"pack_id": "A"})
    store.upsert_node("Claim", "b", {"pack_id": "A"})
    # Edge tagged with foreign pack_id should be dropped even though
    # both endpoints satisfy the node filter.
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {"pack_id": "B"})

    rows = store.find_neighbors("a", pack_ids=["A"])
    assert rows == []


def test_t6k_edge_unpackaged_requires_both_endpoints(store) -> None:
    store.upsert_node("Claim", "a", {"pack_id": "A"})
    store.upsert_node("Claim", "b", {"pack_id": "A"})
    store.upsert_node("Claim", "c", {})  # legacy / unpackaged
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {})
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "c", {})

    rows = store.find_neighbors("a", pack_ids=["A"])
    assert {r["properties"]["id"] for r in rows} == {"b"}

    rows_opt = store.find_neighbors("a", pack_ids=["A"], include_unpackaged=True)
    assert {r["properties"]["id"] for r in rows_opt} == {"b", "c"}


def test_t6k_anchor_outside_filter_returns_empty(store) -> None:
    store.upsert_node("Claim", "a", {"pack_id": "B"})
    store.upsert_node("Claim", "b", {"pack_id": "A"})
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {})

    rows = store.find_neighbors("a", pack_ids=["A"])
    assert rows == []


def test_t6k_parallel_edges_foreign_first_does_not_block_allowed(store) -> None:
    """Foreign-pack edge must not pre-mark the destination visited and block the allowed edge."""
    store.upsert_node("Claim", "a", {"pack_id": "A"})
    store.upsert_node("Claim", "b", {"pack_id": "A"})
    store.upsert_edge("Claim", "a", "FOREIGN_REL", "Claim", "b", {"pack_id": "B"})
    store.upsert_edge("Claim", "a", "ALLOWED_REL", "Claim", "b", {"pack_id": "A"})

    rows = store.find_neighbors("a", direction="out", depth=1, pack_ids=["A"])
    ids = {r["properties"]["id"] for r in rows}
    assert ids == {"b"}


def test_t6k_parallel_edges_foreign_first_depth2_expansion_not_blocked(store) -> None:
    """Depth-2 expansion from b must work even if one depth-1 edge to b was foreign."""
    store.upsert_node("Claim", "a", {"pack_id": "A"})
    store.upsert_node("Claim", "b", {"pack_id": "A"})
    store.upsert_node("Claim", "d", {"pack_id": "A"})
    store.upsert_edge("Claim", "a", "FOREIGN_REL", "Claim", "b", {"pack_id": "B"})
    store.upsert_edge("Claim", "a", "ALLOWED_REL", "Claim", "b", {"pack_id": "A"})
    store.upsert_edge("Claim", "b", "REL", "Claim", "d", {"pack_id": "A"})

    rows = store.find_neighbors("a", direction="out", depth=2, pack_ids=["A"])
    ids = {r["properties"]["id"] for r in rows}
    assert "b" in ids
    assert "d" in ids


def test_t6k_no_filter_dedup_same_node_via_two_edges(store) -> None:
    """Without pack filter, a node reachable via two edges must appear only once."""
    store.upsert_node("Claim", "a", {"name": "anchor"})
    store.upsert_node("Claim", "b", {"name": "target"})
    store.upsert_edge("Claim", "a", "REL1", "Claim", "b", {})
    store.upsert_edge("Claim", "a", "REL2", "Claim", "b", {})

    rows = store.find_neighbors("a", direction="out", depth=1)
    ids = [r["properties"]["id"] for r in rows]
    assert ids.count("b") == 1
