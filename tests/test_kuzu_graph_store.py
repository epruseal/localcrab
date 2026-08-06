"""Tests for KuzuGraphStore — mirrors test_local_graph_store_extended.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("ladybug")


@pytest.fixture
def store(tmp_path: Path):
    from opencrab.stores.kuzu_graph_store import KuzuGraphStore

    s = KuzuGraphStore(db_path=str(tmp_path / "test_kuzu"))
    yield s
    s.close()


# ------------------------------------------------------------------
# Basic CRUD
# ------------------------------------------------------------------


def test_available_flag(store) -> None:
    assert store.available is True


def test_ping(store) -> None:
    assert store.ping() is True


def test_upsert_and_get_node_by_id(store) -> None:
    store.upsert_node("Concept", "c1", {"name": "Alpha", "space": "test"})
    node = store.get_node_by_id("c1")
    assert node is not None
    assert node["node_type"] == "Concept"
    assert node["id"] == "c1"
    assert node["name"] == "Alpha"


def test_get_node_by_id_missing(store) -> None:
    assert store.get_node_by_id("does_not_exist") is None


def test_upsert_overwrites(store) -> None:
    store.upsert_node("Concept", "c1", {"name": "Alpha"})
    store.upsert_node("Concept", "c1", {"name": "Beta"})
    node = store.get_node_by_id("c1")
    assert node["name"] == "Beta"


def test_get_node(store) -> None:
    store.upsert_node("Concept", "c1", {"val": 42})
    result = store.get_node("Concept", "c1")
    assert result is not None
    assert result["val"] == 42


def test_count_nodes_all(store) -> None:
    store.upsert_node("Concept", "c1", {})
    store.upsert_node("Lever", "l1", {})
    assert store.count_nodes() == 2


def test_count_nodes_by_type(store) -> None:
    store.upsert_node("Concept", "c1", {})
    store.upsert_node("Concept", "c2", {})
    store.upsert_node("Lever", "l1", {})
    assert store.count_nodes("Concept") == 2
    assert store.count_nodes("Lever") == 1


def test_delete_node(store) -> None:
    store.upsert_node("Concept", "c1", {})
    store.upsert_node("Concept", "c2", {})
    store.upsert_edge("Concept", "c1", "related", "Concept", "c2")

    deleted = store.delete_node("Concept", "c1")
    assert deleted is True
    assert store.get_node_by_id("c1") is None
    # edge should also be gone (DETACH DELETE)
    neighbors = store.find_neighbors("c2", direction="in", depth=1, limit=10)
    assert all(n.get("properties", {}).get("id") != "c1" for n in neighbors)


# ------------------------------------------------------------------
# find_neighbors
# ------------------------------------------------------------------


def _make_graph(store) -> None:
    store.upsert_node("User", "u1", {"name": "Alice"})
    store.upsert_node("Resource", "r1", {"name": "Doc"})
    store.upsert_node("Resource", "r2", {"name": "Sheet"})
    store.upsert_node("Group", "g1", {"name": "Team"})
    store.upsert_edge("User", "u1", "owns", "Resource", "r1")
    store.upsert_edge("User", "u1", "can_view", "Resource", "r2")
    store.upsert_edge("User", "u1", "member_of", "Group", "g1")


def test_find_neighbors_out(store) -> None:
    _make_graph(store)
    neighbors = store.find_neighbors("u1", direction="out", depth=1, limit=10)
    ids = {n["properties"]["id"] for n in neighbors}
    assert ids == {"r1", "r2", "g1"}
    rel_types = {n["relation_type"] for n in neighbors}
    assert "owns" in rel_types
    for n in neighbors:
        assert "labels" in n
        assert n["depth"] == 1
        assert "relationship_types" in n


def test_find_neighbors_in(store) -> None:
    _make_graph(store)
    neighbors = store.find_neighbors("r1", direction="in", depth=1, limit=10)
    ids = {n["properties"]["id"] for n in neighbors}
    assert "u1" in ids


def test_find_neighbors_both(store) -> None:
    _make_graph(store)
    # r1 has u1 incoming; add an outgoing edge too
    store.upsert_node("Tag", "t1", {})
    store.upsert_edge("Resource", "r1", "tagged", "Tag", "t1")
    neighbors = store.find_neighbors("r1", direction="both", depth=1, limit=10)
    ids = {n["properties"]["id"] for n in neighbors}
    assert "u1" in ids
    assert "t1" in ids


def test_find_neighbors_depth2(store) -> None:
    store.upsert_node("A", "a", {})
    store.upsert_node("B", "b", {})
    store.upsert_node("C", "c", {})
    store.upsert_edge("A", "a", "next", "B", "b")
    store.upsert_edge("B", "b", "next", "C", "c")
    neighbors = store.find_neighbors("a", direction="out", depth=2, limit=10)
    ids = {n["properties"]["id"] for n in neighbors}
    assert "b" in ids
    assert "c" in ids


def test_find_neighbors_limit(store) -> None:
    store.upsert_node("Hub", "h", {})
    for i in range(20):
        store.upsert_node("Spoke", f"s{i}", {})
        store.upsert_edge("Hub", "h", "connects", "Spoke", f"s{i}")
    neighbors = store.find_neighbors("h", direction="out", depth=1, limit=5)
    assert len(neighbors) <= 5


def test_find_neighbors_pack_filter(store) -> None:
    store.upsert_node("X", "x1", {"pack_id": "pack-A"})
    store.upsert_node("X", "x2", {"pack_id": "pack-B"})
    store.upsert_node("Src", "src", {"pack_id": "pack-A"})
    store.upsert_edge("Src", "src", "rel", "X", "x1")
    store.upsert_edge("Src", "src", "rel", "X", "x2")
    neighbors = store.find_neighbors(
        "src", direction="out", depth=1, limit=10,
        pack_ids=["pack-A"], include_unpackaged=False
    )
    ids = {n["properties"]["id"] for n in neighbors}
    assert "x1" in ids
    assert "x2" not in ids


# ------------------------------------------------------------------
# find_by_relations
# ------------------------------------------------------------------


def test_find_by_relations(store) -> None:
    _make_graph(store)
    results = store.find_by_relations("u1", ["owns", "can_view"], direction="out", limit=10)
    ids = {r["properties"]["id"] for r in results}
    assert "r1" in ids
    assert "r2" in ids
    assert "g1" not in ids
    for r in results:
        assert r["relation_type"] in ("owns", "can_view")


def test_find_by_relations_empty(store) -> None:
    _make_graph(store)
    results = store.find_by_relations("u1", [], direction="out")
    assert results == []


def test_find_by_relations_limit_string_coerced(store) -> None:
    """limit may arrive as a str from raw JSON; must not crash the "both" branch."""
    _make_graph(store)
    results = store.find_by_relations("u1", ["owns", "can_view"], direction="both", limit="10")
    ids = {r["properties"]["id"] for r in results}
    assert "r1" in ids
    assert "r2" in ids


# ------------------------------------------------------------------
# find_path
# ------------------------------------------------------------------


def test_find_path(store) -> None:
    store.upsert_node("A", "a", {})
    store.upsert_node("B", "b", {})
    store.upsert_node("C", "c", {})
    store.upsert_edge("A", "a", "link", "B", "b")
    store.upsert_edge("B", "b", "link", "C", "c")
    path = store.find_path("a", "c", max_depth=4)
    assert len(path) == 2
    assert path[0]["relation"] == "link"
    assert path[1]["node"]["id"] == "c"


def test_find_path_no_path(store) -> None:
    store.upsert_node("A", "a", {})
    store.upsert_node("B", "b", {})
    assert store.find_path("a", "b") == []


# ------------------------------------------------------------------
# list_packs
# ------------------------------------------------------------------


def test_list_packs(store) -> None:
    for i in range(3):
        store.upsert_node("X", f"p1_{i}", {"pack_id": "pack-1", "name": f"node{i}"})
    store.upsert_node("X", "p2_0", {"pack_id": "pack-2"})

    packs = store.list_packs(min_nodes=1)
    pack_ids = {p["pack_id"] for p in packs}
    assert "pack-1" in pack_ids
    assert "pack-2" in pack_ids


def test_list_packs_min_nodes(store) -> None:
    for i in range(5):
        store.upsert_node("X", f"big_{i}", {"pack_id": "big-pack"})
    store.upsert_node("X", "small_0", {"pack_id": "small-pack"})

    packs = store.list_packs(min_nodes=3)
    pack_ids = {p["pack_id"] for p in packs}
    assert "big-pack" in pack_ids
    assert "small-pack" not in pack_ids


def test_list_packs_count_and_order(store) -> None:
    for i in range(4):
        store.upsert_node("X", f"a_{i}", {"pack_id": "pack-A"})
    for i in range(2):
        store.upsert_node("X", f"b_{i}", {"pack_id": "pack-B"})

    packs = store.list_packs(min_nodes=1)
    assert packs[0]["pack_id"] == "pack-A"
    assert packs[0]["node_count"] == 4


# ------------------------------------------------------------------
# export_nodes / export_edges
# ------------------------------------------------------------------


def test_export_nodes(store) -> None:
    store.upsert_node("Concept", "c1", {"name": "Alpha"})
    store.upsert_node("Lever", "l1", {"name": "Beta"})
    rows = store.export_nodes()
    assert len(rows) == 2
    for row in rows:
        assert "props" in row
        assert "labels" in row
        assert isinstance(row["labels"], list)


def test_export_nodes_pack_filter(store) -> None:
    store.upsert_node("X", "x1", {"pack_id": "target"})
    store.upsert_node("X", "x2", {"pack_id": "other"})
    store.upsert_node("X", "x3", {})
    rows = store.export_nodes(pack_id="target")
    assert len(rows) == 1
    assert rows[0]["props"]["id"] == "x1"


def test_export_nodes_space_filter_pushed_ahead_of_limit(store) -> None:
    """issue #54: space must be pushed into the Cypher WHERE clause ahead of
    LIMIT, not Python-filtered after -- otherwise target-space rows sorting
    past the limit boundary would be silently undercounted."""
    for i in range(20):
        store.upsert_node("X", f"a{i:02d}", {}, space_id="noise")
    for i in range(5):
        store.upsert_node("X", f"z{i:02d}", {}, space_id="concept")

    rows = store.export_nodes(space="concept", limit=10)
    assert len(rows) == 5
    assert all(r["props"]["space"] == "concept" for r in rows)


def test_export_nodes_pack_id_filter_pushed_ahead_of_limit(store) -> None:
    """issue #54 audit finding [3]: export_nodes' pack_id filter must apply
    BEFORE limit truncation, same as count_exported_nodes already does --
    otherwise a caller can see an accurate `total` (from
    count_exported_nodes) alongside a wrongly-truncated-to-empty `nodes`
    page from export_nodes, e.g. `total: 5, nodes: []`, whenever the first
    `limit` space-matching rows Cypher returns all happen to be the wrong
    pack_id. Seeds 150 wrong-pack_id "noise" nodes first, then 5
    right-pack_id nodes last, all in the same space, so with a small limit
    the old LIMIT-before-pack_id-filter order would return zero matches."""
    for i in range(150):
        store.upsert_node("X", f"a{i:03d}", {"pack_id": "wrong"}, space_id="concept")
    for i in range(5):
        store.upsert_node("X", f"z{i:02d}", {"pack_id": "target"}, space_id="concept")

    page = store.export_nodes(pack_id="target", space="concept", limit=10)
    total = store.count_exported_nodes(pack_id="target", space="concept")

    assert total == 5
    assert len(page) == 5  # NOT [] -- total and the page must agree
    assert all(r["props"]["pack_id"] == "target" for r in page)


def test_count_exported_nodes_space_only_not_capped_by_limit(store) -> None:
    """issue #54: count_exported_nodes(space=...) is a real Cypher count(n)
    pushdown, so it must report the true match count even when it exceeds
    export_nodes' own display limit."""
    for i in range(30):
        store.upsert_node("X", f"n{i:02d}", {}, space_id="concept")

    page = store.export_nodes(space="concept", limit=5)
    assert len(page) == 5

    assert store.count_exported_nodes(space="concept") == 30


