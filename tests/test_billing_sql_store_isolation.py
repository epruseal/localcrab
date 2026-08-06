"""
Tests for opencrab.stores.factory.make_billing_sql_store (issue #105).

Focus: billing_events gets its own SQLite file in local/kuzu mode instead
of sharing opencrab.db with write.lock'd tables (see opencrab/billing/
hooks.py's module docstring for why sharing the file was the actual bug),
PG/docker mode is untouched, and any rows a pre-#105 install already wrote
to the old, shared table get migrated over exactly once.
"""

from __future__ import annotations

import sqlite3

from opencrab.billing.hooks import BillingHooks

_OLD_BILLING_DDL = """
CREATE TABLE billing_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    tenant_id   TEXT NOT NULL DEFAULT 'default',
    subject_id  TEXT,
    event_type  TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    metadata    TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
)
"""


class TestMakeBillingSqlStoreRouting:
    def test_local_mode_gets_a_separate_billing_db_file(self, tmp_path):
        from opencrab.config import Settings
        from opencrab.stores.factory import make_billing_sql_store, make_sql_store

        settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(tmp_path))
        sql = make_sql_store(settings)

        billing_store, migrate_from = make_billing_sql_store(settings, sql)

        assert billing_store is not sql
        assert billing_store._url == f"sqlite:///{tmp_path / 'billing.db'}"
        assert sql._url == f"sqlite:///{tmp_path / 'opencrab.db'}"
        assert migrate_from is sql

    def test_kuzu_mode_also_gets_a_separate_billing_db_file(self, tmp_path):
        """kuzu is a local-mode variant (only the graph store differs) --
        billing must be separated there too, not just under storage_mode
        literally equal to "local"."""
        from opencrab.config import Settings
        from opencrab.stores.factory import make_billing_sql_store, make_sql_store

        settings = Settings(STORAGE_MODE="kuzu", LOCAL_DATA_DIR=str(tmp_path))
        sql = make_sql_store(settings)

        billing_store, migrate_from = make_billing_sql_store(settings, sql)

        assert billing_store is not sql
        assert billing_store._url == f"sqlite:///{tmp_path / 'billing.db'}"
        assert migrate_from is sql

    def test_pg_mode_reuses_the_same_store_and_skips_migration(self, tmp_path):
        """PG uses row-level locking, not a whole-file lock -- there is no
        contention to separate billing away from, so no second connection
        should even be opened."""
        from opencrab.config import Settings
        from opencrab.stores.factory import make_billing_sql_store

        settings = Settings(STORAGE_MODE="pg", POSTGRES_URL="postgresql://unused/db")
        fake_sql = object()  # identity check only -- must never be dereferenced

        billing_store, migrate_from = make_billing_sql_store(settings, fake_sql)

        assert billing_store is fake_sql
        assert migrate_from is None

    def test_docker_mode_reuses_the_same_store_and_skips_migration(self, tmp_path):
        from opencrab.config import Settings
        from opencrab.stores.factory import make_billing_sql_store

        settings = Settings(STORAGE_MODE="docker", POSTGRES_URL="postgresql://unused/db")
        fake_sql = object()

        billing_store, migrate_from = make_billing_sql_store(settings, fake_sql)

        assert billing_store is fake_sql
        assert migrate_from is None


class TestBillingEventsMigration:
    def test_migrates_pre_105_rows_and_marks_old_table_stale(self, tmp_path):
        from opencrab.config import Settings
        from opencrab.stores.factory import make_billing_sql_store, make_sql_store

        settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(tmp_path))
        sql = make_sql_store(settings)  # creates opencrab.db (no billing_events yet)
        old_path = tmp_path / "opencrab.db"

        # Simulate a pre-#105 install: billing_events already lives in
        # opencrab.db with real historical rows written before this fix.
        conn = sqlite3.connect(str(old_path))
        conn.execute(_OLD_BILLING_DDL)
        conn.execute(
            "INSERT INTO billing_events (event_id, tenant_id, event_type) VALUES "
            "('evt_pre105_a', 'legacy', 'query'), ('evt_pre105_b', 'legacy', 'node_write')"
        )
        conn.commit()
        conn.close()

        billing_store, migrate_from = make_billing_sql_store(settings, sql)
        hooks = BillingHooks(billing_store, migrate_from=migrate_from)

        events = hooks.list_events(tenant_id="legacy", limit=10)
        assert {e["event_id"] for e in events} == {"evt_pre105_a", "evt_pre105_b"}

        # Old table is unmistakably stale -- renamed, not silently left
        # under the live name for a future reader to mistake for current
        # data (issue #105 review: "keep it, but leave a trace").
        check = sqlite3.connect(str(old_path))
        names = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        check.close()
        assert "billing_events" not in names
        assert "billing_events_migrated_to_billing_db" in names

        marker = tmp_path / "billing.db.migrated"
        assert marker.exists()

    def test_second_construction_short_circuits_on_marker_no_db_touched(self, tmp_path, monkeypatch):
        """Cost after the first successful migration must be ~free: a
        Path.exists() stat, nothing more. Proven here by making sqlite3.connect
        raise -- if the migration path were re-entered (e.g. a naive
        re-scan of a possibly-large old table on every startup), this
        would fail loudly instead of just being slow."""
        from opencrab.config import Settings
        from opencrab.stores.factory import make_billing_sql_store, make_sql_store

        settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(tmp_path))
        sql = make_sql_store(settings)
        old_path = tmp_path / "opencrab.db"

        conn = sqlite3.connect(str(old_path))
        conn.execute(_OLD_BILLING_DDL)
        conn.execute("INSERT INTO billing_events (event_id, event_type) VALUES ('evt_x', 'query')")
        conn.commit()
        conn.close()

        billing_store, migrate_from = make_billing_sql_store(settings, sql)
        BillingHooks(billing_store, migrate_from=migrate_from)  # first run: real migration
        assert (tmp_path / "billing.db.migrated").exists()

        def _boom(*args, **kwargs):
            raise AssertionError("sqlite3.connect must not be called once the marker exists")

        monkeypatch.setattr(sqlite3, "connect", _boom)
        billing_store2, migrate_from2 = make_billing_sql_store(settings, sql)
        hooks2 = BillingHooks(billing_store2, migrate_from=migrate_from2)  # must not touch sqlite3
        monkeypatch.undo()

        events = hooks2.list_events(limit=10)
        assert len(events) == 1  # unchanged -- no duplicate work happened

    def test_fresh_install_with_no_old_table_sets_marker_immediately(self, tmp_path):
        """No pre-existing billing_events at all (e.g. a fresh install, or
        the old opencrab.db doesn't even exist yet) must not error, and
        must still set the marker so future startups skip the check too."""
        from opencrab.config import Settings
        from opencrab.stores.factory import make_billing_sql_store, make_sql_store

        settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(tmp_path))
        sql = make_sql_store(settings)  # opencrab.db exists, but no billing_events table

        billing_store, migrate_from = make_billing_sql_store(settings, sql)
        hooks = BillingHooks(billing_store, migrate_from=migrate_from)

        assert hooks.list_events(limit=10) == []
        assert (tmp_path / "billing.db.migrated").exists()

    def test_pg_mode_migrate_from_none_is_a_true_no_op(self, tmp_path):
        """migrate_from=None (PG/docker) must not attempt anything -- pins
        that BillingHooks.__init__ only calls _migrate_from when a real old
        store is passed."""
        from opencrab.stores.sql_store import SQLStore

        store = SQLStore("sqlite:///:memory:")
        hooks = BillingHooks(store, migrate_from=None)  # must not raise
        assert hooks.list_events(limit=10) == []
