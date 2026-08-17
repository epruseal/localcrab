"""Direct tests for PG-only store internals not exercised by the SQLite<->PG
parity suite (test_pg_graph_doc_parity.py): PGGraphStore's wide-frontier
batch helpers, the `_require_available` guard contract, PgVectorStore's
`_build_where_sql` operator table plus its dim/identifier guards, and
PgDocStore's Korean-text keyword_search leg (tsvector/pg_trgm).

Skips entirely when OPENCRAB_PG_TEST_URL is not set, mirroring
test_pg_graph_doc_parity.py. Each test gets its own uuid-prefixed
schema/table (dropped in teardown) for parallel-safe isolation.
"""

from __future__ import annotations

import os
import uuid

import pytest

PG_URL = os.environ.get("OPENCRAB_PG_TEST_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="OPENCRAB_PG_TEST_URL not set — PG store tests skipped"
)

if PG_URL:
    from sqlalchemy import create_engine, text

    from opencrab.stores.pg_doc_store import PgDocStore
    from opencrab.stores.pg_graph_store import PGGraphStore
    from opencrab.stores.pg_vector_store import PgVectorStore, _build_where_sql


@pytest.fixture
def pg_engine():
    engine = create_engine(PG_URL)
    yield engine
    engine.dispose()


def _drop_schema(pg_engine, schema: str) -> None:
    with pg_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


# ---------------------------------------------------------------------------
# PGGraphStore — wide-frontier batching (_batch_frontier_edges / _batch_node_props_multi)
# ---------------------------------------------------------------------------


class TestPgGraphBatchHelpers:
    def test_wide_frontier_batches_in_one_round_trip_and_respects_limit(self, pg_engine):
        """A BFS level whose frontier (120 nodes) is far wider than the
        traversal's `limit` (30) still resolves correctly: `_batch_frontier_edges`
        collects every frontier node's candidate edges via one `unnest(...)`
        query per hop (not one query per frontier node), and the Python
        'remaining slot' loop still caps total results at `limit`."""
        schema = f"t{uuid.uuid4().hex[:12]}_wf"
        store = PGGraphStore(pg_engine, schema=schema)
        try:
            store.upsert_node("Hub", "hub", {})
            for i in range(120):
                store.upsert_node("Person", f"p{i}", {})
                store.upsert_edge("Hub", "hub", "has", "Person", f"p{i}", {})
            for i in range(120):
                store.upsert_node("Leaf", f"leaf{i}", {})
                store.upsert_edge("Person", f"p{i}", "owns", "Leaf", f"leaf{i}", {})

            # depth=2: the hop-1 frontier is all 120 "Person" nodes (>> limit=30).
            results = store.find_neighbors("hub", direction="out", depth=2, limit=30)
            assert len(results) == 30
            ids = [r["properties"]["id"] for r in results]
            assert len(ids) == len(set(ids)), "duplicate neighbour across batched levels"
            assert {r["depth"] for r in results} <= {1, 2}
        finally:
            store.close()
            _drop_schema(pg_engine, schema)

    def test_pack_filter_matches_node_passes_across_falsy_and_typed_pack_ids(self, pg_engine):
        """PG counterpart of test_sql_graph_base.py's same-named test — issue
        #62 follow-up. Confirms ``SqlDialect.json_truthy_text``'s PG branch
        (``jsonb_typeof`` + numeric-cast zero check, not text comparison)
        agrees with ``_node_passes`` on the same falsy/typed pack_id shapes,
        via ``_batch_frontier_edges`` (PG's actual find_neighbors query
        path, not just the SQLite base's). Manually verified live against
        this same PG instance before this test existed; this pins that
        verification instead of leaving it a one-off.
        """
        from opencrab.stores._graph_common import _node_passes

        schema = f"t{uuid.uuid4().hex[:12]}_pf"
        store = PGGraphStore(pg_engine, schema=schema)
        try:
            store.upsert_node("Hub", "hub", {})
            variants: dict[str, dict] = {
                "n_null": {"pack_id": None},
                "n_missing": {},
                "n_empty": {"pack_id": ""},
                "n_zero": {"pack_id": 0},
                "n_real_zero": {"pack_id": 0.0},  # trap: text "0.0" != "0"
                "n_false": {"pack_id": False},
                "n_own_pack": {"pack_id": "A"},
                "n_foreign": {"pack_id": "B"},
                "n_number": {"pack_id": 5},
                "n_true": {"pack_id": True},
                "n_string_zero": {"pack_id": "0"},  # trap: truthy, not falsy 0
            }
            for node_id, props in variants.items():
                store.upsert_node("Item", node_id, props)
                store.upsert_edge("Hub", "hub", "touches", "Item", node_id)

            for pack_ids, include_unpackaged in [
                (["A"], False),
                (["A"], True),
                (["5", "True"], False),
                (["0"], False),
            ]:
                pack_set = set(pack_ids)
                expected = {
                    node_id
                    for node_id, props in variants.items()
                    if _node_passes({**props, "id": node_id}, pack_set, include_unpackaged)
                }
                with store._conn() as conn:
                    rows = store._batch_frontier_edges(
                        conn, ["hub"], cap=50, out=True,
                        pack_set=pack_set, include_unpackaged=include_unpackaged,
                    )
                actual = {other_id for _t, other_id, _rel, _props in rows.get("hub", [])}
                assert actual == expected, (pack_ids, include_unpackaged, actual, expected)
        finally:
            store.close()
            _drop_schema(pg_engine, schema)

    def test_empty_frontier_short_circuits_without_a_query(self, pg_engine):
        schema = f"t{uuid.uuid4().hex[:12]}_ef"
        store = PGGraphStore(pg_engine, schema=schema)
        try:
            store.upsert_node("Solo", "solo1", {})
            # depth=0 -> nothing is expandable -> both batch helpers take the
            # empty-input early-return path inside find_neighbors.
            assert store.find_neighbors("solo1", direction="both", depth=0, limit=10) == []

            with store._engine.connect() as conn:
                assert store._batch_frontier_edges(conn, [], cap=10, out=True) == {}
                assert store._batch_node_props_multi(conn, set()) == {}
        finally:
            store.close()
            _drop_schema(pg_engine, schema)


