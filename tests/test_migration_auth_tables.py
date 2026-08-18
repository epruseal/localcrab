"""#151: users/api_tokens/packs migrate in both directions.

#144 added those three tables to SQLStore but left them out of both migration
scripts, guarding the gap with a fail-closed ``--allow-unmigrated`` flag because
no PostgreSQL was available to verify a migration against. One exists now, so
this file both pins the copy itself and pins the properties that made the copy
risky enough to defer: a corrupted value must not be coerced, an outdated column
set must not produce a partial copy, and a token hash must never reach operator
output.

Tests needing PostgreSQL skip without ``OPENCRAB_PG_TEST_URL`` and otherwise run
inside a per-test schema they create and drop, so they never see (or disturb)
anything else in the shared test database.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import _migration_tables as mt  # noqa: E402
import migrate_sqlite_to_pg as fwd  # noqa: E402
import migrate_to_local as rev  # noqa: E402

AUTH_TABLES = ("users", "api_tokens", "packs")

# A value that must never appear in operator output. Distinctive enough that a
# substring search cannot collide with anything else the scripts print.
SECRET_HASH = hashlib.sha256(b"issue-151-never-print-me").hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sqlite_source(path: str, *, tables: tuple[str, ...] | None = None) -> None:
    """Build a source opencrab.db through SQLStore itself, so the schema under
    test is the real one rather than a copy that can drift from it."""
    from opencrab.stores.sql_store import SQLStore

    store = SQLStore(url=f"sqlite:///{path}")
    assert store.available
    store._engine.dispose()
    if tables is not None:
        with sqlite3.connect(path) as conn:
            for spec in mt.SQL_TABLE_SPECS:
                if spec.name not in tables:
                    conn.execute(f"DROP TABLE IF EXISTS {spec.name}")


def _seed_auth_rows(path: str) -> None:
    """Populate the three tables with a matrix chosen to break sloppy copies.

    Every nullable column appears both NULL and non-NULL, an empty string sits
    next to a NULL so the two cannot be confused, timestamps differ per column
    so a transposed copy shows up, and the microsecond value is six digits --
    SQLite renders three-digit subsecond text that a round trip would widen.
    """
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO users (user_id, display_name, is_local, disabled, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("u-local", "Local User", 1, 0, "2026-01-02 03:04:05"),
                ("u-remote", "Remote User", 0, 0, "2026-01-02 03:04:06"),
                ("u-empty", "", 0, 0, "2026-01-02 03:04:07.123456"),
                ("u-disabled", "Disabled User", 0, 1, "2026-01-02 03:04:08"),
            ],
        )
        conn.executemany(
            "INSERT INTO api_tokens "
            "(token_id, user_id, token_hash, name, created_at, last_used_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("t-bare", "u-local", "hash-bare", None, "2026-02-01 00:00:01", None, None),
                (
                    "t-full",
                    "u-remote",
                    "hash-full",
                    "named token",
                    "2026-02-01 00:00:02",
                    "2026-02-02 11:22:33.654321",
                    "2026-02-03 22:33:44",
                ),
                ("t-partial", "u-remote", "hash-partial", "", "2026-02-01 00:00:03", None,
                 "2026-02-04 05:06:07"),
            ],
        )
        conn.executemany(
            "INSERT INTO packs (pack_id, owner_id, visibility, title, description, "
            "forked_from, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("p-bare", "u-local", "private", None, None, None,
                 "2026-03-01 00:00:01", "2026-03-01 00:00:02"),
                ("p-full", "u-remote", "public", "Titled", "Described", "p-bare",
                 "2026-03-02 00:00:03", "2026-03-02 00:00:04.999999"),
                ("p-empty", "u-remote", "private", "", "", None,
                 "2026-03-03 00:00:05", "2026-03-03 00:00:06"),
            ],
        )


def _dump(path: str, table: str, columns: list[str]) -> set[tuple]:
    """Rows as tuples over an explicit column list -- ``SELECT *`` would hide a
    column-order mistake behind a matching set."""
    with sqlite3.connect(path) as conn:
        return set(conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall())


def _cols(spec: mt.SqlTableSpec) -> list[str]:
    return sorted(spec.required_columns)


@pytest.fixture
def pg_schema_dsn() -> Any:
    """A DSN pinned to a throwaway schema via ``search_path``.

    The skip is decided before the schema is created: skipping after would leave
    it behind in the shared database, and ``DROP`` runs from ``finally`` so a
    failing test does not either.
    """
    dsn = os.environ.get("OPENCRAB_PG_TEST_URL")
    if not dsn:
        pytest.skip("OPENCRAB_PG_TEST_URL 미설정 - PG 이관 테스트 스킵")

    from sqlalchemy import create_engine, text

    schema = f"t{uuid.uuid4().hex[:12]}_o151"
    admin = create_engine(dsn)
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    except Exception:  # noqa: BLE001
        admin.dispose()
        pytest.skip(f"PG 테스트 DB 접속 불가: {dsn.rsplit('@', 1)[-1]}")
    sep = "&" if "?" in dsn else "?"
    try:
        yield f"{dsn}{sep}options=-csearch_path%3D{schema}"
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def _pg_rows(dsn: str, table: str, columns: list[str]) -> set[tuple]:
    from sqlalchemy import create_engine, text

    engine = create_engine(dsn)
    try:
        with engine.connect() as conn:
            return set(conn.execute(text(f"SELECT {', '.join(columns)} FROM {table}")).fetchall())
    finally:
        engine.dispose()


def _pg_count(dsn: str, table: str) -> int:
    from sqlalchemy import create_engine, text

    engine = create_engine(dsn)
    try:
        with engine.connect() as conn:
            return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
    finally:
        engine.dispose()


def _run_forward(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["migrate_sqlite_to_pg.py", *argv])
    return fwd.main()


# ---------------------------------------------------------------------------
# Single source of truth
# ---------------------------------------------------------------------------


class TestTableSpecs:
    def test_specs_match_sqlstore_tables(self) -> None:
        """The drift detector. The three lists this issue merged went stale
        silently; measuring SQLStore's real tables means the next table added
        there fails here instead of being dropped by a migration."""
        assert mt.MIGRATED_TABLES == mt.sqlstore_owned_tables()

    def test_drift_reference_is_measured_not_restated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guards the guard: if `sqlstore_owned_tables` ever came from the spec
        list, the comparison above would be true by construction and would never
        detect anything. Emptying the spec list must not change what it reports.
        """
        monkeypatch.setattr(mt, "MIGRATED_TABLES", frozenset())
        monkeypatch.setattr(mt, "SQL_TABLE_SPECS", ())
        assert "users" in mt.sqlstore_owned_tables()

    def test_users_is_copied_before_its_dependents(self) -> None:
        """api_tokens.user_id and packs.owner_id reference users, and
        PostgreSQL enforces that even though SQLite does not."""
        order = [s.name for s in mt.SQL_TABLE_SPECS]
        assert order.index("users") < order.index("api_tokens")
        assert order.index("users") < order.index("packs")

    def test_token_hash_is_never_a_natural_key(self) -> None:
        """safe_error_text prints ``key=``, so a conflict_key must not be a
        secret."""
        for spec in mt.SQL_TABLE_SPECS:
            assert "token_hash" not in (spec.conflict_key or ())

    def test_no_hardcoded_table_lists_remain(self) -> None:
        """Pins the removal itself: the point of #151 is that these lists exist
        once, so a reintroduced literal is a regression even if tests pass."""
        for script in ("migrate_sqlite_to_pg.py", "migrate_to_local.py"):
            source = (SCRIPTS_DIR / script).read_text(encoding="utf-8")
            assert "_AUTH_TABLES" not in source, script
            assert '"lever_simulations", "rebac_policies"' not in source, script
            assert "'lever_simulations', 'rebac_policies'" not in source, script


