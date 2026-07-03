"""Golden parity tests: PGGraphStore/PgDocStore vs LocalGraphStore/LocalSQLDocStore.

Skips entirely when OPENCRAB_PG_TEST_URL is not set (no PG dependency for the
default test run). Loads an identical synthetic dataset (hundreds of
nodes/edges, one high-degree hub, three packs) into both backends and asserts
their query methods return equivalent results.

ORDERING NOTE: neither SQLite nor PostgreSQL guarantee row order for
"WHERE from_id=? LIMIT n" without an ORDER BY — this is a pre-existing,
documented divergence in LocalGraphStore itself (see its find_neighbors()
docstring: Neo4j vs SQLite Jaccard ~96.5% when a LIMIT truncates a hub's
edge list). So: queries that return *complete* (untruncated) result sets are
compared as sorted sets/lists (exact equality); the one query that
deliberately exercises the hub fan-out cap (find_neighbors on the hub with
the default limit) is compared structurally (bounded size, valid members, no
duplicates) rather than as an exact identical subset.

Each test gets its own PG schema pair (dropped in teardown) for parallel-safe
isolation; LocalGraphStore/LocalSQLDocStore each get a fresh tmp_path sqlite
file.
"""

from __future__ import annotations

import os
import uuid

import pytest

PG_URL = os.environ.get("OPENCRAB_PG_TEST_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="OPENCRAB_PG_TEST_URL not set — PG parity tests skipped"
)

if PG_URL:
    from sqlalchemy import create_engine, text

    from opencrab.stores.local_graph_store import LocalGraphStore
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
    from opencrab.stores.pg_doc_store import PgDocStore
    from opencrab.stores.pg_graph_store import PGGraphStore

PACKS = ["packA", "packB", "packC"]
NODES_PER_PACK = 40
HUB_FANOUT_PER_PACK = 30  # 3 * 30 = 90 > default limit(50) -> exercises the cap


# ---------------------------------------------------------------------------
# Synthetic dataset
# ---------------------------------------------------------------------------


def build_graph_dataset():
    nodes: list[tuple[str, str, dict]] = []
    edges: list[tuple[str, str, str, str, str, dict]] = []

    for pack in PACKS:
        for i in range(NODES_PER_PACK):
            nid = f"{pack}_n{i}"
            nodes.append(("Person", nid, {"pack_id": pack, "name": nid}))
    for i in range(10):
        nodes.append(("Person", f"unpk_n{i}", {"name": f"unpk_n{i}"}))
    nodes.append(("Hub", "hub1", {"name": "hub1"}))  # unpackaged hub

    # Hub fan-out: high out-degree from the hub, exceeds default limit(50).
    for pack in PACKS:
        for i in range(HUB_FANOUT_PER_PACK):
            edges.append(("Hub", "hub1", "touches", "Person", f"{pack}_n{i}", {}))

    # Deterministic chain inside packA for find_path (unique shortest path).
    for i in range(5):
        edges.append(("Person", f"packA_n{i}", "next", "Person", f"packA_n{i + 1}", {}))

    # Cross-pack edge carrying its own pack_id (exercises 3-rule edge filter).
    edges.append(("Person", "packA_n0", "linked", "Person", "packB_n0", {"pack_id": "packA"}))

    # Edges into unpackaged nodes (exercises include_unpackaged).
    for i in range(5):
        edges.append(("Person", f"packA_n{i + 10}", "ref", "Person", f"unpk_n{i}", {}))

    return nodes, edges


def build_doc_dataset():
    doc_nodes: list[tuple[str, str, dict]] = []
    sources: list[tuple[str, str, dict]] = []
    for pack in PACKS:
        for i in range(20):
            nid = f"{pack}_d{i}"
            doc_nodes.append(("Doc", nid, {"pack_id": pack, "title": nid}))

    keywords = {
        "packA": "postgresql database engine",
        "packB": "kubernetes container orchestration",
        "packC": "machine learning AI inference",  # "AI" (2 chars) feeds the short-token test
    }
    for pack in PACKS:
        for i in range(30):
            sid = f"{pack}_src{i}"
            text = f"{keywords[pack]} document number {i} about {pack} systems and SQL tips"
            sources.append((sid, text, {"node_id": f"{pack}_d{i % 20}", "pack_id": pack}))
    return doc_nodes, sources


def load_graph(store, nodes, edges):
    for node_type, node_id, props in nodes:
        store.upsert_node(node_type, node_id, props)
    for from_type, from_id, relation, to_type, to_id, props in edges:
        store.upsert_edge(from_type, from_id, relation, to_type, to_id, props)


