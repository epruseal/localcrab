"""
#144 fail-closed guard tests for scripts/migrate_sqlite_to_pg.py (forward,
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
(it only re-checks tables already in migrate_sql's own count map). These
tests cover the fail-closed guard added instead: detect the auth tables
before any work happens and require an explicit --allow-unmigrated opt-in.

No live PostgreSQL is available in this environment. Tests that would need
one either stay inside code paths that don't require a live connection
(dry-run; --sql-db counting, which runs identically regardless of dry-run),
or hit a fast local connection-refused (127.0.0.1 unassigned port) to prove
the run gets past the counts stage without live PostgreSQL. The reverse
script's PG-enumeration wrapper (_pg_table_names) is exercised only via its
pure decision function (_filter_auth_tables) with an injected table list, per
instructions; see the module docstring notes below and the test docstrings
for exactly which parts are executed vs. code-reviewed only.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import migrate_sqlite_to_pg as fwd  # noqa: E402
import migrate_to_local as rev  # noqa: E402

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
    """Five base tables plus users/api_tokens/packs -- the schema the guard
    must catch."""
    return _make_db(tmp_path / "post144.db", _PRE144_DDL + _AUTH_TABLES_DDL)


# ---------------------------------------------------------------------------
# Forward script (migrate_sqlite_to_pg.py) -- pure helpers
# ---------------------------------------------------------------------------


class TestForwardTableEnumeration:
    def test_pre144_db_has_no_auth_tables(self, pre144_db: str) -> None:
        assert fwd._auth_tables_present(pre144_db) == []

    def test_post144_db_reports_all_three_sorted(self, post144_db: str) -> None:
        assert fwd._auth_tables_present(post144_db) == ["api_tokens", "packs", "users"]

    def test_missing_db_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert fwd._auth_tables_present(str(tmp_path / "does_not_exist.db")) == []

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
# Forward script -- end to end via main() (no live PostgreSQL needed: the
# guard fires before any engine is created)
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
        report."""
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
        reaching out over the network."""
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

    def test_post144_db_aborts_without_flag(
        self, post144_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            sys, "argv", ["migrate_sqlite_to_pg.py", "--sql-db", post144_db, "--only", "sql", "--dry-run"]
        )
        rc = fwd.main()
        out = capsys.readouterr().out
        assert rc == 2
        for t in ("users", "api_tokens", "packs"):
            assert t in out

    def test_post144_db_aborts_in_dry_run_too(
        self, post144_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A dry run reporting success while omitting credentials is the same
        trap as a real run doing it -- the guard must fire in --dry-run too."""
        monkeypatch.setattr(
            sys, "argv", ["migrate_sqlite_to_pg.py", "--sql-db", post144_db, "--only", "sql", "--dry-run"]
        )
        rc = fwd.main()
        assert rc == 2
        out = capsys.readouterr().out
        assert "RESULT: PASS" not in out  # aborted before any PASS is printed

    def test_post144_db_proceeds_with_allow_unmigrated(
        self, post144_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
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
        assert "excluded" in out.lower()


# ---------------------------------------------------------------------------
# Reverse script (migrate_to_local.py) -- decision logic only, per
# instructions: no live PostgreSQL, so _pg_table_names (the thin
# information_schema.tables wrapper) is code-reviewed, not executed here;
# _filter_auth_tables (the actual decision) is tested directly with an
# injected table list.
# ---------------------------------------------------------------------------


class TestReverseAuthTableFilter:
    def test_no_auth_tables_present(self) -> None:
        assert rev._filter_auth_tables(
            ["ontology_nodes", "ontology_edges", "impact_records", "lever_simulations", "rebac_policies"]
        ) == []

    def test_some_auth_tables_present(self) -> None:
        assert rev._filter_auth_tables(["ontology_nodes", "users", "packs"]) == ["packs", "users"]

    def test_all_auth_tables_present_sorted(self) -> None:
        assert rev._filter_auth_tables(["packs", "users", "api_tokens", "rebac_policies"]) == [
            "api_tokens",
            "packs",
            "users",
        ]

    def test_accepts_a_set_not_just_a_list(self) -> None:
        assert rev._filter_auth_tables({"users"}) == ["users"]
