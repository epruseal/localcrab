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
from datetime import UTC, datetime

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
        """max_depth is a HOP bound (unified B1 contract) — the dataset's
        chain is 5 hops long, so max_depth must be >= 5 to find it."""
        local, pg = graph_pair
        local_path = local.find_path("packA_n0", "packA_n5", max_depth=5)
        pg_path = pg.find_path("packA_n0", "packA_n5", max_depth=5)
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

    def test_delete_node_returns_true_when_node_deleted(self, graph_pair):
        """delete_node()'s return value reflects whether the NODE itself was
        deleted (unified B2 contract) — verify both backends agree,
        regardless of incident edge count."""
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
            # lonely1 has one incident edge and exists -> True
            assert store.delete_node("Person", "lonely1") is True
            # unpk_n9 has zero incident edges but exists -> True
            assert store.delete_node("Person", "unpk_n9") is True
            # already deleted -> False
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

    def test_keyword_search_korean_corpus_top_set_overlap(self, tmp_path, pg_engine):
        """FTS5 unicode61 (SQLite) vs to_tsvector('simple', ...) (PG) tokenize
        CJK differently in general, but for whitespace-segmented Korean words
        (this app's actual corpus shape) both should still retrieve the same
        documents — asserted as set-membership overlap, matching the ASCII
        keyword test's acceptance bar above."""
        schema = f"t{uuid.uuid4().hex[:12]}_kr"
        local = LocalSQLDocStore(str(tmp_path / "kr_doc.db"))
        pg = PgDocStore(pg_engine, schema=schema)
        try:
            docs = [
                ("kr_src0", "인공지능 기계학습 자연어처리 연구 문서", {"node_id": "kr_d0"}),
                ("kr_src1", "인공지능 딥러닝 신경망 모델 학습", {"node_id": "kr_d1"}),
                ("kr_src2", "데이터베이스 트랜잭션 격리 수준 설명", {"node_id": "kr_d2"}),
            ]
            for sid, text_, meta in docs:
                local.upsert_source(sid, text_, meta)
                pg.upsert_source(sid, text_, meta)

            local_hits = local.keyword_search("인공지능 학습", limit=10)
            pg_hits = pg.keyword_search("인공지능 학습", limit=10)
            assert local_hits and pg_hits
            local_ids = {h["source_id"] for h in local_hits}
            pg_ids = {h["source_id"] for h in pg_hits}
            assert local_ids & pg_ids, (
                f"no top-10 overlap for Korean query: local={local_ids} pg={pg_ids}"
            )
        finally:
            local.close()
            with pg_engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    def test_doc_node_updated_at_roundtrip_same_logical_instant(self, doc_pair):
        """SQLite stores updated_at as an ISO-8601 string
        (``datetime.now(UTC).isoformat()``); PG stores it as TIMESTAMPTZ and
        stringifies it back via ``.isoformat()`` on read. Both must parse as
        timezone-aware UTC instants captured around the same wall-clock
        moment (this test's own upsert call), not merely as opaque strings."""
        local, pg = doc_pair
        local.upsert_node_doc("s1", "Doc", "ts_probe", {"x": 1})
        pg.upsert_node_doc("s1", "Doc", "ts_probe", {"x": 1})
        local_ts = datetime.fromisoformat(local.get_node_doc("s1", "ts_probe")["updated_at"])
        pg_ts = datetime.fromisoformat(pg.get_node_doc("s1", "ts_probe")["updated_at"])
        assert local_ts.tzinfo is not None and pg_ts.tzinfo is not None
        now = datetime.now(UTC)
        assert abs((now - local_ts).total_seconds()) < 10
        assert abs((now - pg_ts).total_seconds()) < 10


# ---------------------------------------------------------------------------
# Typed properties / NULL semantics / unicode / upsert-overwrite / pack_id
# filter edge cases (golden contract additions — R7 pre-unification hardening)
# ---------------------------------------------------------------------------


