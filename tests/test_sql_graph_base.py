"""Unit tests for opencrab/stores/_sql_graph_base.py (_SqlGraphStoreBase).

Exercises the shared 20-method graph-store surface against a minimal
SQLite-backed test double — no PG dependency, no adoption by
LocalGraphStore/PGGraphStore needed (this base is not yet wired into either,
per its own module docstring). Proves the shared SQL text/logic is not just
structurally plausible but actually correct against a real sqlite3
connection, mirroring test_sql_dialect.py's "executes against a real
connection" strategy for _sql_doc_base.py.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from opencrab.common.graph_identity import (
    EdgeIdentityConflict,
    GraphReadCapabilityUnavailable,
    NodeIdentityConflict,
)
from opencrab.stores._sql_dialect import SQLITE
from opencrab.stores._sql_graph_base import GRAPH_STORE_SCHEMA, _SqlGraphStoreBase


class _SqliteGraphStoreDouble(_SqlGraphStoreBase):
    """Minimal concrete adopter — implements only the hooks, no lifecycle
    frills (thread-locals, WAL, locks) since single-threaded tests don't
    need them; proves the base's hook CONTRACT is sufficient on its own."""

    _dialect = SQLITE

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._available = True
        for stmt in SQLITE.render_ddl(GRAPH_STORE_SCHEMA):
            self._conn.execute(stmt)
        self._conn.commit()

    def _table(self, name: str) -> str:
        return name

    def _fetch_all(self, sql: str, params: dict[str, Any]) -> list[tuple]:
        return self._conn.execute(sql, params).fetchall()

    def _fetch_one(self, sql: str, params: dict[str, Any]) -> tuple | None:
        return self._conn.execute(sql, params).fetchone()

    def _exec_write(self, sql: str, params: dict[str, Any]) -> int:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur.rowcount

    def _exec_write_many(self, statements: list[tuple[str, dict[str, Any]]]) -> list[int]:
        rowcounts = []
        for sql, params in statements:
            cur = self._conn.execute(sql, params)
            rowcounts.append(cur.rowcount)
        self._conn.commit()
        return rowcounts

    def _exec_write_batch(self, sql: str, params_list: list[dict[str, Any]]) -> None:
        self._conn.executemany(sql, params_list)
        self._conn.commit()

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError("not available")


def _store() -> _SqliteGraphStoreDouble:
    return _SqliteGraphStoreDouble()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_graph_store_schema_renders_and_executes_sqlite():
    conn = sqlite3.connect(":memory:")
    try:
        for stmt in SQLITE.render_ddl(GRAPH_STORE_SCHEMA):
            conn.execute(stmt)
        conn.commit()
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"graph_nodes", "graph_edges"} <= tables
        indexes = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {"idx_nodes_pack", "idx_nodes_space", "idx_edges_from", "idx_edges_to"} <= indexes
    finally:
        conn.close()


def test_graph_store_schema_postgres_pack_index_double_parens():
    stmts = SQLITE.render_ddl(GRAPH_STORE_SCHEMA)
    from opencrab.stores._sql_dialect import POSTGRES

    pg_stmts = POSTGRES.render_ddl(GRAPH_STORE_SCHEMA, schema_name="s1")
    pg_idx = next(s for s in pg_stmts if "idx_nodes_pack" in s)
    assert "((properties->>'pack_id'))" in pg_idx

    sqlite_idx = next(s for s in stmts if "idx_nodes_pack" in s)
    assert "(json_extract(properties, '$.pack_id'))" in sqlite_idx
    assert "((json_extract" not in sqlite_idx  # single parens, not double


# ---------------------------------------------------------------------------
# Node / edge CRUD
# ---------------------------------------------------------------------------


def test_upsert_and_get_node_roundtrip():
    store = _store()
    props = store.upsert_node("Person", "p1", {"name": "Alice"})
    assert props == {"name": "Alice", "id": "p1"}
    assert store.get_node("Person", "p1") == {"name": "Alice", "id": "p1"}
    assert store.get_node("Person", "nope") is None


def test_upsert_node_conflict_is_rejected_without_mutation():
    store = _store()
    store.upsert_node("Person", "p1", {"first_only": "x", "shared": "old"})
    with pytest.raises(NodeIdentityConflict):
        store.upsert_node("Person", "p1", {"shared": "new"})
    node = store.get_node("Person", "p1")
    assert node == {"first_only": "x", "shared": "old", "id": "p1"}