# ---------------------------------------------------------------------------
# Column validation
# ---------------------------------------------------------------------------


class TestColumnResolution:
    def test_stale_source_column_set_is_rejected(self) -> None:
        """A packs table predating #146 has no owner_id. Copying what overlaps
        would look like success while dropping ownership."""
        spec = mt.SPEC_BY_NAME["packs"]
        stale = [c for c in sorted(spec.required_columns) if c != "owner_id"]
        with pytest.raises(mt.MigrationError) as excinfo:
            mt.resolve_columns(spec, stale, sorted(spec.required_columns))
        assert "owner_id" in str(excinfo.value)
        assert "packs" in str(excinfo.value)

    def test_stale_target_column_set_is_rejected(self) -> None:
        spec = mt.SPEC_BY_NAME["packs"]
        stale = [c for c in sorted(spec.required_columns) if c != "visibility"]
        with pytest.raises(mt.MigrationError) as excinfo:
            mt.resolve_columns(spec, sorted(spec.required_columns), stale)
        assert "visibility" in str(excinfo.value)

    def test_source_only_column_is_rejected(self) -> None:
        """Asymmetric on purpose: this direction loses data."""
        spec = mt.SPEC_BY_NAME["users"]
        src = [*sorted(spec.required_columns), "extra_col"]
        with pytest.raises(mt.MigrationError) as excinfo:
            mt.resolve_columns(spec, src, sorted(spec.required_columns))
        assert "extra_col" in str(excinfo.value)

    def test_target_only_column_is_allowed(self) -> None:
        """The other direction is filled by the target's default -- an older
        SQLite migrating into a newer PostgreSQL must keep working."""
        spec = mt.SPEC_BY_NAME["users"]
        src = sorted(spec.required_columns)
        cols = mt.resolve_columns(spec, src, [*src, "added_later"])
        assert "added_later" not in cols
        assert set(cols) == spec.required_columns

    def test_surrogate_id_is_never_copied(self) -> None:
        """An explicit id does not advance a PostgreSQL SERIAL, so the first
        application write after the migration would collide."""
        spec = mt.SPEC_BY_NAME["ontology_nodes"]
        cols = mt.resolve_columns(
            spec, ["id", *sorted(spec.required_columns)], ["id", *sorted(spec.required_columns)]
        )
        assert "id" not in cols

    def test_copy_column_order_follows_the_source(self) -> None:
        spec = mt.SPEC_BY_NAME["users"]
        src = ["created_at", "user_id", "disabled", "display_name", "is_local"]
        assert mt.resolve_columns(spec, src, sorted(spec.required_columns)) == src


# ---------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------


