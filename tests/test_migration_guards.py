"""
#144/#151 fail-closed guard tests for scripts/migrate_sqlite_to_pg.py (forward,
SQLite -> PostgreSQL) and scripts/migrate_to_local.py (reverse, PostgreSQL ->
SQLite).

Background: a previous implementer added users/api_tokens/packs to the
forward script's copy logic, but that broke migrate_sql for any pre-#144
database (OperationalError: no such table: users, raised while computing
counts BEFORE the dry-run branch -- so none of the five pre-existing tables
migrated either). That copy logic was reverted to origin/main byte-for-byte.
The real risk it was trying to fix is real too: a migration that completes
without copying users/api_tokens/packs silently drops every user and every
issued token while still reporting success, and --verify would not catch it
(it only re-checks tables already in migrate_sql's own count map). A
fail-closed guard was added instead: detect SQLStore-owned tables the
migration does not cover before any work happens and require an explicit
--allow-unmigrated opt-in.

#151 replaced the original guard's implementation. The hard-coded
``_AUTH_TABLES`` tuple (duplicated in both scripts) and the helpers that read
it (``fwd._auth_tables_present``, ``rev._filter_auth_tables``) are gone.
The guard is now a single derived function shared by both scripts,
``_migration_tables.unmigrated_tables(source_tables)`` = (tables SQLStore
actually creates) ∩ (tables present in the source) − (tables the migration
copies). #151 also migrates users/api_tokens/packs, so for the *current*
schema that set is always empty -- the guard exists for the next table
SQLStore grows that neither script learns about. The tests in
TestUnmigratedTablesGuard exercise that derived function directly by
injecting both sides of the set arithmetic (via monkeypatching
``sqlstore_owned_tables`` and passing a source table list), which is why
they no longer need a live SQLite fixture file for the decision logic
itself; they replace the old TestForwardTableEnumeration/
TestReverseAuthTableFilter classes, which tested the two hard-coded
duplicates of the same idea separately.

No live PostgreSQL is available in this environment. Tests that would need
one either stay inside code paths that don't require a live connection
(dry-run; --sql-db counting, which runs identically regardless of dry-run),
or hit a fast local connection-refused (127.0.0.1 unassigned port) to prove
the run gets past the counts stage without live PostgreSQL.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import _migration_tables as mt  # noqa: E402
import migrate_sqlite_to_pg as fwd  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures: throwaway SQLite files under tmp_path only -- never touches any
# real data dir.
# ---------------------------------------------------------------------------

# Exact origin/main SQLite DDL (opencrab/stores/sql_store.py::_TABLES_SQL_SQLITE)
# for the five pre-#144 tables.
_PRE144_DDL = """
CREATE TABLE IF NOT EXISTS ontology_nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    space       TEXT NOT NULL,
    node_type   TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (space, node_id)
);
CREATE TABLE IF NOT EXISTS ontology_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_space  TEXT NOT NULL,
    from_id     TEXT NOT NULL,
    relation    TEXT NOT NULL,
    to_space    TEXT NOT NULL,
    to_id       TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (from_space, from_id, relation, to_space, to_id)
);
CREATE TABLE IF NOT EXISTS impact_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id     TEXT NOT NULL,
    change_type TEXT NOT NULL,
    impact_json TEXT NOT NULL,
    analyzed_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS lever_simulations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lever_id     TEXT NOT NULL,
    direction    TEXT NOT NULL,
    magnitude    REAL NOT NULL,
    results      TEXT NOT NULL,
    simulated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS rebac_policies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id   TEXT NOT NULL,
    permission   TEXT NOT NULL,
    resource_id  TEXT NOT NULL,
    granted      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (subject_id, permission, resource_id)
);
"""

# HEAD's opencrab/stores/sql_store.py SQLite DDL for the three #144 auth
# tables, appended on top of _PRE144_DDL to build a post-#144 fixture.
_AUTH_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    is_local     INTEGER NOT NULL DEFAULT 0 CHECK (is_local IN (0, 1)),
    disabled     INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    created_at   TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS api_tokens (
    token_id     TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users (user_id),
    token_hash   TEXT NOT NULL UNIQUE,
    name         TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE TABLE IF NOT EXISTS packs (
    pack_id      TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL REFERENCES users (user_id),
    visibility   TEXT NOT NULL DEFAULT 'private',
    title        TEXT,
    description  TEXT,
    forked_from  TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);
"""


