from __future__ import annotations

from pathlib import Path

import pytest

from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.neo4j_store import Neo4jStore


def _make_store(tmp_path: Path) -> LocalGraphStore:
    return LocalGraphStore(str(tmp_path / "graph.db"))


def test_t6_node_strict_excludes_foreign_pack(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.upsert_node("Claim", "a", {"pack_id": "A", "name": "anchor"})
    store.upsert_node("Claim", "b", {"pack_id": "A", "name": "neighbour"})
    store.upsert_node("Claim", "c", {"pack_id": "B", "name": "foreign"})
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {"pack_id": "A"})
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "c", {"pack_id": "B"})

    rows = store.find_neighbors("a", direction="both", depth=1, pack_ids=["A"])
    ids = {r["properties"]["id"] for r in rows}
    assert ids == {"b"}


def test_t6_node_strict_excludes_unpackaged_by_default(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.upsert_node("Claim", "a", {"pack_id": "A"})
    store.upsert_node("Claim", "b", {})  # legacy, no pack_id
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {})

    rows = store.find_neighbors("a", direction="both", depth=1, pack_ids=["A"])
    assert rows == []


def test_t6_include_unpackaged_allows_legacy_node(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.upsert_node("Claim", "a", {"pack_id": "A"})
    store.upsert_node("Claim", "b", {})  # legacy, no pack_id
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {})

    rows = store.find_neighbors(
        "a", direction="both", depth=1,
        pack_ids=["A"], include_unpackaged=True,
    )
    ids = {r["properties"]["id"] for r in rows}
    assert ids == {"b"}


def test_t6_edge_pack_id_in_set_passes(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.upsert_node("Claim", "a", {"pack_id": "A"})
    store.upsert_node("Claim", "b", {"pack_id": "A"})
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {"pack_id": "A"})

    rows = store.find_neighbors("a", pack_ids=["A"])
    assert {r["properties"]["id"] for r in rows} == {"b"}


def test_t6_edge_pack_id_foreign_always_excluded(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.upsert_node("Claim", "a", {"pack_id": "A"})
    store.upsert_node("Claim", "b", {"pack_id": "A"})
    # Edge explicitly tagged with foreign pack_id should drop even though
    # endpoints satisfy the filter.
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {"pack_id": "B"})

    rows = store.find_neighbors("a", pack_ids=["A"])
    assert rows == []


def test_t6_edge_unpackaged_requires_both_endpoints(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.upsert_node("Claim", "a", {"pack_id": "A"})
    store.upsert_node("Claim", "b", {"pack_id": "A"})
    store.upsert_node("Claim", "c", {})  # legacy / unpackaged
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {})
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "c", {})

    rows = store.find_neighbors("a", pack_ids=["A"])
    # With include_unpackaged=False, the orphan endpoint blocks the
    # unpackaged edge entirely.
    assert {r["properties"]["id"] for r in rows} == {"b"}

    rows_opt = store.find_neighbors("a", pack_ids=["A"], include_unpackaged=True)
    # Opt-in allows the orphan endpoint, so the unpackaged edge also passes.
    assert {r["properties"]["id"] for r in rows_opt} == {"b", "c"}


def test_t6_anchor_outside_filter_returns_empty(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.upsert_node("Claim", "a", {"pack_id": "B"})
    store.upsert_node("Claim", "b", {"pack_id": "A"})
    store.upsert_edge("Claim", "a", "RELATED_TO", "Claim", "b", {})

    # Anchor "a" has pack_id=B, but we filter on A — anchor itself fails.
    rows = store.find_neighbors("a", pack_ids=["A"])
    assert rows == []


# ---------------------------------------------------------------------------
# T11 — Neo4j Cypher structure (no live Neo4j)
# ---------------------------------------------------------------------------


def test_t11_neo4j_no_filter_omits_where() -> None:
    cypher, params = Neo4jStore._build_neighbors_cypher(
        node_id="x", direction="both", depth=2, limit=10,
        pack_ids=None, include_unpackaged=False,
    )
    assert "WHERE" not in cypher
    assert "pack_id" not in cypher
    assert params == {"id": "x", "limit": 10}


def test_t11_neo4j_strict_pack_filter() -> None:
    cypher, params = Neo4jStore._build_neighbors_cypher(
        node_id="x", direction="out", depth=1, limit=5,
        pack_ids=["A", "B"], include_unpackaged=False,
    )
    assert "neighbor.pack_id IN $pack_ids" in cypher
    assert "ALL(r IN relationships(path)" in cypher
    assert "r.pack_id IS NULL OR r.pack_id IN $pack_ids" in cypher
    assert params["pack_ids"] == ["A", "B"]


def test_t11_neo4j_include_unpackaged_allows_null_neighbor() -> None:
    cypher, _params = Neo4jStore._build_neighbors_cypher(
        node_id="x", direction="both", depth=1, limit=5,
        pack_ids=["A"], include_unpackaged=True,
    )
    assert "neighbor.pack_id IS NULL OR neighbor.pack_id IN $pack_ids" in cypher
