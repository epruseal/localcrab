"""
Tests for opencrab.stores.factory.make_billing_sql_store (issue #105).

Focus: billing_events gets its own SQLite file in local/kuzu mode instead
of sharing opencrab.db with write.lock'd tables (see opencrab/billing/
hooks.py's module docstring for why sharing the file was the actual bug),
and PG/docker mode is untouched.

NO MIGRATION (issue #105, second review round): an earlier version of this
fix copied any pre-#105 rows out of opencrab.db's billing_events into the
new billing.db on startup, gated by a marker file. codex's re-review found
three real problems with that: the copy-then-rename-then-mark-done sequence
wasn't atomic against a mid-sequence crash, two processes racing the very
first startup could both attempt it with no lock between them, and the
rename itself was an unlocked schema write against the shared file --
exactly the class of hazard this fix exists to remove. Since nothing in
this codebase reads billing_events (see opencrab.billing.hooks's module
docstring), paying that complexity had no payoff. The tests below pin the
reverted behaviour: billing.db starts empty and opencrab.db's old table (if
any) is left completely alone.
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

        billing_store = make_billing_sql_store(settings, sql)

        assert billing_store is not sql
        assert billing_store._url == f"sqlite:///{tmp_path / 'billing.db'}"
        assert sql._url == f"sqlite:///{tmp_path / 'opencrab.db'}"

    def test_kuzu_mode_also_gets_a_separate_billing_db_file(self, tmp_path):
        """kuzu is a local-mode variant (only the graph store differs) --
        billing must be separated there too, not just under storage_mode
        literally equal to "local"."""
        from opencrab.config import Settings
        from opencrab.stores.factory import make_billing_sql_store, make_sql_store

        settings = Settings(STORAGE_MODE="kuzu", LOCAL_DATA_DIR=str(tmp_path))
        sql = make_sql_store(settings)

        billing_store = make_billing_sql_store(settings, sql)

        assert billing_store is not sql
        assert billing_store._url == f"sqlite:///{tmp_path / 'billing.db'}"

    def test_pg_mode_reuses_the_same_store(self, tmp_path):
        """PG uses row-level locking, not a whole-file lock -- there is no
        contention to separate billing away from, so no second connection
        should even be opened."""
        from opencrab.config import Settings
        from opencrab.stores.factory import make_billing_sql_store

        settings = Settings(STORAGE_MODE="pg", POSTGRES_URL="postgresql://unused/db")
        fake_sql = object()  # identity check only -- must never be dereferenced

        billing_store = make_billing_sql_store(settings, fake_sql)

        assert billing_store is fake_sql

    def test_docker_mode_reuses_the_same_store(self, tmp_path):
        from opencrab.config import Settings
        from opencrab.stores.factory import make_billing_sql_store

        settings = Settings(STORAGE_MODE="docker", POSTGRES_URL="postgresql://unused/db")
        fake_sql = object()

        billing_store = make_billing_sql_store(settings, fake_sql)

        assert billing_store is fake_sql


class TestNoAutomaticMigration:
    def test_billing_db_starts_empty_even_when_old_table_has_rows(self, tmp_path):
        """The core reverted behaviour: opencrab.db having historical
        billing_events rows must NOT make billing.db start pre-populated --
        no ATTACH, no copy, nothing reads the old table at all."""
        from opencrab.config import Settings
        from opencrab.stores.factory import make_billing_sql_store, make_sql_store

        settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(tmp_path))
        sql = make_sql_store(settings)  # creates opencrab.db
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

        billing_store = make_billing_sql_store(settings, sql)
        hooks = BillingHooks(billing_store)

        assert hooks.list_events(tenant_id="legacy", limit=10) == []

    def test_old_table_in_opencrab_db_is_left_completely_untouched(self, tmp_path):
        """Not renamed, not dropped, not written to -- the exact row count,
        name, and content from before BillingHooks ever ran must survive
        unchanged. No marker file is created anywhere either."""
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
        before_mtime = old_path.stat().st_mtime_ns

        billing_store = make_billing_sql_store(settings, sql)
        BillingHooks(billing_store)

        after_mtime = old_path.stat().st_mtime_ns
        assert after_mtime == before_mtime  # never opened for writing

        check = sqlite3.connect(str(old_path))
        names = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        rows = check.execute("SELECT event_id FROM billing_events").fetchall()
        check.close()
        assert "billing_events" in names  # still the live name, not renamed
        assert [r[0] for r in rows] == ["evt_x"]  # untouched

        assert not (tmp_path / "billing.db.migrated").exists()  # no marker machinery left behind

    def test_no_second_connection_or_marker_ever_touches_old_db(self, tmp_path, monkeypatch):
        """Constructing BillingHooks against the new store must never open a
        second sqlite3 connection at all -- there is no migration code path
        left to open one."""
        from opencrab.config import Settings
        from opencrab.stores.factory import make_billing_sql_store, make_sql_store

        settings = Settings(STORAGE_MODE="local", LOCAL_DATA_DIR=str(tmp_path))
        sql = make_sql_store(settings)

        def _boom(*args, **kwargs):
            raise AssertionError("BillingHooks must not touch raw sqlite3 at all")

        monkeypatch.setattr(sqlite3, "connect", _boom)
        billing_store = make_billing_sql_store(settings, sql)
        hooks = BillingHooks(billing_store)  # uses SQLAlchemy only, not raw sqlite3
        monkeypatch.undo()

        assert hooks.list_events(limit=10) == []