def _make_db(path: Path, ddl: str) -> str:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()
    return str(path)


@pytest.fixture
def pre144_db(tmp_path: Path) -> str:
    """Five pre-#144 tables only -- the schema that broke under the reverted
    (buggy) copy logic."""
    return _make_db(tmp_path / "pre144.db", _PRE144_DDL)


@pytest.fixture
def post144_db(tmp_path: Path) -> str:
    """Five base tables plus users/api_tokens/packs. Before #151 this schema
    tripped the fail-closed guard; #151 migrates all eight, so this is now
    the schema that must migrate cleanly with no flag required (see
    TestForwardMainGuard's post144_db_* tests below)."""
    return _make_db(tmp_path / "post144.db", _PRE144_DDL + _AUTH_TABLES_DDL)


# ---------------------------------------------------------------------------
# Forward script (migrate_sqlite_to_pg.py) -- pure table-name enumeration
# helper. Unrelated to the auth-table guard; kept unchanged from before #151.
# ---------------------------------------------------------------------------


class TestSqliteTableNames:
    def test_sqlite_sequence_is_excluded(self, tmp_path: Path) -> None:
        """AUTOINCREMENT columns make SQLite create an internal sqlite_sequence
        table; it must never show up in table enumeration."""
        db = tmp_path / "seq.db"
        _make_db(db, _PRE144_DDL)  # ontology_nodes uses AUTOINCREMENT
        conn = sqlite3.connect(str(db))
        try:
            has_seq = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
            ).fetchone()
        finally:
            conn.close()
        assert has_seq is not None, "test fixture didn't actually create sqlite_sequence"
        names = fwd._sqlite_table_names(str(db))
        assert "sqlite_sequence" not in names
        assert not any(n.startswith("sqlite_") for n in names)


# ---------------------------------------------------------------------------
# #151: the derived fail-closed guard, tested as a pure function. Replaces
# TestForwardTableEnumeration (which tested fwd._auth_tables_present) and
# TestReverseAuthTableFilter (which tested rev._filter_auth_tables) -- both
# hard-coded duplicates of the same set arithmetic that unmigrated_tables now
# does once, for both scripts.
# ---------------------------------------------------------------------------