def test_count_exported_nodes_with_pack_id_falls_back_to_exact_scan(store) -> None:
    """pack_id can't be pushed into Cypher here (JSON blob props), so
    count_exported_nodes(pack_id=..., space=...) scans every space-matching
    row and Python-filters. This must still be EXACT -- not capped by any
    limit -- even though it costs an O(n) scan instead of a cheap COUNT
    (pre-existing Kuzu pack_id characteristic, not a new bug)."""
    for i in range(30):
        store.upsert_node("X", f"a{i:02d}", {"pack_id": "target"}, space_id="concept")
    for i in range(10):
        store.upsert_node("X", f"b{i:02d}", {"pack_id": "other"}, space_id="concept")

    assert store.count_exported_nodes(pack_id="target", space="concept") == 30


def test_count_exported_nodes_with_pack_id_has_no_limit_clause(store, monkeypatch) -> None:
    """issue #54 audit finding [7]: an earlier version delegated to
    export_nodes(pack_id=..., limit=500_000), which silently re-capped
    `total` at 500,000 instead of the caller's original small `limit` --
    same bug, just a bigger ceiling. 500k+ rows aren't practical to seed in
    a unit test, so this proves the fix structurally instead: the Cypher
    count_exported_nodes(pack_id=...) issues has no LIMIT clause at all, so
    there is no ceiling of any size to hit."""
    for i in range(5):
        store.upsert_node("X", f"a{i:02d}", {"pack_id": "target"}, space_id="concept")

    queries: list[str] = []
    real_execute = store._conn.execute

    def spy(query, *args, **kwargs):
        queries.append(query)
        return real_execute(query, *args, **kwargs)

    monkeypatch.setattr(store._conn, "execute", spy)

    total = store.count_exported_nodes(pack_id="target", space="concept")

    assert total == 5
    assert queries  # the spy actually intercepted at least one call
    assert all("LIMIT" not in q for q in queries)


