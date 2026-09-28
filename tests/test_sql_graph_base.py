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
    GraphPropertyCorruptionError,
    GraphPropertyValidationError,
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


def test_delete_edge_duplicate_key_row_is_corrupted_not_silently_deleted():
    """#402 rev.6: delete_edge decodes its ownership check with _as_dict
    (plain json.loads, no object_pairs_hook), which collapses a duplicate-key
    `properties` value to its LAST key -- direct-SQL corruption only, a
    normal upsert can never serialize duplicate keys. Pre-fix: an
    owner_pack_id matching the last-written key deletes the row even though
    parse_properties_object/decode_properties -- the shared decode contract
    every other pack-ownership write path already uses (upsert_edge,
    update_edge, upsert_node, update_node) -- would reject the row outright.
    Post-fix: decode_properties rejects the duplicate key and delete_edge
    raises fail-closed instead, matching update_edge's sibling pattern."""
    store = _store()
    store.upsert_node("Person", "a", {"pack_id": "victim-pack"})
    store.upsert_node("Person", "b", {"pack_id": "victim-pack"})
    store.upsert_edge("Person", "a", "knows", "Person", "b", {"pack_id": "victim-pack"})
    _corrupt_edge_properties(
        store, "a", "b",
        raw='{"pack_id": "victim-pack", "pack_id": "attacker-pack"}',
    )

    with pytest.raises(GraphPropertyCorruptionError):
        store.delete_edge("a", "knows", "b", owner_pack_id="attacker-pack")


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


# ---------------------------------------------------------------------------
# graph_schema_state / iter_graph_node_identities / get_node_identity_by_id (#404)
# ---------------------------------------------------------------------------


def test_graph_schema_state_matches_schema_kind():
    store = _store()
    assert store.graph_schema_state() == store._schema_kind()
    store._schema_state = "target"
    assert store.graph_schema_state() == "target"


def test_graph_schema_state_raises_when_unavailable():
    store = _store()
    store._available = False
    with pytest.raises(RuntimeError):
        store.graph_schema_state()


def test_iter_graph_node_identities_returns_empty_for_fresh_schema():
    # "unconfigured" maps to schema_kind "fresh" -- the short-circuit must
    # fire before any SELECT against graph_nodes runs (the table exists in
    # this double, but a real fresh store may not have it yet).
    store = _store()
    store.upsert_node("Person", "p1", {})
    store._schema_state = "unconfigured"
    assert list(store.iter_graph_node_identities()) == []


def test_iter_graph_node_identities_batch_size_non_positive_returns_empty():
    store = _store()
    store.upsert_node("Person", "p1", {})
    assert list(store.iter_graph_node_identities(batch_size=0)) == []
    assert list(store.iter_graph_node_identities(batch_size=-1)) == []


def test_iter_graph_node_identities_pages_via_multiple_fetch_calls():
    # Correctness alone (the boundary test below) can't distinguish paged
    # fetches from one unbounded fetchall that happens to return the right
    # rows -- count the underlying _fetch_all calls directly to prove this
    # is actually keyset pagination, not a single full-table read.
    store = _store()
    for i in range(5):
        store.upsert_node("Person", f"n{i}", {})

    calls: list[Any] = []
    original = store._fetch_all

    def counting_fetch_all(sql: str, params: dict[str, Any]) -> list[tuple]:
        calls.append(params.get("batch"))
        return original(sql, params)

    store._fetch_all = counting_fetch_all  # type: ignore[method-assign]

    list(store.iter_graph_node_identities(batch_size=2))

    # 5 rows at batch_size=2: pages of 2, 2, 1, then an empty terminating
    # fetch -- 4 calls. A single unbounded fetch would show only 2 (one
    # full read, one empty confirmation).
    assert len(calls) >= 3


def test_iter_graph_node_identities_streams_across_batch_boundaries():
    store = _store()
    ids = [f"n{i:02d}" for i in range(7)]
    for node_id in ids:
        store.upsert_node("Person", node_id, {"name": node_id})

    # batch_size smaller than the row count forces at least three pages.
    streamed = list(store.iter_graph_node_identities(batch_size=2))

    assert sorted(row.key.node_id for row in streamed) == sorted(ids)
    assert len(streamed) == len(ids)
    # Parity with a full scan: same key set, same normalized properties.
    full = store.inspect_graph_identity()
    assert {row.key for row in streamed} == {row.key for row in full.nodes}


