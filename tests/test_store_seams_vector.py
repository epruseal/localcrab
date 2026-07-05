"""Seam tests for the vector-store trio (SqliteVecStore/PgVectorStore/ChromaStore)
ahead of _vector_base/_SqliteConnMixin adoption (S3 mechanical refactor).

Written to be green against the PRE-adoption code and to stay green after
_vector_base helpers / _SqliteConnMixin are wired in — the point is to pin
current, characterized behaviour (including a real cross-backend asymmetry,
see TestOpAfterClose) so the mechanical refactor cannot silently change it.

PG cases use build_vector_store("pg", ...) from _vec_helpers, which skips
cleanly when OPENCRAB_PG_TEST_URL is unset or unreachable (see that module's
docstring for the isolation/teardown convention).
"""

from __future__ import annotations

import uuid

import pytest
from _vec_helpers import build_vector_store

DIM = 16


def _drop_pg_table(store) -> None:
    from sqlalchemy import text

    try:
        with store._engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {store._table}"))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestAddQueryRoundtrip:
    @pytest.mark.parametrize("backend", ["sqlite-vec", "pg"])
    def test_add_then_query_returns_the_added_text(self, backend, tmp_path):
        store = build_vector_store(backend, tmp_path, dim=DIM)
        try:
            store.add_texts(
                texts=["red apple fruit"],
                metadatas=[{"pack_id": "p1"}],
                ids=["n1"],
            )
            hits = store.query("apple", n_results=1)
            assert len(hits) == 1
            assert hits[0]["id"] == "n1"
            assert hits[0]["document"] == "red apple fruit"
            assert hits[0]["metadata"]["pack_id"] == "p1"
        finally:
            if backend == "pg":
                _drop_pg_table(store)
            store.close()


class TestUpsertIdDeterminism:
    @pytest.mark.parametrize("backend", ["sqlite-vec", "pg"])
    def test_same_text_upserted_twice_gets_the_same_id(self, backend, tmp_path):
        store = build_vector_store(backend, tmp_path, dim=DIM)
        try:
            ids1 = store.upsert_texts(texts=["same text"], metadatas=[{"pack_id": "p"}])
            ids2 = store.upsert_texts(texts=["same text"], metadatas=[{"pack_id": "p"}])
            assert ids1 == ids2
            assert len(ids1[0]) == 16
            assert store.count() == 1
        finally:
            if backend == "pg":
                _drop_pg_table(store)
            store.close()

    def test_id_matches_sha256_16_of_text(self, tmp_path):
        import hashlib

        store = build_vector_store("sqlite-vec", tmp_path, dim=DIM)
        try:
            ids = store.upsert_texts(texts=["deterministic"], metadatas=[{"pack_id": "p"}])
            assert ids == [hashlib.sha256(b"deterministic").hexdigest()[:16]]
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestDimMismatchMessage:
    """Same message text on both app-side-embedding backends (chroma has no
    equivalent check — see _vector_base module docstring)."""

    def _bad_ef(self, texts):
        return [[0.0, 0.0, 0.0] for _ in texts]  # dim 3, stores declare dim 16

    def test_sqlite_vec_dim_mismatch_message(self, tmp_path):
        from opencrab.stores.sqlite_vec_store import SqliteVecStore

        store = SqliteVecStore(
            db_path=str(tmp_path / "v.db"),
            embedding_function=self._bad_ef,
            dim=DIM,
            collection_name="vtest",
        )
        with pytest.raises(RuntimeError, match=r"Embedding dim 3 != table dim 16\."):
            store.upsert_texts(texts=["a"], metadatas=[{"pack_id": "p"}], ids=["x"])
        store.close()

    def test_pg_vector_dim_mismatch_message(self, tmp_path):
        import os

        from opencrab.stores.pg_vector_store import PgVectorStore

        dsn = os.environ.get("OPENCRAB_PG_TEST_URL")
        if not dsn:
            pytest.skip("OPENCRAB_PG_TEST_URL not set - pg backend tests skipped")
        store = PgVectorStore(
            dsn_or_engine=dsn,
            embedding_function=self._bad_ef,
            dim=DIM,
            collection_name=f"vseam_{uuid.uuid4().hex[:12]}",
        )
        if not store.available:
            pytest.skip(f"Cannot connect to PG test DB at {dsn!r}")
        try:
            with pytest.raises(RuntimeError, match=r"Embedding dim 3 != table dim 16\."):
                store.upsert_texts(texts=["a"], metadatas=[{"pack_id": "p"}], ids=["x"])
        finally:
            _drop_pg_table(store)
            store.close()