class TestUnmigratedTablesGuard:
    """``mt.unmigrated_tables(source_tables)`` = owned ∩ source − migrated.

    ``sqlstore_owned_tables()`` is monkeypatched to inject the "owned" side
    without spinning up a real in-memory SQLStore, and ``MIGRATED_TABLES`` is
    monkeypatched where a test needs to simulate a table SQLStore owns that
    the migration does not (yet) cover -- for the *current* schema that set
    is always empty, so exercising the guard's actual firing behaviour
    requires overriding it, same as test_guard_fires_for_unknown_sqlstore_table
    below does end-to-end.
    """

    def test_empty_when_source_has_no_owned_tables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """pre-#144-shaped source: nothing SQLStore owns is missing from
        coverage -- the direct replacement for the old
        test_pre144_db_has_no_auth_tables."""
        monkeypatch.setattr(mt, "sqlstore_owned_tables", lambda: frozenset({"ontology_nodes"}))
        assert mt.unmigrated_tables(["ontology_nodes"]) == []

    def test_owned_table_present_but_not_migrated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replaces test_some_auth_tables_present: a table SQLStore owns and
        that is present in the source, but that no spec covers, is reported."""
        monkeypatch.setattr(
            mt, "sqlstore_owned_tables", lambda: frozenset({"ontology_nodes", "users"})
        )
        monkeypatch.setattr(mt, "MIGRATED_TABLES", frozenset({"ontology_nodes"}))
        assert mt.unmigrated_tables(["ontology_nodes", "users"]) == ["users"]

    def test_multiple_unmigrated_tables_sorted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replaces test_all_auth_tables_present_sorted: the result is
        alphabetically sorted, not source or owned-set order."""
        monkeypatch.setattr(
            mt, "sqlstore_owned_tables", lambda: frozenset({"users", "api_tokens", "packs"})
        )
        monkeypatch.setattr(mt, "MIGRATED_TABLES", frozenset())
        assert mt.unmigrated_tables(["packs", "users", "api_tokens", "rebac_policies"]) == [
            "api_tokens",
            "packs",
            "users",
        ]

    def test_owned_table_absent_from_source_not_flagged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A table SQLStore owns that simply doesn't exist in *this* source
        (never created, e.g. a fresh install) must not be reported -- only
        tables actually present and left uncovered trip the guard."""
        monkeypatch.setattr(
            mt, "sqlstore_owned_tables", lambda: frozenset({"ontology_nodes", "users"})
        )
        monkeypatch.setattr(mt, "MIGRATED_TABLES", frozenset({"ontology_nodes"}))
        assert mt.unmigrated_tables(["ontology_nodes"]) == []

    def test_non_sqlstore_table_in_source_not_flagged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """opencrab.db is not SQLStore-exclusive (node_aliases,
        approval_queue, workflow_runs, action_log all share the file); a
        table like that being present in the source must not false-positive
        just because it's an unrecognised name."""
        monkeypatch.setattr(mt, "sqlstore_owned_tables", lambda: frozenset({"users"}))
        monkeypatch.setattr(mt, "MIGRATED_TABLES", frozenset())
        assert mt.unmigrated_tables(["users", "node_aliases"]) == ["users"]

    def test_accepts_a_set_not_just_a_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replaces test_accepts_a_set_not_just_a_list: both call sites pass
        different container types (a list of dict keys vs. a set built from
        information_schema rows) -- the signature must accept either."""
        monkeypatch.setattr(mt, "sqlstore_owned_tables", lambda: frozenset({"users"}))
        monkeypatch.setattr(mt, "MIGRATED_TABLES", frozenset())
        assert mt.unmigrated_tables({"users"}) == ["users"]


# ---------------------------------------------------------------------------
# Forward script -- end to end via main() (no live PostgreSQL needed: either
# the guard fires before any engine is created, or -- for the missing-file
# and post144 cases below -- migrate_sql never touches the engine at all).
# ---------------------------------------------------------------------------


class TestForwardMainGuard:
    def test_pre144_dry_run_no_crash_five_tables_counted(
        self, pre144_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """This is the exact bug that was reported: counts = {t: _count(src, t)
        for t in tables} used to include users/api_tokens/packs and raised
        OperationalError: no such table: users on a pre-#144 db, before any
        table (including the five real ones) got counted. Reverted copy logic
        must count all five and reach the dry-run PASS with no auth tables to
        report. Unchanged by #151: this is the regression pin for "a database
        predating these tables migrates cleanly"."""
        monkeypatch.setattr(
            sys, "argv", ["migrate_sqlite_to_pg.py", "--sql-db", pre144_db, "--only", "sql", "--dry-run"]
        )
        rc = fwd.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "no such table" not in out.lower()
        for t in ("ontology_nodes", "ontology_edges", "impact_records", "lever_simulations", "rebac_policies"):
            assert t in out
        assert "RESULT: PASS (dry-run, no writes)" in out
        assert "excluded" not in out

    def test_pre144_non_dry_run_reaches_past_counts_stage(
        self, pre144_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Same counts computation as above, but through the non-dry-run
        branch (identical code, executed before the dry_run check) -- proves
        the fix isn't dry-run-only. No live PostgreSQL is available, so this
        points --pg-url at an unassigned local port to get a fast, hermetic
        connection-refused *after* the counts line, instead of hanging or
        reaching out over the network. Unchanged by #151."""
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "migrate_sqlite_to_pg.py",
                "--sql-db",
                pre144_db,
                "--only",
                "sql",
                "--pg-url",
                "postgresql://baduser@127.0.0.1:1/nodb",
            ],
        )
        rc = fwd.main()
        out = capsys.readouterr().out
        assert rc == 1  # fails later at the PG connection, not at counting
        assert "no such table" not in out.lower()
        assert "source rows:" in out  # counts succeeded before the PG failure
        assert "migration failed" in out

    def test_missing_sql_db_file_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A source file that does not exist is not an error. Replaces the old
        unit test against the now-removed fwd._auth_tables_present helper
        (which returned [] for a missing path); the equivalent behaviour is
        now that main() itself exits 0 -- migrate_sql short-circuits before
        touching SQLStore or PostgreSQL when os.path.exists(db_path) is
        False, so this needs no --dry-run and no live PostgreSQL."""
        missing = str(tmp_path / "does_not_exist.db")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "migrate_sqlite_to_pg.py",
                "--sql-db",
                missing,
                "--only",
                "sql",
                "--pg-url",
                "postgresql://baduser@127.0.0.1:1/nodb",
            ],
        )
        rc = fwd.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "source not found, skipping" in out

    def test_post144_db_migrates_without_flag(
        self, post144_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """INVERTED from pre-#151 (was test_post144_db_aborts_without_flag,
        rc==2, "excluded" in output): #151 actually migrates
        users/api_tokens/packs, so a source containing them now needs no
        --allow-unmigrated at all and exits 0 with the three tables appearing
        in the counts."""
        monkeypatch.setattr(
            sys, "argv", ["migrate_sqlite_to_pg.py", "--sql-db", post144_db, "--only", "sql", "--dry-run"]
        )
        rc = fwd.main()
        out = capsys.readouterr().out
        assert rc == 0
        for t in ("users", "api_tokens", "packs"):
            assert t in out
        assert "excluded" not in out

    def test_post144_db_migrates_in_dry_run_too(
        self, post144_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """INVERTED from pre-#151 (was test_post144_db_aborts_in_dry_run_too,
        which asserted "RESULT: PASS" was NOT printed). The guard used to fire
        in --dry-run precisely because a dry run reporting success while
        omitting credentials is the same trap as a real run doing it; now
        that #151 covers the tables for real, --dry-run must reach
        RESULT: PASS the same as a source with no auth tables at all."""
        monkeypatch.setattr(
            sys, "argv", ["migrate_sqlite_to_pg.py", "--sql-db", post144_db, "--only", "sql", "--dry-run"]
        )
        rc = fwd.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "RESULT: PASS" in out

    def test_post144_db_allow_unmigrated_flag_is_now_a_noop(
        self, post144_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """INVERTED from pre-#151 (was test_post144_db_proceeds_with_allow_unmigrated,
        which required the flag to reach rc==0 and asserted "excluded" was
        printed). The flag/plumbing is preserved (#151 design section 1-2),
        but for the current schema there is nothing left for it to exclude:
        passing it changes nothing about the outcome."""
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "migrate_sqlite_to_pg.py",
                "--sql-db",
                post144_db,
                "--only",
                "sql",
                "--dry-run",
                "--allow-unmigrated",
            ],
        )
        rc = fwd.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "RESULT: PASS" in out
        for t in ("users", "api_tokens", "packs"):
            assert t in out
        assert "excluded" not in out.lower()

    def test_guard_fires_for_unknown_sqlstore_table(
        self, post144_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Proves the guard mechanism is still alive for its actual purpose.
        Since the current schema alone can never trip it (all eight tables
        SQLStore owns are migrated), this monkeypatches a hypothetical ninth
        table into sqlstore_owned_tables, creates that table in the source
        fixture, and checks the guard both blocks without --allow-unmigrated
        and proceeds with it -- exactly the #144 behaviour this guard exists
        to preserve for the next table SQLStore grows. Without a test like
        this the derived guard would be untested dead code."""
        conn = sqlite3.connect(post144_db)
        try:
            conn.execute("CREATE TABLE mystery_table (id INTEGER PRIMARY KEY)")
            conn.commit()
        finally:
            conn.close()

        real_owned = mt.sqlstore_owned_tables
        monkeypatch.setattr(mt, "sqlstore_owned_tables", lambda: real_owned() | {"mystery_table"})

        monkeypatch.setattr(
            sys,
            "argv",
            ["migrate_sqlite_to_pg.py", "--sql-db", post144_db, "--only", "sql", "--dry-run"],
        )
        rc = fwd.main()
        out = capsys.readouterr().out
        assert rc == 2
        assert "mystery_table" in out

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "migrate_sqlite_to_pg.py",
                "--sql-db",
                post144_db,
                "--only",
                "sql",
                "--dry-run",
                "--allow-unmigrated",
            ],
        )
        rc = fwd.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "mystery_table" in out
        assert "excluded" in out.lower()