def test_iter_graph_node_identities_does_not_drop_empty_string_node_id():
    # node_id="" is not blocked by the schema; the None-sentinel pagination
    # design must not silently skip it as an empty-string sentinel would
    # (#404 design-verification round 1 finding).
    store = _store()
    store._conn.execute(
        "INSERT INTO graph_nodes (node_type, node_id, space_id, properties) VALUES (?, ?, ?, ?)",
        ("Person", "", "space-a", "{}"),
    )
    store.upsert_node("Person", "p1", {})
    store._conn.commit()

    node_ids = {row.key.node_id for row in store.iter_graph_node_identities(batch_size=1)}

    assert node_ids == {"", "p1"}


def test_get_node_identity_by_id_missing_returns_none():
    store = _store()
    assert store.get_node_identity_by_id("nope") is None


def _corrupt_node_properties(store, node_id: str, raw: str = "[1, 2, 3]") -> None:
    """Directly writes a corrupted ``properties`` value for ``node_id``,
    bypassing ``upsert_node``'s validation (the only way to reach the
    corrupted-row paths this file's #402 tests exercise). The default is a
    JSON ARRAY, not truly malformed text: SQLite's own ``json_extract``
    raises ``OperationalError: malformed JSON`` for syntactically-broken
    text (e.g. ``"not json"``), so that shape can never reach the BFS SQL
    pushdown path at all -- it fails before Python ever sees the row. A JSON
    array is syntactically valid (``json_extract('[1,2,3]', '$.pack_id')``
    resolves to NULL, same as "key absent"), so SQL happily treats it as
    "no pack_id" while ``decode_properties``/``parse_properties_object``
    still correctly reject it (top-level value is not an object) -- this is
    exactly the shape the BFS leak (§10 item 6) is about. ``idx_nodes_pack``
    is an expression index over ``json_extract(properties, ...)`` -- SQLite
    recomputes it on every UPDATE and refuses non-JSON text outright, so it
    must be dropped first (same trick ``test_get_node_identity_by_id_reports
    _property_error_unlike_get_node`` already uses)."""
    store._conn.execute("DROP INDEX IF EXISTS idx_nodes_pack")
    store._conn.execute(
        "UPDATE graph_nodes SET properties = :raw WHERE node_id = :id", {"raw": raw, "id": node_id}
    )
    store._conn.commit()


def _corrupt_edge_properties(store, from_id: str, to_id: str, raw: str = "[1, 2, 3]") -> None:
    """Edge counterpart of ``_corrupt_node_properties`` (see its docstring
    for why the default is a JSON array, not malformed text). No expression
    index exists over ``graph_edges.properties`` (only ``idx_edges_from``/
    ``idx_edges_to``, both plain column indexes), so no DROP INDEX is
    needed here."""
    store._conn.execute(
        "UPDATE graph_edges SET properties = :raw WHERE from_id = :fid AND to_id = :tid",
        {"raw": raw, "fid": from_id, "tid": to_id},
    )
    store._conn.commit()


# ---------------------------------------------------------------------------
# Cluster B BFS corruption leak (#402, lead's critical correction, §10 item 6)
#
# Mechanism (see design.md §4.2): a corrupted node's `properties` column
# decodes to `{}`. Pre-fix, `_batch_node_props` merged that `{}` with the
# node's own (independently valid) `space_id` COLUMN via `_merge_space`,
# producing a truthy `{"space": ...}` dict that survived `_expand`'s
# `if not other_props: continue` skip. `_node_passes` then saw no `pack_id`
# key and returned `include_unpackaged` -- i.e. corruption disguised itself
# as "unpackaged" and leaked through whenever `include_unpackaged=True`.
# The analogous edge-side leak: a corrupted edge's properties also decode to
# `{}`, which has no `pack_id` key, so `_edge_passes` falls through to
# `src_passes and dst_passes` -- exposing the edge whenever both endpoints
# already pass, with no `include_unpackaged`-style opt-out.
#
# 6a/6c were run against the pre-#402-fix `_sql_graph_base.py` (git show
# 6e1fd76:opencrab/stores/_sql_graph_base.py, the commit immediately
# preceding this fix) to confirm RED before the fix restored them to GREEN;
# see the PR description for the exact commands and captured output.
# ---------------------------------------------------------------------------