class TestValueConversion:
    @pytest.mark.parametrize("bad", [2, -1, 7])
    def test_corrupt_boolean_is_refused_not_coerced(self, bad: int) -> None:
        """rebac_policies.granted has no CHECK constraint, so a real database
        can hold this today; the previous bool() call turned it into True."""
        with pytest.raises(mt.MigrationError) as excinfo:
            mt.to_pg_bool(bad, table="rebac_policies", column="granted", key="s,p,r")
        assert str(bad) in str(excinfo.value)

    def test_boolean_error_does_not_echo_a_long_value(self) -> None:
        """SQLite is dynamically typed, so a 'boolean' column can hold a
        secret-length string; only int/bool are safe to print back."""
        with pytest.raises(mt.MigrationError) as excinfo:
            mt.to_pg_bool(SECRET_HASH, table="users", column="is_local", key="u-1")
        assert SECRET_HASH not in str(excinfo.value)
        assert SECRET_HASH[:16] not in str(excinfo.value)
        assert "str" in str(excinfo.value)

    @pytest.mark.parametrize("good", [0, 1, True, False, None])
    def test_valid_booleans_pass(self, good: Any) -> None:
        out = mt.to_pg_bool(good, table="users", column="is_local", key="u-1")
        assert out is None if good is None else isinstance(out, bool)

    @pytest.mark.parametrize(
        "text_value",
        ["2026-08-18 12:34:56", "2026-08-18 12:34:56.123456", "2026-08-18 12:34:56+00:00", None],
    )
    def test_accepted_timestamp_text(self, text_value: Any) -> None:
        """The ``+00:00`` spelling is what the reverse script wrote before this
        change, so rejecting it would strand existing local databases."""
        mt.check_sqlite_timestamp(text_value, table="users", column="created_at", key="u-1")

    def test_unparseable_timestamp_is_refused_before_the_write(self) -> None:
        with pytest.raises(mt.MigrationError) as excinfo:
            mt.check_sqlite_timestamp(
                SECRET_HASH, table="api_tokens", column="last_used_at", key="t-1"
            )
        assert SECRET_HASH not in str(excinfo.value)
        assert "last_used_at" in str(excinfo.value)

    def test_timestamp_serialises_to_sqlite_spelling(self) -> None:
        """Binding psycopg2's tz-aware datetime straight into TEXT yields
        ``+00:00``, which is not what datetime('now') writes -- so a round trip
        did not come back identical."""
        from datetime import UTC, datetime, timedelta, timezone

        assert (
            mt.to_sqlite_timestamp(datetime(2026, 8, 18, 12, 34, 56, tzinfo=UTC))
            == "2026-08-18 12:34:56"
        )
        assert (
            mt.to_sqlite_timestamp(datetime(2026, 8, 18, 12, 34, 56, 123456, tzinfo=UTC))
            == "2026-08-18 12:34:56.123456"
        )
        assert mt.to_sqlite_timestamp(None) is None
        seoul = datetime(2026, 8, 18, 21, 34, 56, tzinfo=timezone(timedelta(hours=9)))
        assert mt.to_sqlite_timestamp(seoul) == "2026-08-18 12:34:56"


# ---------------------------------------------------------------------------
# Operator-facing error text
# ---------------------------------------------------------------------------


class _StubDiag:
    """psycopg2's diag, with every free-text field poisoned.

    Three narrower redaction rules shipped and leaked in review: suppressing
    bind parameters still let ``DETAIL: Key (token_hash)=(...)`` through,
    dropping DETAIL/HINT/CONTEXT still let the ``LINE 1: ...`` context line
    through, and keeping the first line still leaked because a syntax error
    quotes the offending literal inside it. Emitting any of these fields is the
    shape every one of those bugs had.
    """

    sqlstate = "23505"
    constraint_name = "api_tokens_token_hash_key"
    table_name = "api_tokens"
    column_name = "token_hash"
    message_primary = f'duplicate key value violates unique constraint "{SECRET_HASH}"'
    message_detail = f"Key (token_hash)=({SECRET_HASH}) already exists."
    message_hint = SECRET_HASH
    context = SECRET_HASH


class _StubOrigError(Exception):
    diag = _StubDiag()


class _StubWrapperError(Exception):
    def __init__(self) -> None:
        super().__init__(f"wrapped: {SECRET_HASH}")
        self.orig = _StubOrigError(f"orig: {SECRET_HASH}")


