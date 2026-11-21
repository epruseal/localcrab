"""PgVectorStore write-path validation ordering (issue #82 review follow-up).

A GitHub review on PR #333 (commit 5cb29a4) found that ``add_texts`` and
``upsert_texts`` call ``self._embed(texts)`` -- and ``upsert_texts``
additionally runs an ownership SELECT via ``_slot_owners`` -- before the
per-row ``dump_props()`` call validates metadata. A batch carrying
non-finite metadata therefore pays for the embedding call (and, for
upsert, a DB round trip) before the ``ValueError`` fires, unlike the
Chroma/sqlite-vec paths, which validate the whole batch up front.
``import_vectors`` has the same shape: within its single transaction it
can execute earlier ``INSERT`` statements before a later record's
``dump_props()`` raises -- harmless under the ``engine.begin()`` rollback,
but still wasted round trips.

These tests exercise the three write paths against a mocked SQLAlchemy
engine/connection (no live Postgres needed -- ``PgVectorStore`` is built
via ``__new__`` to skip the live-connection check in ``__init__``) and
assert that a non-finite metadata value is rejected BEFORE any
side-effecting work (the embedding call, any DB ``execute``) happens.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opencrab.stores.pg_vector_store import PgVectorStore


class _SpyEmbed:
    """Records how many times it was called; never touches a network/DB."""

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.calls = 0

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.0] * self.dim for _ in texts]


def _make_store(dim: int = 4) -> tuple[PgVectorStore, MagicMock]:
    """A PgVectorStore wired to a mock engine, bypassing the real-PG
    ``__init__`` connect/schema steps entirely."""
    store = PgVectorStore.__new__(PgVectorStore)
    store._ef = _SpyEmbed(dim)
    store._dim = dim
    store._table = "t_ordering"
    store._ef_search = 500
    store._available = True
    store._owns_engine = False
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []
    conn.execute.return_value.rowcount = 1
    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value = conn
    engine.connect.return_value.__enter__.return_value = conn
    store._engine = engine
    return store, conn


class TestAddTextsValidatesBeforeEmbedding:
    def test_non_finite_metadata_rejected_before_embed_call(self) -> None:
        store, conn = _make_store()
        with pytest.raises(ValueError):
            store.add_texts(["a"], metadatas=[{"score": float("nan")}])
        assert store._ef.calls == 0, (
            "dump_props must reject the batch before _embed() runs a single "
            "embedding call"
        )
        conn.execute.assert_not_called()

    def test_finite_metadata_still_embeds_and_writes(self) -> None:
        store, conn = _make_store()
        store.add_texts(["a"], metadatas=[{"score": 1.0}])
        assert store._ef.calls == 1
        conn.execute.assert_called_once()


class TestUpsertTextsValidatesBeforeEmbeddingOrOwnershipCheck:
    def test_non_finite_metadata_rejected_before_embed_call(self) -> None:
        store, conn = _make_store()
        with pytest.raises(ValueError):
            store.upsert_texts(["a"], metadatas=[{"score": float("inf")}], ids=["x"])
        assert store._ef.calls == 0
        conn.execute.assert_not_called()

    def test_finite_metadata_still_embeds_and_writes(self) -> None:
        store, conn = _make_store()
        store.upsert_texts(["a"], metadatas=[{"score": 1.0}], ids=["x"])
        assert store._ef.calls == 1
        assert conn.execute.called


def test_precomputed_vector_write_does_not_embed() -> None:
    store, conn = _make_store()
    vectors = store.prepare_upsert_vectors(["a"])
    assert store._ef.calls == 1
    assert store.upsert_precomputed(["a"], [{"pack_id": "p"}], ["x"], vectors) == ["x"]
    assert store._ef.calls == 1
    assert conn.execute.called


@pytest.mark.parametrize("vectors", [[], [[1.0]]])
def test_precomputed_vector_rejects_bad_shape_before_write(vectors) -> None:
    store, conn = _make_store()
    with pytest.raises(ValueError):
        store.upsert_precomputed(["a"], [{"pack_id": "p"}], ["x"], vectors)
    conn.execute.assert_not_called()
    assert store._ef.calls == 0


@pytest.mark.parametrize("vectors", [[], [[0.0] * 4, [0.0] * 4]])
def test_upsert_preserves_embedding_count_behavior(vectors) -> None:
    store, conn = _make_store()
    store._ef = lambda texts: vectors
    assert store.upsert_texts(["a"], [{"pack_id": "p"}], ["x"]) == ["x"]
    assert conn.execute.call_count == 1 + min(len(vectors), 1)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True])
@pytest.mark.parametrize("method", ["add", "upsert", "precomputed"])
def test_vector_values_reach_pg_as_before(value, method) -> None:
    store, conn = _make_store()
    vector = [value, 0.0, 0.0, 0.0]
    store._ef = lambda texts: [vector for _ in texts]
    if method == "add":
        store.add_texts(["a"], [{"pack_id": "p"}], ["x"])
    elif method == "upsert":
        store.upsert_texts(["a"], [{"pack_id": "p"}], ["x"])
    else:
        store.upsert_precomputed(["a"], [{"pack_id": "p"}], ["x"], [vector])
    params = conn.execute.call_args.args[1]
    assert params["embedding"].startswith(f"[{float(value)!r},")


class TestImportVectorsValidatesBeforeAnyInsert:
    def test_non_finite_metadata_rejected_before_any_execute(self) -> None:
        store, conn = _make_store()
        records = [
            {
                "id": "a",
                "document": "d1",
                "embedding": [0.0] * 4,
                "metadata": {"score": 1.0},
            },
            {
                "id": "b",
                "document": "d2",
                "embedding": [0.0] * 4,
                "metadata": {"score": float("nan")},
            },
        ]
        with pytest.raises(ValueError):
            store.import_vectors(records, pack_id="p1")
        conn.execute.assert_not_called()

    def test_all_finite_records_get_inserted(self) -> None:
        store, conn = _make_store()
        records = [
            {
                "id": "a",
                "document": "d1",
                "embedding": [0.0] * 4,
                "metadata": {"score": 1.0},
            },
            {
                "id": "b",
                "document": "d2",
                "embedding": [0.0] * 4,
                "metadata": {"score": 2.0},
            },
        ]
        store.import_vectors(records, pack_id="p1")
        assert conn.execute.call_count == 2