def test_lookup_node_type():
    store = _store()
    store.upsert_node("Person", "p1", {})
    assert store.lookup_node_type("p1") == "Person"
    assert store.lookup_node_type("nope") is None


def test_lookup_node_type_malformed_raises():
    # A row matched but node_type came back empty -- a data-integrity fault,
    # not "not found" (#162). The column is NOT NULL, so an empty string is
    # the reachable malformed shape; write it directly since upsert_node's
    # own validation would refuse it through the normal API.
    store = _store()
    store.upsert_node("Person", "p1", {})
    store._conn.execute("UPDATE graph_nodes SET node_type = '' WHERE node_id = :id", {"id": "p1"})
    store._conn.commit()

    with pytest.raises(GraphReadCapabilityUnavailable):
        store.lookup_node_type("p1")


@pytest.mark.parametrize("bad_label", ["has space", "1leadingdigit", "kebab-case"])
def test_lookup_node_type_truthy_but_invalid_label_raises(bad_label):
    # A row matched with a NON-empty node_type that is still not a legal
    # label -- the column disallows NULL and upsert_node's own validation
    # would refuse this shape through the normal API, so it is written
    # directly. The bare "not node_type" check (#162 v1/v2) let this pass
    # through as if it were a real type, and OntologyBuilder.add_edge would
    # forward it to get_node/upsert_edge, which raise a raw
    # TypeError/ValueError there instead of the intended fail-closed
    # graph-unavailable receipt (#162 codex review round 6).
    store = _store()
    store.upsert_node("Person", "p1", {})
    store._conn.execute(
        "UPDATE graph_nodes SET node_type = :nt WHERE node_id = :id", {"nt": bad_label, "id": "p1"}
    )
    store._conn.commit()

    with pytest.raises(GraphReadCapabilityUnavailable):
        store.lookup_node_type("p1")


def test_lookup_node_type_raises_when_unavailable():
    # #162: an unavailable store cannot tell "node absent" from "store
    # down" -- it must raise instead of degrading to None, so
    # OntologyBuilder.add_edge can refuse the write instead of guessing a
    # default type.
    store = _store()
    store._available = False
    with pytest.raises(GraphReadCapabilityUnavailable):
        store.lookup_node_type("p1")


@pytest.mark.parametrize(
    "exc_type", [KeyError, TypeError, AttributeError, IndexError, ValueError, AssertionError]
)
def test_lookup_node_type_propagates_programming_errors(exc_type):
    # _fetch_one is implemented differently per backend (local_graph_store.py
    # vs pg_graph_store.py, #162 v3 codex review) -- an adapter mistype or
    # signature drift there must surface as itself, not be disguised as
    # "store unavailable" (full denylist coverage round 5).
    store = _store()

    def _boom(sql, params):
        raise exc_type("boom")

    store._fetch_one = _boom
    with pytest.raises(exc_type):
        store.lookup_node_type("p1")


def test_lookup_node_type_wraps_other_errors_with_cause():
    store = _store()
    original = sqlite3.OperationalError("database is locked")

    def _boom(sql, params):
        raise original

    store._fetch_one = _boom
    with pytest.raises(GraphReadCapabilityUnavailable) as excinfo:
        store.lookup_node_type("p1")
    assert excinfo.value.__cause__ is original


def test_delete_node_matches_type_and_id_pair():
    store = _store()
    store.upsert_node("Person", "p1", {})
    # wrong node_type must NOT delete
    assert store.delete_node("WrongType", "p1") is False
    assert store.get_node("Person", "p1") is not None
    # correct pair deletes
    assert store.delete_node("Person", "p1") is True
    assert store.get_node("Person", "p1") is None
    assert store.delete_node("Person", "p1") is False


def test_delete_node_also_removes_incident_edges():
    store = _store()
    store.upsert_node("Person", "a", {})
    store.upsert_node("Person", "b", {})
    store.upsert_edge("Person", "a", "knows", "Person", "b")
    assert store.delete_node("Person", "a") is True
    assert store.find_by_relations("b", ["knows"], direction="in") == []