class TestSafeErrorText:
    def test_stub_diag_output_is_exactly_the_identifiers(self) -> None:
        """Exact equality, not just absence of the secret: an implementation
        that appends one more diag field passes an absence check on a synthetic
        exception (whose free-text fields are empty in real life) and only
        leaks on a class of error the test suite does not provoke."""
        assert mt.safe_error_text(_StubWrapperError(), table="api_tokens", key="t-full") == (
            "_StubOrigError [23505] constraint=api_tokens_token_hash_key "
            "pg_table=api_tokens pg_column=token_hash table=api_tokens key=t-full"
        )

    @pytest.mark.parametrize(
        "exc",
        [
            _StubWrapperError(),
            RuntimeError(f"DETAIL:  Key (token_hash)=({SECRET_HASH}) already exists."),
            RuntimeError(f'syntax error at or near "{SECRET_HASH}"'),
            RuntimeError(f"line one\nDETAIL:  Failing row contains ({SECRET_HASH},\nmore)"),
            RuntimeError(f"LINE 1: ...VALUES ('{SECRET_HASH}')\n                  ^"),
            RuntimeError(f"Not a boolean value: '{SECRET_HASH}'"),
            RuntimeError(f"Could not decode to UTF-8 column 'x' with text '{SECRET_HASH}'"),
            RuntimeError(""),
            ValueError(),
        ],
    )
    def test_no_driver_text_ever_survives(self, exc: Exception) -> None:
        out = mt.safe_error_text(exc, table="api_tokens", key="t-full")
        assert SECRET_HASH not in out
        assert SECRET_HASH[:16] not in out
        assert type(exc).__name__ in out or "_StubOrigError" in out

    @pytest.mark.parametrize(
        ("sqlstate", "column"),
        [("23502", "user_id"), ("23503", "user_id"), ("22007", None), ("42804", "is_local")],
    )
    def test_other_sqlstates_emit_identifiers_only(
        self, sqlstate: str, column: str | None
    ) -> None:
        """23505 alone is not enough coverage: an implementation that adds a
        free-text diag field only for one other sqlstate would pass a
        23505-only suite while leaking. A not-null violation is the worst case
        -- its DETAIL is the entire failing row."""

        class _Diag(_StubDiag):
            pass

        _Diag.sqlstate = sqlstate
        _Diag.column_name = column
        _Diag.constraint_name = None

        class _OrigError(Exception):
            diag = _Diag()

        class _WrappedError(Exception):
            orig = _OrigError()

        out = mt.safe_error_text(_WrappedError(), table="api_tokens", key="t-full")
        assert SECRET_HASH not in out
        assert SECRET_HASH[:16] not in out
        assert sqlstate in out

    def test_migration_error_passes_through(self) -> None:
        """Provenance, not content: this class's message is contractually built
        from identifiers, and swallowing it would leave the operator with a bare
        exception name for every schema and corruption failure."""
        err = mt.MigrationError("source table packs: required column(s) ['owner_id'] missing")
        assert mt.safe_error_text(err) == str(err)

    def test_known_constraint_gets_remedy_text(self) -> None:
        """The sanitizer can surface a constraint name but cannot invent the fix
        for it, so the guidance is a static mapping. An upsert on user_id cannot
        absorb a partial-index conflict, so this is the one failure an operator
        cannot diagnose from the constraint name alone."""

        class _Diag(_StubDiag):
            constraint_name = "idx_users_single_local"

        class _OrigError(Exception):
            diag = _Diag()

        class _WrappedError(Exception):
            orig = _OrigError()

        out = mt.safe_error_text(_WrappedError(), table="users")
        assert mt.CONSTRAINT_REMEDIES["idx_users_single_local"] in out
        assert SECRET_HASH not in out

    def test_unknown_constraint_gets_no_remedy(self) -> None:
        assert mt.CONSTRAINT_REMEDIES["idx_users_single_local"] not in mt.safe_error_text(
            _StubWrapperError()
        )


# ---------------------------------------------------------------------------
# Forward: absent tables (no PostgreSQL needed)
# ---------------------------------------------------------------------------


class TestForwardAbsentTables:
    def test_pre144_source_dry_run_reports_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The counts used to be taken over a fixed list before the dry-run
        branch, so a database predating these tables died on 'no such table'."""
        db = str(tmp_path / "opencrab.db")
        _sqlite_source(db, tables=tuple(s.name for s in mt.SQL_TABLE_SPECS if s.name not in AUTH_TABLES))

        rc = _run_forward(monkeypatch, "--sql-db", db, "--only", "sql", "--dry-run")
        out = capsys.readouterr().out
        assert rc == 0
        assert "no such table" not in out.lower()
        for table in AUTH_TABLES:
            assert table in out

    def test_full_source_needs_no_allow_unmigrated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The #144 guard existed because these three were not copied. They are
        now, so it must not fire for them."""
        db = str(tmp_path / "opencrab.db")
        _sqlite_source(db)
        _seed_auth_rows(db)

        rc = _run_forward(monkeypatch, "--sql-db", db, "--only", "sql", "--dry-run")
        out = capsys.readouterr().out
        assert rc == 0
        assert "RESULT: PASS" in out
        assert "excluded" not in out.lower()


# ---------------------------------------------------------------------------
# Forward: real PostgreSQL
# ---------------------------------------------------------------------------