# ------------------------------------------------------------------
# Issue #86: keyword search_nodes must not truncate the scan before
# applying the keyword filter (the pack_id-in-Kuzu #54 pattern applied to
# a keyword predicate instead of pack_id).
# ------------------------------------------------------------------


def test_search_nodes_keyword_pushed_ahead_of_any_scan_cap(store) -> None:
    """HybridQuery.keyword_search used to call export_nodes(limit=50000)
    and search only those rows in Python, silently missing the rest of a
    corpus larger than that cap. search_nodes instead streams every
    space-matching row with no LIMIT and Python-filters keyword, stopping
    only once `limit` matches are found -- so matches after any arbitrary
    scan boundary are still found. Seeds 60 non-matching nodes first, then
    5 matching nodes last."""
    for i in range(60):
        store.upsert_node("X", f"noise{i:03d}", {"name": f"unrelated {i}"})
    for i in range(5):
        store.upsert_node("X", f"hit{i:02d}", {"name": f"needle-in-haystack {i}"})

    rows = store.search_nodes("needle", limit=10)

    assert len(rows) == 5
    assert all("needle" in r["props"]["name"] for r in rows)


def test_search_nodes_space_filter_pushed_into_cypher(store) -> None:
    """spaces is pushed into the Cypher WHERE clause (space_id is a real
    column), narrowing the scan before the Python keyword filter runs."""
    store.upsert_node("X", "n-claim", {"name": "shared term"}, space_id="claim")
    store.upsert_node("X", "n-policy", {"name": "shared term"}, space_id="policy")

    rows = store.search_nodes("shared", spaces=["claim"], limit=10)

    assert len(rows) == 1
    assert rows[0]["props"]["space"] == "claim"