# ---------------------------------------------------------------------------
# _require_available guard contract
# ---------------------------------------------------------------------------


class TestRequireAvailableGuard:
    """`Engine.dispose()` alone does not break a SQLAlchemy engine — it
    transparently rebuilds the connection pool on the next checkout — so the
    real availability gate each store exposes is its own `_available` flag,
    flipped by whatever detects a lost connection upstream. These tests pin
    that guard's contract directly: once `_available` is False, every public
    method raises a clean, identifiable RuntimeError instead of a raw
    DBAPI/SQLAlchemy exception."""

    def test_graph_store_raises_after_marked_unavailable(self, pg_engine):
        schema = f"t{uuid.uuid4().hex[:12]}_ra"
        store = PGGraphStore(pg_engine, schema=schema)
        try:
            store._available = False
            with pytest.raises(RuntimeError, match="PGGraphStore is not available"):
                store.get_node("X", "n1")
            with pytest.raises(RuntimeError, match="PGGraphStore is not available"):
                store.find_neighbors("n1")
            with pytest.raises(RuntimeError, match="PGGraphStore is not available"):
                store.upsert_node("X", "n1", {})
        finally:
            store._available = True
            store.close()
            _drop_schema(pg_engine, schema)

    def test_doc_store_raises_after_marked_unavailable(self, pg_engine):
        schema = f"t{uuid.uuid4().hex[:12]}_rad"
        store = PgDocStore(pg_engine, schema=schema)
        try:
            store._available = False
            with pytest.raises(RuntimeError, match="PgDocStore is not available"):
                store.get_node_doc("s1", "n1")
            with pytest.raises(RuntimeError, match="PgDocStore is not available"):
                store.upsert_node_doc("s1", "Doc", "n1", {})
        finally:
            store._available = True
            store.close()
            _drop_schema(pg_engine, schema)