def test_upsert_edge_conflict_is_rejected_without_mutation():
    store = _store()
    store.upsert_node("Person", "a", {})
    store.upsert_node("Person", "b", {})
    store.upsert_edge("Person", "a", "knows", "Person", "b", {"since": 2020})
    with pytest.raises(EdgeIdentityConflict):
        store.upsert_edge("Person", "a", "knows", "Person", "b", {"since": 2021})
    edge = store.get_edge("Person", "a", "knows", "Person", "b")
    assert edge["since"] == 2020


def test_run_cypher_is_noop():
    store = _store()
    assert store.run_cypher("MATCH (n) RETURN n") == []


def test_count_nodes():
    store = _store()
    store.upsert_node("Person", "p1", {})
    store.upsert_node("Person", "p2", {})
    store.upsert_node("Org", "o1", {})
    assert store.count_nodes() == 3
    assert store.count_nodes("Person") == 2
    assert store.count_nodes("Org") == 1
    assert store.count_nodes("Nope") == 0


def test_ensure_constraints_is_noop():
    store = _store()
    store.ensure_constraints()  # must not raise


# ---------------------------------------------------------------------------
# list_packs — pack_id str unification
# ---------------------------------------------------------------------------


def test_list_packs_pack_id_is_always_str():
    """Even on the SQLite dialect (json_extract preserves native JSON types),
    the base's list_packs() must coerce pack_id to str (Stage 6b Deliverable
    2 — unify to str)."""
    store = _store()
    store.upsert_node("Item", "n1", {"pack_id": 42})
    store.upsert_node("Item", "n2", {"pack_id": 42})
    packs = store.list_packs()
    assert len(packs) == 1
    assert packs[0]["pack_id"] == "42"
    assert isinstance(packs[0]["pack_id"], str)
    assert packs[0]["node_count"] == 2


def test_list_packs_sample_title_from_dataset_anchor():
    store = _store()
    store.upsert_node(
        "Dataset",
        "dataset:packA",
        {"pack_id": "packA", "title": "My Pack", "description": "About pack A"},
    )
    store.upsert_node("Item", "i1", {"pack_id": "packA"})
    packs = store.list_packs()
    assert packs[0] == {
        "pack_id": "packA",
        "node_count": 2,
        "sample_title": "My Pack",
        # description은 anchor에만 있고 노드 단위 폴백이 없다.
        "sample_description": "About pack A",
    }


def test_list_packs_sample_description_empty_without_anchor():
    store = _store()
    store.upsert_node("Item", "i1", {"pack_id": "packB", "description": "노드 설명"})
    packs = store.list_packs()
    assert packs[0]["sample_description"] == ""


def test_list_packs_min_nodes_filter():
    store = _store()
    store.upsert_node("Item", "i1", {"pack_id": "small"})
    store.upsert_node("Item", "i1b", {"pack_id": "big"})
    store.upsert_node("Item", "i2b", {"pack_id": "big"})
    packs = store.list_packs(min_nodes=2)
    assert [p["pack_id"] for p in packs] == ["big"]


# ---------------------------------------------------------------------------
# find_neighbors (BFS) / find_path / find_by_relations
# ---------------------------------------------------------------------------


def _make_chain(store, length: int, prefix: str = "n") -> None:
    for i in range(length + 1):
        store.upsert_node("Item", f"{prefix}{i}", {})
    for i in range(length):
        store.upsert_edge("Item", f"{prefix}{i}", "next", "Item", f"{prefix}{i + 1}")


def test_find_neighbors_basic_bfs():
    store = _store()
    _make_chain(store, 3)
    res = store.find_neighbors("n0", direction="out", depth=1, limit=50)
    assert len(res) == 1
    assert res[0]["properties"]["id"] == "n1"
    assert res[0]["relation_type"] == "next"
    assert res[0]["depth"] == 1

    res2 = store.find_neighbors("n0", direction="out", depth=2, limit=50)
    ids = sorted(r["properties"]["id"] for r in res2)
    assert ids == ["n1", "n2"]


def test_find_neighbors_pack_filter():
    store = _store()
    store.upsert_node("Item", "a", {"pack_id": "p1"})
    store.upsert_node("Item", "b", {"pack_id": "p2"})
    store.upsert_edge("Item", "a", "rel", "Item", "b")
    assert store.find_neighbors("a", direction="out", pack_ids=["p1"]) == []
    res = store.find_neighbors("a", direction="out", pack_ids=["p1", "p2"])
    assert len(res) == 1


