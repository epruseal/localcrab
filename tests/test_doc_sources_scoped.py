"""Tests for `list_sources_scoped` (issue #201 §4-B).

Two backends, two fixture styles -- matching the precedents already in this
suite:
  - `_SqlDocStoreBase`'s shared implementation, exercised through the real
    SQLite subclass (`LocalSQLDocStore`), same `tmp_path` fixture pattern as
    `test_local_sql_doc_store.py`.
  - `MongoStore`, via a mocked `find().limit()` collection double -- there is
    no Mongo server in this environment, matching
    `test_read_scope_isolation.py::TestMongoDocStoreScoping`'s approach for
    `list_nodes_scoped`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

PACK_A = "pack-a"
PACK_B = "pack-b"


@pytest.fixture
def store(tmp_path):
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    return LocalSQLDocStore(str(tmp_path / "doc_store.db"))


def _source(store, source_id: str, pack_id: str) -> None:
    store.upsert_source(source_id, f"text of {source_id}", {"pack_id": pack_id})


class TestListSourcesScopedSql:
    def test_returns_only_sources_for_the_requested_packs(self, store):
        _source(store, "a1", PACK_A)
        _source(store, "a2", PACK_A)
        _source(store, "b1", PACK_B)

        got_a = {s["source_id"] for s in store.list_sources_scoped([PACK_A], limit=100)}
        assert got_a == {"a1", "a2"}

        got_both = {
            s["source_id"] for s in store.list_sources_scoped([PACK_A, PACK_B], limit=100)
        }
        assert got_both == {"a1", "a2", "b1"}

    def test_empty_pack_ids_returns_empty_without_querying(self, store, monkeypatch):
        _source(store, "a1", PACK_A)
        calls = []
        monkeypatch.setattr(
            store, "_fetch_all", lambda sql, params: calls.append((sql, params)) or []
        )

        assert store.list_sources_scoped([], limit=100) == []
        assert calls == []

    def test_limit_zero_or_negative_returns_empty_without_querying(self, store, monkeypatch):
        _source(store, "a1", PACK_A)
        calls = []
        monkeypatch.setattr(
            store, "_fetch_all", lambda sql, params: calls.append((sql, params)) or []
        )

        assert store.list_sources_scoped([PACK_A], limit=0) == []
        assert store.list_sources_scoped([PACK_A], limit=-1) == []
        assert calls == []

    def test_limit_is_honoured(self, store):
        """`pack_fork`'s truncation detection (design §5-1 step 4) reads
        `CAP+1` rows and checks whether it got exactly that many back --
        insert N+1, request N, must get back exactly N."""
        n = 5
        for i in range(n + 1):
            _source(store, f"a{i}", PACK_A)

        got = store.list_sources_scoped([PACK_A], limit=n)
        assert len(got) == n

    def test_legacy_source_fallback_when_pack_id_is_absent_or_falsy(self, store):
        # Canon row: proper pack_id tag.
        _source(store, "a1", PACK_A)
        # Legacy row: pack_id key absent entirely, `source` names the pack
        # instead -- `_doc_owner_pred`'s fallback branch. A plain `pack_id
        # ==` predicate would miss this row; that asymmetry is documented,
        # relied-upon behaviour `pack_fork` depends on for source discovery.
        store.upsert_source("legacy1", "legacy text", {"source": PACK_A})
        # Falsy (present but empty), not merely absent -- json_truthy_text
        # treats "" the same as missing.
        store.upsert_source("legacy2", "legacy text 2", {"pack_id": "", "source": PACK_A})

        got = {s["source_id"] for s in store.list_sources_scoped([PACK_A], limit=100)}
        assert got == {"a1", "legacy1", "legacy2"}

    def test_mixed_tagged_row_is_not_pulled_into_the_wrong_pack(self, store):
        # pack_id="B", source="A": must stay B's, never leak into A's scope
        # via the source fallback -- the exact bug `_doc_owner_pred`'s
        # docstring documents fixing (an unconditional OR would reintroduce
        # it here independently of that fix).
        store.upsert_source("mixed", "mixed text", {"pack_id": PACK_B, "source": PACK_A})

        got_a = {s["source_id"] for s in store.list_sources_scoped([PACK_A], limit=100)}
        assert "mixed" not in got_a
        got_b = {s["source_id"] for s in store.list_sources_scoped([PACK_B], limit=100)}
        assert "mixed" in got_b

    def test_unavailable_store_raises(self, tmp_path):
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        s = LocalSQLDocStore(str(tmp_path / "dead.db"))
        s._available = False
        with pytest.raises(RuntimeError, match="not available"):
            s.list_sources_scoped([PACK_A], limit=100)

    def test_unavailable_store_raises_even_for_empty_scope(self, tmp_path):
        """`_require_available()` runs BEFORE the empty-pack_ids/limit<=0
        short-circuit (same order as `list_nodes_scoped`) -- an outage must
        raise regardless of what the caller passed, never quietly agree
        with an empty-scope `[]` return."""
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        s = LocalSQLDocStore(str(tmp_path / "dead2.db"))
        s._available = False
        with pytest.raises(RuntimeError, match="not available"):
            s.list_sources_scoped([], limit=0)


class TestListSourcesScopedMongo:
    """No Mongo server in this environment -- a collection double is enough
    to pin that the ownership filter reaches the query at all, matching
    `test_read_scope_isolation.py::TestMongoDocStoreScoping`'s approach for
    `list_nodes_scoped`."""

    def _store(self, rows):
        from opencrab.stores.mongo_store import MongoStore

        store = MongoStore.__new__(MongoStore)
        store._available = True
        cursor = MagicMock()
        cursor.limit.return_value = rows
        collection = MagicMock()
        collection.find.return_value = cursor
        store._db = {"sources": collection}
        return store, collection

    def test_query_carries_the_pack_filter(self):
        store, collection = self._store([])
        store.list_sources_scoped([PACK_A, PACK_B], limit=10)

        query = collection.find.call_args[0][0]
        pack_clause = query["$or"][0]["metadata.pack_id"]
        assert pack_clause["$in"] == [PACK_A, PACK_B]
        assert pack_clause["$type"] == "string"
        fallback = query["$or"][1]["$and"][1]["metadata.source"]
        assert fallback["$in"] == [PACK_A, PACK_B]

    def test_empty_scope_never_queries(self):
        store, collection = self._store([])
        assert store.list_sources_scoped([], limit=10) == []
        collection.find.assert_not_called()

    def test_limit_zero_never_queries(self):
        store, collection = self._store([])
        assert store.list_sources_scoped([PACK_A], limit=0) == []
        collection.find.assert_not_called()

    def test_unavailable_store_raises(self):
        from opencrab.stores.mongo_store import MongoStore

        store = MongoStore.__new__(MongoStore)
        store._available = False
        with pytest.raises(RuntimeError, match="not available"):
            store.list_sources_scoped([PACK_A], limit=10)
