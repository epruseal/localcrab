"""
Integration tests for opencrab.stores.factory.make_doc_store().

Verifies that local mode routes to LocalSQLDocStore (SQLite) instead of
the legacy LocalDocStore (JSON).  Settings are constructed directly (no
lru_cache re-use) so each test gets a clean, isolated Settings instance.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(tmp_path, storage_mode: str = "local"):
    """Return a fresh Settings instance pointing at tmp_path."""
    from opencrab.config import Settings

    return Settings(
        STORAGE_MODE=storage_mode,
        LOCAL_DATA_DIR=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# make_doc_store — local mode
# ---------------------------------------------------------------------------


class TestMakeDocStoreLocalMode:
    def test_returns_local_sql_doc_store(self, tmp_path):
        """local 모드에서 make_doc_store()가 LocalSQLDocStore를 반환하는지 확인."""
        from opencrab.stores.factory import make_doc_store
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

        settings = _make_settings(tmp_path)
        store = make_doc_store(settings)

        assert isinstance(store, LocalSQLDocStore)

    def test_db_file_created_at_expected_path(self, tmp_path):
        """local 모드에서 db가 LOCAL_DATA_DIR/doc_store.db에 생성되는지 확인."""
        import os

        from opencrab.stores.factory import make_doc_store

        settings = _make_settings(tmp_path)
        make_doc_store(settings)

        expected = tmp_path / "doc_store.db"
        assert os.path.exists(str(expected)), (
            f"doc_store.db not found at {expected}"
        )

    def test_returned_store_is_available(self, tmp_path):
        """반환된 LocalSQLDocStore.available == True."""
        from opencrab.stores.factory import make_doc_store

        settings = _make_settings(tmp_path)
        store = make_doc_store(settings)

        assert store.available is True

    def test_returned_store_pings(self, tmp_path):
        """반환된 store가 SQLite 라운드트립을 정상 완료한다."""
        from opencrab.stores.factory import make_doc_store

        settings = _make_settings(tmp_path)
        store = make_doc_store(settings)

        assert store.ping() is True

    def test_upsert_and_list_nodes_roundtrip(self, tmp_path):
        """make_doc_store()가 반환한 store로 실제 데이터를 저장하고 조회한다."""
        from opencrab.stores.factory import make_doc_store

        settings = _make_settings(tmp_path)
        store = make_doc_store(settings)

        store.upsert_node_doc("subject", "User", "alice", {"name": "Alice"})
        nodes = store.list_nodes(limit=10)

        assert len(nodes) == 1
        assert nodes[0]["node_id"] == "alice"
        assert nodes[0]["properties"]["name"] == "Alice"

    def test_legacy_local_doc_store_class_still_importable(self):
        """LocalDocStore (JSON) は削除されていない — レガシー互換性確認."""
        from opencrab.stores.local_doc_store import LocalDocStore  # noqa: F401

        assert LocalDocStore is not None

    def test_two_calls_same_settings_create_independent_stores(self, tmp_path):
        """make_doc_store()를 두 번 호출해도 독립적인 인스턴스가 반환된다."""
        from opencrab.stores.factory import make_doc_store

        settings = _make_settings(tmp_path)
        store_a = make_doc_store(settings)
        store_b = make_doc_store(settings)

        # Both available, but are distinct Python objects sharing the same DB.
        assert store_a is not store_b
        assert store_a.available is True
        assert store_b.available is True