def test_find_neighbors_anchor_fails_filter_returns_empty():
    store = _store()
    store.upsert_node("Item", "a", {"pack_id": "p1"})
    assert store.find_neighbors("a", pack_ids=["other"]) == []


def test_pack_filter_matches_node_passes_across_falsy_and_typed_pack_ids():
    """Issue #62 follow-up: SQL's pushed-down pack predicate (_pack_where /
    ``SqlDialect.json_truthy_text``) must admit exactly the same nodes
    ``_node_passes`` does, for every JSON pack_id shape — not just
    null/missing. A bare JSON extraction is non-NULL for ``""``/``0``/
    ``false`` (Python-falsy, "no pack_id" per ``_node_pack_id``) and
    SQLite's ``json_extract`` preserves a JSON number's native type (never
    text-equal to a bound string ``pack_ids`` entry) — either gap would
    make the SQL side wrongly exclude/admit rows relative to Python,
    silently reproducing a narrower form of issue #62's LIMIT-before-filter
    bug for these specific value shapes.

    Contrastive by construction: for each (pack_ids, include_unpackaged)
    config, the expected admit set is computed directly from
    ``_node_passes`` (not hand-derived), so this catches either side
    drifting from the other, not just today's specific bug.
    """
    from opencrab.stores._graph_common import _node_passes

    # Exercises _fetch_edges_for_node directly (not the full find_neighbors
    # BFS) so the anchor's own pack membership — a separate, already-covered
    # concern (test_find_neighbors_anchor_fails_filter_returns_empty) — can't
    # confound which (pack_ids, include_unpackaged) configs are exercisable
    # below. Every edge here carries no properties of its own, so
    # ``_edge_passes`` collapses to exactly the node-side check (its
    # ``src_passes`` is always True by the BFS invariant ``_pack_where``
    # documents, and ``dst_passes`` is the node check itself).
    store = _store()
    store.upsert_node("Hub", "hub", {})
    variants: dict[str, dict] = {
        "n_null": {"pack_id": None},
        "n_missing": {},
        "n_empty": {"pack_id": ""},
        "n_zero": {"pack_id": 0},
        "n_real_zero": {"pack_id": 0.0},  # trap: text "0.0" != text "0", must still be falsy
        "n_false": {"pack_id": False},
        "n_own_pack": {"pack_id": "A"},
        "n_foreign": {"pack_id": "B"},
        "n_number": {"pack_id": 5},
        "n_true": {"pack_id": True},
        "n_string_zero": {"pack_id": "0"},  # trap: truthy string, must NOT be folded into falsy 0
    }
    for node_id, props in variants.items():
        store.upsert_node("Item", node_id, props)
        store.upsert_edge("Hub", "hub", "touches", "Item", node_id)

    for pack_ids, include_unpackaged in [
        (["A"], False),
        (["A"], True),
        (["5", "True"], False),
        (["0"], False),  # n_string_zero must be admitted here, n_zero/n_real_zero must not
    ]:
        pack_set = set(pack_ids)
        expected = {
            node_id
            for node_id, props in variants.items()
            if _node_passes({**props, "id": node_id}, pack_set, include_unpackaged)
        }
        rows = store._fetch_edges_for_node(
            "hub", cap=50, out=True, pack_set=pack_set, include_unpackaged=include_unpackaged
        )
        actual = {other_id for _other_type, other_id, _rel, _props in rows}
        assert actual == expected, (pack_ids, include_unpackaged, actual, expected)


def test_find_neighbors_hub_fanout_respects_limit():
    store = _store()
    store.upsert_node("Hub", "hub", {})
    for i in range(30):
        store.upsert_node("Item", f"i{i}", {})
        store.upsert_edge("Hub", "hub", "touches", "Item", f"i{i}")
    res = store.find_neighbors("hub", direction="out", depth=1, limit=10)
    assert len(res) == 10
    ids = [r["properties"]["id"] for r in res]
    assert len(ids) == len(set(ids))


def test_find_path():
    store = _store()
    _make_chain(store, 4)
    path = store.find_path("n0", "n4", max_depth=4)
    assert [step["relation"] for step in path] == ["next"] * 4
    assert path[-1]["node"]["id"] == "n4"