def test_find_neighbors_corrupted_node_excluded_when_include_unpackaged_true():
    """6a: RED pre-fix (the corrupted node WAS reachable here whenever its
    space_id column was set -- see mechanism note above) -> GREEN post-fix
    (always excluded)."""
    store = _store()
    store.upsert_node("Hub", "hub", {"pack_id": "p1"})
    store.upsert_node("Item", "corrupt", {}, space_id="s1")
    store.upsert_edge("Hub", "hub", "touches", "Item", "corrupt")
    _corrupt_node_properties(store, "corrupt")

    res = store.find_neighbors("hub", direction="out", pack_ids=["p1"], include_unpackaged=True)
    # `to_id` (not `properties["id"]`) is the detector: the corrupted node's
    # leaked entry has properties `{"space": "s1"}` with NO "id" key at all
    # (the corruption wiped out the id `upsert_node` normally stamps), so a
    # `properties.get("id")`-based check would silently pass either way --
    # `to_id` is set by `_expand` from the raw node_id independent of
    # whatever the (possibly corrupted) properties decoded to.
    to_ids = {r["to_id"] for r in res}
    assert "corrupt" not in to_ids


def test_find_neighbors_corrupted_node_excluded_when_include_unpackaged_false():
    """6b control: include_unpackaged=False already excluded the corrupted
    node before this fix (no include_unpackaged escape hatch to leak
    through) and still does after -- unchanged behavior, no RED needed."""
    store = _store()
    store.upsert_node("Hub", "hub", {"pack_id": "p1"})
    store.upsert_node("Item", "corrupt", {}, space_id="s1")
    store.upsert_edge("Hub", "hub", "touches", "Item", "corrupt")
    _corrupt_node_properties(store, "corrupt")

    res = store.find_neighbors("hub", direction="out", pack_ids=["p1"], include_unpackaged=False)
    to_ids = {r["to_id"] for r in res}
    assert "corrupt" not in to_ids


def test_find_neighbors_corrupted_edge_always_excluded():
    """6c: RED pre-fix (a corrupted edge whose both endpoints already pass
    the pack filter WAS exposed -- _edge_passes saw no pack_id key on the
    decoded-to-{} edge and fell through to `src_passes and dst_passes`) ->
    GREEN post-fix (unconditionally excluded; no control group exists for
    this path, there is no include_unpackaged-style switch for edges)."""
    store = _store()
    store.upsert_node("Hub", "hub", {"pack_id": "p1"})
    store.upsert_node("Item", "leaf", {"pack_id": "p1"})
    store.upsert_edge("Hub", "hub", "touches", "Item", "leaf", {})
    _corrupt_edge_properties(store, "hub", "leaf")

    res = store.find_neighbors("hub", direction="out", pack_ids=["p1"], include_unpackaged=False)
    to_ids = {r["to_id"] for r in res}
    assert "leaf" not in to_ids


def test_find_neighbors_normal_node_and_edge_unaffected_control():
    """6d control: a normal, uncorrupted node/edge pair -- one packed, one
    unpackaged -- must surface in BFS results exactly the same whether or
    not the #402 fix is present, since decode_properties(valid_dict) is a
    passthrough with corrupted=False."""
    store = _store()
    store.upsert_node("Hub", "hub", {"pack_id": "p1"})
    store.upsert_node("Item", "packed", {"pack_id": "p1"})
    store.upsert_node("Item", "unpackaged", {})
    store.upsert_edge("Hub", "hub", "touches", "Item", "packed")
    store.upsert_edge("Hub", "hub", "touches", "Item", "unpackaged")

    res = store.find_neighbors("hub", direction="out", pack_ids=["p1"], include_unpackaged=True)
    ids = {r["properties"]["id"] for r in res}
    assert ids == {"packed", "unpackaged"}