class TestOpAfterClose:
    """Real, pre-existing cross-backend asymmetry (characterized, not a bug to
    fix here): SqliteVecStore's per-thread sqlite3 connection is physically
    closed, so any op on the same thread raises sqlite3.ProgrammingError.
    PgVectorStore.close() disposes the SQLAlchemy engine's pool, but the
    engine itself stays usable (dispose() does not disable future connect()
    calls), so an op after close() reconnects transparently and succeeds."""

    def test_sqlite_vec_op_after_close_raises(self, tmp_path):
        import sqlite3

        store = build_vector_store("sqlite-vec", tmp_path, dim=DIM)
        store.upsert_texts(texts=["a"], metadatas=[{"pack_id": "p"}], ids=["x"])
        store.close()
        with pytest.raises(sqlite3.ProgrammingError):
            store.upsert_texts(texts=["b"], metadatas=[{"pack_id": "p"}], ids=["y"])

    def test_pg_vector_op_after_close_reconnects(self, tmp_path):
        store = build_vector_store("pg", tmp_path, dim=DIM)
        try:
            store.upsert_texts(texts=["a"], metadatas=[{"pack_id": "p"}], ids=["x"])
            store.close()
            # must NOT raise -- dispose() only drains the pool
            store.upsert_texts(texts=["b"], metadatas=[{"pack_id": "p"}], ids=["y"])
            assert store.count() == 2
        finally:
            _drop_pg_table(store)


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestEmptyAddBatch:
    @pytest.mark.parametrize("backend", ["sqlite-vec", "pg"])
    def test_empty_add_texts_returns_empty_list_and_creates_nothing(self, backend, tmp_path):
        store = build_vector_store(backend, tmp_path, dim=DIM)
        try:
            assert store.add_texts(texts=[]) == []
            assert store.count() == 0
        finally:
            if backend == "pg":
                _drop_pg_table(store)
            store.close()

    @pytest.mark.parametrize("backend", ["sqlite-vec", "pg"])
    def test_empty_upsert_texts_returns_empty_list_and_creates_nothing(self, backend, tmp_path):
        store = build_vector_store(backend, tmp_path, dim=DIM)
        try:
            assert store.upsert_texts(texts=[]) == []
            assert store.count() == 0
        finally:
            if backend == "pg":
                _drop_pg_table(store)
            store.close()


class TestLengthMismatch:
    @pytest.mark.parametrize("backend", ["sqlite-vec", "pg"])
    def test_metadatas_ids_length_mismatch_raises_value_error(self, backend, tmp_path):
        store = build_vector_store(backend, tmp_path, dim=DIM)
        try:
            with pytest.raises(ValueError, match="same length"):
                store.upsert_texts(
                    texts=["a", "b"], metadatas=[{"pack_id": "p"}], ids=["only-one"]
                )
            assert store.count() == 0
        finally:
            if backend == "pg":
                _drop_pg_table(store)
            store.close()


class TestSqliteVecDoubleClose:
    def test_double_close_is_idempotent(self, tmp_path):
        store = build_vector_store("sqlite-vec", tmp_path, dim=DIM)
        store.upsert_texts(texts=["a"], metadatas=[{"pack_id": "p"}], ids=["x"])
        store.close()
        store.close()  # must not raise
        assert store._all_conns == []