def test_find_path_hop_bound_not_found():
    store = _store()
    _make_chain(store, 5)
    assert store.find_path("n0", "n5", max_depth=4) == []


def test_find_path_no_path():
    store = _store()
    store.upsert_node("Item", "a", {})
    store.upsert_node("Item", "b", {})
    assert store.find_path("a", "b", max_depth=4) == []


def test_find_by_relations_direction_both():
    store = _store()
    store.upsert_node("Item", "a", {})
    store.upsert_node("Item", "b", {})
    store.upsert_node("Item", "c", {})
    store.upsert_edge("Item", "a", "next", "Item", "b")
    store.upsert_edge("Item", "c", "next", "Item", "a")
    res = store.find_by_relations("a", ["next"], direction="both")
    ids = sorted(r["properties"]["id"] for r in res)
    assert ids == ["b", "c"]


def test_find_by_relations_empty_relations_returns_empty():
    store = _store()
    store.upsert_node("Item", "a", {})
    assert store.find_by_relations("a", []) == []


# ---------------------------------------------------------------------------
# get_node_by_id / export_nodes / export_edges / batch upserts
# ---------------------------------------------------------------------------


def test_get_node_by_id():
    store = _store()
    store.upsert_node("Person", "p1", {"name": "Alice"})
    node = store.get_node_by_id("p1")
    assert node["node_type"] == "Person"
    assert node["name"] == "Alice"
    assert store.get_node_by_id("nope") is None


def test_get_nodes_by_id_returns_the_single_global_identity_row():
    # A node_id is globally unique, independent of node_type. A second
    # logical row with the same id is rejected before any ambiguous lookup.
    store = _store()
    store.upsert_node("Document", "dup", {"pack_id": "packA"})
    with pytest.raises(NodeIdentityConflict):
        store.upsert_node("Concept", "dup", {"pack_id": "packB"})

    nodes = store.get_nodes_by_id("dup")

    assert nodes == [{"pack_id": "packA", "id": "dup", "node_type": "Document"}]


def test_get_nodes_by_id_missing_returns_empty_list():
    store = _store()
    assert store.get_nodes_by_id("nope") == []


def test_get_nodes_by_id_row_shape_matches_get_node_by_id():
    store = _store()
    store.upsert_node("Person", "p1", {"name": "Alice"}, space_id="resource")

    [node] = store.get_nodes_by_id("p1")
    single = store.get_node_by_id("p1")

    assert node == single
    assert node["node_type"] == "Person"
    assert node["space"] == "resource"


def test_export_nodes_and_edges_with_pack_filter():
    store = _store()
    store.upsert_node("Item", "a", {"pack_id": "p1"})
    store.upsert_node("Item", "b", {"pack_id": "p2"})
    store.upsert_edge("Item", "a", "rel", "Item", "b", {})

    all_nodes = store.export_nodes()
    assert len(all_nodes) == 2
    p1_nodes = store.export_nodes(pack_id="p1")
    assert len(p1_nodes) == 1
    assert p1_nodes[0]["props"]["id"] == "a"

    all_edges = store.export_edges()
    assert len(all_edges) == 1
    assert all_edges[0]["source_props"]["id"] == "a"
    assert all_edges[0]["target_props"]["id"] == "b"

    p2_edges = store.export_edges(pack_id="p2")
    assert len(p2_edges) == 1  # target node b carries pack_id=p2


def test_export_nodes_pack_id_and_space_pushdown_beyond_limit_boundary():
    """issue #54: pack_id + space together must not undercount when the
    matching (target-space) rows sort AFTER the limit boundary.

    Seeds one pack with 20 "noise"-space nodes inserted first, then 5
    "concept"-space nodes inserted last. With limit=10 and the old
    limit-before-filter behaviour, export_nodes(pack_id=..., limit=10) would
    fetch only the first 10 rows (all "noise") and a Python space post-filter
    would find zero matches -- undercounting 5 real matches down to 0. The
    fix pushes space into the WHERE clause ahead of LIMIT, so all 5 matches
    are returned regardless of scan order.
    """
    store = _store()
    for i in range(20):
        store.upsert_node("Item", f"a{i:02d}", {"pack_id": "p1"}, space_id="noise")
    for i in range(5):
        store.upsert_node("Item", f"z{i:02d}", {"pack_id": "p1"}, space_id="concept")

    rows = store.export_nodes(pack_id="p1", space="concept", limit=10)
    assert len(rows) == 5
    assert all(r["props"]["space"] == "concept" for r in rows)