def test_get_node_identity_by_id_matches_normal_row_shape():
    store = _store()
    store.upsert_node("Person", "p1", {"name": "Alice", "pack_id": "pack-x"}, space_id="space-a")

    row = store.get_node_identity_by_id("p1")

    assert row.key.node_type == "Person"
    assert row.key.node_id == "p1"
    assert row.space_id == "space-a"
    assert row.pack_id == "pack-x"
    assert row.property_error is None
    assert dict(row.normalized_properties) == {"name": "Alice"}


def test_get_node_identity_by_id_reports_property_error_unlike_get_node():
    # get_node_identity_by_id() reuses _node_inventory_row(), the same decode
    # path diagnose() relies on to reject a node as healable.
    store = _store()
    store.upsert_node("Person", "p1", {"name": "Alice"})
    # idx_nodes_pack is a json_extract() expression index: SQLite recomputes
    # it on every UPDATE to properties and refuses non-JSON text outright,
    # so the index must be dropped first to reach the malformed-data path
    # decode_raw_properties()/get_node_identity_by_id() exist to handle.
    store._conn.execute("DROP INDEX idx_nodes_pack")
    store._conn.execute(
        "UPDATE graph_nodes SET properties = 'not json' WHERE node_id = :id", {"id": "p1"}
    )
    store._conn.commit()

    row = store.get_node_identity_by_id("p1")
    assert row.property_error == "malformed_json"

    # Contrast (#402): the single-identity accessor now fails loud instead of
    # silently coercing to {} -- while the bulk/export-style accessor keeps
    # returning the row, only marked with property_decode_error.
    with pytest.raises(GraphPropertyCorruptionError):
        store.get_node("Person", "p1")
    node = store.get_node_by_id("p1")
    assert node["node_type"] == "Person"
    assert node["property_decode_error"] is True


# ---------------------------------------------------------------------------
# #402 alternative-review findings 1-4 (PR #420): design-verification rounds
# 1-3 plus lead arbitration (see design.md) landed on the following fixes.
# Findings 2-4 are proven against pre-fix code in the PR description's RED
# capture; finding 1's individual funnel tests below are the RED/GREEN
# evidence themselves (each raised nothing pre-fix, GraphPropertyValidation
# Error post-fix).
# ---------------------------------------------------------------------------


# --- finding 1: a fresh write must never define the property_decode_error
# marker key (a synthetic flag only a bulk/multi-row read path ever
# synthesizes for an already-corrupted row -- see decode_properties /
# get_nodes_by_id -- never a value a normal write stores). A write that let a
# caller set it would collide with that marker on the next bulk read.
#
# Scope note: the migration write paths (explicit-merge apply, migration-plan
# apply in update_node/update_nodes_batch's incident-edge handling and the
# dedicated migration-apply bodies) share the exact same
# normalize_node_properties()/normalize_edge_properties() guard exercised by
# the funnels below. They get no separate fixtures here: this file has no
# pre-existing migration-path test infrastructure to extend (that lives in
# tests/test_issue80_sql_graph.py, tests/test_issue80_migration.py, and
# tests/test_migrate_graph_identity_cli.py), and a second fixture would only
# re-exercise the identical shared branch at disproportionate setup cost.


def test_upsert_node_rejects_property_decode_error_marker():
    store = _store()
    with pytest.raises(GraphPropertyValidationError):
        store.upsert_node("Person", "p1", {"property_decode_error": True})


def test_update_node_rejects_property_decode_error_marker():
    store = _store()
    receipt = store.upsert_node("Person", "p1", {}, return_receipt=True)
    with pytest.raises(GraphPropertyValidationError):
        store.update_node("p1", receipt.digest, "Person", {"property_decode_error": True})


def test_upsert_nodes_batch_rejects_property_decode_error_marker():
    store = _store()
    with pytest.raises(GraphPropertyValidationError):
        store.upsert_nodes_batch([
            {"node_type": "Item", "node_id": "a", "properties": {"property_decode_error": True}},
        ])


def test_update_nodes_batch_rejects_property_decode_error_marker():
    store = _store()
    receipt = store.upsert_node("Item", "a", {}, return_receipt=True)
    with pytest.raises(GraphPropertyValidationError):
        store.update_nodes_batch([
            {
                "node_id": "a", "expected_current_digest": receipt.digest,
                "new_type": "Item", "new_properties": {"property_decode_error": True},
            },
        ])


