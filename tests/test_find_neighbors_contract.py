"""
R6 characterization contract: pins ``find_neighbors()`` behavior across
LocalGraphStore, PGGraphStore (env-gated), and KuzuGraphStore (importorskip)
BEFORE the planned ``_expand`` extraction, so that refactor can be verified
by re-running this suite unchanged and getting the same green result.

This is NOT a cross-backend equivalence suite (test_pg_graph_doc_parity.py /
test_graph_pack_filter*.py already cover pack-filter parity in depth) — it
pins each backend's *own* current behavior, including one deliberate,
already-existing divergence (self-loop handling at depth=1, see
TestFindNeighborsEdge.test_self_loop_excludes_anchor_from_results): Kuzu's
depth==1 fast path (``_find_neighbors_1hop``) does not filter the anchor id
out of results the way Local/PG's BFS ``visited`` seed does, so a self-loop
edge makes Kuzu return the anchor node as its own "neighbour" while Local/PG
do not. Verified empirically against current code before writing this file
(see Stage 4 report) — not a bug this stage fixes, just a fact this stage
must not accidentally change.

Fixtures follow the project's existing gating conventions:
    - local: always runs (tmp_path sqlite file).
    - pg: skipped unless OPENCRAB_PG_TEST_URL is set (mirrors
      test_pg_graph_doc_parity.py / test_pg_stores_direct.py), own uuid
      schema per test, dropped in teardown.
    - kuzu: ``pytest.importorskip("ladybug")`` (mirrors
      test_graph_pack_filter_kuzu.py).
"""

from __future__ import annotations

import os
import uuid

import pytest

BACKENDS = ["local", "pg", "kuzu"]


def _make_local(tmp_path):
    from opencrab.stores.local_graph_store import LocalGraphStore

    return LocalGraphStore(str(tmp_path / "graph.db"))


def _make_pg():
    pg_url = os.environ.get("OPENCRAB_PG_TEST_URL")
    if not pg_url:
        pytest.skip("OPENCRAB_PG_TEST_URL not set — PG find_neighbors contract skipped")
    from opencrab.stores.pg_graph_store import PGGraphStore

    schema = f"t4fn_{uuid.uuid4().hex[:8]}"
    return PGGraphStore(pg_url, schema=schema), pg_url, schema


def _make_kuzu(tmp_path):
    pytest.importorskip("ladybug")
    from opencrab.stores.kuzu_graph_store import KuzuGraphStore

    return KuzuGraphStore(db_path=str(tmp_path / "graph_kuzu"))


@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path):
    """Yields (backend_name, store) for each of local/pg/kuzu.

    D1/D2 adopting this suite for the _expand extraction only need to keep
    this fixture (or their own equivalent) producing a fresh store per test
    — the seeding + assertions below are shared and backend-agnostic except
    where a backend name is checked explicitly (self-loop case).
    """
    name = request.param
    if name == "local":
        store = _make_local(tmp_path)
        yield name, store
        store.close()
    elif name == "pg":
        store, pg_url, schema = _make_pg()
        yield name, store
        store.close()
        from sqlalchemy import create_engine, text

        engine = create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
    else:
        store = _make_kuzu(tmp_path)
        yield name, store
        store.close()


# ---------------------------------------------------------------------------
# Shared graph-seeding helpers
# ---------------------------------------------------------------------------


def _seed_chain(store) -> None:
    """Linear chain n0 -> n1 -> n2 -> n3 -> n4 (relation 'next')."""
    for i in range(5):
        store.upsert_node("Chain", f"n{i}", {"name": f"n{i}"})
    for i in range(4):
        store.upsert_edge("Chain", f"n{i}", "next", "Chain", f"n{i + 1}", {})