class TestTypedPropertiesAndNullSemantics:
    def test_typed_properties_roundtrip_graph(self, graph_pair):
        """int/float/bool/None/nested dict/list all roundtrip through
        upsert_node/get_node with identical Python types and values on both
        backends (both go through a full JSON encode/decode of the whole
        properties blob, so no per-scalar type coercion applies here — unlike
        the pack_id GROUP BY projection in list_packs(), see the dedicated
        divergence test below)."""
        local, pg = graph_pair
        props = {
            "count": 7,
            "ratio": 3.14,
            "flag_true": True,
            "flag_false": False,
            "nothing": None,
            "nested": {"a": 1, "b": [1, 2, 3], "c": {"d": None}},
            "items": [1, "two", 3.0, None, True],
        }
        for store in (local, pg):
            store.upsert_node("TypedProbe", "typed1", props)
        local_node = local.get_node("TypedProbe", "typed1")
        pg_node = pg.get_node("TypedProbe", "typed1")
        for key, value in props.items():
            assert local_node[key] == value
            assert pg_node[key] == value
        assert local_node == pg_node

    def test_null_value_vs_missing_key_distinction(self, graph_pair):
        local, pg = graph_pair
        for store in (local, pg):
            store.upsert_node("NullProbe", "null1", {"present_null": None})
        local_node = local.get_node("NullProbe", "null1")
        pg_node = pg.get_node("NullProbe", "null1")
        for node in (local_node, pg_node):
            assert "present_null" in node
            assert node["present_null"] is None
            assert "absent_key" not in node

    def test_unicode_korean_and_emoji_roundtrip(self, graph_pair):
        local, pg = graph_pair
        node_id = "한국어_노드_🐛"
        props = {"name": "한글 이름 테스트", "emoji": "🦀🔥✨", "mixed": "hello 안녕 👋"}
        for store in (local, pg):
            store.upsert_node("UnicodeProbe", node_id, props)
        local_node = local.get_node("UnicodeProbe", node_id)
        pg_node = pg.get_node("UnicodeProbe", node_id)
        for key, value in props.items():
            assert local_node[key] == value
            assert pg_node[key] == value
        assert local.get_node_by_id(node_id)["id"] == node_id
        assert pg.get_node_by_id(node_id)["id"] == node_id

    def test_upsert_conflict_overwrites_not_merges(self, graph_pair):
        """Second upsert_node() replaces the properties column wholesale
        (``ON CONFLICT DO UPDATE SET properties = excluded/EXCLUDED``); a key
        present only in the first write must NOT survive the second."""
        local, pg = graph_pair
        for store in (local, pg):
            store.upsert_node("OverwriteProbe", "ow1", {"first_only": "x", "shared": "old"})
            store.upsert_node("OverwriteProbe", "ow1", {"shared": "new"})
        for store in (local, pg):
            node = store.get_node("OverwriteProbe", "ow1")
            assert "first_only" not in node
            assert node["shared"] == "new"

    def test_edge_upsert_conflict_overwrites_not_merges(self, graph_pair):
        """Second upsert_edge() on the same (from_type, from_id, relation,
        to_type, to_id) key replaces the properties column wholesale — a key
        present only in the first write must NOT survive the second, on
        either backend."""
        local, pg = graph_pair
        for store in (local, pg):
            store.upsert_edge(
                "Person", "packA_n0", "edgeprobe", "Person", "packA_n1",
                {"first_only": "x", "shared": "old"},
            )
            store.upsert_edge(
                "Person", "packA_n0", "edgeprobe", "Person", "packA_n1",
                {"shared": "new"},
            )
        for store in (local, pg):
            edges = [
                e for e in store.export_edges()
                if e["relation"] == "edgeprobe"
                and e["source_props"].get("id") == "packA_n0"
                and e["target_props"].get("id") == "packA_n1"
            ]
            assert len(edges) == 1  # no duplicate edge row from the second upsert
            assert "first_only" not in edges[0]["rel_props"]
            assert edges[0]["rel_props"]["shared"] == "new"

    def test_find_neighbors_empty_pack_ids_equals_none(self, graph_pair):
        """``pack_ids=[]`` (empty list) must behave identically to
        ``pack_ids=None`` on both backends — both are falsy in Python, so the
        BFS's ``pack_set = set(pack_ids) if pack_ids else None`` guard treats
        them the same (no filtering applied)."""
        local, pg = graph_pair
        kwargs_common = dict(direction="both", depth=1, limit=1000)
        local_none = sorted_neighbors(local.find_neighbors("packA_n0", pack_ids=None, **kwargs_common))
        local_empty = sorted_neighbors(local.find_neighbors("packA_n0", pack_ids=[], **kwargs_common))
        pg_none = sorted_neighbors(pg.find_neighbors("packA_n0", pack_ids=None, **kwargs_common))
        pg_empty = sorted_neighbors(pg.find_neighbors("packA_n0", pack_ids=[], **kwargs_common))
        assert local_none == local_empty
        assert pg_none == pg_empty
        assert local_none == pg_none

    # ------------------------------------------------------------------
    # Doc-store equivalents of the graph-store typed-properties/NULL/
    # unicode/upsert-overwrite cases above (Stage 6a doc-store dialect
    # unification golden contract — these were previously only exercised
    # for the graph pair, not the doc pair).
    # ------------------------------------------------------------------

    def test_typed_properties_roundtrip_doc(self, doc_pair):
        """Same JSON-typed-value roundtrip as
        test_typed_properties_roundtrip_graph, but through
        upsert_node_doc/get_node_doc (doc_nodes.properties)."""
        local, pg = doc_pair
        props = {
            "count": 7,
            "ratio": 3.14,
            "flag_true": True,
            "flag_false": False,
            "nothing": None,
            "nested": {"a": 1, "b": [1, 2, 3], "c": {"d": None}},
            "items": [1, "two", 3.0, None, True],
        }
        for store in (local, pg):
            store.upsert_node_doc("s1", "Doc", "typed_doc1", props)
        local_props = local.get_node_doc("s1", "typed_doc1")["properties"]
        pg_props = pg.get_node_doc("s1", "typed_doc1")["properties"]
        for key, value in props.items():
            assert local_props[key] == value
            assert pg_props[key] == value
        assert local_props == pg_props

    def test_null_value_vs_missing_key_distinction_doc(self, doc_pair):
        local, pg = doc_pair
        for store in (local, pg):
            store.upsert_node_doc("s1", "Doc", "null_doc1", {"present_null": None})
        for store in (local, pg):
            props = store.get_node_doc("s1", "null_doc1")["properties"]
            assert "present_null" in props
            assert props["present_null"] is None
            assert "absent_key" not in props

    def test_unicode_korean_and_emoji_roundtrip_doc(self, doc_pair):
        """Unicode in both the doc node_id and the source content/metadata."""
        local, pg = doc_pair
        node_id = "한국어_문서_🐛"
        props = {"title": "한글 제목 테스트", "emoji": "🦀🔥✨", "mixed": "hello 안녕 👋"}
        for store in (local, pg):
            store.upsert_node_doc("s1", "Doc", node_id, props)
        local_doc = local.get_node_doc("s1", node_id)
        pg_doc = pg.get_node_doc("s1", node_id)
        for key, value in props.items():
            assert local_doc["properties"][key] == value
            assert pg_doc["properties"][key] == value

        source_id = "소스_🦀_src"
        text_ = "한국어 문서 검색 테스트 unicode emoji 🔥 content"
        for store in (local, pg):
            store.upsert_source(source_id, text_, {"lang": "ko", "tag": "🐛"})
        local_src = local.get_source(source_id)
        pg_src = pg.get_source(source_id)
        assert local_src["text"] == text_ == pg_src["text"]
        assert local_src["metadata"] == pg_src["metadata"] == {"lang": "ko", "tag": "🐛"}

    def test_upsert_conflict_overwrites_not_merges_doc(self, doc_pair):
        """Second upsert_node_doc()/upsert_source() replaces the JSON column
        wholesale — a key present only in the first write must NOT survive."""
        local, pg = doc_pair
        for store in (local, pg):
            store.upsert_node_doc("s1", "Doc", "ow_doc1", {"first_only": "x", "shared": "old"})
            store.upsert_node_doc("s1", "Doc", "ow_doc1", {"shared": "new"})
        for store in (local, pg):
            props = store.get_node_doc("s1", "ow_doc1")["properties"]
            assert "first_only" not in props
            assert props["shared"] == "new"

        for store in (local, pg):
            store.upsert_source("ow_src1", "first text", {"first_only": "x", "shared": "old"})
            store.upsert_source("ow_src1", "second text", {"shared": "new"})
        for store in (local, pg):
            src = store.get_source("ow_src1")
            assert src["text"] == "second text"
            assert "first_only" not in src["metadata"]
            assert src["metadata"]["shared"] == "new"

    def test_list_packs_pack_id_is_str_on_both_backends(self, tmp_path, pg_engine):
        """Positive contract (supersedes the former
        test_list_packs_pack_id_type_divergence_int_vs_str, which merely
        documented the split): pack_id must be ``str`` on BOTH backends,
        even when the stored JSON value is a native int."""
        schema = f"t{uuid.uuid4().hex[:12]}_ipk"
        local = LocalGraphStore(str(tmp_path / "intpack.db"))
        pg = PGGraphStore(pg_engine, schema=schema)
        try:
            for store in (local, pg):
                store.upsert_node("Item", "ipk_n1", {"pack_id": 42})
                store.upsert_node("Item", "ipk_n2", {"pack_id": 42})
            local_pid = local.list_packs()[0]["pack_id"]
            pg_pid = pg.list_packs()[0]["pack_id"]
            assert isinstance(local_pid, str) and local_pid == "42"
            assert isinstance(pg_pid, str) and pg_pid == "42"
            assert local_pid == pg_pid
        finally:
            local.close()
            with pg_engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