def test_upsert_edge_rejects_property_decode_error_marker():
    store = _store()
    store.upsert_node("Person", "a", {})
    store.upsert_node("Person", "b", {})
    with pytest.raises(GraphPropertyValidationError):
        store.upsert_edge("Person", "a", "knows", "Person", "b", {"property_decode_error": True})


def test_update_edge_rejects_property_decode_error_marker():
    store = _store()
    store.upsert_node("Person", "a", {"pack_id": "p1"})
    store.upsert_node("Person", "b", {"pack_id": "p1"})
    receipt = store.upsert_edge(
        "Person", "a", "knows", "Person", "b", {"pack_id": "p1"}, return_receipt=True
    )
    with pytest.raises(GraphPropertyValidationError):
        store.update_edge(
            "Person", "a", "knows", "Person", "b",
            {"pack_id": "p1", "property_decode_error": True},
            expected_current_digest=receipt.digest, owner_pack_id="p1",
        )


def test_upsert_edges_batch_rejects_property_decode_error_marker():
    store = _store()
    store.upsert_node("Person", "a", {})
    store.upsert_node("Person", "b", {})
    with pytest.raises(GraphPropertyValidationError):
        store.upsert_edges_batch([
            {
                "from_type": "Person", "from_id": "a", "relation": "knows",
                "to_type": "Person", "to_id": "b",
                "properties": {"property_decode_error": True},
            },
        ])


def test_update_edges_batch_rejects_property_decode_error_marker():
    store = _store()
    store.upsert_node("Person", "a", {"pack_id": "p1"})
    store.upsert_node("Person", "b", {"pack_id": "p1"})
    receipt = store.upsert_edge(
        "Person", "a", "knows", "Person", "b", {"pack_id": "p1"}, return_receipt=True
    )
    with pytest.raises(GraphPropertyValidationError):
        store.update_edges_batch([
            {
                "from_type": "Person", "from_id": "a", "relation": "knows",
                "to_type": "Person", "to_id": "b",
                "properties": {"pack_id": "p1", "property_decode_error": True},
                "expected_current_digest": receipt.digest, "owner_pack_id": "p1",
            },
        ])


def test_get_edge_read_path_unaffected_by_marker_guard():
    """#402 finding 1, arbitration condition 2 (known limitation): a row that
    stored this key as a plain property before this fix shipped -- written
    here by direct SQL since no write path can produce it anymore -- must
    still be readable exactly as before. get_edge()'s own re-validation call
    to normalize_edge_properties() stays at the default
    reject_reserved_marker=False, so it is provably unaffected by the new
    opt-in check added only to the genuine write call sites above."""
    store = _store()
    store.upsert_node("Person", "a", {})
    store.upsert_node("Person", "b", {})
    store.upsert_edge("Person", "a", "knows", "Person", "b", {"pack_id": "p1"})
    store._conn.execute(
        "UPDATE graph_edges SET properties = :raw WHERE from_id='a' AND to_id='b'",
        {"raw": '{"pack_id": "p1", "property_decode_error": true}'},
    )
    store._conn.commit()

    props = store.get_edge("Person", "a", "knows", "Person", "b")
    assert props["property_decode_error"] is True


def test_backfill_pack_provenance_preserves_marker_key_documented_limitation():
    """#402 arbitration ruling (design-verification round 3 -> lead decision,
    option 2): backfill_pack_provenance() re-persists a node/edge's EXISTING
    properties unchanged except for ownership (pack_id/pack) -- its node
    branch never calls prepare_node()/normalize_node_properties(), and its
    edge branch rebuilds the persisted after_props from a plain
    dict(raw_current) copy rather than the normalized `current` value, so
    neither branch passes through the finding-1 guard. This is an accepted,
    documented exception, not a new collision vector: it takes no fresh
    `properties` payload from its caller, only an ownership assignment. A
    marker key already present on a row -- written here by direct SQL to
    stand in for a pre-fix row -- therefore survives a backfill call
    unchanged instead of being stripped or rejected; recovery of such a row
    stays manual and out of this fix's scope (see issue #416)."""
    import hashlib

    store = _store()
    store.upsert_node("Person", "p1", {})
    store._conn.execute(
        "UPDATE graph_nodes SET properties = :raw WHERE node_id='p1'",
        {"raw": '{"id": "p1", "property_decode_error": true}'},
    )
    store._conn.commit()
    target = store.graph_fingerprint()
    current_digest = store.get_node_digest("p1", node_type="Person")
    record = {
        "kind": "node", "target_fingerprint": target,
        "expected_current_digest": current_digest, "proposed_pack_id": "pack-x",
        "node_id": "p1", "node_type": "Person", "reason": "inferred",
        "dry_run_evidence_digest": hashlib.sha256(b"evidence").hexdigest(),
        "allowed_properties_delta": {"set": {"pack_id": "pack-x"}, "remove": []},
    }
    store.backfill_pack_provenance([record])

    row = store._conn.execute("SELECT properties FROM graph_nodes WHERE node_id='p1'").fetchone()
    assert '"property_decode_error": true' in row[0]
    assert '"pack_id": "pack-x"' in row[0]