# ---------------------------------------------------------------------------
# PgVectorStore — _build_where_sql operator table (pure function, no DB needed)
# ---------------------------------------------------------------------------


class TestBuildWhereSqlOperators:
    def test_none_or_empty_where_returns_no_filter(self) -> None:
        assert _build_where_sql(None) == (None, {})
        assert _build_where_sql({}) == (None, {})

    def test_scalar_equality_pack_id_uses_dedicated_column(self) -> None:
        sql, params = _build_where_sql({"pack_id": "packA"})
        assert sql == "pack_id = :w1"
        assert params == {"w1": "packA"}

    def test_scalar_equality_metadata_key_uses_jsonb_arrow(self) -> None:
        sql, params = _build_where_sql({"status": "active"})
        assert sql == "(metadata ->> :w1) = :w2"
        assert params == {"w1": "status", "w2": "active"}

    @pytest.mark.parametrize(
        "op,frag",
        [
            ("$eq", "="),
            ("$ne", "!="),
            ("$gt", ">"),
            ("$gte", ">="),
            ("$lt", "<"),
            ("$lte", "<="),
        ],
    )
    def test_comparison_operators(self, op, frag) -> None:
        sql, params = _build_where_sql({"score": {op: "5"}})
        assert sql == f"(metadata ->> :w1) {frag} :w2"
        assert params == {"w1": "score", "w2": "5"}

    def test_in_with_values_matches_and_binds_each(self) -> None:
        sql, params = _build_where_sql({"pack_id": {"$in": ["a", "b"]}})
        assert sql == "pack_id IN (:w1, :w2)"
        assert params == {"w1": "a", "w2": "b"}

    def test_in_empty_list_is_conservative_false(self) -> None:
        sql, params = _build_where_sql({"pack_id": {"$in": []}})
        assert sql == "FALSE"
        assert params == {}

    def test_nin_with_values_excludes_null_and_listed(self) -> None:
        sql, params = _build_where_sql({"status": {"$nin": ["x"]}})
        assert sql == (
            "(metadata ->> :w1) IS NOT NULL AND (metadata ->> :w1) NOT IN (:w2)"
        )
        assert params == {"w1": "status", "w2": "x"}

    def test_nin_empty_list_only_requires_non_null(self) -> None:
        sql, params = _build_where_sql({"status": {"$nin": []}})
        assert sql == "(metadata ->> :w1) IS NOT NULL"
        assert params == {"w1": "status"}

    def test_unsupported_operator_is_conservative_false(self) -> None:
        sql, _ = _build_where_sql({"status": {"$regex": ".*"}})
        assert sql == "FALSE"

    def test_and_or_nesting(self) -> None:
        where = {
            "$and": [
                {"pack_id": "a"},
                {"$or": [{"status": "x"}, {"status": "y"}]},
            ]
        }
        sql, params = _build_where_sql(where)
        assert sql.count(" AND ") >= 1
        assert " OR " in sql
        assert set(params.values()) == {"a", "status", "x", "y"}


# ---------------------------------------------------------------------------
# PgVectorStore — end-to-end guards and where-filter execution against real PG
# ---------------------------------------------------------------------------