class TestUpsertRowidStability:
    """Regression pin for the "ROWID STABILITY" fix in _sql_dialect.py:
    SqlDialect.upsert() must use ``ON CONFLICT (...) DO UPDATE SET ...`` on
    BOTH backends, never SQLite's ``INSERT OR REPLACE`` (a delete+reinsert
    that allocates a new rowid). find_neighbors()/export_*() have no
    ORDER BY, so their scan order tracks physical row order; re-upserting an
    already-seen edge must not silently reshuffle that order — a LIMIT-capped
    hub fan-out must return a *stable* truncated subset across repeated
    re-ingestion (the everyday reingest-pipeline case), not just an
    unordered-equivalent one from run to run.

    Was RED against the SQLite-adopted _sql_graph_base.py before the fix
    (INSERT OR REPLACE moved the re-upserted edge to the end of the physical
    scan order); confirmed green after switching SQLite to ON CONFLICT DO
    UPDATE (rowid-preserving, matching pre-refactor local_graph_store.py's
    hand-written SQL and PG's pre-existing behavior)."""

    def _neighbor_ids(self, store, anchor: str) -> list[str]:
        return [
            n["properties"]["id"]
            for n in store.find_neighbors(anchor, direction="out", depth=1, limit=100)
        ]

    def test_reupsert_edge_does_not_reshuffle_scan_order(self, tmp_path, pg_engine):
        schema = f"t{uuid.uuid4().hex[:12]}_rowid"
        local = LocalGraphStore(str(tmp_path / "rowid.db"))
        pg = PGGraphStore(pg_engine, schema=schema)
        try:
            for store in (local, pg):
                store.upsert_node("Anchor", "anchor1", {})
                for i in range(10):
                    store.upsert_node("Leaf", f"leaf{i}", {})
                    store.upsert_edge("Anchor", "anchor1", "touches", "Leaf", f"leaf{i}", {"v": 1})

            local_before = self._neighbor_ids(local, "anchor1")
            pg_before = self._neighbor_ids(pg, "anchor1")
            assert len(local_before) == 10
            assert len(pg_before) == 10

            # Re-upsert a MIDDLE edge (same conflict key: from/relation/to) with
            # changed properties — must UPDATE in place, not delete+reinsert.
            for store in (local, pg):
                store.upsert_edge("Anchor", "anchor1", "touches", "Leaf", "leaf5", {"v": 2})

            local_after = self._neighbor_ids(local, "anchor1")
            pg_after = self._neighbor_ids(pg, "anchor1")

            assert local_after == local_before, (
                "SQLite scan order changed after a same-key edge re-upsert — "
                "rowid was not preserved (INSERT OR REPLACE regression)"
            )
            assert pg_after == pg_before
        finally:
            local.close()
            with pg_engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
