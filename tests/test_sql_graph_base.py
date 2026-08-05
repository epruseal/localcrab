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
        assert {"idx_nodes_pack", "idx_edges_from", "idx_edges_to"} <= indexes
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


def test_upsert_node_conflict_overwrites_not_merges():
    store = _store()
    store.upsert_node("Person", "p1", {"first_only": "x", "shared": "old"})
    store.upsert_node("Person", "p1", {"shared": "new"})
    node = store.get_node("Person", "p1")
    assert "first_only" not in node
    assert node["shared"] == "new"


def test_lookup_node_type():
    store = _store()
    store.upsert_node("Person", "p1", {})
    assert store.lookup_node_type("p1") == "Person"
    assert store.lookup_node_type("nope") is None


def test_lookup_node_type_returns_none_when_unavailable():
    store = _store()
    store._available = False
    assert store.lookup_node_type("p1") is None


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


def test_upsert_edge_conflict_overwrites_not_merges():
    store = _store()
    store.upsert_node("Person", "a", {})
    store.upsert_node("Person", "b", {})
    store.upsert_edge("Person", "a", "knows", "Person", "b", {"since": 2020})
    store.upsert_edge("Person", "a", "knows", "Person", "b", {"since": 2021})
    results = store.find_by_relations("a", ["knows"], direction="out")
    assert len(results) == 1  # no duplicate row from the second upsert


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