class TestForwardAgainstPostgres:
    def test_auth_rows_are_copied_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str
    ) -> None:
        db = str(tmp_path / "opencrab.db")
        _sqlite_source(db)
        _seed_auth_rows(db)

        rc = _run_forward(
            monkeypatch, "--sql-db", db, "--only", "sql", "--pg-url", pg_schema_dsn, "--verify"
        )
        assert rc == 0
        for table in AUTH_TABLES:
            cols = _cols(mt.SPEC_BY_NAME[table])
            source = _dump(db, table, cols)
            target = {
                tuple(mt.to_sqlite_timestamp(v) for v in row)
                for row in _pg_rows(pg_schema_dsn, table, cols)
            }
            # is_local/disabled/granted come back as bool from PostgreSQL.
            target = {tuple(int(v) if isinstance(v, bool) else v for v in row) for row in target}
            assert target == source, table

    def test_round_trip_is_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str
    ) -> None:
        """SQLite -> PostgreSQL -> SQLite. Row preservation is the acceptance
        criterion, and only an exact comparison catches a dropped column that a
        DEFAULT NOW() on the target would otherwise paper over."""
        import logging

        src_db = str(tmp_path / "source.db")
        _sqlite_source(src_db)
        _seed_auth_rows(src_db)
        assert _run_forward(
            monkeypatch, "--sql-db", src_db, "--only", "sql", "--pg-url", pg_schema_dsn
        ) == 0

        back_db = str(tmp_path / "back.db")
        result = rev.migrate_sql(pg_schema_dsn, back_db, logging.getLogger(__name__))

        for table in AUTH_TABLES:
            cols = _cols(mt.SPEC_BY_NAME[table])
            assert _dump(back_db, table, cols) == _dump(src_db, table, cols), table
            stats = result["tables"][table]
            assert stats["target"] == stats["source"], table

    def test_corrupt_boolean_aborts_before_any_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str
    ) -> None:
        """The corruption sits in the last table copied, with rows in every
        earlier one: without a scan that precedes the first write, the auth
        tables would already be committed when it is found."""
        db = str(tmp_path / "opencrab.db")
        _sqlite_source(db)
        _seed_auth_rows(db)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO rebac_policies (subject_id, permission, resource_id, granted, "
                "created_at) VALUES ('s', 'p', 'r', 2, '2026-04-01 00:00:00')"
            )

        rc = _run_forward(monkeypatch, "--sql-db", db, "--only", "sql", "--pg-url", pg_schema_dsn)
        assert rc != 0
        for spec in mt.SQL_TABLE_SPECS:
            assert _pg_count(pg_schema_dsn, spec.name) == 0, spec.name

    def test_corrupt_boolean_detection_is_not_hardcoded_to_granted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str
    ) -> None:
        """users.is_local carries the same risk, and deriving the boolean set
        from the PostgreSQL catalogue is what makes both cases covered. The
        current SQLite DDL has a CHECK, so the fixture is built without it --
        which is exactly the shape of a database created before that CHECK."""
        db = str(tmp_path / "opencrab.db")
        _sqlite_source(db)
        _seed_auth_rows(db)
        with sqlite3.connect(db) as conn:
            conn.execute("DROP TABLE users")
            conn.execute(
                "CREATE TABLE users (user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, "
                "is_local INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0, "
                "created_at TEXT)"
            )
            conn.execute(
                "INSERT INTO users VALUES ('u-bad', 'Bad', 2, 0, '2026-01-01 00:00:00')"
            )

        rc = _run_forward(monkeypatch, "--sql-db", db, "--only", "sql", "--pg-url", pg_schema_dsn)
        assert rc != 0
        assert _pg_count(pg_schema_dsn, "users") == 0

    def test_corrupt_timestamp_aborts_before_any_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """PostgreSQL's own complaint about unparseable timestamp text quotes
        the cell in its primary message, mid-run."""
        db = str(tmp_path / "opencrab.db")
        _sqlite_source(db)
        _seed_auth_rows(db)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE token_id = 't-bare'",
                         (SECRET_HASH,))

        rc = _run_forward(monkeypatch, "--sql-db", db, "--only", "sql", "--pg-url", pg_schema_dsn)
        out = capsys.readouterr().out
        assert rc != 0
        assert SECRET_HASH not in out
        assert _pg_count(pg_schema_dsn, "api_tokens") == 0

    def test_token_hash_is_absent_from_failure_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """PostgreSQL reports a unique violation with
        ``DETAIL: Key (token_hash)=(...)``, which survives parameter hiding."""
        from sqlalchemy import create_engine, text

        db = str(tmp_path / "opencrab.db")
        _sqlite_source(db)
        _seed_auth_rows(db)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE api_tokens SET token_hash = ? WHERE token_id = 't-bare'",
                         (SECRET_HASH,))

        # Occupy the hash under a different token_id, so ON CONFLICT (token_id)
        # cannot absorb it and the unique index on token_hash is what fails.
        engine = create_engine(pg_schema_dsn)
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE users (user_id VARCHAR(64) PRIMARY KEY, "
                "display_name VARCHAR(256) NOT NULL, is_local BOOLEAN NOT NULL DEFAULT FALSE, "
                "disabled BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ)"
            ))
            conn.execute(text(
                "CREATE TABLE api_tokens (token_id VARCHAR(64) PRIMARY KEY, "
                "user_id VARCHAR(64) NOT NULL REFERENCES users (user_id), "
                "token_hash VARCHAR(64) NOT NULL UNIQUE, name VARCHAR(256), "
                "created_at TIMESTAMPTZ, last_used_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ)"
            ))
            conn.execute(text(
                "INSERT INTO users (user_id, display_name) VALUES ('u-squatter', 'Squatter')"
            ))
            conn.execute(text(
                "INSERT INTO api_tokens (token_id, user_id, token_hash) "
                "VALUES ('t-squatter', 'u-squatter', :h)"
            ), {"h": SECRET_HASH})
        engine.dispose()

        rc = _run_forward(monkeypatch, "--sql-db", db, "--only", "sql", "--pg-url", pg_schema_dsn)
        captured = capsys.readouterr()
        assert rc != 0
        for stream in (captured.out, captured.err):
            assert SECRET_HASH not in stream
            assert SECRET_HASH[:16] not in stream
        assert "migration failed" in captured.out

    def test_pre144_source_migrates_and_verifies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """The real-execution counterpart of the dry-run case: absent must stay
        out of the copy stage and out of --verify's comparison, not only out of
        the count print."""
        db = str(tmp_path / "opencrab.db")
        _sqlite_source(db, tables=tuple(s.name for s in mt.SQL_TABLE_SPECS if s.name not in AUTH_TABLES))
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO ontology_nodes (space, node_type, node_id) VALUES ('s', 'T', 'n1')"
            )

        rc = _run_forward(
            monkeypatch, "--sql-db", db, "--only", "sql", "--pg-url", pg_schema_dsn, "--verify"
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "RESULT: PASS" in out
        assert "MISMATCH" not in out
        assert _pg_count(pg_schema_dsn, "ontology_nodes") == 1

    def test_verify_marks_absent_tables_skip_not_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """--verify assumed every source count was an integer. Mapping absent to
        0 instead of skipping it would still pass a run where the target is also
        empty, so the target is given a row first: only a real skip survives it.
        """
        from sqlalchemy import create_engine, text

        db = str(tmp_path / "opencrab.db")
        _sqlite_source(db, tables=tuple(s.name for s in mt.SQL_TABLE_SPECS if s.name not in AUTH_TABLES))

        engine = create_engine(pg_schema_dsn)
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE users (user_id VARCHAR(64) PRIMARY KEY, "
                "display_name VARCHAR(256) NOT NULL, is_local BOOLEAN NOT NULL DEFAULT FALSE, "
                "disabled BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ)"
            ))
            conn.execute(text(
                "INSERT INTO users (user_id, display_name) VALUES ('u-preexisting', 'Kept')"
            ))
        engine.dispose()

        rc = _run_forward(
            monkeypatch, "--sql-db", db, "--only", "sql", "--pg-url", pg_schema_dsn, "--verify"
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "RESULT: PASS" in out
        assert "MISMATCH" not in out
        assert "[SKIP]" in out
        # An absent source table over a non-empty target is not a migration
        # failure, but it is not something to pass over in silence either.
        assert "target already has" in out
        assert _pg_count(pg_schema_dsn, "users") == 1

    def test_source_file_is_not_modified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str
    ) -> None:
        """The forward script's documented contract is that it only ever reads
        the source, which is why it has no --backup-to flag."""
        db = str(tmp_path / "opencrab.db")
        _sqlite_source(db)
        _seed_auth_rows(db)
        before = hashlib.sha256(Path(db).read_bytes()).hexdigest()

        assert _run_forward(
            monkeypatch, "--sql-db", db, "--only", "sql", "--pg-url", pg_schema_dsn
        ) == 0
        assert hashlib.sha256(Path(db).read_bytes()).hexdigest() == before