def _seed_ring(store, size: int = 4) -> None:
    """Directed ring r0 -> r1 -> ... -> r(size-1) -> r0 (relation 'next')."""
    for i in range(size):
        store.upsert_node("Ring", f"r{i}", {"name": f"r{i}"})
    for i in range(size):
        store.upsert_edge("Ring", f"r{i}", "next", "Ring", f"r{(i + 1) % size}", {})


def _seed_hub(store, fanout: int = 5) -> None:
    store.upsert_node("Hub", "h", {"name": "h"})
    for i in range(fanout):
        store.upsert_node("Leaf", f"l{i}", {"name": f"l{i}"})
        store.upsert_edge("Hub", "h", "touches", "Leaf", f"l{i}", {})


def _ids(rows: list[dict]) -> set[str]:
    return {r["properties"].get("id") for r in rows}


# ---------------------------------------------------------------------------
# Normal — direction x depth combinations on a linear chain
# ---------------------------------------------------------------------------


class TestFindNeighborsNormal:
    def test_out_direction_depth1(self, backend):
        _name, store = backend
        _seed_chain(store)
        rows = store.find_neighbors("n1", direction="out", depth=1)
        assert _ids(rows) == {"n2"}

    def test_in_direction_depth1(self, backend):
        _name, store = backend
        _seed_chain(store)
        rows = store.find_neighbors("n1", direction="in", depth=1)
        assert _ids(rows) == {"n0"}

    def test_both_direction_depth1(self, backend):
        _name, store = backend
        _seed_chain(store)
        rows = store.find_neighbors("n1", direction="both", depth=1)
        assert _ids(rows) == {"n0", "n2"}

    def test_out_direction_depth2(self, backend):
        _name, store = backend
        _seed_chain(store)
        rows = store.find_neighbors("n0", direction="out", depth=2)
        assert _ids(rows) == {"n1", "n2"}
        by_id = {r["properties"]["id"]: r["depth"] for r in rows}
        assert by_id == {"n1": 1, "n2": 2}

    def test_in_direction_depth2(self, backend):
        _name, store = backend
        _seed_chain(store)
        rows = store.find_neighbors("n4", direction="in", depth=2)
        assert _ids(rows) == {"n3", "n2"}
        by_id = {r["properties"]["id"]: r["depth"] for r in rows}
        assert by_id == {"n3": 1, "n2": 2}

    def test_both_direction_depth2(self, backend):
        _name, store = backend
        _seed_chain(store)
        rows = store.find_neighbors("n2", direction="both", depth=2)
        assert _ids(rows) == {"n0", "n1", "n3", "n4"}
        by_id = {r["properties"]["id"]: r["depth"] for r in rows}
        assert by_id == {"n1": 1, "n3": 1, "n0": 2, "n4": 2}

    def test_result_shape_has_expected_keys(self, backend):
        _name, store = backend
        _seed_chain(store)
        rows = store.find_neighbors("n0", direction="out", depth=1)
        assert len(rows) == 1
        row = rows[0]
        assert set(row) == {
            "properties",
            "labels",
            "relation_type",
            "relationship_types",
            "depth",
        }
        assert row["labels"] == ["Chain"]
        assert row["relation_type"] == "next"
        assert row["relationship_types"] == ["next"]


# ---------------------------------------------------------------------------
# Error — unknown anchor id
# ---------------------------------------------------------------------------


class TestFindNeighborsError:
    def test_unknown_anchor_returns_empty_list_not_raise(self, backend):
        _name, store = backend
        _seed_chain(store)
        rows = store.find_neighbors("does-not-exist", direction="both", depth=2)
        assert rows == []

    def test_unknown_anchor_empty_graph_returns_empty_list(self, backend):
        _name, store = backend
        rows = store.find_neighbors("nothing-seeded", direction="both", depth=1)
        assert rows == []


# ---------------------------------------------------------------------------
# Edge — depth 0, self-loop, max-depth ring closure, limit boundary, pack filter
# ---------------------------------------------------------------------------