# --- finding 2: delete_node() previously selected only node_type before
# deleting -- neither the node's own properties nor any incident edge's
# properties were ever inspected, so a corrupted row (or an edge alongside
# it) could be silently destroyed with no verification at all.


def test_delete_node_raises_on_corrupted_node_properties():
    store = _store()
    store.upsert_node("Person", "p1", {})
    _corrupt_node_properties(store, "p1")

    with pytest.raises(GraphPropertyCorruptionError):
        store.delete_node("Person", "p1")
    assert store._conn.execute("SELECT 1 FROM graph_nodes WHERE node_id='p1'").fetchone() is not None


def test_delete_node_raises_on_corrupted_incident_edge_properties():
    store = _store()
    store.upsert_node("Person", "a", {})
    store.upsert_node("Person", "b", {})
    store.upsert_edge("Person", "a", "knows", "Person", "b")
    _corrupt_edge_properties(store, "a", "b")

    with pytest.raises(GraphPropertyCorruptionError):
        store.delete_node("Person", "a")
    assert store._conn.execute("SELECT 1 FROM graph_nodes WHERE node_id='a'").fetchone() is not None
    assert store._conn.execute("SELECT 1 FROM graph_edges WHERE from_id='a'").fetchone() is not None


# --- finding 3: find_path() never selected/decoded the start node's, an
# intermediate node's, or an edge's properties at all -- a corrupted start
# node was invisible to it, a corrupted intermediate node was substituted
# with a bare {"id": nid} placeholder and the search kept going through it,
# and a corrupted edge was traversed exactly like a normal one.


def test_find_path_corrupted_start_node_returns_empty():
    store = _store()
    _make_chain(store, 2)
    _corrupt_node_properties(store, "n0")
    assert store.find_path("n0", "n2", max_depth=4) == []


def test_find_path_skips_corrupted_intermediate_node():
    store = _store()
    _make_chain(store, 3)  # n0 -> n1 -> n2 -> n3, single path through n1
    _corrupt_node_properties(store, "n1")
    assert store.find_path("n0", "n3", max_depth=4) == []


def test_find_path_skips_corrupted_edge():
    store = _store()
    _make_chain(store, 2)  # n0 -> n1 -> n2
    _corrupt_edge_properties(store, "n0", "n1")
    assert store.find_path("n0", "n2", max_depth=4) == []


# --- finding 4: _expand()'s corrupted-edge check used to live INSIDE the
# `if pack_set is not None:` branch, so the default
# find_neighbors(..., pack_ids=None) call path returned a corrupted edge as
# if it were a normal relationship. test_find_neighbors_corrupted_edge_
# always_excluded (6c, above) only exercises the pack_ids=["p1"] path, which
# this specific bug did NOT affect.


def test_find_neighbors_corrupted_edge_excluded_on_default_pack_ids_none_path():
    store = _store()
    store.upsert_node("Hub", "hub", {})
    store.upsert_node("Item", "leaf", {})
    store.upsert_edge("Hub", "hub", "touches", "Item", "leaf", {})
    _corrupt_edge_properties(store, "hub", "leaf")

    res = store.find_neighbors("hub", direction="out")  # pack_ids=None default
    to_ids = {r["to_id"] for r in res}
    assert "leaf" not in to_ids