# ---------------------------------------------------------------------------
# Reverse: real row preservation
# ---------------------------------------------------------------------------


class TestReverseAgainstPostgres:
    def test_missing_rows_are_reported_not_counted_as_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str
    ) -> None:
        """The old return value counted inserts, so a row rejected by the target
        looked identical to a re-run that had nothing to do. A pre-created table
        with a CHECK makes one row fail; SQLStore's CREATE IF NOT EXISTS leaves
        that table in place."""
        import logging

        src_db = str(tmp_path / "source.db")
        _sqlite_source(src_db)
        _seed_auth_rows(src_db)
        assert _run_forward(
            monkeypatch, "--sql-db", src_db, "--only", "sql", "--pg-url", pg_schema_dsn
        ) == 0

        back_db = str(tmp_path / "back.db")
        with sqlite3.connect(back_db) as conn:
            conn.execute(
                "CREATE TABLE users (user_id TEXT PRIMARY KEY CHECK (user_id <> 'u-local'), "
                "display_name TEXT NOT NULL, is_local INTEGER NOT NULL DEFAULT 0, "
                "disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT)"
            )

        result = rev.migrate_sql(pg_schema_dsn, back_db, logging.getLogger(__name__))
        stats = result["tables"]["users"]
        assert stats["target"] < stats["source"]
        assert rev._row_preservation_mismatches(result) == ["users"]

    def test_rerun_preserves_rows_without_recopying(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str
    ) -> None:
        """A second run rewrites the same rows rather than skipping them: the
        natural-key tables upsert, so the source stays authoritative instead of
        a stale local row surviving. What must not change is the row count."""
        import logging

        src_db = str(tmp_path / "source.db")
        _sqlite_source(src_db)
        _seed_auth_rows(src_db)
        assert _run_forward(
            monkeypatch, "--sql-db", src_db, "--only", "sql", "--pg-url", pg_schema_dsn
        ) == 0

        back_db = str(tmp_path / "back.db")
        log = logging.getLogger(__name__)
        first = rev.migrate_sql(pg_schema_dsn, back_db, log)
        second = rev.migrate_sql(pg_schema_dsn, back_db, log)

        for table in AUTH_TABLES:
            assert first["tables"][table]["copied"] > 0, table
            assert second["tables"][table]["copied"] == second["tables"][table]["source"], table
            assert second["tables"][table]["target"] == first["tables"][table]["target"], table
            assert second["tables"][table]["failed_rows"] == 0, table
        assert rev._row_preservation_mismatches(second) == []

    def test_row_failure_log_carries_no_driver_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """SQLite names constraints rather than values, so asserting the secret
        is absent would pass with the sanitizer removed. Assert the sanitized
        shape and the absence of the driver's own wording instead.

        The failure is raised from a trigger because ``INSERT OR IGNORE``
        swallows ordinary constraint violations -- which is why silent row loss
        is caught by the post-copy re-count rather than by this log at all.
        """
        import logging

        src_db = str(tmp_path / "source.db")
        _sqlite_source(src_db)
        _seed_auth_rows(src_db)
        with sqlite3.connect(src_db) as conn:
            conn.execute("UPDATE api_tokens SET token_hash = ? WHERE token_id = 't-bare'",
                         (SECRET_HASH,))
        assert _run_forward(
            monkeypatch, "--sql-db", src_db, "--only", "sql", "--pg-url", pg_schema_dsn
        ) == 0

        back_db = str(tmp_path / "back.db")
        with sqlite3.connect(back_db) as conn:
            conn.execute(
                "CREATE TABLE api_tokens (token_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
                "token_hash TEXT NOT NULL, name TEXT, created_at TEXT, last_used_at TEXT, "
                "revoked_at TEXT)"
            )
            conn.execute(
                "CREATE TRIGGER block_tokens BEFORE INSERT ON api_tokens "
                "BEGIN SELECT RAISE(ABORT, 'driver-wording-marker'); END"
            )

        with caplog.at_level(logging.WARNING):
            result = rev.migrate_sql(pg_schema_dsn, back_db, logging.getLogger(__name__))

        assert caplog.text, "expected a row-level failure warning"
        assert SECRET_HASH not in caplog.text
        assert "driver-wording-marker" not in caplog.text
        assert "table=api_tokens" in caplog.text
        assert "key=t-bare" in caplog.text
        assert result["tables"]["api_tokens"]["target"] == 0

    def test_main_exits_nonzero_on_row_loss(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str
    ) -> None:
        """main() returned None and the entrypoint discarded it, so "the reverse
        migration fails loudly" was unreachable no matter what migrate_sql
        found. preflight needs Neo4j/Mongo/Chroma, so it is stubbed to reach the
        SQL step -- the exit path itself is what is under test."""
        from sqlalchemy import create_engine

        src_db = str(tmp_path / "source.db")
        _sqlite_source(src_db)
        _seed_auth_rows(src_db)
        assert _run_forward(
            monkeypatch, "--sql-db", src_db, "--only", "sql", "--pg-url", pg_schema_dsn
        ) == 0

        data_dir = tmp_path / "local"
        data_dir.mkdir()
        with sqlite3.connect(str(data_dir / "opencrab.db")) as conn:
            conn.execute(
                "CREATE TABLE users (user_id TEXT PRIMARY KEY CHECK (user_id <> 'u-local'), "
                "display_name TEXT NOT NULL, is_local INTEGER NOT NULL DEFAULT 0, "
                "disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT)"
            )

        engine = create_engine(pg_schema_dsn)
        monkeypatch.setattr(
            rev, "preflight", lambda _args: {"counts": {}, "pg_engine": engine}
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "migrate_to_local.py",
                "--skip-graph",
                "--skip-docs",
                "--skip-vectors",
                "--local-data-dir",
                str(data_dir),
                "--pg-url",
                pg_schema_dsn,
            ],
        )
        try:
            assert rev.main() == 5
        finally:
            engine.dispose()

    def test_main_sanitises_an_escaping_exception(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """An uncaught exception prints its __cause__ chain, so the driver text a
        MigrationError was raised *from* would reach stderr anyway."""

        def _boom(_args: object) -> int:
            try:
                raise RuntimeError(f"DETAIL:  Key (token_hash)=({SECRET_HASH}) already exists.")
            except RuntimeError as exc:
                raise mt.MigrationError("table users: copy failed") from exc

        monkeypatch.setattr(rev, "_run", _boom)
        monkeypatch.setattr(sys, "argv", ["migrate_to_local.py", "--dry-run"])
        assert rev.main() == 1
        captured = capsys.readouterr()
        for stream in (captured.out, captured.err):
            assert SECRET_HASH not in stream
            assert SECRET_HASH[:16] not in stream
        assert "table users: copy failed" in captured.out


# ---------------------------------------------------------------------------
# Review findings on PR #203
# ---------------------------------------------------------------------------


class TestIndependentlyInitialisedTarget:
    """A target that already holds its own rows is the case where equal row
    counts stop meaning equal rows -- and for these tables an unnoticed
    substitution means the wrong credentials survive."""

    def test_forward_does_not_skip_a_same_sized_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str
    ) -> None:
        """One source user, one different target user: the count matched, so the
        copy was skipped entirely and --verify agreed."""
        from sqlalchemy import create_engine, text

        db = str(tmp_path / "opencrab.db")
        _sqlite_source(db)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO users (user_id, display_name, is_local, disabled, created_at) "
                "VALUES ('u-source', 'From Source', 0, 0, '2026-01-01 00:00:00')"
            )

        engine = create_engine(pg_schema_dsn)
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE users (user_id VARCHAR(64) PRIMARY KEY, "
                "display_name VARCHAR(256) NOT NULL, is_local BOOLEAN NOT NULL DEFAULT FALSE, "
                "disabled BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ)"
            ))
            # is_local=0 deliberately: two local users would collide on
            # idx_users_single_local and fail this test for the wrong reason.
            conn.execute(text(
                "INSERT INTO users (user_id, display_name) VALUES ('u-target', 'Already Here')"
            ))
        engine.dispose()

        rc = _run_forward(
            monkeypatch, "--sql-db", db, "--only", "sql", "--pg-url", pg_schema_dsn, "--verify"
        )
        assert rc == 0
        assert _pg_rows(pg_schema_dsn, "users", ["user_id"]) == {("u-source",), ("u-target",)}

    def test_reverse_reports_a_row_dropped_by_another_constraint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str
    ) -> None:
        """INSERT OR IGNORE discards a row for any constraint, not just the
        conflict key, and says nothing. One local user on each side keeps the
        counts equal, so only a key comparison notices the source user is gone.
        """
        import logging

        src_db = str(tmp_path / "source.db")
        _sqlite_source(src_db)
        with sqlite3.connect(src_db) as conn:
            conn.execute(
                "INSERT INTO users (user_id, display_name, is_local, disabled, created_at) "
                "VALUES ('u-source-local', 'Source Local', 1, 0, '2026-01-01 00:00:00')"
            )
        assert _run_forward(
            monkeypatch, "--sql-db", src_db, "--only", "sql", "--pg-url", pg_schema_dsn
        ) == 0

        back_db = str(tmp_path / "back.db")
        _sqlite_source(back_db)
        with sqlite3.connect(back_db) as conn:
            conn.execute(
                "INSERT INTO users (user_id, display_name, is_local, disabled, created_at) "
                "VALUES ('u-target-local', 'Target Local', 1, 0, '2026-01-01 00:00:00')"
            )

        result = rev.migrate_sql(pg_schema_dsn, back_db, logging.getLogger(__name__))
        stats = result["tables"]["users"]
        assert stats["target"] == stats["source"] == 1, "the count must match, or this proves nothing"
        assert stats["missing_keys"] == 1
        assert rev._row_preservation_mismatches(result) == ["users"]
        # The summary and the exit code must not disagree: judging the table by
        # count alone renders it green while the run exits 5, pointing the
        # operator at numbers that do not explain the failure.
        assert "MISMATCH" in rev._sql_status(stats)
        assert "missing_keys" in rev._sql_status(stats)