def test_search_nodes_limit_zero_and_negative_return_empty(store) -> None:
    """issue #86 boundary check: the break condition is ``len(results) >=
    limit``, checked only AFTER appending a match -- so ``limit=0``
    previously still returned the first match found (1 row, not 0), and
    any negative limit behaved like ``limit=1`` (same off-by-one: an empty
    ``results`` list's length, 0, is never `>=` a negative number until
    AFTER the first append). Neither is "caller wants nothing back" (the
    same class of surprise issue #120 flagged for Mongo's ``.limit(0)``)."""
    store.upsert_node("X", "n1", {"name": "matches everything"})
    store.upsert_node("X", "n2", {"name": "matches everything"})

    assert store.search_nodes("matches", limit=0) == []
    assert store.search_nodes("matches", limit=-1) == []


# ------------------------------------------------------------------
# Issue #118: space_id (column) vs properties["space"] (JSON) divergence
# ------------------------------------------------------------------


def test_upsert_node_normalizes_space_id_to_match_props_space(store) -> None:
    """Direct invariant check: after upsert_node, the space_id column must
    equal the effective properties["space"] value -- the two can no longer
    diverge. Precedence (codex review [2]): the explicit space_id ARGUMENT
    wins, matching Neo4j (which does this natively -- see neo4j_store.py)."""
    store.upsert_node("Item", "a", {"space": "claim"}, space_id="evidence")
    r = store._conn.execute(
        "MATCH (n:OntologyNode {node_id: $id}) RETURN n.space_id, n.props", {"id": "a"}
    )
    space_id, props_raw = r.get_next()
    assert space_id == "evidence"
    assert '"space": "evidence"' in props_raw