def _fake_embed(dim: int):
    def embed(texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            seed = sum(ord(c) for c in t) % 1000
            out.append([float((seed + i) % 97) / 97.0 for i in range(dim)])
        return out

    return embed


class TestPgVectorStoreDirect:
    def test_invalid_collection_name_rejected(self, pg_engine) -> None:
        with pytest.raises(ValueError, match="Unsafe collection_name"):
            PgVectorStore(
                pg_engine,
                embedding_function=_fake_embed(4),
                dim=4,
                collection_name="bad; drop table x",
            )

    def test_dim_mismatch_raises_runtime_error(self, pg_engine) -> None:
        table = f"t{uuid.uuid4().hex[:8]}_dimcheck"

        def wrong_dim_embed(texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2, 0.3] for _ in texts]  # 3 dims, table declares 4

        store = PgVectorStore(
            pg_engine, embedding_function=wrong_dim_embed, dim=4, collection_name=table
        )
        try:
            with pytest.raises(RuntimeError, match=r"Embedding dim 3 != table dim 4"):
                store.add_texts(["hello"])
        finally:
            with pg_engine.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS {table}"))

    def test_where_filter_matches_and_excludes(self, pg_engine) -> None:
        table = f"t{uuid.uuid4().hex[:8]}_wheretest"
        store = PgVectorStore(
            pg_engine, embedding_function=_fake_embed(4), dim=4, collection_name=table
        )
        try:
            store.upsert_texts(
                texts=["alpha doc", "beta doc"],
                metadatas=[
                    {"pack_id": "packA", "status": "active"},
                    {"pack_id": "packB", "status": "inactive"},
                ],
                ids=["v1", "v2"],
            )
            hits_match = store.query("alpha doc", n_results=10, where={"pack_id": "packA"})
            assert {h["id"] for h in hits_match} == {"v1"}

            hits_none = store.query(
                "alpha doc", n_results=10, where={"pack_id": "no-such-pack"}
            )
            assert hits_none == []

            hits_op = store.query(
                "alpha doc", n_results=10, where={"status": {"$ne": "inactive"}}
            )
            assert {h["id"] for h in hits_op} == {"v1"}
        finally:
            with pg_engine.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS {table}"))


# ---------------------------------------------------------------------------
# PgDocStore — Korean-text keyword_search (tsvector + pg_trgm fallback)
# ---------------------------------------------------------------------------


class TestPgDocKoreanKeywordSearch:
    def test_normal_korean_multiword_query_matches(self, pg_engine) -> None:
        schema = f"t{uuid.uuid4().hex[:12]}_krn"
        store = PgDocStore(pg_engine, schema=schema)
        try:
            # #147: rows must belong to a pack to be reachable, and the
            # caller must name the scope they are reading.
            store.upsert_source("kr1", "인공지능 기계학습 연구 문서", {"node_id": "d1", "pack_id": "p"})
            store.upsert_source("kr2", "데이터베이스 트랜잭션 설명", {"node_id": "d2", "pack_id": "p"})
            hits = store.keyword_search("인공지능 연구", pack_ids=["p"], limit=10)
            assert {h["source_id"] for h in hits} == {"kr1"}
        finally:
            store.close()
            _drop_schema(pg_engine, schema)

    def test_error_keyword_unsupported_degrades_to_empty_list(self, pg_engine) -> None:
        schema = f"t{uuid.uuid4().hex[:12]}_kre"
        store = PgDocStore(pg_engine, schema=schema)
        try:
            store.upsert_source("kr1", "인공지능 연구", {"node_id": "d1", "pack_id": "p"})
            store._kw_ok = False  # simulate pg_trgm/index unavailable
            assert store.keyword_search("인공지능", pack_ids=["p"], limit=10) == []
        finally:
            store.close()
            _drop_schema(pg_engine, schema)

    def test_edge_short_korean_token_forces_trigram_fallback(self, pg_engine) -> None:
        """A token under 3 chars ('AI') cannot match a whole tsvector lexeme,
        forcing the ILIKE + pg_trgm similarity() leg — same contract as the
        ASCII short-token case in test_pg_graph_doc_parity.py, exercised here
        with a Korean corpus to confirm the fallback isn't ASCII-only."""
        schema = f"t{uuid.uuid4().hex[:12]}_krs"
        store = PgDocStore(pg_engine, schema=schema)
        try:
            store.upsert_source("kr1", "AI 인공지능 연구 문서입니다", {"node_id": "d1", "pack_id": "p"})
            store.upsert_source("kr2", "무관한 다른 내용입니다", {"node_id": "d2", "pack_id": "p"})
            hits = store.keyword_search("AI", pack_ids=["p"], limit=10)
            assert {h["source_id"] for h in hits} == {"kr1"}
        finally:
            store.close()
            _drop_schema(pg_engine, schema)