class TestPreflightCounts:
    def test_absent_table_does_not_zero_the_rest(self, pg_schema_dsn: str) -> None:
        """PostgreSQL aborts a transaction on a failed statement, so counting
        table by table inside one connection turns the first missing table into
        zeros for everything after it. Ordering `users` first made a pre-#144
        source report its existing ontology rows as empty."""
        from sqlalchemy import create_engine, text

        engine = create_engine(pg_schema_dsn)
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "CREATE TABLE ontology_nodes (id SERIAL PRIMARY KEY, space VARCHAR(64), "
                    "node_type VARCHAR(64), node_id VARCHAR(256), created_at TIMESTAMPTZ, "
                    "updated_at TIMESTAMPTZ)"
                ))
                conn.execute(text(
                    "INSERT INTO ontology_nodes (space, node_type, node_id) "
                    "VALUES ('s', 'T', 'n1')"
                ))
            counts, absent = rev._pg_sql_counts(engine)
        finally:
            engine.dispose()

        assert counts["ontology_nodes"] == 1
        assert set(AUTH_TABLES).issubset(absent)
        # Absent stays out of the counts entirely: a marker value would break
        # the caller's sum() and thousands formatting, and would not be
        # distinguishable from a table that is present and empty.
        for table in AUTH_TABLES:
            assert table not in counts