def test_count_exported_nodes_not_capped_by_limit():
    """issue #54's actual complaint: `total` must reflect the true match
    count even when it EXCEEDS the caller's display `limit` -- not just
    "not undercounted below the real total while <= limit" (the previous
    test above). Seeds 30 matching nodes, asks export_nodes for only a
    limit=5 page, and asserts count_exported_nodes (no LIMIT) reports the
    full 30 -- something len(export_nodes(..., limit=5)) can never do
    since it is capped at 5 by construction."""
    store = _store()
    for i in range(30):
        store.upsert_node("Item", f"n{i:02d}", {"pack_id": "p1"}, space_id="concept")

    page = store.export_nodes(pack_id="p1", space="concept", limit=5)
    assert len(page) == 5  # display page still capped, as intended

    total = store.count_exported_nodes(pack_id="p1", space="concept")
    assert total == 30  # but the true count is not


def test_count_exported_nodes_query_uses_space_index_not_full_scan():
    """issue #54 audit finding [4]: adding count_exported_nodes doubles the
    number of queries ontology_list_nodes issues (one for the page, one for
    total). Measured against 250k rows / 200 packs x 3 spaces, the combined
    "(pack_id OR source OR source_id) AND space_id" predicate did a full
    `SCAN graph_nodes` (idx_nodes_pack alone can't help: SQLite won't turn a
    3-way OR across one indexed + two unindexed expressions into an index
    union) -- ~209ms per call at that scale. Adding idx_nodes_space (a
    plain column index, same idea as idx_edges_from/idx_edges_to) flips the
    plan to `SEARCH ... USING INDEX idx_nodes_space`, since space_id is
    always present in this call path and highly selective. This asserts the
    plan, not just correctness, so a future change that silently drops
    space_id from the WHERE clause (defeating the index) is caught here."""
    store = _store()
    conn = store._conn  # _SqliteGraphStoreDouble exposes the raw sqlite3 connection
    # Reuse the exact same WHERE builder count_exported_nodes calls (not a
    # hand-typed reconstruction) so this test's query can't silently drift
    # from what the implementation actually runs.
    where_sql, params = store._export_nodes_where("p1", "concept")
    sql = f"SELECT COUNT(*) FROM graph_nodes{where_sql}"
    plan = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    plan_text = " ".join(str(row) for row in plan)
    assert "SCAN graph_nodes" not in plan_text
    assert "idx_nodes_space" in plan_text


def test_search_nodes_keyword_pushed_ahead_of_any_scan_cap():
    """issue #86: HybridQuery.keyword_search used to call
    ``export_nodes(limit=_BM25_NODE_LIMIT)`` (50,000) and search only THOSE
    rows in Python -- on a 252k-row corpus, ~80% of nodes were silently
    unreachable by keyword search. This is #54's limit-before-filter class
    applied to the keyword predicate instead of pack_id/space.

    Seeds 60 "noise" nodes (no keyword match) inserted first, then 5
    keyword-matching nodes inserted last -- if search_nodes truncated the
    scan to some cap before matching (the old export_nodes-based approach,
    with any cap smaller than 60), these 5 would sort past the boundary and
    never be found. search_nodes pushes the keyword predicate into the SQL
    WHERE clause instead, so it finds all 5 regardless of scan order or
    corpus size."""
    store = _store()
    for i in range(60):
        store.upsert_node("Item", f"noise{i:03d}", {"name": f"unrelated {i}", "pack_id": "p1"})
    for i in range(5):
        store.upsert_node(
            "Item", f"hit{i:02d}", {"name": f"needle-in-haystack {i}", "pack_id": "p1"}
        )

    rows = store.search_nodes("needle", pack_ids=["p1"], limit=10)

    assert len(rows) == 5
    assert all("needle" in r["props"]["name"] for r in rows)


def test_search_nodes_space_filter_pushed_ahead_of_limit():
    """spaces is pushed into the same WHERE clause as the keyword predicate
    (both ahead of LIMIT), mirroring export_nodes' space pushdown (#54)."""
    store = _store()
    store.upsert_node(
        "Item", "n-claim", {"name": "shared term", "pack_id": "p1"}, space_id="claim"
    )
    store.upsert_node(
        "Item", "n-policy", {"name": "shared term", "pack_id": "p1"}, space_id="policy"
    )

    rows = store.search_nodes("shared", pack_ids=["p1"], spaces=["claim"], limit=10)

    assert len(rows) == 1
    assert rows[0]["props"]["space"] == "claim"