def load_docs(store, space, doc_nodes, sources):
    for node_type, node_id, props in doc_nodes:
        store.upsert_node_doc(space, node_type, node_id, props)
    for source_id, text, meta in sources:
        store.upsert_source(source_id, text, meta)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_engine():
    engine = create_engine(PG_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def graph_pair(tmp_path, pg_engine):
    schema = f"t{uuid.uuid4().hex[:12]}_g"
    local = LocalGraphStore(str(tmp_path / "graph.db"))
    pg = PGGraphStore(pg_engine, schema=schema)
    nodes, edges = build_graph_dataset()
    load_graph(local, nodes, edges)
    load_graph(pg, nodes, edges)
    yield local, pg
    local.close()
    with pg_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.fixture
def doc_pair(tmp_path, pg_engine):
    schema = f"t{uuid.uuid4().hex[:12]}_d"
    local = LocalSQLDocStore(str(tmp_path / "doc.db"))
    pg = PgDocStore(pg_engine, schema=schema)
    doc_nodes, sources = build_doc_dataset()
    load_docs(local, "s1", doc_nodes, sources)
    load_docs(pg, "s1", doc_nodes, sources)
    yield local, pg
    local.close()
    with pg_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------


def _neighbor_key(item):
    return (item["properties"].get("id"), item["relation_type"], item["depth"])


def sorted_neighbors(items):
    return sorted(items, key=_neighbor_key)


def _pid(d):
    return d.get("id")


# ---------------------------------------------------------------------------
# Graph parity
# ---------------------------------------------------------------------------


class TestGraphParity:
    def test_availability_and_ping(self, graph_pair):
        local, pg = graph_pair
        assert local.available and pg.available
        assert local.ping() and pg.ping()

    def test_get_node_and_lookup_type(self, graph_pair):
        local, pg = graph_pair
        assert local.get_node("Person", "packA_n0") == pg.get_node("Person", "packA_n0")
        assert local.lookup_node_type("packA_n0") == pg.lookup_node_type("packA_n0")
        assert local.get_node("Person", "nope") is None
        assert pg.get_node("Person", "nope") is None

    def test_get_node_by_id(self, graph_pair):
        local, pg = graph_pair
        assert local.get_node_by_id("packB_n5") == pg.get_node_by_id("packB_n5")

    def test_count_nodes(self, graph_pair):
        local, pg = graph_pair
        assert local.count_nodes() == pg.count_nodes()
        assert local.count_nodes("Person") == pg.count_nodes("Person")
        assert local.count_nodes("Hub") == pg.count_nodes("Hub") == 1

    def test_list_packs(self, graph_pair):
        local, pg = graph_pair
        local_packs = sorted(local.list_packs(), key=lambda r: r["pack_id"])
        pg_packs = sorted(pg.list_packs(), key=lambda r: r["pack_id"])
        assert local_packs == pg_packs
        assert {p["pack_id"] for p in local_packs} == set(PACKS)

    def test_run_cypher_noop(self, graph_pair):
        local, pg = graph_pair
        assert local.run_cypher("MATCH (n) RETURN n") == []
        assert pg.run_cypher("MATCH (n) RETURN n") == []

    @pytest.mark.parametrize("depth", [1, 2, 3])
    @pytest.mark.parametrize(
        "pack_ids,include_unpackaged",
        [
            (None, False),
            (["packA"], False),
            (["packA", "packB"], False),
            (["packA"], True),
        ],
    )
    def test_find_neighbors_small_degree_full_parity(
        self, graph_pair, depth, pack_ids, include_unpackaged
    ):
        """Anchor with low degree -> result never truncated by limit -> exact
        set parity is expected (no ordering ambiguity from the LIMIT cap)."""
        local, pg = graph_pair
        kwargs = dict(
            direction="both", depth=depth, limit=1000,
            pack_ids=pack_ids, include_unpackaged=include_unpackaged,
        )
        local_res = sorted_neighbors(local.find_neighbors("packA_n0", **kwargs))
        pg_res = sorted_neighbors(pg.find_neighbors("packA_n0", **kwargs))
        assert local_res == pg_res

    def test_find_neighbors_hub_fanout_cap(self, graph_pair):
        """Hub out-degree (90) > default limit(50): both backends must cap at
        the limit, return valid distinct 1-hop neighbours, but are NOT
        required to pick the identical subset (pre-existing, documented
        cross-backend divergence — see module docstring)."""
        local, pg = graph_pair
        local_res = local.find_neighbors("hub1", direction="out", depth=1, limit=50)
        pg_res = pg.find_neighbors("hub1", direction="out", depth=1, limit=50)
        assert len(local_res) == 50
        assert len(pg_res) == 50
        for res in (local_res, pg_res):
            ids = [r["properties"]["id"] for r in res]
            assert len(ids) == len(set(ids)), "duplicate neighbour in capped result"
            assert all(r["depth"] == 1 for r in res)
            assert all(r["relation_type"] == "touches" for r in res)
            assert all(r["properties"]["id"].split("_n")[0] in PACKS for r in res)

    def test_find_neighbors_anchor_fails_pack_filter(self, graph_pair):
        local, pg = graph_pair
        kwargs = dict(direction="both", depth=1, limit=50, pack_ids=["packB"])
        assert local.find_neighbors("hub1", **kwargs) == []
        assert pg.find_neighbors("hub1", **kwargs) == []

    def test_find_path(self, graph_pair):
        local, pg = graph_pair
        local_path = local.find_path("packA_n0", "packA_n5", max_depth=4)
        pg_path = pg.find_path("packA_n0", "packA_n5", max_depth=4)
        assert local_path == pg_path
        assert [step["relation"] for step in local_path] == ["next"] * 5

    def test_find_path_no_path(self, graph_pair):
        local, pg = graph_pair
        assert local.find_path("unpk_n0", "packC_n39", max_depth=2) == []
        assert pg.find_path("unpk_n0", "packC_n39", max_depth=2) == []

    def test_find_by_relations(self, graph_pair):
        local, pg = graph_pair
        local_res = sorted(
            local.find_by_relations("packA_n0", ["next", "linked"], direction="out"),
            key=lambda r: (r["properties"]["id"], r["relation_type"]),
        )
        pg_res = sorted(
            pg.find_by_relations("packA_n0", ["next", "linked"], direction="out"),
            key=lambda r: (r["properties"]["id"], r["relation_type"]),
        )
        assert local_res == pg_res
        assert len(local_res) == 2  # "next" -> packA_n1, "linked" -> packB_n0

    def test_export_nodes_and_edges(self, graph_pair):
        local, pg = graph_pair
        local_nodes = sorted(local.export_nodes(), key=lambda r: _pid(r["props"]))
        pg_nodes = sorted(pg.export_nodes(), key=lambda r: _pid(r["props"]))
        assert local_nodes == pg_nodes

        local_nodes_p = sorted(
            local.export_nodes(pack_id="packA"), key=lambda r: _pid(r["props"])
        )
        pg_nodes_p = sorted(
            pg.export_nodes(pack_id="packA"), key=lambda r: _pid(r["props"])
        )
        assert local_nodes_p == pg_nodes_p

        def edge_key(r):
            return (_pid(r["source_props"]), _pid(r["target_props"]), r["relation"])

        local_edges = sorted(local.export_edges(), key=edge_key)
        pg_edges = sorted(pg.export_edges(), key=edge_key)
        assert local_edges == pg_edges

        local_edges_p = sorted(local.export_edges(pack_id="packA"), key=edge_key)
        pg_edges_p = sorted(pg.export_edges(pack_id="packA"), key=edge_key)
        assert local_edges_p == pg_edges_p

    def test_upsert_node_batch_and_delete_node_quirk(self, graph_pair):
        """delete_node()'s return value reflects whether *edges* were removed
        (a reused-cursor rowcount quirk in LocalGraphStore) — verify both
        backends replicate it identically."""
        local, pg = graph_pair
        for store in (local, pg):
            store.upsert_nodes_batch(
                [{"node_type": "Person", "node_id": "lonely1", "properties": {}}]
            )
            store.upsert_edges_batch(
                [{
                    "from_type": "Person", "from_id": "packA_n0", "relation": "extra",
                    "to_type": "Person", "to_id": "lonely1", "properties": {},
                }]
            )
            # lonely1 has one incident edge -> edges DELETE affects 1 row -> True
            assert store.delete_node("Person", "lonely1") is True
            # unpk_n9 has zero incident edges -> edges DELETE affects 0 rows -> False
            assert store.delete_node("Person", "unpk_n9") is False


# ---------------------------------------------------------------------------
# Doc parity
# ---------------------------------------------------------------------------


class TestDocParity:
    def test_availability_and_ping(self, doc_pair):
        local, pg = doc_pair
        assert local.available and pg.available
        assert local.ping() and pg.ping()

    def test_get_and_list_nodes(self, doc_pair):
        local, pg = doc_pair
        assert local.get_node_doc("s1", "packA_d0")["properties"] == \
            pg.get_node_doc("s1", "packA_d0")["properties"]
        local_ids = sorted(n["node_id"] for n in local.list_nodes("s1", limit=1000))
        pg_ids = sorted(n["node_id"] for n in pg.list_nodes("s1", limit=1000))
        assert local_ids == pg_ids

    def test_upsert_and_delete_node_doc(self, doc_pair):
        local, pg = doc_pair
        for store in (local, pg):
            store.upsert_node_doc("s1", "Doc", "newnode", {"x": 1})
            assert store.get_node_doc("s1", "newnode")["properties"] == {"x": 1}
            assert store.delete_node_doc("s1", "newnode") is True
            assert store.get_node_doc("s1", "newnode") is None
            assert store.delete_node_doc("s1", "newnode") is False

    def test_bm25_fingerprint_count_matches(self, doc_pair):
        local, pg = doc_pair
        local_count, _ = local.bm25_fingerprint()
        pg_count, _ = pg.bm25_fingerprint()
        assert local_count == pg_count

    def test_sources_get_and_list(self, doc_pair):
        local, pg = doc_pair
        assert local.get_source("packA_src0")["text"] == pg.get_source("packA_src0")["text"]
        local_sids = sorted(s["source_id"] for s in local.list_sources(limit=1000))
        pg_sids = sorted(s["source_id"] for s in pg.list_sources(limit=1000))
        assert local_sids == pg_sids

    def test_collection_stats(self, doc_pair):
        local, pg = doc_pair
        assert local.collection_stats() == pg.collection_stats()

    def test_audit_log_sequence_parity(self, doc_pair):
        local, pg = doc_pair
        events = [
            ("ingest", "packA_d0", {"n": 1}),
            ("ingest", "packB_d0", {"n": 2}),
            ("delete", "packC_d0", {"n": 3}),
        ]
        for store in (local, pg):
            for etype, subj, details in events:
                store.log_event(etype, subj, details)

        def strip(entries):
            return [(e["event_type"], e["subject_id"], e["details"]) for e in entries]

        local_log = strip(local.get_audit_log(limit=10))
        pg_log = strip(pg.get_audit_log(limit=10))
        assert local_log == pg_log
        assert local_log[0] == ("delete", "packC_d0", {"n": 3})  # most recent first

    def test_keyword_search_supported(self, doc_pair):
        local, pg = doc_pair
        assert local.supports_keyword
        assert pg.supports_keyword

    def test_keyword_search_top_set_overlap(self, doc_pair):
        """FTS5 (SQLite) vs tsvector/ts_rank (PG) rank differently in detail,
        so we assert meaningful top-K overlap rather than identical order —
        matches the task's 'top-set overlap' acceptance bar."""
        local, pg = doc_pair
        for query in ["postgresql database", "kubernetes container", "machine learning"]:
            local_hits = local.keyword_search(query, limit=10)
            pg_hits = pg.keyword_search(query, limit=10)
            assert local_hits and pg_hits
            local_ids = {h["source_id"] for h in local_hits}
            pg_ids = {h["source_id"] for h in pg_hits}
            overlap = local_ids & pg_ids
            assert len(overlap) >= 3, (
                f"low top-10 overlap for {query!r}: local={local_ids} pg={pg_ids}"
            )

    def test_keyword_search_short_token_fallback(self, doc_pair):
        """A token strictly shorter than 3 chars ('AI') forces PG's
        ILIKE + pg_trgm fallback leg (the tsvector leg only matches whole
        normalised lexemes). Both backends must still return packC docs."""
        local, pg = doc_pair
        local_hits = local.keyword_search("AI", limit=10)
        pg_hits = pg.keyword_search("AI", limit=10)
        assert local_hits and pg_hits
        assert all(h["metadata"]["pack_id"] == "packC" for h in local_hits)
        assert all(h["metadata"]["pack_id"] == "packC" for h in pg_hits)

    def test_keyword_search_pack_filter(self, doc_pair):
        local, pg = doc_pair
        for query in ["document systems"]:
            local_hits = local.keyword_search(query, pack_ids=["packA"], limit=50)
            pg_hits = pg.keyword_search(query, pack_ids=["packA"], limit=50)
            assert local_hits and pg_hits
            assert all(h["metadata"]["pack_id"] == "packA" for h in local_hits)
            assert all(h["metadata"]["pack_id"] == "packA" for h in pg_hits)