class TestStaleLocalAuthState:
    """The reverse script migrates PostgreSQL into local mode -- the source is
    authoritative. Leaving an existing local row alone means a credential
    revoked in PostgreSQL keeps working locally, which `verify_token` cannot
    tell apart from a live one (it matches on `revoked_at IS NULL` and
    `disabled`)."""

    def test_existing_rows_are_refreshed_from_the_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str
    ) -> None:
        import logging

        src_db = str(tmp_path / "source.db")
        _sqlite_source(src_db)
        _seed_auth_rows(src_db)
        # Revoke a token and disable a user in what will become the PG source.
        with sqlite3.connect(src_db) as conn:
            conn.execute(
                "UPDATE api_tokens SET revoked_at = '2026-05-01 00:00:00' WHERE token_id = 't-bare'"
            )
            conn.execute("UPDATE users SET disabled = 1 WHERE user_id = 'u-remote'")
        assert _run_forward(
            monkeypatch, "--sql-db", src_db, "--only", "sql", "--pg-url", pg_schema_dsn
        ) == 0

        # The local database still holds the pre-revocation state.
        back_db = str(tmp_path / "back.db")
        _sqlite_source(back_db)
        _seed_auth_rows(back_db)
        assert _dump(back_db, "api_tokens", ["token_id", "revoked_at"]) != _dump(
            src_db, "api_tokens", ["token_id", "revoked_at"]
        ), "the fixture must start out stale, or this proves nothing"

        result = rev.migrate_sql(pg_schema_dsn, back_db, logging.getLogger(__name__))

        # Full rows, not just the two columns that motivated this: an
        # implementation that refreshed only revoked_at/disabled would pass a
        # narrower assertion while leaving every other column stale.
        for table in AUTH_TABLES:
            cols = _cols(mt.SPEC_BY_NAME[table])
            assert _dump(back_db, table, cols) == _dump(src_db, table, cols), table
            assert result["tables"][table]["failed_rows"] == 0, table
        assert rev._row_preservation_mismatches(result) == []

    def test_a_failed_update_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_schema_dsn: str
    ) -> None:
        """An upsert whose UPDATE is rejected leaves the row stale, and the key
        is already in the target, so the key comparison cannot see it. Without
        counting the failure this is the same silent staleness by another
        route."""
        import logging

        src_db = str(tmp_path / "source.db")
        _sqlite_source(src_db)
        _seed_auth_rows(src_db)
        assert _run_forward(
            monkeypatch, "--sql-db", src_db, "--only", "sql", "--pg-url", pg_schema_dsn
        ) == 0

        back_db = str(tmp_path / "back.db")
        _sqlite_source(back_db)
        _seed_auth_rows(back_db)
        with sqlite3.connect(back_db) as conn:
            conn.execute(
                "CREATE TRIGGER block_user_update BEFORE UPDATE ON users "
                "WHEN NEW.user_id = 'u-local' BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )

        result = rev.migrate_sql(pg_schema_dsn, back_db, logging.getLogger(__name__))
        stats = result["tables"]["users"]
        assert stats["missing_keys"] == 0, "the key is present, so only failed_rows can catch this"
        assert stats["failed_rows"] == 1
        assert rev._row_preservation_mismatches(result) == ["users"]
        assert "failed_rows" in rev._sql_status(stats)