def test_search_nodes_escapes_like_wildcards():
    """A literal ``%``/``_`` in the search keyword must be matched literally,
    not interpreted as a SQL LIKE wildcard -- otherwise a keyword like
    "50%" would match every row instead of only rows containing "50%"."""
    store = _store()
    store.upsert_node("Item", "n-1", {"name": "discount 50% today", "pack_id": "p1"})
    store.upsert_node("Item", "n-2", {"name": "discount fifty percent today", "pack_id": "p1"})

    rows = store.search_nodes("50%", pack_ids=["p1"], limit=10)

    assert len(rows) == 1
    assert rows[0]["props"]["name"] == "discount 50% today"


def test_search_nodes_limit_zero_returns_empty_not_unbounded():
    """issue #86 boundary check (same class as issue #120's Mongo
    ``.limit(0)`` surprise): binding ``limit`` straight into SQL ``LIMIT
    :lim`` is dialect-dependent for non-positive values -- SQLite treats a
    NEGATIVE limit as "no limit at all" (unbounded), and PostgreSQL raises
    ``LIMIT must not be negative`` for the same input. search_nodes clamps
    ``limit<=0`` to an empty result up front so both dialects agree and
    neither a full unbounded scan nor a SQL error can happen."""
    store = _store()
    for i in range(5):
        store.upsert_node("Item", f"n{i}", {"name": "matches everything", "pack_id": "p1"})

    assert store.search_nodes("matches", pack_ids=["p1"], limit=0) == []


def test_search_nodes_negative_limit_returns_empty_not_unbounded_scan():
    """The dangerous half of the above: SQLite's ``LIMIT -1`` means
    UNBOUNDED, so a negative limit reaching the raw SQL unclamped would
    silently return every matching row instead of erroring or returning
    nothing -- the same shape of surprise issue #120 flagged for Mongo's
    ``.limit(0)``. Seeds enough matching rows that "unbounded" and "empty"
    are trivially distinguishable."""
    store = _store()
    for i in range(20):
        store.upsert_node("Item", f"n{i}", {"name": "matches everything", "pack_id": "p1"})

    assert store.search_nodes("matches", pack_ids=["p1"], limit=-1) == []


def test_search_nodes_field_injection_cannot_bypass_limit_or_leak_all_rows():
    """issue #86 bot finding (SQL injection, P2 per the bot / higher per
    reviewer): unlike ``keyword`` (a bound SQL parameter), each ``fields``
    entry was interpolated directly into a JSON path expression
    (``self._dialect.json_get``) with no escaping. A crafted field like
    ``"x')) LIKE '%' OR 1=1) --"`` closes the surrounding parens early,
    ORs in an always-true predicate, and comments out everything after it
    -- including the ``LIMIT`` clause -- so every row is returned
    regardless of ``limit``. ``fields`` is now validated against
    ``KEYWORD_SEARCH_FIELDS`` before it touches any SQL, so this raises
    ``ValueError`` instead of executing attacker-controlled SQL."""
    store = _store()
    for i in range(20):
        store.upsert_node("Item", f"n{i}", {"name": f"row {i}"})

    payload = ("x')) LIKE '%' OR 1=1) --",)
    with pytest.raises(ValueError, match="fields"):
        store.search_nodes("nomatch", pack_ids=["p1"], limit=2, fields=payload)


def test_search_nodes_rejects_field_with_apostrophe():
    """A field name containing a plain apostrophe (not even a crafted
    payload -- just a legitimate-looking typo) breaks out of the quoted
    JSON path literal and previously crashed with
    ``sqlite3.OperationalError`` instead of failing predictably. The
    whitelist rejects it before it ever reaches SQL."""
    store = _store()
    store.upsert_node("Item", "n1", {"o'clock": "irrelevant"})

    with pytest.raises(ValueError, match="fields"):
        store.search_nodes("irrelevant", pack_ids=["p1"], fields=("o'clock",))