def test_export_nodes_space_mismatch_reports_the_requested_space_not_the_stale_json(store) -> None:
    """Issue #118: every row a space="target" query returns must be LABELED
    space=="target", and count_exported_nodes (SQL-only) must agree with
    len(export_nodes(..., a limit large enough to not truncate)). pack_id
    is given so both calls go through _scan_space_matching (issue #118
    explicitly names it as a path sharing this risk: its Cypher WHERE
    filters on the raw space_id column). Pre-fix, a row selected by that
    column filter could still be merged (via _merge_space, which reads
    whatever properties["space"] literally says) into a report claiming a
    DIFFERENT space entirely."""
    for i in range(3):
        store.upsert_node(
            "Item", f"mis{i}", {"pack_id": "p1", "space": "other"}, space_id="target"
        )
    store.upsert_node("Item", "real", {"pack_id": "p1"}, space_id="target")

    total = store.count_exported_nodes(pack_id="p1", space="target")
    page = store.export_nodes(pack_id="p1", space="target", limit=10)

    assert total == len(page) == 4
    assert all(r["props"]["space"] == "target" for r in page)


def test_export_edges(store) -> None:
    store.upsert_node("A", "a", {})
    store.upsert_node("B", "b", {})
    store.upsert_edge("A", "a", "connects", "B", "b", {"weight": 1})
    rows = store.export_edges()
    assert len(rows) == 1
    r = rows[0]
    assert "source_props" in r
    assert "target_props" in r
    assert "source_labels" in r
    assert "target_labels" in r
    assert "rel_props" in r
    assert r["relation"] == "connects"
    assert r["rel_props"]["weight"] == 1


# ------------------------------------------------------------------
# Batch operations
# ------------------------------------------------------------------


def test_upsert_nodes_batch(store) -> None:
    nodes = [
        {"node_type": "X", "node_id": f"n{i}", "properties": {"val": i}, "space_id": None}
        for i in range(5)
    ]
    count = store.upsert_nodes_batch(nodes)
    assert count == 5
    assert store.count_nodes() == 5


def test_upsert_edges_batch(store) -> None:
    store.upsert_node("A", "a", {})
    store.upsert_node("B", "b", {})
    store.upsert_node("C", "c", {})
    edges = [
        {"from_type": "A", "from_id": "a", "relation": "r", "to_type": "B", "to_id": "b"},
        {"from_type": "A", "from_id": "a", "relation": "r", "to_type": "C", "to_id": "c"},
    ]
    count = store.upsert_edges_batch(edges)
    assert count == 2
    neighbors = store.find_neighbors("a", direction="out", depth=1, limit=10)
    assert len(neighbors) == 2


# ------------------------------------------------------------------
# run_cypher
# ------------------------------------------------------------------


def test_run_cypher_returns_dicts(store) -> None:
    store.upsert_node("X", "x1", {"val": "hello"})
    rows = store.run_cypher(
        "MATCH (n:OntologyNode {node_id: $id}) RETURN n.node_id AS nid, n.node_type AS nt",
        {"id": "x1"},
    )
    assert len(rows) == 1
    assert rows[0]["nid"] == "x1"
    assert rows[0]["nt"] == "X"


def test_run_cypher_invalid_returns_empty(store) -> None:
    rows = store.run_cypher("THIS IS NOT VALID CYPHER")
    assert rows == []


# ------------------------------------------------------------------
# ReBACEngine integration
# ------------------------------------------------------------------


def test_rebac_direct_permission_with_kuzu(store) -> None:
    from opencrab.ontology.rebac import ReBACEngine

    sql_stub = MagicMock()
    sql_stub.available = False

    engine = ReBACEngine(neo4j=store, sql=sql_stub)

    store.upsert_node("User", "user1", {})
    store.upsert_node("Resource", "res1", {})
    store.upsert_edge("User", "user1", "owns", "Resource", "res1")

    decision = engine.check("user1", "view", "res1")
    assert decision.granted is True


def test_rebac_transitive_permission_with_kuzu(store) -> None:
    from opencrab.ontology.rebac import ReBACEngine

    sql_stub = MagicMock()
    sql_stub.available = False

    engine = ReBACEngine(neo4j=store, sql=sql_stub)

    store.upsert_node("User", "user1", {})
    store.upsert_node("Group", "grp1", {})
    store.upsert_node("Resource", "res1", {})
    store.upsert_edge("User", "user1", "member_of", "Group", "grp1")
    store.upsert_edge("Group", "grp1", "can_view", "Resource", "res1")

    decision = engine.check("user1", "view", "res1")
    assert decision.granted is True


def test_rebac_denied_when_no_edge(store) -> None:
    from opencrab.ontology.rebac import ReBACEngine

    sql_stub = MagicMock()
    sql_stub.available = False

    engine = ReBACEngine(neo4j=store, sql=sql_stub)

    store.upsert_node("User", "user1", {})
    store.upsert_node("Resource", "res1", {})

    decision = engine.check("user1", "view", "res1")
    assert decision.granted is False