class TestFindNeighborsEdge:
    def test_depth_zero_returns_empty(self, backend):
        _name, store = backend
        _seed_chain(store)
        rows = store.find_neighbors("n1", direction="both", depth=0)
        assert rows == []

    def test_self_loop_excludes_anchor_from_results(self, backend):
        """Pins a real, pre-existing cross-backend divergence.

        Local/PG's BFS seeds ``visited = {node_id}`` before expanding, so a
        self-loop edge's destination (the anchor itself) is always filtered
        out. Kuzu's depth==1 path calls ``_find_neighbors_1hop`` directly
        without that anchor-exclusion check, so a self-loop DOES show up as
        a "neighbour" of itself. This is characterization, not a bug fix —
        the assertion branches on backend name because the two behaviors
        are genuinely different today.
        """
        name, store = backend
        store.upsert_node("Loop", "x", {"name": "x"})
        store.upsert_edge("Loop", "x", "self", "Loop", "x", {})

        rows = store.find_neighbors("x", direction="both", depth=1)
        if name == "kuzu":
            assert _ids(rows) == {"x"}
        else:
            assert rows == []

    def test_exactly_max_depth_ring_does_not_reintroduce_anchor(self, backend):
        _name, store = backend
        _seed_ring(store, size=4)
        rows = store.find_neighbors("r0", direction="out", depth=4)
        # The ring closes back to r0 exactly at hop 4; r0 (the anchor) must
        # not reappear as its own neighbour.
        assert _ids(rows) == {"r1", "r2", "r3"}
        by_id = {r["properties"]["id"]: r["depth"] for r in rows}
        assert by_id == {"r1": 1, "r2": 2, "r3": 3}

    def test_limit_boundary_caps_result_count(self, backend):
        _name, store = backend
        _seed_hub(store, fanout=5)
        rows = store.find_neighbors("h", direction="out", depth=1, limit=2)
        assert len(rows) == 2
        # Every returned id must be a real leaf of the hub (no fabricated rows).
        assert _ids(rows) <= {f"l{i}" for i in range(5)}

    def test_limit_larger_than_available_returns_all(self, backend):
        _name, store = backend
        _seed_hub(store, fanout=5)
        rows = store.find_neighbors("h", direction="out", depth=1, limit=50)
        assert _ids(rows) == {f"l{i}" for i in range(5)}

    def test_pack_ids_filter_excludes_foreign_and_unpackaged(self, backend):
        _name, store = backend
        store.upsert_node("Claim", "a", {"pack_id": "A"})
        store.upsert_node("Claim", "b", {"pack_id": "A"})
        store.upsert_node("Claim", "c", {})  # unpackaged
        store.upsert_edge("Claim", "a", "rel", "Claim", "b", {})
        store.upsert_edge("Claim", "a", "rel", "Claim", "c", {})

        rows = store.find_neighbors("a", pack_ids=["A"])
        assert _ids(rows) == {"b"}

    def test_include_unpackaged_allows_legacy_nodes(self, backend):
        _name, store = backend
        store.upsert_node("Claim", "a", {"pack_id": "A"})
        store.upsert_node("Claim", "b", {"pack_id": "A"})
        store.upsert_node("Claim", "c", {})  # unpackaged
        store.upsert_edge("Claim", "a", "rel", "Claim", "b", {})
        store.upsert_edge("Claim", "a", "rel", "Claim", "c", {})

        rows = store.find_neighbors("a", pack_ids=["A"], include_unpackaged=True)
        assert _ids(rows) == {"b", "c"}

    def test_anchor_outside_pack_filter_returns_empty(self, backend):
        _name, store = backend
        store.upsert_node("Claim", "a", {"pack_id": "B"})
        store.upsert_node("Claim", "b", {"pack_id": "A"})
        store.upsert_edge("Claim", "a", "rel", "Claim", "b", {})

        rows = store.find_neighbors("a", pack_ids=["A"])
        assert rows == []