def test_search_nodes_empty_fields_returns_empty_not_a_sql_error():
    """An empty ``fields`` tuple must not reach SQL as ``WHERE ()``
    (invalid syntax) -- "search zero fields" has exactly one sane meaning
    (nothing can ever match), so it short-circuits to ``[]``."""
    store = _store()
    store.upsert_node("Item", "n1", {"name": "anything"})

    assert store.search_nodes("anything", pack_ids=["p1"], fields=()) == []


def test_upsert_nodes_batch_and_edges_batch():
    store = _store()
    n = store.upsert_nodes_batch([
        {"node_type": "Item", "node_id": "a", "properties": {"x": 1}},
        {"node_type": "Item", "node_id": "b", "properties": {"x": 2}},
    ])
    assert n == 2
    e = store.upsert_edges_batch([
        {"from_type": "Item", "from_id": "a", "relation": "r", "to_type": "Item", "to_id": "b"},
    ])
    assert e == 1
    assert store.get_node("Item", "a")["x"] == 1
    assert len(store.find_by_relations("a", ["r"], direction="out")) == 1


def test_upsert_nodes_batch_empty_returns_zero():
    store = _store()
    assert store.upsert_nodes_batch([]) == 0
    assert store.upsert_edges_batch([]) == 0


def test_upsert_nodes_batch_normalizes_space_same_as_upsert_node():
    """Issue #118: upsert_nodes_batch builds its params dict inline instead
    of delegating to upsert_node, so it needs the identical
    _normalize_space reconciliation applied per node -- otherwise a batch
    caller could reintroduce the exact space_id/properties["space"]
    divergence upsert_node itself no longer allows. Precedence (codex
    review [2]): the explicit space_id ARGUMENT wins, matching Neo4j."""
    store = _store()
    store.upsert_nodes_batch([
        {
            "node_type": "Item", "node_id": "a",
            "properties": {"space": "claim"}, "space_id": "evidence",
        },
    ])
    row = store._conn.execute(
        "SELECT space_id, properties FROM graph_nodes WHERE node_id='a'"
    ).fetchone()
    assert row[0] == "evidence"  # the explicit argument wins, not the stale JSON key
    assert '"space": "evidence"' in row[1]


# ---------------------------------------------------------------------------
# Issue #118: space_id (column) vs properties["space"] (JSON) divergence
# ---------------------------------------------------------------------------


def test_upsert_node_normalizes_space_id_column_to_match_props_space():
    """Direct invariant check (belt-and-suspenders alongside the
    find_neighbors/export_nodes starvation regression tests): after
    upsert_node, the space_id COLUMN must equal the effective
    properties["space"] value -- the two can no longer diverge."""
    store = _store()
    store.upsert_node("Item", "a", {"space": "claim"}, space_id="evidence")
    row = store._conn.execute(
        "SELECT space_id, properties FROM graph_nodes WHERE node_id='a'"
    ).fetchone()
    assert row[0] == "evidence"
    assert '"space": "evidence"' in row[1]


def test_export_nodes_space_mismatch_reports_the_requested_space_not_the_stale_json():
    """Issue #118: export_nodes has no Python post-filter of its own (unlike
    ontology_list_nodes -- see test_mcp_dispatch_extended.py's
    test_space_mismatch_no_longer_desyncs_total_from_nodes for that end-to-
    end reproduction of the `total: N, nodes: []` symptom); its own
    correctness invariant is narrower but just as real: every row a
    space=X query returns must be LABELED space==X, and count_exported_nodes
    (SQL-only) must agree with len(export_nodes(..., a limit large enough
    to not truncate)). Pre-fix, a row selected by the column filter
    (space_id="target") could still be merged (via _merge_space, which
    reads whatever properties["space"] literally says) into a report
    claiming a DIFFERENT space entirely -- so a caller asking for
    space="target" got back rows self-labeled as "other".
    """
    store = _store()
    for i in range(3):
        store.upsert_node(
            "Item", f"mis{i}", {"pack_id": "p1", "space": "other"}, space_id="target"
        )
    store.upsert_node("Item", "real", {"pack_id": "p1"}, space_id="target")

    total = store.count_exported_nodes(pack_id="p1", space="target")
    page = store.export_nodes(pack_id="p1", space="target", limit=10)

    assert total == len(page) == 4
    assert all(r["props"]["space"] == "target" for r in page)
